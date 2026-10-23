"""Offline drift-guard for the SVC-06 installer Terraform backend swap-gate.

Slice-3 Task 17 (spec: svc-06-identity-provider). Pure-text drift guard on
ansible/roles/svc06_provision/tasks/main.yml: the GitLab-vs-local swap gate
requires all three CI keys with AND semantics, logs the INFO decision, manages
the local override on both branches, and — the SVC-06-specific points — uses the
FIXED state name svc-06-identity-provider (ZITADEL is a single shared service,
not reusable-per-project like SVC-01), AND stands up the backing DB via a CHILD
svc-01-bootstrap run BEFORE the ZITADEL apply (design.md §3.3).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc06_swap_gate.py -v
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION = _REPO_ROOT / "ansible" / "roles" / "svc06_provision" / "tasks" / "main.yml"


def _text() -> str:
    assert _PROVISION.is_file(), f"expected svc06_provision tasks at {_PROVISION}"
    return _PROVISION.read_text(encoding="utf-8")


def test_swap_gate_requires_all_three_ci_keys():
    t = _text()
    for key in ("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"):
        assert key in t, f"{key} missing from the svc06_provision swap gate"
    assert "svc06_use_gitlab_backend" in t, "the swap gate must set svc06_use_gitlab_backend"
    assert "and ('CI_PROJECT_ID' in svc06_env_keys)" in t, "swap gate must AND the three CI key checks"


def test_swap_gate_logs_info_decision():
    t = _text()
    assert "INFO: Terraform backend:" in t, "swap gate must emit an INFO backend-selection log"
    assert "LOCAL (local_backend_override.tf)" in t, "INFO log must name the LOCAL branch"
    assert "GitLab HTTP state" in t, "INFO log must name the GitLab branch"


def test_fixed_state_name_svc06():
    """SVC-06 is a single shared service (NOT reusable-per-project), so the state
    name is the fixed svc-06-identity-provider — distinct from the backing DB's
    SVC-01 state shared-zitadel-svc01-postgres."""
    t = _text()
    assert "terraform/state/svc-06-identity-provider" in t, (
        "the GitLab state address must use the fixed state name svc-06-identity-provider"
    )


def test_local_override_managed_both_branches():
    t = _text()
    assert "local_backend_override.tf" in t and "state: absent" in t, (
        "the swap gate must ensure the local override present (local) / absent (GitLab)"
    )


def test_backing_db_child_bootstrap_runs_before_zitadel_apply():
    """SVC-06-specific: the backing DB is a child svc-01-bootstrap run
    (project_slug=shared-zitadel) that MUST precede the ZITADEL terraform apply
    (design.md §3.3 — the DB exists + its creds are in OpenBao before ZITADEL
    starts)."""
    t = _text()
    db_idx = t.find("svc-01-bootstrap.yml")
    apply_idx = t.find("zitadel_vmid")
    assert db_idx != -1, "svc06_provision must run a child svc-01-bootstrap for the backing DB"
    assert apply_idx != -1, "svc06_provision must terraform-apply the ZITADEL root"
    assert db_idx < apply_idx, (
        "the backing-DB child svc-01-bootstrap must come BEFORE the ZITADEL terraform apply (design §3.3)"
    )
    assert "svc06_db_project_slug" in t, "the child DB bootstrap must use the shared-zitadel slug param"
