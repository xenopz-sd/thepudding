"""Worked-example and edge/error unit tests for the derivation layer.

These are **example-based** unit tests (NOT property tests — the Hypothesis
property suite lives alongside in the property test modules). They pin the pure
``netfoundation`` formulas to the concrete anchors documented in
``NET-00-vlan-ip-addressing-plan.md``'s worked example, plus the edge/error
cases enumerated in the feature design's Testing Strategy §1
("Example unit tests" + "Edge/error unit tests").

Their job is to guard against a formula that is internally self-consistent but
wrong: the property tests prove universal shape, these tests nail the formulas
to the specific values a human can check against NET-00 by inspection.

Anchors pinned here (design Testing Strategy §1, task 5.13):
- VLAN 102 -> ``10.0.102.0/24``; gateway ``10.0.102.1``.
- first host ``.10`` -> hostname ``svc01-102-01``; second host ``.11`` ->
  ``svc02-102-01``.
- shared registry ``svc14-20-01`` at ``10.0.20.10``.

Edge/error cases pinned here:
- empty registry -> next_vlan_id == 100.
- registry with a gap (e.g. [100, 102]) -> 103.
- max at 254 -> VlanExhaustionError.
- host index at the ``.254`` boundary -> ok; beyond -> HostAddressExhaustionError.
- whitespace / over-length slug rejection via ``validate_slug``.

Requirements: 1.5, 2.1, 2.3, 3.1, 9.1, 9.2.

Run (from repo root)::

    PYTHONPATH=infra/platform-foundation/scripts \
        ~/venv/devinfra/bin/pytest \
        infra/platform-foundation/scripts/tests/test_derivation_examples.py
"""

from __future__ import annotations

import pytest

from netfoundation import (
    HostAddressExhaustionError,
    VlanExhaustionError,
    gateway_ip,
    host_ip,
    hostname,
    next_vlan_id,
    parse_hostname,
    proxmox_tag,
    subnet_cidr,
)
from onboard_project import OnboardingError, validate_slug


# --- NET-00 worked-example anchors (Requirements 2.1, 2.3, 3.1) ----------


def test_vlan_102_subnet_cidr():
    """VLAN 102 -> ``10.0.102.0/24`` (NET-00 worked example, Req 2.1)."""
    assert subnet_cidr(102) == "10.0.102.0/24"


def test_vlan_102_gateway():
    """VLAN 102 gateway is ``10.0.102.1`` (NET-00 worked example, Req 2.1)."""
    assert gateway_ip(102) == "10.0.102.1"


def test_vlan_102_first_host_is_dot_10():
    """First host (index 0) on VLAN 102 is ``.10`` (Req 2.2, 2.3)."""
    assert host_ip(102, 0) == "10.0.102.10"


def test_vlan_102_second_host_is_dot_11():
    """Second host (index 1) on VLAN 102 is ``.11`` (Req 2.3)."""
    assert host_ip(102, 1) == "10.0.102.11"


def test_first_host_hostname_svc01_102_01():
    """First host on VLAN 102 -> hostname ``svc01-102-01`` (NET-00 §4, Req 3.1)."""
    assert hostname("svc01", 102, 1) == "svc01-102-01"


def test_second_host_hostname_svc02_102_01():
    """Second host (svc02) on VLAN 102 -> ``svc02-102-01`` (NET-00 §4, Req 3.1)."""
    assert hostname("svc02", 102, 1) == "svc02-102-01"


def test_worked_example_hostnames_round_trip():
    """The worked-example hostnames parse back to their components (Req 3.1)."""
    assert parse_hostname("svc01-102-01") == {
        "svc_code": "svc01",
        "component": None,
        "vlan_id": 102,
        "instance": 1,
    }
    assert parse_hostname("svc02-102-01") == {
        "svc_code": "svc02",
        "component": None,
        "vlan_id": 102,
        "instance": 1,
    }


def test_shared_registry_svc14_20_01_hostname():
    """Shared registry host on VLAN 20 -> hostname ``svc14-20-01`` (NET-00 §4)."""
    assert hostname("svc14", 20, 1) == "svc14-20-01"


def test_shared_registry_svc14_first_host_at_10_0_20_10():
    """Shared registry first host is ``10.0.20.10`` (NET-00 worked example)."""
    assert subnet_cidr(20) == "10.0.20.0/24"
    assert host_ip(20, 0) == "10.0.20.10"


# --- Edge/error: VLAN-ID allocation (Requirements 1.5, 9.1, 9.2) ---------


def test_empty_registry_allocates_100():
    """An empty registry allocates the first per-project VLAN 100 (Req 1.5, 9.1)."""
    assert next_vlan_id([]) == 100


def test_registry_with_gap_allocates_above_max():
    """A non-contiguous registry [100, 102] still allocates ``max + 1`` = 103.

    IDs are allocated strictly above the highest recorded ID; the gap at 101
    (e.g. a decommissioned project) is never backfilled/reused (Req 1.5, 1.6).
    """
    assert next_vlan_id([100, 102]) == 103


def test_registry_at_max_raises_vlan_exhaustion():
    """Highest recorded ID at 254 -> ``VlanExhaustionError`` (Req 9.2)."""
    with pytest.raises(VlanExhaustionError):
        next_vlan_id([100, 200, 254])


# --- Edge/error: host-IP boundary (Requirements 2.3, 2.6) ----------------


def test_host_index_at_254_boundary_ok():
    """Index 244 maps to the last usable octet ``.254`` (boundary is inclusive)."""
    assert host_ip(102, 244) == "10.0.102.254"


def test_host_index_beyond_254_raises_exhaustion():
    """Index 245 would push past ``.254`` -> ``HostAddressExhaustionError`` (Req 2.6)."""
    with pytest.raises(HostAddressExhaustionError):
        host_ip(102, 245)


# --- Edge/error: slug rejection (Requirement 3.1 token validation) -------


def test_validate_slug_rejects_whitespace():
    """A slug containing whitespace is rejected (PF §4)."""
    with pytest.raises(OnboardingError):
        validate_slug("bad slug")


def test_validate_slug_rejects_over_length():
    """A slug longer than 20 characters is rejected (PF §4)."""
    with pytest.raises(OnboardingError):
        validate_slug("a" * 21)


def test_validate_slug_rejects_empty():
    """An empty slug is rejected (PF §4)."""
    with pytest.raises(OnboardingError):
        validate_slug("")


def test_validate_slug_accepts_well_formed():
    """A well-formed slug at the length boundary is accepted unchanged (PF §4)."""
    assert validate_slug("edge-sensors") == "edge-sensors"
    assert validate_slug("a" * 20) == "a" * 20


# --- Tag/hostname distinctness cross-check (Requirement 3.4) -------------


def test_tag_is_distinct_from_hostname():
    """The Proxmox tag ``proj-<slug>-<service>`` is not the hostname (Req 3.4)."""
    tag = proxmox_tag("edge-sensors", "registry")
    name = hostname("svc14", 20, 1)
    assert tag == "proj-edge-sensors-registry"
    assert tag != name
