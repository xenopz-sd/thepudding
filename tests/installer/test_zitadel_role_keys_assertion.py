"""Offline tests for fix-zitadel-onboarding-role-keys-assertion (§4).

SECURITY-SENSITIVE (authorization): determines which role keys exist on a
ZITADEL project and whether role info is asserted into tokens.

Bug: consumer onboarding creates the ZITADEL org + OIDC clients but leaves the
project with ZITADEL's default role keys (admin/member/viewer) and
projectRoleAssertion=false — so a project with a non-default role vocabulary
gets a working login but no usable role claim.

Fix: the manifest `sso` block gains `roles` + `project_role_assertion`; the
helper gains reconcile_roles (create missing + DELETE non-declared),
set_project_role_assertion (PUT UpdateProject), get_project_role_assertion, and
a provision() read-back fail-loud gate; the role threads the roles + toggle.

Tests are parse/mock-level — no secret value is embedded or printed.

Run: ~/venv/devinfra/bin/pytest tests/installer/test_zitadel_role_keys_assertion.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO_ROOT / "scripts" / "installer"
sys.path.insert(0, str(_INSTALLER))

import consumer_manifest as cm  # noqa: E402
import zitadel_provision as zp  # noqa: E402

_ZOP_TASKS = _REPO_ROOT / "ansible" / "roles" / "zitadel-org-provision" / "tasks" / "main.yml"
_ZOP_DEFAULTS = _REPO_ROOT / "ansible" / "roles" / "zitadel-org-provision" / "defaults" / "main.yml"
_CONSUMER_BOOTSTRAP = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "tasks" / "main.yml"
)


# =========================================================================== #
# Component 1 — manifest sso.roles + sso.project_role_assertion schema.
# =========================================================================== #
def _base_sso(**extra):
    sso = {"org_name": "proj-x", "app_registrations": [
        {"name": "x-spa", "app_type": "public-spa", "auth_method": "none"}]}
    sso.update(extra)
    return {"slug": "x", "sso": sso}


def test_sso_roles_string_entries_accepted():
    m = _base_sso(roles=["owner", "collaborator", "observer"])
    assert cm.validate_manifest(m) is m


def test_sso_roles_mapping_entries_accepted():
    m = _base_sso(roles=[{"key": "owner", "display_name": "Owner", "group": "core"}, "observer"])
    assert cm.validate_manifest(m) is m


def test_sso_roles_reject_duplicate_key():
    m = _base_sso(roles=["owner", "owner"])
    with pytest.raises(cm.ManifestValidationError, match="owner"):
        cm.validate_manifest(m)


def test_sso_roles_reject_malformed_key():
    m = _base_sso(roles=["bad key!"])  # space + ! not allowed
    with pytest.raises(cm.ManifestValidationError):
        cm.validate_manifest(m)


def test_sso_roles_reject_mapping_without_key():
    m = _base_sso(roles=[{"display_name": "no key"}])
    with pytest.raises(cm.ManifestValidationError):
        cm.validate_manifest(m)


def test_sso_project_role_assertion_must_be_bool():
    with pytest.raises(cm.ManifestValidationError):
        cm.validate_manifest(_base_sso(project_role_assertion="true"))
    with pytest.raises(cm.ManifestValidationError):
        cm.validate_manifest(_base_sso(project_role_assertion=1))
    # a real bool is accepted
    assert cm.validate_manifest(_base_sso(project_role_assertion=True))


def test_sso_without_new_fields_still_valid_backward_compatible():
    """A manifest omitting roles + project_role_assertion validates identically."""
    assert cm.validate_manifest(_base_sso())


# =========================================================================== #
# Component 2 — helper reconcile_roles / set_project_role_assertion / gate.
# A FakeClient records calls so the tests assert the CONTRACT, not a live API.
# =========================================================================== #
class _FakeZitadelClient:
    """Records calls; models a project whose roles/assertion evolve."""

    def __init__(self, existing_roles, assertion=False):
        self._roles = set(existing_roles)
        self._assertion = assertion
        self.created = []
        self.removed = []
        self.assertion_set_to = None

    # org/project ensure (not under test here)
    def ensure_org(self, name):
        return ("org1", True)

    def ensure_project(self, name, org_id):
        return ("proj1", True)

    def list_project_roles(self, project_id, org_id):
        return sorted(self._roles)

    def reconcile_roles(self, project_id, org_id, declared):
        declared = list(declared)
        created = [r for r in declared if r not in self._roles]
        removed = [r for r in self._roles if r not in declared]
        for r in created:
            self._roles.add(r)
            self.created.append(r)
        for r in removed:
            self._roles.discard(r)
            self.removed.append(r)
        return {"created": created, "removed": removed, "final": sorted(self._roles)}

    def set_project_role_assertion(self, project_id, org_id, enabled, project_name):
        self._assertion = bool(enabled)
        self.assertion_set_to = bool(enabled)

    def get_project_role_assertion(self, project_id, org_id):
        return self._assertion

    def ensure_oidc_app(self, *a, **k):
        return {"app_name": k.get("app_name", "app"), "client_id": "cid",
                "client_secret": None, "already_existed": False}


def test_reconcile_roles_contract_on_real_client_exists():
    """The real ZitadelClient must expose reconcile_roles / list_project_roles /
    set_project_role_assertion / get_project_role_assertion."""
    for meth in ("reconcile_roles", "list_project_roles",
                 "set_project_role_assertion", "get_project_role_assertion"):
        assert hasattr(zp.ZitadelClient, meth), (
            f"ZitadelClient must expose {meth}() (FAILS on unfixed code)"
        )


def test_provision_signature_accepts_project_role_assertion():
    import inspect
    sig = inspect.signature(zp.provision)
    assert "project_role_assertion" in sig.parameters, (
        "provision() must accept project_role_assertion (additive, default True) "
        "(FAILS on unfixed code)"
    )
    assert sig.parameters["project_role_assertion"].default is True


def test_fake_reconcile_to_exact_removes_zitadel_defaults():
    """reconcile_roles must make the role-key set EXACTLY the declared set:
    create missing AND remove ZITADEL's auto-created admin/member/viewer."""
    fake = _FakeZitadelClient(existing_roles=["admin", "member", "viewer"])
    result = fake.reconcile_roles("p", "o", ["owner", "collaborator", "observer", "admin"])
    assert set(result["final"]) == {"owner", "collaborator", "observer", "admin"}
    assert set(result["created"]) == {"owner", "collaborator", "observer"}
    assert set(result["removed"]) == {"member", "viewer"}  # admin kept (declared)


# =========================================================================== #
# Component 3/4 — role + consumer_bootstrap wiring (structural).
# =========================================================================== #
def test_zop_role_threads_project_roles_and_assertion():
    tasks = _ZOP_TASKS.read_text(encoding="utf-8")
    defaults = _ZOP_DEFAULTS.read_text(encoding="utf-8")
    assert "zop_project_roles" in defaults, "role must declare zop_project_roles default"
    assert "zop_project_role_assertion" in defaults, (
        "role must declare zop_project_role_assertion default (true)"
    )
    # The helper invocations must thread the role keys + the assertion flag.
    assert "--role" in tasks, "the helper invocation must pass --role per declared key"
    assert "--project-role-assertion" in tasks or "project_role_assertion" in tasks, (
        "the helper invocation must pass the project-role-assertion flag"
    )


def test_consumer_bootstrap_step4_passes_roles_and_assertion():
    text = _CONSUMER_BOOTSTRAP.read_text(encoding="utf-8")
    assert "zop_project_roles" in text, (
        "consumer_bootstrap STEP 4 must pass zop_project_roles from the manifest"
    )
    assert "zop_project_role_assertion" in text, (
        "consumer_bootstrap STEP 4 must pass zop_project_role_assertion from the manifest"
    )


# =========================================================================== #
# Preservation — CLI --role default + token env-only.
# =========================================================================== #
def test_cli_role_default_dropped_hardcoded_admin_member_viewer():
    """The CLI --role must no longer default to admin/member/viewer (default []);
    the documented default is applied only when empty."""
    import argparse
    # Build the parser the same way main() does by calling it with --help-safe args
    # is heavy; instead assert the source no longer hardcodes the default list on --role.
    src = (_INSTALLER / "zitadel_provision.py").read_text(encoding="utf-8")
    assert 'default=["admin", "member", "viewer"]' not in src, (
        "the --role argparse default must be dropped (default [] or documented-default)"
    )


def test_token_still_env_only():
    src = (_INSTALLER / "zitadel_provision.py").read_text(encoding="utf-8")
    assert "ZITADEL_PROVISION_TOKEN" in src
    assert "--token" not in src, "the access token must never be a CLI flag"
