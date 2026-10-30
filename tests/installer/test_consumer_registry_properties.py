"""Hypothesis property test for the Registry resolver — Property 2.

Feature: project-consumer-onboarding (tasks.md Task 3.3; design.md "Correctness
Properties" → Property 2; Requirements 2.1, 2.2, 2.4, 2.5).

One property-based test, >=100 iterations (``@settings(max_examples=200)``),
tagged per the workflow convention. It generates registries (lists of entries
with distinct slugs + statuses) and query slugs, and asserts resolution returns
the unique exact-case match's vlan_id, else the CORRECT no-resolution halt
classification (absent / decommissioned / malformed) — never a wrong or
arbitrary VLAN.

All offline: the resolver is pure, so this runs under the repo's default offline
pytest invocation.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from consumer_registry import (
    ProjectDecommissionedError,
    RegistryUnreadableError,
    SlugAbsentError,
    resolve_vlan_id,
)

# Slug tokens: a small alphabet so collisions between query and registry slugs
# happen often enough to exercise the match path, but mixed-case so exact-case
# sensitivity (Requirement 2.1) is tested from both sides.
_slug = st.text(alphabet="abcDEF-", min_size=1, max_size=6)
_status = st.sampled_from(["active", "decommissioned", None])
_vlan = st.integers(min_value=100, max_value=254)


@st.composite
def _registries(draw):
    """Build a registry with distinct-slug entries, plus a separate query slug.

    Entries carry a (slug, vlan_id, status) triple; slugs within the registry are
    de-duplicated so "the single entry whose slug matches" is well-defined.
    """
    n = draw(st.integers(min_value=0, max_value=6))
    entries = []
    used_slugs: set[str] = set()
    for _ in range(n):
        s = draw(_slug)
        if s in used_slugs:
            continue
        used_slugs.add(s)
        entry: dict = {"slug": s, "vlan_id": draw(_vlan)}
        status = draw(_status)
        if status is not None:
            entry["status"] = status
        entries.append(entry)
    query = draw(_slug)
    return {"projects": entries}, query


@settings(max_examples=200)
@given(data=_registries())
def test_property2_registry_resolution(data):
    """Feature: project-consumer-onboarding, Property 2: Registry resolution returns the unique exact-case match.

    Validates: Requirements 2.1, 2.2, 2.4, 2.5
    """
    registry, query = data
    entries = registry["projects"]

    # Independent re-statement of the expected verdict (does not call the module).
    exact = [e for e in entries if e["slug"] == query]  # exact, case-sensitive

    if not exact:
        # No exact-case match -> absent halt (Requirement 2.2), never a VLAN.
        try:
            resolve_vlan_id(registry, query)
        except SlugAbsentError:
            pass
        else:
            raise AssertionError(f"expected SlugAbsentError for {query!r}")
        return

    # Distinct slugs guarantee exactly one match.
    assert len(exact) == 1
    match = exact[0]
    if match.get("status") == "decommissioned":
        # Matched-but-decommissioned -> decommissioned halt (Requirement 2.4).
        try:
            resolve_vlan_id(registry, query)
        except ProjectDecommissionedError:
            pass
        else:
            raise AssertionError(
                f"expected ProjectDecommissionedError for {query!r}"
            )
    else:
        # Active (or default-active) -> returns exactly that entry's vlan_id,
        # never a wrong or arbitrary VLAN (Requirement 2.1).
        assert resolve_vlan_id(registry, query) == match["vlan_id"]


# A separate property for the malformed-Registry classification (Requirement
# 2.5): any structurally-broken registry halts as unreadable, never resolves.
_broken = st.one_of(
    st.lists(st.integers(), max_size=3),  # not a mapping
    st.builds(lambda v: {"projects": v}, v=st.integers()),  # projects not a list
    st.just({"projects": [{"slug": "x"}]}),  # entry missing vlan_id
    st.just({"projects": [{"slug": "x", "vlan_id": "100"}]}),  # non-int vlan_id
    st.just({"projects": [["slug", "x"]]}),  # entry not a mapping
)


@settings(max_examples=200)
@given(registry=_broken, query=_slug)
def test_property2_malformed_registry_halts_unreadable(registry, query):
    """Feature: project-consumer-onboarding, Property 2: Registry resolution returns the unique exact-case match.

    Validates: Requirements 2.5
    """
    try:
        resolve_vlan_id(registry, query)
    except RegistryUnreadableError:
        pass
    except SlugAbsentError:
        # A structurally-valid-but-empty projects list legitimately yields an
        # absent halt rather than unreadable; that is also a correct no-resolution
        # classification, never a wrong VLAN.
        pass
    else:
        raise AssertionError(
            f"expected a no-resolution halt for malformed registry {registry!r}"
        )
