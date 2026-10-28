"""Offline structural drift-guard for the obs_clean_slate teardown role.

Task 10.2 (spec: svc-17-18-observability) — requirements.md FR-18 / AC-10;
design.md §3.5 (obs_clean_slate) + §"Correctness Properties" (clean-slate
residue). The OPTIONAL (``*``) structural test the design names: pins the
clean-slate branch BODY of the SVC-17/18 observability installer
(``ansible/roles/obs_clean_slate/tasks/main.yml``) by parsing it, never by
running ``ansible-playbook`` or touching live Proxmox.

WHAT MAKES SVC-17/18 DIFFERENT FROM svc09 — svc09_clean_slate resets ONE guest
in ONE Terraform state; obs_clean_slate resets THREE guests
(Prometheus/Grafana/Loki) sharing the ONE ``svc-17-18-observability`` Terraform
state. So the empty-baseline gate must cover all three guests AND the single
shared state's non-guest ``terraform_data`` residue. This file guards exactly
that three-guests-one-state shape:

  * Empty-state gate (FR-18/AC-10): the ``pct list`` survivor check covers ALL
    THREE guest VMIDs (prometheus/grafana/loki), AND the state-list-empty check
    runs over the ONE shared state; the FINAL gate ``assert`` requires BOTH the
    surviving-VMID set empty AND the surviving-state-address set empty — a
    surviving resource of either kind FAILS (no false-clean).
  * Residue sweep (FR-18): the sweep enumerates ALL remaining state addresses
    and ``terraform state rm``s each PROVIDER-FREE (NOT ``terraform destroy``),
    so the non-guest ``terraform_data`` residue
    (``terraform_data.ledger_index_assert`` + the three
    ``module.*.terraform_data.input_guards``) the container-only state rm leaves
    behind is cleared from the single shared state.
  * Agent-live throwaway-guard consult is FIRST, gated ``when: agent_live_run``,
    fail-closed (rc-assert refuses an unauthorized target before any destruction).
  * Confirmation gating: a ``pause`` gated ``when: clean_slate_confirm != 'force'``
    and a non-affirmative ``fail`` abort precede the first destructive marker.

HOW IT STAYS OFFLINE — pure PyYAML + structure parse of the committed role. No
``ansible-playbook`` binary, no live Proxmox; always runs, no ``requires_infra``
gate. It PARSES YAML only and never embeds/prints a secret. Mirrors the repo's
drift-guard style (``test_platform_clean_slate_wiring.py``,
``test_obs_mocks.py``): helpers are COPIED locally, no cross-test-tree imports
(the tests/installer vs infra/tests conftest-collision rule), repo root via
``parents[2]``.

Validates: Requirements FR-18, AC-10.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_obs_clean_slate_structural.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_obs_clean_slate_structural.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_TASKS = _REPO_ROOT / "ansible" / "roles" / "obs_clean_slate" / "tasks" / "main.yml"

# Keys on a task dict that are directives/containers, NOT the invoked module.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)

# The three guest VMID vars the three-guests-one-state shape pivots on.
_GUEST_VMID_VARS = ("obs_prometheus_vmid", "obs_grafana_vmid", "obs_loki_vmid")

# The non-guest terraform_data residue the sweep must clear from the ONE shared
# state (design §Correctness Properties / role header).
_RESIDUE_ADDRS = (
    "terraform_data.ledger_index_assert",
    "module.prometheus.terraform_data.input_guards",
    "module.grafana.terraform_data.input_guards",
    "module.loki.terraform_data.input_guards",
)


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally per the repo drift-guard convention (no cross-tree
# import; the conftest-collision rule). Mirrors test_platform_clean_slate_wiring.py.
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


def _load_role_tasks(path: Path) -> list[dict]:
    """Return the flat, document-ordered task list of a role tasks/ file.

    The obs_clean_slate role body is a FLAT task list (the enclosing
    ``when: clean_slate`` block/rescue lives in the orchestrator play), so the
    document order of this list IS the teardown execution order the ordering
    assertions below rely on.
    """
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
        elif isinstance(doc, dict):
            tasks.extend(_iter_tasks([doc]))
    return tasks


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


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
    """Flatten a task's ``when`` (scalar or list) into one string."""
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, (list, tuple)):
        return " ".join(str(w) for w in when)
    return str(when)


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


def _is_module(task: dict, *names: str) -> bool:
    mods = _module_names(task)
    return any(n in mods for n in names)


def _set_fact_text(task: dict, fact_name: str) -> str | None:
    """Return the string body assigned to ``fact_name`` by a ``set_fact`` task.

    ``None`` if the task is not a ``set_fact`` that assigns ``fact_name``. Used to
    inspect HOW a survivor set is COMPUTED (not merely that it is referenced).
    """
    spec = task.get("ansible.builtin.set_fact") or task.get("set_fact")
    if not isinstance(spec, dict) or fact_name not in spec:
        return None
    return str(spec[fact_name])


# --------------------------------------------------------------------------- #
# Marker detectors.
# --------------------------------------------------------------------------- #
def _is_guard_consult(task: dict) -> bool:
    """True if the task is the throwaway_guard consult scoped to --service obs."""
    argv = _task_argv_text(task)
    return "throwaway_guard" in argv and "--service" in argv and " obs" in f" {argv}"


def _is_guard_rc_assert(task: dict) -> bool:
    """The rc gate: an ``assert`` on the guard's ``*throwaway_decision.rc``."""
    return _is_assert(task) and "throwaway_decision.rc" in _assert_that_text(task)


def _is_child_clean_slate_run(task: dict) -> bool:
    """A child clean-slate.yml run: the clean-slate playbook + clean_slate_enabled."""
    argv = _task_argv_text(task)
    return "clean-slate.yml" in argv and "clean_slate_enabled=true" in argv


def _is_state_rm(task: dict) -> bool:
    """A provider-free ``terraform state rm`` (the residue sweep op)."""
    argv = _task_argv_text(task)
    return "terraform" in argv and "state" in argv and "rm" in argv


def _is_destructive_marker(task: dict) -> bool:
    """True if the task is a destructive teardown marker (ordering anchor).

    A child clean-slate run, a ``terraform state rm`` / ``terraform destroy``, or
    any ``pct`` mutation.
    """
    if _is_child_clean_slate_run(task):
        return True
    argv = _task_argv_text(task)
    if "terraform" in argv and ("destroy" in argv or ("state" in argv and "rm" in argv)):
        return True
    if "pct destroy" in argv:
        return True
    return False


def _first_index(tasks: list[dict], pred) -> int | None:
    return next((i for i, t in enumerate(tasks) if pred(t)), None)


# --------------------------------------------------------------------------- #
# Sanity — the role parsed to a non-trivial number of tasks (guard against a
# vacuous pass), matching the sibling guards.
# --------------------------------------------------------------------------- #
def test_role_parses_to_non_trivial_task_list():
    """Sanity: the role file exists and parses to many tasks (non-vacuous).

    Guards against a silent no-op where a parse regression yields an empty task
    list and every ordering/gating assertion below passes vacuously. Mirrors the
    non-vacuous sanity checks in the sibling drift-guards.
    """
    assert _ROLE_TASKS.is_file(), (
        f"Expected the obs_clean_slate role tasks at {_ROLE_TASKS} — not found; "
        "the role layout may have moved, repoint this guard."
    )
    tasks = _load_role_tasks(_ROLE_TASKS)
    assert len(tasks) >= 12, (
        "DRIFT / vacuous-parse guard: the obs_clean_slate role must parse to a "
        f"substantial task list (>=12 tasks); parsed only {len(tasks)}. A "
        "near-empty parse would make the ordering/gating assertions pass "
        "vacuously."
    )
    assert _first_index(tasks, _is_guard_consult) is not None, (
        "DRIFT: no throwaway-guard consult (--service obs) task found in the role."
    )
    assert _first_index(tasks, _is_destructive_marker) is not None, (
        "DRIFT: no destructive marker (child clean-slate / terraform state rm / "
        "pct destroy) found in the role — the ordering anchors are missing."
    )


# =============================================================================
# Agent-live throwaway-guard consult is FIRST, gated, fail-closed.
# Validates: Requirements FR-18, AC-10 (agent-live safety preamble).
# =============================================================================
def test_guard_consult_and_rc_assert_precede_first_destruction():
    """Validates: Requirements FR-18, AC-10.

    The throwaway-guard consult AND its rc-assert (both gated
    ``when: agent_live_run``) must appear BEFORE the first destructive marker in
    the role's document order — so an armed non-throwaway run is REFUSED before
    ANY child clean-slate / state rm / pct runs. BITES if the guard is moved
    after any destructive marker.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    consult_idx = _first_index(tasks, _is_guard_consult)
    rc_assert_idx = _first_index(tasks, _is_guard_rc_assert)
    destroy_idx = _first_index(tasks, _is_destructive_marker)

    assert consult_idx is not None, (
        "DRIFT: no throwaway-guard consult (argv -> throwaway_guard --service obs) "
        "found in the role."
    )
    assert rc_assert_idx is not None, (
        "DRIFT: no rc-assert on the guard decision (*throwaway_decision.rc) found."
    )
    assert destroy_idx is not None, (
        "DRIFT: no destructive marker found — cannot anchor the "
        "guard-before-destruction ordering check."
    )

    # The consult is the VERY FIRST task of the role (the agent-live preamble).
    assert consult_idx == 0, (
        "DRIFT: the throwaway-guard consult must be the FIRST task of the role "
        f"(the agent-live preamble); found it at index={consult_idx}."
    )

    # Both guard tasks must be gated on the armed interlock so an ordinary
    # operator run skips them, while an armed run is gated fail-closed.
    assert "agent_live_run" in _when_text(tasks[consult_idx]), (
        "DRIFT: the throwaway-guard consult must be gated `when: agent_live_run`; "
        f"its when is {_when_text(tasks[consult_idx])!r}."
    )
    assert "agent_live_run" in _when_text(tasks[rc_assert_idx]), (
        "DRIFT: the guard rc-assert must be gated `when: agent_live_run`; its when "
        f"is {_when_text(tasks[rc_assert_idx])!r}."
    )

    assert consult_idx < destroy_idx, (
        "DRIFT: the throwaway-guard consult "
        f"(index={consult_idx}) must precede the FIRST destructive marker "
        f"(index={destroy_idx}). A non-throwaway target must be refused before "
        "anything is mutated."
    )
    assert rc_assert_idx < destroy_idx, (
        "DRIFT: the guard rc-assert (the fail-closed REFUSED halt, "
        f"index={rc_assert_idx}) must precede the first destructive marker "
        f"(index={destroy_idx})."
    )


# =============================================================================
# Confirmation-unless-force.
# Validates: Requirements FR-18, AC-10.
# =============================================================================
def test_confirmation_pause_gated_unless_force_and_abort_before_destruction():
    """Validates: Requirements FR-18, AC-10.

    The confirmation ``pause`` must be gated
    ``when: (clean_slate_confirm ...) != 'force'`` (so ``force`` skips the prompt),
    and a non-affirmative ``fail`` (the not-confirmed abort) must appear BEFORE
    the first destructive marker — so a declined/timed-out prompt aborts having
    touched nothing.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    pause_idx = _first_index(tasks, lambda t: _is_module(t, "ansible.builtin.pause", "pause"))
    assert pause_idx is not None, "DRIFT: no confirmation `pause` task found in the role."
    pause_when = _when_text(tasks[pause_idx]).replace(" ", "")
    assert "clean_slate_confirm" in pause_when and "!='force'" in pause_when, (
        "DRIFT: the confirmation pause must be gated "
        "`when: (clean_slate_confirm ...) != 'force'` so a force token skips the "
        f"prompt; its when is {_when_text(tasks[pause_idx])!r}."
    )

    # The not-confirmed abort: a `fail` gated on `not ... confirmed`.
    def _is_not_confirmed_fail(task: dict) -> bool:
        return _is_module(task, "ansible.builtin.fail", "fail") and "confirmed" in _when_text(task)

    abort_idx = _first_index(tasks, _is_not_confirmed_fail)
    assert abort_idx is not None, (
        "DRIFT: no not-confirmed `fail` abort (a fail gated on the resolved "
        "confirmed fact) found in the role."
    )

    destroy_idx = _first_index(tasks, _is_destructive_marker)
    assert destroy_idx is not None, (
        "DRIFT: no destructive marker found — cannot anchor the "
        "confirmation-before-destruction ordering check."
    )
    assert abort_idx < destroy_idx, (
        f"DRIFT: the not-confirmed abort `fail` (index={abort_idx}) must precede "
        f"the first destructive marker (index={destroy_idx}) — a "
        "non-affirmative/timed-out response must abort destroying nothing."
    )


# =============================================================================
# Three guests / ONE shared state — the pct-list survivor check covers all
# three VMIDs AND the state-list-empty check runs over the single shared state.
# Validates: Requirements FR-18, AC-10.
# =============================================================================
def test_pct_survivor_check_covers_all_three_guest_vmids():
    """Validates: Requirements FR-18, AC-10.

    The surviving-VMID set is computed from the ``pct list`` output over ALL
    THREE guest VMIDs (prometheus, grafana, loki) — not one, not two. This is the
    three-guests half of the two-way empty-baseline gate. BITES if a guest is
    dropped from the survivor computation.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # A pct list must be run on the host.
    assert any("pct list" in _task_argv_text(t) for t in tasks), (
        "DRIFT: no `pct list` task found — the host-survivor check is missing."
    )

    vmid_bodies = [
        b for t in tasks
        if (b := _set_fact_text(t, "obs_surviving_vmids")) is not None
    ]
    assert vmid_bodies, (
        "DRIFT: no set_fact computing `obs_surviving_vmids` found — the "
        "pct-list-derived guest survivor set is missing."
    )
    for body in vmid_bodies:
        for vmid_var in _GUEST_VMID_VARS:
            assert vmid_var in body, (
                "DRIFT (three-guests gate): the `obs_surviving_vmids` computation "
                f"must cover ALL THREE guest VMIDs; missing {vmid_var!r} in body: "
                f"{body!r}"
            )
        # It filters the three VMIDs against the pct list output (not a constant).
        assert "obs_pct_list" in body, (
            "DRIFT: the surviving-VMID set must be derived by selecting the three "
            f"VMIDs present in the `pct list` output; body: {body!r}"
        )


def test_state_list_empty_check_runs_over_the_single_shared_state():
    """Validates: Requirements FR-18, AC-10.

    The ``terraform state list`` survivor check runs over the ONE shared
    ``svc-17-18-observability`` state (chdir -> obs_tf_root), and the surviving
    state-address set enumerates EVERY remaining address (any non-blank line is a
    survivor) — so a non-guest ``terraform_data`` residue counts. This is the
    one-shared-state half of the gate.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # A `terraform state list` chdir'd into the single shared obs_tf_root.
    def _is_state_list_over_shared_state(task: dict) -> bool:
        argv = _task_argv_text(task)
        if not ("terraform" in argv and "state" in argv and "list" in argv):
            return False
        chdir = str((task.get("args") or {}).get("chdir", ""))
        return "obs_tf_root" in chdir

    state_lists = [t for t in tasks if _is_state_list_over_shared_state(t)]
    assert state_lists, (
        "DRIFT: no `terraform state list` chdir'd into the single shared "
        "`obs_tf_root` found — the state-empty check over the one shared "
        "svc-17-18-observability state is missing."
    )

    state_bodies = [
        b for t in tasks
        if (b := _set_fact_text(t, "obs_surviving_state_addrs")) is not None
    ]
    assert state_bodies, (
        "DRIFT: no set_fact computing `obs_surviving_state_addrs` found — the "
        "state-list-derived survivor set is missing."
    )
    for body in state_bodies:
        # Every non-blank state line is a survivor (so terraform_data residue
        # counts — no 'guest-only' filtering that would mask the residue).
        assert "splitlines" in body and "obs_state_list" in body, (
            "DRIFT: the surviving-state-address set must enumerate EVERY line of "
            f"the FULL `terraform state list` output; body: {body!r}"
        )


# =============================================================================
# Residue sweep — enumerate ALL remaining addresses and state-rm each
# PROVIDER-FREE (NOT terraform destroy).
# Validates: Requirements FR-18.
# =============================================================================
def test_residue_sweep_state_rms_all_remaining_addresses_provider_free():
    """Validates: Requirements FR-18.

    The container-only child clean-slate leaves the single shared state's
    non-guest ``terraform_data`` book-keeping behind
    (``terraform_data.ledger_index_assert`` + the three
    ``module.*.terraform_data.input_guards``). The sweep must:
      (a) list ALL remaining addresses, bind them to a residual-address set, then
      (b) ``terraform state rm`` EACH (loop over that set), PROVIDER-FREE —
          i.e. NEVER ``terraform destroy`` (which would contact the provider).
    BITES if the sweep is replaced by a destroy, or hard-codes only some
    addresses instead of enumerating all remaining ones.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # (a) A residual-address set is computed from a `terraform state list`.
    residual_bodies = [
        b for t in tasks
        if (b := _set_fact_text(t, "obs_residual_state_addrs")) is not None
    ]
    assert residual_bodies, (
        "DRIFT (residue sweep): no set_fact computing `obs_residual_state_addrs` "
        "found — the enumeration of ALL remaining addresses is missing."
    )
    for body in residual_bodies:
        assert "splitlines" in body, (
            "DRIFT (residue sweep): the residual-address set must enumerate ALL "
            f"remaining state addresses (splitlines over state list); body: {body!r}"
        )

    # (b) A `terraform state rm` loops over that residual set — provider-free.
    rm_tasks = [t for t in tasks if _is_state_rm(t)]
    assert rm_tasks, (
        "DRIFT (residue sweep): no `terraform state rm` task found — the "
        "provider-free residue removal is missing."
    )
    looped_rm = [
        t for t in rm_tasks
        if "obs_residual_state_addrs" in str(t.get("loop", ""))
    ]
    assert looped_rm, (
        "DRIFT (residue sweep): the `terraform state rm` must loop over "
        "`obs_residual_state_addrs` (remove EACH remaining address), so the "
        "non-guest terraform_data residue is cleared; no rm task loops over it."
    )
    for t in looped_rm:
        chdir = str((t.get("args") or {}).get("chdir", ""))
        assert "obs_tf_root" in chdir, (
            "DRIFT (residue sweep): the residue `terraform state rm` must run in "
            f"the single shared `obs_tf_root`; chdir={chdir!r}."
        )

    # PROVIDER-FREE: the role must NEVER run `terraform destroy` (it would
    # contact the provider and risk a hang / stale lock on a residue-only sweep).
    destroy_tasks = [
        t for t in tasks
        if "terraform" in _task_argv_text(t) and "destroy" in _task_argv_text(t)
    ]
    assert not destroy_tasks, (
        "DRIFT (residue sweep): the role must clear residue PROVIDER-FREE via "
        "`terraform state rm`, NEVER `terraform destroy`; found "
        f"{len(destroy_tasks)} destroy task(s)."
    )

    # Documentation signal: the role names the four residue addresses it targets,
    # so the intent (ledger_index_assert + three module input_guards) is pinned.
    role_text = _ROLE_TASKS.read_text(encoding="utf-8")
    for addr in _RESIDUE_ADDRS:
        assert addr in role_text, (
            "DRIFT (residue sweep): the role must document the non-guest "
            f"terraform_data residue it clears; missing reference to {addr!r}."
        )


# =============================================================================
# Final GATE — BOTH surviving sets empty (no false-clean).
# Validates: Requirements FR-18, AC-10.
# =============================================================================
def test_final_gate_requires_both_vmid_and_state_survivor_sets_empty():
    """Validates: Requirements FR-18, AC-10.

    The final empty-baseline ``assert`` must require BOTH the surviving-VMID set
    AND the surviving-state-address set be empty — a surviving resource of EITHER
    kind FAILS the gate (no false-clean). BITES if the gate drops one of the two
    survivor checks, or weakens the equality to a non-empty / truthy test.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    gate_asserts = [
        t for t in tasks
        if _is_assert(t)
        and "obs_surviving_vmids" in _assert_that_text(t)
        and "obs_surviving_state_addrs" in _assert_that_text(t)
    ]
    assert gate_asserts, (
        "DRIFT: no final empty-baseline `assert` found whose `that:` references "
        "BOTH `obs_surviving_vmids` AND `obs_surviving_state_addrs`. The gate must "
        "prove zero surviving guests (pct list) AND zero surviving state addresses "
        "(the single shared state incl. terraform_data residue) before reporting a "
        "clean baseline."
    )

    for gate in gate_asserts:
        that = _assert_that_text(gate)
        # Each survivor set is asserted EMPTY (length == 0), not merely referenced.
        assert "obs_surviving_vmids | length) == 0" in that or "obs_surviving_vmids|length)==0" in that.replace(" ", ""), (
            "DRIFT (no false-clean): the gate must assert the surviving-VMID set is "
            f"EMPTY (`(obs_surviving_vmids | length) == 0`); that: {that!r}"
        )
        assert (
            "obs_surviving_state_addrs | length) == 0" in that
            or "obs_surviving_state_addrs|length)==0" in that.replace(" ", "")
        ), (
            "DRIFT (no false-clean): the gate must assert the surviving-state set "
            f"is EMPTY (`(obs_surviving_state_addrs | length) == 0`); that: {that!r}"
        )
