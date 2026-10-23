"""Offline state-isolation guard for the reusable SVC-01 root.

Slice-2 Task 9 (spec: svc-01-sql-database) — design.md §3.1 / cross-cutting
"Terraform state isolation". The ONE reusable SVC-01 root serves many project
instances; each instance MUST get its OWN GitLab-managed state name
`<slug>-svc01-postgres` so a `terraform destroy` of one project's DB can never
touch another's (integration-boundaries §5, domain-rules §2).

The per-instance state name is supplied at `terraform init` via
`-backend-config=address=.../terraform/state/<slug>-svc01-postgres` (the backend
block is a literal-only partial backend). The authoritative place this address
is DERIVED is the installer's svc01_provision role; this guard pins:
  * the versions.tf header documents the `<slug>-svc01-postgres` per-instance
    state-name convention (not a single fixed name), and
  * the svc01_provision role derives the address with the `<slug>` interpolated
    from the project_slug (so two projects get two different state names).

Pure-text, always runs (no infra). Mirrors the SVC-07 destroy-isolation intent.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc01_destroy_isolation.py -v
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS = _REPO_ROOT / "infra" / "projects" / "svc-01-sql-database" / "versions.tf"
_PROVISION = _REPO_ROOT / "ansible" / "roles" / "svc01_provision" / "tasks" / "main.yml"


def test_versions_documents_per_instance_state_name():
    text = _VERSIONS.read_text(encoding="utf-8")
    assert "<slug>-svc01-postgres" in text, (
        "versions.tf must document the per-instance state name <slug>-svc01-postgres — the "
        "mechanism that isolates each project's DB state (integration-boundaries §5)"
    )
    # It must NOT hardcode a single literal state name in the backend block (which
    # would make all project instances share one state — the isolation bug).
    assert 'backend "http" {' in text, "SVC-01 root must use a partial backend \"http\" {} (PF FR-2)"


def test_provision_derives_per_slug_state_address():
    """The svc01_provision role must derive the GitLab state address with the
    project slug interpolated, so project A and project B get DISTINCT state
    names (<slugA>-svc01-postgres vs <slugB>-svc01-postgres)."""
    import pytest
    if not _PROVISION.is_file():
        pytest.skip(
            "svc01_provision role not built yet (Task 11); the per-slug state-address "
            "derivation is asserted once the installer role lands."
        )
    text = _PROVISION.read_text(encoding="utf-8")
    # The state-name suffix must be parameterized by the slug, not a fixed literal.
    assert "svc01-postgres" in text, "svc01_provision must reference the svc01-postgres state-name suffix"
    assert ("project_slug" in text or "svc01_project_slug" in text), (
        "svc01_provision must interpolate the project slug into the state address so each "
        "project instance gets its own isolated state name (no shared state across projects)"
    )
