"""Offline drift-guard test for debug consolidation (Property 8, Task 7.3).

Feature: svc07-installer-simplification.

Spec: ``.kiro/specs/svc07-installer-simplification/`` — design.md
"Debug-Instrumentation Model (plan item 4)" and Correctness Property 8
("Debug consolidated & gated").

**Validates: Requirements 4.1, 4.2, 4.3, 4.5, 4.6, 4.7**

WHAT IS ASSERTED (offline, no infra, no ansible-playbook run)
-------------------------------------------------------------
The rework consolidates every ``svc07_debug_instrumentation`` probe into ONE
guarded include per phase and removes all inline debug tasks from the functional
loops (design plan item 4, Req 4.1). This pure-text + structural drift guard
pins that consolidation so it cannot silently regress to interleaved inline
debug probes:

  * ``ansible/roles/svc07_bootstrap/tasks/main.yml`` — the functional Phase-3
    bootstrap loop (common run, unsealer bootstrap, seal-token capture, primary
    bootstrap, drift guard, admin-token persist) carries NO inline debug task.
    The ONLY ``svc07_debug_instrumentation`` construct in the file is a single
    ``import_tasks: debug_probes.yml`` task guarded by
    ``when: svc07_debug_instrumentation | default(false) | bool``.
  * ``ansible/roles/svc07_bootstrap/tasks/debug_probes.yml`` exists and holds the
    moved Phase-3 probes (Task 8.4 child-log + non-secret rc/length/TOKEN=
    summary).
  * ``ansible/playbooks/svc07-health.yml`` — the functional Phase-4 Stage-1 /
    Stage-2 health loops carry NO inline debug probe. The ONLY
    ``svc07_debug_instrumentation`` construct is a single
    ``import_tasks: tasks/svc07-health-debug-probes.yml`` guarded by
    ``when: svc07_debug_instrumentation | default(false) | bool``.
  * ``ansible/playbooks/tasks/svc07-health-debug-probes.yml`` exists and holds
    the moved Phase-4 probes (``docker ps -a`` listing, Stage-1/Stage-2 probe
    log-appends, final result dumps).

Together these pin Req 4.1 (one guarded include per phase, none inline in the
functional loops), Req 4.2 (every probe gated by the flag), Req 4.3 (a flag-off
run reaches zero debug tasks — established structurally by the fact that the only
gated construct is the guarded import), Req 4.5/4.6 (secret-safe vs
secret-bearing posture lives in the include files, which exist), and Req 4.7
(the capability is retained, not deleted — the include files exist and are
imported).

HOW IT STAYS OFFLINE
--------------------
Pure text + YAML structure inspection of the reworked role/play files — no
``ansible-playbook`` run, no live Proxmox. This mirrors the drift-guard half of
``test_swap_gate.py`` / ``test_tls_insecure.py`` (repo-root via ``parents[2]``,
read the files, assert structure). PyYAML ships with the devinfra venv (Ansible
depends on it), so the structural parse always runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# tests/installer/test_debug_consolidated.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_BOOTSTRAP_MAIN = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_bootstrap" / "tasks" / "main.yml"
)
_BOOTSTRAP_DEBUG = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_bootstrap" / "tasks" / "debug_probes.yml"
)
_HEALTH_PLAY = _REPO_ROOT / "ansible" / "playbooks" / "svc07-health.yml"
_HEALTH_DEBUG = (
    _REPO_ROOT / "ansible" / "playbooks" / "tasks" / "svc07-health-debug-probes.yml"
)

_FLAG = "svc07_debug_instrumentation"
# The guard every consolidated debug include must carry.
_GUARD_RE = re.compile(
    r"svc07_debug_instrumentation\s*\|\s*default\(\s*false\s*\)\s*\|\s*bool"
)
# The Ansible module keys that constitute a "debug probe" (as opposed to a
# functional task). A guarded include is import_tasks; the probes themselves are
# debug / copy(secret log) / command|shell(docker ps etc).
_PROBE_MODULE_KEYS = (
    "ansible.builtin.debug",
    "ansible.builtin.copy",
    "ansible.builtin.command",
    "ansible.builtin.shell",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_role_tasks(path: Path) -> list[dict]:
    """Load a role tasks file (a top-level YAML list of task dicts)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, list), f"{path} is not a YAML task list"
    return [t for t in data if isinstance(t, dict)]


def _load_play_tasks(path: Path) -> list[dict]:
    """Load the ``tasks:`` list from a single-play playbook file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, f"{path} is not a non-empty play list"
    play = data[0]
    assert isinstance(play, dict), f"{path} first play is not a mapping"
    tasks = play.get("tasks") or []
    assert isinstance(tasks, list), f"{path} play has no tasks list"
    return [t for t in tasks if isinstance(t, dict)]


def _guarded_debug_imports(tasks: list[dict]) -> list[dict]:
    """Tasks that are an ``import_tasks`` guarded by the debug flag."""
    out = []
    for t in tasks:
        has_import = "ansible.builtin.import_tasks" in t or "import_tasks" in t
        when = t.get("when", "")
        when_text = when if isinstance(when, str) else " ".join(map(str, when))
        if has_import and _FLAG in when_text and _GUARD_RE.search(when_text):
            out.append(t)
    return out


def _flag_gated_tasks(tasks: list[dict]) -> list[dict]:
    """Every task whose own ``when:`` references the debug flag."""
    out = []
    for t in tasks:
        when = t.get("when", "")
        when_text = when if isinstance(when, str) else " ".join(map(str, when))
        if _FLAG in when_text:
            out.append(t)
    return out


# =============================================================================
# svc07_bootstrap Phase-3 consolidation
# =============================================================================
def test_bootstrap_main_has_single_guarded_debug_import_and_no_inline_probe():
    """The Phase-3 functional loop carries exactly one guarded debug include.

    Feature: svc07-installer-simplification, Property 8 (Req 4.1, 4.2).

    The ONLY svc07_debug_instrumentation-gated construct in
    svc07_bootstrap/tasks/main.yml must be a single ``import_tasks:
    debug_probes.yml`` guarded by
    ``svc07_debug_instrumentation | default(false) | bool``. No inline debug task
    (debug/copy/command/shell) may itself be gated by the flag — those live only
    in the imported include.
    """
    tasks = _load_role_tasks(_BOOTSTRAP_MAIN)

    guarded_imports = _guarded_debug_imports(tasks)
    assert len(guarded_imports) == 1, (
        "svc07_bootstrap/tasks/main.yml must contain EXACTLY ONE guarded "
        f"import_tasks for the debug probes; found {len(guarded_imports)}. "
        "The Phase-3 debug instrumentation must be consolidated into a single "
        "guarded include (Req 4.1)."
    )

    # That single guarded construct must import debug_probes.yml.
    imp = guarded_imports[0]
    target = imp.get("ansible.builtin.import_tasks") or imp.get("import_tasks")
    assert target == "debug_probes.yml", (
        "the guarded debug include in svc07_bootstrap/tasks/main.yml must import "
        f"'debug_probes.yml'; found {target!r}."
    )

    # Every flag-gated task in main.yml must BE that import — no inline debug
    # probe (debug/copy/command/shell) may carry the flag in the functional loop.
    for t in _flag_gated_tasks(tasks):
        is_the_import = ("ansible.builtin.import_tasks" in t) or ("import_tasks" in t)
        assert is_the_import, (
            "svc07_bootstrap/tasks/main.yml has an INLINE debug task gated by "
            f"{_FLAG} in the functional bootstrap loop: {t.get('name')!r}. All "
            "Phase-3 debug probes must be moved into debug_probes.yml (Req 4.1)."
        )


def test_bootstrap_functional_tasks_carry_no_debug_module_probe():
    """None of the functional Phase-3 tasks is a flag-gated debug module task.

    Feature: svc07-installer-simplification, Property 8 (Req 4.1).

    Belt-and-suspenders: assert no debug/copy/command/shell task in the
    functional loop is guarded by the flag (they must all be in the include).
    """
    tasks = _load_role_tasks(_BOOTSTRAP_MAIN)
    for t in tasks:
        when = t.get("when", "")
        when_text = when if isinstance(when, str) else " ".join(map(str, when))
        if _FLAG not in when_text:
            continue
        for key in _PROBE_MODULE_KEYS:
            assert key not in t, (
                f"functional task {t.get('name')!r} in svc07_bootstrap/tasks/"
                f"main.yml is a flag-gated {key} probe inline in the bootstrap "
                "loop; it must live in debug_probes.yml instead (Req 4.1)."
            )


def test_bootstrap_debug_probes_file_exists_with_moved_phase3_probes():
    """debug_probes.yml exists and holds the moved Phase-3 probes.

    Feature: svc07-installer-simplification, Property 8 (Req 4.5, 4.6, 4.7).

    The include must exist (capability retained, not deleted — Req 4.7) and
    contain the Phase-3 probes: the secret-bearing Task 8.4 child stdout log
    (no_log: true) and the non-secret rc/length/TOKEN= marker summary
    (no_log: false).
    """
    assert _BOOTSTRAP_DEBUG.is_file(), (
        "svc07_bootstrap/tasks/debug_probes.yml must exist — the Phase-3 debug "
        "capability is consolidated here, not deleted (Req 4.7)."
    )
    probes = _load_role_tasks(_BOOTSTRAP_DEBUG)
    assert probes, "debug_probes.yml must contain at least one probe task."

    text = _BOOTSTRAP_DEBUG.read_text(encoding="utf-8")

    # The secret-bearing child-stdout log write must exist and be no_log: true
    # (Req 4.6). It references the Task-8.4 registered result.
    assert "svc07_primary_bootstrap" in text, (
        "the Phase-3 debug include must reference the Task 8.4 registered result "
        "(svc07_primary_bootstrap) — it is the moved Phase-3 probe."
    )
    copy_probe = next(
        (t for t in probes if "ansible.builtin.copy" in t or "copy" in t), None
    )
    assert copy_probe is not None, (
        "debug_probes.yml must contain the secret-bearing child-stdout log write "
        "(a copy task) — Req 4.6."
    )
    assert copy_probe.get("no_log") is True, (
        "the secret-bearing child-stdout log write in debug_probes.yml MUST be "
        "no_log: true (it contains the revealed Admin_Token) — Req 4.6."
    )

    # The non-secret rc/length/TOKEN= summary must exist and be no_log: false
    # (Req 4.5).
    debug_probe = next(
        (t for t in probes if "ansible.builtin.debug" in t or "debug" in t), None
    )
    assert debug_probe is not None, (
        "debug_probes.yml must contain the non-secret rc/length/TOKEN= summary "
        "debug task — Req 4.5."
    )
    assert debug_probe.get("no_log") is False, (
        "the non-secret summary probe in debug_probes.yml MUST be no_log: false "
        "so the operator can read it (it prints no secret) — Req 4.5."
    )
    assert "TOKEN=" in text, (
        "the non-secret summary must report the TOKEN= marker presence — Req 4.4."
    )


# =============================================================================
# svc07-health.yml Phase-4 consolidation
# =============================================================================
def test_health_play_has_single_guarded_debug_import_and_no_inline_probe():
    """The Phase-4 functional loops carry exactly one guarded debug include.

    Feature: svc07-installer-simplification, Property 8 (Req 4.1, 4.2).

    The ONLY svc07_debug_instrumentation-gated construct in svc07-health.yml must
    be a single ``import_tasks: tasks/svc07-health-debug-probes.yml`` guarded by
    ``svc07_debug_instrumentation | default(false) | bool``. No inline debug
    probe may be gated by the flag inside the functional Stage-1/Stage-2 loops.
    """
    tasks = _load_play_tasks(_HEALTH_PLAY)

    guarded_imports = _guarded_debug_imports(tasks)
    assert len(guarded_imports) == 1, (
        "svc07-health.yml must contain EXACTLY ONE guarded import_tasks for the "
        f"Phase-4 debug probes; found {len(guarded_imports)}. The Phase-4 debug "
        "instrumentation must be consolidated into a single guarded include "
        "(Req 4.1)."
    )

    imp = guarded_imports[0]
    target = imp.get("ansible.builtin.import_tasks") or imp.get("import_tasks")
    assert target == "tasks/svc07-health-debug-probes.yml", (
        "the guarded debug include in svc07-health.yml must import "
        f"'tasks/svc07-health-debug-probes.yml'; found {target!r}."
    )

    # Every flag-gated task in the play must BE that import — no inline probe.
    for t in _flag_gated_tasks(tasks):
        is_the_import = ("ansible.builtin.import_tasks" in t) or ("import_tasks" in t)
        assert is_the_import, (
            "svc07-health.yml has an INLINE debug task gated by "
            f"{_FLAG} in the functional health loop: {t.get('name')!r}. All "
            "Phase-4 debug probes must be moved into "
            "tasks/svc07-health-debug-probes.yml (Req 4.1)."
        )


def test_health_functional_stages_carry_no_debug_module_probe():
    """None of the functional Stage-1/Stage-2 tasks is a flag-gated probe.

    Feature: svc07-installer-simplification, Property 8 (Req 4.1).

    Belt-and-suspenders: the Stage-1 ``docker inspect`` and Stage-2
    ``bao status`` functional loops (and their asserts) carry no flag-gated
    debug module task — those all live in the include.
    """
    tasks = _load_play_tasks(_HEALTH_PLAY)
    for t in tasks:
        when = t.get("when", "")
        when_text = when if isinstance(when, str) else " ".join(map(str, when))
        if _FLAG not in when_text:
            continue
        for key in _PROBE_MODULE_KEYS:
            assert key not in t, (
                f"functional task {t.get('name')!r} in svc07-health.yml is a "
                f"flag-gated {key} probe inline in the health loop; it must live "
                "in tasks/svc07-health-debug-probes.yml instead (Req 4.1)."
            )


def test_health_debug_probes_file_exists_with_moved_phase4_probes():
    """svc07-health-debug-probes.yml exists and holds the moved Phase-4 probes.

    Feature: svc07-installer-simplification, Property 8 (Req 4.4, 4.5, 4.7).

    The include must exist (capability retained, not deleted — Req 4.7) and
    contain the Phase-4 probes: the ``docker ps -a`` container listing, the
    Stage-1/Stage-2 probe log-appends, and the final Stage-1/Stage-2 result
    dumps — all secret-safe (no_log: false, Req 4.5).
    """
    assert _HEALTH_DEBUG.is_file(), (
        "ansible/playbooks/tasks/svc07-health-debug-probes.yml must exist — the "
        "Phase-4 debug capability is consolidated here, not deleted (Req 4.7)."
    )
    probes = _load_role_tasks(_HEALTH_DEBUG)
    assert probes, "svc07-health-debug-probes.yml must contain at least one probe."

    text = _HEALTH_DEBUG.read_text(encoding="utf-8")

    # The moved Phase-4 probes: docker ps -a listing + the two per-stage probe
    # log-appends + the final Stage-1/Stage-2 result dumps (Req 4.4).
    assert "docker" in text and "ps" in text, (
        "the Phase-4 include must contain the `docker ps -a` container listing "
        "probe (Req 4.4)."
    )
    assert "svc07-stage1-probe.log" in text, (
        "the Phase-4 include must contain the Stage-1 probe log-append (Req 4.4)."
    )
    assert "svc07-stage2-probe.log" in text, (
        "the Phase-4 include must contain the Stage-2 probe log-append (Req 4.4)."
    )
    assert "svc07_primary_container_running" in text, (
        "the Phase-4 include must dump the final Stage-1 `docker inspect` result "
        "(svc07_primary_container_running) — Req 4.4."
    )
    assert "svc07_primary_status" in text, (
        "the Phase-4 include must dump the final Stage-2 `bao status` result "
        "(svc07_primary_status) — Req 4.4."
    )

    # Every probe in the Phase-4 include is secret-safe (Req 4.5): these touch
    # only docker ps / docker inspect / bao status output (no secret material),
    # so none may be no_log: true.
    for t in probes:
        assert t.get("no_log") is not True, (
            f"Phase-4 debug probe {t.get('name')!r} is no_log: true, but Phase-4 "
            "probes expose only non-secret container/seal status and must be "
            "no_log: false so the operator can read them (Req 4.5)."
        )


# =============================================================================
# repo-wide: no flag-gated inline probe leaks outside the two guarded includes
# =============================================================================
def test_flag_only_appears_in_guarded_imports_and_include_files():
    """Every svc07_debug_instrumentation reference is a guarded include, not
    an inline probe, across the reworked bootstrap role and health play.

    Feature: svc07-installer-simplification, Property 8 (Req 4.1, 4.2, 4.3).

    In the two functional files (svc07_bootstrap/tasks/main.yml and
    svc07-health.yml) the flag may appear ONLY on the guarded import_tasks; the
    include files themselves (debug_probes.yml, svc07-health-debug-probes.yml)
    need no per-task guard because the single import guard covers them. This pins
    Req 4.3: with the flag off, zero probe tasks run (the only gated construct is
    the guarded import).
    """
    for functional_file in (_BOOTSTRAP_MAIN, _HEALTH_PLAY):
        text = functional_file.read_text(encoding="utf-8")
        # Every line that mentions the flag must be part of a guarded when: on an
        # import (already asserted structurally above); here confirm the flag is
        # only ever used with the default(false) | bool guard form in these files
        # — never as a bare inline `when: svc07_debug_instrumentation`.
        for line in text.splitlines():
            if _FLAG not in line:
                continue
            # The flag line must carry the full guard form. (Comments explaining
            # the flag are fine — they are not `when:` lines, so skip pure
            # comment lines.)
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert _GUARD_RE.search(line), (
                f"{functional_file.name} references {_FLAG} outside the "
                f"default(false) | bool guard form: {line.strip()!r}. In the "
                "functional files the flag may only appear on the guarded "
                "import_tasks (Req 4.2)."
            )
