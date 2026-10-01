"""Offline drift-guard for the platform_devmachine bucket (Task 8.2 / Property 5).

Feature: platform-prerequisites-bootstrap, Task 8.2 — Property 5
(DevMachine emit-by-default).

Spec: .kiro/specs/platform-prerequisites-bootstrap/ (requirements.md
Requirement 5, design.md "Component 6: platform_devmachine" + "Property 5:
DevMachine emit-by-default"; tasks.md Task 8.2). This pins Property 5 as an
offline text/structure drift-guard, per the design's Testing Strategy
(structural PyYAML parse, not a live run).

WHAT IS ASSERTED (offline, PyYAML parse only, no infra)
-------------------------------------------------------
Against ``ansible/roles/platform_devmachine/tasks/main.yml`` (implemented in
Task 8.1):

  1. Every MUTATING devmachine task — the one that adds the ``ip route``, the
     one that writes the ifupdown persistence file (+ its drop-in directory),
     and the one that ``blockinfile``s the ``~/.ssh/config`` identity pin —
     carries ``when: devmachine_apply`` (directly on the task or via a
     block-level ``when``) (Req 5.2, Property 5).

  2. The EMIT task(s) (``debug`` / print) do NOT carry
     ``when: devmachine_apply`` — emit is the DEFAULT and runs unconditionally,
     mutating nothing (Req 5.1, Property 5).

  3. The emitted commands reference cluster.env-resolved vars (the served VLAN
     id(s) / Proxmox LAN IP / SSH identity file) rather than hardcoded literals
     — i.e. the emit ``debug`` body is composed from the facts bound off the
     ``cluster_env.py`` resolve (``platform_devmachine_lan_ip`` /
     ``platform_devmachine_served_vlans`` / ``platform_devmachine_ssh_identity``
     or the per-VLAN artefacts built from them), so the printed route/ssh_config
     is directly runnable and never a static literal (Req 5.4).

HOW IT STAYS OFFLINE
--------------------
Pure PyYAML + structure parse of the committed role file — no
``ansible-playbook`` binary, no live workstation, no Proxmox. Always runs; needs
no ``requires_infra`` gate. Mirrors the drift-guard style of
``test_thin_orchestrator_play.py`` / ``test_secret_hygiene_no_log.py`` (the
``_iter_tasks`` block/rescue recursion, the ``_NON_MODULE_KEYS`` module
detection, repo-root via ``parents[2]``).

The helpers below are COPIED locally (NOT imported across the installer test
tree — the conftest-collision rule) from the sibling structural tests.

Validates: Requirements 5.1, 5.2 / Property 5
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_platform_devmachine_guard.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEVMACHINE_MAIN = (
    _REPO_ROOT / "ansible" / "roles" / "platform_devmachine" / "tasks" / "main.yml"
)

# The opt-in apply gate every mutating devmachine task must reference.
_APPLY_GATE_VAR = "devmachine_apply"

# Facts bound off the cluster_env.py resolve (the "cluster.env-resolved vars").
# The emit debug must be composed from these (directly or via the per-VLAN
# artefact list built from them), never from hardcoded literals (Req 5.4).
_RESOLVED_VARS = (
    "platform_devmachine_lan_ip",
    "platform_devmachine_served_vlans",
    "platform_devmachine_ssh_identity",
    "platform_devmachine_artefacts",
)

# Keys on a task dict that are directives/containers, NOT the invoked module.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)

# Modules that MUTATE the developer's workstation state (network/ssh/fs). A task
# invoking one of these is a "mutating devmachine task" and MUST be gated on
# devmachine_apply. `command`/`shell` count only when they run with `become`
# (the route add), since the cluster.env resolve is a read-only `command`.
_FS_NET_MUTATING_MODULES = frozenset(
    {
        "ansible.builtin.copy", "copy",
        "ansible.builtin.file", "file",
        "ansible.builtin.blockinfile", "blockinfile",
        "ansible.builtin.lineinfile", "lineinfile",
        "ansible.builtin.template", "template",
    }
)
_COMMAND_MODULES = frozenset(
    {"ansible.builtin.command", "command", "ansible.builtin.shell", "shell"}
)
_DEBUG_MODULES = frozenset({"ansible.builtin.debug", "debug"})


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally (no cross-tree import; conftest-collision rule).
# --------------------------------------------------------------------------- #
def _iter_tasks_with_block_when(tasks, inherited_when=None):
    """Yield (task_dict, effective_when_texts) for every task recursively.

    Descends block/rescue/always. ``effective_when_texts`` is the list of
    stringified ``when`` clauses in force for that task: the task's own ``when``
    PLUS any ``when`` inherited from an enclosing ``block`` — so a mutating task
    gated only at the block level is still recognised as gated.
    """
    inherited_when = list(inherited_when or [])
    for task in tasks or []:
        if not isinstance(task, dict):
            continue

        own_when = task.get("when")
        own_when_texts: list[str] = []
        if own_when is not None:
            if isinstance(own_when, list):
                own_when_texts = [str(w) for w in own_when]
            else:
                own_when_texts = [str(own_when)]

        effective = inherited_when + own_when_texts
        yield task, effective

        for key in ("block", "rescue", "always"):
            if key in task:
                # A block/rescue/always node's own `when` propagates to children.
                yield from _iter_tasks_with_block_when(task[key], effective)


def _load_tasks(path: Path) -> list[dict]:
    """Return the flat ordered list of task dicts from a role tasks/ file."""
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    tasks: list[tuple[dict, list[str]]] = []
    for doc in docs:
        if isinstance(doc, list):
            tasks.extend(_iter_tasks_with_block_when(doc))
    return tasks


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _has_apply_gate(when_texts) -> bool:
    """True if any effective ``when`` clause references ``devmachine_apply``."""
    return any(_APPLY_GATE_VAR in text for text in when_texts)


def _is_become(task: dict) -> bool:
    val = task.get("become")
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "yes", "1")


def _is_mutating_task(task: dict) -> bool:
    """True if the task changes workstation network/ssh/fs state.

    - Any fs/net-mutating module (copy/file/blockinfile/lineinfile/template).
    - A command/shell that runs with ``become`` (the privileged ``ip route add``)
      — a non-become command (the read-only cluster.env resolve) is NOT mutating.
    """
    mods = _module_names(task)
    if mods & _FS_NET_MUTATING_MODULES:
        return True
    if (mods & _COMMAND_MODULES) and _is_become(task):
        return True
    return False


def _is_emit_task(task: dict) -> bool:
    """True for a debug/print task (the emit-mode output)."""
    return bool(_module_names(task) & _DEBUG_MODULES)


def _task_body_text(task: dict) -> str:
    """A flat string of the task's module args (to check var references)."""
    parts: list[str] = [_task_name(task)]
    for mod in _module_names(task):
        spec = task.get(mod)
        parts.append(str(mod))
        if isinstance(spec, dict):
            for key, value in spec.items():
                parts.append(str(key))
                parts.append(str(value))
        else:
            parts.append(str(spec))
    return " ".join(parts)


def _all_tasks() -> list[tuple[dict, list[str]]]:
    return _load_tasks(_DEVMACHINE_MAIN)


# =============================================================================
# Sanity — the scan actually reached the role file.
# =============================================================================
def test_devmachine_task_file_parsed():
    """Guard against a silent no-op: the role file must parse to real tasks."""
    assert _DEVMACHINE_MAIN.is_file(), (
        f"platform_devmachine tasks/main.yml not found at "
        f"{_DEVMACHINE_MAIN.relative_to(_REPO_ROOT)} — the role layout changed; "
        "repoint this guard."
    )
    tasks = _all_tasks()
    assert len(tasks) > 5, (
        "The platform_devmachine task scan parsed suspiciously few tasks "
        f"({len(tasks)}); a parse regression may be hiding tasks."
    )


# =============================================================================
# Assertion 1 — every mutating devmachine task is gated on devmachine_apply.
# Validates: Requirement 5.2 / Property 5
# =============================================================================
def test_every_mutating_task_is_gated_on_devmachine_apply():
    """Validates: Requirement 5.2 / Property 5.

    Every task that mutates the developer's workstation — the ``ip route add``
    (privileged command), the ifupdown persistence directory + file writes, and
    the ``~/.ssh/config`` ``blockinfile`` — MUST carry ``when: devmachine_apply``
    (on the task itself or inherited from an enclosing block). Emit is the
    default; a mutation running unconditionally would change the workstation with
    no explicit GO.
    """
    mutating = [(t, w) for (t, w) in _all_tasks() if _is_mutating_task(t)]

    assert mutating, (
        "No mutating devmachine tasks were detected — the apply-mode route/"
        "persistence/ssh_config tasks are expected in "
        f"{_DEVMACHINE_MAIN.relative_to(_REPO_ROOT)}. Either the role changed "
        "shape or the mutating-module classification needs updating."
    )

    ungated = [
        f"  - {_task_name(t)!r} (when={w})"
        for (t, w) in mutating
        if not _has_apply_gate(w)
    ]

    assert not ungated, (
        "EMIT-DEFAULT REGRESSION: found mutating devmachine task(s) NOT gated on "
        f"`{_APPLY_GATE_VAR}` — apply-mode mutations must run only after the "
        "explicit GO (`-e devmachine_apply=true`), never by default (Req 5.2, "
        "Property 5). Ungated mutating task(s):\n" + "\n".join(ungated)
    )


# =============================================================================
# Assertion 2 — the emit (debug) task(s) are NOT gated on devmachine_apply.
# Validates: Requirement 5.1 / Property 5
# =============================================================================
def test_emit_tasks_are_not_gated_on_devmachine_apply():
    """Validates: Requirement 5.1 / Property 5.

    The EMIT ``debug`` task(s) run by DEFAULT and mutate nothing, so they must
    NOT carry ``when: devmachine_apply`` — otherwise emit mode would print
    nothing, defeating the review/share purpose of the default mode.
    """
    emit = [(t, w) for (t, w) in _all_tasks() if _is_emit_task(t)]

    assert emit, (
        "No emit (debug) task found in "
        f"{_DEVMACHINE_MAIN.relative_to(_REPO_ROOT)} — emit mode is the DEFAULT "
        "and must print the runnable route/persistence/ssh_config commands "
        "(Req 5.1)."
    )

    gated = [
        f"  - {_task_name(t)!r} (when={w})"
        for (t, w) in emit
        if _has_apply_gate(w)
    ]

    assert not gated, (
        "EMIT-DEFAULT REGRESSION: an emit (debug) task is gated on "
        f"`{_APPLY_GATE_VAR}` — emit is the DEFAULT and must run unconditionally "
        "(mutating nothing). A gated emit would print nothing at the default "
        "posture (Req 5.1, Property 5). Offending emit task(s):\n"
        + "\n".join(gated)
    )


# =============================================================================
# Assertion 3 — the emitted commands reference cluster.env-resolved vars.
# Validates: Requirement 5.4 / Property 5
# =============================================================================
def test_emit_references_cluster_env_resolved_vars():
    """Validates: Requirement 5.4 / Property 5.

    The emitted route/persistence/ssh_config must be composed from the
    cluster.env-resolved facts (the served VLAN id(s) / Proxmox LAN IP / SSH
    identity file — or the per-VLAN artefact list built from them), NOT from
    hardcoded literals. Assert the emit ``debug`` body references at least the
    Proxmox LAN IP and the served-VLAN/artefact facts, so a regression that
    hardcodes an IP or subnet is caught.
    """
    emit_bodies = [
        _task_body_text(t) for (t, _w) in _all_tasks() if _is_emit_task(t)
    ]
    assert emit_bodies, (
        "No emit (debug) task to inspect for cluster.env-resolved var references."
    )
    joined = "\n".join(emit_bodies)

    # The `via` gateway (Proxmox host LAN IP) MUST come from the resolved fact.
    assert "platform_devmachine_lan_ip" in joined, (
        "EMIT REGRESSION: the emit debug does not reference "
        "`platform_devmachine_lan_ip` (the cluster.env-resolved Proxmox host LAN "
        "IP used as the route `via` gateway) — the emitted route must use the "
        "resolved value, not a hardcoded IP (Req 5.4)."
    )

    # The served VLAN(s) / per-VLAN artefacts (route_cmd, subnet, persist_lines,
    # ssh_block) MUST come from the resolved facts too. Accept any of the
    # cluster.env-derived fact names, since the route/persistence lines are
    # composed via the per-VLAN artefact list built off served_vlans.
    assert any(var in joined for var in _RESOLVED_VARS), (
        "EMIT REGRESSION: the emit debug does not reference any cluster.env-"
        f"resolved fact ({', '.join(_RESOLVED_VARS)}) — the served VLAN "
        "subnet(s) and SSH identity must be composed from the resolved values, "
        "not hardcoded literals (Req 5.4)."
    )

    # And it must NOT hardcode a concrete VLAN-20 subnet literal in place of the
    # composed-from-served-VLANs form (a common regression). The subnet is built
    # as '10.0.' ~ item ~ '.0/24' from the artefacts, so a bare '10.0.20.0/24'
    # literal in the emit body would signal a hardcode.
    assert "10.0.20.0/24" not in joined, (
        "EMIT REGRESSION: the emit debug hardcodes the literal subnet "
        "'10.0.20.0/24' instead of composing it per served VLAN from the "
        "resolved artefacts (Req 5.4)."
    )
