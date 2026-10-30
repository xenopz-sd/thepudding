"""Offline check-mode idempotency structural assertions (both roles).

Feature: openbao-workload-auth (tasks.md Task 6.3).

Task 6.3 asks for an assertion that both roles report ``changed=0`` for an
already-converged state in check mode. True ``ansible-playbook --check`` with
mocked ``docker exec`` reads is impractical offline here — ``ansible`` /
``ansible-lint`` are NOT installed in the project venv (see the sibling
``openbao_project_onboard`` README, whose task 9.2 handles this identically).
So, exactly as ``openbao_project_onboard`` does, this is implemented as a
STRUCTURAL assertion that every mutating task is gated by a check-then-write read
+ a ``needs_write``/``when`` guard (so a converged state is a no-op), plus the
data role's idempotent-render shape. The AUTHORITATIVE live ``changed=0`` proof
is the ``requires-infra`` **task 8.2** (idempotency + divergence live test).

WHAT IS ASSERTED (offline, PyYAML parse only, no infra)
-------------------------------------------------------
Control role ``openbao_workload_approle`` (Req 1.3, 2.6):
1. Every state-mutating task is EITHER a ``bao`` enable/write guarded by a
   ``when:`` on a prior read's decision fact (``*_present`` / ``*_needs_write``),
   OR the by-design always-mint Secret_ID write + its dependent handoff tasks
   (an un-rereadable, TTL/num_uses-bounded credential — design.md trade-offs),
   OR an unconditional READ (``changed_when: false``) that feeds a decision.
2. A read task exists for each decision fact the writes are gated on
   (``openbao_approle_present`` and ``openbao_workload_role_needs_write`` are
   both produced by a prior ``set_fact`` derived from a ``bao`` read) — i.e. the
   check genuinely precedes the write.

Data role ``openbao-agent`` (Req 4.6):
3. The ``agent.hcl`` render is a single ``ansible.builtin.template`` task (a
   byte-identical re-render on unchanged input reports ``changed=0`` natively),
   NOT a ``copy`` with inline ``content`` or a ``shell``/``command`` that would
   churn. The stack itself is reconciled declaratively by ``docker-compose-app``
   (``include_role``), not a ``shell: docker compose up`` (which is never
   idempotent).

Helpers are COPIED locally (same-named-``conftest`` cross-tree import ban),
adapted from the sibling structural tests. PyYAML parse only; no secret value is
embedded or printed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_CONTROL_ROLE = "openbao_workload_approle"
_AGENT_ROLE = "openbao-agent"


# --------------------------------------------------------------------------- #
# Helpers (copied locally)
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_task_file(path: Path):
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    tasks: list[dict] = []
    for doc in docs:
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _name(task: dict) -> str:
    return str(task.get("name", ""))


def _body_text(task: dict) -> str:
    parts: list[str] = [_name(task)]
    env = task.get("environment")
    if env is not None:
        parts.append(str(env))
    for mod in _module_names(task):
        spec = task.get(mod)
        parts.append(str(mod))
        if isinstance(spec, dict):
            for k, v in spec.items():
                parts.append(str(k))
                parts.append(str(v))
        else:
            parts.append(str(spec))
    return " ".join(parts).lower()


def _changed_when_false(task: dict) -> bool:
    val = task.get("changed_when")
    if val is None:
        return False
    return str(val).strip().lower() in ("false", "no", "0")


# A task is state-mutating unless it is a read/assert/debug/set_fact or is
# explicitly marked changed_when: false.
_READONLY_MODULES = {
    "ansible.builtin.assert", "assert",
    "ansible.builtin.debug", "debug",
    "ansible.builtin.set_fact", "set_fact",
    "ansible.builtin.slurp", "slurp",
    "ansible.builtin.include_role", "include_role",
    "ansible.builtin.import_role", "import_role",
}


def _is_readonly_task(task: dict) -> bool:
    mods = _module_names(task)
    if mods & _READONLY_MODULES:
        return True
    if _changed_when_false(task):
        return True
    return False


# --------------------------------------------------------------------------- #
# Control role — every mutation is gated or a sanctioned always-mint.
# --------------------------------------------------------------------------- #
def test_control_role_every_mutation_is_gated_or_sanctioned_mint():
    """Req 1.3 / 2.6 — a converged state is a no-op offline-provable structure.
    Live changed=0 proof is requires-infra task 8.2."""
    path = _ROLES_DIR / _CONTROL_ROLE / "tasks" / "main.yml"
    tasks = _load_task_file(path)

    offenders = []
    for task in tasks:
        if _is_readonly_task(task):
            continue
        body = _body_text(task)
        when = task.get("when")
        when_text = str(when).lower() if when is not None else ""

        # (a) gated by a prior read's decision fact.
        if when is not None and ("present" in when_text or "needs_write" in when_text):
            continue
        # (b) the by-design always-mint Secret_ID write (an un-rereadable
        #     credential) + its dependent facts/handoff/scrub, which are each
        #     gated on a channel selector or carry changed_when tied to rc.
        if "secret-id" in body and ("'write'" in body or "write" in body):
            continue
        # (c) handoff tasks gated by the handoff channel selector.
        if when is not None and "openbao_workload_handoff" in when_text:
            continue
        # (d) the creds-dir/template file writes are part of the (b) handoff and
        #     are themselves gated on the handoff channel (file) selector.
        if when is not None and "handoff" in when_text:
            continue
        offenders.append(_name(task) or "<unnamed>")

    assert not offenders, (
        "control role has state-mutating task(s) that are neither gated by a "
        "check-then-write decision fact, nor the by-design Secret_ID mint, nor "
        f"gated by the handoff channel selector (Req 1.3/2.6): {offenders}"
    )


def test_control_role_decision_facts_are_set_before_the_writes_they_gate():
    """The check genuinely precedes the write: each decision fact a write is
    gated on is produced by a set_fact earlier in the file (from a prior bao
    read)."""
    path = _ROLES_DIR / _CONTROL_ROLE / "tasks" / "main.yml"
    tasks = _load_task_file(path)

    # positions where each decision fact is first SET.
    set_pos: dict[str, int] = {}
    for idx, task in enumerate(tasks):
        if "set_fact" in _module_names(task) or "ansible.builtin.set_fact" in _module_names(task):
            spec = task.get("set_fact") or task.get("ansible.builtin.set_fact") or {}
            if isinstance(spec, dict):
                for key in spec:
                    if key in ("openbao_approle_present", "openbao_workload_role_needs_write"):
                        set_pos.setdefault(key, idx)

    assert "openbao_approle_present" in set_pos, (
        "expected a set_fact producing openbao_approle_present (the approle/ "
        "enable decision) in the control role"
    )
    assert "openbao_workload_role_needs_write" in set_pos, (
        "expected a set_fact producing openbao_workload_role_needs_write (the "
        "role write decision) in the control role"
    )

    # every write gated on a decision fact appears AFTER that fact is set.
    for idx, task in enumerate(tasks):
        when = task.get("when")
        if when is None:
            continue
        when_text = str(when).lower()
        for fact in ("openbao_approle_present", "openbao_workload_role_needs_write"):
            if fact.lower() in when_text:
                assert idx > set_pos[fact], (
                    f"task {_name(task)!r} is gated on {fact} but appears before "
                    "that fact is set — the read must precede the write it gates."
                )


# --------------------------------------------------------------------------- #
# Data role — the agent.hcl render is idempotent-by-construction.
# --------------------------------------------------------------------------- #
def test_agent_role_renders_agent_hcl_via_template_module():
    """Req 4.6 — agent.hcl is rendered by ansible.builtin.template (a
    byte-identical re-render reports changed=0 natively), not a churning
    shell/command/copy-with-inline-content."""
    path = _ROLES_DIR / _AGENT_ROLE / "tasks" / "main.yml"
    tasks = _load_task_file(path)

    render_tasks = [
        t
        for t in tasks
        if ("template" in _module_names(t) or "ansible.builtin.template" in _module_names(t))
        and "agent.hcl" in _body_text(t)
    ]
    assert len(render_tasks) == 1, (
        "expected exactly one ansible.builtin.template task rendering agent.hcl "
        f"in {_AGENT_ROLE}; found {len(render_tasks)}"
    )
    # the render uses the template module with the committed src.
    t = render_tasks[0]
    spec = t.get("template") or t.get("ansible.builtin.template") or {}
    assert str(spec.get("src", "")).strip() == "agent.hcl.j2"


def test_agent_role_has_no_churning_compose_shell():
    """Req 4.6 / 7.5 — the stack is reconciled declaratively via
    docker-compose-app (include_role), never a non-idempotent
    `shell: docker compose up`."""
    path = _ROLES_DIR / _AGENT_ROLE / "tasks" / "main.yml"
    tasks = _load_task_file(path)

    for task in tasks:
        mods = _module_names(task)
        if mods & {"shell", "ansible.builtin.shell", "command", "ansible.builtin.command"}:
            body = _body_text(task)
            assert "docker compose" not in body and "docker-compose" not in body, (
                f"{_AGENT_ROLE} task {_name(task)!r} shells out to docker compose "
                "— the stack must be reconciled by docker-compose-app "
                "(include_role), which is idempotent, not a shell up."
            )

    includes = [
        t
        for t in tasks
        if ("include_role" in _module_names(t) or "ansible.builtin.include_role" in _module_names(t))
    ]
    names = []
    for t in includes:
        spec = t.get("include_role") or t.get("ansible.builtin.include_role") or {}
        if isinstance(spec, dict):
            names.append(str(spec.get("name", "")))
    assert "docker-compose-app" in names, (
        f"{_AGENT_ROLE} must include the docker-compose-app role to reconcile the "
        "Compose stack declaratively (idempotent)."
    )
