"""Hypothesis property + example suite for the pure registry-view layer.

Feature: sdn-vlan-gateway-reachability

This module implements Properties 1-4 from the feature ``design.md``
("Correctness Properties" section) over the pure registry-view layer
(``netfoundation.registry_views`` — ``normalize_status``, ``all_vlan_ids``,
``active_vlan_ids``), plus the example/edge unit tests from the design's
"Testing Strategy" (§1-§2).

``projects.yaml`` is one append-only file read two ways: the
Allocation_Ledger_View (``all_vlan_ids`` — every recorded VLAN ID regardless of
status, the input to ``next_vlan_id`` so a retired ID is never reused) and the
Desired_State_View (``active_vlan_ids`` — the VLAN IDs of ``status: active``
rows, the input to the ``sdn_gateway`` converge role). These functions are pure
and side-effect-free over a well-defined input space, so the four properties
assert universal characteristics (allocation invariance to decommissioning, the
desired-state view being exactly the active subset, missing status degrading to
active, and the ledger counting all rows) rather than single worked examples
(those live in the example/edge unit tests below).

Rules honored (per tasks.md 5 and the testing-strategy steering, mirroring
``test_property_derivation.py``):
- Every property is exactly ONE Hypothesis property-based test.
- Every test runs a MINIMUM of 100 iterations (``settings(max_examples=100)``).
- Generators are Hypothesis strategies, never hand-rolled loops.
- Each test carries the exact design property text in its docstring, tagged
  ``Feature: sdn-vlan-gateway-reachability, Property N: {property_text}``, with
  a ``Validates: Requirements ...`` line.
"""

from __future__ import annotations

import copy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from netfoundation.derivation import MAX_PROJECT_VLAN_ID, MIN_PROJECT_VLAN_ID
from netfoundation.registry_views import (
    DEFAULT_STATUS,
    VALID_STATUSES,
    active_vlan_ids,
    all_vlan_ids,
    normalize_status,
)
from onboard_project import next_vlan_id

# --- Minimum-iteration setting shared by every property ------------------

# Every property test runs at least 100 examples per tasks.md 5.
PROPERTY_SETTINGS = settings(max_examples=100)


# --- Reusable Hypothesis strategies (not hand-rolled generators) ---------

# A token: lowercase alphanumeric segments joined by single hyphens, used for
# the slug field (mirrors the derivation suite's ``tokens`` strategy).
tokens = st.from_regex(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z", fullmatch=True).filter(
    lambda s: len(s) <= 20
)

# An ISO 8601 onboarding date. Any well-formed date-ish string works — the view
# functions never inspect it — so a fixed literal keeps generation cheap.
onboarding_dates = st.just("2024-01-01")

# A per-row ``status`` field. Sometimes present and one of the valid values,
# sometimes absent (the key is omitted), sometimes an empty string — every one
# of which the view layer must handle (present/absent mix, active/decommissioned
# mix, empty ⇒ active).
_status_values = st.sampled_from([*VALID_STATUSES, "", None])


@st.composite
def registries(draw):
    """Synthesize a parsed registry mapping ``{"projects": [...]}``.

    Rows carry ``{slug, vlan_id, onboarding_date}`` always, and ``status`` in a
    mix of forms: present-``active``, present-``decommissioned``, absent (key
    omitted), and empty-string. VLAN IDs are drawn unique within the valid
    per-project range so ``next_vlan_id`` has well-defined behaviour and no two
    rows collide. Slugs are drawn unique to mirror the real registry's
    never-reuse rule, though the view functions do not depend on it.
    """
    size = draw(st.integers(min_value=0, max_value=12))
    slugs = draw(
        st.lists(tokens, min_size=size, max_size=size, unique=True)
    )
    # Kept strictly below the ceiling so ``next_vlan_id`` always has a free ID
    # to hand out — VLAN-ID exhaustion is the derivation suite's Property 9,
    # not a concern of the registry views, so it must not perturb Property 1's
    # invariance check.
    vlan_id_pool = st.integers(
        min_value=MIN_PROJECT_VLAN_ID, max_value=MAX_PROJECT_VLAN_ID - 1
    )
    vlan_ids = draw(
        st.lists(vlan_id_pool, min_size=size, max_size=size, unique=True)
    )
    projects = []
    for slug, vlan_id in zip(slugs, vlan_ids):
        row = {
            "slug": slug,
            "vlan_id": vlan_id,
            "onboarding_date": draw(onboarding_dates),
        }
        status = draw(_status_values)
        if status is not None:
            row["status"] = status
        projects.append(row)
    return {"projects": projects}


def _expected_active_ids(registry):
    """Reference computation of the active subset, independent of the SUT."""
    return [
        row["vlan_id"]
        for row in registry["projects"]
        if (row.get("status") or DEFAULT_STATUS) == "active"
    ]


def _decommission_subset(registry, mask):
    """Return a deep copy of ``registry`` with the ``mask``-selected rows flipped
    to ``status: decommissioned`` (leaving other rows' status untouched)."""
    flipped = copy.deepcopy(registry)
    for row, flip in zip(flipped["projects"], mask):
        if flip:
            row["status"] = "decommissioned"
    return flipped


# --- Property 1: Allocation is invariant to decommissioning --------------


@PROPERTY_SETTINGS
@given(data=st.data())
def test_property_1_allocation_invariant_to_decommissioning(data):
    """Feature: sdn-vlan-gateway-reachability, Property 1: Allocation is invariant to decommissioning

    For any synthesized registry, ``next_vlan_id(all_vlan_ids(registry))`` is
    unchanged when any subset of rows is flipped to ``decommissioned``.
    Retiring a project never lowers the ceiling or frees an ID.

    Validates: Requirements 4.5, 5.2
    """
    registry = data.draw(registries())
    rows = registry["projects"]
    mask = data.draw(
        st.lists(st.booleans(), min_size=len(rows), max_size=len(rows))
    )

    before = next_vlan_id(all_vlan_ids(registry))

    flipped = _decommission_subset(registry, mask)
    after = next_vlan_id(all_vlan_ids(flipped))

    assert before == after


# --- Property 2: Desired-state view is exactly the active subset ---------


@PROPERTY_SETTINGS
@given(registry=registries())
def test_property_2_desired_state_is_active_subset(registry):
    """Feature: sdn-vlan-gateway-reachability, Property 2: Desired-state view is exactly the active subset

    For any synthesized registry, ``active_vlan_ids(registry)`` equals the VLAN
    IDs of rows whose normalized status is ``active`` — every active row
    present, every decommissioned row absent, none duplicated, and
    ``active_vlan_ids ⊆ all_vlan_ids``.

    Validates: Requirements 6.1, 6.3
    """
    active = active_vlan_ids(registry)
    all_ids = all_vlan_ids(registry)

    # Exactly the active subset, in registry order.
    assert active == _expected_active_ids(registry)

    # Every active row present, every decommissioned row absent.
    for row in registry["projects"]:
        if normalize_status(row) == "active":
            assert row["vlan_id"] in active
        else:
            assert row["vlan_id"] not in active

    # None duplicated.
    assert len(active) == len(set(active))

    # Subset of the ledger view.
    assert set(active).issubset(set(all_ids))


# --- Property 3: Missing status defaults to active (safe degradation) ----


@PROPERTY_SETTINGS
@given(data=st.data())
def test_property_3_missing_status_defaults_to_active(data):
    """Feature: sdn-vlan-gateway-reachability, Property 3: Missing status defaults to active (safe degradation)

    For any synthesized registry where an arbitrary subset of rows omits
    ``status``, those rows are treated as ``active`` — they appear in
    ``active_vlan_ids`` and ``all_vlan_ids`` identically to an explicit
    ``status: active`` row. A registry with no ``status`` anywhere behaves
    exactly as the pre-feature registry.

    Validates: Requirements 5.2, FD.3
    """
    registry = data.draw(registries())
    rows = registry["projects"]

    # Build an "explicit" twin where every row that would default to active
    # (missing/empty/active) carries an explicit ``status: active`` instead.
    explicit = copy.deepcopy(registry)
    for row in explicit["projects"]:
        if normalize_status(row) == "active":
            row["status"] = "active"

    # Omitting/empty status behaves identically to explicit active.
    assert active_vlan_ids(registry) == active_vlan_ids(explicit)
    assert all_vlan_ids(registry) == all_vlan_ids(explicit)

    # A registry with no status field anywhere behaves as the pre-feature
    # registry: every recorded ID is active.
    stripped = copy.deepcopy(registry)
    for row in stripped["projects"]:
        row.pop("status", None)
    assert active_vlan_ids(stripped) == all_vlan_ids(stripped)
    assert all_vlan_ids(stripped) == [row["vlan_id"] for row in rows]


# --- Property 4: Ledger counts all rows regardless of status -------------


@PROPERTY_SETTINGS
@given(data=st.data())
def test_property_4_ledger_counts_all_rows(data):
    """Feature: sdn-vlan-gateway-reachability, Property 4: Ledger counts all rows regardless of status

    For any synthesized registry, ``all_vlan_ids(registry)`` contains every
    recorded VLAN ID exactly once regardless of each row's status, so the
    allocation-ledger view never shrinks when a row is decommissioned.

    Validates: Requirements 4.5, 5.2
    """
    registry = data.draw(registries())
    recorded = [row["vlan_id"] for row in registry["projects"]]

    ledger = all_vlan_ids(registry)

    # Every recorded ID present, exactly once, in registry order.
    assert ledger == recorded
    assert len(ledger) == len(set(ledger))

    # Never shrinks on decommission: flip an arbitrary subset and re-check.
    mask = data.draw(
        st.lists(st.booleans(), min_size=len(recorded), max_size=len(recorded))
    )
    flipped = _decommission_subset(registry, mask)
    assert all_vlan_ids(flipped) == recorded


# --- Example / edge unit tests (design Testing Strategy §2) --------------


class TestNormalizeStatusExamples:
    """Worked examples for :func:`normalize_status`."""

    def test_present_active(self):
        assert normalize_status({"status": "active"}) == "active"

    def test_present_decommissioned(self):
        assert normalize_status({"status": "decommissioned"}) == "decommissioned"

    def test_missing_defaults_to_active(self):
        # No ``status`` key at all — degrades to active (Req 5.2, FD.3).
        assert normalize_status({"slug": "x", "vlan_id": 100}) == DEFAULT_STATUS
        assert normalize_status({}) == "active"

    def test_empty_string_defaults_to_active(self):
        assert normalize_status({"status": ""}) == "active"

    def test_none_defaults_to_active(self):
        assert normalize_status({"status": None}) == "active"

    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_status({"status": "suspended"})


class TestLedgerVersusActiveView:
    """A fixed registry with one decommissioned row: the retired ID is absent
    from the desired-state view but present in the allocation ledger."""

    REGISTRY = {
        "projects": [
            {"slug": "dronefleet", "vlan_id": 100, "onboarding_date": "2024-06-01", "status": "active"},
            {"slug": "mlvideo", "vlan_id": 101, "onboarding_date": "2024-06-14", "status": "decommissioned"},
            {"slug": "edge-sensors", "vlan_id": 102, "onboarding_date": "2024-07-02", "status": "active"},
        ]
    }

    def test_ledger_contains_all_ids(self):
        assert all_vlan_ids(self.REGISTRY) == [100, 101, 102]

    def test_active_view_excludes_decommissioned(self):
        assert active_vlan_ids(self.REGISTRY) == [100, 102]

    def test_decommissioned_id_absent_from_active_present_in_ledger(self):
        assert 101 not in active_vlan_ids(self.REGISTRY)
        assert 101 in all_vlan_ids(self.REGISTRY)

    def test_allocation_over_ledger_never_reuses_retired_id(self):
        # next_vlan_id counts the full ledger (max 102), so the retired 101 is
        # never reused — allocation is unaffected by the decommission.
        assert next_vlan_id(all_vlan_ids(self.REGISTRY)) == 103
