"""Offline drift-guard for the thin SVC-17/18 observability orchestrator play.

Task 9.6 (spec: svc-17-18-observability). Clone of
``test_svc09_thin_orchestrator_play.py`` for the SVC-17/18 installer: pins that
``ansible/playbooks/svc-17-18-bootstrap.yml`` is a thin orchestrator of exactly
five ``import_role`` calls (the five phase roles), carries no inline phase tasks,
and preserves the per-phase block/rescue phase-naming. The clean-slate branch
(``obs_clean_slate``, gated ``when: clean_slate``) must be FIRST; Phases 1-4 each
import exactly one role (obs_preflight / obs_provision / obs_bootstrap /
obs_handoff) in a block/rescue, all gated ``when: not clean_slate``.

Pure PyYAML + structure parse of the committed playbook — no ansible-playbook
binary, no live Proxmox. Always runs.

These live in tests/installer/ (a SEPARATE pytest root from infra/tests — the
same-named conftest collision rule forbids a cross-tree import), so the
task-walking helpers are LOCAL copies of the svc09 sibling's idiom.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_obs_thin_orchestrator_play.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _REPO_ROOT / "ansible" / "playbooks" / "svc-17-18-bootstrap.yml"

_EXPECTED_ROLES = frozenset(
    {
        "obs_clean_slate",
        "obs_preflight",
        "obs_provision",
        "obs_bootstrap",
        "obs_handoff",
    }
)

_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)

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


def _load_plays(path: Path) -> list[dict]:
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _orchestrator_play() -> dict:
    plays = _load_plays(_ORCHESTRATOR)
    assert len(plays) == 1, (
        f"svc-17-18-bootstrap.yml must be a single-play orchestrator; found {len(plays)}."
    )
    return plays[0]


def _all_tasks() -> list[dict]:
    return list(_iter_tasks(_orchestrator_play().get("tasks")))


def _import_role_names(tasks: list[dict]) -> list[str]:
    names: list[str] = []
    for task in tasks:
        for key in ("ansible.builtin.import_role", "import_role"):
            spec = task.get(key)
            if isinstance(spec, dict) and "name" in spec:
                names.append(str(spec["name"]))
    return names


def _rescue_fail_tasks(tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if "ansible.builtin.fail" in t or "fail" in t]


def _fail_msg(task: dict) -> str:
    spec = task.get("ansible.builtin.fail") or task.get("fail") or {}
    return str(spec.get("msg", "")) if isinstance(spec, dict) else str(spec)


def test_play_is_localhost_orchestrator_with_any_errors_fatal():
    """Dual-role-of-Ansible: the Day-0 orchestrator runs on hosts: localhost and
    is any_errors_fatal (a phase failure stops the whole bring-up)."""
    play = _orchestrator_play()
    assert str(play.get("hosts")) == "localhost", (
        "the thin orchestrator must run on hosts: localhost (Day-0 workflow orchestrator)"
    )
    assert play.get("any_errors_fatal") is True, (
        "the thin orchestrator must set any_errors_fatal: true"
    )


def test_play_imports_exactly_the_five_phase_roles():
    names = _import_role_names(_all_tasks())
    assert len(names) == 5, (
        "The thin orchestrator must contain EXACTLY five import_role invocations; "
        f"found {len(names)}: {names}."
    )
    assert set(names) == _EXPECTED_ROLES, (
        f"The five imported roles must be exactly {sorted(_EXPECTED_ROLES)}; found {sorted(names)}."
    )


def test_play_has_no_inline_phase_tasks():
    offenders: list[str] = []
    for task in _all_tasks():
        mods = _module_names(task)
        if not mods:
            continue
        leaked = mods & _FORBIDDEN_INLINE_MODULES
        if leaked:
            offenders.append(f"{task.get('name', '<unnamed>')!r} -> {sorted(leaked)}")
    assert not offenders, (
        "Inline phase task(s) leaked into the thin orchestrator play:\n  " + "\n  ".join(offenders)
    )


def test_play_task_modules_are_only_import_role_or_fail():
    allowed = {"ansible.builtin.import_role", "import_role", "ansible.builtin.fail", "fail"}
    offenders: list[str] = []
    for task in _all_tasks():
        mods = _module_names(task)
        if not mods:
            continue
        extra = mods - allowed
        if extra:
            offenders.append(f"{task.get('name', '<unnamed>')!r} -> {sorted(extra)}")
    assert not offenders, (
        "The orchestrator's only functional modules must be import_role + fail; found:\n  "
        + "\n  ".join(offenders)
    )


def test_five_rescue_fail_tasks_name_their_phases():
    fail_tasks = _rescue_fail_tasks(_all_tasks())
    assert len(fail_tasks) == 5, (
        f"There must be exactly five per-phase rescue `fail` tasks; found {len(fail_tasks)}."
    )
    msgs = [_fail_msg(t) for t in fail_tasks]
    joined = "\n".join(msgs)
    for phase_label in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert any(phase_label in m for m in msgs), (
            f"No rescue `fail` task names {phase_label!r}. Messages:\n{joined}"
        )
    assert any("Clean-Slate" in m or "clean-slate" in m.lower() for m in msgs), (
        f"No rescue `fail` task names the Clean-Slate reset. Messages:\n{joined}"
    )


def test_each_phase_is_a_block_with_a_rescue():
    top_tasks = _orchestrator_play().get("tasks") or []
    block_rescue = [t for t in top_tasks if isinstance(t, dict) and "block" in t and "rescue" in t]
    assert len(block_rescue) == 5, (
        f"The play must have exactly five top-level block/rescue wrappers; found {len(block_rescue)}."
    )
    for wrapper in block_rescue:
        block_roles = _import_role_names(list(_iter_tasks(wrapper["block"])))
        assert len(block_roles) == 1, (
            f"Phase wrapper {wrapper.get('name')!r} must import exactly one role; found {block_roles}."
        )
        rescue_fails = _rescue_fail_tasks(list(_iter_tasks(wrapper["rescue"])))
        assert rescue_fails, (
            f"Phase wrapper {wrapper.get('name')!r} rescue must contain a `fail` diagnostic."
        )


def test_clean_slate_branch_is_first_and_gated_when_clean_slate():
    """The clean-slate branch must be the FIRST top-level task (destructive
    branch gated before any install phase runs), importing obs_clean_slate and
    gated `when: clean_slate`."""
    top_tasks = _orchestrator_play().get("tasks") or []
    assert top_tasks, "orchestrator has no tasks"
    first = top_tasks[0]
    first_roles = _import_role_names(list(_iter_tasks(first.get("block", []))))
    assert "obs_clean_slate" in first_roles, (
        "the first top-level block must be the clean-slate branch (obs_clean_slate)"
    )
    assert "clean_slate" in str(first.get("when", "")), (
        "the clean-slate branch must be gated `when: clean_slate`"
    )
    assert "not" not in str(first.get("when", "")), (
        "the clean-slate branch gate must be `when: clean_slate` (NOT `not clean_slate`)"
    )


def test_install_phases_gated_when_not_clean_slate():
    """Phases 1-4 (the four non-clean-slate wrappers) must each be gated
    `when: not clean_slate` so a clean-slate invocation skips the install path."""
    top_tasks = _orchestrator_play().get("tasks") or []
    install_wrappers = [
        t for t in top_tasks
        if isinstance(t, dict) and "block" in t
        and "obs_clean_slate" not in _import_role_names(list(_iter_tasks(t["block"])))
    ]
    assert len(install_wrappers) == 4, (
        f"expected 4 install-phase wrappers (Phases 1-4); found {len(install_wrappers)}"
    )
    for wrapper in install_wrappers:
        when = str(wrapper.get("when", ""))
        assert "not" in when and "clean_slate" in when, (
            f"install-phase wrapper {wrapper.get('name')!r} must be gated "
            f"`when: not clean_slate`; found when={when!r}"
        )
