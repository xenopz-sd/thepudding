"""Unit tests for the Consumer_Manifest schema validator.

Feature: project-consumer-onboarding (tasks.md Task 2.2; design.md "Data Models"
schema-field table; Requirements 1.2-1.7).

Concrete accept/reject examples: a full database+sso+secrets manifest accepts;
each enumerated invalid-field example rejects naming the field; an unknown /
duplicate top-level key rejects naming it; a valid app_type/auth_method combo
accepts and an invalid one rejects.

All offline: the module under test (``scripts/installer/consumer_manifest.py``)
is pure, stdlib-only, Ansible-free, so these run under the repo's default offline
pytest invocation (``-m "not requires_infra"``).
"""

from __future__ import annotations

import textwrap

import pytest

from consumer_manifest import (
    ManifestValidationError,
    _load_yaml_detecting_duplicates,
    validate_manifest,
)


def _full_manifest() -> dict:
    """A complete, well-formed manifest declaring all three service blocks."""
    return {
        "slug": "dronefleet",
        "database": {
            "db_name": "dronefleet",
            "db_role": "dronefleet_app",
            "engine": "postgres",
            "tier": "small",
        },
        "sso": {
            "org_name": "proj-dronefleet",
            "app_registrations": [
                {
                    "name": "dronefleet-spa",
                    "app_type": "public-spa",
                    "auth_method": "none",
                },
                {
                    "name": "dronefleet-api",
                    "app_type": "api",
                    "auth_method": "jwt-profile",
                },
            ],
        },
        "secrets": {
            "policy": "policy-dronefleet",
            "path": "secret/data/dronefleet",
            "workload_token_ttl": 3600,
            "workload_token_max_ttl": 14400,
            "secret_id_ttl": 86400,
            "secret_id_num_uses": 0,
        },
    }


# --- Accept cases --------------------------------------------------------


def test_full_manifest_accepts():
    manifest = _full_manifest()
    assert validate_manifest(manifest) is manifest


def test_slug_only_manifest_accepts():
    # All service blocks optional; a project may consume only a subset.
    manifest = {"slug": "mlvideo"}
    assert validate_manifest(manifest) is manifest


def test_single_char_slug_accepts():
    assert validate_manifest({"slug": "a"})["slug"] == "a"


def test_twenty_char_slug_accepts():
    slug = "a" + ("b" * 18) + "c"  # 20 chars exactly
    assert len(slug) == 20
    assert validate_manifest({"slug": slug})["slug"] == slug


def test_database_defaults_omitted_accepts():
    # engine/tier are optional with documented defaults.
    manifest = {
        "slug": "edge-sensors",
        "database": {"db_name": "edge", "db_role": "edge_app"},
    }
    assert validate_manifest(manifest) is manifest


@pytest.mark.parametrize("app_type", ["public-spa", "confidential-web", "api"])
@pytest.mark.parametrize("auth_method", ["none", "basic", "jwt-profile"])
def test_valid_app_type_auth_method_combo_accepts(app_type, auth_method):
    manifest = {
        "slug": "proj",
        "sso": {
            "org_name": "proj-org",
            "app_registrations": [
                {"name": "app", "app_type": app_type, "auth_method": auth_method}
            ],
        },
    }
    assert validate_manifest(manifest) is manifest


# --- Reject cases: slug (Req 1.5) ----------------------------------------


@pytest.mark.parametrize(
    "bad_slug",
    [
        "Dronefleet",            # uppercase
        "drone_fleet",           # underscore
        "-drone",                # leading hyphen
        "drone-",                # trailing hyphen
        "a" * 21,                # too long (>20)
        "",                      # empty
    ],
)
def test_bad_slug_rejects_naming_slug(bad_slug):
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest({"slug": bad_slug})
    assert "slug" in str(exc.value)


def test_missing_slug_rejects():
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest({"database": {"db_name": "x", "db_role": "y"}})
    assert "slug" in str(exc.value)


# --- Reject cases: database (Req 1.2) ------------------------------------


def test_database_missing_db_name_rejects_naming_field():
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest({"slug": "p", "database": {"db_role": "r"}})
    assert "database.db_name" in str(exc.value)


def test_database_bad_engine_rejects_naming_field():
    manifest = {
        "slug": "p",
        "database": {"db_name": "d", "db_role": "r", "engine": "oracle"},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "database.engine" in str(exc.value)


def test_database_bad_tier_rejects_naming_field():
    manifest = {
        "slug": "p",
        "database": {"db_name": "d", "db_role": "r", "tier": "xlarge"},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "database.tier" in str(exc.value)


# --- Reject cases: sso (Req 1.3) -----------------------------------------


def test_sso_missing_org_name_rejects():
    manifest = {
        "slug": "p",
        "sso": {"app_registrations": [{"name": "a", "app_type": "api", "auth_method": "none"}]},
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "sso.org_name" in str(exc.value)


def test_sso_empty_app_registrations_rejects():
    manifest = {"slug": "p", "sso": {"org_name": "o", "app_registrations": []}}
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "sso.app_registrations" in str(exc.value)


def test_sso_bad_app_type_rejects_naming_field():
    manifest = {
        "slug": "p",
        "sso": {
            "org_name": "o",
            "app_registrations": [
                {"name": "a", "app_type": "native-mobile", "auth_method": "none"}
            ],
        },
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "app_type" in str(exc.value)


def test_sso_bad_auth_method_rejects_naming_field():
    manifest = {
        "slug": "p",
        "sso": {
            "org_name": "o",
            "app_registrations": [
                {"name": "a", "app_type": "api", "auth_method": "mtls"}
            ],
        },
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "auth_method" in str(exc.value)


# --- Reject cases: secrets (Req 1.4) -------------------------------------


def test_secrets_path_without_prefix_rejects():
    manifest = {
        "slug": "p",
        "secrets": {
            "policy": "policy-p",
            "path": "kv/data/p",
            "workload_token_ttl": 10,
            "workload_token_max_ttl": 20,
            "secret_id_ttl": 30,
            "secret_id_num_uses": 0,
        },
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "secrets.path" in str(exc.value)


def test_secrets_ttl_ordering_violation_rejects():
    manifest = {
        "slug": "p",
        "secrets": {
            "policy": "policy-p",
            "path": "secret/data/p",
            "workload_token_ttl": 20,
            "workload_token_max_ttl": 10,  # ttl > max_ttl
            "secret_id_ttl": 30,
            "secret_id_num_uses": 0,
        },
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "workload_token_ttl" in str(exc.value)


def test_secrets_non_positive_ttl_rejects():
    manifest = {
        "slug": "p",
        "secrets": {
            "policy": "policy-p",
            "path": "secret/data/p",
            "workload_token_ttl": 0,
            "workload_token_max_ttl": 10,
            "secret_id_ttl": 30,
            "secret_id_num_uses": 0,
        },
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "workload_token_ttl" in str(exc.value)


def test_secrets_negative_num_uses_rejects():
    manifest = {
        "slug": "p",
        "secrets": {
            "policy": "policy-p",
            "path": "secret/data/p",
            "workload_token_ttl": 10,
            "workload_token_max_ttl": 20,
            "secret_id_ttl": 30,
            "secret_id_num_uses": -1,
        },
    }
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "secret_id_num_uses" in str(exc.value)


def test_secrets_zero_num_uses_accepts():
    # 0 = unlimited uses within TTL; accepted.
    manifest = {
        "slug": "p",
        "secrets": {
            "policy": "policy-p",
            "path": "secret/data/p",
            "workload_token_ttl": 10,
            "workload_token_max_ttl": 20,
            "secret_id_ttl": 30,
            "secret_id_num_uses": 0,
        },
    }
    assert validate_manifest(manifest) is manifest


# --- Reject cases: top-level key set (Req 1.7) ---------------------------


def test_unknown_top_level_key_rejects_naming_it():
    manifest = {"slug": "p", "cache": {"engine": "redis"}}
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "cache" in str(exc.value)


def test_duplicate_top_level_key_detected_via_loader():
    text = textwrap.dedent(
        """\
        slug: p
        database:
          db_name: d
          db_role: r
        database:
          db_name: e
          db_role: s
        """
    )
    manifest = _load_yaml_detecting_duplicates(text)
    assert manifest.get("_duplicate_top_level_keys") == {"database"}
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(manifest)
    assert "database" in str(exc.value)
    assert "duplicate" in str(exc.value).lower()


def test_non_mapping_manifest_rejects():
    with pytest.raises(ManifestValidationError):
        validate_manifest(["slug", "p"])
