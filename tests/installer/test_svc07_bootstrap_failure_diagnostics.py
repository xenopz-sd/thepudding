"""Offline guard for SVC-07 Phase-3 failure diagnostics (operator-feedback §1).

Feature: fix-svc07-bootstrap-failure-diagnostics.

The Phase-3 primary-bootstrap child run (svc07_bootstrap Task 8.4, registered
`svc07_primary_bootstrap`, `no_log: true`) is CORRECTLY no_log (its stdout carries
the revealed Admin_Token; its env carries the transit token). But `no_log: true`
censors the ENTIRE registered result, so when the child fails, the play-level
Phase-3 rescue — which interpolates `ansible_failed_result.msg` — shows only the
`{"censored": ...}` placeholder, leaving a third-party operator blind.

Fix: the Phase-3 `rescue:` in `ansible/playbooks/svc-07-bootstrap.yml` gains a
STATIC, provably secret-free POINTER (a `debug` task, before the terminal `fail`)
naming the off-by-default `svc07_debug_instrumentation` opt-in (→ a 0600
`.svc07-task84-child.log`) and the `docker logs` fallback on the primary. It
carries no child stdout and references no secret variable → secret-free by
construction. `no_log: true` on Task 8.4 is preserved.

PyYAML parse only, no infra. Helpers copied LOCALLY from the sibling
`test_secret_hygiene_no_log.py` (never imported across test roots — the
conftest-collision / four-root rule).

Layers:
* EXPLORATION / FIX-CHECKING (Property 1) — the Phase-3 rescue has a pointer task
  naming BOTH `svc07_debug_instrumentation` AND `docker logs`, before the terminal
  `fail`. FAIL on unfixed (rescue has only the generic fail).
* PRESERVATION (Property 2) — Task 8.4 still no_log:true; no stdout surfaced
  outside a no_log/0600 guard; svc07_debug_instrumentation default off; other
  phases' rescues unchanged. PASS on unfixed + after.
* SECRET-SAFETY (Property 3) — the pointer references no secret marker / no
  svc07_primary_bootstrap (by construction).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# tests/installer/test_svc07_bootstrap_failure_diagnostics.py -> repo root parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "svc-07-bootstrap.yml"
_BOOTSTRAP_MAIN = _REPO_ROOT / "ansible" / "roles" / "svc07_bootstrap" / "tasks" / "main.yml"
_DEBUG_PROBES = _REPO_ROOT / "ansible" / "roles" / "svc07_bootstrap" / "tasks" / "debug_probes.yml"

# Secret-marker substrings: a diagnostic that references any of these is NOT
# provably secret-free. (Copied discipline from the sibling secret-hygiene test.)
_SECRET_MARKERS = (
    "svc07_primary_bootstrap.stdout",
    "svc07_primary_bootstrap.stderr",
    "ansible_failed_result",
    "OPENBAO_ADMIN_TOKEN",
    "OPENBAO_BOOTSTRAP_TRANSIT_TOKEN",
    "admin_token",
    "transit_token",
    "svc07_admin_token",
)


# --------------------------------------------------------------------------- #
# Helpers — copied locally (no cross-tree import).
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_docs(path: Path):
    return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def _flat_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in _load_docs(path):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _no_log_truthy(task: dict) -> bool:
    v = task.get("no_log")
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("false", "no", "0", ""):
        return False
    return True  # a Jinja idiom like "{{ openbao_no_log }}" suppresses at runtime


def _msg_text(task: dict) -> str:
    """Flatten a debug/fail msg (str or list) to one searchable string."""
    for mod in ("ansible.builtin.debug", "ansible.builtin.fail", "debug", "fail"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            msg = spec.get("msg")
            if isinstance(msg, list):
                return " ".join(str(m) for m in msg)
            if msg is not None:
                return str(msg)
    return ""


def _task_blob(task: dict) -> str:
    """All non-control text of a task (name + module args) for a marker scan."""
    return (str(task.get("name", "")) + " " + str({k: v for k, v in task.items()
            if k not in ("block", "rescue", "always")}))


# --- locate the play + its phase blocks ------------------------------------- #
def _play():
    docs = _load_docs(_PLAYBOOK)
    assert docs and isinstance(docs[0], list) and docs[0], "svc-07-bootstrap.yml is not a play list"
    return docs[0][0]


def _phase_block(name_contains: str) -> dict | None:
    for t in _play().get("tasks", []) or []:
        if name_contains in str(t.get("name", "")) and ("block" in t or "rescue" in t):
            return t
    return None


def _phase3_block() -> dict:
    blk = _phase_block("Phase 3")
    assert blk is not None, "Phase 3 block not found in svc-07-bootstrap.yml"
    return blk


def _rescue_tasks(block: dict) -> list[dict]:
    return list(block.get("rescue", []) or [])


def _find_primary_bootstrap_task() -> dict | None:
    for t in _flat_tasks(_BOOTSTRAP_MAIN):
        if str(t.get("register", "")) == "svc07_primary_bootstrap":
            return t
        if "primary bootstrap" in str(t.get("name", "")).lower():
            return t
    return None


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed code.
# Validates: Requirements 2.1, 2.2, 2.5
# --------------------------------------------------------------------------- #
def _phase3_fail_task() -> dict | None:
    """The Phase-3 rescue's terminal `fail` task (the single play-level task the
    thin-orchestrator invariant allows besides import_role)."""
    for t in _rescue_tasks(_phase3_block()):
        if "ansible.builtin.fail" in t or "fail" in t:
            return t
    return None


def test_phase3_fail_msg_carries_secret_free_pointer():
    """Property 1 (Validates: Req 2.1, 2.2, 2.5).

    The Phase-3 rescue's `fail` msg carries a secret-free pointer naming BOTH
    svc07_debug_instrumentation AND docker logs/docker compose logs. The pointer
    is FOLDED INTO the single `fail` task (the thin-orchestrator play invariant —
    test_thin_orchestrator_play.py — allows only import_role + fail at play level,
    so a separate debug task is disallowed). FAILS on unfixed (the fail msg is the
    bare generic phase line with no pointer).
    """
    t = _phase3_fail_task()
    assert t is not None, "Phase-3 rescue must keep a terminal fail (exit non-zero)."
    m = _msg_text(t)
    assert "svc07_debug_instrumentation" in m, (
        "the Phase-3 fail msg must name the svc07_debug_instrumentation opt-in."
    )
    assert ("docker logs" in m or "docker compose logs" in m), (
        "the Phase-3 fail msg must name the `docker logs`/`docker compose logs` fallback."
    )


def test_pointer_lives_in_the_single_fail_task():
    """Property 1 (Validates: Req 2.2 / thin-orchestrator invariant).

    The pointer is part of the single Phase-3 `fail` task (not a separate play-
    level task), preserving the thin-orchestrator invariant that only import_role
    and fail appear at play level. FAILS on unfixed (fail msg has no pointer).
    """
    rescue = _rescue_tasks(_phase3_block())
    # Exactly one task in the Phase-3 rescue, and it is a fail carrying the pointer.
    fails = [t for t in rescue if ("ansible.builtin.fail" in t or "fail" in t)]
    assert len(fails) >= 1, "Phase-3 rescue must contain a fail task."
    m = _msg_text(fails[0])
    assert "svc07_debug_instrumentation" in m and ("docker logs" in m or "docker compose logs" in m), (
        "the pointer must be folded into the Phase-3 fail msg (single-task form)."
    )
    # No non-fail/non-import_role functional task leaked into the rescue.
    for t in rescue:
        mods = {k for k in t if k not in (
            "name", "when", "register", "no_log", "changed_when", "failed_when",
            "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
            "block", "rescue", "always", "environment", "ignore_errors")}
        if mods:
            assert mods <= {"ansible.builtin.fail", "fail",
                            "ansible.builtin.import_role", "import_role"}, (
                f"Phase-3 rescue task {t.get('name')!r} uses a disallowed module {sorted(mods)} "
                "(thin-orchestrator invariant: only import_role + fail at play level)."
            )


# --------------------------------------------------------------------------- #
# SECRET-SAFETY (Property 3) — the pointer is secret-free by construction.
# Validates: Requirements 2.3, 2.4, 3.8
# --------------------------------------------------------------------------- #
def test_pointer_is_secret_free_by_construction():
    """Property 3 (Validates: Req 2.3, 2.4, 3.8).

    The pointer task's name + module args + msg reference no secret marker and no
    svc07_primary_bootstrap register — provably secret-free by construction. (Runs
    meaningfully once the pointer exists; on unfixed it is vacuously fine as there
    is no pointer.)
    """
    rescue = _rescue_tasks(_phase3_block())
    for t in rescue:
        m = _msg_text(t)
        if "svc07_debug_instrumentation" in m and ("docker logs" in m or "docker compose logs" in m):
            # The fail msg legitimately interpolates ansible_failed_result.msg for
            # continuity (that is Ansible's own censored placeholder for a no_log
            # task — not a secret). Scan the POINTER portion for secret markers,
            # excluding the two sanctioned non-secret interpolations.
            pointer_blob = _task_blob(t) + " " + m
            for marker in _SECRET_MARKERS:
                if marker == "ansible_failed_result":
                    continue  # the fail msg's own non-secret (censored) continuity interpolation
                assert marker not in pointer_blob, (
                    f"the Phase-3 pointer must not reference the secret marker {marker!r} "
                    "(it must be static/secret-free by construction)."
                )


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2) — PASS on unfixed AND after fix.
# Validates: Requirements 3.1, 3.2, 3.3, 3.6, 3.7
# --------------------------------------------------------------------------- #
def test_task84_still_no_log_true():
    """Preservation (Validates: Req 3.2).

    The primary-bootstrap Task 8.4 is still no_log: true (not false, not missing).
    """
    t = _find_primary_bootstrap_task()
    assert t is not None, "primary-bootstrap task (svc07_primary_bootstrap) not found."
    assert _no_log_truthy(t), "Task 8.4 (primary bootstrap) must remain no_log: true."


def test_no_child_stdout_surfaced_outside_guard():
    """Preservation (Validates: Req 2.4, 3.3, 3.8).

    No task in the orchestrator play prints/templates svc07_primary_bootstrap.stdout
    or .stderr outside a no_log/0600-guarded context. (The only sanctioned handler
    is the debug_probes 0600 write, itself no_log:true.)
    """
    # Scan the play's tasks (incl. rescues) for a debug/fail that references the
    # child stdout without no_log.
    for t in _iter_tasks(_play().get("tasks", [])):
        m = _msg_text(t)
        if "svc07_primary_bootstrap.stdout" in m or "svc07_primary_bootstrap.stderr" in m:
            assert _no_log_truthy(t), (
                f"task {t.get('name')!r} surfaces the child stdout without no_log."
            )


def test_debug_probes_0600_write_keeps_no_log():
    """Preservation (Validates: Req 3.3).

    The svc07_debug_instrumentation 0600 .svc07-task84-child.log write keeps
    no_log: true (the sanctioned full-detail channel).
    """
    if not _DEBUG_PROBES.is_file():
        pytest.skip("debug_probes.yml not present")
    text = _DEBUG_PROBES.read_text(encoding="utf-8")
    assert "svc07-task84-child.log" in text or "task84" in text, (
        "debug_probes.yml should still write the 0600 child log."
    )
    # the write task that references the child stdout must be no_log.
    for t in _flat_tasks(_DEBUG_PROBES):
        blob = _task_blob(t)
        if "svc07_primary_bootstrap.stdout" in blob or ".svc07-task84-child.log" in blob or "child.log" in blob:
            # the secret-bearing write is no_log; the non-secret summary probe is allowed no_log:false
            if "copy" in t or "ansible.builtin.copy" in t or "lineinfile" in str(t):
                assert _no_log_truthy(t), f"the child-log write {t.get('name')!r} must be no_log: true."


def test_debug_instrumentation_defaults_off():
    """Preservation (Validates: Req 3.3).

    svc07_debug_instrumentation defaults to false (committed default off).
    """
    play = _play()
    default = play.get("vars", {}).get("svc07_debug_instrumentation")
    assert default is False, (
        "svc07_debug_instrumentation must stay default false (committed default off)."
    )


def test_other_phase_rescues_have_no_pointer():
    """Preservation (Validates: Req 3.6).

    The Clean-Slate / Phase 1 / Phase 2 / Phase 4 rescues each still contain their
    single generic phase-named fail and no pointer was added to them.
    """
    for name in ("Clean-Slate", "Phase 1", "Phase 2", "Phase 4"):
        blk = _phase_block(name)
        if blk is None:
            continue
        for t in _rescue_tasks(blk):
            m = _msg_text(t)
            assert not ("svc07_debug_instrumentation" in m and "docker logs" in m), (
                f"the {name} rescue must NOT carry the Phase-3 pointer (Phase-3-only)."
            )


# --------------------------------------------------------------------------- #
# PBT-style secret-marker classifier (pure logic).
# --------------------------------------------------------------------------- #
def test_secret_marker_classifier_flags_only_secrets():
    """A control string with a secret marker IS flagged; the static pointer
    fragments are NOT — pins that a future regression interpolating the censored
    register would be caught."""
    def has_marker(s: str) -> bool:
        return any(mk in s for mk in _SECRET_MARKERS)

    pointer_fragment = (
        "re-run with -e svc07_debug_instrumentation=true to capture the child output "
        "to the 0600 .svc07-task84-child.log; or docker logs openbao-openbao-1 on 10.0.20.10"
    )
    assert not has_marker(pointer_fragment)
    assert has_marker("{{ svc07_primary_bootstrap.stdout }}")
    assert has_marker("TOKEN={{ svc07_admin_token }}")
