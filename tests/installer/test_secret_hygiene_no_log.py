"""Offline drift-guard for SVC-07 installer secret hygiene (Property 3).

Feature: svc07-installer-simplification, Task 5.4 — Property 3 (secret hygiene).

Spec: .kiro/specs/svc07-installer-simplification/ (design.md "Property 3:
Secret hygiene", requirements.md Requirement 3). This is the structural,
offline pin for the ONE behavioral fix of the rework (Task 5.2): the
primary-bootstrap child run in ``svc07_bootstrap`` must be ``no_log: true``,
and NO task across the five reworked roles that reads / registers / templates a
token value may carry ``no_log: false``.

WHAT IS ASSERTED (offline, PyYAML parse only, no infra)
-------------------------------------------------------
1. The primary-bootstrap task in
   ``ansible/roles/svc07_bootstrap/tasks/main.yml`` — the Task-8.4 child run
   that registers ``svc07_primary_bootstrap`` and whose captured child stdout
   carries the revealed Admin_Token (``TOKEN=<value>``) — is ``no_log: true``
   (NOT ``false``, NOT missing). Located robustly by the register var
   ``svc07_primary_bootstrap`` or a task name containing "primary bootstrap"
   (Req 3.1).

2. A structural scan of every task in the five reworked roles
   (``svc07_preflight``, ``svc07_provision``, ``svc07_bootstrap``,
   ``svc07_handoff``, ``svc07_clean_slate``) finds NO ``no_log: false`` task
   that handles a token value. Any task that DOES set ``no_log: false`` must be
   a NON-secret probe: neither its ``name`` nor its module args may reference a
   token/secret value (token, secret, admin_token, transit,
   OPENBAO_ADMIN_TOKEN, ...). The only sanctioned ``no_log: false`` is the
   consolidated debug SUMMARY probe that prints rc / stdout-length /
   ``TOKEN=``-marker-presence (a boolean) and NO secret value (Req 3.2, 3.3,
   3.4, 3.6).

This is a pure-structure drift guard (PyYAML ``safe_load_all`` of the committed
role files). It PARSES YAML only; it never embeds or prints any secret value.
The AUTHORITATIVE end-to-end confirmation remains the live from-scratch
acceptance (Task 15 / Property 9): no secret value in the run log at the default
posture. This guard pins the invariant so it cannot silently regress to the
baseline's ``no_log: false`` debug edit.

The helpers below are COPIED locally (NOT imported across test trees — the
conftest-collision rule) from the sibling structural tests
(``test_seal_token_orphan_mint.py`` / ``test_unsealer_reseal_ordering.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_secret_hygiene_no_log.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"

# The five reworked roles whose tasks are scanned (design "Playbook & Role
# Structure"). Every ``tasks/*.yml`` file under each is included.
_REWORKED_ROLES = (
    "svc07_preflight",
    "svc07_provision",
    "svc07_bootstrap",
    "svc07_handoff",
    "svc07_clean_slate",
)

_BOOTSTRAP_MAIN = _ROLES_DIR / "svc07_bootstrap" / "tasks" / "main.yml"


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally (no cross-tree import; conftest-collision rule).
# PyYAML safe_load_all parse only.
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_task_file(path: Path):
    """Return the flat ordered list of task dicts from a role tasks/ file.

    A role tasks/ file is a single YAML document whose top-level node is a LIST
    of tasks. Flatten block/rescue/always so ordering is by document position.
    """
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    tasks: list[dict] = []
    for doc in docs:
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _iter_role_task_files():
    """Yield every ``tasks/*.yml`` file under each of the five reworked roles."""
    for role in _REWORKED_ROLES:
        tasks_dir = _ROLES_DIR / role / "tasks"
        if not tasks_dir.is_dir():
            continue
        for path in sorted(tasks_dir.glob("*.yml")):
            yield path


def _iter_all_reworked_tasks():
    """Yield (file_path, task_dict) for every task in the five reworked roles."""
    for path in _iter_role_task_files():
        for task in _load_task_file(path):
            yield path, task


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _no_log_value(task: dict):
    """Return the task's ``no_log`` as one of ``True`` / ``False`` / ``None``.

    ``None`` means the key is absent. A literal bool is returned as-is; a
    string idiom (e.g. ``"{{ openbao_no_log }}"``) that isn't a plain
    false-like literal is treated as truthy (True) — it suppresses at runtime.
    A plain false-like literal (``false`` / ``no`` / ``0`` / empty) is False.
    """
    if "no_log" not in task:
        return None
    val = task["no_log"]
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in ("false", "no", "0", ""):
        return False
    return True


def _task_body_text(task: dict) -> str:
    """A flat lowercase string of everything the task DOES + how it is named.

    Concatenates the task ``name``, its ``environment`` mapping, and every
    module-arg spec (module name + its args, incl. ``argv`` / ``stdin`` /
    ``content`` / ``line`` / templated values). Used to decide whether a task
    handles a token value. Deliberately broad — a task that so much as
    references a token variable/marker anywhere in its args is treated as
    token-handling.
    """
    parts: list[str] = [_task_name(task)]

    env = task.get("environment")
    if env is not None:
        parts.append(str(env))

    for mod in _task_module_names(task):
        spec = task.get(mod)
        parts.append(str(mod))
        if isinstance(spec, dict):
            for key, value in spec.items():
                parts.append(str(key))
                parts.append(str(value))
        else:
            parts.append(str(spec))

    return " ".join(parts).lower()


# Tokens whose presence in a task's name/args means the task handles a secret
# value. Lowercase substring match against the task body (see _task_body_text).
# NOTE: the marker string ``token=`` (as in the boolean "has TOKEN= marker"
# probe) is NOT a secret — that probe reports only presence/absence, never a
# value — so it is handled explicitly in _is_token_handling below.
_SECRET_MARKERS = (
    "token",
    "secret",
    "admin_token",
    "transit",
    "openbao_admin_token",
    "seal_token",
    "root_token",
    "unseal",
)


def _is_nonsecret_marker_probe(task: dict) -> bool:
    """True for the sanctioned debug SUMMARY probe (rc / length / TOKEN= flag).

    The one allowed ``no_log: false`` task: it prints only non-secret metadata
    about the primary-bootstrap child run — return code, stdout LENGTH, and a
    boolean ``TOKEN=``-marker-presence — and NEVER the child stdout itself, so
    it reveals no secret value. It is a ``debug`` module whose body references
    the ``TOKEN=`` marker for a presence check but no token VALUE.
    """
    mods = _task_module_names(task)
    is_debug = "ansible.builtin.debug" in mods or "debug" in mods
    if not is_debug:
        return False
    body = _task_body_text(task)
    # It reports rc / length / marker-presence; the giveaways are the
    # length filter and the boolean marker check, and the absence of any
    # ``.stdout }}`` value print (which would leak the token-bearing output).
    prints_marker_presence = "token=" in body and "in (" in body
    prints_length = "length" in body
    return prints_marker_presence or prints_length


def _is_token_handling(task: dict) -> bool:
    """True if the task reads / registers / templates / carries a token value.

    Token-handling := the task body (name + module args + environment)
    references any secret marker. The sanctioned non-secret marker-presence
    SUMMARY probe (rc / length / boolean TOKEN= flag) is explicitly NOT
    token-handling — it names the ``TOKEN=`` marker only to report its
    presence, never a value.
    """
    if _is_nonsecret_marker_probe(task):
        return False
    body = _task_body_text(task)
    return any(marker in body for marker in _SECRET_MARKERS)


def _is_primary_bootstrap_task(task: dict) -> bool:
    """Identify the Task-8.4 primary-bootstrap child run robustly.

    Located by EITHER the register var ``svc07_primary_bootstrap`` OR a task
    name containing "primary bootstrap" — so a future rename of one signal
    still leaves the other to find it.
    """
    registers_primary = str(task.get("register", "")) == "svc07_primary_bootstrap"
    name_marks_primary = "primary bootstrap" in _task_name(task).lower()
    return registers_primary or name_marks_primary


# =============================================================================
# Assertion 1 — the primary-bootstrap task is no_log: true (Req 3.1).
# =============================================================================
def test_primary_bootstrap_task_is_no_log_true():
    """Property 3 (secret hygiene) — Validates: Requirements 3.1.

    The Task-8.4 primary-bootstrap child run in
    ``svc07_bootstrap/tasks/main.yml`` — the one that registers
    ``svc07_primary_bootstrap`` and whose captured child stdout carries the
    revealed Admin_Token (``TOKEN=<value>``) — MUST be ``no_log: true``. This is
    THE behavioral fix of the rework (Task 5.2): the baseline carried a
    preserved ``no_log: false`` debug edit here that leaked the Admin_Token into
    the run log. It MUST NOT regress to ``no_log: false`` (or be left unset).
    """
    tasks = _load_task_file(_BOOTSTRAP_MAIN)
    candidates = [t for t in tasks if _is_primary_bootstrap_task(t)]

    assert candidates, (
        "Could not locate the primary-bootstrap task in "
        f"{_BOOTSTRAP_MAIN.relative_to(_REPO_ROOT)} — expected a task that "
        "registers `svc07_primary_bootstrap` or whose name contains 'primary "
        "bootstrap'. If the task was renamed, update this guard AND confirm the "
        "child run is still no_log: true."
    )

    for task in candidates:
        no_log = _no_log_value(task)
        assert no_log is True, (
            "SECRET-HYGIENE REGRESSION: the primary-bootstrap task "
            f"({_task_name(task)!r}) in "
            f"{_BOOTSTRAP_MAIN.relative_to(_REPO_ROOT)} must be `no_log: true` — "
            f"it is {('missing no_log' if no_log is None else 'no_log: false')}. "
            "This is the Task-8.4 child run whose captured stdout carries the "
            "revealed Admin_Token (TOKEN=<value>); leaving it not-true leaks the "
            "token into the run log at the default posture (Req 3.1). Restore "
            "`no_log: true`."
        )


# =============================================================================
# Assertion 2 — no no_log:false task handles a token value (Req 3.2/3.3/3.4/3.6).
# =============================================================================
def test_no_no_log_false_task_handles_a_token_value():
    """Property 3 (secret hygiene) — Validates: Requirements 3.2, 3.3, 3.4, 3.6.

    Scan EVERY task across the five reworked roles (``svc07_preflight``,
    ``svc07_provision``, ``svc07_bootstrap``, ``svc07_handoff``,
    ``svc07_clean_slate``). Any task that sets ``no_log: false`` MUST be a
    NON-secret probe — neither its name nor its module args may reference a
    token/secret value. The ONLY sanctioned ``no_log: false`` is the
    consolidated debug SUMMARY probe that prints rc / stdout-length / a boolean
    ``TOKEN=``-marker-presence and NO secret value. Any ``no_log: false`` task
    that reads / registers / templates / carries a token value is a hygiene
    regression.
    """
    offenders: list[str] = []

    for path, task in _iter_all_reworked_tasks():
        if _no_log_value(task) is not False:
            continue
        # This task explicitly sets no_log: false. It is only allowed if it does
        # NOT handle a token value.
        if _is_token_handling(task):
            offenders.append(
                f"  - {path.relative_to(_REPO_ROOT)} :: {_task_name(task)!r}"
            )

    assert not offenders, (
        "SECRET-HYGIENE REGRESSION: found `no_log: false` task(s) that "
        "read/register/template/carry a token value in the reworked roles. Every "
        "token-handling task must default to `no_log: true`; the only allowed "
        "`no_log: false` is the non-secret debug summary probe (rc / stdout "
        "length / boolean TOKEN=-marker presence), which prints no secret value "
        "(Req 3.2, 3.3, 3.4, 3.6). Offending task(s):\n" + "\n".join(offenders)
    )


# =============================================================================
# Assertion 3 — the sanctioned no_log:false debug probe is recognised as such.
# =============================================================================
def test_only_sanctioned_no_log_false_is_the_nonsecret_marker_probe():
    """Property 3 (secret hygiene) — Validates: Requirements 3.4, 3.6.

    Positive corroboration of Assertion 2: enumerate every ``no_log: false``
    task in the five reworked roles and assert that EACH one is classified as a
    non-secret probe (``_is_nonsecret_marker_probe``) — i.e. a ``debug`` module
    that reports only rc / stdout-length / a boolean ``TOKEN=``-marker presence,
    never a secret value. This pins that the sole exposure path is the
    off-by-default debug summary, so a future edit that adds a value-printing
    ``no_log: false`` task (even one that dodges the secret-marker scan) is
    caught here.
    """
    non_probe_no_log_false: list[str] = []

    for path, task in _iter_all_reworked_tasks():
        if _no_log_value(task) is not False:
            continue
        if not _is_nonsecret_marker_probe(task):
            non_probe_no_log_false.append(
                f"  - {path.relative_to(_REPO_ROOT)} :: {_task_name(task)!r}"
            )

    assert not non_probe_no_log_false, (
        "The only sanctioned `no_log: false` task across the reworked roles is "
        "the consolidated non-secret debug SUMMARY probe (a `debug` printing rc "
        "/ stdout length / boolean TOKEN=-marker presence, no secret value). "
        "Found `no_log: false` task(s) that are NOT that probe — re-confirm they "
        "print no secret value and, if legitimate, either drop the explicit "
        "`no_log: false` (defaulting to suppressed) or narrow this guard "
        "deliberately:\n" + "\n".join(non_probe_no_log_false)
    )


# =============================================================================
# Sanity — the scan actually reached the reworked role files.
# =============================================================================
def test_scan_covered_the_reworked_roles():
    """Guard against a silent no-op: the scan must have parsed real task files.

    If a role path moves and the glob finds nothing, Assertions 2/3 would pass
    vacuously. Assert the scan saw at least the five roles' task files and a
    non-trivial number of tasks, so an empty scan can never masquerade as
    'clean'.
    """
    files = list(_iter_role_task_files())
    assert files, (
        "No task files found under the five reworked roles — the role layout "
        f"under {_ROLES_DIR.relative_to(_REPO_ROOT)} changed; repoint this guard."
    )
    roles_seen = {p.parent.parent.name for p in files}
    for role in _REWORKED_ROLES:
        assert role in roles_seen, (
            f"Role {role!r} contributed no tasks/*.yml to the scan — expected "
            f"under {_ROLES_DIR.relative_to(_REPO_ROOT)}/{role}/tasks/."
        )

    total_tasks = sum(1 for _ in _iter_all_reworked_tasks())
    assert total_tasks > 20, (
        "The reworked-roles task scan parsed suspiciously few tasks "
        f"({total_tasks}); a parse/glob regression may be hiding tasks from the "
        "secret-hygiene check."
    )
