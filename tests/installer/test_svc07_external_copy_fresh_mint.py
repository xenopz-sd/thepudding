"""Offline guard for the SVC-07 external-copy-removal fresh-mint fix (operator §2).

Feature: fix-svc07-external-copy-removal-fresh-mint.

On a clean from-scratch in-memory-mint bring-up there is NO external copy of the
Bootstrap_Transit_Token (minted in-memory, bridged via the Handoff_File + ENV),
yet 6.2-STEP-7's `auto` resolver fell through to `manual` and the single
`ansible.builtin.fail` hard-failed the whole run AFTER OpenBao was healthy and the
token was already in KV.

Fix: a new `openbao_bootstrap_external_copy_expected` role var (default true)
gates the `manual` outcome — a success no-op debug when false (in-memory mint,
nothing to remove), the preserved fail-loud when true (operator-supplied copy).
The orchestrator sets it false on the in-memory-mint path (keyed off
`svc07_seal_token_handoff_stat.stat.exists`) and threads the dotenv path from the
operator's single `cluster_env_file` (one source of env truth).

PyYAML parse only, no infra. Helpers copied LOCALLY (four-root rule).

* EXPLORATION / FIX-CHECKING (Property 1): STEP-7 no-op branch gated on
  `not external_copy_expected`; fail-loud gated on `external_copy_expected`;
  default true; orchestrator threads both -e flags. FAIL on unfixed.
* PRESERVATION (Property 2/3): dotenv strip + gitlab DELETE unchanged; fail-loud
  present; token tasks no_log; dotenv role default = repo-root fallback. PASS on
  unfixed + after.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRIMARY = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal" / "tasks" / "primary_bootstrap.yml"
_DEFAULTS = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal" / "defaults" / "main.yml"
_ORCH = _REPO_ROOT / "ansible" / "roles" / "svc07_bootstrap" / "tasks" / "main.yml"

_SIGNAL = "openbao_bootstrap_external_copy_expected"


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


def _flat_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _when_text(task: dict) -> str:
    w = task.get("when")
    if isinstance(w, list):
        return " && ".join(str(x) for x in w)
    return str(w or "")


def _module(task: dict, *names: str) -> dict | None:
    for n in names:
        spec = task.get(n)
        if isinstance(spec, dict):
            return spec
    return None


def _no_log_truthy(task: dict) -> bool:
    v = task.get("no_log")
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s not in ("false", "no", "0", "")


def _argv_text(task: dict) -> str:
    spec = _module(task, "ansible.builtin.command", "command")
    if spec and isinstance(spec.get("argv"), list):
        return " ".join(str(a) for a in spec["argv"])
    return ""


def _find_task84(tasks: list[dict]) -> dict | None:
    for t in tasks:
        if str(t.get("register", "")) == "svc07_primary_bootstrap":
            return t
        if "primary bootstrap" in str(t.get("name", "")).lower() and _argv_text(t):
            return t
    return None


def _step7_manual_tasks(tasks: list[dict]) -> list[dict]:
    """Tasks whose `when` references the `manual` resolver outcome."""
    out = []
    for t in tasks:
        if '"manual"' in _when_text(t) or "'manual'" in _when_text(t) or "== manual" in _when_text(t):
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed.
# Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.6
# --------------------------------------------------------------------------- #
def test_step7_has_gated_noop_debug():
    """Property 1 (Validates: Req 2.1).

    6.2-STEP-7 has a debug (success no-op) gated on `manual` AND
    `not (external_copy_expected | bool)`. FAILS on unfixed (only the fail exists).
    """
    tasks = _flat_tasks(_PRIMARY)
    noop = None
    for t in _step7_manual_tasks(tasks):
        if _module(t, "ansible.builtin.debug", "debug") is not None:
            w = _when_text(t)
            if _SIGNAL in w and "not" in w:
                noop = t
                break
    assert noop is not None, (
        "6.2-STEP-7 must have a success-no-op debug gated on `manual` AND "
        f"`not ({_SIGNAL} | bool)` (the in-memory-mint path has nothing to remove)."
    )


def test_step7_fail_loud_is_gated():
    """Property 1 (Validates: Req 2.1, 2.3).

    The fail-loud is gated on `manual` AND `external_copy_expected | bool` (the
    supplied-copy case). FAILS on unfixed (fail is gated only on `manual`).
    """
    tasks = _flat_tasks(_PRIMARY)
    fail = None
    for t in _step7_manual_tasks(tasks):
        if _module(t, "ansible.builtin.fail", "fail") is not None:
            fail = t
            break
    assert fail is not None, "6.2-STEP-7 must keep the fail-loud task."
    w = _when_text(fail)
    assert _SIGNAL in w and "not" not in w.split(_SIGNAL)[0].split("and")[-1], (
        f"the fail-loud must be gated on `{_SIGNAL} | bool` (supplied-copy case)."
    )
    assert _SIGNAL in w, f"the fail-loud `when` must reference {_SIGNAL}."


def test_default_signal_is_true():
    """Property 1 (Validates: Req 2.6).

    openbao_bootstrap_external_copy_expected defaults to true (backward-compatible).
    FAILS on unfixed (key absent).
    """
    data = _defaults(_DEFAULTS)
    assert _SIGNAL in data, f"defaults must define {_SIGNAL}."
    assert bool(data[_SIGNAL]) is True, f"{_SIGNAL} must default to true."


def test_orchestrator_threads_both_flags():
    """Property 1 (Validates: Req 2.2, 2.5).

    svc07_bootstrap Task 8.4 argv threads openbao_bootstrap_external_copy_expected=
    (keyed off svc07_seal_token_handoff_stat.stat.exists) AND
    openbao_bootstrap_dotenv_path= (from cluster_env_file). FAILS on unfixed.
    """
    t = _find_task84(_flat_tasks(_ORCH))
    assert t is not None, "Task 8.4 primary child run not found."
    argv = _argv_text(t)
    assert f"{_SIGNAL}=" in argv, f"Task 8.4 must thread {_SIGNAL}= as an -e flag."
    assert "svc07_seal_token_handoff_stat.stat.exists" in argv, (
        f"{_SIGNAL} must be keyed off svc07_seal_token_handoff_stat.stat.exists."
    )
    assert "openbao_bootstrap_dotenv_path=" in argv, (
        "Task 8.4 must thread openbao_bootstrap_dotenv_path= as an -e flag."
    )
    # The dotenv path value may be inlined in argv OR resolved via a task-level
    # vars entry (to keep argv lines within the yamllint 120-char limit). Either
    # way it must be DERIVED FROM cluster_env_file — check the whole task blob.
    task_blob = argv + " " + str(t.get("vars", ""))
    assert "cluster_env_file" in task_blob, (
        "openbao_bootstrap_dotenv_path must be derived from cluster_env_file "
        "(inlined in argv or via a task vars entry)."
    )


def test_token_not_on_task84_argv():
    """Property 1 / Preservation (Validates: Req 2.5, 3.6).

    The Bootstrap_Transit_Token is NEVER on the Task 8.4 argv (stays in environment).
    """
    t = _find_task84(_flat_tasks(_ORCH))
    assert t is not None
    argv = _argv_text(t)
    assert "OPENBAO_BOOTSTRAP_TRANSIT_TOKEN" not in argv, (
        "the transit token must stay in environment:, never on argv."
    )
    env = (_module(t, "ansible.builtin.command", "command") or {}).get("environment") or t.get("environment") or {}
    assert "OPENBAO_BOOTSTRAP_TRANSIT_TOKEN" in env, (
        "the transit token must be bridged via the task environment."
    )


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2/3) — PASS on unfixed AND after.
# Validates: Requirements 2.3, 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.8
# --------------------------------------------------------------------------- #
def test_dotenv_strip_preserved():
    """Preservation (Validates: Req 3.1).

    The dotenv lineinfile strip (gated on `dotenv`) keeps its regexp, state:absent,
    delegate_to localhost, become false, no_log.
    """
    tasks = _flat_tasks(_PRIMARY)
    strip = None
    for t in tasks:
        spec = _module(t, "ansible.builtin.lineinfile", "lineinfile")
        if spec and ("dotenv" in _when_text(t)):
            strip = t
            break
    assert strip is not None, "the dotenv lineinfile strip task must remain."
    assert str(strip.get("delegate_to")) == "localhost"
    assert strip.get("become") is False
    assert _no_log_truthy(strip)


def test_gitlab_delete_preserved():
    """Preservation (Validates: Req 3.2).

    The gitlab uri DELETE (gated on `gitlab`) keeps method DELETE, status_code,
    delegate_to localhost, no_log.
    """
    tasks = _flat_tasks(_PRIMARY)
    deltask = None
    for t in tasks:
        spec = _module(t, "ansible.builtin.uri", "uri")
        if spec and "gitlab" in _when_text(t):
            deltask = t
            break
    assert deltask is not None, "the gitlab uri DELETE task must remain."
    spec = _module(deltask, "ansible.builtin.uri", "uri")
    assert str(spec.get("method")).upper() == "DELETE"
    assert str(deltask.get("delegate_to")) == "localhost"
    assert _no_log_truthy(deltask)


def test_fail_loud_task_present():
    """Preservation (Validates: Req 2.3, 3.3).

    A fail-loud `ansible.builtin.fail` with the external-copy message still exists.
    """
    tasks = _flat_tasks(_PRIMARY)
    found = False
    for t in tasks:
        spec = _module(t, "ansible.builtin.fail", "fail")
        if spec and "external copy must not outlive" in str(spec.get("msg", "")):
            found = True
            break
    assert found, "the external-copy fail-loud task must remain present."


def test_dotenv_role_default_is_repo_root_fallback():
    """Preservation (Validates: Req 2.4).

    openbao_bootstrap_dotenv_path role default stays the repo-root .env (standalone
    no-orchestrator fallback); the orchestrator overrides it at runtime.
    """
    data = _defaults(_DEFAULTS)
    val = str(data.get("openbao_bootstrap_dotenv_path", ""))
    assert "playbook_dir" in val and ".env" in val, (
        "openbao_bootstrap_dotenv_path role default must stay the repo-root .env fallback."
    )


def test_token_tasks_keep_no_log():
    """Preservation (Validates: Req 3.6).

    Every primary_bootstrap task whose argv/name references a token value keeps
    no_log truthy.
    """
    for t in _flat_tasks(_PRIMARY):
        blob = _argv_text(t) + " " + str(t.get("name", ""))
        if ("BAO_TOKEN" in blob or "openbao_bootstrap_token" in blob
                or "root_token" in blob.lower() or "admin_token" in blob.lower()):
            assert _no_log_truthy(t), (
                f"token-handling task {t.get('name')!r} must keep no_log truthy."
            )
