#!/usr/bin/env python3
"""Pure Host_Index_Authority ledger-derivation helper (fix-consumer-onboarding-
prerequisites, §2).

Derives, from a Consumer_Manifest, the Host_Index_Authority rows a project VLAN
MUST contain for onboarding to provision its declared guests, and checks them
against the committed ledger (``infra/platform-foundation/host-indices.yaml``).

Derivation rule (design "Required_Ledger_Rows"):
  * a ``database`` block  ⇒ an ``svc_code: svc01`` row at ``host_index 0`` (the
    DB guest; ``anchor: true``, ``status: allocated``);
  * ``workload.enabled: true``  ⇒ a ``role_code: workload`` row at
    ``host_index 1`` (the workload guest; ``status: allocated``).

A manifest that declares neither derives the EMPTY set (the strict no-op path).

MATCH SHAPE. A required row is "present" iff the VLAN's ledger block contains an
entry whose identity key matches — ``svc_code == "svc01"`` with NO ``component``
for the DB row, ``role_code == "workload"`` with NO ``component`` for the
workload row — the SAME ``lookup``-based identity semantics the ``db/`` and
``workload/`` Terraform roots use, so the Phase-1 check and the Terraform
backstop can never disagree.

This module is pure and stdlib-only (the established platform pattern alongside
``cluster_env.py`` / ``consumer_registry.py`` / ``datastore_select.py``): the
derivation is a pure function; only the thin ``main``/CLI reads the ledger file.
It NEVER writes the committed ledger (ADR-0015 committed-input invariant;
ADR-0012 author-then-apply) — at most it can EMIT the derived rows to a separate
proposal file for an operator to review and commit.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


def required_ledger_rows(manifest: dict[str, Any], vlan_id: int) -> list[dict[str, Any]]:
    """Return the ledger rows the manifest implies on ``vlan_id``.

    Pure: depends only on its arguments. The returned rows are in the committed
    ledger's field order/shape so an emitted proposal is paste-ready.
    """
    rows: list[dict[str, Any]] = []
    manifest = manifest or {}

    # A declared `database` block ⇒ the per-project SVC-01 DB guest at index 0.
    if manifest.get("database") is not None:
        rows.append(
            {
                "svc_code": "svc01",
                "instance": 1,
                "host_index": 0,
                "anchor": True,
                "status": "allocated",
            }
        )

    workload = manifest.get("workload")
    if isinstance(workload, dict) and bool(workload.get("enabled", False)):
        # workload.enabled: true ⇒ the per-project workload guest at index 1.
        rows.append(
            {
                "role_code": "workload",
                "instance": 1,
                "host_index": 1,
                "status": "allocated",
            }
        )
    return rows


def _row_identity_present(required: dict[str, Any], ledger_rows: list[dict[str, Any]]) -> bool:
    """True iff an existing ledger row matches the required row's identity key.

    Matches on EXACTLY the identity semantics the Terraform roots use:
    svc_code/role_code equality AND the same (absent) component.
    """
    req_svc = required.get("svc_code")
    req_role = required.get("role_code")
    req_component = required.get("component")  # None for the primary/sole guest
    for e in ledger_rows or []:
        if not isinstance(e, dict):
            continue
        if e.get("component") != req_component:
            continue
        if req_svc is not None and e.get("svc_code") == req_svc:
            return True
        if req_role is not None and e.get("role_code") == req_role:
            return True
    return False


def missing_ledger_rows(
    manifest: dict[str, Any], vlan_id: int, ledger: dict[str, Any]
) -> list[dict[str, Any]]:
    """The required rows for ``vlan_id`` that are NOT present in ``ledger``."""
    required = required_ledger_rows(manifest, vlan_id)
    vlan_block = (ledger or {}).get(vlan_id) or (ledger or {}).get(int(vlan_id)) or []
    return [r for r in required if not _row_identity_present(r, vlan_block)]


def _render_rows_yaml(vlan_id: int, rows: list[dict[str, Any]]) -> str:
    """Render the rows as a paste-ready YAML block under the ``<vlan_id>:`` key."""
    if yaml is not None:
        body = yaml.safe_dump({vlan_id: rows}, sort_keys=False, default_flow_style=False)
        return body
    # Minimal fallback if PyYAML is unavailable (should not happen in this repo).
    lines = [f"{vlan_id}:"]
    for r in rows:
        first = True
        for k, v in r.items():
            prefix = "  - " if first else "    "
            lines.append(f"{prefix}{k}: {v}")
            first = False
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Derive/check Host_Index_Authority rows for a consumer project VLAN."
    )
    p.add_argument("--manifest", required=True, help="path to the Consumer_Manifest YAML")
    p.add_argument("--vlan-id", required=True, type=int, help="the project's Registry-resolved VLAN id")
    p.add_argument(
        "--ledger",
        default="infra/platform-foundation/host-indices.yaml",
        help="path to the committed Host_Index_Authority ledger",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero (3) if any required row is missing; print the exact block to add",
    )
    p.add_argument(
        "--emit",
        metavar="PROPOSAL_PATH",
        default=None,
        help="write the derived rows to a SEPARATE proposal file (never the committed ledger)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    if yaml is None:  # pragma: no cover
        sys.stderr.write("PyYAML is required for consumer_ledger.py\n")
        return 2
    args = _build_parser().parse_args(argv)
    with open(args.manifest, encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh) or {}
    try:
        with open(args.ledger, encoding="utf-8") as fh:
            ledger = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        ledger = {}

    missing = missing_ledger_rows(manifest, args.vlan_id, ledger)

    if args.emit:
        proposed = required_ledger_rows(manifest, args.vlan_id)
        with open(args.emit, "w", encoding="utf-8") as fh:
            fh.write(_render_rows_yaml(args.vlan_id, proposed))
        sys.stderr.write(f"Wrote {len(proposed)} proposed ledger row(s) to {args.emit}\n")

    if args.check and missing:
        sys.stderr.write(
            f"Host_Index_Authority ledger is missing {len(missing)} required row(s) for "
            f"VLAN {args.vlan_id}. Append the following under the `{args.vlan_id}:` key in "
            f"{args.ledger} and commit it as a reviewable MR, then re-run "
            "(nothing was provisioned):\n\n"
            + _render_rows_yaml(args.vlan_id, missing)
            + "\n"
        )
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
