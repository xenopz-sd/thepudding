"""Offline (Tier-1) bug-condition exploration + fix-validation test for the
SVC-06 clean-slate backing-DB residue defect.

Spec: .kiro/specs/fix-svc06-clean-slate-backing-db-residue/
(bugfix.md, design.md, tasks.md). This file is Task 1 (bug-condition
exploration) + Task 2 (preservation) and doubles as the Task 3.3/3.4
fix-validation test — the SAME tests, which FAIL on the unfixed tree (proving
both root causes) and PASS once the two coordinated fixes land. It mirrors the
sibling ``infra/tests/test_svc07_clean_slate_state_residue.py`` approach: parse
the COMMITTED YAML with PyYAML; no infrastructure required.

=== WHAT THE BUG IS ===

SVC-06 (ZITADEL)'s clean-slate is contracted to tear down BOTH the ZITADEL guest
AND its backing SVC-01 Postgres DB (slug ``shared-zitadel``, VLAN 20, VMID 1102)
by orchestrating the backing DB's OWN ``svc-01-bootstrap`` clean-slate as a child
run (step B of ``svc06_clean_slate/tasks/main.yml``). But the three identity
values the child run needs — ``svc06_db_vmid`` / ``svc06_db_project_slug`` /
``svc06_db_vlan_id`` — are defined ONLY in ``svc06_provision/defaults/main.yml``,
which is NOT loaded on a ``clean_slate=true`` run (the normal phases, including
``svc06_provision``, are skipped). On the clean_slate branch those vars are
undefined/empty, the child ``svc-01-bootstrap`` receives empty
``svc01_vmid=``/``project_slug=``/``vlan_id=``, VMID 1102 is never targeted, and
its ZFS rootfs + ``shared_zitadel`` Postgres data survive.

A SECOND, independent defect hides the first: the empty-baseline gate computes
``svc06_surviving_vmids`` from ``[svc06_zitadel_vmid | string, svc06_db_vmid |
string]`` filtered by ``select('in', pct_list.stdout)``. When ``svc06_db_vmid``
is empty, ``"" in <any string>`` is always true (a SUBSTRING test), so the
backing-DB VMID silently drops out of the survivor set and the gate PASSES while
VMID 1102 is still running.

=== THE TWO COORDINATED FIXES ===

- Fix 1 (var scope): hoist the backing-DB identity to the play-level ``vars:`` of
  ``svc-06-bootstrap.yml`` (single source of truth) and REMOVE the three keys
  from ``svc06_provision/defaults/main.yml``.
- Fix 2 (gate hardening): a pre-survivor ``assert`` requiring both VMIDs non-empty
  ``int(0) > 0``, and a whole-token membership test
  (``select('in', (stdout | default('')).split())``) so an empty string can never
  defeat the gate.

=== HOW THIS TEST ENCODES IT (offline, provider-free, no infra) ===

- Case A (var-scope exploration, FAILS unfixed): the three ``svc06_db_*`` keys are
  in the play-level ``vars:`` of ``svc-06-bootstrap.yml`` AND no longer in
  ``svc06_provision/defaults/main.yml`` (single source of truth).
- Case B (gate-blind-spot exploration, FAILS unfixed): the gate region of
  ``svc06_clean_slate/tasks/main.yml`` has a pre-survivor ``assert`` that both
  VMIDs are non-empty, AND the survivor ``set_fact`` tokenizes stdout
  (``.split()``) rather than a bare substring ``select('in', <raw stdout>)``.
- Preservation (PASSES on the unfixed tree, must stay green post-fix): the normal
  provision child argv still renders ``project_slug=shared-zitadel`` /
  ``vlan_id=20`` / ``svc01_vmid=1102`` / ``host_index=4``; the residue sweep +
  ZITADEL teardown, the Route_Fragment removal, the confirm gate, and the
  ``AGENT_LIVE_AUTHORIZED`` passthrough are structurally present/unchanged.
- A pure-logic unit check encodes the hardened gate expression intent directly:
  a whole-token match detects ``1102``; a substring-only coincidence does not; an
  empty VMID never matches.

Run (offline, no infra):
  ~/venv/devinfra/bin/pytest \\
    infra/tests/test_svc06_clean_slate_backing_db_residue.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "svc-06-bootstrap.yml"
_PROVISION_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "svc06_provision" / "defaults" / "main.yml"
)
_PROVISION_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc06_provision" / "tasks" / "main.yml"
)
_CLEAN_SLATE_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc06_clean_slate" / "tasks" / "main.yml"
)

# The three backing-DB identity vars consumed on BOTH branches (Fix 1 subject).
_DB_IDENTITY_KEYS = ("svc06_db_vmid", "svc06_db_project_slug", "svc06_db_vlan_id")


# --------------------------------------------------------------------------- #
# Parsing helpers.
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _play_vars(playbook_path: Path) -> dict:
    """Return the play-level ``vars:`` dict of the (single) play in a playbook."""
    doc = _load_yaml(playbook_path)
    assert isinstance(doc, list) and doc, f"{playbook_path} is not a play list"
    play = doc[0]
    assert isinstance(play, dict), f"{playbook_path} first play is not a mapping"
    return play.get("vars", {}) or {}


def _flatten_tasks(node) -> list[dict]:
    """Recursively flatten tasks, descending into block/rescue/always."""
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_flatten_tasks(item))
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                out.extend(_flatten_tasks(node[key]))
    return out


def _role_tasks(role_tasks_path: Path) -> list[dict]:
    parsed = _load_yaml(role_tasks_path) or []
    return _flatten_tasks(parsed)


def _task_argv(task: dict) -> list[str]:
    """Flat argv/cmd token list of a command/shell task."""
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                return [str(a) for a in argv]
            if isinstance(spec.get("cmd"), str):
                return spec["cmd"].split()
        elif isinstance(spec, str):
            return spec.split()
    return []


def _set_fact_body(tasks: list[dict], fact_name: str) -> str | None:
    """Return the string body of a ``set_fact`` setting ``fact_name``, or None."""
    for t in tasks:
        sf = t.get("ansible.builtin.set_fact") or t.get("set_fact")
        if isinstance(sf, dict) and fact_name in sf:
            return str(sf[fact_name])
    return None


def _child_svc01_argv(tasks: list[dict]) -> list[str] | None:
    """Return the argv of the task that invokes a child ``svc-01-bootstrap.yml``."""
    for t in tasks:
        argv = _task_argv(t)
        if any("svc-01-bootstrap.yml" in tok for tok in argv):
            return argv
    return None


def _argv_eval(argv: list[str], key: str) -> str | None:
    """Return the ``value`` from an argv entry of the shape ``key=value``.

    Ansible ``-e`` args are passed as separate ``key=value`` tokens; find the
    token beginning ``key=`` and return its RHS (a Jinja expression string).
    """
    prefix = f"{key}="
    for tok in argv:
        if tok.startswith(prefix):
            return tok[len(prefix):]
    return None


# =========================================================================== #
# Pure-logic model of the hardened gate's whole-token membership (Fix 2b).
# Mirrors the Jinja ``[...] | select('in', stdout.split()) | list`` semantics.
# =========================================================================== #
def _survivors_whole_token(vmids: list[str], pct_list_stdout: str) -> list[str]:
    """Whole-token survivor membership — the FIXED gate expression's semantics.

    ``select('in', stdout.split())`` keeps each vmid that equals a whitespace-
    delimited token of ``stdout`` — a genuine membership test that an empty
    string can never satisfy and a substring coincidence can never trip.
    """
    tokens = (pct_list_stdout or "").split()
    return [v for v in vmids if v in tokens]


def _survivors_substring(vmids: list[str], pct_list_stdout: str) -> list[str]:
    """Substring survivor membership — the BUGGY gate expression's semantics.

    ``select('in', stdout)`` keeps each vmid that is a SUBSTRING of ``stdout`` —
    so ``"" in stdout`` is always true and a VMID appearing only as a substring
    of some other column trips a false survivor.
    """
    return [v for v in vmids if v in (pct_list_stdout or "")]


# =========================================================================== #
# ================  Case A — var-scope exploration (FAILS unfixed)  ========== #
# =========================================================================== #
# Validates: Requirements 1.1 (bug), 2.1/3.2 (fix). FAILS on the unfixed tree
# because the three svc06_db_* keys live only in svc06_provision/defaults.
def test_case_a_backing_db_identity_is_play_level_single_source():
    """Validates: Requirements 1.1 (bug), 2.1, 3.2 (fix).

    The three backing-DB identity vars MUST be in the play-level ``vars:`` of
    ``svc-06-bootstrap.yml`` (so the clean_slate branch sees them) AND MUST NOT
    be defined in ``svc06_provision/defaults/main.yml`` (single source of truth,
    no two editable copies). On the UNFIXED tree this FAILS: they live only in
    the provision defaults and are absent from play vars.
    """
    play_vars = _play_vars(_PLAYBOOK)
    provision_defaults = _load_yaml(_PROVISION_DEFAULTS) or {}

    missing_from_play = [k for k in _DB_IDENTITY_KEYS if k not in play_vars]
    assert not missing_from_play, (
        "VAR-SCOPE BUG (Req 1.1): the backing-DB identity vars are NOT in the "
        f"play-level vars: of {_PLAYBOOK.name} — missing {missing_from_play}. "
        "On a clean_slate=true run svc06_provision is skipped, so these are "
        "undefined/empty when svc06_clean_slate composes the child svc-01 "
        "argv, and VMID 1102 is never targeted. Fix 1 hoists them to the play "
        "vars: as the single source of truth (Req 2.1/3.2)."
    )

    # Expected values are preserved exactly (Req 3.1 — same identity).
    assert str(play_vars.get("svc06_db_vmid")) == "1102", (
        f"svc06_db_vmid must be 1102 at play level; got {play_vars.get('svc06_db_vmid')!r}"
    )
    assert play_vars.get("svc06_db_project_slug") == "shared-zitadel", (
        f"svc06_db_project_slug must be shared-zitadel; got "
        f"{play_vars.get('svc06_db_project_slug')!r}"
    )
    assert str(play_vars.get("svc06_db_vlan_id")) == "20", (
        f"svc06_db_vlan_id must be 20; got {play_vars.get('svc06_db_vlan_id')!r}"
    )

    # Single source of truth: the three keys must NOT also live in provision
    # defaults (two editable copies that can drift — Req 3.2 forbids it).
    duplicated = [k for k in _DB_IDENTITY_KEYS if k in provision_defaults]
    assert not duplicated, (
        "SINGLE-SOURCE-OF-TRUTH violation (Req 3.2): the backing-DB identity "
        f"keys {duplicated} are STILL defined in "
        f"{_PROVISION_DEFAULTS.relative_to(_REPO_ROOT)} as well as the play "
        "vars:. Fix 1 removes them from the provision defaults, leaving the "
        "play-level definition as the sole editable copy."
    )


# =========================================================================== #
# ================  Case B — gate blind-spot exploration (FAILS unfixed)  ==== #
# =========================================================================== #
# Validates: Requirements 1.3 (bug), 2.3 (fix). FAILS on the unfixed tree:
# no pre-survivor non-empty assert; survivor set_fact uses a bare substring
# select('in', <raw stdout>) with no .split() tokenization.
def test_case_b_gate_fails_closed_and_whole_token_matches():
    """Validates: Requirements 1.3 (bug), 2.3 (fix).

    The gate region of ``svc06_clean_slate/tasks/main.yml`` MUST (a) assert both
    ``svc06_zitadel_vmid`` and ``svc06_db_vmid`` are non-empty BEFORE the survivor
    computation (fail closed), and (b) compute ``svc06_surviving_vmids`` by
    tokenizing ``pct_list.stdout`` (``.split()``) so a whole-token membership test
    is used — not a bare substring ``select('in', <raw stdout>)``. On the UNFIXED
    tree this FAILS: there is no pre-survivor non-empty assert, and the survivor
    expression is a substring test over the raw stdout.
    """
    tasks = _role_tasks(_CLEAN_SLATE_TASKS)

    # (b) The survivor set_fact must tokenize stdout with .split().
    survivor_body = _set_fact_body(tasks, "svc06_surviving_vmids")
    assert survivor_body is not None, (
        "no set_fact computing svc06_surviving_vmids found in "
        f"{_CLEAN_SLATE_TASKS.relative_to(_REPO_ROOT)}"
    )
    uses_split = ".split()" in survivor_body.replace(" ", "")
    assert uses_split, (
        "GATE BLIND SPOT (Req 1.3): svc06_surviving_vmids uses a SUBSTRING "
        "membership test `select('in', <raw stdout>)`, so an empty "
        "svc06_db_vmid ('' in <anything> == True) silently drops the backing-DB "
        "VMID from the survivor set and the gate reports clean while 1102 runs. "
        "Fix 2b must tokenize stdout — select('in', (stdout | default('')).split()) "
        f"— for a whole-token match. body={survivor_body!r}"
    )

    # (a) A pre-survivor assert must require both VMIDs non-empty and int>0.
    # It must appear in the task list BEFORE the svc06_surviving_vmids set_fact.
    survivor_idx = None
    for i, t in enumerate(tasks):
        sf = t.get("ansible.builtin.set_fact") or t.get("set_fact")
        if isinstance(sf, dict) and "svc06_surviving_vmids" in sf:
            survivor_idx = i
            break
    assert survivor_idx is not None

    def _is_nonempty_vmid_assert(task: dict) -> bool:
        a = task.get("ansible.builtin.assert") or task.get("assert")
        if not isinstance(a, dict):
            return False
        that = a.get("that")
        clauses = that if isinstance(that, list) else [str(that)]
        blob = " ".join(str(c) for c in clauses)
        mentions_both = "svc06_zitadel_vmid" in blob and "svc06_db_vmid" in blob
        # fail-closed on empty: a length check AND an int(0) > 0 check.
        checks_nonempty = "length" in blob and "int(0)" in blob and "> 0" in blob
        return mentions_both and checks_nonempty

    pre_survivor_asserts = [
        t for t in tasks[:survivor_idx] if _is_nonempty_vmid_assert(t)
    ]
    assert pre_survivor_asserts, (
        "GATE FAIL-CLOSED MISSING (Req 2.3): there is no `assert` task BEFORE "
        "the svc06_surviving_vmids set_fact that requires both svc06_zitadel_vmid "
        "and svc06_db_vmid be defined, non-empty, and int(0) > 0. Without it an "
        "unresolved backing-DB VMID is silently tolerated. Fix 2a inserts a "
        "pre-survivor assert naming both VMIDs in its fail_msg."
    )


# =========================================================================== #
# ================  Preservation (PASSES unfixed, must stay green)  ========== #
# =========================================================================== #
# Validates: Requirements 3.1, 3.3, 3.4, 3.5, 3.6. These assert that the
# unchanged behaviors are structurally present; they are GREEN on the unfixed
# tree (baseline to preserve) and must remain green after the two fixes.
def test_preservation_provision_child_argv_identity_unchanged():
    """Validates: Requirement 3.1.

    The normal provision path's child ``svc-01-bootstrap`` argv still renders
    ``project_slug=shared-zitadel``, ``vlan_id=20``, ``svc01_vmid=1102``, and
    ``host_index=4`` — i.e. it references the same identity (via the play-level
    vars after Fix 1, or the provision defaults before it), and
    ``svc06_db_host_index`` STAYS in the provision defaults.
    """
    tasks = _role_tasks(_PROVISION_TASKS)
    argv = _child_svc01_argv(tasks)
    assert argv is not None, (
        "no child svc-01-bootstrap task found in "
        f"{_PROVISION_TASKS.relative_to(_REPO_ROOT)}"
    )

    # The provision argv references the identity vars (same names, resolving to
    # 1102 / shared-zitadel / 20 / 4 whether from play vars or provision defaults).
    expectations = {
        "project_slug": "svc06_db_project_slug",
        "vlan_id": "svc06_db_vlan_id",
        "svc01_vmid": "svc06_db_vmid",
        "host_index": "svc06_db_host_index",
    }
    for arg_key, var_name in expectations.items():
        rhs = _argv_eval(argv, arg_key)
        assert rhs is not None, (
            f"provision child argv missing `{arg_key}=` token; argv={argv!r}"
        )
        assert var_name in rhs, (
            f"provision child argv `{arg_key}=` must reference {{{{ {var_name} }}}}; "
            f"got {rhs!r}"
        )

    # host_index MUST remain in provision defaults (provision-only knob, Req 3.1).
    provision_defaults = _load_yaml(_PROVISION_DEFAULTS) or {}
    assert str(provision_defaults.get("svc06_db_host_index")) == "4", (
        "svc06_db_host_index must STAY in svc06_provision/defaults/main.yml as 4 "
        f"(provision-only knob); got {provision_defaults.get('svc06_db_host_index')!r}"
    )


def test_preservation_zitadel_teardown_and_residue_sweep_present():
    """Validates: Requirement 3.3.

    Step A (ZITADEL guest teardown by ``svc06_zitadel_vmid``) and the
    provider-free residue sweep (``terraform state rm`` of all remaining SVC-06
    state addresses) are structurally present/unchanged.
    """
    text = _CLEAN_SLATE_TASKS.read_text(encoding="utf-8")
    tasks = _role_tasks(_CLEAN_SLATE_TASKS)

    # Step A target carries vmid: {{ svc06_zitadel_vmid }}.
    targets_body = _set_fact_body(tasks, "svc06_clean_slate_targets")
    assert targets_body is not None and "svc06_zitadel_vmid" in targets_body, (
        "ZITADEL teardown target (vmid: {{ svc06_zitadel_vmid }}) missing/changed"
    )

    # Residue sweep: a `terraform state rm` command driven by the residual addrs.
    has_state_rm = any(
        {"terraform", "state", "rm"}.issubset(set(_task_argv(t))) for t in tasks
    )
    assert has_state_rm, (
        "provider-free residue sweep (terraform state rm) missing from the "
        "SVC-06 clean-slate (Req 3.3 — fix-svc07 residue behavior preserved)"
    )
    assert "svc06_residual_state_addrs" in text, (
        "residue-sweep residual-address computation missing/changed (Req 3.3)"
    )
    # No `terraform destroy` — the sweep must stay provider-free.
    assert not any(
        {"terraform", "destroy"}.issubset(set(_task_argv(t))) for t in tasks
    ), "clean-slate must stay provider-free — no `terraform destroy` (Req 2.5/3.3)"


def test_preservation_route_fragment_removal_present():
    """Validates: Requirement 3.4.

    The cross-guest Route_Fragment removal (known_hosts reconcile + add_host +
    delegated ``state: absent`` removal of the ZITADEL route) is present/unchanged.
    """
    text = _CLEAN_SLATE_TASKS.read_text(encoding="utf-8")
    tasks = _role_tasks(_CLEAN_SLATE_TASKS)

    assert "ssh-keyscan" in text, "known_hosts ssh-keyscan reconcile missing (Req 3.4)"

    has_add_host = any(
        isinstance(t.get("ansible.builtin.add_host") or t.get("add_host"), dict)
        for t in tasks
    )
    assert has_add_host, "Traefik_Guest add_host missing (Req 3.4)"

    # Delegated `state: absent` file removal of the ZITADEL route fragment.
    delegated_absent = False
    for t in tasks:
        f = t.get("ansible.builtin.file") or t.get("file")
        if (
            isinstance(f, dict)
            and f.get("state") == "absent"
            and "delegate_to" in t
            and "svc06_zitadel_route_fragment" in str(f.get("path", ""))
        ):
            delegated_absent = True
            break
    assert delegated_absent, (
        "delegated `state: absent` removal of the ZITADEL Route_Fragment "
        "missing/changed (Req 3.4)"
    )


def test_preservation_confirm_gate_and_guard_forwarding_present():
    """Validates: Requirements 3.5, 3.6.

    The confirm/abort tasks and the throwaway-guard consult + the
    ``AGENT_LIVE_AUTHORIZED`` environment passthrough on the child backing-DB
    step are present/unchanged.
    """
    tasks = _role_tasks(_CLEAN_SLATE_TASKS)
    text = _CLEAN_SLATE_TASKS.read_text(encoding="utf-8")

    # Confirm gate (Req 3.5): a pause prompt + a fail that aborts when unconfirmed.
    has_pause = any(
        "ansible.builtin.pause" in t or "pause" in t for t in tasks
    )
    assert has_pause, "confirm prompt (pause) missing (Req 3.5)"
    confirmed_body = _set_fact_body(tasks, "svc06_clean_slate_confirmed")
    assert confirmed_body is not None, "svc06_clean_slate_confirmed resolution missing (Req 3.5)"
    has_abort = any(
        isinstance(t.get("ansible.builtin.fail") or t.get("fail"), dict)
        and "not (svc06_clean_slate_confirmed" in str(t.get("when", ""))
        for t in tasks
    )
    assert has_abort, "unconfirmed-abort fail task missing (Req 3.5)"

    # Throwaway guard consult FIRST (Req 3.6).
    assert "throwaway_guard" in text, "throwaway-guard consult missing (Req 3.6)"

    # AGENT_LIVE_AUTHORIZED env passthrough on the child backing-DB step (Req 3.6).
    child_has_env_passthrough = False
    for t in tasks:
        argv = _task_argv(t)
        if any("svc-01-bootstrap.yml" in tok for tok in argv):
            env = t.get("environment") or {}
            if isinstance(env, dict) and "AGENT_LIVE_AUTHORIZED" in env:
                child_has_env_passthrough = True
            break
    assert child_has_env_passthrough, (
        "AGENT_LIVE_AUTHORIZED environment passthrough on the child "
        "svc-01-bootstrap step missing/changed (Req 3.6)"
    )


def test_preservation_child_backing_db_no_log_retained():
    """Validates: Requirement 2.5 / 3.6 secrets posture.

    The child backing-DB step is still driven through a child ``svc-01-bootstrap
    -e clean_slate=true`` run (dependency-through-own-path) and retains
    ``no_log: true`` (no secret echoed).
    """
    tasks = _role_tasks(_CLEAN_SLATE_TASKS)
    for t in tasks:
        argv = _task_argv(t)
        if any("svc-01-bootstrap.yml" in tok for tok in argv):
            assert "clean_slate=true" in argv, (
                "child backing-DB run must pass -e clean_slate=true "
                "(dependency-through-own-path, Req 2.5)"
            )
            assert t.get("no_log") is True, (
                "child backing-DB step must retain no_log: true (secrets posture)"
            )
            return
    raise AssertionError("no child svc-01-bootstrap clean-slate task found")


# =========================================================================== #
# ================  Pure-logic gate expression semantics (Fix 2b)  =========== #
# =========================================================================== #
# Validates: Requirement 2.3. Encodes the hardened whole-token membership intent
# directly — independent of the committed YAML. PASSES regardless of tree state
# (it tests the Python model of the fixed semantics), locking in the invariant.
def test_gate_whole_token_detects_real_vmid_substring_does_not():
    """Validates: Requirement 2.3.

    Whole-token membership detects VMID ``1102`` when it appears as a real
    ``pct list`` column, does NOT match when ``1102`` appears only as a substring
    of another field, and NEVER matches an empty VMID.
    """
    # A realistic `pct list` with 1102 as a whole-token VMID column.
    stdout_running = (
        "VMID       Status     Lock         Name\n"
        "1102       running                 svc01-20-01\n"
        "1103       running                 svc06-20-01\n"
    )
    assert _survivors_whole_token(["1102", "1103"], stdout_running) == ["1102", "1103"], (
        "whole-token match must detect VMIDs present as pct-list columns"
    )

    # 1102 appears ONLY as a substring of another field (e.g. a name/port), never
    # as its own token. Whole-token match must NOT flag it; substring match wrongly does.
    stdout_substring_only = (
        "VMID       Status     Lock         Name\n"
        "9999       running                 app-11025-test\n"
    )
    assert _survivors_whole_token(["1102"], stdout_substring_only) == [], (
        "whole-token match must NOT flag a VMID that appears only as a substring"
    )
    assert _survivors_substring(["1102"], stdout_substring_only) == ["1102"], (
        "sanity: the OLD substring test DID wrongly flag the substring coincidence"
    )


def test_gate_empty_vmid_never_matches_whole_token():
    """Validates: Requirement 2.3.

    An empty ``svc06_db_vmid`` ('') can never be a survivor under the whole-token
    test, whereas under the buggy substring test it always matched (the blind
    spot). The Fix-2a pre-assert additionally refuses to run the gate at all when
    the VMID is empty — modelled here by the empty string never being selected.
    """
    stdout_any = "VMID Status Name\n1103 running svc06-20-01\n"
    assert _survivors_whole_token([""], stdout_any) == [], (
        "empty VMID must NEVER be a whole-token survivor"
    )
    # Demonstrate the bug the fix removes: substring '' in <anything> is always true.
    assert _survivors_substring([""], stdout_any) == [""], (
        "sanity: the OLD substring test always matched '' (the gate blind spot)"
    )
