"""Bug-condition + preservation tests: preflights verify a VNet is REALIZED, not only committed (WS-C10).

Feature: fix-sdn-vnet-realized-preflight.
Spec: .kiro/specs/fix-sdn-vnet-realized-preflight/.

THE BUG (platform-runtime-patterns P3, "committed != realized"): the guest-placing preflights
asserted the VNet exists in the SDN CONFIG (/cluster/sdn/vnets). A VNet can be committed but not
realized as a live bridge on the node (the applier did not run, or reloaded without it); the
preflight passed, Terraform created resources, and the container then failed at start with
"bridge 'p100' does not exist".

THE FIX: the shared read-only role ``sdn_vnet_realized`` reads the per-node realized zone content
(/nodes/<node>/sdn/zones/<zone>/content, status ``available``) with a bounded poll and fails
closed with the remedy. Each guest-placing preflight includes it right after its config assert.
Pure offline: structure + the real expressions evaluated with Ansible's templater.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_R = _REPO / "ansible/roles"
_ROLE = _R / "sdn_vnet_realized"

# preflight role -> (variable prefix, expected vnet expression)
_PREFLIGHTS = {
    "svc01_preflight": ("svc01", "p{{ vlan_id }}"),
    "svc06_preflight": ("svc06", "p20"),
    "svc07_preflight": ("svc07", "p20"),
    "svc09_preflight": ("svc09", "p20"),
    "mailcatcher_preflight": ("mailcatcher", "p20"),
    "obs_preflight": ("obs", "p20"),
}
# Preflights that read /cluster/sdn/vnets but do NOT place a guest, so the realized check is not
# theirs to make (consumer_preflight only decides whether to auto-create the network slice; the
# guest-placing svc01 child that follows does the realized check).
_NOT_GUEST_PLACING = {"consumer_preflight"}


def _tasks(path: Path) -> list[dict]:
    return [t for t in yaml.safe_load(path.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _role_task(fragment: str) -> dict:
    hits = [t for t in _tasks(_ROLE / "tasks/main.yml") if fragment in str(t.get("name", ""))]
    assert len(hits) == 1, (fragment, len(hits))
    return hits[0]


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


# --------------------------------------------------------------------------- #
# The shared role
# --------------------------------------------------------------------------- #
def test_role_reads_the_per_node_realized_zone_content_read_only():
    t = _role_task("Read the per-node realized zone content")
    uri = t["ansible.builtin.uri"]
    assert "/nodes/{{ sdn_vnet_realized_node }}/sdn/zones/" in " ".join(uri["url"].split())
    assert uri["url"].strip().endswith("/content")
    assert uri["method"] == "GET"
    assert t["check_mode"] is False and t["changed_when"] is False and t["failed_when"] is False
    assert t["no_log"] is True, "the PVE token is in the request header"


def test_role_poll_is_bounded():
    t = _role_task("Read the per-node realized zone content")
    assert "until" in t and "retries" in t and "delay" in t
    d = yaml.safe_load((_ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert 2 <= d["sdn_vnet_realized_retries"] <= 12 and 1 <= d["sdn_vnet_realized_delay"] <= 15


def _live_shape(statuses):
    """The shape the live Proxmox returned: [{'vnet': 'p20', 'status': 'available', ...}]."""
    return {"status": 200, "json": {"data": [{"vnet": v, "status": s, "statusmsg": ""} for v, s in statuses]}}


def _until(content, vnet="p100"):
    t = _role_task("Read the per-node realized zone content")
    return _tpl("{{ " + t["until"].strip() + " }}",
                {"sdn_vnet_realized_content": content, "sdn_vnet_realized_vnet": vnet})


def test_until_is_true_only_for_an_available_vnet():
    assert _until(_live_shape([("p20", "available"), ("p100", "available")])) is True


def test_BUG_a_committed_but_unrealized_vnet_is_not_ready():
    """The bug: p100 is in the config but absent from (or not available in) the node's realized content."""
    assert _until(_live_shape([("p20", "available")])) is False, "absent from the realized content"
    assert _until(_live_shape([("p20", "available"), ("p100", "pending")])) is False
    assert _until(_live_shape([("p100", "error")])) is False


def test_until_is_false_on_a_failed_read():
    assert _until({"status": 403}) is False
    assert _until({"status": -1}) is False
    assert _until({"status": 200, "json": {"data": []}}) is False


def _facts(content, vnet="p100"):
    t = _role_task("Compute the realized status of the VNet")
    out = {}
    for k, v in t["ansible.builtin.set_fact"].items():
        out[k] = _tpl(v, {"sdn_vnet_realized_content": content, "sdn_vnet_realized_vnet": vnet})
    return out


def test_statuses_fact_for_each_shape():
    assert _facts(_live_shape([("p100", "available")]))["sdn_vnet_realized_statuses"] == ["available"]
    assert _facts(_live_shape([("p20", "available")]))["sdn_vnet_realized_statuses"] == []
    assert _facts({"status": 403})["sdn_vnet_realized_statuses"] == []
    assert _facts({"status": 403})["sdn_vnet_realized_http"] in (403, "403")


def test_assert_passes_only_when_available_is_among_the_statuses():
    a = _role_task("Fail closed unless the VNet is REALIZED")["ansible.builtin.assert"]
    assert a["that"] == ["'available' in sdn_vnet_realized_statuses"]
    assert _tpl("{{ " + a["that"][0] + " }}", {"sdn_vnet_realized_statuses": ["available"]}) is True
    assert _tpl("{{ " + a["that"][0] + " }}", {"sdn_vnet_realized_statuses": []}) is False
    assert _tpl("{{ " + a["that"][0] + " }}", {"sdn_vnet_realized_statuses": ["pending"]}) is False


def test_fail_message_names_the_vnet_the_symptom_and_the_remedy():
    msg = _role_task("Fail closed unless the VNet is REALIZED")["ansible.builtin.assert"]["fail_msg"]
    assert "NOT realized" in msg and "does not exist" in msg
    assert "proxmox_sdn_applier" in msg and "-replace" in msg and "Nothing was provisioned" in msg


def test_zone_is_taken_from_the_config_list_with_a_safe_default():
    t = _role_task("Resolve the zone that owns the VNet")
    expr = t["ansible.builtin.set_fact"]["sdn_vnet_realized_zone"]
    base = {"sdn_vnet_realized_vnet": "p100", "sdn_vnet_realized_default_zone": "vlanzone"}
    got = _tpl(expr, {**base, "sdn_vnet_realized_vnets": [{"vnet": "p100", "zone": "projzone"}]}).strip()
    assert got == "projzone"
    got = _tpl(expr, {**base, "sdn_vnet_realized_vnets": [{"vnet": "p20", "zone": "vlanzone"}]}).strip()
    assert got == "vlanzone", "an unlisted VNet falls back to the default zone rather than erroring"


# --------------------------------------------------------------------------- #
# Wiring into the guest-placing preflights
# --------------------------------------------------------------------------- #
def _preflight_tasks(role):
    return _tasks(_R / role / "tasks/main.yml")


def _names(tasks):
    return [str(t.get("name", "")) for t in tasks]


@pytest.mark.parametrize("role", sorted(_PREFLIGHTS))
def test_preflight_includes_the_realized_check_right_after_the_config_assert(role):
    tasks = _preflight_tasks(role)
    names = _names(tasks)
    cfg = next(i for i, n in enumerate(names) if re.search(r"Preflight \| Assert the .*(VNet|bridge)", n))
    inc = next((i for i, t in enumerate(tasks)
                if isinstance(t.get("ansible.builtin.include_role"), dict)
                and t["ansible.builtin.include_role"].get("name") == "sdn_vnet_realized"), None)
    assert inc is not None, f"{role} only checks the VNet in config: committed != realized (WS-C10)"
    assert inc == cfg + 1, "the realized check must follow the config assert before any other work"


@pytest.mark.parametrize("role", sorted(_PREFLIGHTS))
def test_preflight_threads_its_own_node_endpoint_token_and_vnets(role):
    pfx, vnet = _PREFLIGHTS[role]
    inc = next(t for t in _preflight_tasks(role)
               if isinstance(t.get("ansible.builtin.include_role"), dict)
               and t["ansible.builtin.include_role"].get("name") == "sdn_vnet_realized")
    v = inc["vars"]
    assert v["sdn_vnet_realized_endpoint"] == f"{{{{ {pfx}_proxmox_endpoint_base }}}}"
    assert v["sdn_vnet_realized_token"] == f"{{{{ {pfx}_proxmox_api_token }}}}"
    assert v["sdn_vnet_realized_validate_certs"] == f"{{{{ {pfx}_proxmox_validate_certs }}}}"
    assert v["sdn_vnet_realized_node"] == f"{{{{ {pfx}_proxmox_node_name }}}}"
    assert v["sdn_vnet_realized_vnets"] == f"{{{{ {pfx}_sdn_vnets.json.data }}}}"
    assert v["sdn_vnet_realized_vnet"] == vnet


def test_drift_every_guest_placing_preflight_that_reads_vnets_includes_the_realized_check():
    """A new preflight that lists /cluster/sdn/vnets must also verify realization (or be allowlisted)."""
    offenders = []
    for d in sorted(_R.glob("*_preflight")):
        text = (d / "tasks/main.yml").read_text(encoding="utf-8")
        if "cluster/sdn/vnets" in text and d.name not in _NOT_GUEST_PLACING and "sdn_vnet_realized" not in text:
            offenders.append(d.name)
    assert not offenders, f"preflights check the VNet only in config: {offenders}"
    assert set(_PREFLIGHTS) <= {d.name for d in _R.glob("*_preflight")}


def test_no_preflight_gains_a_mutation():
    """The realized check is read-only: no command/shell/file/copy/template inside the new role."""
    for t in _tasks(_ROLE / "tasks/main.yml"):
        assert not {"ansible.builtin.command", "ansible.builtin.shell", "ansible.builtin.file",
                    "ansible.builtin.copy", "ansible.builtin.template"} & set(t)
