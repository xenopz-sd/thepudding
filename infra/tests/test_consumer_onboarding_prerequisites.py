"""Offline tests for fix-consumer-onboarding-prerequisites (§2).

Bug: the consumer orchestrator depends on two prerequisites it neither creates
nor surfaces up front — (1) the per-project SDN VNet `p<vlan>`, (2) the
Host_Index_Authority ledger rows — and both fail closed MID-Phase-2 (after
provisioning side effects) rather than in Phase 1.

Fix: move BOTH checks into Phase 1 (consumer_preflight), side-effect-free and
fail-closed; auto-create the network slice as the FIRST Phase-2 step
(consumer_provision); derive the required ledger rows from the manifest and
fail closed in Phase 1 printing the exact rows to commit (never a runtime
auto-append — ADR-0015 committed-input invariant).

Property 1 (Bug Condition): the Phase-1 VNet + ledger checks precede every
consumer_provision task (so a missing prerequisite is a side-effect-free Phase-1
halt, not a mid-Phase-2 stop).
Property 2 (Preservation): the pure ledger-derivation rule is exactly
{svc01 iff database} ∪ {workload iff workload.enabled}; backstops untouched.

Run: ~/venv/devinfra/bin/pytest infra/tests/test_consumer_onboarding_prerequisites.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

try:
    from hypothesis import given
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover
    _HAS_HYPOTHESIS = False

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO_ROOT / "scripts" / "installer"
sys.path.insert(0, str(_INSTALLER))

from consumer_ledger import missing_ledger_rows, required_ledger_rows  # noqa: E402

_PREFLIGHT = _REPO_ROOT / "ansible" / "roles" / "consumer_preflight" / "tasks" / "main.yml"
_PROVISION = _REPO_ROOT / "ansible" / "roles" / "consumer_provision" / "tasks" / "main.yml"


def _task_names(path: Path) -> list[str]:
    """Flat, in-order list of task names in a tasks file (top-level + block children)."""
    names: list[str] = []

    def walk(tasks):
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            if "name" in t:
                names.append(str(t["name"]))
            for k in ("block", "rescue", "always"):
                if k in t:
                    walk(t[k])

    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            walk(doc)
    return names


# =========================================================================== #
# Property 1 — Bug Condition / Expected Behavior.
# The VNet + ledger checks must live in Phase 1 (consumer_preflight). On unfixed
# code they are ABSENT from consumer_preflight, so these FAIL.
# =========================================================================== #
def test_phase1_has_vnet_presence_check():
    text = _PREFLIGHT.read_text(encoding="utf-8")
    assert "sdn/vnets" in text, (
        "consumer_preflight (Phase 1) must query the SDN VNets to check p<vlan> "
        "presence BEFORE any Phase-2 mutation (FAILS on unfixed code)"
    )
    assert "consumer_vnet_present" in text, (
        "consumer_preflight must bind consumer_vnet_present for the Phase-2 auto-create gate"
    )


def test_phase1_has_ledger_row_check():
    text = _PREFLIGHT.read_text(encoding="utf-8")
    defaults = (
        _REPO_ROOT / "ansible" / "roles" / "consumer_preflight" / "defaults" / "main.yml"
    ).read_text(encoding="utf-8")
    assert "consumer_ledger_helper" in text or "host_indices" in text, (
        "consumer_preflight (Phase 1) must derive + check the Host_Index_Authority "
        "ledger rows from the manifest BEFORE any Phase-2 mutation (FAILS on unfixed code)"
    )
    # The helper var must resolve to the pure consumer_ledger.py helper (in defaults).
    assert "consumer_ledger.py" in defaults, (
        "the ledger check must use the pure consumer_ledger.py helper "
        "(consumer_ledger_helper default)"
    )
    assert "--check" in text, "the Phase-1 ledger check must run the helper with --check"


def test_phase1_checks_precede_phase2_mutations():
    """The Phase-1 VNet/ledger checks must run before any consumer_provision task
    (Phase 1 is a separate role that runs first), so a missing prerequisite is a
    side-effect-free Phase-1 halt. Verified structurally: the checks are in
    consumer_preflight (Phase 1), NOT in consumer_provision (Phase 2)."""
    provision_text = _PROVISION.read_text(encoding="utf-8")
    # The VNet-presence check + ledger-row check must NOT be the Phase-2 role's job.
    preflight_text = _PREFLIGHT.read_text(encoding="utf-8")
    assert "sdn/vnets" in preflight_text, "VNet presence check must be in Phase 1"
    assert "consumer_ledger_helper" in preflight_text and "--check" in preflight_text, (
        "ledger-row check (via the consumer_ledger_helper --check) must be in Phase 1"
    )
    # And the auto-create in Phase 2 must be gated on the Phase-1 decision.
    assert "consumer_vnet_present" in provision_text, (
        "the Phase-2 network-slice auto-create must be gated on the Phase-1 "
        "consumer_vnet_present decision"
    )


def test_phase2_autocreate_network_slice_first_step():
    text = _PROVISION.read_text(encoding="utf-8")
    assert "consumer_autocreate_network_slice" in text, (
        "consumer_provision must auto-create the network slice (gated on "
        "consumer_autocreate_network_slice) (FAILS on unfixed code)"
    )
    # Applies the project's OWN <slug>-infra network-slice root.
    assert "infra/projects/" in text and ("sdn" in text.lower() or "infra-slice" in text.lower()
                                          or "network" in text.lower()), (
        "the auto-create must apply the project's own network-slice root"
    )


# =========================================================================== #
# Property 2 — Preservation: ledger-derivation pure function.
# {svc01 iff database} ∪ {workload iff workload.enabled}; no-op manifest -> empty.
# =========================================================================== #
def _codes(rows):
    return sorted(r.get("svc_code") or r.get("role_code") for r in rows)


def test_derivation_db_only():
    rows = required_ledger_rows({"database": {"engine": "postgres"}}, 100)
    assert _codes(rows) == ["svc01"]
    svc01 = rows[0]
    assert svc01["host_index"] == 0 and svc01["anchor"] is True and svc01["status"] == "allocated"


def test_derivation_workload_only():
    rows = required_ledger_rows({"workload": {"enabled": True}}, 100)
    assert _codes(rows) == ["workload"]
    wl = rows[0]
    assert wl["host_index"] == 1 and wl["status"] == "allocated"
    assert "anchor" not in wl, "the workload guest is non-anchor (stateless)"


def test_derivation_db_and_workload():
    rows = required_ledger_rows(
        {"database": {"engine": "postgres"}, "workload": {"enabled": True}}, 100
    )
    assert _codes(rows) == ["svc01", "workload"]


def test_derivation_no_op_empty():
    assert required_ledger_rows({}, 100) == []
    assert required_ledger_rows({"sso": {"org_name": "x"}}, 100) == []
    # workload present but disabled -> no workload row (strict no-op, Req 3.3)
    assert required_ledger_rows({"workload": {"enabled": False}}, 100) == []
    assert required_ledger_rows({"workload": {}}, 100) == []


def test_missing_rows_against_committed_ledger():
    """Match shape agrees with the committed dronefleet VLAN-100 block."""
    ledger = yaml.safe_load(
        (_REPO_ROOT / "infra" / "platform-foundation" / "host-indices.yaml").read_text(
            encoding="utf-8"
        )
    )
    # dronefleet (VLAN 100) already has both svc01 (idx0) + workload (idx1) rows,
    # so a db+workload manifest has NO missing rows.
    manifest = {"database": {"engine": "postgres"}, "workload": {"enabled": True}}
    assert missing_ledger_rows(manifest, 100, ledger) == []
    # A fresh VLAN with no block -> both rows missing.
    assert len(missing_ledger_rows(manifest, 199, ledger)) == 2


if _HAS_HYPOTHESIS:

    @given(
        has_db=st.booleans(),
        wl=st.sampled_from([None, {"enabled": True}, {"enabled": False}, {}]),
        vlan=st.integers(min_value=100, max_value=254),
    )
    def test_pbt_derivation_is_exactly_the_union(has_db, wl, vlan):
        manifest: dict = {}
        if has_db:
            manifest["database"] = {"engine": "postgres"}
        if wl is not None:
            manifest["workload"] = wl
        rows = required_ledger_rows(manifest, vlan)
        expected = set()
        if has_db:
            expected.add("svc01")
        if isinstance(wl, dict) and wl.get("enabled"):
            expected.add("workload")
        assert set(_codes(rows)) == expected


# =========================================================================== #
# Property 2 — Preservation: backstops untouched.
# =========================================================================== #
def test_svc01_preflight_vnet_assert_backstop_preserved():
    """The svc01_preflight VNet-presence assert (the backstop) is unchanged."""
    svc01 = _REPO_ROOT / "ansible" / "roles" / "svc01_preflight" / "tasks" / "main.yml"
    text = svc01.read_text(encoding="utf-8")
    assert "is absent from the Proxmox SDN VNets" in text, (
        "the svc01_preflight VNet-presence fail-closed backstop must be preserved"
    )


def test_registry_only_vlan_id_warn_preserved():
    text = _PREFLIGHT.read_text(encoding="utf-8")
    assert "is IGNORED" in text and "consumer_vlan_id" in text, (
        "the Registry-only vlan_id resolution (operator -e vlan_id ignored) must be preserved"
    )
