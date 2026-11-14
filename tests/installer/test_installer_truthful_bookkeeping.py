"""Bug-condition + preservation tests: installer bookkeeping steps report "changed" truthfully (WS-C39).

THE BUG (found live, WS-C34/C37/C41): every provision role reported `terraform init`, inventory generation and the
known_hosts refresh as changed on every run (`changed_when: true`, and an unconditional `ssh-keygen -R` followed by
a re-append), so a converged re-run of any installer was never `changed=0`.
THE FIX: `terraform init` is changed only when it downloads something; the inventory generator leaves an identical
file untouched and says so; known_hosts goes through the shared `known_hosts_refresh` role, which touches the file
only when the presented keys differ from the recorded ones.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROLES = Path(__file__).resolve().parents[2] / "ansible/roles"
_PROVISION = ["consumer", "mailcatcher", "obs", "svc01", "svc06", "svc07", "svc09"]
_KHR = _ROLES / "known_hosts_refresh/tasks"


def _flat(tasks):
    for t in tasks or []:
        if isinstance(t, dict):
            yield t
            for k in ("block", "rescue", "always"):
                yield from _flat(t.get(k))


def _tasks(role):
    return list(_flat(yaml.safe_load((_ROLES / f"{role}_provision/tasks/main.yml").read_text(encoding="utf-8"))))


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


@pytest.mark.parametrize("role", _PROVISION)
def test_BUG_no_bookkeeping_task_is_hard_coded_changed(role):
    for t in _tasks(role):
        name = t.get("name", "")
        if "terraform init" in name or "inventory" in name.lower() and ("Generate" in name or "Regenerate" in name):
            assert t.get("changed_when") is not True, (role, name)


@pytest.mark.parametrize("role", _PROVISION)
def test_terraform_init_is_changed_only_on_a_download(role):
    inits = [t for t in _tasks(role) if "terraform init" in t.get("name", "")]
    assert inits, "non-vacuity"
    for t in inits:
        cw = t["changed_when"]
        assert "'Installing '" in cw
        reg = t["register"]
        assert _tpl("{{ " + cw + " }}", {reg: {"stdout": "- Using previously-installed bpg/proxmox v0.107.0"}}) is False
        assert _tpl("{{ " + cw + " }}", {reg: {"stdout": "- Installing bpg/proxmox v0.107.0..."}}) is True


@pytest.mark.parametrize("role", _PROVISION)
def test_inventory_generation_is_changed_only_when_rewritten(role):
    gens = [t for t in _tasks(role)
            if "generate_inventory.py" in str(t.get("ansible.builtin.command", {}).get("argv", ""))]
    assert gens, "non-vacuity"
    for t in gens:
        cw, reg = t["changed_when"], t["register"]
        assert _tpl("{{ " + cw + " }}", {reg: {"stderr": "inventory unchanged: x.yml"}}) is False
        assert _tpl("{{ " + cw + " }}", {reg: {"stderr": "wrote inventory: x.yml"}}) is True


@pytest.mark.parametrize("role", _PROVISION)
def test_BUG_known_hosts_goes_through_the_shared_role(role):
    tasks = _tasks(role)
    for t in tasks:
        argv = str((t.get("ansible.builtin.command") or {}).get("argv", ""))
        assert "ssh-keygen" not in argv and "ansible.builtin.blockinfile" not in t, ("inline purge/re-append left",
                                                                                     t.get("name"))
    inc = [t for t in tasks if (t.get("ansible.builtin.include_role") or {}).get("name") == "known_hosts_refresh"]
    assert inc
    for t in inc:
        assert "ssh-keyscan" in t["name"]


def _host_tasks():
    return list(_flat(yaml.safe_load((_KHR / "host.yml").read_text(encoding="utf-8"))))


def _by(name_fragment):
    hits = [t for t in _host_tasks() if name_fragment in t.get("name", "")]
    assert len(hits) == 1, name_fragment
    return hits[0]


_SCAN = "10.0.20.10 ssh-ed25519 AAAAC3key1\n10.0.20.10 ecdsa-sha2-nistp256 AAAAE2key2\n"


def _compare(scan, known):
    sf = _by("Compare the presented keys")["ansible.builtin.set_fact"]
    v = {"khr_scan": {"stdout_lines": scan.splitlines()}, "khr_known": {"stdout_lines": known.splitlines()}}
    return str(_tpl(sf["khr_in_sync"], v)).strip(), _tpl(sf["khr_scanned_lines"], v)


def test_recorded_keys_in_any_order_and_hashed_hosts_are_in_sync():
    known = ("# Host 10.0.20.10 found: line 3\n|1|hashedhost= ecdsa-sha2-nistp256 AAAAE2key2\n"
             "|1|hashedhost= ssh-ed25519 AAAAC3key1\n")
    assert _compare(_SCAN, known)[0] == "True"


def test_a_reprovisioned_guest_with_new_keys_is_not_in_sync():
    assert _compare(_SCAN, "10.0.20.10 ssh-ed25519 OLDKEY\n")[0] == "False"
    assert _compare(_SCAN, "")[0] == "False"


def test_the_written_block_is_sorted_and_stable():
    _, lines = _compare(_SCAN, "")
    assert list(lines) == sorted(_SCAN.splitlines())


@pytest.mark.parametrize("fragment", ["Purge the stale fingerprints", "Write the current keys"])
def test_purge_and_write_happen_only_when_the_keys_changed(fragment):
    w = _by(fragment)["when"]
    assert "not (khr_in_sync | bool)" in w


def test_unreachable_handling_is_configurable_and_fails_by_default():
    d = yaml.safe_load((_ROLES / "known_hosts_refresh/defaults/main.yml").read_text(encoding="utf-8"))
    assert d["khr_fail_unreachable"] is True
    fw = _by("ssh-keyscan")["failed_when"]
    assert "khr_fail_unreachable" in fw


def test_markers_are_kept_identical_to_the_previous_inline_ones():
    """Same marker text as before, so the existing managed blocks are updated in place, not duplicated."""
    expected = {
        "svc07": "# {mark} SVC-07 installer host keys for ",
        "svc09": "# {mark} SVC-09 installer host keys for ",
        "obs": "# {mark} SVC-17/18 installer host keys for ",
        "mailcatcher": "# {mark} Mail catcher installer host keys for ",
    }
    for role, prefix in expected.items():
        inc = next(t for t in _tasks(role) if (t.get("ansible.builtin.include_role") or {}).get("name") == "known_hosts_refresh")
        assert inc["vars"]["khr_marker_prefix"] == prefix
    svc01 = next(t for t in _tasks("svc01") if (t.get("ansible.builtin.include_role") or {}).get("name") == "known_hosts_refresh")
    assert svc01["vars"]["khr_hosts"][0]["marker"] == "# {mark} SVC-01 installer host keys for {{ project_slug }}"


def test_BUG_the_transient_zitadel_helper_staging_is_not_a_change():
    """Found live: the dev-path helper is copied to /tmp on the guest and scrubbed again every run, so the copy always
    reported changed and SVC-06's org-provision child was never changed=0. The scrub is already changed_when false;
    the staging is scratch too."""
    tasks = list(_flat(yaml.safe_load((_ROLES / "zitadel-org-provision/tasks/main.yml").read_text(encoding="utf-8"))))
    stage = next(t for t in tasks if "stage the stdlib provisioning helper" in t.get("name", ""))
    scrub = next(t for t in tasks if "scrub the staged helper" in t.get("name", ""))
    assert stage["changed_when"] is False and scrub["changed_when"] is False
