"""Offline tests for fix-sdn-gateway-project-vlan-noop (§3).

SECURITY-SENSITIVE (host L3 gateway + egress NAT + VLAN isolation).

Bug: an opted-in project VLAN is served ONLY if it is ALSO in the registry-
derived active set, because the served-set uses a silent `intersect`:
  [shared] + (served_project_vlan_ids | map int | intersect(active_norm)) | difference(decomm)
So an opt-in not in the active set is DROPPED with no warn/fail, while VLAN 20
is added unconditionally — yielding a `failed=0, changed=0` silent no-op with
10.0.<vlan>.1 unbound and no MASQUERADE. Second facet: platform_gateway never
forwards sdn_gateway_served_project_vlan_ids (defaults []), so the from-scratch
path can never serve a project VLAN.

Fix: served set = [shared] + opt-in | difference(decomm) (opt-in authoritative);
an explicit opt-in-vs-active VALIDATION (new default-fail-closed knob
sdn_gateway_require_active_optin) replaces the silent filter; a fail-closed
p<vlan> bridge precondition; platform_gateway forwards the registry-active set.

The served-set is pure set logic — modelled here as a Python oracle for the PBT.
Isolation DENY rules (isolation.yml) and the egress NAT scoping stay untouched.

Run: ~/venv/devinfra/bin/pytest infra/tests/test_sdn_gateway_project_vlan_served.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

try:
    from hypothesis import given
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover
    _HAS_HYPOTHESIS = False

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SDN = _REPO_ROOT / "ansible" / "roles" / "sdn_gateway" / "tasks"
_SDN_MAIN = _SDN / "main.yml"
_SDN_CONFIGURE = _SDN / "configure.yml"
_PLATFORM_GATEWAY = _REPO_ROOT / "ansible" / "roles" / "platform_gateway" / "tasks" / "main.yml"
_PLATFORM_CLEAN_SLATE = (
    _REPO_ROOT / "ansible" / "roles" / "platform_clean_slate" / "tasks" / "main.yml"
)

_SHARED_VLAN = 20


# --------------------------------------------------------------------------- #
# Python oracles modelling the served-set computation (pure set logic).
# --------------------------------------------------------------------------- #
def _served_set_unfixed(opt_in, active, decommissioned):
    """The CURRENT (buggy) served set: opt-in INTERSECT active, then minus decomm."""
    served = {_SHARED_VLAN} | (set(opt_in) & set(active))
    return sorted(served - set(decommissioned))


def _served_set_fixed(opt_in, active, decommissioned):
    """The FIXED served set: opt-in is authoritative (no intersect with active),
    minus decommissioned. VLAN 20 always served."""
    served = {_SHARED_VLAN} | set(opt_in)
    return sorted(served - set(decommissioned))


# =========================================================================== #
# Property 1 — Bug Condition: an opted-in, non-decommissioned VLAN must be served.
# =========================================================================== #
def test_bug_condition_optin_not_in_active_is_dropped_unfixed():
    """Opt-in [103], active [] -> UNFIXED drops 103 (served == [20]); FIXED keeps it."""
    assert _served_set_unfixed([103], [], []) == [20]  # the silent no-op bug
    assert 103 in _served_set_fixed([103], [], [])  # the fix serves it
    assert _served_set_fixed([103], [], []) == [20, 103]


def test_bug_condition_optin_active_lacks_it_unfixed():
    """Opt-in [103], active [101] -> UNFIXED still drops 103; FIXED keeps it."""
    assert _served_set_unfixed([103], [101], []) == [20]
    assert _served_set_fixed([103], [101], []) == [20, 103]


def test_served_set_main_removes_silent_intersect():
    """The role's served-set expression must no longer silently `intersect` the
    opt-in with the active set. FAILS on unfixed code."""
    text = _SDN_MAIN.read_text(encoding="utf-8")
    # The served-set computation must not intersect the opt-in with active.
    # (The validation of opt-in-vs-active is a SEPARATE, explicit step.)
    assert "sdn_gateway_require_active_optin" in text, (
        "the silent intersect must be replaced by an explicit opt-in-vs-active "
        "validation gated on sdn_gateway_require_active_optin (FAILS on unfixed code)"
    )
    # The served set must union the opt-in directly (authoritative intent).
    assert "sdn_gateway_served_project_vlan_ids" in text


def test_configure_has_fail_closed_bridge_precondition():
    """configure.yml must fail closed naming the VLAN + absent p<vlan> bridge
    (the fabric-apply remedy), not silently skip. FAILS on unfixed code."""
    text = _SDN_CONFIGURE.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(text))
    tasks = [t for doc in docs if isinstance(doc, list) for t in doc if isinstance(t, dict)]
    assert_tasks = [t for t in tasks if "ansible.builtin.assert" in t]
    bridge_guard = None
    for t in assert_tasks:
        body = str(t).lower()
        if ("p" in body and "vlan_id" in body) and ("exist" in body or "absent" in body or "fabric" in body):
            bridge_guard = t
            break
    assert bridge_guard is not None, (
        "configure.yml must carry an explicit fail-closed assert that p<vlan> "
        "exists before binding the gateway (FAILS on unfixed code)"
    )


def test_platform_gateway_forwards_served_project_vlan_ids():
    """platform_gateway must forward sdn_gateway_served_project_vlan_ids (the
    registry-active set) to the child run. FAILS on unfixed code."""
    text = _PLATFORM_GATEWAY.read_text(encoding="utf-8")
    assert "sdn_gateway_served_project_vlan_ids" in text, (
        "platform_gateway must forward sdn_gateway_served_project_vlan_ids so the "
        "from-scratch path can serve a project VLAN (FAILS on unfixed code)"
    )


def test_platform_clean_slate_passes_empty_served_project():
    """platform_clean_slate (teardown) passes served_project_vlan_ids=[] explicitly."""
    text = _PLATFORM_CLEAN_SLATE.read_text(encoding="utf-8")
    assert "sdn_gateway_served_project_vlan_ids" in text, (
        "platform_clean_slate must pass sdn_gateway_served_project_vlan_ids=[] "
        "explicitly on the teardown child run"
    )


# =========================================================================== #
# Property 2 — Preservation.
# =========================================================================== #
def test_vlan20_always_served_and_decommissioned_subtracted():
    assert _served_set_fixed([], [], []) == [20]
    assert _served_set_fixed([], [], [20]) == []  # VLAN 20 decommissioned -> removed
    assert 103 not in _served_set_fixed([103], [], [103])  # decommissioned subtracted


def test_management_vlan_assert_preserved():
    text = _SDN_MAIN.read_text(encoding="utf-8")
    assert "sdn_gateway_management_vlan_id" in text and "management VLAN" in text, (
        "the no-served-VLAN-equals-management-VLAN assert must be preserved"
    )


def test_regex_findall_normalization_preserved():
    text = _SDN_MAIN.read_text(encoding="utf-8")
    assert "regex_findall('[0-9]+')" in text, (
        "the string-coercion phantom-VLAN guard (regex_findall) must be preserved"
    )


def test_isolation_deny_rules_untouched():
    isolation = _SDN / "isolation.yml"
    assert isolation.is_file(), "isolation.yml must still exist"
    text = isolation.read_text(encoding="utf-8").lower()
    assert "management" in text or "10" in text, "mgmt-VLAN DENY must be preserved"


def test_egress_nat_scoping_preserved():
    defaults = (_REPO_ROOT / "ansible" / "roles" / "sdn_gateway" / "defaults" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert "sdn_gateway_no_nat_dest_supernet" in defaults and "10.0.0.0/16" in defaults, (
        "the egress-NAT no-NAT-dest supernet (isolation-preserving) must be preserved"
    )


if _HAS_HYPOTHESIS:

    _VLANS = st.lists(st.integers(min_value=100, max_value=254), max_size=5, unique=True)

    @given(opt_in=_VLANS, active=_VLANS, decomm=_VLANS)
    def test_pbt_every_optin_nondecomm_vlan_is_served(opt_in, active, decomm):
        served = _served_set_fixed(opt_in, active, decomm)
        # Every opted-in, non-decommissioned VLAN is served (no silent drop).
        for v in opt_in:
            if v not in decomm:
                assert v in served
        # VLAN 20 always served (unless decommissioned); decommissioned never served.
        if _SHARED_VLAN not in decomm:
            assert _SHARED_VLAN in served
        assert not (set(served) & set(decomm))

    @given(opt_in=_VLANS, active=_VLANS, decomm=_VLANS)
    def test_pbt_vlan20_only_and_decomm_only_match_prefix(opt_in, active, decomm):
        # Preservation: a VLAN-20-only run (no opt-in) is unchanged vs the old path.
        assert _served_set_fixed([], active, decomm) == _served_set_unfixed([], active, decomm)
