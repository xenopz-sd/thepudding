#!/usr/bin/env python3
"""Pure datastore-selection helper (fix-workload-datastore-autodetect, §5a).

The SINGLE implementation of the platform's "which Proxmox datastore" rule,
consumed by BOTH the SVC-01 preflight and the consumer-onboarding preflight so
the per-project DB guest and the per-project workload guest can never disagree
on datastore selection for the same node.

Rule (design "Fix Implementation" §1):
  * an explicit, non-empty, non-sentinel ``configured`` value  -> that value
    verbatim (operator override wins; no node query needed);
  * else ``local-zfs`` if present on the node;
  * else ``local-lvm`` if present on the node;
  * else a fail-closed "unresolved" result (NEVER silently ``local-zfs``).

This module is pure and stdlib-only (the established platform pattern alongside
``cluster_env.py`` / ``consumer_registry.py``): it performs no network call and
no filesystem mutation. The live ``GET /nodes/<node>/storage`` query stays in the
Ansible layer; only the selection over the returned storage-id list lives here,
keeping the helper offline-testable. It reuses the single ``AUTODETECT`` sentinel
defined in :mod:`cluster_env` rather than re-declaring a second literal.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, NamedTuple

try:  # normal package-relative import when run as a module
    from .cluster_env import AUTODETECT
except ImportError:  # pragma: no cover - direct-script / test import fallback
    from cluster_env import AUTODETECT  # type: ignore[no-redef]

# Preference order for auto-detection (design: local-zfs preferred, local-lvm
# fallback). Kept as a tuple so the order is the single source of truth.
_PREFERENCE_ORDER: tuple[str, ...] = ("local-zfs", "local-lvm")


class Resolution(NamedTuple):
    """The outcome of a datastore selection.

    ``datastore`` is the resolved storage id when ``resolved`` is True; it is the
    empty string when ``resolved`` is False (the fail-closed case). ``source`` is
    one of ``"override"`` | ``"autodetect"`` | ``"unresolved"`` for diagnostics.
    """

    resolved: bool
    datastore: str
    source: str


def select_datastore(node_storage_ids: Iterable[str], configured: str) -> Resolution:
    """Select the datastore for a guest on a node.

    :param node_storage_ids: storage ids present on the target Proxmox node
        (as returned by ``GET /nodes/<node>/storage``). Ignored when an explicit
        ``configured`` value is supplied.
    :param configured: the operator-configured datastore, or the ``AUTODETECT``
        sentinel (or empty) to request auto-detection.
    """
    configured = (configured or "").strip()
    # Explicit override wins verbatim — no node query, no preference logic.
    if configured and configured != AUTODETECT:
        return Resolution(resolved=True, datastore=configured, source="override")

    present = set(node_storage_ids or ())
    for candidate in _PREFERENCE_ORDER:
        if candidate in present:
            return Resolution(resolved=True, datastore=candidate, source="autodetect")

    # Fail closed — never silently fall back to a datastore that may not exist.
    return Resolution(resolved=False, datastore="", source="unresolved")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select a Proxmox datastore: explicit override verbatim, else "
            "local-zfs, else local-lvm, else fail closed."
        )
    )
    parser.add_argument(
        "--node-storage-ids",
        default="",
        help="comma-separated storage ids present on the target node",
    )
    parser.add_argument(
        "--configured",
        default=AUTODETECT,
        help=f"operator-configured datastore, or the {AUTODETECT} sentinel",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ids = [s.strip() for s in args.node_storage_ids.split(",") if s.strip()]
    result = select_datastore(ids, args.configured)
    if not result.resolved:
        sys.stderr.write(
            "No datastore configured and neither 'local-zfs' nor 'local-lvm' is "
            f"available on the target node (storage ids: {sorted(ids) or 'none'}). "
            "Set PROXMOX_DATASTORE_ID in the cluster env file and re-run.\n"
        )
        return 3
    sys.stdout.write(result.datastore + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
