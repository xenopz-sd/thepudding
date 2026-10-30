"""Unit tests for the per-project consumer derivation helpers.

Feature: project-consumer-onboarding (tasks.md Task 4.2; design.md "Correctness
Properties" → Properties 3 & 4; Requirements 3.4, 3.5).

Golden examples for a known slug/vlan, plus a reuse sanity check that the host-IP
helper delegates to the shared ``netfoundation.derivation.host_ip`` symbol (not a
forked copy).

All offline: the module under test is pure; it imports the shared netfoundation
derivation layer (reuse, not reimplementation).
"""

from __future__ import annotations

import pytest

from consumer_derivation import (
    HostAddressExhaustionError,
    consumer_db_host_ip,
    consumer_db_state_name,
    network_slice_state_name,
)


# --- Host-IP golden examples (Requirement 3.4) ---------------------------


def test_dronefleet_vlan100_index0_is_dot_10():
    # dronefleet, VLAN 100, host_index 0 -> 10.0.100.10 (design's worked example).
    assert consumer_db_host_ip(100, 0) == "10.0.100.10"


def test_vlan100_index1_is_dot_11():
    assert consumer_db_host_ip(100, 1) == "10.0.100.11"


def test_vlan101_index0_is_dot_10():
    assert consumer_db_host_ip(101, 0) == "10.0.101.10"


def test_host_ip_reuses_shared_netfoundation_symbol():
    # The wrapper must delegate to the shared symbol, not re-derive. Equality of
    # the function object proves reuse.
    import consumer_derivation
    from netfoundation.derivation import host_ip as shared_host_ip

    assert consumer_derivation.host_ip is shared_host_ip
    assert consumer_db_host_ip(102, 5) == shared_host_ip(102, 5)


def test_host_ip_exhaustion_surfaces_shared_error():
    with pytest.raises(HostAddressExhaustionError):
        consumer_db_host_ip(100, 245)  # 10 + 245 = 255 > 254


# --- State-name golden examples (Requirement 3.5) ------------------------


def test_dronefleet_db_state_name():
    assert consumer_db_state_name("dronefleet") == "dronefleet-db"


def test_db_state_name_differs_from_infra_state_name():
    assert consumer_db_state_name("dronefleet") != network_slice_state_name(
        "dronefleet"
    )
    assert network_slice_state_name("dronefleet") == "dronefleet-infra"


def test_db_state_name_injective_for_distinct_slugs():
    assert consumer_db_state_name("mlvideo") != consumer_db_state_name(
        "edge-sensors"
    )


@pytest.mark.parametrize("bad_slug", ["Dronefleet", "-x", "x-", "a" * 21, ""])
def test_state_name_rejects_malformed_slug(bad_slug):
    with pytest.raises(ValueError):
        consumer_db_state_name(bad_slug)
