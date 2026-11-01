"""Unit tests for the optional ``workload`` block in the Consumer_Manifest.

Feature: consumer-last-mile-hello-workload (tasks.md Task 1.1; design.md
"Components and interfaces" → Component 1 (``workload`` manifest block);
Requirements 2.1, 5.4).

TDD note: these tests are written FIRST, against the as-yet-unfixed validator
in ``scripts/installer/consumer_manifest.py``. The ACCEPT cases for a
``workload`` block are EXPECTED to fail against the unfixed validator — the
validator's current ``_ALLOWED_TOP_LEVEL_KEYS`` does not include ``workload``,
so a manifest declaring one is rejected as an unknown top-level service entry
(Requirement 1.7 message naming ``workload``). That failure confirms the schema
gap this feature closes; Task 1.2 adds ``WORKLOAD_TIERS`` + ``_validate_workload``
+ grows the allowlist, after which every case here passes.

The schema (design Component 1):

    workload:
      enabled: true          # bool; default false. false/absent = no guest.
      tier: small            # small | medium (reuse the DATABASE_TIERS bands)
      count: 1               # int >= 1; default 1.

Validation mirrors ``_validate_database``: ``enabled`` a bool, ``tier`` an enum
member, ``count`` a positive int; first-offender naming; additive + backward
compatible (an existing manifest with no ``workload`` block validates and
behaves identically — Requirement 5.4).

All offline: the module under test is pure, stdlib-only, Ansible-free, so these
run under the repo's default offline pytest invocation (``-m "not
requires_infra"``). Run scoped to ``tests/installer`` only (separate pytest
root; combining with other roots collides on same-named ``conftest.py``)::

    ~/venv/devinfra/bin/pytest tests/installer/test_consumer_manifest_workload.py
"""

from __future__ import annotations

import pytest

from consumer_manifest import (
    ManifestValidationError,
    validate_manifest,
)


# --- Accept cases (EXPECTED TO FAIL against the unfixed validator) --------
#
# These declare a `workload` top-level block. The unfixed validator rejects
# `workload` as an unknown top-level service entry, so each of these raises
# ManifestValidationError today. After Task 1.2 they pass.


def test_full_workload_block_accepts():
    manifest = {
        "slug": "dronefleet",
        "workload": {"enabled": True, "tier": "small", "count": 1},
    }
    assert validate_manifest(manifest) is manifest


def test_workload_enabled_false_accepts():
    # enabled: false is explicitly accepted (backward-compat / opt-out).
    manifest = {"slug": "dronefleet", "workload": {"enabled": False}}
    assert validate_manifest(manifest) is manifest


def test_workload_block_defaults_omitted_accepts():
    # tier/count/enabled are optional with documented defaults; an empty
    # workload block is well-formed.
    manifest = {"slug": "dronefleet", "workload": {}}
    assert validate_manifest(manifest) is manifest


def test_workload_medium_tier_accepts():
    manifest = {
        "slug": "mlvideo",
        "workload": {"enabled": True, "tier": "medium", "count": 2},
    }
    assert validate_manifest(manifest) is manifest


def test_workload_alongside_other_service_blocks_accepts():
    # workload coexists with database/sso/secrets.
    manifest = {
        "slug": "dronefleet",
        "database": {"db_name": "dronefleet", "db_role": "dronefleet_app"},
        "workload": {"enabled": True, "tier": "small", "count": 1},
    }
    assert validate_manifest(manifest) is manifest


# --- Backward-compat: a manifest with NO workload block still validates ---
#
# This is NOT a new behavior; it asserts the additive change does not regress
# the no-block case (Requirement 5.4 / design Property 3). It passes both
# before and after the fix.


def test_manifest_without_workload_block_still_validates():
    manifest = {"slug": "edge-sensors"}
    assert validate_manifest(manifest) is manifest


# --- Reject cases: workload field validation (first-offender naming) ------
#
# Once the allowlist permits `workload`, these assert each malformed field is
# rejected naming the offending field. Against the unfixed validator they also
# raise ManifestValidationError (for the "unknown workload key" reason), but the
# message would not name the specific field — the assertions on the field name
# are what distinguishes the fixed behavior.


def test_workload_bad_tier_rejects_naming_field():
    manifest = {
        "slug": "p",
        "workload": {"enabled": True, "tier": "xlarge", "count": 1},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "workload.tier" in str(exc.value)


def test_workload_non_positive_count_rejects_naming_field():
    manifest = {
        "slug": "p",
        "workload": {"enabled": True, "tier": "small", "count": 0},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "workload.count" in str(exc.value)


def test_workload_negative_count_rejects_naming_field():
    manifest = {
        "slug": "p",
        "workload": {"enabled": True, "tier": "small", "count": -3},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "workload.count" in str(exc.value)


def test_workload_non_bool_enabled_rejects_naming_field():
    manifest = {
        "slug": "p",
        "workload": {"enabled": "yes", "tier": "small", "count": 1},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "workload.enabled" in str(exc.value)


def test_workload_non_mapping_rejects_naming_workload():
    manifest = {"slug": "p", "workload": ["enabled", True]}
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "workload" in str(exc.value)


# --- Allowlist: workload permitted, unknown/duplicate still rejected ------


def test_unknown_top_level_key_still_rejects_naming_it():
    # The allowlist grows by exactly `workload`; an unrelated unknown key is
    # still rejected naming the offender (Requirement 2.1 / 1.7).
    manifest = {
        "slug": "p",
        "workload": {"enabled": True},
        "cache": {"engine": "redis"},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "cache" in str(exc.value)
