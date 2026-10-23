"""Offline secret-hygiene drift-guard for the SVC-01 installer roles.

Slice-2 Task 11 (spec: svc-01-sql-database). Pins that every SVC-01 installer
task that carries the Proxmox API token, the raw env-file content, the
cluster_env.py JSON stdout, or a DB password sets no_log: true, and that no task
sets no_log: false while handling such a value. Markers tuned (as in SVC-09) to
avoid false positives on env-KEY-NAME presence checks and non-secret derivations.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc01_secret_hygiene.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_ROLES = ("svc01_preflight", "svc01_provision", "svc01_bootstrap", "svc01_handoff", "svc01_clean_slate")

_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when", "loop", "loop_control",
    "delegate_to", "become", "vars", "tags", "args", "block", "rescue", "always", "until",
    "retries", "delay", "environment", "check_mode", "notify", "listen", "ignore_errors",
}

# Secret VALUE markers (the token itself / CI job token / derived GitLab address).
_SECRET_MARKERS = (
    "proxmox_api_token",
    "svc01_env_ci_job_token",
    "svc01_env_gitlab_state_address",
    "openbao_admin_token",
)
# Register / set_fact names that BIND secret-bearing content.
_SECRET_REGISTERS = (
    "svc01_env_resolve", "svc01_cs_env_resolve", "svc01_tfvars",
    "svc01_env_slurp", "svc01_env_pairs", "svc01_env_ci_values",
)


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_task_files():
    for role in _ROLES:
        d = _ROLES_DIR / role / "tasks"
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.yml")):
            tasks = []
            for doc in yaml.safe_load_all(p.read_text(encoding="utf-8")):
                if isinstance(doc, list):
                    tasks.extend(_iter_tasks(doc))
            yield p, tasks


def _module_names(t: dict) -> set[str]:
    return {k for k in t if k not in _NON_MODULE_KEYS}


def _body_text(t: dict) -> str:
    parts = [str(t.get("name", "")), str(t.get("environment", ""))]
    for m in _module_names(t):
        parts.append(str(m))
        parts.append(str(t.get(m)))
    return " ".join(parts).lower()


def _no_log(t: dict):
    if "no_log" not in t:
        return None
    v = t["no_log"]
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("false", "no", "0", "")


def _set_fact_keys(t: dict) -> set[str]:
    for k in ("ansible.builtin.set_fact", "set_fact"):
        spec = t.get(k)
        if isinstance(spec, dict):
            return set(spec.keys())
    return set()


def _is_token_handling(t: dict) -> bool:
    if any(m in _body_text(t) for m in _SECRET_MARKERS):
        return True
    if str(t.get("register", "")) in _SECRET_REGISTERS:
        return True
    if _set_fact_keys(t) & set(_SECRET_REGISTERS):
        return True
    return False


def test_token_handling_tasks_are_no_log_true():
    offenders = []
    for p, tasks in _load_task_files():
        for t in tasks:
            if _is_token_handling(t) and _no_log(t) is not True:
                offenders.append(f"  - {p.relative_to(_REPO_ROOT)} :: {t.get('name', '<unnamed>')!r}")
    assert not offenders, (
        "SECRET-HYGIENE: SVC-01 task(s) handling a token / raw env content must be no_log: true:\n"
        + "\n".join(offenders)
    )


def test_no_no_log_false_token_task():
    offenders = []
    for p, tasks in _load_task_files():
        for t in tasks:
            if _no_log(t) is False and _is_token_handling(t):
                offenders.append(f"  - {p.relative_to(_REPO_ROOT)} :: {t.get('name', '<unnamed>')!r}")
    assert not offenders, "SECRET-HYGIENE: no_log: false on a token-handling task:\n" + "\n".join(offenders)


def test_scan_covered_the_roles():
    files = list(_load_task_files())
    roles_seen = {p.parent.parent.name for p, _ in files}
    for role in _ROLES:
        assert role in roles_seen, f"role {role!r} contributed no tasks to the scan"
