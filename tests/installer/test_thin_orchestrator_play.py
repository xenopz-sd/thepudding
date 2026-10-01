"""Offline drift-guard for the thinned SVC-07 orchestrator play (Task 10.1).

Feature: svc07-installer-simplification.

Spec: .kiro/specs/svc07-installer-simplification/ (requirements.md Requirement 1,
design.md Testing Strategy; tasks.md Task 10 / Task 10.1). This pins
**Property 1 (Thin play)** as an offline text/structure drift-guard, per the
design's Testing Strategy (structural assertions, not generative PBT).

WHAT IS ASSERTED (offline, no infra, PyYAML parse of the committed play)
------------------------------------------------------------------------
Against ``ansible/playbooks/svc-07-bootstrap.yml``:

  * It contains EXACTLY FIVE ``ansible.builtin.import_role`` invocations, naming
    exactly the five roles ``svc07_clean_slate``, ``svc07_preflight``,
    ``svc07_provision``, ``svc07_bootstrap``, ``svc07_handoff`` (Req 1.1).
  * No inline phase tasks: at the play task level the only functional module
    tasks are the ``import_role`` calls and the per-phase ``rescue`` ``fail``
    tasks — no ``command``/``shell``/``uri``/``set_fact``/``copy``/``slurp``/
    ``stat``/``lineinfile``/``blockinfile``/``pause``/``add_host`` module task
    survives inline (Req 1.1, the phases moved into the roles).
  * The per-phase ``block``/``rescue`` phase-naming is preserved: five
    ``ansible.builtin.fail`` rescue tasks whose ``msg`` names its phase — one
    each for "Phase 1", "Phase 2", "Phase 3", "Phase 4", and the Clean-Slate
    reset (Req 1.2, 1.6).

HOW IT STAYS OFFLINE
--------------------
Pure PyYAML + structure parse of the committed playbook — no ``ansible-playbook``
binary, no live Proxmox. Always runs; needs no ``requires_infra`` gate. Mirrors
the drift-guard style of ``test_unsealer_delegation.py`` (the ``_iter_tasks``
block/rescue recursion, the ``_NON_MODULE_KEYS`` module detection, repo-root via
``parents[2]``).

Validates: Requirements 1.1, 1.2, 1.6
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_thin_orchestrator_play.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _REPO_ROOT / "ansible" / "playbooks" / "svc-07-bootstrap.yml"

# The five roles the thin orchestrator must import, in no required order.
_EXPECTED_ROLES = frozenset(
    {
        "svc07_clean_slate",
        "svc07_preflight",
        "svc07_provision",
        "svc07_bootstrap",
        "svc07_handoff",
    }
)

# Keys on a task dict that are directives/containers, NOT the invoked module.
# Anything left after removing these is treated as a module name. Mirrors the
# set in test_unsealer_delegation.py.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)

# Inline-phase-task modules that MUST have moved into the roles. If any of these
# appears as a play-level task module, the play is no longer thin (a phase leaked
# back in). import_role and the rescue `fail` are the only sanctioned tasks.
_FORBIDDEN_INLINE_MODULES = frozenset(
    {
        "ansible.builtin.command", "command",
        "ansible.builtin.shell", "shell",
        "ansible.builtin.uri", "uri",
        "ansible.builtin.set_fact", "set_fact",
        "ansible.builtin.copy", "copy",
        "ansible.builtin.slurp", "slurp",
        "ansible.builtin.stat", "stat",
        "ansible.builtin.lineinfile", "lineinfile",
        "ansible.builtin.blockinfile", "blockinfile",
        "ansible.builtin.pause", "pause",
        "ansible.builtin.add_host", "add_host",
    }
)


# --------------------------------------------------------------------------- #
# Helpers — parse the play and flatten its block/rescue/always task tree.
# --------------------------------------------------------------------------- #
def _load_plays(path: Path) -> list[dict]:
    """Return the list of play dicts.

    An Ansible playbook is a single YAML document whose top-level node is a LIST
    of plays; flatten any list documents and keep the play dicts.
    """
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always.

    Yields the container node itself too (a block/rescue/always node is a task).
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _module_names(task: dict) -> set[str]:
    """The set of module keys on a task (everything that is not a directive)."""
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _orchestrator_play() -> dict:
    plays = _load_plays(_ORCHESTRATOR)
    assert len(plays) == 1, (
        f"svc-07-bootstrap.yml must be a single-play orchestrator; found {len(plays)}."
    )
    return plays[0]


def _all_tasks() -> list[dict]:
    return list(_iter_tasks(_orchestrator_play().get("tasks")))


def _import_role_names(tasks: list[dict]) -> list[str]:
    """Ordered list of role names from every ansible.builtin.import_role task."""
    names: list[str] = []
    for task in tasks:
        for key in ("ansible.builtin.import_role", "import_role"):
            spec = task.get(key)
            if isinstance(spec, dict) and "name" in spec:
                names.append(str(spec["name"]))
    return names


def _rescue_fail_tasks(tasks: list[dict]) -> list[dict]:
    """Every ansible.builtin.fail task in the tree (rescue diagnostics)."""
    out: list[dict] = []
    for task in tasks:
        if "ansible.builtin.fail" in task or "fail" in task:
            out.append(task)
    return out


def _fail_msg(task: dict) -> str:
    spec = task.get("ansible.builtin.fail") or task.get("fail") or {}
    if isinstance(spec, dict):
        return str(spec.get("msg", ""))
    return str(spec)


# =============================================================================
# Property 1 — the play imports exactly the five phase roles.
# Validates: Requirement 1.1
# =============================================================================
def test_play_imports_exactly_the_five_phase_roles():
    """Validates: Requirement 1.1 / Property 1.

    The thin orchestrator must contain exactly FIVE ``import_role`` invocations,
    naming exactly the five phase roles (no more, no fewer, none renamed).
    """
    names = _import_role_names(_all_tasks())

    assert len(names) == 5, (
        "The thin orchestrator must contain EXACTLY five import_role invocations "
        f"(one per phase role); found {len(names)}: {names}."
    )
    assert set(names) == _EXPECTED_ROLES, (
        "The five imported roles must be exactly "
        f"{sorted(_EXPECTED_ROLES)}; found {sorted(names)}."
    )
    # No role imported twice.
    assert len(set(names)) == 5, f"A phase role is imported more than once: {names}."


# =============================================================================
# Property 1 — no inline phase tasks survive in the play.
# Validates: Requirement 1.1
# =============================================================================
def test_play_has_no_inline_phase_tasks():
    """Validates: Requirement 1.1 / Property 1.

    Every functional module task at the play level must be either an
    ``import_role`` or a per-phase ``fail`` rescue diagnostic. No inline phase
    module (command/shell/uri/set_fact/copy/slurp/stat/lineinfile/blockinfile/
    pause/add_host) may have leaked back into the play — the phases live in the
    roles now.
    """
    offenders: list[str] = []
    for task in _all_tasks():
        mods = _module_names(task)
        # Pure block/rescue container nodes carry no module key of their own.
        if not mods:
            continue
        leaked = mods & _FORBIDDEN_INLINE_MODULES
        if leaked:
            offenders.append(
                f"{task.get('name', '<unnamed>')!r} -> {sorted(leaked)}"
            )

    assert not offenders, (
        "Inline phase task(s) leaked back into the thin orchestrator play — "
        "these belong in the phase roles, not the play:\n  "
        + "\n  ".join(offenders)
    )


def test_play_task_modules_are_only_import_role_or_fail():
    """Validates: Requirement 1.1 / Property 1 (stronger, allow-list form).

    Beyond the explicit forbidden list, assert the ONLY functional modules the
    play invokes are ``import_role`` and ``fail``. This catches any other module
    (e.g. a stray ``debug`` or ``command`` alias) sneaking in as an inline task.
    """
    allowed = {"ansible.builtin.import_role", "import_role",
               "ansible.builtin.fail", "fail"}
    offenders: list[str] = []
    for task in _all_tasks():
        mods = _module_names(task)
        if not mods:
            continue  # block/rescue container node
        extra = mods - allowed
        if extra:
            offenders.append(f"{task.get('name', '<unnamed>')!r} -> {sorted(extra)}")

    assert not offenders, (
        "The thin orchestrator's only functional module tasks must be "
        "import_role and the per-phase fail rescue; found other module task(s):\n  "
        + "\n  ".join(offenders)
    )


# =============================================================================
# Property 1 — per-phase block/rescue phase-naming preserved.
# Validates: Requirements 1.2, 1.6
# =============================================================================
def test_five_rescue_fail_tasks_name_their_phases():
    """Validates: Requirements 1.2, 1.6 / Property 1.

    The per-phase ``block``/``rescue`` phase-naming must survive: exactly five
    ``fail`` rescue tasks, whose messages name the four numbered phases
    ("Phase 1"..."Phase 4") plus the Clean-Slate reset.
    """
    fail_tasks = _rescue_fail_tasks(_all_tasks())

    assert len(fail_tasks) == 5, (
        "There must be exactly five per-phase rescue `fail` tasks (four install "
        f"phases + the Clean-Slate reset); found {len(fail_tasks)}."
    )

    msgs = [_fail_msg(t) for t in fail_tasks]
    joined = "\n".join(msgs)

    for phase_label in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert any(phase_label in m for m in msgs), (
            f"No rescue `fail` task names {phase_label!r}; the per-phase "
            f"phase-naming was not preserved. Messages seen:\n{joined}"
        )

    # The Clean-Slate reset must have its own named rescue too.
    assert any("Clean-Slate" in m or "Clean Slate" in m or "clean-slate" in m.lower()
               for m in msgs), (
        "No rescue `fail` task names the Clean-Slate reset; its phase-naming was "
        f"not preserved. Messages seen:\n{joined}"
    )


def test_each_phase_is_a_block_with_a_rescue():
    """Validates: Requirements 1.2, 1.6 / Property 1.

    Each phase must be wrapped in its own ``block`` that carries a ``rescue`` —
    there must be exactly five such block/rescue wrappers (one per role import),
    each rescue containing a ``fail`` diagnostic.
    """
    top_tasks = _orchestrator_play().get("tasks") or []
    block_rescue = [
        t for t in top_tasks
        if isinstance(t, dict) and "block" in t and "rescue" in t
    ]

    assert len(block_rescue) == 5, (
        "The play must have exactly five top-level block/rescue wrappers (one per "
        f"phase role); found {len(block_rescue)}."
    )

    for wrapper in block_rescue:
        # The block imports exactly one role...
        block_roles = _import_role_names(list(_iter_tasks(wrapper["block"])))
        assert len(block_roles) == 1, (
            f"Phase wrapper {wrapper.get('name')!r} must import exactly one role; "
            f"found {block_roles}."
        )
        # ...and the rescue carries a `fail` diagnostic.
        rescue_fails = _rescue_fail_tasks(list(_iter_tasks(wrapper["rescue"])))
        assert rescue_fails, (
            f"Phase wrapper {wrapper.get('name')!r} rescue must contain a `fail` "
            "diagnostic naming the phase."
        )
