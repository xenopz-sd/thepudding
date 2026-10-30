#!/usr/bin/env python3
"""Registry resolver for the project-consumer-onboarding feature.

Feature: project-consumer-onboarding (design.md §Components → Phase-1 preflight
"Registry resolution"; "Correctness Properties" → Property 2; tasks.md Task 3.1;
Requirements 2.1-2.5).

The Consumer_Onboarding_Orchestrator's Phase-1 ``consumer_preflight`` role resolves
a project's ``vlan_id`` **only** from the Registry
(``infra/platform-foundation/projects.yaml``) — never from an operator free input
(Requirement 2.3). This module is that pure resolver surface, extracted as a
standalone, **Ansible-free, stdlib+PyYAML** module so it can be unit- and
property-tested offline.

Resolution contract (Property 2):

- :func:`resolve_vlan_id` returns the ``vlan_id`` of the SINGLE registry entry whose
  ``slug`` equals the query slug **exactly and case-sensitively** (Requirement 2.1).
- A query slug absent from the Registry raises :class:`SlugAbsentError`
  (Requirement 2.2) — a no-resolution halt directing the operator to the onboarding
  script + merge-request flow, NOT a wrong/arbitrary VLAN.
- A matched entry with ``status: decommissioned`` raises
  :class:`ProjectDecommissionedError` (Requirement 2.4).
- An unreadable / malformed / schema-invalid Registry raises
  :class:`RegistryUnreadableError` (Requirement 2.5).

Each halt is a DISTINCT exception subclass of :class:`RegistryResolutionError`, so
the orchestrator preflight (and the property tests) can tell the three
no-resolution classifications apart (Property 2). Every halt leaves zero
provisioning side effect — this module only reads a file.

``vlan_id`` is intentionally an OUTPUT of this module and never an input: there is
no parameter, env read, or override by which an operator could supply a VLAN
(Requirement 2.3).

Per ``testing-strategy.md`` "Verification Scope", any change to the signatures,
return types, or raised exceptions below is a shared-symbol change: it requires a
repo-wide caller search and the FULL offline suite, not just this feature's tests.

Invoke the offline ``--resolve`` doctest surface via the venv binary by absolute
path, e.g.::

    ~/venv/devinfra/bin/python scripts/installer/consumer_registry.py \
      --resolve dronefleet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "RegistryResolutionError",
    "SlugAbsentError",
    "ProjectDecommissionedError",
    "RegistryUnreadableError",
    "DEFAULT_REGISTRY_PATH",
    "resolve_vlan_id",
    "resolve_vlan_id_from_file",
    "main",
]

#: Default location of the Registry relative to this script:
#: scripts/installer/ -> repo root -> infra/platform-foundation/projects.yaml.
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "platform-foundation"
    / "projects.yaml"
)

#: Status marking a retired project. A matched-but-decommissioned entry halts
#: rather than resolving (Requirement 2.4); the row stays in the Registry so its
#: VLAN ID is never reused (projects.yaml append-only discipline).
_DECOMMISSIONED_STATUS = "decommissioned"


class RegistryResolutionError(Exception):
    """Base class for every Registry no-resolution halt.

    The three concrete subclasses below are DISTINCT so the orchestrator and the
    property tests can classify the halt (Property 2). Every subclass leaves zero
    provisioning side effect.
    """


class SlugAbsentError(RegistryResolutionError):
    """Raised when the query slug is not present in the Registry (Req 2.2).

    Directs the operator to onboard the slug via the onboarding script + MR flow
    first; no VLAN is invented.
    """


class ProjectDecommissionedError(RegistryResolutionError):
    """Raised when the matched Registry entry is ``status: decommissioned`` (Req 2.4)."""


class RegistryUnreadableError(RegistryResolutionError):
    """Raised when the Registry is missing, unreadable, or malformed (Req 2.5)."""


def resolve_vlan_id(registry: Any, slug: str) -> int:
    """Resolve a slug to its ``vlan_id`` from an already-parsed Registry mapping.

    Pure and side-effect-free: operates only on the passed-in parsed structure.
    The exact-case match is the single entry whose ``slug`` equals ``slug`` with
    ``==`` (case-sensitive); a case-mismatch is treated as absent, never coerced
    to a near match (Requirement 2.1, Property 2).

    Args:
        registry: The parsed Registry (``{"projects": [ {slug, vlan_id, status?},
            ... ]}``). Anything else is treated as malformed.
        slug: The query project slug.

    Returns:
        The matched entry's integer ``vlan_id``.

    Raises:
        RegistryUnreadableError: If ``registry`` is not the expected
            ``{"projects": [...]}`` shape, an entry is not a mapping, lacks a
            ``slug``/``vlan_id``, or carries a non-integer ``vlan_id``
            (Requirement 2.5).
        SlugAbsentError: If no entry's ``slug`` equals ``slug`` exactly and
            case-sensitively (Requirement 2.2).
        ProjectDecommissionedError: If the matched entry is
            ``status: decommissioned`` (Requirement 2.4).
    """
    if not isinstance(registry, dict):
        raise RegistryUnreadableError(
            "Registry root must be a mapping with a 'projects' list; got "
            f"{type(registry).__name__}."
        )
    projects = registry.get("projects")
    if not isinstance(projects, list):
        raise RegistryUnreadableError(
            "Registry 'projects' key must be a list; got "
            f"{type(projects).__name__}."
        )

    match: dict[str, Any] | None = None
    for i, entry in enumerate(projects):
        if not isinstance(entry, dict):
            raise RegistryUnreadableError(
                f"Registry entry #{i} is not a mapping: {entry!r}."
            )
        if "slug" not in entry or "vlan_id" not in entry:
            raise RegistryUnreadableError(
                f"Registry entry #{i} is missing a 'slug' or 'vlan_id': {entry!r}."
            )
        # Exact, case-sensitive match only (Requirement 2.1). A case-mismatch is
        # NOT a match, so it falls through to the absent halt below.
        if entry["slug"] == slug:
            match = entry
            break

    if match is None:
        raise SlugAbsentError(
            f"Project slug {slug!r} is absent from the Registry "
            "(infra/platform-foundation/projects.yaml); onboard it via the "
            "onboarding script + merge-request flow before consumer onboarding "
            "(Requirement 2.2)."
        )

    vlan_id = match["vlan_id"]
    if not isinstance(vlan_id, int) or isinstance(vlan_id, bool):
        raise RegistryUnreadableError(
            f"Registry entry for slug {slug!r} has a non-integer vlan_id: "
            f"{vlan_id!r} (Requirement 2.5)."
        )

    status = match.get("status")
    # A missing/empty status defaults to active (projects.yaml semantics); only an
    # explicit decommissioned status halts.
    if status == _DECOMMISSIONED_STATUS:
        raise ProjectDecommissionedError(
            f"Project {slug!r} is decommissioned (vlan_id {vlan_id} retired); "
            "consumer onboarding is refused (Requirement 2.4)."
        )

    return vlan_id


def resolve_vlan_id_from_file(
    slug: str, registry_path: Path | str = DEFAULT_REGISTRY_PATH
) -> int:
    """Read the Registry file and resolve ``slug`` to its ``vlan_id``.

    The thin reader layer around :func:`resolve_vlan_id`: it performs the only
    filesystem/parse I/O (mirroring ``onboard_project.load_registry``) and then
    delegates to the pure resolver. A missing/unreadable file or invalid YAML is
    surfaced as :class:`RegistryUnreadableError` (Requirement 2.5).

    ``vlan_id`` is never accepted as input — the only inputs are the query slug
    and the Registry path (Requirement 2.3).

    Args:
        slug: The query project slug.
        registry_path: Path to ``projects.yaml`` (defaults to the in-repo
            Registry).

    Returns:
        The matched entry's integer ``vlan_id``.

    Raises:
        RegistryUnreadableError, SlugAbsentError, ProjectDecommissionedError:
            as documented on :func:`resolve_vlan_id`, plus
            :class:`RegistryUnreadableError` for file-IO / YAML-parse failures.
    """
    import yaml  # local import: PyYAML only needed for the file read path.

    path = Path(registry_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RegistryUnreadableError(
            f"Registry file not found: {path} (Requirement 2.5)."
        ) from exc
    except OSError as exc:
        raise RegistryUnreadableError(
            f"Registry file could not be read: {path} ({exc}) (Requirement 2.5)."
        ) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RegistryUnreadableError(
            f"Registry file is not valid YAML: {path} ({exc}) (Requirement 2.5)."
        ) from exc

    if data is None:
        data = {}
    return resolve_vlan_id(data, slug)


# --- CLI (offline doctest surface) ---------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI parser for the ``--resolve`` offline doctest mode."""
    parser = argparse.ArgumentParser(
        prog="consumer_registry.py",
        description=(
            "Resolve a project slug to its VLAN ID from the projects.yaml "
            "Registry. Offline, side-effect-free: reads the Registry and prints "
            "the vlan_id, or halts with a distinct classification (absent / "
            "decommissioned / unreadable). Never accepts a vlan_id as input and "
            "never mutates infrastructure. Exit 0 = resolved, 1 = no-resolution "
            "halt, 2 = usage error."
        ),
    )
    parser.add_argument(
        "--resolve",
        metavar="SLUG",
        required=True,
        help="Project slug to resolve to its VLAN ID.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help=(
            "Path to the projects.yaml Registry "
            f"(default: {DEFAULT_REGISTRY_PATH})."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code.

    Exit 0 and print the resolved ``vlan_id`` on success; exit 1 with a distinct
    classification message on any no-resolution halt (absent / decommissioned /
    unreadable); exit 2 on a usage error. Never mutates infrastructure.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        vlan_id = resolve_vlan_id_from_file(args.resolve, args.registry)
    except SlugAbsentError as exc:
        print(f"HALT (absent): {exc}", file=sys.stderr)
        return 1
    except ProjectDecommissionedError as exc:
        print(f"HALT (decommissioned): {exc}", file=sys.stderr)
        return 1
    except RegistryUnreadableError as exc:
        print(f"HALT (unreadable/malformed): {exc}", file=sys.stderr)
        return 1

    print(f"{args.resolve} -> VLAN {vlan_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
