#!/usr/bin/env python3
"""Emit the ``projects.yaml`` desired-state view as JSON for the gateway role.

This is the "one reader of truth" seam described in the feature design's
Components (C6): the ``sdn_gateway`` Ansible role must know which project VLANs
are currently ``active`` (configure their host gateway + firewall) versus
``decommissioned`` (tear that config down), but Ansible must NOT re-parse
``projects.yaml`` itself and duplicate the reader. Instead this small CLI reads
the registry once via ``onboard_project.load_registry`` and emits the two sets
computed by the pure view functions in ``netfoundation.registry_views``::

    {"active_vlan_ids": [...], "decommissioned_vlan_ids": [...]}

- ``active_vlan_ids`` is the Desired_State_View: every row whose normalized
  status is ``active`` (Requirement 6.1). A missing/empty ``status`` defaults to
  ``active`` (Requirement 5.2, FD.3).
- ``decommissioned_vlan_ids`` is the complement over the Allocation_Ledger_View:
  ``all_vlan_ids`` minus ``active_vlan_ids`` — the recorded project VLANs that
  are NOT active (Requirement 6.3). These drive the role's teardown direction.

Note that the always-served shared-services VLAN (20) is **not** part of this
payload: VLAN 20 is served unconditionally by the role and is not registry
driven (see design "Served-VLAN status payload"). This emitter reports only the
per-project VLANs recorded in ``projects.yaml``.

The command reads only the registry file (no network, no Terraform state, no
Proxmox call) and writes JSON to stdout. To feed it straight into the gateway
playbook as ``--extra-vars``, pass ``--ansible-vars`` so the keys carry the role's
variable names (``sdn_gateway_active_vlan_ids`` ...)::

    PYTHONPATH=infra/platform-foundation/scripts ~/venv/devinfra/bin/python \\
      infra/platform-foundation/scripts/registry_status.py --json --ansible-vars --out /tmp/vlan-status.json
    ~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/proxmox-hosts.yml \\
      ansible/playbooks/sdn-gateway.yml \\
      --extra-vars "@/tmp/vlan-status.json" \\
      --extra-vars "sdn_gateway_admin_source_cidr=<dev-net-cidr>"

Invoke via the project venv binary by absolute path, e.g.::

    ~/venv/devinfra/bin/python infra/platform-foundation/scripts/registry_status.py --json

(set ``PYTHONPATH`` to include this ``scripts`` dir so ``netfoundation`` and
``onboard_project`` resolve, as ``conftest.py`` does for the test suite).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from netfoundation.registry_views import active_vlan_ids, all_vlan_ids
from onboard_project import _DEFAULT_REGISTRY, OnboardingError, load_registry


def compute_vlan_status(registry: dict[str, Any]) -> dict[str, list[int]]:
    """Compute the desired-state VLAN payload from a parsed registry.

    Pure and side-effect-free: given the ``{"projects": [...]}`` mapping it
    returns ``{"active_vlan_ids": [...], "decommissioned_vlan_ids": [...]}``.

    - ``active_vlan_ids`` is the Desired_State_View (Requirement 6.1).
    - ``decommissioned_vlan_ids`` is the complement over the ledger:
      ``all_vlan_ids`` minus ``active_vlan_ids`` (Requirement 6.3), preserving
      registry order and reporting each ID at most once. Because a row is either
      active or not, this is exactly the set of recorded VLAN IDs whose row is
      ``decommissioned``.

    The always-served VLAN 20 is deliberately absent — it is not registry
    driven (design "Served-VLAN status payload").

    Args:
        registry: The parsed registry mapping with a ``projects`` list.

    Returns:
        A mapping with sorted ``active_vlan_ids`` and ``decommissioned_vlan_ids``
        lists (the role treats them as sets; sorting makes the output stable).

    Raises:
        ValueError: If any recorded ``vlan_id`` is not an integer or any row
            carries an unrecognised ``status`` value (surfaced from the view
            functions).
    """
    active = active_vlan_ids(registry)
    active_set = set(active)
    decommissioned = [vid for vid in all_vlan_ids(registry) if vid not in active_set]
    return {
        "active_vlan_ids": sorted(active_set),
        "decommissioned_vlan_ids": sorted(set(decommissioned)),
    }


#: Payload key -> the ``sdn_gateway`` role input variable it feeds (``--ansible-vars``).
SDN_GATEWAY_VAR_NAMES = {
    "active_vlan_ids": "sdn_gateway_active_vlan_ids",
    "decommissioned_vlan_ids": "sdn_gateway_decommissioned_vlan_ids",
}


def as_sdn_gateway_vars(payload: dict[str, list[int]]) -> dict[str, list[int]]:
    """Rename the payload keys to the ``sdn_gateway`` role's input variable names.

    The default payload keys (``active_vlan_ids`` ...) are what ``platform_gateway``
    parses; the role itself reads ``sdn_gateway_active_vlan_ids`` /
    ``sdn_gateway_decommissioned_vlan_ids``. A file fed straight to
    ``sdn-gateway.yml`` with ``--extra-vars @<file>`` must use the role's names, or
    the role sees an empty active set (fix-consumer-expose-route-and-gateway-docs).
    """
    return {SDN_GATEWAY_VAR_NAMES[key]: value for key, value in payload.items()}


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI parser."""
    parser = argparse.ArgumentParser(
        prog="registry_status.py",
        description=(
            "Emit the projects.yaml desired-state view as JSON "
            '({"active_vlan_ids": [...], "decommissioned_vlan_ids": [...]}) '
            "for the sdn_gateway Ansible role. Reads the registry only; never "
            "touches infrastructure. VLAN 20 (always served) is not included."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit the payload as JSON (the default and only output format; "
            "the flag exists for explicitness at the call site)."
        ),
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=_DEFAULT_REGISTRY,
        help=(
            "Path to the projects.yaml VLAN-ID registry "
            f"(default: {_DEFAULT_REGISTRY})."
        ),
    )
    parser.add_argument(
        "--ansible-vars",
        action="store_true",
        help=(
            "Name the keys after the sdn_gateway role's input variables "
            "(sdn_gateway_active_vlan_ids / sdn_gateway_decommissioned_vlan_ids), "
            "so the file can be passed to sdn-gateway.yml as --extra-vars @<file>. "
            "Same values; without this flag the keys are active_vlan_ids / "
            "decommissioned_vlan_ids."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Write the JSON payload to this file as well as stdout. Useful for "
            "feeding the role via `--extra-vars @<file>` when process "
            "substitution is unavailable."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code.

    Emits the JSON payload to stdout (and optionally to ``--out``). Aborts with
    a clear ``ERROR: ...`` line on stderr and exit 1 if the registry cannot be
    read/parsed or a row is malformed — no partial or invalid JSON is written.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        registry = load_registry(args.registry)
        payload = compute_vlan_status(registry)
    except (OnboardingError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.ansible_vars:
        payload = as_sdn_gateway_vars(payload)
    rendered = json.dumps(payload)
    if args.out is not None:
        try:
            args.out.write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: could not write {args.out}: {exc}", file=sys.stderr)
            return 1
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
