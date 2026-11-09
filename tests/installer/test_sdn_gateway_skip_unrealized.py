"""WS-C26 — the platform gateway phase skips (loudly) registry-active project VLANs whose bridge is not realized yet.

After WS-C20 the stop is clean, but ``platform-bootstrap.yml -e bootstrap_scope=host,api`` on a cluster whose registry lists
active project VLANs (100, 101, 102) and has no project network slices still ENDS in a fail-closed stop at VLAN 100: the
shared VLAN 20 is configured, then the run fails for a project VLAN nobody asked to bring up in this run.

Decision (see the spec): the platform bring-up serves "every registry-active project VLAN" as a DEFAULT, not as a
deliberate per-VLAN request, so for THAT caller an unrealized project VLAN is skipped with a WARNING naming it and the
remedy. An explicit ``sdn-gateway.yml`` run keeps the fail-closed behavior that ``fix-sdn-gateway-project-vlan-noop``
introduced (an opted-in VLAN is never silently dropped), and the shared VLAN 20 is never skipped.

Pure offline: the task list is parsed and the real Jinja expressions are evaluated with Ansible's templater.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_MAIN = _ROOT / "ansible" / "roles" / "sdn_gateway" / "tasks" / "main.yml"
_DEFAULTS = _ROOT / "ansible" / "roles" / "sdn_gateway" / "defaults" / "main.yml"
_PLATFORM_GATEWAY = _ROOT / "ansible" / "roles" / "platform_gateway" / "tasks" / "main.yml"
_SDN_PLAYBOOK = _ROOT / "ansible" / "playbooks" / "sdn-gateway.yml"

_FLAG = "sdn_gateway_skip_unrealized_project_vlans"
_PROBE = "Probe the SDN bridge of each served project VLAN"
_FILTER = "Compute which served project VLANs have a realized bridge"
_WARN = "WARN — served project VLAN(s) skipped"
_LOOP = "Configure each served VLAN's host gateway"


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(_MAIN.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _idx(fragment: str) -> int:
    for i, t in enumerate(_tasks()):
        if fragment in str(t.get("name")):
            return i
    raise AssertionError(f"no task named like {fragment!r}")


def _task(fragment: str) -> dict:
    return _tasks()[_idx(fragment)]


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def test_non_vacuity_the_tasks_under_test_exist():
    for frag in (_PROBE, _FILTER, _WARN, _LOOP):
        _idx(frag)


def test_the_flag_defaults_off_so_an_explicit_gateway_run_stays_fail_closed():
    d = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert d[_FLAG] is False
    parsed = yaml.safe_load(_SDN_PLAYBOOK.read_text(encoding="utf-8"))  # comments are not data
    assert _FLAG not in str(parsed), "the explicit sdn-gateway.yml entry point must not set the skip"


def test_the_platform_bring_up_turns_it_on_for_its_child_run():
    text = _PLATFORM_GATEWAY.read_text(encoding="utf-8")
    assert f"{_FLAG}=true" in text, "platform_gateway serves every registry-active VLAN by default, so it opts in to the skip"


def test_probe_filter_warn_and_loop_are_ordered_before_the_configure_loop():
    assert _idx(_PROBE) < _idx(_FILTER) < _idx(_WARN) < _idx(_LOOP)


def test_the_configure_loop_iterates_the_filtered_set_not_the_raw_served_set():
    """Bug condition: the loop iterated every served VLAN, so an unrealized project VLAN hard-failed the whole run."""
    loop = str(_task(_LOOP)["loop"])
    assert "sdn_gateway_configure_vlan_ids" in loop
    assert "sdn_gateway_served_vlan_ids" not in loop


def test_the_probe_is_read_only_and_covers_only_project_vlans_never_the_shared_one():
    t = _task(_PROBE)
    cmd = t["ansible.builtin.command"]["cmd"]
    assert cmd.startswith("ip -o link show dev p")
    assert t["changed_when"] is False and t["failed_when"] is False and t["check_mode"] is False
    assert _FLAG in str(t["when"])
    served = _tpl(str(t["loop"]), {"sdn_gateway_served_vlan_ids": [20, 100, 101], "sdn_gateway_shared_vlan_id": 20})
    assert [int(x) for x in served] == [100, 101], "VLAN 20 is the fabric the api bucket just created: never skipped"


def _filter_vars(skip, probe_results, served=(20, 100, 101, 102)):
    return {"sdn_gateway_skip_unrealized_project_vlans": skip, "sdn_gateway_served_vlan_ids": list(served),
            "sdn_gateway_shared_vlan_id": 20, "sdn_gateway_bridge_probe": {"results": probe_results}}


def _result(vlan, rc):
    return {"item": vlan, "rc": rc, "stderr": '' if rc == 0 else f'Device "p{vlan}" does not exist.'}


def _facts(variables):
    """Resolve the task's own ``vars`` first (as Ansible does), then each set_fact value."""
    task = _task(_FILTER)
    scope = dict(variables)
    for name, expr in (task.get("vars") or {}).items():
        scope[name] = _tpl(expr, scope)
    return {k: _tpl(v, scope) for k, v in task["ansible.builtin.set_fact"].items()}


def test_skip_on_drops_only_the_unrealized_project_vlans():
    f = _facts(_filter_vars(True, [_result(100, 1), _result(101, 0), _result(102, 1)]))
    assert [int(x) for x in f["sdn_gateway_unrealized_project_vlan_ids"]] == [100, 102]
    assert [int(x) for x in f["sdn_gateway_configure_vlan_ids"]] == [20, 101]


def test_skip_on_with_every_bridge_present_changes_nothing():
    f = _facts(_filter_vars(True, [_result(100, 0), _result(101, 0), _result(102, 0)]))
    assert f["sdn_gateway_unrealized_project_vlan_ids"] == []
    assert [int(x) for x in f["sdn_gateway_configure_vlan_ids"]] == [20, 100, 101, 102]


def test_skip_off_keeps_every_served_vlan_so_the_existing_fail_closed_assert_still_fires():
    f = _facts(_filter_vars(False, []))
    assert f["sdn_gateway_unrealized_project_vlan_ids"] == []
    assert [int(x) for x in f["sdn_gateway_configure_vlan_ids"]] == [20, 100, 101, 102]


def test_the_shared_vlan_is_never_skipped_even_if_a_probe_result_claims_it_is_absent():
    f = _facts(_filter_vars(True, [_result(20, 1), _result(100, 1)]))
    assert 20 in [int(x) for x in f["sdn_gateway_configure_vlan_ids"]]


def test_the_warning_names_each_skipped_vlan_the_remedy_and_that_nothing_was_configured():
    w = _task(_WARN)
    assert "unrealized" in str(w["when"]) or "length" in str(w["when"])
    msg = _tpl(w["ansible.builtin.debug"]["msg"], {
        "sdn_gateway_unrealized_project_vlan_ids": [100, 102], "sdn_gateway_configure_vlan_ids": [20, 101]})
    msg = " ".join(str(msg).split())
    assert "100" in msg and "102" in msg
    assert "NOT configured" in msg
    assert "network-slice" in msg, "the remedy (apply the project's network-slice root) must be named"
    assert "sdn_gateway_skip_unrealized_project_vlans" in msg, "and how to make it strict again"


def test_shared_vlan_failure_path_is_unchanged_the_configure_assert_is_still_there():
    """The skip filters the LOOP INPUT only; configure.yml keeps its own bridge assertion (WS-C20 guard)."""
    configure = yaml.safe_load((_ROOT / "ansible/roles/sdn_gateway/tasks/configure.yml").read_text(encoding="utf-8"))
    assert any("Assert the SDN VNet bridge" in str(t.get("name")) for t in configure if isinstance(t, dict))


# --------------------------------------------------------------------------- #
# Found by the live run: the parent swallowed the child's stdout on success, so the warning was invisible.
# --------------------------------------------------------------------------- #
def _gateway_tasks() -> list[dict]:
    return [t for t in yaml.safe_load(_PLATFORM_GATEWAY.read_text(encoding="utf-8")) if isinstance(t, dict)]


_CHILD_STDOUT = (
    "TASK [sdn_gateway : WARN — served project VLAN(s) skipped because their SDN bridge does not exist yet] ***\n"
    'ok: [shrimp] => {\n    "msg": "WARNING: served project VLAN(s) [100, 101, 102] were NOT configured: their SDN bridge '
    '(p<vlan>) does not exist on this host yet. Configuring only [20]."\n}\n'
    "PLAY RECAP *********************************************************************\n"
    "shrimp                     : ok=30   changed=4    unreachable=0    failed=0\n"
)


def test_the_parent_surfaces_the_childs_skip_warning_on_a_successful_run():
    tasks = _gateway_tasks()
    names = [str(t.get("name")) for t in tasks]
    run_i = next(i for i, n in enumerate(names) if "run the sdn_gateway role via the child" in n)
    surface = next(t for t in tasks if "Surface the skipped-VLAN warning" in str(t.get("name")))
    assert names.index(str(surface["name"])) > run_i, "it must come after the child run it reports on"
    assert "platform_gateway_run" in str(surface["when"])
    msg = _tpl(surface["ansible.builtin.debug"]["msg"], {"platform_gateway_run": {"stdout": _CHILD_STDOUT}})
    text = " ".join(str(msg).split())
    assert "100, 101, 102" in text and "NOT configured" in text


def test_nothing_is_printed_when_the_child_skipped_nothing():
    surface = next(t for t in _gateway_tasks() if "Surface the skipped-VLAN warning" in str(t.get("name")))
    quiet = "PLAY RECAP\nshrimp : ok=20 changed=0 unreachable=0 failed=0\n"
    got = _tpl("{{ " + " ".join(str(surface["when"]).split()) + " }}", {"platform_gateway_run": {"stdout": quiet}})
    assert got is False or str(got).lower() == "false"
    shown = _tpl("{{ " + " ".join(str(surface["when"]).split()) + " }}", {"platform_gateway_run": {"stdout": _CHILD_STDOUT}})
    assert shown is True or str(shown).lower() == "true"
