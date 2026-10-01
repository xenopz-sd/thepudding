"""Pure derivation layer for VLAN allocation and IP addressing.

Every function here is deterministic and side-effect-free: given identical
inputs it returns identical outputs, reads no files, touches no network, and
mutates no shared state (Requirement 8.2, Property 8 — Determinism).

The formulas implement the binding scheme in
``NET-00-vlan-ip-addressing-plan.md`` and the feature design's Data Models /
Correctness Properties sections:

- VLAN-ID allocation: ``max(existing, default=99) + 1``; per-project range is
  100-254 inclusive (Requirements 1.4, 1.5, 1.6, 9.1).
- Subnet CIDR: ``10.0.<vlan_id>.0/24`` (Requirement 2.1).
- Gateway: ``10.0.<vlan_id>.1`` (Requirement 2.1).
- Host IP: ``cidrhost("10.0.<vlan_id>.0/24", 10 + index)`` (Requirement 2.3),
  where the ``.2``-``.9`` addresses are deliberately left unallocated
  (Requirement 2.2) by starting host offsets at ``10``.

Exhaustion is always surfaced as a distinct, catchable exception rather than a
wrapped, reused, or out-of-range value (Requirements 2.6, 9.2 —
Properties 9 and 10). The custom exception classes let the property tests in
tasks 5.11 / 5.12 assert on the specific failure mode.

The ``ipaddress`` stdlib module is used for host-IP computation because its
``ip_network(...)[n]`` indexing mirrors Terraform's ``cidrhost()`` semantics
directly (see the constitution's derivation-layer language decision).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

# --- Scheme constants (NET-00 §2-§3) -------------------------------------

#: Lowest per-project VLAN ID. IDs below this are management (10), shared (20),
#: or reserved (30-99) tiers and are never handed out by ``next_vlan_id``.
MIN_PROJECT_VLAN_ID = 100

#: Highest VLAN ID expressible under the single-octet ``10.0.<vlan_id>.0/24``
#: formula. The third octet cannot exceed 254 (255 is the broadcast-ish edge of
#: the second-octet range this scheme reserves), so this is also the ceiling
#: the exhaustion guard enforces (Requirement 9.1).
MAX_PROJECT_VLAN_ID = 254

#: Sentinel used so that an empty registry allocates the first project VLAN:
#: ``max([], default=99) + 1 == 100``.
_VLAN_ALLOCATION_FLOOR = 99

#: First host offset within a subnet. Offsets ``2``-``9`` (i.e. ``.2``-``.9``)
#: are left unallocated to guests in every subnet (Requirement 2.2), so host
#: addressing starts at offset ``10`` -> ``.10``.
FIRST_HOST_OFFSET = 10

#: Largest usable last octet for a host address under the ``/24`` formula.
MAX_HOST_OCTET = 254


class VlanExhaustionError(Exception):
    """Raised when the per-project VLAN-ID space (100-254) is exhausted.

    Surfaced by :func:`next_vlan_id` when the highest recorded ID is already
    at :data:`MAX_PROJECT_VLAN_ID`, so the next sequential ID would fall
    outside the range. The scheme never wraps or reuses an ID
    (Requirement 9.2, Property 9).
    """


class HostAddressExhaustionError(Exception):
    """Raised when a host index would push the address past ``.254``.

    Surfaced by :func:`host_ip` when ``FIRST_HOST_OFFSET + index`` exceeds
    :data:`MAX_HOST_OCTET`. The scheme never wraps, duplicates, or emits an
    out-of-range address (Requirement 2.6, Property 10).
    """


def next_vlan_id(existing_ids: Iterable[int]) -> int:
    """Return the next sequential, never-reused VLAN ID.

    The next ID is ``max(existing, default=99) + 1``: strictly greater than
    every recorded ID, at least :data:`MIN_PROJECT_VLAN_ID`, and never equal to
    any recorded ID (Requirements 1.5, 1.6, 9.1; Property 1).

    Args:
        existing_ids: All VLAN IDs ever recorded in the registry, including
            non-contiguous and decommissioned entries. Order does not matter.

    Returns:
        The next VLAN ID to assign, in the range 100-254 inclusive.

    Raises:
        VlanExhaustionError: If the highest recorded ID is already 254, so no
            larger in-range ID exists (Requirement 9.2).
    """
    highest = max(existing_ids, default=_VLAN_ALLOCATION_FLOOR)
    candidate = highest + 1
    if candidate > MAX_PROJECT_VLAN_ID:
        raise VlanExhaustionError(
            "VLAN-ID space exhausted: highest recorded ID is "
            f"{highest}; the next sequential ID {candidate} exceeds the "
            f"maximum {MAX_PROJECT_VLAN_ID}. IDs are never reused or wrapped."
        )
    return candidate


def subnet_cidr(vlan_id: int) -> str:
    """Return the subnet CIDR ``10.0.<vlan_id>.0/24`` for a VLAN (Req 2.1).

    The third octet recovers the VLAN ID by inspection (Property 2).
    """
    return f"10.0.{vlan_id}.0/24"


def gateway_ip(vlan_id: int) -> str:
    """Return the gateway address ``10.0.<vlan_id>.1`` for a VLAN (Req 2.1)."""
    return f"10.0.{vlan_id}.1"


def host_ip(vlan_id: int, index: int) -> str:
    """Return the static host IP for the zero-based ``index`` within a VLAN.

    Equivalent to ``cidrhost("10.0.<vlan_id>.0/24", 10 + index)`` — the same
    semantics the Terraform ``proxmox-compute`` module uses at apply time.
    Offset ``10`` maps index ``0`` to ``.10``, leaving ``.2``-``.9``
    unallocated (Requirements 2.2, 2.3; Property 2).

    Args:
        vlan_id: The project's assigned VLAN ID.
        index: Zero-based position of the host within its VLAN's host set.

    Returns:
        The dotted-quad IPv4 address string, e.g. ``"10.0.102.10"``.

    Raises:
        HostAddressExhaustionError: If ``10 + index`` exceeds 254, so the
            address would wrap out of the ``/24`` (Requirement 2.6).
    """
    offset = FIRST_HOST_OFFSET + index
    if offset > MAX_HOST_OCTET:
        raise HostAddressExhaustionError(
            f"Host-address space exhausted for VLAN {vlan_id}: host index "
            f"{index} maps to last octet {offset}, which exceeds the maximum "
            f"{MAX_HOST_OCTET}. Addresses are never wrapped or duplicated."
        )
    network = ipaddress.ip_network(subnet_cidr(vlan_id))
    return str(network[offset])

# --- Hostname / tag construction and input guards (task 5.2) -------------
#
# These extend the pure derivation layer with the hostname and Proxmox-tag
# conventions (NET-00 §4, PF §4) and the three input guards enumerated in the
# design's Error Handling table. Like everything above, they are deterministic
# and side-effect-free: they validate and transform their arguments and touch
# no files, network, or shared state.
#
# The hostname convention (`<svc-code>[-<component>]-<vlan_id>-<instance>`) and
# the tag convention (`proj-<slug>-<service>`) are DELIBERATELY DISTINCT and
# not derivable from one another (Requirement 3.4, Property 4): the hostname is
# machine-facing and VLAN-correlated, keyed to the stable catalog slot number;
# the tag is human-facing and slug-correlated. A technology swap within a
# service slot changes neither.

import re

#: The management VLAN (NET-00 §2). No guest VM/LXC may be placed on it; a
#: request to do so is rejected before any resource is created (Requirement 1.7,
#: Property 5).
MANAGEMENT_VLAN_ID = 10

#: Lowest and highest ``<instance>`` replica counter values. The counter is a
#: zero-padded two-digit number in ``01``-``99`` (Requirement 3.1).
MIN_INSTANCE = 1
MAX_INSTANCE = 99

#: A catalog service code: the stable ``SVC-NN`` slot rendered lowercase with
#: no hyphen, e.g. ``svc01``, ``svc14`` (Requirement 3.1, 3.5).
_SVC_CODE_RE = re.compile(r"^svc\d{2,}$")

#: A hostname component / service / slug token: lowercase alphanumeric with
#: internal hyphens permitted, but no leading/trailing hyphen and no other
#: punctuation. Used for the optional ``<component>`` field and for the tag's
#: ``<slug>`` / ``<service>`` fields.
_TOKEN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: A dotted-quad IPv4 literal, used only to detect and reject operator-supplied
#: addresses (Requirement 2.4). Kept intentionally permissive on octet ranges —
#: any string that *looks like* an IPv4 literal is rejected, whether or not it
#: is strictly valid, so a near-miss like ``10.0.999.1`` is still refused.
_IPV4_LITERAL_RE = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){3}\s*$")


class ManagementVlanPlacementError(Exception):
    """Raised when a guest is requested on the Management_VLAN (VLAN 10).

    Surfaced by :func:`reject_management_vlan` (and, at apply time, the
    ``proxmox-compute`` module precondition). No interface is created
    (Requirement 1.7, Property 5).
    """


class OperatorIpLiteralError(Exception):
    """Raised when an operator supplies an IPv4 literal as a host address.

    Host addresses are derived solely from the ``cidrhost`` formula
    (:func:`host_ip`); an operator-supplied literal is rejected rather than
    honored (Requirement 2.4, Property 6).
    """


class HostnameCollisionError(Exception):
    """Raised when two hosts within a single VLAN resolve to the same hostname.

    Surfaced by :func:`assert_unique_hostnames`. A colliding set is rejected
    rather than silently producing a duplicate (Requirement 3.6, Property 3).
    """


def _validate_instance(instance: int) -> str:
    """Validate and zero-pad a replica counter to two digits (Req 3.1, 3.3)."""
    if not isinstance(instance, int) or isinstance(instance, bool):
        raise ValueError(f"instance must be an int, got {instance!r}")
    if not (MIN_INSTANCE <= instance <= MAX_INSTANCE):
        raise ValueError(
            f"instance {instance} out of range; must be "
            f"{MIN_INSTANCE}-{MAX_INSTANCE} inclusive (rendered as 2 digits)."
        )
    return f"{instance:02d}"


def hostname(
    svc_code: str,
    vlan_id: int,
    instance: int = 1,
    component: str | None = None,
) -> str:
    """Construct a machine-facing hostname (NET-00 §4, Requirements 3.1-3.3).

    Produces ``<svc-code>-<vlan_id>-<instance>``, or
    ``<svc-code>-<component>-<vlan_id>-<instance>`` when ``component`` is given
    (Requirement 3.2). ``<instance>`` is a zero-padded two-digit counter that a
    singleton sets to ``01`` (Requirement 3.3).

    The result is the round-trip inverse of :func:`parse_hostname`.

    Args:
        svc_code: Catalog slot rendered lowercase with no hyphen, e.g.
            ``svc01`` (Requirements 3.1, 3.5).
        vlan_id: The host's numeric VLAN ID.
        instance: Replica counter in 1-99; defaults to 1 -> ``01``.
        component: Optional lowercase component name for a catalog line item
            that deploys more than one distinct service (Requirement 3.2).

    Returns:
        The constructed hostname string.

    Raises:
        ValueError: If ``svc_code``, ``component``, ``vlan_id``, or ``instance``
            is malformed / out of range.
    """
    if not isinstance(svc_code, str) or not _SVC_CODE_RE.match(svc_code):
        raise ValueError(
            f"svc_code {svc_code!r} must match 'svc' + digits (e.g. 'svc01'), "
            "lowercase with no hyphen (Requirement 3.1/3.5)."
        )
    if not isinstance(vlan_id, int) or isinstance(vlan_id, bool) or vlan_id < 0:
        raise ValueError(f"vlan_id must be a non-negative int, got {vlan_id!r}")
    instance_str = _validate_instance(instance)
    if component is not None:
        if not isinstance(component, str) or not _TOKEN_RE.match(component):
            raise ValueError(
                f"component {component!r} must be lowercase alphanumeric with "
                "internal hyphens only (no leading/trailing hyphen)."
            )
        return f"{svc_code}-{component}-{vlan_id}-{instance_str}"
    return f"{svc_code}-{vlan_id}-{instance_str}"


def parse_hostname(name: str) -> dict[str, object]:
    """Recover the components of a hostname built by :func:`hostname`.

    Round-trip inverse of :func:`hostname` (Requirement 3.1, Property 3).
    Parsing is unambiguous because the fields are read from the right: the last
    dash-separated field is the two-digit ``instance``, the next is the numeric
    ``vlan_id``, the first is ``svc_code``, and anything in between (rejoined
    with hyphens) is the optional ``component``. ``svc_code`` never contains a
    hyphen (Requirement 3.1), so this decomposition is well-defined.

    Args:
        name: A hostname produced by :func:`hostname`.

    Returns:
        A dict with keys ``svc_code`` (str), ``component`` (str or ``None``),
        ``vlan_id`` (int), and ``instance`` (int).

    Raises:
        ValueError: If ``name`` does not match the hostname convention.
    """
    if not isinstance(name, str):
        raise ValueError(f"hostname must be a str, got {name!r}")
    parts = name.split("-")
    if len(parts) < 3:
        raise ValueError(
            f"hostname {name!r} does not match "
            "'<svc-code>[-<component>]-<vlan_id>-<instance>'."
        )
    svc_code, *middle, vlan_str, instance_str = parts
    if not _SVC_CODE_RE.match(svc_code):
        raise ValueError(f"hostname {name!r}: leading field {svc_code!r} is not a svc-code.")
    if not (vlan_str.isdigit() and instance_str.isdigit()):
        raise ValueError(
            f"hostname {name!r}: vlan_id / instance fields must be numeric."
        )
    if len(instance_str) != 2:
        raise ValueError(
            f"hostname {name!r}: instance field {instance_str!r} must be two digits."
        )
    component = "-".join(middle) if middle else None
    if component is not None and not _TOKEN_RE.match(component):
        raise ValueError(f"hostname {name!r}: component {component!r} is malformed.")
    vlan_id = int(vlan_str)
    instance = int(instance_str)
    if not (MIN_INSTANCE <= instance <= MAX_INSTANCE):
        raise ValueError(
            f"hostname {name!r}: instance {instance} out of range 1-99."
        )
    return {
        "svc_code": svc_code,
        "component": component,
        "vlan_id": vlan_id,
        "instance": instance,
    }


def proxmox_tag(slug: str, service: str) -> str:
    """Construct the human-facing Proxmox ``tags`` value ``proj-<slug>-<service>``.

    This is a DISTINCT convention from :func:`hostname` and is neither equal to
    nor derived from a hostname (Requirement 3.4, Property 4): it is keyed on
    the project slug and service name, whereas the hostname is keyed on the
    stable catalog slot number and VLAN ID.

    Args:
        slug: The project slug (lowercase-hyphenated, PF §4).
        service: The service name.

    Returns:
        The tag string ``proj-<slug>-<service>``.

    Raises:
        ValueError: If ``slug`` or ``service`` is malformed.
    """
    if not isinstance(slug, str) or not _TOKEN_RE.match(slug):
        raise ValueError(
            f"slug {slug!r} must be lowercase alphanumeric with internal "
            "hyphens only (no leading/trailing hyphen)."
        )
    if not isinstance(service, str) or not _TOKEN_RE.match(service):
        raise ValueError(
            f"service {service!r} must be lowercase alphanumeric with internal "
            "hyphens only (no leading/trailing hyphen)."
        )
    return f"proj-{slug}-{service}"


def reject_management_vlan(vlan_id: int) -> int:
    """Reject a guest placement on the Management_VLAN (Requirement 1.7).

    A management-VLAN placement is refused *before* any resource is created,
    with a distinct exception the property tests can assert on (Property 5).
    Any other VLAN ID is accepted and returned unchanged.

    Args:
        vlan_id: The target VLAN ID for a guest interface.

    Returns:
        ``vlan_id`` unchanged, when it is not the management VLAN.

    Raises:
        ManagementVlanPlacementError: If ``vlan_id`` is the management VLAN (10).
    """
    if vlan_id == MANAGEMENT_VLAN_ID:
        raise ManagementVlanPlacementError(
            f"Management-VLAN placement violation: VLAN {MANAGEMENT_VLAN_ID} "
            "carries Proxmox host/corosync/API traffic only; no guest VM or "
            "LXC interface may be attached to it (NET-00 §2, Requirement 1.7)."
        )
    return vlan_id


def reject_ip_literal(value: object) -> None:
    """Reject an operator-supplied IPv4 literal offered as a host address.

    Host addresses are derived solely from :func:`host_ip` / the ``cidrhost``
    formula; an operator-supplied literal is refused (Requirement 2.4,
    Property 6). Non-IP-literal values pass through without error (this guard
    only rejects address literals, not every free-form string).

    Args:
        value: A candidate host-address value from operator input.

    Raises:
        OperatorIpLiteralError: If ``value`` is an IPv4 dotted-quad literal.
    """
    if isinstance(value, str) and _IPV4_LITERAL_RE.match(value):
        raise OperatorIpLiteralError(
            f"Operator-supplied IP literal {value.strip()!r} is rejected; host "
            "addresses are computed from the cidrhost formula only "
            "(NET-00 §3, Requirement 2.4)."
        )


def assert_unique_hostnames(hostnames: Iterable[str]) -> None:
    """Assert that a set of hosts within a VLAN have distinct hostnames.

    A colliding set is rejected rather than allowed to produce a duplicate
    (Requirement 3.6, Property 3). Intended to be called with all hostnames
    scheduled within a single VLAN.

    Args:
        hostnames: The hostnames to check for within-VLAN uniqueness.

    Raises:
        HostnameCollisionError: If any hostname appears more than once.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in hostnames:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        collided = ", ".join(sorted(duplicates))
        raise HostnameCollisionError(
            f"Hostname collision within a VLAN: {collided} appears more than "
            "once. Each host must have a unique hostname within its VLAN "
            "(Requirement 3.6)."
        )
