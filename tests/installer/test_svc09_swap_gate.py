"""Offline drift-guard for the SVC-09 installer Terraform backend swap-gate.

Slice-1 Task 5 (spec: svc-09-reverse-proxy). The SVC-09 provision role carries
the same composition-root-analogue swap gate as svc07_provision: GitLab HTTP
state backend when the cluster env configures all three CI state-backend keys
(CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN), else the local override, with an
INFO decision log (composition-wiring.md).

Pure-text drift guard (always runs, no infra): asserts the decision expression,
the three CI keys with AND semantics, the state name svc-09-reverse-proxy, and
the INFO-log markers are present in ansible/roles/svc09_provision/tasks/main.yml.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc09_swap_gate.py -v
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION = _REPO_ROOT / "ansible" / "roles" / "svc09_provision" / "tasks" / "main.yml"


def _provision_text() -> str:
    assert _PROVISION.is_file(), f"expected svc09_provision tasks at {_PROVISION}"
    return _PROVISION.read_text(encoding="utf-8")


def test_swap_gate_requires_all_three_ci_keys():
    text = _provision_text()
    for key in ("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"):
        assert key in text, f"{key} missing from the svc09_provision swap gate"
    # AND semantics: the decision fact ANDs the three key-presence checks.
    assert "svc09_use_gitlab_backend" in text, (
        "the swap gate must set svc09_use_gitlab_backend"
    )
    assert "and ('CI_PROJECT_ID' in svc09_env_keys)" in text, (
        "the swap gate must AND the three CI key-presence checks (all required)"
    )


def test_swap_gate_logs_info_decision_both_branches():
    text = _provision_text()
    assert "INFO: Terraform backend:" in text, (
        "the swap gate must emit an INFO backend-selection log line"
    )
    assert "LOCAL (local_backend_override.tf)" in text, (
        "the INFO log must name the LOCAL backend branch"
    )
    assert "GitLab HTTP state (svc-09-reverse-proxy)" in text, (
        "the INFO log must name the GitLab HTTP backend branch with the svc-09 state name"
    )


def test_swap_gate_uses_svc09_state_name():
    text = _provision_text()
    assert "terraform/state/svc-09-reverse-proxy" in text, (
        "the GitLab state address must use the svc-09-reverse-proxy state name (state isolation)"
    )


def test_local_override_present_in_local_branch_absent_in_gitlab_branch():
    text = _provision_text()
    # Local branch ensures the override present; GitLab branch removes it.
    assert "local_backend_override.tf" in text, (
        "the swap gate must manage local_backend_override.tf"
    )
    assert "state: absent" in text, (
        "the GitLab branch must remove local_backend_override.tf (state: absent)"
    )
