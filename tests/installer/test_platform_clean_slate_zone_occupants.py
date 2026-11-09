"""Bug-condition + preservation tests: fabric teardown handles SDN-zone occupants (WS-C12).

Feature: fix-platform-fabric-zone-occupants. Spec: .kiro/specs/fix-platform-fabric-zone-occupants/.

THE BUG: the platform clean-slate's fabric step ran `terraform destroy` on platform-foundation, which
removes only what ITS state tracks. The SDN zone cannot be deleted while any VNet references it, so a VNet
the foundation state does not own blocked the teardown. Live shape (throwaway cluster): foundation state =
zone only; the zone held p20 (shared VNet, untracked), p100 (a project slice tracked by the PROJECT's state)
and p103 (an orphan with no state).

THE FIX: before the destroy, compute the occupants, FAIL CLOSED unless explicitly opted in, refuse to touch a
VNet with a guest still attached, remove the occupants via the Proxmox SDN API, and clean the project slice's
dangling local state. These tests pin the structure and EVALUATE the real expressions with Ansible's templater.
"""
from __future__ import annotations

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
_SLICE = _ROLE / "tasks/zone_slice_state.yml"


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


# --------------------------------------------------------------------------- #
# Placement and defaults
# --------------------------------------------------------------------------- #
def test_the_occupant_step_runs_after_init_and_before_the_foundation_destroy():
    names = _names(_tasks(_MAIN))
    init = names.index("Clean-Slate | terraform init (platform-foundation, before destroy)")
    zone = next(i for i, n in enumerate(names) if "ZONE — Handle SDN zone occupants" in n)
    destroy = names.index("Clean-Slate | terraform destroy (platform-foundation SDN fabric, scoped)")
    assert init < zone < destroy


def test_removal_is_opt_in_and_defaults_fail_closed():
    d = yaml.safe_load((_ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert d["platform_clean_slate_remove_zone_occupants"] is False
    assert d["platform_clean_slate_sdn_zone_id"] == "vlanzone"
    assert d["platform_clean_slate_registry_path"] == "infra/platform-foundation/projects.yaml"
    assert 2 <= d["platform_clean_slate_zone_poll_retries"] <= 12


# --------------------------------------------------------------------------- #
# Occupant computation (the real expressions, live-shaped data)
# --------------------------------------------------------------------------- #
_RAW = {"json": {"data": [
    {"vnet": "p20", "zone": "vlanzone", "tag": 20},
    {"vnet": "p100", "zone": "vlanzone", "tag": 100},
    {"vnet": "p103", "zone": "vlanzone", "tag": 103},
    {"vnet": "other", "zone": "someotherzone", "tag": 7},
]}}
_REGISTRY = {"projects": [{"slug": "dronefleet", "vlan_id": 100, "status": "active"},
                          {"slug": "smoke", "vlan_id": 101, "status": "decommissioned"}]}


def _fact(task_fragment, key, variables):
    t = _by(_tasks(_ZONE), task_fragment)
    return _tpl(t["ansible.builtin.set_fact"][key], variables)


def _base(foundation_state_stdout=""):
    return {
        "platform_cs_zone_vnets_raw": _RAW,
        "platform_clean_slate_sdn_zone_id": "vlanzone",
        "platform_clean_slate_sdn_vnet_id": "p20",
        "platform_cs_zone_foundation_state": {"stdout": foundation_state_stdout},
    }


def test_only_the_foundation_zones_vnets_are_considered():
    ids = _fact("Compute the zone's VNets and the VNet the foundation state owns", "platform_cs_zone_vnet_ids", _base())
    assert sorted(ids) == ["p100", "p103", "p20"], "a VNet in another zone is never an occupant"


def test_BUG_live_shape_untracked_p20_plus_project_and_orphan_are_all_occupants():
    """Foundation state tracks only the zone: p20 is NOT owned, so all three block the zone."""
    v = _base("proxmox_sdn_zone_vlan.foundation\n")
    owned = _fact("Compute the zone's VNets and the VNet the foundation state owns", "platform_cs_zone_foundation_owned", v)
    assert owned == []
    v["platform_cs_zone_vnet_ids"], v["platform_cs_zone_foundation_owned"] = ["p100", "p103", "p20"], owned
    occ = _fact("Compute the occupants the foundation does not own", "platform_cs_zone_occupants", v)
    assert occ == ["p100", "p103", "p20"]


def test_when_the_foundation_tracks_p20_it_is_owned_and_not_an_occupant():
    state = "proxmox_sdn_zone_vlan.foundation\nproxmox_sdn_vnet.shared_services\nproxmox_sdn_subnet.shared_services\n"
    v = _base(state)
    owned = _fact("Compute the zone's VNets and the VNet the foundation state owns", "platform_cs_zone_foundation_owned", v)
    assert owned == ["p20"]
    v["platform_cs_zone_vnet_ids"], v["platform_cs_zone_foundation_owned"] = ["p100", "p20"], owned
    assert _fact("Compute the occupants the foundation does not own", "platform_cs_zone_occupants", v) == ["p100"]


def test_a_clean_zone_has_no_occupants():
    v = {"platform_cs_zone_vnet_ids": ["p20"], "platform_cs_zone_foundation_owned": ["p20"]}
    assert _fact("Compute the occupants the foundation does not own", "platform_cs_zone_occupants", v) == []


def test_occupant_owners_come_from_the_registry_else_untracked():
    t = _by(_tasks(_ZONE), "Map each occupant VNet to its owning project")
    # The registry is read with lookup('file', <repo>/<registry path>) | from_yaml; the bare templater has no lookup
    # plugins, so supply the parsed registry and assert the lookup by structure below.
    task_vars = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t["vars"].items()}
    task_vars["platform_cs_zone_registry"] = _REGISTRY
    v = {"platform_cs_zone_occupants": ["p100", "p103", "p20"], **task_vars}
    owners = _tpl(t["ansible.builtin.set_fact"]["platform_cs_zone_occupant_owners"], v)
    assert owners == {"p100": "dronefleet", "p103": "untracked", "p20": "untracked"}


def test_the_registry_is_read_from_the_committed_file():
    t = _by(_tasks(_ZONE), "Map each occupant VNet to its owning project")
    assert "platform_clean_slate_registry_path" in t["vars"]["platform_cs_zone_registry_file"]
    assert "lookup('ansible.builtin.file'" in t["vars"]["platform_cs_zone_registry"]
    assert "from_yaml" in t["vars"]["platform_cs_zone_registry"]


# --------------------------------------------------------------------------- #
# The fail-closed gate
# --------------------------------------------------------------------------- #
def _gate(occupants, optin):
    g = _by(_tasks(_ZONE), "GATE: halt (fail closed) on zone occupants")["ansible.builtin.assert"]["that"][0]
    return _tpl("{{ " + g + " }}", {"platform_cs_zone_occupants": occupants,
                                    "platform_clean_slate_remove_zone_occupants": optin})


def test_gate_truth_table():
    assert _gate([], False) is True, "no occupants: proceeds"
    assert _gate([], True) is True
    assert _gate(["p100"], False) is False, "occupants without the opt-in HALT"
    assert _gate(["p100"], True) is True


def test_gate_message_names_owners_and_both_remedies_and_says_nothing_changed():
    msg = _by(_tasks(_ZONE), "GATE: halt (fail closed) on zone occupants")["ansible.builtin.assert"]["fail_msg"]
    assert "platform_cs_zone_occupant_owners" in msg
    assert "Nothing in the fabric was changed" in msg
    assert "platform_clean_slate_remove_zone_occupants=true" in msg and "OWN root" in msg


def test_the_gate_precedes_every_mutation():
    tasks = _tasks(_ZONE)
    names = _names(tasks)
    gate = next(i for i, n in enumerate(names) if "GATE: halt (fail closed)" in n)
    for i, t in enumerate(tasks):
        uri = t.get("ansible.builtin.uri") or {}
        if uri.get("method") in ("DELETE", "PUT"):
            assert i > gate, f"mutation before the fail-closed gate: {t['name']}"


# --------------------------------------------------------------------------- #
# Never remove a VNet that still has a guest attached
# --------------------------------------------------------------------------- #
def _attached(configs, occupants):
    t = _by(_tasks(_ZONE), "Compute the guests still attached to an occupant bridge")
    return _tpl(t["ansible.builtin.set_fact"]["platform_cs_zone_attached"],
                {"platform_cs_zone_guest_configs": {"results": configs}, "platform_cs_zone_occupants": occupants})


def _cfg(kind, vmid, **nets):
    return {"item": {"kind": kind, "vmid": vmid}, "json": {"data": dict(nets, hostname="x", memory=512)}}


def test_an_lxc_and_a_vm_on_an_occupant_bridge_are_reported():
    cfgs = [_cfg("lxc", 100001, net0="name=eth0,bridge=p100,firewall=0,hwaddr=AA:BB,ip=10.0.100.11/24"),
            _cfg("qemu", 200, net0="virtio=AA:BB:CC,bridge=p103,firewall=1"),
            _cfg("lxc", 1070, net0="name=eth0,bridge=p20,ip=10.0.20.10/24")]
    assert sorted(_attached(cfgs, ["p100", "p103"])) == ["lxc/100001 on p100", "qemu/200 on p103"]


def test_guests_on_other_bridges_or_without_nics_do_not_block():
    cfgs = [_cfg("lxc", 1070, net0="name=eth0,bridge=p20"), _cfg("lxc", 5)]
    assert _attached(cfgs, ["p100"]) == []


def test_a_guest_with_several_nics_is_found_on_any_of_them():
    cfgs = [_cfg("lxc", 9, net0="name=eth0,bridge=p20", net1="name=eth1,bridge=p100")]
    assert _attached(cfgs, ["p100"]) == ["lxc/9 on p100"]


def test_attached_gate_blocks_and_precedes_the_first_deletion():
    tasks = _tasks(_ZONE)
    names = _names(tasks)
    gate = next(i for i, n in enumerate(names) if "refuse to remove a VNet that still has a guest attached" in n)
    first_delete = next(i for i, t in enumerate(tasks) if (t.get("ansible.builtin.uri") or {}).get("method") == "DELETE")
    assert gate < first_delete
    a = tasks[gate]["ansible.builtin.assert"]
    assert a["that"] == ["(platform_cs_zone_attached | length) == 0"]
    assert "OWN clean-slate" in a["fail_msg"] and "Nothing was removed" in a["fail_msg"]


# --------------------------------------------------------------------------- #
# Removal order and verification
# --------------------------------------------------------------------------- #
def test_subnets_then_vnets_then_apply_then_poll_then_verify():
    tasks = _tasks(_ZONE)
    names = _names(tasks)
    i_sub = next(i for i, n in enumerate(names) if "Delete the occupants' subnets" in n)
    i_vn = next(i for i, n in enumerate(names) if "Delete the occupant VNets" in n)
    i_apply = next(i for i, n in enumerate(names) if "Apply the SDN" in n)
    i_poll = next(i for i, n in enumerate(names) if "Poll until the occupants are gone" in n)
    i_verify = next(i for i, n in enumerate(names) if "GATE: the occupants are gone" in n)
    assert i_sub < i_vn < i_apply < i_poll < i_verify
    assert tasks[i_sub]["ansible.builtin.uri"]["method"] == "DELETE"
    assert tasks[i_vn]["ansible.builtin.uri"]["method"] == "DELETE"
    assert tasks[i_apply]["ansible.builtin.uri"]["method"] == "PUT"
    assert tasks[i_apply]["ansible.builtin.uri"]["url"].endswith("/cluster/sdn")


def test_subnets_are_flattened_with_their_vnet():
    t = _by(_tasks(_ZONE), "Flatten the subnets")
    lists = {"results": [{"item": "p100", "json": {"data": [{"subnet": "vlanzone-10.0.100.0-24"}]}},
                         {"item": "p103", "json": {"data": []}}]}
    out = _tpl(t["ansible.builtin.set_fact"]["platform_cs_zone_subnets"], {"platform_cs_zone_subnet_lists": lists})
    assert out == [{"vnet": "p100", "subnet": "vlanzone-10.0.100.0-24"}]


def test_poll_is_bounded_and_true_only_when_every_occupant_is_gone():
    t = _by(_tasks(_ZONE), "Poll until the occupants are gone")
    until = t["until"]
    assert "retries" in t and "delay" in t
    gone = {"platform_cs_zone_vnets_after": {"status": 200, "json": {"data": [{"vnet": "p999"}]}},
            "platform_cs_zone_occupants": ["p100", "p103"]}
    assert _tpl("{{ " + until.strip() + " }}", gone) is True
    still = {"platform_cs_zone_vnets_after": {"status": 200, "json": {"data": [{"vnet": "p103"}]}},
             "platform_cs_zone_occupants": ["p100", "p103"]}
    assert _tpl("{{ " + until.strip() + " }}", still) is False
    assert _tpl("{{ " + until.strip() + " }}", {**gone, "platform_cs_zone_vnets_after": {"status": 500}}) is False


def test_every_token_bearing_task_is_no_log():
    for t in _tasks(_ZONE):
        uri = t.get("ansible.builtin.uri")
        if uri and "PVEAPIToken" in str(uri.get("headers", "")):
            assert t.get("no_log") is True, t["name"]


def test_all_mutations_are_gated_on_the_opt_in_and_having_occupants():
    for t in _tasks(_ZONE):
        uri = t.get("ansible.builtin.uri") or {}
        if uri.get("method") in ("DELETE", "PUT"):
            w = " ".join(str(x) for x in t["when"]) if isinstance(t["when"], list) else str(t["when"])
            assert "platform_cs_zone_occupants | length) > 0" in w and "platform_clean_slate_remove_zone_occupants" in w, t["name"]


def test_read_only_steps_never_change_anything():
    for fragment in ("List the SDN VNets", "List the foundation root's tracked resources", "Read each guest's NIC config"):
        t = _by(_tasks(_ZONE), fragment)
        assert t["changed_when"] is False and t["check_mode"] is False


# --------------------------------------------------------------------------- #
# Dangling slice state cleanup
# --------------------------------------------------------------------------- #
def test_slice_state_cleanup_is_provider_free_local_only_and_skips_missing_files():
    tasks = _tasks(_SLICE)
    rm = _by(tasks, "Remove the dangling slice addresses")
    argv = [str(a) for a in rm["ansible.builtin.command"]["argv"]]
    assert argv[:3] == ["terraform", "state", "rm"] and any(a.startswith("-state=") for a in argv)
    assert "destroy" not in argv, "state is cleaned, never `terraform destroy`ed from the platform role"
    assert rm["when"] == "platform_cs_zone_slice_state.stat.exists"
    assert _by(tasks, "List the dangling slice addresses")["when"] == "platform_cs_zone_slice_state.stat.exists"


def test_only_registry_known_owners_get_a_state_cleanup():
    inc = _by(_tasks(_ZONE), "Clean the dangling local slice state")
    assert "rejectattr('value', 'equalto', 'untracked')" in inc["loop"]
    assert inc["ansible.builtin.include_tasks"] == "zone_slice_state.yml"


# --------------------------------------------------------------------------- #
# Preservation
# --------------------------------------------------------------------------- #
def test_the_existing_destroy_and_gates_are_unchanged():
    names = _names(_tasks(_MAIN))
    for n in ("Clean-Slate | terraform destroy (platform-foundation SDN fabric, scoped)",
              "Clean-Slate | GATE — assert an empty baseline (0 guests, 0 state, no SDN), Req 7.4/7.5",
              "Clean-Slate | Halt-before-fabric — assert every discovered service reached empty (Req 2.4)"):
        assert n in names
    destroy = _by(_tasks(_MAIN), "terraform destroy (platform-foundation SDN fabric, scoped)")
    assert destroy["ansible.builtin.command"]["argv"] == ["terraform", "destroy", "-input=false", "-auto-approve"]


# --------------------------------------------------------------------------- #
# Host gateway stanzas of PROJECT VLANs are decommissioned too (found live)
# --------------------------------------------------------------------------- #
def test_BUG_gateway_teardown_decommissions_the_shared_vlan_and_every_registry_vlan():
    """Live: only VLAN 20 was decommissioned, so sdn-gw-100.cfg / sdn-gw-103.cfg survived the clean-slate."""
    t = _by(_tasks(_MAIN), "Compute the VLANs whose host gateway to decommission")
    task_vars = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t["vars"].items()}
    task_vars["platform_cs_gateway_registry"] = _REGISTRY | {"projects": _REGISTRY["projects"] + [{"slug": "x", "vlan_id": 103}]}
    got = _tpl("{{ platform_cs_gateway_decommission_vlans_expr }}".replace("_expr", "_calc").replace("decommission_vlans", "vlans"),
               {"platform_clean_slate_shared_vlan_id": 20, **task_vars})
    assert got == [20, 100, 101, 103]


def test_an_empty_registry_still_decommissions_the_shared_vlan():
    t = _by(_tasks(_MAIN), "Compute the VLANs whose host gateway to decommission")
    task_vars = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t["vars"].items()}
    task_vars["platform_cs_gateway_registry"] = {"projects": []}
    assert _tpl("{{ platform_cs_gateway_vlans_calc }}", {"platform_clean_slate_shared_vlan_id": 20, **task_vars}) == [20]


def test_the_gateway_child_play_receives_that_set_and_runs_after_it_is_computed():
    tasks = _tasks(_MAIN)
    names = _names(tasks)
    compute = next(i for i, n in enumerate(names) if "Compute the VLANs whose host gateway to decommission" in n)
    teardown = next(i for i, n in enumerate(names) if "Tear down the host L3 gateway" in n)
    assert compute < teardown
    argv = [str(a) for a in tasks[teardown]["ansible.builtin.command"]["argv"]]
    assert "sdn_gateway_decommissioned_vlan_ids={{ platform_cs_gateway_decommission_json }}" in argv
    assert "sdn_gateway_active_vlan_ids={{ [] | to_json }}" in argv, "nothing is served after a clean-slate"


# --------------------------------------------------------------------------- #
# The key=value --extra-vars token must contain NO whitespace (found live)
# --------------------------------------------------------------------------- #
def _json_fact(task, key, extra_vars):
    t = _by(_tasks(task[0]), task[1])
    tv = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t.get("vars", {}).items()}
    return _tpl(t["ansible.builtin.set_fact"][key], {**tv, **extra_vars})


def test_BUG_the_platform_clean_slate_decommission_token_has_no_whitespace():
    """`to_json` renders '[20, 100]'; Ansible splits key=value extra-vars on whitespace, so VLAN 100 was dropped
    and its sdn-gw-100.cfg stanza survived the clean-slate (found live)."""
    t = _by(_tasks(_MAIN), "Compute the VLANs whose host gateway to decommission")
    tv = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t["vars"].items()}
    tv["platform_cs_gateway_registry"] = {"projects": [{"slug": "a", "vlan_id": 100}, {"slug": "b", "vlan_id": 103}]}
    j = _tpl(t["ansible.builtin.set_fact"]["platform_cs_gateway_decommission_json"],
             {"platform_clean_slate_shared_vlan_id": 20, **tv})
    assert j == "[20,100,103]" and " " not in j


def test_BUG_the_bring_up_gateway_caller_has_the_same_compact_encoding():
    role = _REPO / "ansible/roles/platform_gateway/tasks/main.yml"
    fact = next(t for t in _tasks(role) if "Encode the served-VLAN sets" in str(t.get("name")))["ansible.builtin.set_fact"]
    sets = {"platform_gateway_vlan_sets": {"active_vlan_ids": [100, 101], "decommissioned_vlan_ids": [102, 103]}}
    assert _tpl(fact["platform_gateway_active_json"], sets) == "[100,101]"
    assert _tpl(fact["platform_gateway_decomm_json"], sets) == "[102,103]"


def _calc(stanza_stdout, registry_vlans):
    t = _by(_tasks(_MAIN), "Compute the VLANs whose host gateway to decommission")
    tv = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t["vars"].items()}
    tv["platform_cs_gateway_registry"] = {"projects": [{"slug": f"s{v}", "vlan_id": v} for v in registry_vlans]}
    return _tpl("{{ platform_cs_gateway_vlans_calc }}",
                {"platform_clean_slate_shared_vlan_id": 20,
                 "platform_cs_gateway_stanzas": {"stdout": stanza_stdout}, **tv})


def test_BUG_an_orphan_gateway_stanza_unknown_to_the_registry_is_decommissioned_too():
    """Live: sdn-gw-103.cfg belonged to a VLAN (p103) the registry did not know; it survived the clean-slate."""
    out = "interfaces.d\nsdn\nsdn-gw-100.cfg\nsdn-gw-103.cfg\n"
    assert _calc(out, [100]) == [20, 100, 103]


def test_the_management_vlan_stanza_is_never_decommissioned_by_discovery():
    assert _calc("sdn-gw-10.cfg\nsdn-gw-100.cfg\n", []) == [20, 100]


def test_no_stanzas_and_an_unreachable_host_degrade_to_shared_plus_registry():
    assert _calc("", [100]) == [20, 100]
    t = _by(_tasks(_MAIN), "Discover the host gateway stanzas present")
    assert t["failed_when"] is False and t["changed_when"] is False, "read-only; never fails the teardown itself"


def test_discovery_is_read_only_and_precedes_the_compute():
    names = _names(_tasks(_MAIN))
    d = next(i for i, n in enumerate(names) if "Discover the host gateway stanzas present" in n)
    c = next(i for i, n in enumerate(names) if "Compute the VLANs whose host gateway to decommission" in n)
    assert d < c
    argv = [str(a) for a in _by(_tasks(_MAIN), "Discover the host gateway stanzas present")["ansible.builtin.command"]["argv"]]
    assert argv[-1] == "ls /etc/network/interfaces.d" and "ansible.builtin.command" in argv
