"""Offline secret-hygiene drift-guard for the SVC-17/18 installer roles.

Task 9.6 (spec: svc-17-18-observability). Clone of test_svc09_secret_hygiene.py
scaled to the observability installer's richer secret surface: the Proxmox API
token + CI job token flow through the cluster-env resolve / the TF_VAR_* env /
the raw env-file slurp (as in SVC-09), AND SVC-17/18 additionally reads the
Grafana Break_Glass_Admin password + OIDC client secret from OpenBao via the
delegated bao-CLI docker-exec (FR-15), bridges OPENBAO_ADMIN_TOKEN in a child
ENV, and writes a 0600 secret env.

This guard pins that every installer task that carries a secret/token value sets
no_log: true, that NO task sets no_log: false while handling one, and — the
agent-live coverage half — that obs_preflight AND obs_clean_slate each carry the
throwaway-guard consult gated `when: agent_live_run` with a fail-closed assert.

Pure PyYAML parse (always runs, no infra). Scans the five obs_* installer roles
PLUS observability_stack/tasks/grafana.yml (the bao-CLI reads + secret-env write).

These live in tests/installer/ (a SEPARATE pytest root from infra/tests — the
same-named conftest collision rule forbids a cross-tree import), so the
task-walking helpers are LOCAL copies of the svc09 sibling's idiom.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_obs_secret_hygiene.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_OBS_ROLES = (
    "obs_preflight",
    "obs_provision",
    "obs_bootstrap",
    "obs_handoff",
    "obs_clean_slate",
)
# The observability_stack grafana.yml is where the OpenBao-delegated bao-CLI
# secret reads + the 0600 secret-env write live (FR-15). Scanned as an extra
# explicit file (it is NOT one of the five obs_* installer roles, but it carries
# the richest secret surface of the whole feature).
_GRAFANA_TASKS = _ROLES_DIR / "observability_stack" / "tasks" / "grafana.yml"

_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen", "ignore_errors",
}

# Markers whose presence in a task body means the task REGISTERS/BINDS or
# OTHERWISE carries a secret-bearing VALUE — the Proxmox API token, the CI job
# token, the raw env-file content that contains them, the OpenBao admin/platform
# token, or the Grafana admin password / OIDC client-secret value. These tasks
# must be no_log: true.
#
# Deliberately EXCLUDED (non-secret derivations, matching the SVC-07/09
# convention where the equivalents carry no no_log): obs_env_keys (key NAMES
# only), obs_use_gitlab_backend (a boolean), the SSH public-key-file PATH bind
# (a path to a PUBLIC key), and the bare env-key NAME "CI_JOB_TOKEN" (a
# key-presence check in the non-secret backend-decision set_fact). The secret
# VALUE facts (obs_env_ci_job_token, proxmox_api_token, OPENBAO_ADMIN_TOKEN,
# observability_bao_token, the grafana admin password / OIDC client secret) ARE
# markers below.
#
# NOTE on the Grafana admin password / OIDC client secret: these are NOT body
# markers. They are caught instead as set_fact BIND keys in _SECRET_REGISTERS
# below, so the task that actually binds the secret VALUE is flagged while the
# fail-fast DIAGNOSTIC tasks that merely reference the read's `.rc`/field-NAME in
# a message (and emit no value) are correctly NOT flagged — matching the
# SVC-07/09 convention where such diagnostics carry no no_log, and mirroring how
# grafana.yml itself leaves those fail tasks un-no_log'd on purpose.
_SECRET_MARKERS = (
    "proxmox_api_token",
    "obs_env_ci_job_token",
    "obs_env_gitlab_state_address",
    "openbao_admin_token",            # child ENV key for the delegated bao-CLI
    "observability_bao_token",        # BAO_TOKEN in the bao-CLI argv
)

# Register vars / set_fact names / register names that BIND secret-bearing
# content (the raw env-file slurp, the KEY=VALUE pairs whose values contain the
# token, the extracted CI values, the cluster_env.py JSON stdout, and the
# bao-CLI stdout that returns a secret). A task that REGISTERS or BINDS one of
# these must be no_log: true. A task that merely READS one to derive a NON-secret
# projection (key NAMES, a boolean, an SSH key PATH) is NOT flagged — matching
# the SVC-07/09 convention.
_SECRET_REGISTERS = (
    "obs_env_resolve",
    "obs_cs_env_resolve",
    "obs_tfvars",
    "obs_env_slurp",
    "obs_env_pairs",
    "obs_env_ci_values",
    "observability_grafana_admin_read",
    "observability_grafana_oidc_read",
    "observability_grafana_admin_password",
    "observability_grafana_oidc_client_secret",
)


def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_tasks_from_file(path: Path):
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    tasks = []
    for doc in docs:
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _load_task_files():
    for role in _OBS_ROLES:
        tasks_dir = _ROLES_DIR / role / "tasks"
        if not tasks_dir.is_dir():
            continue
        for path in sorted(tasks_dir.glob("*.yml")):
            yield path, _load_tasks_from_file(path)
    if _GRAFANA_TASKS.is_file():
        yield _GRAFANA_TASKS, _load_tasks_from_file(_GRAFANA_TASKS)


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_body_text(task: dict) -> str:
    parts = [str(task.get("name", ""))]
    env = task.get("environment")
    if env is not None:
        parts.append(str(env))
    for mod in _module_names(task):
        spec = task.get(mod)
        parts.append(str(mod))
        parts.append(str(spec))
    return " ".join(parts).lower()


def _no_log_value(task: dict):
    if "no_log" not in task:
        return None
    val = task["no_log"]
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("false", "no", "0", "")


def _set_fact_keys(task: dict) -> set[str]:
    """Keys a set_fact task defines (the facts it BINDS)."""
    for key in ("ansible.builtin.set_fact", "set_fact"):
        spec = task.get(key)
        if isinstance(spec, dict):
            return set(spec.keys())
    return set()


def _is_token_handling(task: dict) -> bool:
    """A task HANDLES a secret value if it either:

    * references a secret VALUE marker in its name/args/env (the token itself or
      raw token-bearing content), OR
    * REGISTERS a secret-bearing register var, OR
    * BINDS (set_fact) one of the secret register/fact names.

    A task that merely READS a secret-bearing fact in an expression to derive a
    NON-secret value (e.g. extracting the SSH key-file path or the key-NAMES set)
    is NOT token-handling — mirroring the SVC-07/09 convention where those
    derivations carry no no_log.
    """
    body = _task_body_text(task)
    if any(marker in body for marker in _SECRET_MARKERS):
        return True
    reg = str(task.get("register", ""))
    if reg in _SECRET_REGISTERS:
        return True
    if _set_fact_keys(task) & set(_SECRET_REGISTERS):
        return True
    return False


def test_token_handling_tasks_are_no_log_true():
    """Every SVC-17/18 installer task that carries a token / raw env content /
    OpenBao token / Grafana secret value must set no_log: true."""
    offenders = []
    for path, tasks in _load_task_files():
        for task in tasks:
            if not _is_token_handling(task):
                continue
            if _no_log_value(task) is not True:
                offenders.append(
                    f"  - {path.relative_to(_REPO_ROOT)} :: {task.get('name', '<unnamed>')!r}"
                )
    assert not offenders, (
        "SECRET-HYGIENE: SVC-17/18 installer task(s) handling a token / raw env-file "
        "content / OpenBao token / Grafana secret must set no_log: true. Offending "
        "task(s):\n" + "\n".join(offenders)
    )


def test_no_no_log_false_token_task():
    """No SVC-17/18 installer task may set no_log: false while handling a secret."""
    offenders = []
    for path, tasks in _load_task_files():
        for task in tasks:
            if _no_log_value(task) is False and _is_token_handling(task):
                offenders.append(
                    f"  - {path.relative_to(_REPO_ROOT)} :: {task.get('name', '<unnamed>')!r}"
                )
    assert not offenders, (
        "SECRET-HYGIENE: SVC-17/18 task(s) set no_log: false while handling a secret value:\n"
        + "\n".join(offenders)
    )


def test_scan_covered_the_roles_and_grafana():
    """Guard against a vacuous scan: the five obs_* roles AND grafana.yml must
    contribute tasks."""
    files = list(_load_task_files())
    assert files, "no SVC-17/18 role task files found — repoint this guard"
    roles_seen = {
        p.parent.parent.name for p, _ in files if p.parent.parent.name in _OBS_ROLES
    }
    for role in _OBS_ROLES:
        assert role in roles_seen, f"role {role!r} contributed no tasks to the scan"
    assert any(p == _GRAFANA_TASKS for p, _ in files), (
        "observability_stack/tasks/grafana.yml (the bao-CLI secret reads) was not scanned"
    )


def test_grafana_bao_cli_reads_and_secret_env_are_no_log():
    """The two OpenBao-delegated bao-CLI reads (admin password + OIDC client
    secret) and the 0600 secret-env write in grafana.yml must each be
    no_log: true (FR-15 — the secret value is never echoed)."""
    assert _GRAFANA_TASKS.is_file(), f"expected grafana.yml at {_GRAFANA_TASKS}"
    tasks = _load_tasks_from_file(_GRAFANA_TASKS)
    bao_reads = [
        t for t in tasks
        if "observability_bao_token" in _task_body_text(t)
        and ("ansible.builtin.command" in t or "command" in t)
    ]
    assert len(bao_reads) >= 2, (
        "expected at least two delegated bao-CLI reads (admin password + OIDC client "
        f"secret) in grafana.yml; found {len(bao_reads)}"
    )
    for task in bao_reads:
        assert _no_log_value(task) is True, (
            f"grafana.yml bao-CLI read {task.get('name', '<unnamed>')!r} must be no_log: true"
        )
    # The 0600 secret-env write (admin password + OIDC secret into the guest env).
    secret_env_writes = [
        t for t in tasks
        if "grafana.secret.env" in _task_body_text(t)
    ]
    assert secret_env_writes, "expected a grafana.secret.env write task in grafana.yml"
    for task in secret_env_writes:
        assert _no_log_value(task) is True, (
            f"grafana.yml secret-env write {task.get('name', '<unnamed>')!r} must be no_log: true"
        )


# --------------------------------------------------------------------------- #
# Agent-live throwaway-guard coverage (obs_preflight + obs_clean_slate).
# Mirrors the svc07/svc09 reference wiring: a three-task consult — a guard
# command (gated when: agent_live_run), an INFO decision debug, and a fail-closed
# assert on the guard's rc — present in BOTH the preflight (before any Proxmox
# call) and the clean-slate (before any destroy).
# --------------------------------------------------------------------------- #
def _role_main_tasks(role: str):
    path = _ROLES_DIR / role / "tasks" / "main.yml"
    assert path.is_file(), f"expected {role} main.yml at {path}"
    return path, _load_tasks_from_file(path)


def _has_agent_live_guard(tasks) -> tuple[bool, bool]:
    """Return (has_gated_guard_consult, has_gated_fail_closed_assert)."""
    gated_consult = False
    gated_assert = False
    for task in tasks:
        when = str(task.get("when", ""))
        gated = "agent_live_run" in when
        if not gated:
            continue
        body = _task_body_text(task)
        mods = _module_names(task)
        # The guard command consult: a command that runs the throwaway_guard.
        if ("ansible.builtin.command" in mods or "command" in mods) and "throwaway_guard" in body:
            gated_consult = True
        # The fail-closed assert on the guard's rc.
        if ("ansible.builtin.assert" in mods or "assert" in mods) and "throwaway_decision" in body:
            gated_assert = True
    return gated_consult, gated_assert


def test_agent_live_guard_in_preflight():
    _, tasks = _role_main_tasks("obs_preflight")
    consult, assert_closed = _has_agent_live_guard(tasks)
    assert consult, (
        "obs_preflight must consult the throwaway-cluster guard gated `when: agent_live_run`"
    )
    assert assert_closed, (
        "obs_preflight must fail CLOSED (gated assert on the guard rc) when agent_live_run"
    )


def test_agent_live_guard_in_clean_slate():
    _, tasks = _role_main_tasks("obs_clean_slate")
    consult, assert_closed = _has_agent_live_guard(tasks)
    assert consult, (
        "obs_clean_slate must consult the throwaway-cluster guard gated `when: agent_live_run`"
    )
    assert assert_closed, (
        "obs_clean_slate must fail CLOSED (gated assert on the guard rc) when agent_live_run"
    )
