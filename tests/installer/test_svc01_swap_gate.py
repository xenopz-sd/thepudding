"""Offline drift-guard for the SVC-01 installer Terraform backend swap-gate.

Slice-2 Task 11 (spec: svc-01-sql-database). Pure-text drift guard on
ansible/roles/svc01_provision/tasks/main.yml: the GitLab-vs-local swap gate
requires all three CI keys with AND semantics, logs the INFO decision, and — the
SVC-01-specific point — derives a PER-INSTANCE state name <slug>-svc01-postgres
by interpolating the project slug (so each project's DB state is isolated).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc01_swap_gate.py -v
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION = _REPO_ROOT / "ansible" / "roles" / "svc01_provision" / "tasks" / "main.yml"


def _text() -> str:
    assert _PROVISION.is_file(), f"expected svc01_provision tasks at {_PROVISION}"
    return _PROVISION.read_text(encoding="utf-8")


def test_swap_gate_requires_all_three_ci_keys():
    t = _text()
    for key in ("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"):
        assert key in t, f"{key} missing from the svc01_provision swap gate"
    assert "svc01_use_gitlab_backend" in t, "the swap gate must set svc01_use_gitlab_backend"
    assert "and ('CI_PROJECT_ID' in svc01_env_keys)" in t, "swap gate must AND the three CI key checks"


def test_swap_gate_logs_info_decision():
    t = _text()
    assert "INFO: Terraform backend:" in t, "swap gate must emit an INFO backend-selection log"
    assert "LOCAL (local_backend_override.tf)" in t, "INFO log must name the LOCAL branch"
    assert "GitLab HTTP state" in t, "INFO log must name the GitLab branch"


def test_per_instance_state_name_from_slug():
    """The SVC-01-specific isolation point: the state name is <slug>-svc01-postgres,
    derived by interpolating project_slug — not a single fixed name."""
    t = _text()
    assert "svc01_state_name" in t, "svc01_provision must derive a per-instance svc01_state_name"
    assert "project_slug" in t and "svc01-postgres" in t, (
        "the state name must interpolate project_slug into <slug>-svc01-postgres (state isolation)"
    )
    assert "terraform/state/{{ svc01_state_name }}" in t, (
        "the GitLab state address must use the per-instance svc01_state_name"
    )


def test_local_override_managed_both_branches():
    t = _text()
    assert "local_backend_override.tf" in t and "state: absent" in t, (
        "the swap gate must ensure the local override present (local) / absent (GitLab)"
    )
