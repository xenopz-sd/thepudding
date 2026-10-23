"""Offline drift-guard for the thin SVC-01 orchestrator play.

Slice-2 Task 11 (spec: svc-01-sql-database). Pins that
`ansible/playbooks/svc-01-bootstrap.yml` is a thin orchestrator of exactly five
`import_role` calls (the five phase roles), carries no inline phase tasks, and
preserves the per-phase block/rescue phase-naming with the clean-slate branch first.

Pure PyYAML + structure parse — no ansible-playbook binary. Always runs.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc01_thin_orchestrator_play.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _REPO_ROOT / "ansible" / "playbooks" / "svc-01-bootstrap.yml"

_EXPECTED_ROLES = frozenset(
    {"svc01_clean_slate", "svc01_preflight", "svc01_provision", "svc01_bootstrap", "svc01_handoff"}
)

_NON_MODULE_KEYS = frozenset(
    {"name", "when", "register", "no_log", "changed_when", "failed_when", "loop", "loop_control",
     "delegate_to", "become", "vars", "tags", "args", "block", "rescue", "always", "until",
     "retries", "delay", "environment", "check_mode", "notify", "listen", "ignore_errors"}
)

_FORBIDDEN_INLINE_MODULES = frozenset(
    {"ansible.builtin.command", "command", "ansible.builtin.shell", "shell", "ansible.builtin.uri",
     "uri", "ansible.builtin.set_fact", "set_fact", "ansible.builtin.copy", "copy",
     "ansible.builtin.slurp", "slurp", "ansible.builtin.stat", "stat", "ansible.builtin.lineinfile",
     "lineinfile", "ansible.builtin.blockinfile", "blockinfile", "ansible.builtin.pause", "pause",
     "ansible.builtin.add_host", "add_host"}
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
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _module_names(t: dict) -> set[str]:
    return {k for k in t if k not in _NON_MODULE_KEYS}


def _play() -> dict:
    plays = _load_plays(_ORCHESTRATOR)
    assert len(plays) == 1, f"svc-01-bootstrap.yml must be a single-play orchestrator; found {len(plays)}"
    return plays[0]


def _all_tasks() -> list[dict]:
    return list(_iter_tasks(_play().get("tasks")))


def _import_role_names(tasks) -> list[str]:
    names = []
    for t in tasks:
        for k in ("ansible.builtin.import_role", "import_role"):
            spec = t.get(k)
            if isinstance(spec, dict) and "name" in spec:
                names.append(str(spec["name"]))
    return names


def _rescue_fail_tasks(tasks) -> list[dict]:
    return [t for t in tasks if "ansible.builtin.fail" in t or "fail" in t]


def _fail_msg(t: dict) -> str:
    spec = t.get("ansible.builtin.fail") or t.get("fail") or {}
    return str(spec.get("msg", "")) if isinstance(spec, dict) else str(spec)


def test_play_imports_exactly_the_five_phase_roles():
    names = _import_role_names(_all_tasks())
    assert len(names) == 5 and set(names) == _EXPECTED_ROLES, (
        f"must import exactly {sorted(_EXPECTED_ROLES)}; found {sorted(names)}"
    )


def test_play_has_no_inline_phase_tasks():
    offenders = []
    for t in _all_tasks():
        mods = _module_names(t)
        if mods and (mods & _FORBIDDEN_INLINE_MODULES):
            offenders.append(f"{t.get('name', '<unnamed>')!r} -> {sorted(mods & _FORBIDDEN_INLINE_MODULES)}")
    assert not offenders, "inline phase task(s) leaked into the thin orchestrator:\n  " + "\n  ".join(offenders)


def test_play_modules_are_only_import_role_or_fail():
    allowed = {"ansible.builtin.import_role", "import_role", "ansible.builtin.fail", "fail"}
    offenders = []
    for t in _all_tasks():
        mods = _module_names(t)
        if mods and (mods - allowed):
            offenders.append(f"{t.get('name', '<unnamed>')!r} -> {sorted(mods - allowed)}")
    assert not offenders, "only import_role + fail allowed:\n  " + "\n  ".join(offenders)


def test_five_rescue_fail_tasks_name_their_phases():
    fails = _rescue_fail_tasks(_all_tasks())
    assert len(fails) == 5, f"expected five per-phase rescue fail tasks; found {len(fails)}"
    msgs = [_fail_msg(t) for t in fails]
    for label in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert any(label in m for m in msgs), f"no rescue names {label!r}"
    assert any("Clean-Slate" in m or "clean-slate" in m.lower() for m in msgs), "no rescue names Clean-Slate"


def test_clean_slate_branch_is_first():
    top = _play().get("tasks") or []
    assert top, "orchestrator has no tasks"
    first_roles = _import_role_names(list(_iter_tasks(top[0].get("block", []))))
    assert "svc01_clean_slate" in first_roles, "the first top-level block must be the clean-slate branch"
