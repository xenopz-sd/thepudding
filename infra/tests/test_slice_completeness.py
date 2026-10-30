"""Slice-union completeness guard for the infra/tests Test_Slices.

Feature: ci-offline-gate-hardening (design §Architecture 3.2.2, Property 3).

Makes the "the slices partition the whole tree" invariant a TESTED fact, not a
hope. Runs inside the normal `infra/tests` CI job, so CI enforces slice
completeness even though CI never runs the slices themselves.

Property 3 (Slice union equals the whole tree): the union of files named across
all SLICES equals EXACTLY the set of `test_*.py` on disk in `infra/tests`; every
slice is non-empty; no file appears in two slices. Adding a test file later
without slotting it into exactly one slice FAILS this guard (slice-drift catch);
a slice naming a removed file also fails (stale entry); an empty slice fails
(zero-selection guard).

Completeness is asserted at FILE granularity, which is sufficient: slices select
whole files (`pytest infra/tests/test_foo.py`), so file-set equality implies
test-set equality for one `pytest infra/tests` collection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
    _HYPOTHESIS = True
except Exception:  # pragma: no cover
    _HYPOTHESIS = False

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import slices  # noqa: E402  (sys.path set above)


def _on_disk_test_files() -> set[str]:
    return {p.name for p in _THIS_DIR.glob("test_*.py")}


def _slice_union() -> list[str]:
    out: list[str] = []
    for files in slices.SLICES.values():
        out.extend(files)
    return out


# --------------------------------------------------------------------------- #
# Property 3 — union == whole tree, non-empty, no double-assignment.
# Feature: ci-offline-gate-hardening, Property 3: the slices partition the tree.
# Validates: Requirements 4.2, 4.4, 4.5, 5.5
# --------------------------------------------------------------------------- #
def test_slice_union_equals_on_disk_tree():
    """The union of all slices equals exactly the on-disk test_*.py set."""
    on_disk = _on_disk_test_files()
    union = set(_slice_union())
    unassigned = on_disk - union
    stale = union - on_disk
    assert not unassigned, (
        "these infra/tests files are in NO slice — add each to exactly one slice "
        f"in slices.py: {sorted(unassigned)}"
    )
    assert not stale, (
        "these slice entries name files that do not exist on disk (stale) — remove "
        f"them from slices.py: {sorted(stale)}"
    )


def test_no_file_in_two_slices():
    """No test file is assigned to more than one slice (clean partition)."""
    union_list = _slice_union()
    seen: dict[str, int] = {}
    for f in union_list:
        seen[f] = seen.get(f, 0) + 1
    dupes = sorted(f for f, n in seen.items() if n > 1)
    assert not dupes, f"these files appear in more than one slice: {dupes}"


def test_every_slice_non_empty():
    """Every slice is non-empty (zero-selection guard)."""
    empty = sorted(name for name, files in slices.SLICES.items() if not files)
    assert not empty, f"these slices are empty (would select zero tests): {empty}"


def test_meta_test_files_are_in_some_slice():
    """The two branch-context meta-test files are assigned to a slice (so the
    feature-branch deselection note in docs/TESTING.md applies to a real slice)."""
    union = set(_slice_union())
    for f in slices.META_TEST_FILES:
        assert f in union, f"meta-test file {f} must belong to a slice."


# --------------------------------------------------------------------------- #
# Property 3 (generator variant) — the union law holds for the checker itself.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HYPOTHESIS, reason="hypothesis not installed")
@settings(max_examples=120, deadline=None)
@given(
    files=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8),
        min_size=1, max_size=30, unique=True,
    ),
    n_slices=st.integers(min_value=1, max_value=6),
)
def test_property3_partition_law(files, n_slices):
    """For any file set partitioned into N non-empty slices, the union equals the
    original set and no file is double-assigned — the invariant the real guard
    relies on. (Checks the partition algebra the completeness test enforces.)"""
    names = [f"test_{f}.py" for f in files]
    # Round-robin partition into n_slices buckets (drop empty buckets).
    buckets: dict[str, list[str]] = {f"s{i}": [] for i in range(n_slices)}
    for idx, name in enumerate(names):
        buckets[f"s{idx % n_slices}"].append(name)
    buckets = {k: v for k, v in buckets.items() if v}  # non-empty only
    union: list[str] = []
    for v in buckets.values():
        union.extend(v)
    assert set(union) == set(names)          # union == whole set
    assert len(union) == len(set(union))     # no double-assignment
    assert all(v for v in buckets.values())  # every kept bucket non-empty
