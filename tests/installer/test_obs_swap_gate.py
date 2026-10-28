"""Offline drift-guard for the SVC-17/18 installer Terraform backend swap-gate.

Task 9.6 (spec: svc-17-18-observability). The obs_provision role carries the
same composition-root-analogue swap gate as svc09_provision / svc07_provision:
GitLab HTTP state backend when the cluster env configures all three CI
state-backend keys (CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN), else the
local override (local_backend_override.tf), with an INFO decision log
(composition-wiring.md §"Swap-Gate Pattern" step 4).

Pure-text drift guard (always runs, no infra): asserts the decision expression,
the three CI keys with AND semantics, the state name svc-17-18-observability, and
the INFO-log markers are present in
ansible/roles/obs_provision/tasks/main.yml.

Clone of test_svc09_swap_gate.py adapted svc09_->obs_ and the state name to the
three-guest observability root (one shared state for all three guests).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_obs_swap_gate.py -v
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION = _REPO_ROOT / "ansible" / "roles" / "obs_provision" / "tasks" / "main.yml"


def _provision_text() -> str:
    assert _PROVISION.is_file(), f"expected obs_provision tasks at {_PROVISION}"
    return _PROVISION.read_text(encoding="utf-8")


def test_swap_gate_requires_all_three_ci_keys():
    text = _provision_text()
    for key in ("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"):
        assert key in text, f"{key} missing from the obs_provision swap gate"
    # AND semantics: the decision fact ANDs the three key-presence checks.
    assert "obs_use_gitlab_backend" in text, (
        "the swap gate must set obs_use_gitlab_backend"
    )
    assert "and ('CI_PROJECT_ID' in obs_env_keys)" in text, (
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
    assert "GitLab HTTP state (svc-17-18-observability)" in text, (
        "the INFO log must name the GitLab HTTP backend branch with the svc-17-18 state name"
    )


def test_swap_gate_uses_obs_state_name():
    text = _provision_text()
    assert "terraform/state/svc-17-18-observability" in text, (
        "the GitLab state address must use the svc-17-18-observability state name (state isolation)"
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
