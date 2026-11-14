"""Bug-condition + preservation tests: the platform clean-slate gate sees an untracked SDN zone (WS-C33)
and tolerates exactly the standing cluster firewall DENY rules (WS-C14).

WS-C33 THE BUG: the final gate probed only the p20 bridge. A zone that exists in Proxmox but is not in the
foundation Terraform state (and holds no VNet) is neither an occupant nor destroyed by `terraform destroy`,
so the gate reported an empty baseline falsely.
WS-C14 THE DECISION (operator, 2026-11-07): the two standing isolation DENY rules written by
`sdn_gateway/tasks/isolation.yml` are platform-owned BASELINE. The gate ignores EXACTLY those two rules and
flags any other platform-owned (`sdn_gateway:`-prefixed) cluster rule as a survivor.
These tests pin the structure and EVALUATE the real expressions with Ansible's templater.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_ROLE = _REPO / "ansible/roles/platform_clean_slate"
_MAIN = _ROLE / "tasks/main.yml"
_ZONE = _ROLE / "tasks/zone_occupants.yml"
_DEFAULTS = _ROLE / "defaults/main.yml"
_ISOLATION = _REPO / "ansible/roles/sdn_gateway/tasks/isolation.yml"

MGMT = "sdn_gateway: DENY any -> management VLAN 10 (managed)"
XPROJ = "sdn_gateway: DENY cross-project VLAN -> VLAN (managed)"
ALLOW = "sdn_gateway: ALLOW admin -> VLAN 100 (managed)"


def _tasks(path):
    return [t for t in yaml.safe_load(path.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _by(tasks, fragment):
    hits = [t for t in tasks if fragment in str(t.get("name", ""))]
    assert len(hits) == 1, (fragment, len(hits))
    return hits[0]


def _names(tasks):
    return [str(t.get("name", "")) for t in tasks]


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _defaults():
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))


def _fact(path, fragment, key, variables):
    t = _by(_tasks(path), fragment)
    return _tpl(t["ansible.builtin.set_fact"][key], variables)


# --------------------------------------------------------------------------- #
# WS-C14: the expected residue is exactly the two DENY rules, and cannot drift
# --------------------------------------------------------------------------- #
def test_expected_residue_is_exactly_the_two_standing_deny_rules():
    d = _defaults()
    got = [_tpl(x, {"platform_clean_slate_management_vlan_id": d["platform_clean_slate_management_vlan_id"]})
           for x in d["platform_clean_slate_expected_firewall_residue"]]
    assert got == [MGMT, XPROJ]


def test_expected_residue_matches_the_comments_the_isolation_task_writes():
    """Drift guard: the gate's tolerated comments are the very strings isolation.yml creates."""
    src = _ISOLATION.read_text(encoding="utf-8")
    assert "sdn_gateway: DENY any -> management VLAN {{ sdn_gateway_management_vlan_id }} (managed)" in src
    assert "sdn_gateway: DENY cross-project VLAN -> VLAN (managed)" in src
    d = _defaults()
    assert d["platform_clean_slate_expected_firewall_residue"][0] == (
        "sdn_gateway: DENY any -> management VLAN {{ platform_clean_slate_management_vlan_id }} (managed)")
    assert d["platform_clean_slate_expected_firewall_residue"][1] == "sdn_gateway: DENY cross-project VLAN -> VLAN (managed)"


def _classify(rules):
    d = _defaults()
    expected = [_tpl(x, {"platform_clean_slate_management_vlan_id": 10})
                for x in d["platform_clean_slate_expected_firewall_residue"]]
    out = "192.168.0.69 | CHANGED | rc=0 >>\n" + json.dumps(rules)
    return _fact(_MAIN, "GATE — classify the platform-owned cluster firewall rules",
                 "platform_cs_unexpected_firewall_rules",
                 {"platform_cs_gate_fw_out": out, "platform_cs_expected_firewall_residue": expected,
                  "platform_clean_slate_firewall_rule_prefix": d["platform_clean_slate_firewall_rule_prefix"]})


def test_only_the_two_deny_rules_is_a_clean_baseline():
    assert _classify([{"comment": MGMT, "pos": 0}, {"comment": XPROJ, "pos": 1}]) == []


def test_BUG_a_surviving_per_vlan_allow_rule_is_flagged_not_tolerated():
    got = _classify([{"comment": MGMT}, {"comment": XPROJ}, {"comment": ALLOW}])
    assert got == [ALLOW]


def test_an_operator_rule_without_the_platform_prefix_is_not_ours_and_ignored():
    assert _classify([{"comment": MGMT}, {"comment": "my own office rule"}, {"type": "in"}]) == []


def test_no_rules_at_all_is_clean():
    assert _classify([]) == []


def test_a_near_miss_deny_comment_is_not_tolerated():
    """Exact match only: a tampered/extra DENY with the platform prefix is a survivor."""
    got = _classify([{"comment": MGMT + " extra"}, {"comment": XPROJ}])
    assert got == [MGMT + " extra"]


def test_the_firewall_probe_is_read_only_and_never_fails_the_play():
    t = _by(_tasks(_MAIN), "GATE — read the cluster firewall rules")
    assert t["changed_when"] is False
    argv = " ".join(str(a) for a in t["ansible.builtin.command"]["argv"])
    assert "pvesh get /cluster/firewall/rules" in argv and "--output-format json" in argv
    for verb in ("create", "delete", " set "):
        assert verb not in argv


# --------------------------------------------------------------------------- #
# WS-C33: an untracked zone is detected before the destroy and at the final gate
# --------------------------------------------------------------------------- #
_ZONES = {"json": {"data": [{"zone": "vlanzone", "type": "vlan", "bridge": "vmbr0"}, {"zone": "other", "type": "vlan"}]}}


def _untracked(state, zones=_ZONES):
    return _fact(_ZONE, "Compute the untracked zone", "platform_cs_zone_untracked",
                 {"platform_cs_zone_list_raw": zones,
                  "platform_clean_slate_sdn_zone_id": "vlanzone",
                  "platform_cs_zone_foundation_state": {"stdout": state}})


def test_BUG_empty_zone_the_foundation_state_does_not_track_is_untracked():
    assert _untracked("") == ["vlanzone"]
    assert _untracked("terraform_data.registry_watch\n") == ["vlanzone"]


def test_a_zone_the_foundation_tracks_is_not_untracked():
    assert _untracked("proxmox_sdn_zone_vlan.foundation\n") == []


def test_no_zone_in_proxmox_means_nothing_untracked():
    assert _untracked("", {"json": {"data": [{"zone": "other"}]}}) == []


def test_other_zones_are_never_touched():
    assert "other" not in _untracked("")


def _gate(untracked, optin):
    g = _by(_tasks(_ZONE), "GATE: halt on an untracked zone")["ansible.builtin.assert"]["that"][0]
    return _tpl("{{ " + g + " }}", {"platform_cs_zone_untracked": untracked,
                                    "platform_clean_slate_remove_zone_occupants": optin})


def test_untracked_zone_gate_truth_table():
    assert _gate([], False) is True
    assert _gate([], True) is True
    assert _gate(["vlanzone"], False) is False, "fail closed without the opt-in"
    assert _gate(["vlanzone"], True) is True


def test_untracked_zone_gate_names_the_zone_and_both_remedies():
    msg = _by(_tasks(_ZONE), "GATE: halt on an untracked zone")["ansible.builtin.assert"]["fail_msg"]
    assert "platform_cs_zone_untracked" in msg
    assert "Nothing in the fabric was changed" in msg
    assert "platform_clean_slate_remove_zone_occupants=true" in msg


def test_zone_removal_is_opt_in_gated_token_hidden_and_after_the_vnet_removal():
    tasks = _tasks(_ZONE)
    names = _names(tasks)
    delete = _by(tasks, "Delete the untracked zone")
    assert delete["ansible.builtin.uri"]["method"] == "DELETE"
    assert delete["no_log"] is True
    cond = " ".join(str(c) for c in (delete["when"] if isinstance(delete["when"], list) else [delete["when"]]))
    assert "platform_cs_zone_untracked" in cond and "platform_clean_slate_remove_zone_occupants" in cond
    vnet_delete = next(i for i, n in enumerate(names) if "Delete the occupant VNets" in n)
    gate = next(i for i, n in enumerate(names) if "GATE: halt on an untracked zone" in n)
    assert vnet_delete < names.index(delete["name"])
    assert gate < names.index(delete["name"])


def test_every_new_token_bearing_zone_task_is_no_log():
    for t in _tasks(_ZONE):
        if "uri" in " ".join(t.keys()) and "Authorization" in json.dumps(t):
            assert t.get("no_log") is True, t["name"]


def _surviving_zone(out):
    return _fact(_MAIN, "GATE — compute the surviving SDN zone", "platform_cs_surviving_zone",
                 {"platform_cs_gate_zone_out": out, "platform_clean_slate_sdn_zone_id": "vlanzone"})


def test_BUG_final_gate_reports_a_surviving_zone():
    assert _surviving_zone("192.168.0.69 | CHANGED | rc=0 >>\n│ vlanzone │ vlan │") == ["vlanzone"]


def test_final_gate_is_clean_when_the_zone_is_gone():
    assert _surviving_zone("192.168.0.69 | CHANGED | rc=0 >>\n") == []
    assert _surviving_zone("zone 'vlanzone' does not exist") == []


def test_the_final_assert_covers_all_five_survivor_signals_and_names_them():
    tasks = _tasks(_MAIN)
    gates = [t for t in tasks if "GATE — assert an empty baseline" in str(t.get("name", ""))]
    assert len(gates) == 1
    a = gates[0]["ansible.builtin.assert"]
    text = json.dumps(a["that"]) + a["fail_msg"]
    for fact in ("platform_cs_surviving_vmids", "platform_cs_surviving_state_addrs", "platform_cs_surviving_sdn",
                 "platform_cs_surviving_zone", "platform_cs_unexpected_firewall_rules"):
        assert fact in text, fact
    assert re.search(r"standing .*DENY", a["success_msg"] + a["fail_msg"], re.I | re.S), "expected residue is named"


def test_the_zone_probe_precedes_the_final_assert():
    names = _names(_tasks(_MAIN))
    gate = names.index("Clean-Slate | GATE — assert an empty baseline (0 guests, 0 state, no SDN, no zone), Req 7.4/7.5")
    probe = next(i for i, n in enumerate(names) if "GATE — compute the surviving SDN zone" in n)
    assert probe < gate
