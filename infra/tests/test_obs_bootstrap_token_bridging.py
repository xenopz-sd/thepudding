"""Offline role-shape tests for the SVC-17/18 one-command bring-up OpenBao
admin-token bridging bugfix.

Spec: .kiro/specs/fix-obs-bootstrap-first-run-transient/
      (bugfix.md, design.md, tasks.md).

Root cause (captured live on the throwaway cluster, no_log temporarily off):
the orchestrated one-command bring-up bridged OPENBAO_ADMIN_TOKEN only via
`lookup('env', 'OPENBAO_ADMIN_TOKEN')` from the ORCHESTRATOR PROCESS ENV, but
the token lives in the cluster env file (loaded into an Ansible fact by
cluster_env.py, which does NOT pass the token through). So the child
observability.yml run received BAO_TOKEN='' and the delegated
`bao kv put secret/observability/grafana-admin` ran unauthenticated -> HTTP 403.

This module pins the fixed token-bridging contract so the regression cannot
silently return:

  Property 1 (Bug Condition): the Phase-3 child-run environment bridges the
    PREFLIGHT-RESOLVED `obs_openbao_admin_token` fact, AND obs_preflight resolves
    OPENBAO_ADMIN_TOKEN from the cluster env file AND fails closed when OpenBao is
    wired but the token is empty. FAILS on the unfixed tree (no env-file
    resolution, no fail-closed gate, bridge references only the env lookup).

  Property 2 (Preservation): the Phase-3 bridge STILL falls back to
    lookup('env','OPENBAO_ADMIN_TOKEN') (process-env precedence preserved); the
    OpenBao reads/seed stay gated on the openbao-addr being non-empty
    (dev/offline preserved); every token-carrying task stays no_log and never
    puts the token on an argv `=` assignment (secret hygiene preserved).

Pure PyYAML + text parse; no ansible-playbook binary, no live infra.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_obs_bootstrap_token_bridging.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"
_BOOTSTRAP_TASKS = _ROLES / "obs_bootstrap" / "tasks" / "main.yml"
_BOOTSTRAP_DEFAULTS = _ROLES / "obs_bootstrap" / "defaults" / "main.yml"
_PREFLIGHT_TASKS = _ROLES / "obs_preflight" / "tasks" / "main.yml"
_GRAFANA_TASKS = _ROLES / "observability_stack" / "tasks" / "grafana.yml"

_TOKEN_VAR = "OPENBAO_ADMIN_TOKEN"
_RESOLVED_FACT = "obs_openbao_admin_token"

_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when", "loop",
    "loop_control", "delegate_to", "become", "vars", "tags", "args", "block",
    "rescue", "always", "until", "retries", "delay", "environment", "check_mode",
    "notify", "listen", "ignore_errors",
}


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _no_log_true(t: dict) -> bool:
    return t.get("no_log") in (True, "true", "True", "yes")


def _child_run_task(tasks: list[dict]) -> dict:
    """The Phase-3 observability.yml child-run command task (carries the
    OPENBAO_ADMIN_TOKEN child ENV)."""
    for t in tasks:
        env = t.get("environment")
        if isinstance(env, dict) and _TOKEN_VAR in env:
            return t
    raise AssertionError(
        "no obs_bootstrap task carries an OPENBAO_ADMIN_TOKEN child environment"
    )


# ---------------------------------------------------------------------------
# Property 1 — Bug Condition (FAILS on the unfixed tree)
# ---------------------------------------------------------------------------

def test_phase3_bridge_references_preflight_resolved_fact():
    """The Phase-3 child-run must bridge the PREFLIGHT-RESOLVED fact, not rely
    solely on the process-env lookup. Unfixed tree: the expression is only
    `lookup('env','OPENBAO_ADMIN_TOKEN')` -> FAILS."""
    task = _child_run_task(_load_tasks(_BOOTSTRAP_TASKS))
    expr = str(task["environment"][_TOKEN_VAR])
    assert _RESOLVED_FACT in expr, (
        "Phase-3 child-run environment.OPENBAO_ADMIN_TOKEN must bridge the "
        f"preflight-resolved '{_RESOLVED_FACT}' fact (so the token resolved from "
        "the cluster env file reaches the child), not only the process-env "
        f"lookup. Got: {expr!r}"
    )


def test_preflight_resolves_token_from_cluster_env_file():
    """obs_preflight must resolve OPENBAO_ADMIN_TOKEN from the cluster env file
    (slurp + extract) into the resolved fact. Unfixed tree: no such resolution
    exists -> FAILS."""
    text = _PREFLIGHT_TASKS.read_text(encoding="utf-8")
    assert _RESOLVED_FACT in text, (
        f"obs_preflight must bind the '{_RESOLVED_FACT}' fact (token resolved "
        "from the cluster env file with a process-env fallback)."
    )
    assert _TOKEN_VAR in text, (
        "obs_preflight must reference OPENBAO_ADMIN_TOKEN when resolving it from "
        "the cluster env file."
    )
    # The resolution reads the cluster env file (slurp of cluster_env_file).
    assert "slurp" in text and "cluster_env_file" in text, (
        "obs_preflight must read the cluster env file (ansible.builtin.slurp of "
        "cluster_env_file) to resolve OPENBAO_ADMIN_TOKEN as the single source "
        "of truth."
    )


def test_preflight_fails_closed_when_wired_but_token_empty():
    """obs_preflight must fail closed (Phase 1) when OpenBao is wired
    (obs_openbao_addr non-empty) but the resolved token is empty. Unfixed tree:
    no such gate -> FAILS."""
    tasks = _load_tasks(_PREFLIGHT_TASKS)
    gate_found = False
    for t in tasks:
        module = {k for k in t if k not in _NON_MODULE_KEYS}
        if not ({"ansible.builtin.assert", "assert", "ansible.builtin.fail",
                 "fail"} & module):
            continue
        blob = (str(t.get("when", "")) + yaml.safe_dump(t)).lower()
        # The gate is keyed on the OpenBao-wired condition and a token-presence
        # signal (either the resolved fact directly, or its non-secret
        # presence projection — both are acceptable).
        if "obs_openbao_addr" in blob and (
            _RESOLVED_FACT.lower() in blob
            or "obs_openbao_token_present" in blob
        ):
            gate_found = True
            break
    assert gate_found, (
        "obs_preflight must contain a fail-closed gate: when obs_openbao_addr is "
        f"non-empty but the resolved token ('{_RESOLVED_FACT}' / "
        "obs_openbao_token_present) is empty, fail with a non-secret remedy "
        "(before any guest is provisioned)."
    )


def test_bootstrap_defaults_declare_resolved_token_default():
    """The resolved-token fact has a safe default so the role is self-contained
    for standalone / --syntax-check. Unfixed tree: default absent -> FAILS."""
    defaults = yaml.safe_load(_BOOTSTRAP_DEFAULTS.read_text(encoding="utf-8")) or {}
    assert _RESOLVED_FACT in defaults, (
        f"obs_bootstrap defaults must declare '{_RESOLVED_FACT}' (default empty)."
    )


# ---------------------------------------------------------------------------
# Property 2 — Preservation (baseline holds on the unfixed tree too)
# ---------------------------------------------------------------------------

def test_phase3_bridge_preserves_process_env_fallback():
    """Process-env precedence (the SVC-06/07 convention) must remain: the bridge
    still references lookup('env','OPENBAO_ADMIN_TOKEN')."""
    task = _child_run_task(_load_tasks(_BOOTSTRAP_TASKS))
    expr = str(task["environment"][_TOKEN_VAR])
    assert "lookup(" in expr and _TOKEN_VAR in expr, (
        "the Phase-3 bridge must keep the lookup('env','OPENBAO_ADMIN_TOKEN') "
        f"fallback so an exported token still works. Got: {expr!r}"
    )


def test_child_run_task_is_no_log():
    """The token-carrying Phase-3 child-run task stays no_log."""
    task = _child_run_task(_load_tasks(_BOOTSTRAP_TASKS))
    assert _no_log_true(task), (
        "the Phase-3 observability.yml child-run task must stay no_log: true "
        "(it carries the admin token in the child ENV)."
    )


def test_grafana_openbao_reads_gated_on_addr_and_no_log():
    """Dev/offline preservation: the grafana OpenBao reads/seed stay gated on the
    openbao addr being non-empty, and every OpenBao bao-CLI task stays no_log."""
    tasks = _load_tasks(_GRAFANA_TASKS)
    bao_tasks = []
    for t in tasks:
        blob = yaml.safe_dump(t)
        # The argv is a folded Jinja list literal, e.g. "... 'kv', 'get'] ...";
        # match the quoted-token forms the role actually uses.
        if "bao" in blob and ("'kv', 'get'" in blob or "'kv', 'put'" in blob):
            bao_tasks.append(t)
    assert bao_tasks, "expected grafana.yml to contain delegated bao kv get/put tasks"
    for t in bao_tasks:
        assert _no_log_true(t), (
            "every grafana OpenBao bao-CLI task must stay no_log: true "
            f"(task: {t.get('name')!r})"
        )
    # At least one of the reads is gated on the openbao addr length (dev/offline
    # skip path).
    gated = any(
        "observability_openbao_addr" in str(t.get("when", ""))
        for t in bao_tasks
    )
    assert gated, (
        "the grafana OpenBao reads must stay gated on observability_openbao_addr "
        "length (dev/offline skips them)."
    )


def test_token_never_on_argv_assignment():
    """Secret hygiene: the admin token must never appear on an argv `=`
    assignment in the token-bridging tasks (ENV only)."""
    for path in (_BOOTSTRAP_TASKS, _PREFLIGHT_TASKS):
        text = path.read_text(encoding="utf-8")
        # The token value is only ever referenced as an ENV value or a fact;
        # it must never be interpolated into an argv element like
        # `BAO_TOKEN=... ` on the command line of a -e assignment.
        assert f"-e {_TOKEN_VAR}=" not in text and f"{_TOKEN_VAR}=\"{{{{" not in text, (
            f"{path} must not place OPENBAO_ADMIN_TOKEN on an argv `=` assignment; "
            "the token travels only in the task ENV (no_log)."
        )
