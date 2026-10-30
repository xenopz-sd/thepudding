#!/usr/bin/env python3
"""Pure Consumer_Manifest schema validator for the project-consumer-onboarding feature.

Feature: project-consumer-onboarding (design.md "Data Models" → Consumer_Manifest
schema + schema-field table; "Correctness Properties" → Property 1; tasks.md
Task 2.1).

This module is the pure, input-varying, side-effect-free validation surface that
the Consumer_Onboarding_Orchestrator's Phase-1 ``consumer_preflight`` role invokes
to accept or reject a per-project manifest BEFORE any mutation (Requirement 1.6,
1.7; the fail-closed preflight gate). It is deliberately a standalone,
**Ansible-free, stdlib-only** module so it can be unit- and property-tested
offline, independent of Ansible and a live cluster.

It validates an already-parsed manifest mapping against the design's schema-field
table:

- ``slug``  (REQUIRED) — ``^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$`` (lowercase,
  hyphenated, <=20 chars, no leading/trailing/doubled-edge hyphen; Requirement 1.5).
- ``database`` (OPTIONAL) — ``db_name`` / ``db_role`` non-empty identifier-safe
  strings; ``engine`` ∈ {postgres, mariadb}; ``tier`` ∈ {small, medium}
  (Requirement 1.2).
- ``sso`` (OPTIONAL) — ``org_name`` non-empty; ``app_registrations`` a list whose
  each entry's ``app_type`` ∈ {public-spa, confidential-web, api} and
  ``auth_method`` ∈ {none, basic, jwt-profile} (Requirement 1.3).
- ``secrets`` (OPTIONAL) — ``policy`` / ``path`` non-empty (``path`` begins
  ``secret/``); positive integer TTLs with
  ``workload_token_ttl <= workload_token_max_ttl``; ``secret_id_num_uses`` >= 0
  (Requirement 1.4).
- Top-level keys are a duplicate-free subset of
  ``{slug, database, sso, secrets}`` — an unknown or duplicate top-level service
  key is rejected naming the offender (Requirement 1.7).

**Fail-closed, first-offender naming.** On any violation :func:`validate_manifest`
raises :class:`ManifestValidationError` whose message names the FIRST offending
field (Requirement 1.6) or the offending / duplicate / unknown service entry
(Requirement 1.7). Validation is ordered deterministically (top-level key set →
slug → database → sso → secrets, each field in table order) so "the first
offender" is well-defined and stable across runs — the invariant Property 1
asserts.

Per ``testing-strategy.md`` "Verification Scope", any change to the signatures,
return types, or raised exceptions below is a shared-symbol change: it requires a
repo-wide caller search and the FULL offline suite, not just this feature's tests.

Only the standard library is used for validation. The ``--check`` CLI mode parses
a YAML file with PyYAML (already a project dependency, exactly as
``onboard_project.load_registry`` does) — the ONLY place this module touches the
filesystem; the core validator takes an already-parsed mapping and touches no
file, network, or shared state.

Invoke the offline ``--check`` doctest surface via the venv binary by absolute
path, e.g.::

    ~/venv/devinfra/bin/python scripts/installer/consumer_manifest.py \
      --check infra/projects/<slug>/consumer-manifest.yml
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

__all__ = [
    "ManifestValidationError",
    "CONSUMABLE_SERVICE_KEYS",
    "DATABASE_ENGINES",
    "DATABASE_TIERS",
    "APP_TYPES",
    "AUTH_METHODS",
    "SLUG_RE",
    "validate_manifest",
    "main",
]


class ManifestValidationError(ValueError):
    """Raised when a Consumer_Manifest fails schema validation.

    A subclass of :class:`ValueError` so callers may catch either. The message
    names the FIRST offending field (Requirement 1.6) or the offending /
    duplicate / unknown service entry (Requirement 1.7). The orchestrator's
    Phase-1 preflight surfaces the message via an ``assert``/``fail`` that runs
    BEFORE any mutation, leaving zero provisioning side effect.
    """


# --- Schema constants (design "Data Models" schema-field table) ----------

#: The slug convention: lowercase alphanumeric with internal single hyphens, no
#: leading/trailing hyphen, 1-20 characters total (Requirement 1.5). The design
#: states this regex verbatim; a single char is allowed, and the max length is
#: 20 (1 + up to 18 middle + 1).
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$")

#: The enumerated consumable SVC-xx service blocks (Requirement 1.1, 1.7). Any
#: other top-level service key is rejected. ``slug`` is the mandatory identifier
#: key and is tracked separately.
CONSUMABLE_SERVICE_KEYS = ("database", "sso", "secrets")

#: The full set of permitted top-level keys: the mandatory ``slug`` plus the
#: optional consumable service blocks.
_ALLOWED_TOP_LEVEL_KEYS = ("slug",) + CONSUMABLE_SERVICE_KEYS

#: ``database.engine`` enum (Requirement 1.2). ``postgres`` is the default.
DATABASE_ENGINES = ("postgres", "mariadb")

#: ``database.tier`` enum (Requirement 1.2). ``small`` is the default.
DATABASE_TIERS = ("small", "medium")

#: ``sso.app_registrations[].app_type`` enum (Requirement 1.3). These are the
#: manifest's own DESCRIPTIVE forms; they are NOT reused verbatim by the sibling
#: zitadel-spa-pkce-onboarding role, which accepts the short forms
#: ``[spa, web, api]``. The consumer composition boundary (consumer_bootstrap
#: STEP 4) MAPS these descriptive forms to the role's vocabulary
#: (public-spa->spa, confidential-web->web, api->api) at compose time; the enum
#: values here are unchanged.
APP_TYPES = ("public-spa", "confidential-web", "api")

#: ``sso.app_registrations[].auth_method`` enum (Requirement 1.3). Likewise
#: descriptive; MAPPED to the role's ``{none, basic, jwt}`` at the
#: consumer_bootstrap STEP 4 boundary (none->none, basic->basic, jwt-profile->jwt),
#: not reused verbatim.
AUTH_METHODS = ("none", "basic", "jwt-profile")


def _require_non_empty_str(value: object, field: str) -> None:
    """Reject a value that is not a non-empty string, naming ``field``."""
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(
            f"field {field!r} must be a non-empty string, got {value!r}"
        )


def _require_enum(value: object, allowed: tuple[str, ...], field: str) -> None:
    """Reject a value that is not one of ``allowed``, naming ``field``."""
    if value not in allowed:
        raise ManifestValidationError(
            f"field {field!r} must be one of {list(allowed)}, got {value!r}"
        )


def _require_positive_int(value: object, field: str) -> int:
    """Return ``value`` as an int iff a strictly-positive integer; else reject.

    ``bool`` is a subclass of ``int`` in Python; a ``True``/``False`` slipping in
    as a TTL is a caller error, not ``1``/``0``, so it is rejected explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(
            f"field {field!r} must be a positive integer, got {value!r}"
        )
    if value <= 0:
        raise ManifestValidationError(
            f"field {field!r} must be > 0, got {value}"
        )
    return value


def _require_nonneg_int(value: object, field: str) -> int:
    """Return ``value`` as an int iff a non-negative integer; else reject."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(
            f"field {field!r} must be a non-negative integer, got {value!r}"
        )
    if value < 0:
        raise ManifestValidationError(
            f"field {field!r} must be >= 0, got {value}"
        )
    return value


def _validate_database(block: object) -> None:
    """Validate the optional ``database`` block (Requirement 1.2)."""
    if not isinstance(block, dict):
        raise ManifestValidationError(
            f"service entry 'database' must be a mapping, got {block!r}"
        )
    _require_non_empty_str(block.get("db_name"), "database.db_name")
    _require_non_empty_str(block.get("db_role"), "database.db_role")
    # engine / tier are optional with documented defaults; validate only if present.
    if "engine" in block:
        _require_enum(block["engine"], DATABASE_ENGINES, "database.engine")
    if "tier" in block:
        _require_enum(block["tier"], DATABASE_TIERS, "database.tier")


def _validate_sso(block: object) -> None:
    """Validate the optional ``sso`` block (Requirement 1.3)."""
    if not isinstance(block, dict):
        raise ManifestValidationError(
            f"service entry 'sso' must be a mapping, got {block!r}"
        )
    _require_non_empty_str(block.get("org_name"), "sso.org_name")
    app_regs = block.get("app_registrations")
    if not isinstance(app_regs, list) or not app_regs:
        raise ManifestValidationError(
            "field 'sso.app_registrations' must be a non-empty list, got "
            f"{app_regs!r}"
        )
    for i, entry in enumerate(app_regs):
        base = f"sso.app_registrations[{i}]"
        if not isinstance(entry, dict):
            raise ManifestValidationError(
                f"field {base!r} must be a mapping, got {entry!r}"
            )
        _require_non_empty_str(entry.get("name"), f"{base}.name")
        _require_enum(entry.get("app_type"), APP_TYPES, f"{base}.app_type")
        # auth_method is required to be a valid enum member when present; the
        # design's table lists it per entry.
        _require_enum(
            entry.get("auth_method"), AUTH_METHODS, f"{base}.auth_method"
        )


def _validate_secrets(block: object) -> None:
    """Validate the optional ``secrets`` block (Requirement 1.4)."""
    if not isinstance(block, dict):
        raise ManifestValidationError(
            f"service entry 'secrets' must be a mapping, got {block!r}"
        )
    _require_non_empty_str(block.get("policy"), "secrets.policy")
    path = block.get("path")
    _require_non_empty_str(path, "secrets.path")
    if not path.startswith("secret/"):
        raise ManifestValidationError(
            "field 'secrets.path' must begin with 'secret/', got "
            f"{path!r}"
        )
    token_ttl = _require_positive_int(
        block.get("workload_token_ttl"), "secrets.workload_token_ttl"
    )
    token_max_ttl = _require_positive_int(
        block.get("workload_token_max_ttl"), "secrets.workload_token_max_ttl"
    )
    if token_ttl > token_max_ttl:
        raise ManifestValidationError(
            "field 'secrets.workload_token_ttl' "
            f"({token_ttl}) must be <= 'secrets.workload_token_max_ttl' "
            f"({token_max_ttl})"
        )
    _require_positive_int(block.get("secret_id_ttl"), "secrets.secret_id_ttl")
    _require_nonneg_int(
        block.get("secret_id_num_uses"), "secrets.secret_id_num_uses"
    )


def validate_manifest(manifest: object) -> dict[str, Any]:
    """Validate an already-parsed Consumer_Manifest mapping; return it on accept.

    Validation is ordered deterministically so the FIRST offender is
    well-defined (Property 1): (1) the top-level key set — unknown or duplicate
    service key → reject naming it (Requirement 1.7); (2) ``slug`` present and
    matching :data:`SLUG_RE` (Requirement 1.5); (3)-(5) each present optional
    block in ``database`` → ``sso`` → ``secrets`` order, each field in
    schema-table order (Requirements 1.2, 1.3, 1.4, 1.6).

    Note on duplicates: a YAML/JSON mapping cannot carry two identical keys
    (the parser keeps the last), so a *duplicate* top-level service entry is
    detected here from a pre-parse ``_duplicate_top_level_keys`` annotation when
    present (set by :func:`_load_yaml_detecting_duplicates`), and otherwise the
    unknown-key check covers the "offending service entry" half of
    Requirement 1.7.

    Args:
        manifest: The parsed manifest. Must be a mapping.

    Returns:
        The manifest mapping, unchanged, when it is well-formed.

    Raises:
        ManifestValidationError: Naming the first offending field or service
            entry (Requirements 1.6, 1.7).
    """
    if not isinstance(manifest, dict):
        raise ManifestValidationError(
            f"manifest must be a mapping, got {type(manifest).__name__}"
        )

    # (1) Duplicate top-level service entries, if the loader flagged any.
    dupes = manifest.get("_duplicate_top_level_keys")
    if dupes:
        offender = sorted(dupes)[0]
        raise ManifestValidationError(
            f"duplicate top-level service entry {offender!r} is not permitted; "
            "each service may be declared at most once (Requirement 1.7)."
        )

    # (1) Unknown top-level keys (reject naming the first offender, sorted for
    #     determinism). The internal duplicate annotation is ignored here.
    unknown = sorted(
        key
        for key in manifest
        if key not in _ALLOWED_TOP_LEVEL_KEYS
        and key != "_duplicate_top_level_keys"
    )
    if unknown:
        raise ManifestValidationError(
            f"unknown top-level service entry {unknown[0]!r} is not an "
            f"enumerated consumable service; permitted entries are "
            f"{list(_ALLOWED_TOP_LEVEL_KEYS)} (Requirement 1.7)."
        )

    # (2) slug — mandatory identifier.
    if "slug" not in manifest:
        raise ManifestValidationError(
            "field 'slug' is required (Requirement 1.5)."
        )
    slug = manifest["slug"]
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ManifestValidationError(
            f"field 'slug' {slug!r} must be lowercase-hyphenated and <=20 "
            f"characters, matching {SLUG_RE.pattern!r} (Requirement 1.5)."
        )

    # (3)-(5) optional service blocks, in a fixed order.
    if "database" in manifest:
        _validate_database(manifest["database"])
    if "sso" in manifest:
        _validate_sso(manifest["sso"])
    if "secrets" in manifest:
        _validate_secrets(manifest["secrets"])

    return manifest


# --- YAML load with duplicate-key detection (CLI only) -------------------


def _load_yaml_detecting_duplicates(text: str) -> dict[str, Any]:
    """Parse a manifest YAML document, flagging duplicate top-level keys.

    A plain ``yaml.safe_load`` silently keeps the last of two identical mapping
    keys, which would hide the Requirement 1.7 "duplicate service entry" case. A
    custom constructor records any top-level key that appears more than once
    under the ``_duplicate_top_level_keys`` annotation, which
    :func:`validate_manifest` then rejects naming the offender.

    This is used only by the ``--check`` CLI; the pure :func:`validate_manifest`
    never reads a file.
    """
    import yaml  # local import: PyYAML is only needed for the file CLI path.

    duplicates: set[str] = set()

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestValidationError(
            f"manifest is not valid YAML: {exc}"
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ManifestValidationError(
            f"manifest root must be a mapping, got {type(data).__name__}"
        )

    # Root-duplicate scan: re-parse to the node graph and inspect the top-level
    # mapping's key nodes, since safe_load silently keeps only the last of two
    # identical keys (which would hide the Requirement 1.7 duplicate case).
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:  # pragma: no cover - exercised via CLI errors
        raise ManifestValidationError(
            f"manifest is not valid YAML: {exc}"
        ) from exc
    if isinstance(root, yaml.MappingNode):
        seen_keys: set[str] = set()
        for key_node, _ in root.value:
            if isinstance(key_node, yaml.ScalarNode):
                k = key_node.value
                if k in seen_keys:
                    duplicates.add(k)
                seen_keys.add(k)

    if duplicates:
        data["_duplicate_top_level_keys"] = duplicates
    return data


# --- CLI (offline doctest surface, Requirement 1.6/1.7 preflight) --------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI parser for the ``--check`` offline doctest mode."""
    parser = argparse.ArgumentParser(
        prog="consumer_manifest.py",
        description=(
            "Validate a per-project Consumer_Manifest against the "
            "project-consumer-onboarding schema. Offline, side-effect-free: "
            "reads the file and reports accept/reject; never mutates "
            "infrastructure. Exit 0 = valid, 1 = invalid, 2 = usage/IO error."
        ),
    )
    parser.add_argument(
        "--check",
        metavar="PATH",
        required=True,
        help="Path to the consumer-manifest.yml to validate.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code.

    Exit 0 when the manifest is valid; 1 when it fails schema validation (the
    diagnostic names the first offending field / service entry); 2 on a usage
    or file-IO error. Never mutates infrastructure.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        with open(args.check, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"ERROR: cannot read manifest {args.check!r}: {exc}", file=sys.stderr)
        return 2

    try:
        manifest = _load_yaml_detecting_duplicates(text)
        validate_manifest(manifest)
    except ManifestValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    print(f"VALID: {args.check} satisfies the Consumer_Manifest schema.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
