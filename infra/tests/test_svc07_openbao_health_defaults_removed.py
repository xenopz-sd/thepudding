"""Offline (Tier-1) guard: the dead openbao_health_* defaults stay removed.
Spec: fix-openbao-health-defaults-cleanup.

The init guard was migrated from an `ansible.builtin.uri` GET to `docker exec bao
status`, orphaning four `openbao_health_*` defaults. This guard asserts they remain
absent from the role defaults and unreferenced by any task/template/handler, and that
the guard still reads `bao status` via `docker exec`.

Run:
  PYTHONPATH=infra/platform-foundation/scripts \
    ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_health_defaults_removed.py -q
"""
from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_TASKS_DIR = _ROLE / "tasks"
_TEMPLATES_DIR = _ROLE / "templates"
_HANDLERS_DIR = _ROLE / "handlers"

_DEAD_VARS = (
    "openbao_health_addr",
    "openbao_health_path",
    "openbao_health_validate_certs",
    "openbao_health_timeout",
)


def test_dead_vars_absent_from_defaults():
    data = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8")) or {}
    for v in _DEAD_VARS:
        assert v not in data, f"{v} must be removed from defaults/main.yml (dead config)"


def test_dead_vars_unreferenced_in_role_logic():
    """No task/template/handler may USE the removed vars. A historical mention in a
    comment/tombstone is allowed; a `{{ ... }}` or bare-key use is not."""
    offenders: list[str] = []
    for d in (_TASKS_DIR, _TEMPLATES_DIR, _HANDLERS_DIR):
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.lstrip()
                # skip pure comment lines (historical notes are fine)
                if stripped.startswith("#") or stripped.startswith("{#"):
                    continue
                for v in _DEAD_VARS:
                    if v in line:
                        offenders.append(f"{f.relative_to(_ROLE)}:{lineno}: {line.strip()}")
    assert not offenders, "removed health vars are still USED (not just mentioned):\n" + "\n".join(offenders)


def test_guard_reads_status_via_docker_exec():
    """The init guard's behaviour is unchanged: it reads `bao status` via docker exec."""
    tasks = yaml.safe_load((_TASKS_DIR / "main.yml").read_text(encoding="utf-8"))
    guard = next(
        (t for t in tasks if "read bao status" in str(t.get("name", "")).lower()),
        None,
    )
    assert guard is not None, "the init guard 'read bao status' task must exist"
    cmd = guard.get("ansible.builtin.command", {})
    # argv is a Jinja folded scalar (a template string); normalise whitespace before
    # matching the literal tokens it builds.
    raw = cmd.get("argv") if isinstance(cmd, dict) else None
    argv = "".join(str(raw).split()) if raw else ""
    assert "'docker'" in argv and "'exec'" in argv and "'status'" in argv, (
        "the guard must read `bao status` via `docker exec` (no uri/health-var read)"
    )
