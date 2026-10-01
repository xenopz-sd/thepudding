"""Offline structural drift-guard for the agent-live-test-authorization wiring.

Feature: agent-live-test-authorization (Task 6).

Spec: .kiro/specs/agent-live-test-authorization/ (design.md "Property 5" +
"Property 6", requirements.md Requirements 2/3/5). This pins **Property 5**
(the guard is consulted before any destructive action, gated on the interlock)
and **Property 6** (config isolation + non-secrecy) as OFFLINE structural
drift-guards, per the design's Testing Strategy — a pure text/YAML parse of the
committed roles + config files, no live Proxmox, no ``ansible-playbook`` binary.

WHAT IS ASSERTED (offline, PyYAML safe_load_all + text parse, no infra)
-----------------------------------------------------------------------
PROPERTY 5 — wiring consults the guard before any destructive action
  Against BOTH ``ansible/roles/svc07_clean_slate/tasks/main.yml`` AND
  ``ansible/roles/svc07_preflight/tasks/main.yml``:
    * a guard-consult ``command`` task whose argv references the guard
      (``throwaway_guard.py`` / ``svc07_throwaway_guard``), ``--service svc07``,
      ``--allowlist`` (the allowlist var/file), and ``--env-file``;
    * that consult task, the INFO decision ``debug``, and the rc ``assert``
      (asserting ``svc07_throwaway_decision.rc == 0``) are ALL gated
      ``when: agent_live_run`` — so ``agent_live_run=false`` skips them (a
      structural proxy for "an unarmed run is unchanged", Req 5.5);
    * EXACTLY ONE INFO decision debug per role (msg mentions "agent-live
      authorization");
    * ORDERING (by document-order index of the flattened task list): in
      ``svc07_clean_slate`` the guard consult + its assert precede the first
      destructive/prompt task (``ansible.builtin.pause`` / ``pct `` /
      ``clean-slate.yml`` / ``terraform`` ``state rm``); in ``svc07_preflight``
      the guard consult precedes the first ``ansible.builtin.uri`` task (the
      Proxmox ``/version`` reachability call).

PROPERTY 6 — config isolation and non-secrecy
    * ``.gitignore`` ignores ``.env.throwaway`` but NOT
      ``.env.throwaway.example`` (a ``.env.throwaway`` line + a
      ``!.env.throwaway.example`` negation);
    * ``scripts/installer/throwaway-clusters.yml`` exists, parses as YAML, has a
      top-level ``throwaway_clusters`` list, and each entry carries ONLY
      ``endpoint_host`` + ``node_name`` (no credential-pattern key);
    * ``.env.throwaway.example`` exists, its ``PROXMOX_API_TOKEN`` value is an
      obvious placeholder (not a plausible real token), and it carries no
      non-comment ``AGENT_LIVE_AUTHORIZED=`` file-key assignment (the interlock
      must be an env var, not a committed file default).

The helpers below are COPIED locally (NOT imported across test trees — the
conftest-collision rule), mirroring the drift-guard idiom of
``test_thin_orchestrator_play.py`` / ``test_health_child_run.py`` (the
``_iter_tasks`` block/rescue recursion, ``_NON_MODULE_KEYS`` module detection,
repo-root via ``Path(__file__).resolve().parents[2]``).

Validates: Requirements 2.5, 3.2, 3.4, 5.1, 5.2, 5.4, 5.5 / Properties 5, 6
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# tests/installer/test_agent_live_wiring.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CLEAN_SLATE = _REPO_ROOT / "ansible" / "roles" / "svc07_clean_slate" / "tasks" / "main.yml"
_PREFLIGHT = _REPO_ROOT / "ansible" / "roles" / "svc07_preflight" / "tasks" / "main.yml"

_GITIGNORE = _REPO_ROOT / ".gitignore"
_ALLOWLIST = _REPO_ROOT / "scripts" / "installer" / "throwaway-clusters.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.throwaway.example"


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally (no cross-tree import; conftest-collision rule).
# PyYAML safe_load_all parse only.
# --------------------------------------------------------------------------- #
# Keys on a task dict that are directives/containers, NOT the invoked module.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)


def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path) -> list[dict]:
    """Return the flat, document-ordered task list of a role tasks/ file.

    A role tasks file is a single YAML document whose top-level node is a LIST
    of tasks (no play wrapper). Flatten block/rescue/always in document order.
    """
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
        elif isinstance(doc, dict):
            tasks.extend(_iter_tasks([doc]))
    return tasks


def _module_names(task: dict) -> set[str]:
    """The set of module keys on a task (everything that is not a directive)."""
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _task_argv_text(task: dict) -> str:
    """Flat string of a command/shell task's argv/cmd/stdin (else '')."""
    parts: list[str] = []
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if spec is None:
            continue
        if isinstance(spec, str):
            parts.append(spec)
        elif isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                parts.append(" ".join(str(a) for a in argv))
            elif isinstance(argv, str):
                parts.append(argv)
            for key in ("cmd", "stdin"):
                if isinstance(spec.get(key), str):
                    parts.append(spec[key])
    return " ".join(parts)


def _when_text(task: dict) -> str:
    """Flat string of a task's ``when`` (a scalar or a list of conditions)."""
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, (list, tuple)):
        return " ".join(str(w) for w in when)
    return str(when)


def _is_debug(task: dict) -> bool:
    mods = _module_names(task)
    return "ansible.builtin.debug" in mods or "debug" in mods


def _debug_msg(task: dict) -> str:
    spec = task.get("ansible.builtin.debug") or task.get("debug") or {}
    if isinstance(spec, dict):
        return str(spec.get("msg", ""))
    return str(spec)


def _is_assert(task: dict) -> bool:
    mods = _module_names(task)
    return "ansible.builtin.assert" in mods or "assert" in mods


def _assert_that_text(task: dict) -> str:
    spec = task.get("ansible.builtin.assert") or task.get("assert") or {}
    if not isinstance(spec, dict):
        return str(spec)
    that = spec.get("that")
    if isinstance(that, (list, tuple)):
        return " ".join(str(t) for t in that)
    return str(that)


def _is_pause(task: dict) -> bool:
    mods = _module_names(task)
    return "ansible.builtin.pause" in mods or "pause" in mods


def _is_uri(task: dict) -> bool:
    mods = _module_names(task)
    return "ansible.builtin.uri" in mods or "uri" in mods


# --- Guard-consult / destructive-task classifiers -------------------------- #
def _references_guard(argv: str) -> bool:
    return "throwaway_guard.py" in argv or "svc07_throwaway_guard" in argv


def _references_allowlist(argv: str) -> bool:
    return "svc07_throwaway_allowlist" in argv or "throwaway-clusters.yml" in argv


def _is_guard_consult(task: dict) -> bool:
    """The guard-consult command task: argv references the guard + its flags."""
    argv = _task_argv_text(task)
    if not _references_guard(argv):
        return False
    return (
        "--service" in argv
        and "svc07" in argv
        and "--allowlist" in argv
        and _references_allowlist(argv)
        and "--env-file" in argv
    )


def _is_rc_assert(task: dict) -> bool:
    """The rc gate: an ``assert`` on ``svc07_throwaway_decision.rc == 0``."""
    if not _is_assert(task):
        return False
    that = _assert_that_text(task)
    return "svc07_throwaway_decision.rc" in that and "0" in that


def _is_info_decision_debug(task: dict) -> bool:
    """The INFO decision debug: a debug whose msg mentions agent-live auth."""
    if not _is_debug(task):
        return False
    return "agent-live authorization" in _debug_msg(task).lower()


def _is_destructive_or_prompt(task: dict) -> bool:
    """A destructive/prompt task in the clean-slate flow.

    Any ``ansible.builtin.pause``, or a command/shell whose argv/cmd contains
    ``pct `` / ``clean-slate.yml`` / a ``terraform ... state ... rm`` sequence.
    """
    if _is_pause(task):
        return True
    argv = _task_argv_text(task)
    if not argv:
        return False
    if "pct " in argv or "clean-slate.yml" in argv:
        return True
    if "terraform" in argv and "state" in argv and "rm" in argv:
        return True
    return False


# --------------------------------------------------------------------------- #
# PROPERTY 5 — the guard is consulted, gated, and ordered before any destroy.
# --------------------------------------------------------------------------- #
def _guard_consult_index(tasks: list[dict]) -> int:
    for idx, task in enumerate(tasks):
        if _is_guard_consult(task):
            return idx
    return -1


def _rc_assert_index(tasks: list[dict]) -> int:
    for idx, task in enumerate(tasks):
        if _is_rc_assert(task):
            return idx
    return -1


def _assert_three_gated_tasks(tasks: list[dict], role_label: str) -> None:
    """Consult + INFO debug + rc assert all present and gated on agent_live_run."""
    consults = [t for t in tasks if _is_guard_consult(t)]
    info_debugs = [t for t in tasks if _is_info_decision_debug(t)]
    rc_asserts = [t for t in tasks if _is_rc_assert(t)]

    assert len(consults) == 1, (
        f"{role_label}: expected exactly ONE guard-consult command task "
        f"(argv referencing throwaway_guard.py + --service svc07 + --allowlist + "
        f"--env-file); found {len(consults)}."
    )
    assert len(info_debugs) == 1, (
        f"{role_label}: expected EXACTLY ONE INFO decision debug (a debug task "
        f"whose msg mentions 'agent-live authorization'); found {len(info_debugs)}."
    )
    assert len(rc_asserts) == 1, (
        f"{role_label}: expected exactly ONE rc assert "
        f"(asserting svc07_throwaway_decision.rc == 0); found {len(rc_asserts)}."
    )

    # All three must be gated on agent_live_run so an unarmed run skips them.
    for kind, task in (
        ("guard consult", consults[0]),
        ("INFO decision debug", info_debugs[0]),
        ("rc assert", rc_asserts[0]),
    ):
        when = _when_text(task)
        assert "agent_live_run" in when, (
            f"{role_label}: the {kind} task ({_task_name(task)!r}) must be gated "
            f"`when: agent_live_run ...` so an unarmed run (agent_live_run=false) "
            f"skips it — found when={when!r}. This is the structural proxy for "
            "'an unarmed run is byte-for-byte unchanged' (Req 5.5)."
        )


def test_clean_slate_consults_guard_gated_and_before_any_destroy():
    """P5 (Validates: Requirements 5.1, 5.2, 5.4, 5.5).

    In ``svc07_clean_slate``: the guard consult + INFO decision debug + rc
    assert are all present and gated ``when: agent_live_run``; there is exactly
    one INFO decision debug; and the guard consult AND its rc assert both appear
    (by document-order index) BEFORE the first destructive/prompt task (a
    ``pause``, or a ``pct ``/``clean-slate.yml``/``terraform state rm`` command).
    """
    tasks = _load_role_tasks(_CLEAN_SLATE)

    _assert_three_gated_tasks(tasks, "svc07_clean_slate")

    consult_idx = _guard_consult_index(tasks)
    rc_idx = _rc_assert_index(tasks)
    assert consult_idx >= 0 and rc_idx >= 0, (
        "svc07_clean_slate: guard consult and/or rc assert not located."
    )

    first_destructive = -1
    for idx, task in enumerate(tasks):
        if _is_destructive_or_prompt(task):
            first_destructive = idx
            break
    assert first_destructive >= 0, (
        "svc07_clean_slate: expected at least one destructive/prompt task "
        "(pause / pct / clean-slate.yml / terraform state rm) — the ordering "
        "guard cannot be vacuously satisfied."
    )

    assert consult_idx < first_destructive, (
        "svc07_clean_slate: the guard consult (index "
        f"{consult_idx}) must appear BEFORE the first destructive/prompt task "
        f"(index {first_destructive}: {_task_name(tasks[first_destructive])!r}) "
        "so a REFUSED decision halts before any prompt, destroy, or state mutation."
    )
    assert rc_idx < first_destructive, (
        "svc07_clean_slate: the rc assert (index "
        f"{rc_idx}) must appear BEFORE the first destructive/prompt task (index "
        f"{first_destructive}: {_task_name(tasks[first_destructive])!r})."
    )


def test_preflight_consults_guard_gated_and_before_first_uri():
    """P5 (Validates: Requirements 5.1, 5.2, 5.4, 5.5).

    In ``svc07_preflight``: the guard consult + INFO decision debug + rc assert
    are all present and gated ``when: agent_live_run``; there is exactly one INFO
    decision debug; and the guard consult appears (by document-order index)
    BEFORE the first ``ansible.builtin.uri`` task (the Proxmox ``/version``
    reachability call) — so an armed run against a non-allowlisted identity is
    refused before any Proxmox resource is touched or queried.
    """
    tasks = _load_role_tasks(_PREFLIGHT)

    _assert_three_gated_tasks(tasks, "svc07_preflight")

    consult_idx = _guard_consult_index(tasks)
    assert consult_idx >= 0, "svc07_preflight: guard consult not located."

    first_uri = -1
    for idx, task in enumerate(tasks):
        if _is_uri(task):
            first_uri = idx
            break
    assert first_uri >= 0, (
        "svc07_preflight: expected at least one ansible.builtin.uri task (the "
        "Proxmox /version reachability call) — the ordering guard cannot be "
        "vacuously satisfied."
    )

    assert consult_idx < first_uri, (
        "svc07_preflight: the guard consult (index "
        f"{consult_idx}) must appear BEFORE the first uri task (index {first_uri}: "
        f"{_task_name(tasks[first_uri])!r}) so an armed from-scratch run against a "
        "non-allowlisted identity is REFUSED before any Proxmox API call."
    )


# --------------------------------------------------------------------------- #
# PROPERTY 6 — config isolation and non-secrecy.
# --------------------------------------------------------------------------- #
def test_gitignore_ignores_env_throwaway_but_not_the_example():
    """P6 (Validates: Requirements 3.2, 3.4).

    ``.gitignore`` must ignore ``.env.throwaway`` (it carries a real throwaway
    token) but NOT ``.env.throwaway.example`` (the committed placeholder
    template). Asserted by a pure-text parse: a bare ``.env.throwaway`` line and
    a ``!.env.throwaway.example`` negation must both be present. Note the repo's
    ``*.env`` pattern matches only names ENDING in ``.env``, so ``.env.throwaway``
    is NOT covered implicitly and must be listed explicitly.
    """
    lines = [ln.strip() for ln in _GITIGNORE.read_text(encoding="utf-8").splitlines()]
    non_comment = [ln for ln in lines if ln and not ln.startswith("#")]

    assert ".env.throwaway" in non_comment, (
        ".gitignore must contain an explicit `.env.throwaway` line — the "
        "throwaway env source carries a real token and must be gitignored "
        "(the `*.env` pattern matches only names ending in `.env`, so "
        "`.env.throwaway` is NOT covered implicitly)."
    )
    assert "!.env.throwaway.example" in non_comment, (
        ".gitignore must contain a `!.env.throwaway.example` negation so the "
        "committed placeholder template is NOT ignored (Req 3.2)."
    )


def test_throwaway_clusters_has_only_identity_keys():
    """P6 (Validates: Requirement 2.5).

    ``scripts/installer/throwaway-clusters.yml`` must exist, parse as YAML, have
    a top-level ``throwaway_clusters`` LIST, and each entry must carry ONLY the
    two identity keys ``endpoint_host`` + ``node_name`` — never a credential
    (token/secret/password/key). It is a committed, non-secret artifact.
    """
    assert _ALLOWLIST.is_file(), (
        f"Expected the committed allowlist at "
        f"{_ALLOWLIST.relative_to(_REPO_ROOT)} — not found."
    )

    doc = yaml.safe_load(_ALLOWLIST.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), (
        "throwaway-clusters.yml must parse to a mapping with a top-level "
        f"`throwaway_clusters` key; parsed to {type(doc).__name__}."
    )
    clusters = doc.get("throwaway_clusters")
    assert isinstance(clusters, list), (
        "throwaway-clusters.yml must have a top-level `throwaway_clusters` LIST; "
        f"found {type(clusters).__name__}."
    )
    assert clusters, (
        "throwaway-clusters.yml `throwaway_clusters` list is empty — expected at "
        "least one throwaway-cluster identity entry."
    )

    _allowed = {"endpoint_host", "node_name"}
    _credential_pat = re.compile(r"token|secret|password|key", re.IGNORECASE)

    for idx, entry in enumerate(clusters):
        assert isinstance(entry, dict), (
            f"throwaway_clusters[{idx}] must be a mapping; found "
            f"{type(entry).__name__}."
        )
        keys = set(entry.keys())
        extra = keys - _allowed
        assert not extra, (
            f"throwaway_clusters[{idx}] must carry ONLY identity keys "
            f"{sorted(_allowed)}; found extra key(s) {sorted(extra)}. The "
            "allowlist is NON-SECRET and must never contain a credential."
        )
        # Belt-and-braces: no key matches a credential pattern.
        cred_keys = [k for k in keys if _credential_pat.search(str(k))]
        assert not cred_keys, (
            f"throwaway_clusters[{idx}] carries credential-pattern key(s) "
            f"{cred_keys} — the allowlist must contain identities only (Req 2.5)."
        )


def test_env_throwaway_example_has_placeholder_token_and_no_interlock_key():
    """P6 (Validates: Requirements 3.2, 3.4).

    ``.env.throwaway.example`` must exist and:
      * its ``PROXMOX_API_TOKEN`` value must be an OBVIOUS placeholder (contains
        one of ``00000000`` / ``PLACEHOLDER`` / ``example``), not a plausible
        real token — no real credential is ever committed; and
      * it must NOT contain a non-comment ``AGENT_LIVE_AUTHORIZED=`` file-key
        assignment — the interlock is an ENV var set per run, never a committed
        file default (a commented mention in guidance text is fine).
    """
    assert _ENV_EXAMPLE.is_file(), (
        f"Expected the committed template at "
        f"{_ENV_EXAMPLE.relative_to(_REPO_ROOT)} — not found."
    )

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Locate the PROXMOX_API_TOKEN assignment (non-comment line).
    token_values: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r'\s*PROXMOX_API_TOKEN\s*=\s*(.+)$', raw)
        if m:
            token_values.append(m.group(1).strip().strip('"').strip("'"))

    assert token_values, (
        ".env.throwaway.example must document a PROXMOX_API_TOKEN assignment "
        "(with a placeholder value)."
    )
    for value in token_values:
        lowered = value.lower()
        is_placeholder = (
            "00000000" in value
            or "placeholder" in lowered
            or "example" in lowered
        )
        assert is_placeholder, (
            "The PROXMOX_API_TOKEN value in .env.throwaway.example must be an "
            f"OBVIOUS placeholder (contains 00000000 / PLACEHOLDER / example); "
            f"found {value!r}, which looks like it could be a real token. Never "
            "commit a real credential (Req 3.2)."
        )

    # No non-comment AGENT_LIVE_AUTHORIZED=... file-key assignment.
    offenders: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        # Only flag a real assignment; ignore _SERVICES etc. by anchoring the
        # var name followed directly by '='.
        if re.match(r'\s*AGENT_LIVE_AUTHORIZED\s*=', raw):
            offenders.append(stripped)

    assert not offenders, (
        ".env.throwaway.example must NOT contain a non-comment "
        "`AGENT_LIVE_AUTHORIZED=` file-key assignment — the interlock is an "
        "environment variable set per run, never a committed file default "
        f"(Req 3.4). Offending line(s): {offenders}."
    )


# --------------------------------------------------------------------------- #
# Sanity — the scan actually found the roles/files (guard against vacuous pass).
# --------------------------------------------------------------------------- #
def test_scan_reached_the_roles_and_config_files():
    """Guard against a silent no-op / vacuous pass.

    If a role path or config file moves, the assertions above could pass
    vacuously (empty task lists, missing files). Assert the scan parsed real,
    non-trivial task lists from BOTH reworked roles and that all three P6 config
    files exist and parse.
    """
    clean_slate_tasks = _load_role_tasks(_CLEAN_SLATE)
    preflight_tasks = _load_role_tasks(_PREFLIGHT)

    assert len(clean_slate_tasks) > 5, (
        "svc07_clean_slate parsed suspiciously few tasks "
        f"({len(clean_slate_tasks)}) — the role layout at "
        f"{_CLEAN_SLATE.relative_to(_REPO_ROOT)} may have moved; repoint this guard."
    )
    assert len(preflight_tasks) > 5, (
        "svc07_preflight parsed suspiciously few tasks "
        f"({len(preflight_tasks)}) — the role layout at "
        f"{_PREFLIGHT.relative_to(_REPO_ROOT)} may have moved; repoint this guard."
    )

    for path in (_GITIGNORE, _ALLOWLIST, _ENV_EXAMPLE):
        assert path.is_file(), (
            f"Expected P6 config file at {path.relative_to(_REPO_ROOT)} — not "
            "found; the config layout changed, repoint this guard."
        )
