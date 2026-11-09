"""Bug-condition + preservation tests: sdn_gateway normalizes the OPTED-IN served VLAN set too (WS-C12 ride-along).

Found live while proving the platform fabric teardown: after the zone and its VNets were removed, the host
L3 gateway teardown child play aborted with "Opted-in project VLAN(s) [0] are NOT in the registry-active set".
Both callers (platform_gateway, platform_clean_slate) pass `sdn_gateway_served_project_vlan_ids` as a single
`--extra-vars "key=[...]"` token, which Ansible parses as the STRING "[]" / "[100]". The role normalized the
active and decommissioned sets but used this one raw, so `map('int')` iterated the string's characters and
produced the phantom VLAN [0] (the same footgun documented in the role). Fix: a `_norm` fact, consumed by the
served-set computation and the opt-in validation.

Pure offline: the real expressions are evaluated with Ansible's templater.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROLE = Path(__file__).resolve().parents[2] / "ansible/roles/sdn_gateway/tasks/main.yml"


def _tasks():
    return [t for t in yaml.safe_load(_ROLE.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _norm(raw):
    t = next(t for t in _tasks() if "Normalize the active / decommissioned VLAN-ID sets" in str(t.get("name")))
    return _tpl(t["ansible.builtin.set_fact"]["sdn_gateway_served_project_vlan_ids_norm"],
                {"sdn_gateway_served_project_vlan_ids": raw})


def test_BUG_the_empty_string_list_no_longer_becomes_the_phantom_vlan_zero():
    """The exact live failure: the platform clean-slate passes served=[] as the STRING '[]'."""
    assert _norm("[]") == [], "'[]' must normalize to an empty set, not [0]"


def test_string_forms_are_reduced_to_their_integers():
    assert _norm("[100]") == [100]
    assert _norm("[100, 101]") == [100, 101]
    assert _norm("100,101") == [100, 101]
    assert _norm("100") == [100]


def test_a_genuine_list_passes_through_unchanged_bring_up_is_preserved():
    assert _norm([100, 101]) == [100, 101]
    assert _norm([]) == []


def _validation_expr():
    t = next(t for t in _tasks() if "Validate every opted-in project VLAN is registry-active" in str(t.get("name")))
    return t["vars"]["_sdn_gateway_opted_in_not_active"]


def test_the_opt_in_validation_is_clean_for_the_teardown_arguments():
    """Teardown args: active=[], decommissioned=[20], served='[]' -> nothing opted in, nothing to reject."""
    v = {"sdn_gateway_served_project_vlan_ids_norm": _norm("[]"),
         "sdn_gateway_active_vlan_ids_norm": [], "sdn_gateway_decommissioned_vlan_ids_norm": [20]}
    assert _tpl(_validation_expr(), v) == []


def test_the_opt_in_validation_still_rejects_a_genuinely_inactive_vlan():
    """Preservation: the fail-closed check is intact for a real inconsistency."""
    v = {"sdn_gateway_served_project_vlan_ids_norm": _norm("[100]"),
         "sdn_gateway_active_vlan_ids_norm": [], "sdn_gateway_decommissioned_vlan_ids_norm": []}
    assert _tpl(_validation_expr(), v) == [100]


def test_served_set_for_the_teardown_is_empty_after_subtracting_the_decommissioned_shared_vlan():
    t = next(t for t in _tasks() if "Compute the served-VLAN set" in str(t.get("name")))
    v = {"sdn_gateway_shared_vlan_id": 20, "sdn_gateway_served_project_vlan_ids_norm": _norm("[]"),
         "sdn_gateway_decommissioned_vlan_ids_norm": [20]}
    assert _tpl(t["ansible.builtin.set_fact"]["sdn_gateway_served_vlan_ids"], v) == []


def test_bring_up_serves_the_shared_vlan_plus_the_opted_in_project_vlan():
    t = next(t for t in _tasks() if "Compute the served-VLAN set" in str(t.get("name")))
    v = {"sdn_gateway_shared_vlan_id": 20, "sdn_gateway_served_project_vlan_ids_norm": _norm("[100]"),
         "sdn_gateway_decommissioned_vlan_ids_norm": []}
    assert _tpl(t["ansible.builtin.set_fact"]["sdn_gateway_served_vlan_ids"], v) == [20, 100]


def test_no_consumer_reads_the_raw_served_variable_any_more():
    text = _ROLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "map('int')" in line:
            assert "sdn_gateway_served_project_vlan_ids " not in line + " ", f"raw served ids fed to map('int'): {line.strip()}"
    norm = next(t for t in _tasks() if "Normalize the active / decommissioned VLAN-ID sets" in str(t.get("name")))
    order = [str(t.get("name")) for t in _tasks()]
    consumers = [i for i, n in enumerate(order) if "Compute the served-VLAN set" in n or "Validate every opted-in" in n]
    assert order.index(str(norm["name"])) < min(consumers), "normalization must precede both consumers"
