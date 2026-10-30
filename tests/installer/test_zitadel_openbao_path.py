"""Property + unit tests for the per-app OpenBao KV path derivation (SVC-06).

Spec: zitadel-spa-pkce-onboarding, Task 6.6 (design Property 7, R5.1). The
``zitadel-org-provision`` role writes each newly-created client secret to a KV
path unique per application within a project. That derivation is factored into
the pure ``zitadel_openbao_path.derive_app_secret_path`` helper so the
stability / collision-free invariant is unit-testable without a live Ansible
run or a live Proxmox cluster.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_zitadel_openbao_path.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# conftest.py adds scripts/installer to sys.path; keep a defensive insert so the
# module imports regardless of invocation cwd (mirrors the sibling test files).
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "installer"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import zitadel_openbao_path as zop_path  # noqa: E402


# Slugs and app names: non-empty strings excluding the path separator and the
# format brace characters, so the derived path has one unambiguous app-name
# segment (mirrors real ZITADEL app names / project slugs).
_SAFE = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=40,
)


# --------------------------------------------------------------------------- #
# Property 7 — per-app OpenBao paths are stable and collision-free.
# --------------------------------------------------------------------------- #
# Feature: zitadel-spa-pkce-onboarding, Property 7: Per-app OpenBao paths are
# stable and collision-free
@settings(max_examples=200)
@given(slug=_SAFE, name_a=_SAFE, name_b=_SAFE)
def test_property7_paths_stable_and_collision_free(slug, name_a, name_b):
    """Validates: Requirements 5.1

    For a project slug and two app names: the derived path is stable for a
    repeated derivation of the same name, and two DISTINCT app names within one
    project (same slug + mount) never resolve to the same path.
    """
    # Stable: the same (slug, app_name) always yields the same path.
    assert zop_path.derive_app_secret_path(slug, name_a) == \
        zop_path.derive_app_secret_path(slug, name_a)

    # Collision-free: distinct app names in the same project -> distinct paths.
    if name_a != name_b:
        assert zop_path.derive_app_secret_path(slug, name_a) != \
            zop_path.derive_app_secret_path(slug, name_b)


# --------------------------------------------------------------------------- #
# Unit tests — concrete path shape + mount override + role-template parity.
# --------------------------------------------------------------------------- #
def test_derived_path_matches_documented_shape():
    """The path equals <mount>/proj-<slug>/zitadel/oidc/<app_name> (design §6)."""
    assert zop_path.derive_app_secret_path("dronefleet", "dronefleet-spa") == \
        "secret/proj-dronefleet/zitadel/oidc/dronefleet-spa"


def test_mount_override_is_honored():
    assert zop_path.derive_app_secret_path("acme", "acme-api", mount="kv2") == \
        "kv2/proj-acme/zitadel/oidc/acme-api"


def test_two_apps_same_project_differ_concrete():
    """Two apps in one project resolve to distinct paths (the R5.1 invariant)."""
    spa = zop_path.derive_app_secret_path("acme", "acme-spa")
    api = zop_path.derive_app_secret_path("acme", "acme-api")
    assert spa != api
    assert spa.endswith("/acme-spa")
    assert api.endswith("/acme-api")
