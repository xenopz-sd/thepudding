"""Hypothesis property tests for the consumer derivation helpers — Properties 3 & 4.

Feature: project-consumer-onboarding (tasks.md Tasks 4.3 & 4.4; design.md
"Correctness Properties" → Properties 3 & 4; Requirements 3.4, 3.5, 3.7).

Two property-based tests, each >=100 iterations (``@settings(max_examples=200)``),
tagged per the workflow convention:

- Property 3 — per-project DB host-IP derivation is in-subnet, stable, and
  collision-free within a VLAN.
- Property 4 — per-project DB state name is distinct (from ``<slug>-infra`` and
  every shared-service state) and injective in the slug.

All offline: the module under test is pure and reuses the shared netfoundation
derivation layer.
"""

from __future__ import annotations

import ipaddress

from hypothesis import given, settings
from hypothesis import strategies as st

from consumer_derivation import (
    SHARED_SERVICE_STATE_NAMES,
    consumer_db_host_ip,
    consumer_db_state_name,
    network_slice_state_name,
)

# VLAN IDs 100..254 (project VLAN band) and host indices 0..244 (within the /24
# usable range, since 10 + 244 = 254). These bounds come straight from the design
# Property 3 statement.
_vlan = st.integers(min_value=100, max_value=254)
_index = st.integers(min_value=0, max_value=244)

# Slugs matching the platform slug convention, including single-char and max-len.
_slug = st.from_regex(
    r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", fullmatch=True
).filter(lambda s: len(s) <= 20)


# =============================================================================
# Task 4.3 — Property 3
# Feature: project-consumer-onboarding, Property 3: Per-project DB host-IP
# derivation is in-subnet, stable, and collision-free
# Validates: Requirements 3.4, 3.7
# =============================================================================
@settings(max_examples=200)
@given(vlan_id=_vlan, index_a=_index, index_b=_index)
def test_property3_host_ip_in_subnet_stable_collision_free(vlan_id, index_a, index_b):
    subnet = ipaddress.ip_network(f"10.0.{vlan_id}.0/24")

    ip_a = consumer_db_host_ip(vlan_id, index_a)
    # Formula: equals cidrhost("10.0.<vlan>.0/24", 10 + index).
    assert ip_a == str(subnet[10 + index_a])
    # In-subnet of the /24.
    assert ipaddress.ip_address(ip_a) in subnet
    # Stable: the same (vlan_id, index) always derives the same IP.
    assert consumer_db_host_ip(vlan_id, index_a) == ip_a

    ip_b = consumer_db_host_ip(vlan_id, index_b)
    # Collision-free within a VLAN: distinct indices never derive the same IP.
    if index_a != index_b:
        assert ip_a != ip_b
    else:
        assert ip_a == ip_b


# =============================================================================
# Task 4.4 — Property 4
# Feature: project-consumer-onboarding, Property 4: Per-project DB state name is
# distinct and injective in the slug
# Validates: Requirements 3.5
# =============================================================================
@settings(max_examples=200)
@given(slug_a=_slug, slug_b=_slug)
def test_property4_state_name_distinct_and_injective(slug_a, slug_b):
    db_a = consumer_db_state_name(slug_a)

    # Distinct from the same slug's network-slice (<slug>-infra) state name.
    assert db_a != network_slice_state_name(slug_a)

    # Distinct from every shared-service state name.
    assert db_a not in SHARED_SERVICE_STATE_NAMES

    # Injective in the slug: distinct slugs -> distinct DB state names;
    # equal slugs -> equal names.
    db_b = consumer_db_state_name(slug_b)
    if slug_a != slug_b:
        assert db_a != db_b
    else:
        assert db_a == db_b
