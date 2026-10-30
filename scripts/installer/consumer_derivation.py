#!/usr/bin/env python3
"""Pure derivation helpers for the project-consumer-onboarding feature.

Feature: project-consumer-onboarding (design.md "Correctness Properties" →
Properties 3 & 4; "Data Models" → SVC01_Consumer_Root inputs; tasks.md Task 4.1;
Requirements 3.4, 3.5, 3.7).

Two pure derivations the SVC01_Consumer_Root and the orchestrator need:

1. **Per-project DB host IP** — ``cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)``.
2. **Per-project DB Terraform state name** — ``<slug>-db``, which MUST be distinct
   from the ``<slug>-infra`` network-slice state and from every shared-service
   state, and injective in the slug (Requirement 3.5, Property 4).

SHARED-SYMBOL REUSE (testing-strategy.md "Verification Scope").
----------------------------------------------------------------
The host-IP arithmetic is the SAME formula already implemented, tested, and
depended on by the NET-00 / fix-vlan-host-index-collision derivation layer at
``infra/platform-foundation/scripts/netfoundation/derivation.py``
(:func:`netfoundation.derivation.host_ip`, used by ``onboard_project`` and the
derivation + host-index property suites). Per the shared-symbol caveat this module
**reuses** that symbol via :func:`consumer_db_host_ip` rather than forking a second
copy of the ``cidrhost`` arithmetic — a repo-wide ``grep`` for ``host_ip`` /
``from netfoundation`` confirmed the existing callers, and this module adds only
NEW symbols (a thin wrapper + the state-name helper), changing no existing
signature, so no existing caller is affected.

Because ``netfoundation`` lives under ``infra/platform-foundation/scripts`` (not on
``scripts/installer``'s path), this module adds that scripts dir to ``sys.path`` on
import using the same robust idiom the cross-tree tests use
(``infra/tests/test_host_indices_obs.py``). The import is therefore a genuine reuse
of the shared package, not a re-implementation.

Nothing here reads the environment, filesystem, or network; every function is
deterministic and side-effect-free.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# --- Reuse the shared netfoundation derivation layer ---------------------
#
# netfoundation lives at infra/platform-foundation/scripts/netfoundation. This
# file is scripts/installer/consumer_derivation.py, so the netfoundation parent
# is parents[2] / "infra" / "platform-foundation" / "scripts". Add it to
# sys.path (idempotently) so ``from netfoundation.derivation import host_ip``
# resolves — the same idiom infra/tests/test_host_indices_obs.py uses.
_NETFOUNDATION_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "platform-foundation"
    / "scripts"
)
if str(_NETFOUNDATION_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_NETFOUNDATION_SCRIPTS_DIR))

from netfoundation.derivation import (  # noqa: E402  (import after sys.path setup)
    HostAddressExhaustionError,
    host_ip,
)

__all__ = [
    "HostAddressExhaustionError",
    "SVC01_STATE_SUFFIX",
    "NETWORK_SLICE_STATE_SUFFIX",
    "SHARED_SERVICE_STATE_NAMES",
    "consumer_db_host_ip",
    "consumer_db_state_name",
    "network_slice_state_name",
]

#: The SVC01_Consumer_Root state-name suffix. The DB root's GitLab-managed state
#: name is ``<slug>-db`` (Requirement 3.5).
SVC01_STATE_SUFFIX = "-db"

#: The network-slice (``<slug>-infra``) state-name suffix the DB state must never
#: collide with (Requirement 3.5).
NETWORK_SLICE_STATE_SUFFIX = "-infra"

#: Representative shared-service state names the per-project ``<slug>-db`` state
#: must remain distinct from (the shared-services VLAN-20 roots, which use
#: service-scoped state names, never a ``<slug>-db`` form). Used by the property
#: test to assert disjointness; a ``<slug>-db`` name can only collide with one of
#: these if a project's slug were literally one of these service names minus the
#: suffix, which the distinctness proof below rules out structurally.
SHARED_SERVICE_STATE_NAMES = (
    "platform-foundation",
    "svc-01-sql-database",
    "svc-06-identity-provider",
    "svc-07-secrets-manager",
    "svc-09-reverse-proxy",
    "svc-13-ci-runners",
    "svc-14-container-registry",
    "svc-17-18-observability",
)

#: A project slug: lowercase alphanumeric with internal single hyphens, no
#: leading/trailing hyphen, <=20 chars (PF §4 / Requirement 1.5). Reused here to
#: guard the state-name derivation against a malformed slug producing a malformed
#: state name.
_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")


def consumer_db_host_ip(vlan_id: int, host_index: int) -> str:
    """Return the per-project DB guest's host IP (Requirement 3.4, Property 3).

    Thin REUSE wrapper over :func:`netfoundation.derivation.host_ip`: the IP is
    ``cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)`` — the exact formula the
    Terraform ``proxmox-compute`` module uses at apply time, and the exact shared
    symbol the NET-00 / host-index layers already implement and test. This
    function does not re-derive the arithmetic; it delegates, so the Python
    derivation stays a single source of truth with the HCL cross-check.

    Args:
        vlan_id: The project's VLAN ID (100+ for a consumer DB; this helper does
            not itself enforce the lower bound — the SVC-01 root's ``vlan_id``
            validation and ``proxmox-compute`` Guard 1 do, Requirement 3.2).
        host_index: Zero-based host index sourced from the Host_Index_Authority
            (Requirement 3.8).

    Returns:
        The dotted-quad IPv4 string, e.g. ``"10.0.100.10"`` for
        ``(vlan_id=100, host_index=0)``.

    Raises:
        HostAddressExhaustionError: If ``10 + host_index`` exceeds ``.254``
            (surfaced from the shared layer, Requirement 2.6 semantics).
    """
    return host_ip(vlan_id, host_index)


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(
            f"slug {slug!r} must be lowercase-hyphenated and <=20 characters "
            f"(matching {_SLUG_RE.pattern!r}) to derive a state name."
        )
    return slug


def consumer_db_state_name(slug: str) -> str:
    """Return the SVC01_Consumer_Root Terraform state name ``<slug>-db`` (Req 3.5).

    The derivation is injective in the slug (two distinct slugs never derive the
    same DB state name, since appending a fixed suffix preserves distinctness)
    and is never equal to the same slug's ``<slug>-infra`` network-slice state
    name (the suffixes ``-db`` and ``-infra`` differ), preserving
    one-state-per-service (Property 4).

    Args:
        slug: The project slug (lowercase-hyphenated, <=20 chars).

    Returns:
        The state name ``"<slug>-db"``.

    Raises:
        ValueError: If ``slug`` is malformed.
    """
    return f"{_validate_slug(slug)}{SVC01_STATE_SUFFIX}"


def network_slice_state_name(slug: str) -> str:
    """Return the network-slice Terraform state name ``<slug>-infra``.

    Provided so the state-name distinctness property can assert
    ``consumer_db_state_name(slug) != network_slice_state_name(slug)`` for every
    slug without hard-coding the suffix in the test (Requirement 3.5, Property 4).

    Args:
        slug: The project slug.

    Returns:
        The state name ``"<slug>-infra"``.

    Raises:
        ValueError: If ``slug`` is malformed.
    """
    return f"{_validate_slug(slug)}{NETWORK_SLICE_STATE_SUFFIX}"
