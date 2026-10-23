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

from pathlib import Path

import pytest
import yaml

from netfoundation import (
    HostAddressExhaustionError,
    HostIndexCollisionError,
    VlanExhaustionError,
    assert_unique_host_indices,
    assigned_host_index,
    gateway_ip,
    host_ip,
    hostname,
    next_vlan_id,
    parse_hostname,
    peer_host_indices,
    proxmox_tag,
    subnet_cidr,
)
from onboard_project import OnboardingError, validate_slug

#: The committed Host_Index_Authority ledger
#: (``infra/platform-foundation/host-indices.yaml``). Located relative to this
#: test file: tests/ -> scripts/ -> platform-foundation/.
_HOST_INDICES_YAML = (
    Path(__file__).resolve().parent.parent.parent / "host-indices.yaml"
)


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


# =========================================================================
# Feature: fix-vlan-host-index-collision — concrete VLAN-20 host-index map
# =========================================================================
#
# These example tests pin the concrete §6 VLAN-20 map from
# .kiro/specs/fix-vlan-host-index-collision/design.md against the committed
# ledger (infra/platform-foundation/host-indices.yaml) and the pure guard /
# read-views. They are the example-based complement to the Property 1-4
# Hypothesis tests in test_property_derivation.py (task 2).


def _load_ledger():
    """Load the committed host-index ledger as a parsed dict keyed by vlan_id."""
    with _HOST_INDICES_YAML.open() as fh:
        return yaml.safe_load(fh)


def _vlan20_index_by_key(ledger):
    """Build a {(svc_code, component): host_index} map for VLAN 20's entries."""
    return {
        (entry["svc_code"], entry.get("component")): entry["host_index"]
        for entry in ledger[20]
    }


# --- The §6 VLAN-20 map: index -> IP, all distinct -----------------------


def test_vlan20_ledger_file_exists():
    """The committed Host_Index_Authority ledger file is present."""
    assert _HOST_INDICES_YAML.is_file(), f"missing ledger: {_HOST_INDICES_YAML}"


def test_vlan20_concrete_index_to_ip_map():
    """The §6 VLAN-20 map resolves each guest to its documented IP.

    primary 0 -> .10, unsealer 1 -> .11, Traefik 2 -> .12,
    SVC-01/shared-zitadel DB 4 -> .14, ZITADEL 5 -> .15.
    """
    ledger = _load_ledger()
    by_key = _vlan20_index_by_key(ledger)

    expected = {
        ("svc07", None): (0, "10.0.20.10"),        # OpenBao primary
        ("svc07", "unsealer"): (1, "10.0.20.11"),  # OpenBao unsealer
        ("svc09", None): (2, "10.0.20.12"),        # Traefik (MOVED from 0)
        ("svc01", "shared-zitadel"): (4, "10.0.20.14"),  # backing DB
        ("svc06", None): (5, "10.0.20.15"),        # ZITADEL
    }
    for key, (index, ip) in expected.items():
        assert by_key[key] == index, f"{key} expected host_index {index}"
        assert host_ip(20, index) == ip, f"{key} expected {ip}"


def test_vlan20_allocated_indices_all_distinct():
    """Every allocated guest on VLAN 20 holds a distinct host index (no .10 clash)."""
    ledger = _load_ledger()
    # svc07 primary=0, unsealer=1, svc09=2, svc01=4, svc06=5 are all distinct.
    primary = next(e for e in ledger[20] if e["svc_code"] == "svc07" and "component" not in e)
    unsealer = next(e for e in ledger[20] if e.get("component") == "unsealer")
    traefik = next(e for e in ledger[20] if e["svc_code"] == "svc09")
    db = next(e for e in ledger[20] if e.get("component") == "shared-zitadel")
    zitadel = next(e for e in ledger[20] if e["svc_code"] == "svc06")

    indices = [
        primary["host_index"],
        unsealer["host_index"],
        traefik["host_index"],
        db["host_index"],
        zitadel["host_index"],
    ]
    assert indices == [0, 1, 2, 4, 5]
    assert len(set(indices)) == len(indices)


def test_vlan20_real_map_passes_uniqueness_guard():
    """``assert_unique_host_indices`` accepts the real VLAN-20 ledger indices."""
    ledger = _load_ledger()
    all_indices = [entry["host_index"] for entry in ledger[20]]
    # Includes reserved headroom (6,7,8,9,10) — still all distinct.
    assert assert_unique_host_indices(20, all_indices) is None


def test_vlan20_prefix_duplicate_is_rejected():
    """The pre-fix state — two guests at index 0 — is rejected with the index named.

    This is the collision the fix removes: SVC-07 primary and SVC-09 Traefik
    both defaulted to host_index 0 -> 10.0.20.10.
    """
    prefix_indices = [0, 1, 0, 4, 5]  # Traefik back at 0 alongside the primary
    with pytest.raises(HostIndexCollisionError) as excinfo:
        assert_unique_host_indices(20, prefix_indices)
    message = str(excinfo.value)
    assert "20" in message  # names the VLAN
    assert "0" in message   # names the repeated index


# --- Read-views (assigned_host_index / peer_host_indices) for SVC-09 -----


def _vlan20_read_view_ledger():
    """A parsed ledger in the read-view shape (guest_key + host_index).

    Mirrors the real VLAN-20 allocated map, but using the ``guest_key`` field
    the ``assigned_host_index`` / ``peer_host_indices`` read-views consume (the
    thin reader layer derives ``guest_key`` from svc_code+component+instance).
    """
    return {
        20: [
            {"guest_key": "svc07", "host_index": 0},
            {"guest_key": "svc07-unsealer", "host_index": 1},
            {"guest_key": "svc09", "host_index": 2},
            {"guest_key": "svc01-shared-zitadel", "host_index": 4},
            {"guest_key": "svc06", "host_index": 5},
        ]
    }


def test_svc09_assigned_host_index_is_2():
    """SVC-09 Traefik's assigned host index on VLAN 20 is 2 (-> 10.0.20.12)."""
    ledger = _vlan20_read_view_ledger()
    assert assigned_host_index(ledger, 20, "svc09") == 2
    assert host_ip(20, assigned_host_index(ledger, 20, "svc09")) == "10.0.20.12"


def test_svc09_peer_host_indices_exclude_self():
    """SVC-09's peer set is every other VLAN-20 index, with its own (2) excluded."""
    ledger = _vlan20_read_view_ledger()
    peers = peer_host_indices(ledger, 20, "svc09")
    assert peers == [0, 1, 4, 5]
    assert 2 not in peers
