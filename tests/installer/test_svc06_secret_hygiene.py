"""Offline secret-hygiene drift-guard for the SVC-06 installer roles.

Slice-3 Task 17 (spec: svc-06-identity-provider). Pins that every SVC-06
installer task carrying the Proxmox API token, raw env-file content,
cluster_env.py JSON stdout, the OpenBao admin token, the ZITADEL provisioning
token, or a child-run that bridges secrets sets no_log: true, and that no task
sets no_log: false while handling such a value. Markers tuned (as in SVC-01/09)
to avoid false positives on env-KEY-NAME presence checks and non-secret
derivations.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc06_secret_hygiene.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_ROLES = ("svc06_preflight", "svc06_provision", "svc06_bootstrap", "svc06_handoff", "svc06_clean_slate")
# The shared ZITADEL roles the org-provision token-wiring feature touches
# (spec: svc06-org-provision-token-wiring). Scanned for provision_token hygiene
# in a dedicated test below so the existing phase-role scan stays unchanged.
_ZITADEL_ROLES = ("zitadel-server", "zitadel-org-provision")

_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when", "loop", "loop_control",
    "delegate_to", "become", "vars", "tags", "args", "block", "rescue", "always", "until",
    "retries", "delay", "environment", "check_mode", "notify", "listen", "ignore_errors",
}

# Secret VALUE markers (the token itself / CI job token / derived GitLab address
# / child-run env bridging the OpenBao + ZITADEL tokens).
_SECRET_MARKERS = (
    "proxmox_api_token",
    "svc06_env_ci_job_token",
    "svc06_env_gitlab_state_address",
    "openbao_admin_token",
    "zitadel_provision_token",
)
# Register / set_fact names that BIND secret-bearing content.
_SECRET_REGISTERS = (
    "svc06_env_resolve", "svc06_cs_env_resolve", "svc06_tfvars",
    "svc06_env_slurp", "svc06_env_pairs", "svc06_env_ci_values",
)
# Child-run tasks whose ENVIRONMENT bridges a secret (OpenBao/ZITADEL token) and
# whose stdout may carry one — identified by the register name.
_SECRET_CHILD_RUNS = (
    "svc06_db_bootstrap", "svc06_zitadel_run", "svc06_org_provision_run", "svc06_db_clean_slate",
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
    if str(t.get("register", "")) in _SECRET_CHILD_RUNS:
        return True
    if _set_fact_keys(t) & set(_SECRET_REGISTERS):
        return True
    # A set_fact binding the OpenBao/ZITADEL token to a *_token fact.
    if _set_fact_keys(t) & {"svc06_bao_token", "zop_bao_token", "zop_provision_token", "zitadel_bao_token"}:
        return True
    return False


def test_token_handling_tasks_are_no_log_true():
    offenders = []
    for p, tasks in _load_task_files():
        for t in tasks:
            if _is_token_handling(t) and _no_log(t) is not True:
                offenders.append(f"  - {p.relative_to(_REPO_ROOT)} :: {t.get('name', '<unnamed>')!r}")
    assert not offenders, (
        "SECRET-HYGIENE: SVC-06 task(s) handling a token / raw env content / secret-bridging child run "
        "must be no_log: true:\n" + "\n".join(offenders)
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


# --------------------------------------------------------------------------- #
# Provision-token hygiene in the shared ZITADEL roles (spec:
# svc06-org-provision-token-wiring). The first-init-minted provisioning token
# must be no_log on every task that binds/reads/writes it, and must never be
# assigned by value on a command argv (stdin `field=-` is the only allowed form).
# --------------------------------------------------------------------------- #
_PROVISION_TOKEN_FACTS = {
    "zitadel_provision_token", "zop_provision_token", "zop_env_token", "zop_bao_token_read",
}


def _load_zitadel_task_files():
    for role in _ZITADEL_ROLES:
        d = _ROLES_DIR / role / "tasks"
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.yml")):
            tasks = []
            for doc in yaml.safe_load_all(p.read_text(encoding="utf-8")):
                if isinstance(doc, list):
                    tasks.extend(_iter_tasks(doc))
            yield p, tasks


# Message-only modules (reference a var NAME, not its value) + the boolean
# is-stored fact carry no secret value, so they need not be no_log.
_MESSAGE_ONLY_MODULES = {"ansible.builtin.fail", "fail", "ansible.builtin.assert", "assert",
                         "ansible.builtin.debug", "debug"}
_NON_VALUE_PROVISION_FACTS = {"zitadel_provision_token_stored"}


def _handles_provision_token(t: dict) -> bool:
    mods = _module_names(t)
    if mods and mods <= _MESSAGE_ONLY_MODULES:
        return False
    sf = _set_fact_keys(t)
    if sf and sf <= _NON_VALUE_PROVISION_FACTS:
        return False
    if "provision_token" in _body_text(t):
        return True
    if sf & _PROVISION_TOKEN_FACTS:
        return True
    if str(t.get("register", "")) in _PROVISION_TOKEN_FACTS:
        return True
    return False


def test_provision_token_tasks_are_no_log_true():
    offenders = []
    for p, tasks in _load_zitadel_task_files():
        for t in tasks:
            if _handles_provision_token(t) and _no_log(t) is not True:
                offenders.append(f"  - {p.relative_to(_REPO_ROOT)} :: {t.get('name', '<unnamed>')!r}")
    assert not offenders, (
        "SECRET-HYGIENE: provision_token-handling task(s) must be no_log: true:\n" + "\n".join(offenders)
    )


def test_provision_token_never_assigned_on_argv():
    for role in _ZITADEL_ROLES:
        p = _ROLES_DIR / role / "tasks" / "main.yml"
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for bad in (
            "provision_token=' ~ zitadel_provision_token",
            "provision_token={{ zitadel_provision_token",
            "provision_token=' ~ zop_provision_token",
        ):
            assert bad not in text, f"{p.name}: provision_token value on argv ({bad!r})"
