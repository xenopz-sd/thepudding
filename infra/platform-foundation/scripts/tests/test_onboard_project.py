"""Unit tests for the onboarding script's side effects (task 2.3).

These tests exercise ``onboard_project.onboard`` and its helpers at the
side-effect level: registry mutation and the GitLab merge-request call. The
GitLab API is ALWAYS mocked/faked via the injectable
:class:`~onboard_project.MergeRequestOpener` seam — no live merge request is
ever created (design Testing Strategy §3 "Onboarding flow", constraint FD.2).

Every test operates on a ``tmp_path`` copy of the real registry fixture so the
committed ``infra/platform-foundation/projects.yaml`` is never modified.

Covers:
- Requirement 5.2 — a new slug appends the correct computed entry AND issues an
  open/update-MR call with the correct Allocation.
- Requirement 5.3 — a duplicate slug aborts (OnboardingError) WITHOUT allocating
  and WITHOUT calling the MR opener.
- FD.2 — a corrupt / duplicate-vlan / missing registry aborts and reports
  (OnboardingError) without assigning and without calling the MR opener.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

import onboard_project
from onboard_project import (
    Allocation,
    OnboardingError,
    _NullMergeRequestOpener,
    load_registry,
    onboard,
)

# --- Fixtures -------------------------------------------------------------

#: The committed registry lives two levels up from this test file:
#: scripts/tests/ -> scripts/ -> platform-foundation/projects.yaml
_REAL_REGISTRY = Path(__file__).resolve().parent.parent.parent / "projects.yaml"

#: A fixed onboarding date so the computed Allocation is deterministic.
_FIXED_TODAY = datetime.date(2024, 8, 15)


class FakeMergeRequestOpener:
    """A hand-written fake MR seam that records calls instead of hitting GitLab.

    Implements the :class:`onboard_project.MergeRequestOpener` protocol
    (``open_or_update``). It NEVER performs any network I/O, so no live merge
    request can be created during a test.
    """

    def __init__(self) -> None:
        self.calls: list[Allocation] = []

    def open_or_update(self, allocation: Allocation) -> dict[str, Any]:
        self.calls.append(allocation)
        return {"iid": 1, "web_url": "https://gitlab.example/mr/1"}


@pytest.fixture
def registry_copy(tmp_path: Path) -> Path:
    """A writable ``tmp_path`` copy of the real registry fixture.

    Guarantees the committed ``projects.yaml`` is never touched by these tests.
    """
    dest = tmp_path / "projects.yaml"
    dest.write_text(_REAL_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


@pytest.fixture
def fake_opener() -> FakeMergeRequestOpener:
    """A fresh call-recording fake MR opener per test."""
    return FakeMergeRequestOpener()


# --- Requirement 5.2: new slug appends + issues MR call -------------------


def test_new_slug_appends_entry_and_opens_mr(
    registry_copy: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A new slug appends the correct computed entry AND opens one MR.

    The fixture registry's highest VLAN ID is 102, so the next allocation must
    be 103. The MR opener must be called exactly once with that Allocation.
    """
    result = onboard(
        "newproject",
        registry_copy,
        fake_opener,
        today=_FIXED_TODAY,
    )

    # Returned allocation is the computed, never-reused successor of 102.
    assert result.allocation == Allocation(
        slug="newproject",
        vlan_id=103,
        onboarding_date="2024-08-15",
    )

    # The fake opener returns a non-empty dict, so an MR was opened.
    assert result.mr_opened is True

    # The MR opener was called exactly once, with the same Allocation.
    assert fake_opener.calls == [result.allocation]

    # The registry file gained exactly the new entry and nothing was lost.
    parsed = yaml.safe_load(registry_copy.read_text(encoding="utf-8"))
    slugs = [p["slug"] for p in parsed["projects"]]
    assert slugs == ["dronefleet", "mlvideo", "edge-sensors", "newproject"]

    # The appended row carries ``status: active`` (Requirement 4.6, C1) in
    # addition to the computed slug/vlan_id/onboarding_date. A newly onboarded
    # project is active by definition.
    new_entry = parsed["projects"][-1]
    assert new_entry == {
        "slug": "newproject",
        "vlan_id": 103,
        "onboarding_date": "2024-08-15",
        "status": "active",
    }


def test_new_slug_mr_call_carries_correct_allocation(
    registry_copy: Path,
) -> None:
    """The MR opener (via unittest.mock) is called once with the Allocation.

    Uses a MagicMock implementing the ``open_or_update`` seam to assert the
    exact call — belt-and-suspenders alongside the hand-written fake above.
    """
    mock_opener = mock.MagicMock(spec=onboard_project.MergeRequestOpener)

    result = onboard(
        "another-proj",
        registry_copy,
        mock_opener,
        today=_FIXED_TODAY,
    )

    mock_opener.open_or_update.assert_called_once_with(result.allocation)
    assert result.allocation.vlan_id == 103
    assert result.allocation.slug == "another-proj"


def test_new_slug_into_empty_registry_starts_at_100(
    tmp_path: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A new slug in an empty registry is allocated the floor ID 100."""
    empty = tmp_path / "projects.yaml"
    empty.write_text("projects: []\n", encoding="utf-8")

    result = onboard("first", empty, fake_opener, today=_FIXED_TODAY)

    assert result.allocation.vlan_id == 100
    assert fake_opener.calls == [result.allocation]


def test_null_opener_yields_mr_opened_false(
    registry_copy: Path,
) -> None:
    """The --no-mr null opener returns ``{}``, so ``mr_opened`` is False.

    The allocation is still computed and returned (VLAN 103, the successor of
    the fixture's max of 102); only the ``mr_opened`` signal differs from the
    MR-opening path.
    """
    result = onboard(
        "nomrproject",
        registry_copy,
        _NullMergeRequestOpener(),
        today=_FIXED_TODAY,
    )

    assert result.mr_opened is False
    assert result.allocation == Allocation(
        slug="nomrproject",
        vlan_id=103,
        onboarding_date="2024-08-15",
    )


# --- Requirement 5.3: duplicate slug aborts, no allocation, no MR ---------


def test_duplicate_slug_aborts_without_allocating_or_mr(
    registry_copy: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A slug already in the registry aborts with OnboardingError.

    No new VLAN ID is allocated, the registry file is left byte-for-byte
    unchanged, and the MR opener is never called.
    """
    before = registry_copy.read_text(encoding="utf-8")

    with pytest.raises(OnboardingError, match="already present"):
        onboard("mlvideo", registry_copy, fake_opener, today=_FIXED_TODAY)

    # Nothing was allocated / written.
    assert registry_copy.read_text(encoding="utf-8") == before
    # The MR side effect never fired.
    assert fake_opener.calls == []


def test_duplicate_slug_does_not_call_mock_opener(
    registry_copy: Path,
) -> None:
    """Duplicate-slug abort must not touch the MR seam (mock variant)."""
    mock_opener = mock.MagicMock(spec=onboard_project.MergeRequestOpener)

    with pytest.raises(OnboardingError):
        onboard("dronefleet", registry_copy, mock_opener, today=_FIXED_TODAY)

    mock_opener.open_or_update.assert_not_called()


# --- FD.2: corrupt / duplicate / missing registry aborts, no assign ------


def test_missing_registry_aborts_and_reports(
    tmp_path: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A missing registry file aborts with OnboardingError and no MR."""
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(OnboardingError, match="not found"):
        onboard("anyslug", missing, fake_opener, today=_FIXED_TODAY)

    assert fake_opener.calls == []


def test_corrupt_yaml_registry_aborts_and_reports(
    tmp_path: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A registry that is not valid YAML aborts with OnboardingError, no MR."""
    corrupt = tmp_path / "projects.yaml"
    # Unbalanced flow-mapping braces -> a YAML parse error.
    corrupt.write_text("projects: {[}: unbalanced\n", encoding="utf-8")

    with pytest.raises(OnboardingError):
        onboard("anyslug", corrupt, fake_opener, today=_FIXED_TODAY)

    assert fake_opener.calls == []


def test_registry_wrong_root_type_aborts(
    tmp_path: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A registry whose root is not a mapping aborts with OnboardingError."""
    bad_root = tmp_path / "projects.yaml"
    bad_root.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(OnboardingError, match="mapping"):
        onboard("anyslug", bad_root, fake_opener, today=_FIXED_TODAY)

    assert fake_opener.calls == []


def test_registry_entry_missing_required_key_aborts(
    tmp_path: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A registry entry lacking slug/vlan_id aborts with OnboardingError."""
    malformed = tmp_path / "projects.yaml"
    malformed.write_text(
        "projects:\n  - slug: hasnovlan\n", encoding="utf-8"
    )

    with pytest.raises(OnboardingError, match="missing a 'slug' or 'vlan_id'"):
        onboard("anyslug", malformed, fake_opener, today=_FIXED_TODAY)

    assert fake_opener.calls == []


def test_registry_noninteger_vlan_id_aborts(
    tmp_path: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A registry with a non-integer vlan_id aborts before assigning, no MR.

    A corrupt vlan_id would poison the allocation math, so it must abort
    (FD.2) rather than silently mis-allocate.
    """
    bad_vlan = tmp_path / "projects.yaml"
    bad_vlan.write_text(
        "projects:\n"
        "  - slug: broken\n"
        '    vlan_id: "not-an-int"\n'
        '    onboarding_date: "2024-01-01"\n',
        encoding="utf-8",
    )

    with pytest.raises(OnboardingError, match="non-integer vlan_id"):
        onboard("anyslug", bad_vlan, fake_opener, today=_FIXED_TODAY)

    assert fake_opener.calls == []


def test_duplicate_vlan_id_in_registry_still_loads_but_new_alloc_is_unique(
    tmp_path: Path, fake_opener: FakeMergeRequestOpener
) -> None:
    """A registry containing a duplicated vlan_id still allocates above the max.

    The allocator computes ``max(existing) + 1``; even with a duplicated ID in
    the historical record, the new allocation is strictly greater than every
    recorded ID (never reused). This guards the never-reuse invariant against a
    (hypothetically) sloppy historical file.
    """
    dup = tmp_path / "projects.yaml"
    dup.write_text(
        "projects:\n"
        "  - slug: a\n"
        "    vlan_id: 100\n"
        '    onboarding_date: "2024-01-01"\n'
        "  - slug: b\n"
        "    vlan_id: 100\n"
        '    onboarding_date: "2024-01-02"\n',
        encoding="utf-8",
    )

    result = onboard("c", dup, fake_opener, today=_FIXED_TODAY)

    assert result.allocation.vlan_id == 101
    assert result.allocation.vlan_id not in {100}
    assert fake_opener.calls == [result.allocation]


# --- Sanity: the load_registry helper on the real fixture ----------------


def test_load_registry_reads_real_fixture(registry_copy: Path) -> None:
    """load_registry parses the shipped fixture into the expected slugs."""
    registry = load_registry(registry_copy)
    slugs = [entry["slug"] for entry in registry["projects"]]
    assert slugs == ["dronefleet", "mlvideo", "edge-sensors"]
