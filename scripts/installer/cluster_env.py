#!/usr/bin/env python3
"""Cluster env-file parse / alias-resolution / validation helper.

Feature: svc-07-automated-installer (Req 1.3, 1.4, 2.1-2.8; design "Cluster
env-file variable schema", Phase 1 env-file loading & validation).

This is the one genuinely pure-logic surface of the SVC-07 automated installer.
The orchestrator playbook (``ansible/playbooks/svc-07-bootstrap.yml``) invokes it
during the Preflight_Phase to load the operator's gitignored ``cluster.env`` (or
an environment-specific ``cluster.dev.env`` / ``cluster.acc.env`` /
``cluster.prod.env``), resolve each logical variable from either its
``PROXMOX_``-prefixed or ``TF_VAR_``-prefixed alias, validate that all mandatory
variables are present, detect conflicting alias values, and emit the resolved
values as canonical ``TF_VAR_*`` entries for the Phase-2 Terraform drive.

Design invariants honoured here:

* **Pure, offline, testable core.** :func:`parse_env`, :func:`resolve`, and
  :func:`validate` are free of file/subprocess side effects and take/return
  plain data, so unit and Hypothesis property tests import them directly
  (design Testing Strategy; the only PBT target in this feature). :func:`main`
  is the thin file-IO / arg-parsing / stdout-emission wrapper.
* **Present == defined AND non-empty after trim (Req 2.4).** A key whose value is
  empty or whitespace-only is treated as absent.
* **Alias resolution (Req 2.4, 2.8).** Either alias may supply a logical var. If
  both are present with identical (trimmed) values they resolve to that single
  value; if both are present with conflicting non-empty values it is an error
  naming the pair.
* **Completeness (Req 2.5).** ALL missing/empty mandatory vars are collected and
  reported together in a single, order-independent message — never one-at-a-time.
* **File-absent is distinct (Req 2.3).** A missing/unreadable file raises a
  distinct error naming the path and directing the operator to copy
  ``cluster.env.example``.
* **No interactive prompt (Req 1.3, 2.7).** The helper never reads from stdin for
  a cluster parameter; it either resolves from the file or fails.
* **No secret ever echoed (Req 2.10 / security-standards).** The emitted
  ``TF_VAR_*`` output is intended to be consumed as an environment mapping by the
  orchestrator, not printed to a shared log; the API-token value is carried by
  key name and never annotated or duplicated into a human-facing message.

Usage::

    # Emit resolved TF_VAR_* entries (KEY=VALUE per line) to stdout as JSON:
    cluster_env.py --env-file cluster.env

    # Validation-only (exit non-zero on any error), no value emission:
    cluster_env.py --env-file cluster.env --check
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import NamedTuple


# --- Sentinel for the optional datastore var (Req 2.6, 3.5) --------------------
#
# When the datastore variable is absent/empty in the env file, the resolved
# datastore is this sentinel, which the orchestrator's Preflight_Phase reads as
# "perform storage auto-detection". A configured non-empty value suppresses
# auto-detection.
AUTODETECT = "__AUTODETECT__"


class LogicalVar(NamedTuple):
    """One row of the design's "Cluster env-file variable schema" table."""

    logical: str  # human-facing logical name, used in error messages
    proxmox_alias: str  # PROXMOX_-prefixed alias key
    tfvar_alias: str  # TF_VAR_-prefixed alias key (also the canonical emit key)
    mandatory: bool


# The schema table, verbatim from design "Cluster env-file variable schema".
# The tfvar_alias doubles as the canonical key emitted for the Terraform drive.
#
# The SSH-public-key-file row (fix-cluster-env-endpoint-path, member (c) of the
# "connection settings not derived from the operator's cluster config" family:
# (a) endpoint path, (b) TLS verification, (c) SSH public key) is OPTIONAL and
# deliberately resolves to a FILE PATH string, not the key itself. It is
# emitted under the passthrough canonical key ``TF_VAR_operator_ssh_public_key_file``
# — note the ``_file`` suffix: it is the PATH, distinct from the list-typed
# Terraform variable ``TF_VAR_operator_ssh_public_keys`` that the guests actually
# consume. Keeping the file-read + JSON-list encoding in the PLAYBOOK (which
# reads this path, ``expanduser``s it, and ``to_json``s a 1-element list) leaves
# this helper a generic path/string resolver — it performs NO file IO and NO
# list/JSON encoding for this one var, so the generic ``resolve``/``validate``
# contract stays intact. Absent => the key is simply not emitted => the playbook
# passes an empty ``[]`` list => Terraform's ``dynamic user_account`` block is
# omitted => strict no-op (guests provision but are SSH-unreachable, the
# documented default).
#
# Platform-prerequisites-bootstrap extension (feature
# platform-prerequisites-bootstrap, design "Component 7: cluster_env.py schema
# extension" / "Data Models"). The platform-bootstrap orchestrator
# (``ansible/playbooks/platform-bootstrap.yml``) drives three selectable buckets
# — host / api / devmachine — and reuses THIS helper to resolve its cluster.env.
# All rows added below are OPTIONAL (``mandatory=False``): per-bucket
# mandatory-ness is decided by the ORCHESTRATOR (Ansible), NOT by this pure core.
# ``cluster_env.py`` simply emits each var when it is present and stays silent
# when it is absent; the orchestrator asserts per-bucket completeness and
# supplies the documented defaults (role/token id, template storage, served
# VLANs) when a key is absent — deliberately NO new default/sentinel mechanism is
# introduced here (only the pre-existing datastore AUTODETECT sentinel remains
# special), keeping the pure ``parse_env``/``resolve``/``validate`` contract and
# its tests intact.
#
# Alias convention: where a Terraform consumer exists the row keeps the
# ``PROXMOX_`` / ``TF_VAR_`` pair as the other rows do. The platform-bootstrap
# params below have NO Terraform-variable consumer (they are consumed by the
# Ansible orchestrator, not passed to a ``terraform apply``), so their canonical
# emit key mirrors the ``PROXMOX_``/``PLATFORM_`` alias under a ``TF_VAR_``-form
# passthrough key purely to satisfy the two-alias resolution contract; the
# orchestrator reads them by their canonical emit key.
#
# SECRECY: ``PROXMOX_ADMIN_PASSWORD`` / ``PROXMOX_ADMIN_TOKEN`` are the
# bootstrap-only Admin_Bootstrap_Credential (used ONLY by the opt-in PVE-identity
# step). Exactly like the existing API token, their VALUES must never appear in
# any human-facing stderr/message — every error/validation message names logical
# vars, never values, and these vars are optional so they never appear in a
# "missing mandatory" list anyway. The resolved mapping (which may carry these
# values by key) goes to stdout only, for the caller to capture.
SCHEMA: tuple[LogicalVar, ...] = (
    # --- SDN / API (existing SVC-07 rows) ---
    LogicalVar("Proxmox endpoint", "PROXMOX_ENDPOINT", "TF_VAR_proxmox_endpoint", True),
    LogicalVar("API token", "PROXMOX_API_TOKEN", "TF_VAR_proxmox_api_token", True),
    LogicalVar("Node name", "PROXMOX_NODE_NAME", "TF_VAR_proxmox_node_name", True),
    LogicalVar("LXC template", "PROXMOX_LXC_TEMPLATE", "TF_VAR_openbao_template_file_id", True),
    LogicalVar("Datastore", "PROXMOX_DATASTORE_ID", "TF_VAR_openbao_datastore_id", False),
    LogicalVar(
        "SSH public key file",
        "PROXMOX_SSH_PUBLIC_KEY_FILE",
        "TF_VAR_operator_ssh_public_key_file",
        False,
    ),
    # --- Host prep (platform-bootstrap, all OPTIONAL — orchestrator-gated) ---
    LogicalVar(
        "Proxmox host LAN IP",
        "PROXMOX_HOST_LAN_IP",
        "TF_VAR_proxmox_host_lan_ip",
        False,
    ),
    # Admin_Bootstrap_Credential — bootstrap-only, VALUE never echoed (see note).
    LogicalVar(
        "Admin password",
        "PROXMOX_ADMIN_PASSWORD",
        "TF_VAR_proxmox_admin_password",
        False,
    ),
    LogicalVar(
        "Admin token",
        "PROXMOX_ADMIN_TOKEN",
        "TF_VAR_proxmox_admin_token",
        False,
    ),
    # Has-a-default vars: absent => NOT emitted; the orchestrator applies the
    # documented default (role id => TerraformProv, token id => installer,
    # template storage => local). No sentinel/default logic added here.
    LogicalVar(
        "Terraform role id",
        "PROXMOX_TERRAFORM_ROLE_ID",
        "TF_VAR_proxmox_terraform_role_id",
        False,
    ),
    LogicalVar(
        "Terraform token id",
        "PROXMOX_TERRAFORM_TOKEN_ID",
        "TF_VAR_proxmox_terraform_token_id",
        False,
    ),
    LogicalVar(
        "Template storage",
        "PROXMOX_TEMPLATE_STORAGE",
        "TF_VAR_proxmox_template_storage",
        False,
    ),
    # --- Gateway (platform-bootstrap) ---
    LogicalVar(
        "Admin source CIDR",
        "ADMIN_SOURCE_CIDR",
        "TF_VAR_sdn_gateway_admin_source_cidr",
        False,
    ),
    # --- Dev machine (platform-bootstrap) ---
    # Served VLAN ids: absent => NOT emitted; orchestrator defaults to 20. The
    # SSH identity file reuses the existing "SSH public key file" row above.
    LogicalVar(
        "Served VLAN ids",
        "PLATFORM_SERVED_VLAN_IDS",
        "TF_VAR_platform_served_vlan_ids",
        False,
    ),
)


class EnvFileNotFoundError(Exception):
    """Raised when the named Cluster_Env_File is absent or unreadable (Req 2.3).

    Distinct from :class:`ValidationError` so the caller (and tests) can tell a
    missing file apart from a present-but-incomplete file.
    """


class ValidationError(Exception):
    """Raised for missing mandatory vars or conflicting alias values.

    The message collects ALL problems found (Req 2.5), so a single raise reports
    every missing/empty mandatory var and every conflicting alias pair together.
    """


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` ``.env`` text into a dict.

    Pure function (Req 2.4 parsing rules):

    * Blank lines and comment lines (first non-space char is ``#``) are ignored.
    * A line without ``=`` is ignored (defensive; a bare token is not a KEY=VALUE).
    * The key is everything before the first ``=``; the value is everything
      after it (so ``=`` inside a value, e.g. a template id ``ds:vztmpl/x``,
      survives).
    * Surrounding whitespace is trimmed from both key and value. A trailing
      inline value is NOT comment-stripped — ``#`` only starts a comment when it
      is the first non-space character of a line — so a value legitimately
      containing ``#`` is preserved.
    * After whitespace trimming, one layer of surrounding matching quotes is
      removed from the value: if the trimmed value is at least two characters
      long and its first and last characters are the SAME quote character
      (single ``'`` or double ``"``), that surrounding pair is stripped. Exactly
      one pair is removed, and only when the pair matches — an unbalanced or
      mismatched quote (``"value``, ``value"``, ``"value'``) is left untouched.
      Interior content — including an interior ``=``, an interior ``#``, and
      interior whitespace — is preserved (only the outermost pair is removed). A
      bare ``""`` / ``''`` normalizes to the empty string.
    * On a duplicate key, the last occurrence wins.

    Values are returned exactly as written (trimmed, with one surrounding
    matching quote pair removed as above); emptiness is judged later by
    :func:`resolve` so callers can distinguish "absent key" from "empty value"
    if they wish — both are treated as absent by resolution.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        result[key] = value
    return result


def _present(env: dict[str, str], key: str) -> str | None:
    """Return the trimmed value of ``key`` if present AND non-empty, else None.

    Implements the Req 2.4 "present == defined and non-empty after trimming"
    rule. ``parse_env`` already trims, but this re-trims defensively so the
    function is correct for any dict, including one built directly by a test.
    """
    if key not in env:
        return None
    value = env[key].strip()
    return value if value else None


class Resolution(NamedTuple):
    """Result of resolving one logical var from its two aliases."""

    value: str | None  # the resolved non-empty value, or None if absent
    conflict: bool  # True if both aliases present with differing non-empty values


def resolve_one(env: dict[str, str], spec: LogicalVar) -> Resolution:
    """Resolve a single logical var from its PROXMOX_ / TF_VAR_ aliases.

    Pure function (Req 2.4, 2.8):

    * Neither alias present/non-empty -> ``Resolution(None, False)``.
    * Exactly one alias present -> that value, no conflict.
    * Both present with identical trimmed values -> that value, no conflict.
    * Both present with differing non-empty values -> ``Resolution(None, True)``
      (a conflict; the value is left unresolved and the caller reports the pair).
    """
    p_val = _present(env, spec.proxmox_alias)
    t_val = _present(env, spec.tfvar_alias)

    if p_val is not None and t_val is not None:
        if p_val == t_val:
            return Resolution(p_val, False)
        return Resolution(None, True)
    if p_val is not None:
        return Resolution(p_val, False)
    if t_val is not None:
        return Resolution(t_val, False)
    return Resolution(None, False)


def resolve(env: dict[str, str]) -> tuple[dict[str, str], list[str], list[str]]:
    """Resolve every logical var in the schema against a parsed env dict.

    Pure function. Returns a 3-tuple:

    * ``resolved`` — mapping of canonical ``TF_VAR_*`` key -> resolved value, for
      every logical var that resolved to a non-empty value. The optional
      datastore, when absent, is emitted as the :data:`AUTODETECT` sentinel so
      the orchestrator knows to auto-detect (Req 2.6, 3.5). The optional
      SSH-public-key-file path, when absent, is simply NOT emitted (no sentinel):
      the playbook reads its absence as "no key -> pass an empty ``[]`` list ->
      strict no-op" (fix-cluster-env-endpoint-path, member (c)).
    * ``missing`` — list of logical names of MANDATORY vars that were absent/empty
      (Req 2.5). Order follows the schema, but callers should treat it as a set.
    * ``conflicts`` — list of ``"PROXMOX_X vs TF_VAR_y"`` strings for each logical
      var whose two aliases held conflicting non-empty values (Req 2.8).

    This function does not raise; :func:`validate` turns ``missing``/``conflicts``
    into a single :class:`ValidationError`. Separating detection from raising
    keeps the property tests able to assert on the exact missing/conflict sets.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    conflicts: list[str] = []

    for spec in SCHEMA:
        res = resolve_one(env, spec)
        if res.conflict:
            conflicts.append(f"{spec.proxmox_alias} vs {spec.tfvar_alias}")
            # A conflicting mandatory var is unresolved; report it as a conflict,
            # not additionally as "missing", to avoid a doubled complaint.
            continue
        if res.value is not None:
            resolved[spec.tfvar_alias] = res.value
        elif spec.mandatory:
            missing.append(spec.logical)
        elif spec is _DATASTORE_SPEC:
            # Optional datastore absent -> sentinel triggers auto-detect.
            resolved[spec.tfvar_alias] = AUTODETECT

    return resolved, missing, conflicts


# Convenience reference to the optional datastore row for the auto-detect branch.
# Located by logical name (NOT by position) so adding further optional rows —
# e.g. the SSH-public-key-file row — cannot silently repoint this at the wrong
# spec. Only the datastore gets the AUTODETECT sentinel when absent; every other
# absent optional var is simply not emitted.
_DATASTORE_SPEC = next(v for v in SCHEMA if v.logical == "Datastore")
assert not _DATASTORE_SPEC.mandatory and _DATASTORE_SPEC.logical == "Datastore"


def validate(env: dict[str, str]) -> dict[str, str]:
    """Validate a parsed env dict and return the resolved canonical mapping.

    Raises a single :class:`ValidationError` collecting ALL missing mandatory
    vars and ALL conflicting alias pairs (Req 2.5, 2.8) — never one problem at a
    time. The message is built from order-independent sorted sets so it is stable
    regardless of dict insertion order.

    On success returns the ``resolved`` mapping (canonical ``TF_VAR_*`` keys),
    with the datastore either configured or set to the :data:`AUTODETECT`
    sentinel.
    """
    resolved, missing, conflicts = resolve(env)

    if missing or conflicts:
        parts: list[str] = []
        if missing:
            names = ", ".join(sorted(missing))
            parts.append(f"missing or empty mandatory variable(s): {names}")
        if conflicts:
            pairs = ", ".join(sorted(conflicts))
            parts.append(f"conflicting alias value(s): {pairs}")
        raise ValidationError("; ".join(parts))

    return resolved


def load_env_file(path: str) -> dict[str, str]:
    """Read and parse a Cluster_Env_File from disk.

    Raises :class:`EnvFileNotFoundError` (distinct type, Req 2.3) naming the path
    and directing the operator to copy ``cluster.env.example`` when the file is
    absent or unreadable. This is the only file-IO seam; the parsing itself is
    the pure :func:`parse_env`.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise EnvFileNotFoundError(
            f"Cluster env file not found: {path}. "
            f"Copy cluster.env.example to {path} and fill in the required values."
        ) from None
    except OSError as exc:
        raise EnvFileNotFoundError(
            f"Cluster env file not readable: {path} ({exc.strerror}). "
            f"Copy cluster.env.example to {path} and fill in the required values."
        ) from None
    return parse_env(text)


def resolve_env_file(path: str) -> dict[str, str]:
    """End-to-end: load ``path``, parse, resolve, and validate.

    Returns the canonical ``TF_VAR_*`` mapping. Raises
    :class:`EnvFileNotFoundError` if the file is absent/unreadable, or
    :class:`ValidationError` if mandatory vars are missing or aliases conflict.
    No interactive prompt (Req 1.3, 2.7).
    """
    env = load_env_file(path)
    return validate(env)


def main(argv: list[str] | None = None) -> int:
    """Thin CLI wrapper. Emits resolved TF_VAR_* mapping as JSON on stdout.

    Exit codes:
    * 0 — resolved and validated successfully (JSON mapping emitted unless
      ``--check``).
    * 2 — file absent/unreadable (Req 2.3).
    * 3 — validation failure: missing mandatory vars and/or alias conflicts.

    Errors go to stderr; the resolved mapping (which carries the API-token value
    by its canonical key) is emitted to stdout only, so a caller can capture it
    without it landing in a shared human-facing log.
    """
    parser = argparse.ArgumentParser(
        description="Parse, alias-resolve, and validate an SVC-07 cluster env file."
    )
    parser.add_argument(
        "--env-file",
        required=True,
        help="Path to the Cluster_Env_File (e.g. cluster.env, cluster.dev.env).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate only; do not emit resolved values to stdout.",
    )
    args = parser.parse_args(argv)

    try:
        resolved = resolve_env_file(args.env_file)
    except EnvFileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"cluster env validation failed: {exc}", file=sys.stderr)
        return 3

    if not args.check:
        json.dump(resolved, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
