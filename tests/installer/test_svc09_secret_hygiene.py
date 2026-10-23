"""Offline secret-hygiene drift-guard for the SVC-09 installer roles.

Slice-1 Task 5 (spec: svc-09-reverse-proxy). SVC-09 handles far fewer secrets
than SVC-07 (no admin-token mint, no seal token), but the Proxmox API token
still flows through the cluster-env resolve, the TF_VAR_* child env, and the raw
env-file slurp. This guard pins that every SVC-09 installer task that carries the
Proxmox API token (or the raw env-file content, which includes it) sets
no_log: true, and that NO task sets no_log: false while handling a token value.

Pure PyYAML parse (always runs, no infra). Scans the five svc09_* roles.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc09_secret_hygiene.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_ROLES = (
    "svc09_preflight",
    "svc09_provision",
    "svc09_bootstrap",
    "svc09_handoff",
    "svc09_clean_slate",
)

_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen", "ignore_errors",
}

# Markers whose presence in a task body means the task REGISTERS/BINDS a
# secret-bearing VALUE (the Proxmox API token, the CI job token, the raw env-file
# content that contains them, or the cluster_env.py JSON stdout that carries the
# token). These tasks must be no_log: true.
#
# Deliberately EXCLUDED (non-secret derivations, matching the SVC-07 convention
# where the equivalents carry no no_log): svc09_env_keys (key NAMES only),
# svc09_use_gitlab_backend (a boolean), and the SSH public-key-file PATH bind
# (a path to a PUBLIC key, not a secret). A marker here must denote a value that
# is itself the token or raw token-bearing content.
# Secret VALUE markers: a task naming one of these in its args/env is handling
# the token itself (not a non-secret derivation). The GitLab state address is
# derived in the same no_log task as the CI job token in SVC-07.
# NOTE: the bare env-key NAME "CI_JOB_TOKEN" is deliberately NOT a marker — it
# appears as a key-presence check in the non-secret backend-decision set_fact
# (`'CI_JOB_TOKEN' in svc09_env_keys`), which handles no token value (exactly
# like SVC-07's decision task, which carries no no_log). The secret VALUE is held
# by the fact `svc09_env_ci_job_token`, which IS a marker below.
_SECRET_MARKERS = (
    "proxmox_api_token",
    "svc09_env_ci_job_token",
    "svc09_env_gitlab_state_address",
)

# Register vars / set_fact names that BIND secret-bearing content (the raw
# env-file slurp, the KEY=VALUE pairs whose values contain the token, the
# extracted CI values, and the cluster_env.py JSON stdout). The task that
# REGISTERS or BINDS one of these must be no_log: true. A task that merely READS
# one to derive a NON-secret projection (key NAMES, a boolean, an SSH key PATH)
# is NOT flagged — matching the SVC-07 convention.
_SECRET_REGISTERS = (
    "svc09_env_resolve",
    "svc09_cs_env_resolve",
    "svc09_tfvars",
    "svc09_env_slurp",
    "svc09_env_pairs",
    "svc09_env_ci_values",
)


def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_task_files():
    for role in _ROLES:
        tasks_dir = _ROLES_DIR / role / "tasks"
        if not tasks_dir.is_dir():
            continue
        for path in sorted(tasks_dir.glob("*.yml")):
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            tasks = []
            for doc in docs:
                if isinstance(doc, list):
                    tasks.extend(_iter_tasks(doc))
            yield path, tasks


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
    NON-secret value (e.g. extracting the SSH key-file path or the key-NAMES set
    from svc09_tfvars/svc09_env_pairs) is NOT token-handling — mirroring the
    SVC-07 convention where those derivations carry no no_log.
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
    """Every SVC-09 installer task that carries the API token / raw env content
    must set no_log: true."""
    offenders = []
    for path, tasks in _load_task_files():
        for task in tasks:
            if not _is_token_handling(task):
                continue
            # A set_fact that only derives a NON-secret key set from the env
            # (svc09_env_keys) is exempt — but those don't match the markers.
            if _no_log_value(task) is not True:
                offenders.append(f"  - {path.relative_to(_REPO_ROOT)} :: {task.get('name', '<unnamed>')!r}")
    assert not offenders, (
        "SECRET-HYGIENE: SVC-09 installer task(s) handling the Proxmox API token / raw "
        "env-file content must set no_log: true. Offending task(s):\n" + "\n".join(offenders)
    )


def test_no_no_log_false_token_task():
    """No SVC-09 installer task may set no_log: false while handling a token value."""
    offenders = []
    for path, tasks in _load_task_files():
        for task in tasks:
            if _no_log_value(task) is False and _is_token_handling(task):
                offenders.append(f"  - {path.relative_to(_REPO_ROOT)} :: {task.get('name', '<unnamed>')!r}")
    assert not offenders, (
        "SECRET-HYGIENE: SVC-09 task(s) set no_log: false while handling a token value:\n"
        + "\n".join(offenders)
    )


def test_scan_covered_the_roles():
    """Guard against a vacuous scan: the five roles must contribute tasks."""
    files = list(_load_task_files())
    assert files, "no SVC-09 role task files found — repoint this guard"
    roles_seen = {p.parent.parent.name for p, _ in files}
    for role in _ROLES:
        assert role in roles_seen, f"role {role!r} contributed no tasks to the scan"
