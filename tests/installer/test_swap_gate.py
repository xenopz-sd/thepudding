"""Offline swap-gate test for the SVC-07 automated installer (Task 12.1).

Feature: svc-07-automated-installer.

Spec: .kiro/specs/svc-07-automated-installer/ (design.md "Testing Strategy" §2,
Requirement 4.2). This is the installer's analogue of a composition-root
real-vs-fake swap gate: the Terraform BACKEND selection.

WHAT IS ASSERTED (offline, no live apply, no infra)
---------------------------------------------------
When the Cluster_Env_File does NOT configure the GitLab HTTP state backend (i.e.
it is missing one or more of the three GitLab CI state-backend keys
CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN), the installer's swap gate:

  * resolves ``svc07_use_gitlab_backend`` to FALSE, and
  * selects the LOCAL backend by ensuring ``local_backend_override.tf`` is
    present in the SVC-07 Terraform root, and
  * logs the decision at INFO level naming the LOCAL backend
    (per ``composition-wiring.md`` dependency-type logging).

The positive case (all three CI keys present -> GitLab HTTP backend selected,
override removed) is asserted too, so the gate is pinned in both directions.

HOW IT STAYS OFFLINE
--------------------
The swap-gate decision now lives in the ``svc07_provision`` role
(``ansible/roles/svc07_provision/tasks/main.yml``) — moved there when
``svc-07-bootstrap.yml`` was thinned to a ~150-line orchestrator of five
import_role calls (svc07-installer-simplification, Task 4.1/10.2). Running the
real installer end-to-end still first executes Phase 1's live Proxmox preflight,
so it is NOT offline. Instead this test drives ``swap_gate_harness.yml``, a
harness play that reproduces the swap-gate decision chain VERBATIM and runs it
purely locally (``connection: local``, ``gather_facts: false``) against a stub
env file in a tmp dir, pointing the override write at a tmp Terraform root so no
real file is mutated. A drift guard
(:func:`test_harness_logic_matches_playbook`) asserts the ``svc07_provision``
role still carries the same decision expression, so the harness cannot silently
diverge.

GATING
------
Needs only an ``ansible-playbook`` binary (PATH or the devinfra venv) — no live
Proxmox, so it is NOT ``requires_infra``. Absent the binary the run-based tests
SKIP cleanly (the same ``shutil.which`` idiom the rest of the SVC-07 suite uses);
the drift guard is pure text and always runs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# tests/installer/ -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS = Path(__file__).resolve().parent / "swap_gate_harness.yml"
# The swap-gate decision chain now lives in the svc07_provision ROLE (spec
# svc07-installer-simplification, Task 4.1/10.2): the ~1900-line
# svc-07-bootstrap.yml was thinned to a ~150-line orchestrator of five
# import_role calls, and Phase 2's backend swap-gate + its INFO decision log
# moved into ansible/roles/svc07_provision/tasks/main.yml. The drift guard below
# therefore reads the PROVISION ROLE, not the play, so it keeps pinning the real
# decision expression the harness stands in for.
_PROVISION_ROLE_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_provision" / "tasks" / "main.yml"
)

_RESULT_RE = re.compile(
    r"SWAP_GATE_RESULT\s+use_gitlab_backend=(?P<gitlab>\w+)\s+"
    r"override_present=(?P<override>\w+)"
)

# The mandatory (non-GitLab) parameters a real Cluster_Env_File always carries.
# The swap gate ignores these; they exist only so the stub env file resembles a
# realistic file rather than an artificial one-key file.
_BASE_ENV_LINES = (
    "# SVC-07 cluster env (offline swap-gate test stub — placeholders only)",
    "PROXMOX_ENDPOINT=https://pve.example:8006",
    "PROXMOX_API_TOKEN=root@pam!id=placeholder-token",
    "PROXMOX_NODE_NAME=node1",
    "PROXMOX_LXC_TEMPLATE=local:vztmpl/debian-12-standard.tar.zst",
)

# The three GitLab CI state-backend keys the gate looks for (placeholder values).
_GITLAB_ENV_LINES = (
    "CI_API_V4_URL=https://gitlab.example/api/v4",
    "CI_PROJECT_ID=1234",
    "CI_JOB_TOKEN=placeholder-ci-job-token",
)


def _ansible_playbook_binary() -> str | None:
    """An ``ansible-playbook`` binary path, or ``None``. PATH first, then venv.

    Same skip idiom as the rest of the SVC-07 suite: absent binary => SKIP
    cleanly, never fail.
    """
    on_path = shutil.which("ansible-playbook")
    if on_path:
        return on_path
    venv_candidate = Path.home() / "venv" / "devinfra" / "bin" / "ansible-playbook"
    if venv_candidate.is_file() and os.access(venv_candidate, os.X_OK):
        return str(venv_candidate)
    return None


requires_ansible = pytest.mark.skipif(
    _ansible_playbook_binary() is None,
    reason="requires ansible-playbook, not installed in venv",
)


def _run_gate(env_lines: tuple[str, ...], tmp_path: Path) -> tuple[bool, bool, str]:
    """Run the harness against a stub env file; return (use_gitlab, override, out).

    Writes the stub Cluster_Env_File and points the harness's repo root + tmp
    Terraform root at ``tmp_path`` so nothing real is touched. Parses the
    machine-parseable ``SWAP_GATE_RESULT`` line the harness emits.
    """
    binary = _ansible_playbook_binary()
    assert binary is not None  # guaranteed by the skipif gate

    env_file = tmp_path / "cluster.env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    tf_root = tmp_path / "tf-root"
    tf_root.mkdir()

    cmd = [
        binary,
        "-i", "localhost,",
        str(_HARNESS),
        "-c", "local",
        "-e", f"svc07_repo_root={tmp_path}",
        "-e", "cluster_env_file=cluster.env",
        "-e", f"svc07_tf_root={tf_root}",
    ]

    env = dict(os.environ)
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_LOCALHOST_WARNING"] = "False"
    env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"
    # Keep the run hermetic and free of any ambient inventory/config.
    env.pop("ANSIBLE_INVENTORY", None)

    proc = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "swap-gate harness play failed:\n"
        f"CMD: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )

    match = _RESULT_RE.search(output)
    assert match is not None, f"no SWAP_GATE_RESULT line in harness output:\n{output}"
    use_gitlab = match.group("gitlab").strip().lower() == "true"
    override_present = match.group("override").strip().lower() == "true"
    return use_gitlab, override_present, output


# =============================================================================
# Task 12.1 — the offline swap-gate assertion (Requirement 4.2)
# =============================================================================


@requires_ansible
def test_no_gitlab_keys_selects_local_backend_and_logs_info(tmp_path):
    """No GitLab HTTP backend configured -> LOCAL override selected + INFO log.

    Feature: svc-07-automated-installer, swap-gate (Requirement 4.2).

    The stub env file has none of the three GitLab CI state-backend keys, so the
    gate must resolve ``svc07_use_gitlab_backend`` FALSE, ensure the local
    override file is present, and log the LOCAL decision at INFO level.
    """
    use_gitlab, override_present, output = _run_gate(_BASE_ENV_LINES, tmp_path)

    # (a) The gate resolves to the LOCAL backend.
    assert use_gitlab is False, (
        "with no GitLab CI state-backend keys the gate must select the LOCAL "
        f"backend (use_gitlab_backend=False); output:\n{output}"
    )

    # The LOCAL branch ensures local_backend_override.tf is present.
    assert override_present is True, (
        "the LOCAL backend branch must ensure local_backend_override.tf is "
        f"present in the Terraform root; output:\n{output}"
    )

    # (b) The decision is logged at INFO level naming the LOCAL backend
    # (composition-wiring.md dependency-type logging).
    assert "INFO: Terraform backend:" in output, (
        f"the swap gate must emit an INFO-level backend-selection log line; output:\n{output}"
    )
    assert "LOCAL (local_backend_override.tf)" in output, (
        "the INFO log line must name the LOCAL backend when GitLab state is not "
        f"configured; output:\n{output}"
    )
    # And it must NOT claim the GitLab HTTP backend was chosen.
    assert "GitLab HTTP state" not in output, (
        "the INFO log must not name the GitLab HTTP backend for a local-only env; "
        f"output:\n{output}"
    )


@requires_ansible
def test_partial_gitlab_keys_still_selects_local_backend(tmp_path):
    """Only SOME GitLab keys present still selects LOCAL (all three required).

    The gate requires ALL THREE of CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN.
    A file with only two of them does NOT configure the GitLab HTTP backend, so
    the gate must still fall back to LOCAL. This pins the AND semantics.
    """
    partial = _BASE_ENV_LINES + (
        "CI_API_V4_URL=https://gitlab.example/api/v4",
        "CI_PROJECT_ID=1234",
        # CI_JOB_TOKEN deliberately absent.
    )
    use_gitlab, override_present, output = _run_gate(partial, tmp_path)

    assert use_gitlab is False, (
        "a partial GitLab key set (missing CI_JOB_TOKEN) must NOT configure the "
        f"GitLab backend — the gate must select LOCAL; output:\n{output}"
    )
    assert override_present is True
    assert "LOCAL (local_backend_override.tf)" in output


@requires_ansible
def test_all_gitlab_keys_selects_gitlab_backend_and_removes_override(tmp_path):
    """All three GitLab keys present -> GitLab HTTP backend, override removed.

    The negative direction of the gate: when the Cluster_Env_File DOES configure
    the GitLab HTTP backend, the gate resolves ``svc07_use_gitlab_backend`` TRUE,
    logs the GitLab decision at INFO, and removes any local override so Terraform
    does not merge the local backend over the http one.
    """
    with_gitlab = _BASE_ENV_LINES + _GITLAB_ENV_LINES

    # Pre-create a stale override to prove the GitLab branch REMOVES it.
    tf_root = tmp_path / "tf-root"
    tf_root.mkdir()
    (tf_root / "local_backend_override.tf").write_text(
        'terraform { backend "local" {} }\n', encoding="utf-8"
    )
    env_file = tmp_path / "cluster.env"
    env_file.write_text("\n".join(with_gitlab) + "\n", encoding="utf-8")

    binary = _ansible_playbook_binary()
    assert binary is not None
    cmd = [
        binary,
        "-i", "localhost,",
        str(_HARNESS),
        "-c", "local",
        "-e", f"svc07_repo_root={tmp_path}",
        "-e", "cluster_env_file=cluster.env",
        "-e", f"svc07_tf_root={tf_root}",
    ]
    proc_env = dict(os.environ)
    proc_env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    proc_env["ANSIBLE_LOCALHOST_WARNING"] = "False"
    proc_env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"
    proc_env.pop("ANSIBLE_INVENTORY", None)

    proc = subprocess.run(
        cmd, cwd=str(_REPO_ROOT), env=proc_env,
        capture_output=True, text=True, timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    match = _RESULT_RE.search(output)
    assert match is not None, f"no SWAP_GATE_RESULT line:\n{output}"

    assert match.group("gitlab").strip().lower() == "true", (
        "all three GitLab CI state-backend keys present must select the GitLab "
        f"HTTP backend; output:\n{output}"
    )
    # The GitLab branch removes the local override.
    assert match.group("override").strip().lower() == "false", (
        "the GitLab HTTP backend branch must remove local_backend_override.tf so "
        f"Terraform does not merge the local backend; output:\n{output}"
    )
    assert "GitLab HTTP state" in output, (
        f"the INFO log must name the GitLab HTTP backend; output:\n{output}"
    )
    assert "LOCAL (local_backend_override.tf)" not in output


# =============================================================================
# Drift guard — the harness must not diverge from the real playbook
# =============================================================================


def test_harness_logic_matches_playbook():
    """The svc07_provision ROLE still carries the swap-gate decision the harness
    reproduces (pure-text, always runs).

    Repointed for the svc07-installer-simplification rework: the swap-gate moved
    from the inline Phase-2 block of ``svc-07-bootstrap.yml`` into
    ``ansible/roles/svc07_provision/tasks/main.yml``. If a future edit changes
    the gate's decision expression or its INFO-log wording in the PROVISION ROLE
    without updating the harness, this guard fails — so the offline test cannot
    silently drift from the code it stands in for. The assertions' INTENT is
    unchanged; only the file they read moved.
    """
    provision = _PROVISION_ROLE_TASKS.read_text(encoding="utf-8")
    harness = _HARNESS.read_text(encoding="utf-8")

    # The three CI keys that define the gate, and the AND semantics, must be
    # present in BOTH the real provision-role tasks and the harness.
    for key in ("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"):
        assert key in provision, (
            f"{key} missing from svc07_provision/tasks/main.yml swap gate"
        )
        assert key in harness, f"{key} missing from the harness swap gate"

    # The decision fact name and the INFO-log markers must match across both, so
    # the harness's assertions map onto the real code path (now in the role).
    for token in (
        "svc07_use_gitlab_backend",
        "INFO: Terraform backend:",
        "LOCAL (local_backend_override.tf)",
        "GitLab HTTP state",
    ):
        assert token in provision, (
            f"'{token}' missing from svc07_provision/tasks/main.yml"
        )
        assert token in harness, f"'{token}' missing from the harness"
