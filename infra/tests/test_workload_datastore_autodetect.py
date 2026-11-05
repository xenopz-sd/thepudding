"""Offline tests for fix-workload-datastore-autodetect (§5a).

Bug: the consumer WORKLOAD provisioning path never auto-detects the Proxmox
datastore. On the `__AUTODETECT__` sentinel it OMITS `-var=workload_datastore_id`,
letting the workload Terraform root fall back to its hard-coded `local-zfs`
default — while the SVC-01 (DB) path autodetects (local-zfs preferred, else
local-lvm). On a `local-lvm`-only node the DB lands on `local-lvm` (works) and
the workload targets a nonexistent `local-zfs` (fails) in the SAME run.

The fix introduces one shared pure selector (`datastore_select.select_datastore`)
used by BOTH preflights and threads a single resolved value to both applies.

Property 1 (Bug Condition): the workload path selects the SAME datastore as the
SVC-01 path on the same node (or fails closed), never the stale hard-coded
`local-zfs` when it is absent.
Property 2 (Preservation): explicit override verbatim; local-zfs preference;
the SVC-01 inline rule reproduced exactly; disabled-workload no-op.

Run: ~/venv/devinfra/bin/pytest infra/tests/test_workload_datastore_autodetect.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

try:
    from hypothesis import given
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover
    _HAS_HYPOTHESIS = False

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO_ROOT / "scripts" / "installer"
sys.path.insert(0, str(_INSTALLER))

from cluster_env import AUTODETECT  # noqa: E402
from datastore_select import select_datastore  # noqa: E402

_CONSUMER_PROVISION_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_provision" / "tasks" / "main.yml"
)
_CONSUMER_PREFLIGHT_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_preflight" / "tasks" / "main.yml"
)
_SVC01_PREFLIGHT_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc01_preflight" / "tasks" / "main.yml"
)


# --------------------------------------------------------------------------- #
# Reference oracle: the SVC-01 inline selection rule as it was (and must stay).
# --------------------------------------------------------------------------- #
def _svc01_reference_select(node_storage_ids, configured):
    """The original svc01_preflight rule: explicit value verbatim; else local-zfs
    if present; else local-lvm if present; else the sentinel (unresolved)."""
    configured = (configured or "").strip()
    if configured and configured != AUTODETECT:
        return configured
    if "local-zfs" in node_storage_ids:
        return "local-zfs"
    if "local-lvm" in node_storage_ids:
        return "local-lvm"
    return AUTODETECT  # unresolved -> svc01 asserts fail-closed


def _helper_select(node_storage_ids, configured):
    """The fixed consumer-workload selection: the shared helper's resolved value,
    or the sentinel when unresolved (so the comparison with the reference oracle's
    unresolved==sentinel is apples-to-apples)."""
    res = select_datastore(node_storage_ids, configured)
    return res.datastore if res.resolved else AUTODETECT


# =========================================================================== #
# Property 1 — Bug Condition / Expected Behavior.
#
# The helper (which the fixed consumer workload path uses) selects the SAME
# datastore as the SVC-01 path, and never stale local-zfs when it is absent.
#
# The offline test exercises the SHARED SELECTOR the fix introduces. On unfixed
# code this file FAILS at import collection iff the helper is absent — but the
# helper lands in Task 3.1; the genuine bug-condition failure is asserted
# against the CURRENT consumer-path behaviour modelled below.
# =========================================================================== #
def _current_consumer_workload_select(node_storage_ids, configured):
    """Model the CURRENT (unfixed) consumer workload datastore decision:

    consumer_provision binds the raw TF_VAR_openbao_datastore_id and, when it is
    the sentinel (or empty), OMITS -var=workload_datastore_id — so the workload
    Terraform root falls back to its hard-coded `default = "local-zfs"`. When an
    explicit value is set, it is threaded verbatim.
    """
    configured = (configured or "").strip()
    if configured and configured != AUTODETECT:
        return configured
    # sentinel/empty -> -var omitted -> root default:
    return "local-zfs"


@pytest.mark.parametrize(
    "node_storage_ids",
    [
        {"local-lvm"},
        {"local-lvm", "nfs-bulk"},
    ],
)
def test_bug_condition_current_consumer_disagrees_with_svc01_on_lvm_node(node_storage_ids):
    """Case 1: __AUTODETECT__ on a node WITHOUT local-zfs.

    The CURRENT consumer workload path picks `local-zfs` (via omission) while the
    SVC-01 path autodetects `local-lvm` — they disagree, and the workload targets
    a nonexistent datastore. This is isBugCondition(X) == true. Documented here as
    the counterexample; the fixed helper (asserted below) resolves it.
    """
    current = _current_consumer_workload_select(node_storage_ids, AUTODETECT)
    svc01 = _svc01_reference_select(node_storage_ids, AUTODETECT)
    # Demonstrate the bug on the CURRENT behaviour: they disagree and current == stale local-zfs.
    assert current == "local-zfs"
    assert svc01 == "local-lvm"
    assert current != svc01  # the defect

    # The FIX: the shared helper (used by the fixed consumer path) agrees with svc01.
    fixed = _helper_select(node_storage_ids, AUTODETECT)
    assert fixed == svc01, "fixed workload selection must equal the SVC-01 selection"
    assert fixed == "local-lvm"
    assert fixed != "local-zfs", "must never pick stale local-zfs when it is absent"


def test_bug_condition_neither_candidate_fails_closed():
    """Case 2: __AUTODETECT__ on a node with NEITHER local-zfs NOR local-lvm.

    The fixed path fails closed (unresolved) before any apply, instead of
    targeting a nonexistent `local-zfs`.
    """
    node_storage_ids = {"nfs-bulk"}
    res = select_datastore(node_storage_ids, AUTODETECT)
    assert res.resolved is False, "neither candidate present must fail closed (unresolved)"
    assert res.datastore == ""


def test_not_bug_local_zfs_present_both_pick_local_zfs():
    """Case 3 (NOT a counterexample): local-zfs present -> both pick local-zfs.

    Documents why the bug was masked on local-zfs nodes (the workload's hard-coded
    default happened to coincide with the autodetected value)."""
    node_storage_ids = {"local-zfs", "local-lvm"}
    current = _current_consumer_workload_select(node_storage_ids, AUTODETECT)
    fixed = _helper_select(node_storage_ids, AUTODETECT)
    svc01 = _svc01_reference_select(node_storage_ids, AUTODETECT)
    assert current == fixed == svc01 == "local-zfs"


# =========================================================================== #
# Property 2 — Preservation.
# =========================================================================== #
def test_explicit_override_verbatim_no_query():
    for configured in ("local-zfs", "local-lvm", "nfs-bulk", "ceph-rbd-1"):
        # Even with an EMPTY node-storage list, an explicit override is returned
        # verbatim (no query needed).
        res = select_datastore([], configured)
        assert res.resolved is True
        assert res.datastore == configured
        assert res.source == "override"


def test_local_zfs_preference():
    # local-zfs always wins over local-lvm (and anything else) on the sentinel.
    res = select_datastore({"local-lvm", "local-zfs", "nfs-bulk"}, AUTODETECT)
    assert res.datastore == "local-zfs" and res.source == "autodetect"


def test_svc01_reference_rule_reproduced_by_helper():
    """The shared helper reproduces the SVC-01 inline rule for every storage set
    (the refactor of svc01_preflight onto the helper is behaviour-preserving)."""
    cases = [
        set(),
        {"local-zfs"},
        {"local-lvm"},
        {"local-zfs", "local-lvm"},
        {"nfs-bulk"},
        {"local-lvm", "nfs-bulk"},
        {"local-zfs", "nfs-bulk", "ceph-1"},
    ]
    for ids in cases:
        assert _helper_select(ids, AUTODETECT) == _svc01_reference_select(ids, AUTODETECT)


def test_empty_configured_treated_as_autodetect():
    assert select_datastore({"local-lvm"}, "").datastore == "local-lvm"
    assert select_datastore({"local-lvm"}, AUTODETECT).datastore == "local-lvm"


if _HAS_HYPOTHESIS:

    _STORAGE_IDS = st.lists(
        st.sampled_from(["local-zfs", "local-lvm", "nfs-bulk", "ceph-1", "dir-backup"]),
        max_size=5,
    )

    @given(ids=_STORAGE_IDS, configured=st.sampled_from(["local-zfs", "local-lvm", "nfs-bulk", "ceph-1"]))
    def test_pbt_explicit_override_always_verbatim(ids, configured):
        assert select_datastore(ids, configured).datastore == configured

    @given(ids=_STORAGE_IDS)
    def test_pbt_helper_equals_svc01_reference(ids):
        # Preservation: helper == original svc01 inline rule across the domain.
        assert _helper_select(set(ids), AUTODETECT) == _svc01_reference_select(set(ids), AUTODETECT)

    @given(ids=_STORAGE_IDS)
    def test_pbt_determinism(ids):
        a = select_datastore(set(ids), AUTODETECT)
        b = select_datastore(set(ids), AUTODETECT)
        assert a == b


# =========================================================================== #
# Argv-level integration + role-structure assertions (Task 4).
# These check the FIXED Ansible wiring: both applies receive the single resolved
# value; svc01_preflight is refactored onto the helper; consumer_preflight hoists
# the resolution.
# =========================================================================== #
def test_consumer_provision_always_passes_workload_datastore_var():
    """After the fix, the workload apply ALWAYS appends
    -var=workload_datastore_id (never the omit-on-sentinel branch)."""
    text = _CONSUMER_PROVISION_TASKS.read_text(encoding="utf-8")
    assert "workload_datastore_id=" in text
    # The old omit-on-sentinel conditional (append only when != sentinel) must be gone.
    assert "consumer_autodetect_sentinel" not in text or "consumer_resolved_datastore" in text, (
        "the workload apply must thread the Phase-1 resolved datastore, not the "
        "omit-on-sentinel branch"
    )
    assert "consumer_resolved_datastore" in text, (
        "the workload apply must use the single Phase-1 consumer_resolved_datastore"
    )


def test_consumer_preflight_hoists_single_resolution():
    text = _CONSUMER_PREFLIGHT_TASKS.read_text(encoding="utf-8")
    assert "consumer_resolved_datastore" in text, (
        "consumer_preflight must resolve the datastore once (consumer_resolved_datastore)"
    )
    assert "datastore_select" in text, (
        "consumer_preflight must invoke the shared datastore_select helper"
    )


def test_svc01_preflight_uses_shared_helper():
    text = _SVC01_PREFLIGHT_TASKS.read_text(encoding="utf-8")
    assert "datastore_select" in text, (
        "svc01_preflight selection must be single-sourced via datastore_select.py"
    )
