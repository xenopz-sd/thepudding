"""Documentation-correctness guard for fix-cluster-env-endpoint-path (Task 1).

Feature: fix-cluster-env-endpoint-path, Property 1: Bug Condition — endpoint
normalized to a canonical base.

This is the offline documentation-correctness half of the Task-1 exploration:
it reads the committed ``cluster.env.example`` at the repo root, extracts the
ACTIVE (uncommented) ``PROXMOX_ENDPOINT`` value AND the ``TF_VAR_proxmox_endpoint``
alias twin shown in the template, and asserts NEITHER ends in ``/api2/json``
(ignoring one trailing slash). On the UNFIXED template both placeholders end in
``/api2/json``, so this FAILS — reproducing, in miniature, the exact defect an
operator copies verbatim.

This guard needs no ``canonical_base`` helper (it is a pure committed-file read),
so it lives in its own module — separate from ``test_endpoint.py``, whose
``from endpoint import canonical_base`` fails to collect on unfixed code — so the
guard can fail on the TEMPLATE's own merits rather than on an ImportError. Both
files are selectable via ``pytest -k api2json``.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/installer/test_endpoint_template.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / "cluster.env.example"


def _extract_endpoint_value(line_key: str) -> str:
    """Return the quoted-or-bare value assigned to ``line_key`` in the template.

    Matches either an ACTIVE line (``KEY=...``) or a commented alias line
    (``#KEY=...``), because ``cluster.env.example`` documents the two endpoint
    aliases as one active + one commented placeholder. Surrounding matching
    quotes are stripped so the raw URL is compared (the sibling quote-fix owns
    quote handling; here we only care about the path shape).
    """
    text = _TEMPLATE.read_text(encoding="utf-8")
    # Allow an optional leading '#' (commented alias) and surrounding whitespace.
    pattern = rf"^\s*#?\s*{re.escape(line_key)}\s*=\s*(.+?)\s*$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    assert match is not None, f"{line_key} placeholder not found in {_TEMPLATE}"
    value = match.group(1).strip()
    # Strip one surrounding matching quote pair, if present.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def _endpoint_has_no_api2json(value: str) -> bool:
    """True iff ``value`` (ignoring one trailing slash) does NOT end in /api2/json."""
    trimmed = value[:-1] if value.endswith("/") else value
    return not trimmed.endswith("/api2/json")


def test_example_endpoint_placeholder_has_no_api2json():
    """Feature: fix-cluster-env-endpoint-path, Property 1: Bug Condition —
    endpoint normalized to a canonical base.

    The committed ``cluster.env.example`` endpoint placeholders (both the
    ``PROXMOX_ENDPOINT`` active form and the ``TF_VAR_proxmox_endpoint`` alias
    twin) MUST document a BASE URL — neither may end in ``/api2/json`` (ignoring
    one trailing slash). FAILS on the unfixed template (which ends in
    ``/api2/json``); passes once Part 1 corrects the template to a base URL.
    """
    proxmox_endpoint = _extract_endpoint_value("PROXMOX_ENDPOINT")
    tfvar_endpoint = _extract_endpoint_value("TF_VAR_proxmox_endpoint")

    assert _endpoint_has_no_api2json(proxmox_endpoint), (
        f"PROXMOX_ENDPOINT placeholder in cluster.env.example ends in /api2/json "
        f"(base-URL-vs-API-path defect): {proxmox_endpoint!r}"
    )
    assert _endpoint_has_no_api2json(tfvar_endpoint), (
        f"TF_VAR_proxmox_endpoint placeholder in cluster.env.example ends in "
        f"/api2/json (base-URL-vs-API-path defect): {tfvar_endpoint!r}"
    )
