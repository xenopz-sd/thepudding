"""Bug-condition + preservation tests: openbao-agent recovery recreate (WS-C5).

Feature: fix-openbao-agent-handler-flush-on-abort.
Spec: .kiro/specs/fix-openbao-agent-handler-flush-on-abort/.

THE BUG
-------
The agent.hcl render task `notify`s the "Recreate openbao-agent sidecar" handler.
That is a ONE-SHOT signal: queued only on the run whose render CHANGED, flushed
only at end-of-play. If the docker-compose-app reconcile then aborts (the workload
`depends_on` the agent being service_healthy, so a stale/crash-looping sidecar
makes `up` itself fail), the handler is DISCARDED. The recovery run re-renders
byte-identical (changed=0), never re-notifies, and the stale sidecar survives every
re-run.

THE FIX (Option B)
------------------
A drift-gated recovery recreate, placed BEFORE the reconcile (the reconcile is what
fails against a crash-looping sidecar, so a recreate after it is unreachable on the
recovery run). It re-derives the need from ACTUAL STATE each converge: the sidecar
is recreated when it is crash-looping, or when agent.hcl is newer than the
container's StartedAt (it never re-read the config). It acts only when THIS run's
render is unchanged, so the notify handler keeps the changed-render case (no double
bounce). Gated on the container existing (== the compose project exists), not on
ordering.

These tests (1) pin the wiring structurally and (2) EVALUATE the real
`needs_recovery` expression, extracted from the task file, with Ansible's own
templater against the scenarios that matter. Pure offline; no Docker, no network.

Validates: bugfix.md requirements (recovery recreate; preservation of the notify
handler, idempotency, scoping, the fail-closed sealed-primary posture).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_TASKS = _REPO_ROOT / "ansible/roles/openbao-agent/tasks/main.yml"

_RENDER_NAME = "4 — Render agent.hcl onto the workload host (idempotent, Req 4.1/4.2/4.6)"
_PROBE_NAME = "4 — Inspect the openbao-agent sidecar (read-only, check-safe, WS-C5)"
_STAT_NAME = "4 — Stat agent.hcl for the staleness comparison (read-only, WS-C5)"
_EPOCH_NAME = "4 — Compute the sidecar StartedAt epoch (UTC, WS-C5)"
_DECIDE_NAME = "4 — Decide whether the sidecar needs a recovery recreate (drift, WS-C5)"
_RECREATE_NAME = "4 — Recreate the stale/crash-looping openbao-agent sidecar (drift-gated, WS-C5)"
_COMPOSE_NAME = "4 — Deploy the Agent sidecar + workload Compose stack (swap-gate, Req 4.3/7.5)"


def _tasks() -> list[dict]:
    docs = yaml.safe_load(_AGENT_TASKS.read_text(encoding="utf-8"))
    assert isinstance(docs, list)
    return [t for t in docs if isinstance(t, dict)]


def _by_name(name: str) -> dict:
    for t in _tasks():
        if t.get("name") == name:
            return t
    raise AssertionError(f"task not found in openbao-agent/tasks/main.yml: {name!r}")


def _index(name: str) -> int:
    for i, t in enumerate(_tasks()):
        if t.get("name") == name:
            return i
    raise AssertionError(f"task not found: {name!r}")


# --------------------------------------------------------------------------- #
# Structural wiring
# --------------------------------------------------------------------------- #
def test_recovery_tasks_exist_and_are_read_only_where_they_should_be():
    """The probe + stat are read-only and check-safe; the recreate is the only mutator."""
    probe = _by_name(_PROBE_NAME)
    assert "community.docker.docker_container_info" in probe
    assert probe.get("check_mode") is False, "probe must run under --check so the fact can evaluate"
    assert probe.get("changed_when") is False, "a read must never report changed"
    assert probe.get("failed_when") is False, "no container yet is a first run, not an error"

    stat = _by_name(_STAT_NAME)
    assert "ansible.builtin.stat" in stat
    assert stat.get("check_mode") is False and stat.get("changed_when") is False

    assert "ansible.builtin.set_fact" in _by_name(_EPOCH_NAME)
    assert "ansible.builtin.set_fact" in _by_name(_DECIDE_NAME)


def test_recovery_runs_after_render_and_BEFORE_the_reconcile():
    """The bug's core: the reconcile fails against a crash-looping sidecar, so the
    recovery recreate MUST precede it or it is unreachable on the recovery run."""
    render = _index(_RENDER_NAME)
    recreate = _index(_RECREATE_NAME)
    compose = _index(_COMPOSE_NAME)
    assert render < recreate < compose, (
        f"order must be render({render}) < recovery-recreate({recreate}) < "
        f"docker-compose-app reconcile({compose}); a recreate placed AFTER the "
        "reconcile is never reached when the reconcile aborts (the WS-C5 bug)"
    )


def test_recreate_is_compose_native_scoped_and_drift_gated():
    task = _by_name(_RECREATE_NAME)
    spec = task.get("community.docker.docker_compose_v2")
    assert spec is not None, "must use declarative docker_compose_v2, never a shell wrapper"
    assert spec.get("services") == ["openbao-agent"], (
        "must be scoped to the openbao-agent service so a healthy workload is not bounced"
    )
    assert spec.get("recreate") == "always"
    assert "openbao_agent_sidecar_needs_recovery" in str(task.get("when")), (
        "the recreate must be gated on the drift fact, not unconditional (idempotency)"
    )


def test_notify_handler_is_preserved_for_the_changed_render_case():
    """Preservation: the existing notify stays (the changed-render case is its job)."""
    render = _by_name(_RENDER_NAME)
    notify = render.get("notify")
    notify_list = [notify] if isinstance(notify, str) else list(notify or [])
    assert "Recreate openbao-agent sidecar" in notify_list
    assert "changed_when" not in render, "render must keep built-in change detection"


def test_no_shell_docker_wrapper_added():
    blob = _AGENT_TASKS.read_text(encoding="utf-8")
    for bad in ("docker compose restart", "docker restart", "docker-compose restart"):
        assert bad not in blob


# --------------------------------------------------------------------------- #
# Behavior: evaluate the REAL expression with Ansible's templater
# --------------------------------------------------------------------------- #
def _needs_recovery_expr() -> str:
    task = _by_name(_DECIDE_NAME)
    return str(task["ansible.builtin.set_fact"]["openbao_agent_sidecar_needs_recovery"])


def _epoch_expr() -> str:
    task = _by_name(_EPOCH_NAME)
    return str(task["ansible.builtin.set_fact"]["openbao_agent_sidecar_started_epoch"])


# 2026-11-05T14:30:14Z
_STARTED = "2026-11-05T14:30:14.548385123Z"
_STARTED_EPOCH = 1793889014.0


def _scenario(*, exists=True, restarting=False, status="running", started=_STARTED,
              mtime=_STARTED_EPOCH - 3600, render_changed=False, stat_exists=True):
    """Build the variable set the two set_fact tasks see, run both, return the verdict."""
    variables = {
        "openbao_agent_sidecar_probe": {
            "exists": exists,
            "container": {"State": {"Restarting": restarting, "Status": status,
                                    "StartedAt": started}},
        },
        "openbao_agent_config_stat": {"stat": {"exists": stat_exists, "mtime": mtime}},
        "openbao_agent_config_render": {"changed": render_changed},
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    epoch = templar.template(trust_as_template(_epoch_expr()))
    templar = Templar(loader=DataLoader(),
                      variables={**variables, "openbao_agent_sidecar_started_epoch": epoch})
    verdict = templar.template(trust_as_template(_needs_recovery_expr()))
    return epoch, bool(verdict)


def test_expression_parses_nanosecond_startedat_to_the_right_epoch():
    epoch, _ = _scenario()
    assert float(epoch) == _STARTED_EPOCH


def test_first_run_no_container_does_not_recreate():
    """No container yet => the reconcile creates the stack; a scoped recreate would
    target a project that does not exist."""
    _, needs = _scenario(exists=False, mtime=_STARTED_EPOCH + 999)
    assert needs is False


def test_converged_host_is_a_noop_idempotent():
    """Container started AFTER the config was written, not restarting => changed=0."""
    _, needs = _scenario(mtime=_STARTED_EPOCH - 3600)
    assert needs is False


def test_crash_looping_sidecar_is_recreated():
    """BUG CONDITION: a crash-looping sidecar survives re-runs on unfixed code."""
    _, needs = _scenario(restarting=True)
    assert needs is True
    _, needs2 = _scenario(status="restarting")
    assert needs2 is True


def test_recovery_run_after_aborted_reconcile_recreates_stale_sidecar():
    """THE WS-C5 SCENARIO. Run N: render changed (handler queued), reconcile aborted,
    handler discarded. Run N+1: render byte-identical (changed=False) but agent.hcl is
    NEWER than the container's StartedAt (it never re-read it). Unfixed code never
    recreates; the drift check must."""
    _, needs = _scenario(render_changed=False, mtime=_STARTED_EPOCH + 600)
    assert needs is True


def test_changed_render_is_left_to_the_notify_handler_no_double_bounce():
    """Same-run changed render: the handler owns it. Acting here too would bounce the
    sidecar twice in one run."""
    _, needs = _scenario(render_changed=True, mtime=_STARTED_EPOCH + 600)
    assert needs is False
    _, needs2 = _scenario(render_changed=True, restarting=True)
    assert needs2 is False


def test_sealed_primary_unhealthy_by_design_is_not_a_trigger():
    """Fail-closed posture: a sidecar that is running-but-unhealthy because the
    Primary is sealed must NOT be recreated (it would not help and would churn every
    run). Only crash-loop / stale-config are triggers."""
    _, needs = _scenario(restarting=False, status="running", mtime=_STARTED_EPOCH - 3600)
    assert needs is False


def test_missing_config_file_does_not_trigger_on_staleness():
    _, needs = _scenario(stat_exists=False, mtime=0)
    assert needs is False
