"""Hypothesis property suite for the pure derivation layer.

Feature: proxmox-network-foundation

This module implements Properties 1-10 from the feature ``design.md``
("Correctness Properties" section) over the pure derivation layer
(``netfoundation.derivation``) and the onboarding allocator
(``onboard_project.allocate``).

Rules honored (per tasks.md 5.3-5.12 and the testing-strategy steering):
- Every property is exactly ONE Hypothesis property-based test.
- Every test runs a MINIMUM of 100 iterations (``settings(max_examples=100)``).
- Generators are Hypothesis strategies, never hand-rolled loops.
- Each test carries the exact design property text in its docstring, tagged
  ``Feature: proxmox-network-foundation, Property N: {property_text}``.

The derivation layer is pure and side-effect-free, so these properties assert
universal characteristics (allocation correctness, addressing-formula
correctness, hostname round-trip + uniqueness, tag/hostname distinctness,
input-guard rejection, onboarding abort, determinism, and exhaustion) rather
than single worked examples (those live in the example/edge unit tests, task
5.13).
"""

from __future__ import annotations

import ipaddress

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from netfoundation.derivation import (
    FIRST_HOST_OFFSET,
    MANAGEMENT_VLAN_ID,
    MAX_HOST_OCTET,
    MAX_PROJECT_VLAN_ID,
    MIN_PROJECT_VLAN_ID,
    HostAddressExhaustionError,
    HostIndexCollisionError,
    HostnameCollisionError,
    ManagementVlanPlacementError,
    OperatorIpLiteralError,
    VlanExhaustionError,
    assert_unique_host_indices,
    assert_unique_hostnames,
    assigned_host_index,
    gateway_ip,
    host_ip,
    hostname,
    next_vlan_id,
    parse_hostname,
    peer_host_indices,
    proxmox_tag,
    reject_ip_literal,
    reject_management_vlan,
    subnet_cidr,
)
from onboard_project import Allocation, OnboardingError, allocate

# --- Minimum-iteration setting shared by every property ------------------

# Every property test runs at least 100 examples per tasks.md 5.3-5.12.
PROPERTY_SETTINGS = settings(max_examples=100)


# --- Reusable Hypothesis strategies (not hand-rolled generators) ---------

# A single valid VLAN ID in the per-project range 100-254 inclusive.
vlan_ids = st.integers(min_value=MIN_PROJECT_VLAN_ID, max_value=MAX_PROJECT_VLAN_ID)

# A set of previously-recorded VLAN IDs. Includes non-contiguous / gappy /
# decommissioned entries: any subset of the full valid range plus, optionally,
# below-range tier IDs (10, 20, 30-99) that must never affect the >=100 floor.
recorded_vlan_id_sets = st.sets(
    st.integers(min_value=MIN_PROJECT_VLAN_ID, max_value=MAX_PROJECT_VLAN_ID),
    min_size=0,
    max_size=40,
)

# A svc_code: 'svc' followed by 2+ digits, lowercase, no hyphen (Req 3.1/3.5).
svc_codes = st.from_regex(r"\Asvc[0-9]{2,3}\Z", fullmatch=True)

# A token: lowercase alphanumeric segments joined by single hyphens, used for
# component / slug / service fields.
tokens = st.from_regex(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z", fullmatch=True).filter(
    lambda s: len(s) <= 20
)

# A replica instance counter in 1-99 (rendered zero-padded, Req 3.1/3.3).
instances = st.integers(min_value=1, max_value=99)


# --- Property 1: Allocation correctness ----------------------------------


@PROPERTY_SETTINGS
@given(existing=recorded_vlan_id_sets)
def test_property_1_allocation_correctness(existing):
    """Feature: proxmox-network-foundation, Property 1: Allocation correctness

    For any recorded set of existing VLAN IDs (including non-contiguous and
    decommissioned entries), the next allocated VLAN ID SHALL be
    ``max(existing, default=99) + 1``, SHALL be strictly greater than every
    recorded ID, SHALL be at least 100, and SHALL NOT equal any recorded ID.

    Validates: Requirements 1.3, 1.4, 1.5, 1.6, 9.1
    """
    # Only exercise non-exhausted registries here; exhaustion is Property 9.
    assume(not existing or max(existing) < MAX_PROJECT_VLAN_ID)

    result = next_vlan_id(existing)

    assert result == (max(existing) + 1 if existing else MIN_PROJECT_VLAN_ID)
    assert result >= MIN_PROJECT_VLAN_ID
    assert all(result > recorded for recorded in existing)
    assert result not in existing


# --- Property 2: Addressing formula correctness --------------------------


@PROPERTY_SETTINGS
@given(
    vlan_id=vlan_ids,
    index_a=st.integers(min_value=0, max_value=MAX_HOST_OCTET - FIRST_HOST_OFFSET),
    index_b=st.integers(min_value=0, max_value=MAX_HOST_OCTET - FIRST_HOST_OFFSET),
)
def test_property_2_addressing_formula_correctness(vlan_id, index_a, index_b):
    """Feature: proxmox-network-foundation, Property 2: Addressing formula correctness

    For any VLAN ID in 100-254 and any zero-based host index, the subnet CIDR
    SHALL be ``10.0.<vlan_id>.0/24`` (its third octet recovering the VLAN ID by
    inspection), the gateway SHALL be ``10.0.<vlan_id>.1``, each computed host
    IP SHALL equal ``cidrhost("10.0.<vlan_id>.0/24", 10 + index)`` with a last
    octet of at least 10, and distinct indices SHALL yield distinct host IPs.

    Validates: Requirements 2.1, 2.2, 2.3, 9.1
    """
    cidr = subnet_cidr(vlan_id)
    assert cidr == f"10.0.{vlan_id}.0/24"
    # Third octet recovers the VLAN ID by inspection.
    assert int(cidr.split("/")[0].split(".")[2]) == vlan_id

    assert gateway_ip(vlan_id) == f"10.0.{vlan_id}.1"

    ip_a = host_ip(vlan_id, index_a)
    # Matches cidrhost("10.0.<vlan_id>.0/24", 10 + index).
    expected = str(ipaddress.ip_network(cidr)[FIRST_HOST_OFFSET + index_a])
    assert ip_a == expected
    # Last octet is at least 10 (.2-.9 left unallocated).
    assert int(ip_a.split(".")[3]) >= FIRST_HOST_OFFSET

    # Distinct indices yield distinct host IPs.
    ip_b = host_ip(vlan_id, index_b)
    if index_a != index_b:
        assert ip_a != ip_b
    else:
        assert ip_a == ip_b


# --- Property 3: Hostname round-trip and within-VLAN uniqueness ----------


@PROPERTY_SETTINGS
@given(
    svc_code=svc_codes,
    component=st.none() | tokens,
    vlan_id=st.integers(min_value=0, max_value=254),
    instance=instances,
)
def test_property_3_hostname_roundtrip_and_uniqueness(
    svc_code, component, vlan_id, instance
):
    """Feature: proxmox-network-foundation, Property 3: Hostname construction round-trip and within-VLAN uniqueness

    For any valid svc_code, optional component, vlan_id, and instance, the
    constructed hostname SHALL match ``<svc-code>[-<component>]-<vlan_id>-<instance>``
    with a zero-padded two-digit instance starting at ``01``, its components
    SHALL be recoverable by parsing (round-trip), and for any set of hosts
    within a single VLAN, all constructed hostnames SHALL be distinct — a
    colliding set SHALL be rejected rather than produce a duplicate.

    Validates: Requirements 3.1, 3.2, 3.3, 3.6
    """
    name = hostname(svc_code, vlan_id, instance, component=component)

    # Matches the documented shape with a zero-padded two-digit instance.
    if component is None:
        assert name == f"{svc_code}-{vlan_id}-{instance:02d}"
    else:
        assert name == f"{svc_code}-{component}-{vlan_id}-{instance:02d}"
    assert name.rsplit("-", 1)[1] == f"{instance:02d}"

    # Round-trip: parsing recovers every component.
    parsed = parse_hostname(name)
    assert parsed == {
        "svc_code": svc_code,
        "component": component,
        "vlan_id": vlan_id,
        "instance": instance,
    }

    # A distinct set within a VLAN is accepted; a colliding set is rejected.
    # Pick a sibling instance that is different but still in the valid 1-99
    # range (avoid 100 when instance == 99).
    sibling_instance = instance - 1 if instance == 99 else instance + 1
    sibling = hostname(svc_code, vlan_id, sibling_instance, component=component)
    assert_unique_hostnames([name, sibling])  # distinct -> no error
    with pytest.raises(HostnameCollisionError):
        assert_unique_hostnames([name, name])


# --- Property 4: Tag and hostname are distinct conventions ---------------


@PROPERTY_SETTINGS
@given(
    slug=tokens,
    service=tokens,
    svc_code=svc_codes,
    vlan_id=st.integers(min_value=0, max_value=254),
    instance=instances,
)
def test_property_4_tag_and_hostname_distinct(
    slug, service, svc_code, vlan_id, instance
):
    """Feature: proxmox-network-foundation, Property 4: Tag and hostname are distinct conventions

    For any slug and service, the Proxmox ``tags`` value SHALL equal
    ``proj-<slug>-<service>`` and SHALL be neither equal to nor derived from
    the host's hostname.

    Validates: Requirements 3.4
    """
    tag = proxmox_tag(slug, service)
    assert tag == f"proj-{slug}-{service}"

    name = hostname(svc_code, vlan_id, instance)
    # The tag is neither equal to the hostname...
    assert tag != name
    # ...nor a substring/superstring relationship that would make one derived
    # from the other: the tag is keyed on slug+service, the hostname on the
    # stable svc-code + VLAN ID.
    assert tag not in name
    assert name not in tag
    assert tag.startswith("proj-")
    assert not name.startswith("proj-")


# --- Property 5: Management-VLAN placement rejection ---------------------


@PROPERTY_SETTINGS
@given(
    vlan_id=st.one_of(
        st.just(MANAGEMENT_VLAN_ID),
        st.just(20),  # shared-services VLAN — a valid placement
        vlan_ids,  # per-project range — valid placements
    )
)
def test_property_5_management_vlan_rejection(vlan_id):
    """Feature: proxmox-network-foundation, Property 5: Management-VLAN placement rejection

    For any provisioning request whose target VLAN ID is 10, the request SHALL
    be rejected with a management-VLAN placement error and no interface SHALL
    be created; for any request whose VLAN ID is a valid project or shared ID,
    placement SHALL be accepted.

    Validates: Requirements 1.7
    """
    if vlan_id == MANAGEMENT_VLAN_ID:
        with pytest.raises(ManagementVlanPlacementError):
            reject_management_vlan(vlan_id)
    else:
        # Accepted and returned unchanged.
        assert reject_management_vlan(vlan_id) == vlan_id


# --- Property 6: Operator IP literals are rejected -----------------------


@PROPERTY_SETTINGS
@given(
    octets=st.lists(
        st.integers(min_value=0, max_value=999), min_size=4, max_size=4
    ),
    lead_ws=st.sampled_from(["", " ", "  "]),
    trail_ws=st.sampled_from(["", " ", "  "]),
)
def test_property_6_operator_ip_literal_rejected(octets, lead_ws, trail_ws):
    """Feature: proxmox-network-foundation, Property 6: Operator IP literals are rejected

    For any operator-supplied IPv4 literal offered as a host address, the
    compute module SHALL reject it and derive the address solely from the
    ``cidrhost`` formula instead.

    Validates: Requirements 2.4
    """
    literal = f"{lead_ws}{'.'.join(str(o) for o in octets)}{trail_ws}"
    with pytest.raises(OperatorIpLiteralError):
        reject_ip_literal(literal)


# --- Property 7: Duplicate-slug onboarding abort -------------------------


@st.composite
def _registry_and_slug(draw):
    """Build a registry mapping plus a candidate slug.

    Half the time the candidate slug is one already recorded (duplicate case);
    otherwise it is guaranteed absent (new case). The recorded slugs and IDs
    are generated by Hypothesis, not hand-rolled.
    """
    slugs = draw(st.lists(tokens, min_size=0, max_size=8, unique=True))
    # Assign each recorded slug a distinct in-range VLAN ID. Kept strictly
    # below the ceiling so the "absent slug proceeds to allocation" branch has
    # a free ID to hand out — VLAN-ID exhaustion is Property 9's concern, not
    # this property's.
    ids = draw(
        st.lists(
            st.integers(
                min_value=MIN_PROJECT_VLAN_ID, max_value=MAX_PROJECT_VLAN_ID - 1
            ),
            min_size=len(slugs),
            max_size=len(slugs),
            unique=True,
        )
    )
    registry = {
        "projects": [
            {"slug": s, "vlan_id": v, "onboarding_date": "2024-01-01"}
            for s, v in zip(slugs, ids)
        ]
    }
    if slugs and draw(st.booleans()):
        candidate = draw(st.sampled_from(slugs))
        is_duplicate = True
    else:
        candidate = draw(tokens.filter(lambda s: s not in slugs))
        is_duplicate = False
    return registry, candidate, is_duplicate


@PROPERTY_SETTINGS
@given(data=_registry_and_slug())
def test_property_7_duplicate_slug_onboarding_abort(data):
    """Feature: proxmox-network-foundation, Property 7: Duplicate-slug onboarding abort

    For any registry state and any slug already present in it, the onboarding
    computation SHALL abort and report the duplicate without allocating a new
    VLAN ID; for any slug absent from the registry, it SHALL proceed to
    allocation.

    Validates: Requirements 5.3
    """
    registry, candidate, is_duplicate = data
    if is_duplicate:
        with pytest.raises(OnboardingError):
            allocate(registry, candidate)
    else:
        result = allocate(registry, candidate)
        assert isinstance(result, Allocation)
        assert result.slug == candidate
        recorded_ids = [p["vlan_id"] for p in registry["projects"]]
        # Proceeded to allocation with a fresh, never-recorded ID.
        assert result.vlan_id not in recorded_ids
        assert result.vlan_id >= MIN_PROJECT_VLAN_ID


# --- Property 8: Determinism of the derivation layer ---------------------


@PROPERTY_SETTINGS
@given(
    existing=recorded_vlan_id_sets,
    vlan_id=vlan_ids,
    index=st.integers(min_value=0, max_value=MAX_HOST_OCTET - FIRST_HOST_OFFSET),
    svc_code=svc_codes,
    component=st.none() | tokens,
    instance=instances,
    slug=tokens,
    service=tokens,
)
def test_property_8_determinism(
    existing, vlan_id, index, svc_code, component, instance, slug, service
):
    """Feature: proxmox-network-foundation, Property 8: Determinism of the derivation layer

    For any fixed inputs (registry state, slug, VLAN ID, host index, hostname
    fields), computing the VLAN ID, subnet CIDR, gateway, host IP, hostname,
    and tag twice SHALL produce identical results each time.

    Validates: Requirements 8.2
    """
    assume(not existing or max(existing) < MAX_PROJECT_VLAN_ID)

    first = (
        next_vlan_id(existing),
        subnet_cidr(vlan_id),
        gateway_ip(vlan_id),
        host_ip(vlan_id, index),
        hostname(svc_code, vlan_id, instance, component=component),
        proxmox_tag(slug, service),
    )
    second = (
        next_vlan_id(existing),
        subnet_cidr(vlan_id),
        gateway_ip(vlan_id),
        host_ip(vlan_id, index),
        hostname(svc_code, vlan_id, instance, component=component),
        proxmox_tag(slug, service),
    )
    assert first == second


# --- Property 9: VLAN-ID exhaustion is surfaced, never wrapped or reused --


@PROPERTY_SETTINGS
@given(
    lower_ids=st.sets(
        st.integers(min_value=MIN_PROJECT_VLAN_ID, max_value=MAX_PROJECT_VLAN_ID - 1),
        min_size=0,
        max_size=20,
    )
)
def test_property_9_vlan_id_exhaustion(lower_ids):
    """Feature: proxmox-network-foundation, Property 9: VLAN-ID exhaustion is surfaced, never wrapped or reused

    For any recorded ID set whose maximum is 254, the next allocation SHALL
    surface an exhaustion error and SHALL NOT wrap to, reuse, or emit an ID
    outside 100-254.

    Validates: Requirements 9.2
    """
    # Force the maximum recorded ID to be 254 (exhausted).
    existing = set(lower_ids) | {MAX_PROJECT_VLAN_ID}
    with pytest.raises(VlanExhaustionError):
        next_vlan_id(existing)


# --- Property 10: Host-IP exhaustion is surfaced, never wrapped ----------


@PROPERTY_SETTINGS
@given(
    vlan_id=vlan_ids,
    # Any index whose 10+index pushes the last octet past 254.
    index=st.integers(
        min_value=MAX_HOST_OCTET - FIRST_HOST_OFFSET + 1,
        max_value=10_000,
    ),
)
def test_property_10_host_ip_exhaustion(vlan_id, index):
    """Feature: proxmox-network-foundation, Property 10: Host-IP exhaustion is surfaced, never wrapped

    For any host index such that ``10 + index > 254``, address computation
    SHALL surface an exhaustion error and SHALL NOT produce a wrapped,
    duplicate, or out-of-range (``> .254``) address.

    Validates: Requirements 2.6
    """
    assert FIRST_HOST_OFFSET + index > MAX_HOST_OCTET  # precondition of the property
    with pytest.raises(HostAddressExhaustionError):
        host_ip(vlan_id, index)


# =========================================================================
# Feature: fix-vlan-host-index-collision — Correctness Properties 1-4
# =========================================================================
#
# These four property tests cover the pure host-index uniqueness guard
# (``assert_unique_host_indices`` + ``HostIndexCollisionError``) and the ledger
# read-views (``assigned_host_index`` / ``peer_host_indices``) added to
# ``derivation.py`` for the fix-vlan-host-index-collision spec. They sit beside
# the proxmox-network-foundation properties above, reuse the same ``vlan_ids``
# strategy and ``PROPERTY_SETTINGS`` (>=100 examples each), and are pure/offline
# — no file, network, or shared state. Each is tagged with its design property
# per tasks.md task 2.


# --- Reusable strategies for the host-index properties -------------------

# A single host index. Zero-based; kept in a range that never trips the
# .254 host-address exhaustion guard (that is proxmox-network-foundation's
# Property 10, not a uniqueness concern here).
host_indices = st.integers(min_value=0, max_value=MAX_HOST_OCTET - FIRST_HOST_OFFSET)

# A SET of distinct host indices on one VLAN (what a valid ledger holds).
distinct_index_sets = st.lists(host_indices, min_size=0, max_size=12, unique=True)


@st.composite
def _ledger_vlan_and_target(draw):
    """Build a parsed single-VLAN ledger plus a target guest key within it.

    Returns ``(vlan_id, ledger, target_key, index_by_key)`` where ``ledger`` is
    the ``{vlan_id: [entries]}`` shape the read-views consume: each entry a dict
    with a stable ``"guest_key"`` and an integer ``"host_index"``. Indices are
    unique within the VLAN (a valid ledger). The recorded keys/indices are
    Hypothesis-generated, not hand-rolled.
    """
    vlan_id = draw(vlan_ids)
    indices = draw(st.lists(host_indices, min_size=1, max_size=10, unique=True))
    # A distinct stable key per entry: g0, g1, ... (keys need only be unique).
    entries = [
        {"guest_key": f"g{i}", "host_index": idx} for i, idx in enumerate(indices)
    ]
    ledger = {vlan_id: entries}
    index_by_key = {e["guest_key"]: e["host_index"] for e in entries}
    target_key = draw(st.sampled_from([e["guest_key"] for e in entries]))
    return vlan_id, ledger, target_key, index_by_key


# --- Property 1: Distinct ledger indices accepted; duplicates rejected ---


@PROPERTY_SETTINGS
@given(distinct=distinct_index_sets, vlan_id=vlan_ids, data=st.data())
def test_fix_collision_property_1_distinct_accepted_duplicates_rejected(
    distinct, vlan_id, data
):
    # Feature: fix-vlan-host-index-collision, Property 1: Distinct ledger indices accepted; duplicates rejected
    """Feature: fix-vlan-host-index-collision, Property 1: Distinct ledger indices accepted; duplicates rejected

    For any VLAN and any multiset of host indices drawn for it,
    ``assert_unique_host_indices`` returns normally iff every index is distinct;
    if any index repeats it raises ``HostIndexCollisionError`` naming the VLAN
    and the repeated index, before any address is emitted.

    Validates: Requirements 1.1, 1.2
    """
    # A fully-distinct set is always accepted (returns None).
    assert assert_unique_host_indices(vlan_id, distinct) is None

    # Seed a duplicate by repeating one of the existing indices, then shuffle so
    # the repeat is not trivially adjacent. Only possible when the set is
    # non-empty.
    if distinct:
        repeated = data.draw(st.sampled_from(distinct))
        colliding = data.draw(st.permutations(list(distinct) + [repeated]))
        with pytest.raises(HostIndexCollisionError) as excinfo:
            assert_unique_host_indices(vlan_id, colliding)
        message = str(excinfo.value)
        # The error names the VLAN and the repeated index.
        assert str(vlan_id) in message
        assert str(repeated) in message


# --- Property 2: Peer-set view excludes self and equals the rest ---------


@PROPERTY_SETTINGS
@given(payload=_ledger_vlan_and_target())
def test_fix_collision_property_2_peer_set_excludes_self(payload):
    # Feature: fix-vlan-host-index-collision, Property 2: Peer-set view excludes self and equals the rest of the VLAN
    """Feature: fix-vlan-host-index-collision, Property 2: Peer-set view excludes self and equals the rest of the VLAN

    For any parsed ledger and any guest in a VLAN, the derived
    ``peer_host_indices`` equals the set of all host indices on that VLAN minus
    the guest's own index — so a guest is never its own peer.

    Validates: Requirement 1.2
    """
    vlan_id, ledger, target_key, index_by_key = payload

    own = assigned_host_index(ledger, vlan_id, target_key)
    peers = peer_host_indices(ledger, vlan_id, target_key)

    all_indices = set(index_by_key.values())
    expected_peers = sorted(all_indices - {own})

    # Equals the rest of the VLAN, with self excluded.
    assert peers == expected_peers
    assert own not in peers
    # Since the ledger has distinct indices, the full set is peers + self.
    assert set(peers) | {own} == all_indices
    # And the precondition !contains(peer, self) can only fire on a real
    # cross-guest collision, which a valid ledger never has.
    assert own not in set(peers)


# --- Property 3: Collision detection is order-independent/deterministic ---


@PROPERTY_SETTINGS
@given(indices=st.lists(host_indices, min_size=0, max_size=12), vlan_id=vlan_ids, data=st.data())
def test_fix_collision_property_3_order_independent_deterministic(
    indices, vlan_id, data
):
    # Feature: fix-vlan-host-index-collision, Property 3: Collision detection is order-independent and deterministic
    """Feature: fix-vlan-host-index-collision, Property 3: Collision detection is order-independent and deterministic

    For any multiset of host indices and any permutation of it,
    ``assert_unique_host_indices`` reaches the same accept/reject verdict (and
    the same reported colliding index on reject) — the verdict depends only on
    the set's contents, not on iteration order or call count.

    Validates: Requirements 1.1, 1.2
    """

    def verdict(seq):
        """Return ('accept', None) or ('reject', sorted-colliding-indices)."""
        try:
            assert_unique_host_indices(vlan_id, seq)
            return ("accept", None)
        except HostIndexCollisionError as exc:
            # Recover the set of repeated indices independently of the message
            # so the comparison is on the reported collision, order-free.
            seen, dups = set(), set()
            for i in seq:
                if i in seen:
                    dups.add(i)
                seen.add(i)
            return ("reject", tuple(sorted(dups)), str(exc))

    perm_a = data.draw(st.permutations(indices))
    perm_b = data.draw(st.permutations(indices))

    v_a = verdict(perm_a)
    v_b = verdict(perm_b)

    # Same accept/reject verdict and same reported colliding index set.
    assert v_a == v_b
    # Determinism: calling twice on the same permutation is identical too.
    assert verdict(perm_a) == v_a


# --- Property 4: The address formula is preserved ------------------------


@PROPERTY_SETTINGS
@given(vlan_id=vlan_ids, indices=st.lists(host_indices, min_size=1, max_size=12, unique=True))
def test_fix_collision_property_4_address_formula_preserved(vlan_id, indices):
    # Feature: fix-vlan-host-index-collision, Property 4: The address formula is preserved
    """Feature: fix-vlan-host-index-collision, Property 4: The address formula is preserved

    For any vlan_id and any non-colliding host index, the emitted address still
    equals ``cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)`` exactly as
    before — the uniqueness guard changes which indices are allowed, never how
    an allowed index maps to an address.

    Validates: Requirements 1.1, 3.1
    """
    # The guard accepts this non-colliding (unique) set...
    assert assert_unique_host_indices(vlan_id, indices) is None

    # ...and every allowed index still maps via the unchanged cidrhost formula.
    cidr = subnet_cidr(vlan_id)
    for index in indices:
        expected = str(ipaddress.ip_network(cidr)[FIRST_HOST_OFFSET + index])
        assert host_ip(vlan_id, index) == expected
