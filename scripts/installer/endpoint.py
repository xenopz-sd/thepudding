#!/usr/bin/env python3
"""Canonical-base normalizer for the Proxmox endpoint value.

Feature: fix-cluster-env-endpoint-path (bugfix; design.md "Fix Implementation",
Option A; Correctness Property 1 — Bug Condition, Property 2 — Preservation).

The SVC-07 automated installer resolves a Proxmox endpoint value from the
operator's cluster env file and consumes it as a **base URL** in two places that
both expect **no** trailing ``/api2/json`` API path:

* Phase-1 Preflight in ``ansible/playbooks/svc-07-bootstrap.yml`` builds three
  Proxmox API URLs by APPENDING ``/api2/json...`` to the resolved endpoint.
* The bpg/proxmox Terraform provider
  (``infra/projects/svc-07-secrets-manager/providers.tf``) takes a bare base
  endpoint — its variable description says "No trailing path beyond the
  host/port."

An endpoint carrying a trailing ``/api2/json`` (or a trailing slash) breaks both:
Preflight doubles the path (``.../api2/json/api2/json/version`` → HTTP 501) and
the provider receives a malformed base. This module is the single, tested
reference for reducing any accepted endpoint form to the one canonical base both
consumers expect.

**Single source of truth.** Under design Option A the playbook performs this
normalization inline as a Jinja ``regex_replace`` chain (to avoid a subprocess
per run). This module is the AUTHORITATIVE reference that the Jinja mirrors, and
``tests/installer/test_endpoint.py`` pins its behavior; if the rule ever changes,
change it here and in the playbook's Jinja together. A ``--endpoint`` CLI is
provided so the playbook (or an operator) can invoke the exact same Python logic
if a subprocess call is ever preferred over the Jinja mirror.
"""

from __future__ import annotations

import re

# The regex chain mirrored by the playbook's Jinja (design.md Option A):
#   regex_replace('/+$', '')          -> drop trailing slash(es)
#   regex_replace('/api2/json$', '')  -> drop a single trailing /api2/json
#   regex_replace('/+$', '')          -> drop any slash the strip exposed
_TRAILING_SLASHES = re.compile(r"/+$")
_TRAILING_API2JSON = re.compile(r"/api2/json$")


def canonical_base(endpoint: str) -> str:
    """Return the canonical Proxmox base URL for ``endpoint``.

    Strips surrounding whitespace, any trailing slash(es), a single trailing
    ``/api2/json`` (exact, case-sensitive) if present, and any slash the strip
    exposes. The result is ``scheme://host[:port]`` with no API path and no
    trailing slash. The function is idempotent: ``canonical_base`` of an
    already-canonical base is a no-op.

    All accepted forms collapse to the same base:

    * ``https://h:8006``            -> ``https://h:8006``
    * ``https://h:8006/``           -> ``https://h:8006``
    * ``https://h:8006/api2/json``  -> ``https://h:8006``
    * ``https://h:8006/api2/json/`` -> ``https://h:8006``
    """
    value = endpoint.strip()
    value = _TRAILING_SLASHES.sub("", value)
    value = _TRAILING_API2JSON.sub("", value)
    value = _TRAILING_SLASHES.sub("", value)
    return value


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Normalize a Proxmox endpoint to its canonical base URL "
            "(strip a trailing /api2/json and any trailing slash)."
        )
    )
    parser.add_argument(
        "--endpoint",
        required=True,
        help="The resolved Proxmox endpoint value to normalize.",
    )
    args = parser.parse_args(argv)
    print(canonical_base(args.endpoint))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(_main())
