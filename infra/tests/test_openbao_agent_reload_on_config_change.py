"""Offline tests for fix-openbao-agent-reload-on-config-change (§5b).

Bug: agent.hcl is bind-mounted :ro into the openbao-agent sidecar. A change to
its CONTENT does not change compose.yaml/.env, so docker_compose_v2
(recreate: auto) sees no drift and no-ops — the running (or mid-backoff) sidecar
keeps the stale config. The render task notifies no handler and
handlers/main.yml is empty, so nothing forces the sidecar to re-read agent.hcl.

Fix (mirrors the openbao_install `Restart openbao` precedent): the agent.hcl
render task notifies a concrete handler that forces a docker_compose_v2
recreate: always SCOPED to the openbao-agent service. The handler is notify-
gated on the render's `changed`, so an unchanged converge stays changed=0.

This is NOT a shared-symbol change (role-internal notify + handler only), so the
verification scope is the openbao-agent role's own offline structure + the
workload-auth offline suite — not the whole-repo shared-symbol rule.

Run: ~/venv/devinfra/bin/pytest infra/tests/test_openbao_agent_reload_on_config_change.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ROLE = _REPO_ROOT / "ansible" / "roles" / "openbao-agent"
_AGENT_TASKS = _AGENT_ROLE / "tasks" / "main.yml"
_AGENT_HANDLERS = _AGENT_ROLE / "handlers" / "main.yml"

_HANDLER_NAME = "Recreate openbao-agent sidecar"


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_docs(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _agent_hcl_render_task():
    for t in _load_docs(_AGENT_TASKS):
        tmpl = t.get("ansible.builtin.template")
        if isinstance(tmpl, dict) and str(tmpl.get("src", "")) == "agent.hcl.j2":
            return t
    return None


def _handlers() -> list[dict]:
    docs = yaml.safe_load(_AGENT_HANDLERS.read_text(encoding="utf-8"))
    return [h for h in (docs or []) if isinstance(h, dict)]


# --------------------------------------------------------------------------- #
# Property 1 — Bug Condition / Expected Behavior.
# On unfixed code these FAIL (render notifies nothing; handlers file empty).
# --------------------------------------------------------------------------- #
def test_agent_hcl_render_task_exists():
    assert _agent_hcl_render_task() is not None, (
        "the agent.hcl render task (ansible.builtin.template src=agent.hcl.j2) must exist"
    )


def test_agent_hcl_render_notifies_the_recreate_handler():
    """Property 1: a changed agent.hcl must notify the recreate handler so the
    running sidecar re-reads the new config. FAILS on unfixed code (no notify)."""
    task = _agent_hcl_render_task()
    assert task is not None
    notify = task.get("notify")
    notify_list = [notify] if isinstance(notify, str) else list(notify or [])
    assert _HANDLER_NAME in notify_list, (
        f"the agent.hcl render task must notify '{_HANDLER_NAME}' — a changed "
        "bind-mounted agent.hcl is invisible to docker_compose_v2 recreate:auto, "
        "so a handler must force the sidecar recreate (FAILS on unfixed code)"
    )


def test_recreate_handler_is_compose_native_and_service_scoped():
    """Property 1: the handler forces a docker_compose_v2 recreate:always scoped
    to ONLY the openbao-agent service (not the whole fleet, not a shell wrapper).
    FAILS on unfixed code (empty handlers file)."""
    handlers = _handlers()
    handler = next((h for h in handlers if h.get("name") == _HANDLER_NAME), None)
    assert handler is not None, (
        f"handlers/main.yml must define the '{_HANDLER_NAME}' handler "
        "(FAILS on unfixed code — the file is intentionally empty)"
    )
    spec = handler.get("community.docker.docker_compose_v2")
    assert isinstance(spec, dict), (
        "the handler must use community.docker.docker_compose_v2 (declarative, "
        "PF §9) — never a shell `docker compose restart` wrapper"
    )
    assert str(spec.get("recreate")) == "always", (
        "recreate: always forces a fresh container that re-reads agent.hcl and "
        "clears a mid-backoff sidecar"
    )
    services = spec.get("services")
    assert isinstance(services, list) and services == ["openbao-agent"], (
        "the handler must be SCOPED to services: [openbao-agent] — a whole-project "
        "recreate would needlessly bounce the healthy workload (Req 2.3)"
    )
    # References the stack dir + project name so it targets the right stack.
    body = str(handler).lower()
    assert "openbao_agent_stack_dir" in body, "handler must target openbao_agent_stack_dir"
    assert "dca_app_name" in body, "handler must target the dca_app_name project"


def test_no_shell_docker_compose_wrapper_in_role():
    """Preservation/contract: the ONLY reconcile path is declarative
    docker_compose_v2 — no shell/command `docker compose up/restart` TASK anywhere
    (Req 3.3). Checks actual task MODULE invocations, not explanatory comments."""
    tasks = _load_docs(_AGENT_TASKS) + _handlers()
    for t in tasks:
        for mod in ("ansible.builtin.shell", "ansible.builtin.command", "shell", "command"):
            spec = t.get(mod)
            if spec is None:
                continue
            spec_text = str(spec).lower()
            assert "docker compose" not in spec_text and "docker-compose" not in spec_text, (
                f"no shell/command docker-compose wrapper allowed (task: {t.get('name')!r}); "
                "the only reconcile path is declarative docker_compose_v2 (Req 3.3)"
            )


# --------------------------------------------------------------------------- #
# Property 2 — Preservation.
# --------------------------------------------------------------------------- #
def test_render_task_has_no_custom_changed_when():
    """Idempotency wiring (Req 3.1): the render task relies on the template
    module's built-in change detection (no custom changed_when), so an unchanged
    converge is changed=0 and the handler is never queued."""
    task = _agent_hcl_render_task()
    assert task is not None
    assert "changed_when" not in task, (
        "the agent.hcl render task must keep built-in template change detection "
        "(no custom changed_when) so notify-on-changed stays idempotent"
    )


def test_render_task_mode_and_dest_unchanged():
    """Preservation (Req 3.2): the render task still writes 0640 agent.hcl to the
    host config path — only the notify line is added."""
    task = _agent_hcl_render_task()
    tmpl = task["ansible.builtin.template"]
    assert str(tmpl.get("mode")) == "0640"
    assert "openbao_agent_config_host_path" in str(tmpl.get("dest"))


def test_swap_gate_depends_on_service_healthy_preserved():
    """Preservation (Req 3.4): the fail-closed swap-gate wiring is untouched."""
    text = _AGENT_TASKS.read_text(encoding="utf-8")
    assert "service_healthy" in text, "the depends_on service_healthy swap-gate must be preserved"
    assert "test" in text and "-s" in text, "the RSF-presence healthcheck must be preserved"
