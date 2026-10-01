"""Offline structural guards for the SVC-07 unsealer re-seal-on-handler-restart bugfix.

Bugfix: fix-svc07-unsealer-reseal-on-handler-restart.

Root cause (see bugfix.md / design.md): on a fresh-provision unsealer Play-1 run,
``openbao_install``'s first-run tasks (unsealer ``config.hcl`` template, mock-TLS
cert/key install, volume-mountpoint chown) each ``notify: Restart openbao``.
Ansible flushes handlers at END OF PLAY — AFTER the ``openbao_init_unseal`` role
in the SAME play has already run ``operator init`` + ``operator unseal``. The
end-of-play restart recreates the just-unsealed, Shamir-sealed unsealer (its
``config.hcl`` has NO ``seal`` stanza, so it does not auto-unseal), leaving it
``Sealed: true, Unseal Progress 0/3``. The primary's Transit auto-unseal then
gets ``503 Vault is sealed`` and crash-loops.

These tests encode the STRUCTURAL invariant whose ABSENCE causes the live bug
(design.md "Exploratory Bug Condition Checking"). They are pure-logic (PyYAML +
text parse of the committed playbook/roles) — no infrastructure needed. They
PARSE YAML only; they never embed or print any secret value.

**On the UNFIXED code these structural cases (1, 2, 3) FAIL** — the flush seam,
the post-flush re-unseal guard, and the already-initialised-sealed fail-loud
assert do not yet exist. That failure is the SUCCESS signal for a bug-condition
exploration test: it confirms the ordering defect's structural cause. The
``requires_infra`` live-repro stub (4) is deselected by default per the root
``pytest.ini`` (``addopts = -m "not requires_infra"``) and documents the live
from-scratch reproduction; it does not run offline.

The authoritative behavioural acceptance is the ``requires_infra`` from-scratch
bring-up (unsealer ends ``Sealed: false``, primary healthy); these guards pin
the ordering STRUCTURE so the fix cannot silently regress.

Test cases:
* 1 — Flush-seam-before-unseal: Play 1 has a ``meta: flush_handlers`` ordered
  BEFORE the ``openbao_init_unseal`` role/include (FAILS on unfixed code).
* 2 — Re-unseal-guard-present: ``unsealer_bootstrap.yml`` has a post-flush
  ``bao status`` re-check + conditional ``operator unseal`` on the in-memory
  threshold keys, ordered AFTER the STEP-3 post-unseal wait and BEFORE the
  STEP-4 Transit engine, with ``no_log`` on the key-bearing task (FAILS on
  unfixed code).
* 3 — Already-initialised-sealed-fail-loud: ``main.yml`` has a fail-closed
  assert/fail for the already-initialised-AND-sealed re-run path (FAILS on
  unfixed code).
* 4 — Live-repro stub (``requires_infra``, deselected by default): documents the
  fresh bring-up reproduction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_unsealer_reseal_ordering.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENBAO_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "openbao.yml"
_INIT_UNSEAL_ROLE = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal" / "tasks"
_UNSEALER_BOOTSTRAP = _INIT_UNSEAL_ROLE / "unsealer_bootstrap.yml"
_MAIN_TASKS = _INIT_UNSEAL_ROLE / "main.yml"


# --------------------------------------------------------------------------- #
# Helpers — same PyYAML idiom as test_unsealer_delegation.py.
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


def _load_plays(path: Path):
    """Return the list of play dicts.

    An Ansible playbook is a single YAML document whose top-level node is a LIST
    of plays; safe_load_all yields that one list. Flatten any list documents and
    keep the play dicts.
    """
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


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


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_argv_text(task: dict) -> str:
    """Return a flat string of a command/shell task's argv (+ stdin), else ''.

    Covers ``argv`` as a YAML list and as a folded-scalar Jinja expression (the
    role builds argv from a ``{{ [...] }}`` list), plus any ``stdin``.
    """
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
            if isinstance(spec.get("stdin"), str):
                parts.append(spec["stdin"])
    return " ".join(parts)


def _play1_ordered_nodes(play: dict) -> list[tuple[str, str]]:
    """Return an ordered list of (kind, identifier) nodes for Play 1.

    Ansible executes a play's sections in the fixed order pre_tasks -> roles ->
    tasks -> post_tasks (handlers flush at the boundaries / end of play). To
    reason about "is there a flush_handlers BEFORE the init/unseal role", we
    build one flat, execution-ordered sequence across those sections:

      * ("role", <role name>)            for each entry in `roles:`
      * ("meta", <meta arg>)             for a `meta:` task in tasks/post_tasks
      * ("include", <included file>)     for include_role/import_role/include_tasks
      * ("task", <first module name>)    for any other task

    This lets the assertions find a `meta: flush_handlers` seam and the
    `openbao_init_unseal` role/include regardless of whether the fix keeps a
    plain `roles:` list or restructures Play 1 into `tasks:` with explicit
    import_role/include_role around a `meta: flush_handlers`.
    """
    nodes: list[tuple[str, str]] = []

    def _emit_task(task: dict) -> None:
        if not isinstance(task, dict):
            return
        if "meta" in task:
            nodes.append(("meta", str(task["meta"])))
            return
        for inc_key in ("ansible.builtin.include_role", "include_role",
                        "ansible.builtin.import_role", "import_role"):
            spec = task.get(inc_key)
            if isinstance(spec, dict):
                nodes.append(("include", str(spec.get("name", ""))))
                return
            if isinstance(spec, str):
                nodes.append(("include", spec))
                return
        for inc_key in ("ansible.builtin.include_tasks", "include_tasks",
                        "ansible.builtin.import_tasks", "import_tasks"):
            spec = task.get(inc_key)
            if spec is not None:
                ident = spec.get("file", "") if isinstance(spec, dict) else str(spec)
                nodes.append(("include", str(ident)))
                return
        mods = _task_module_names(task)
        nodes.append(("task", next(iter(sorted(mods)), "<none>")))

    for section in ("pre_tasks",):
        for task in _iter_tasks(play.get(section)):
            _emit_task(task)

    for role in play.get("roles", []) or []:
        if isinstance(role, dict):
            nodes.append(("role", str(role.get("role", role.get("name", "")))))
        elif isinstance(role, str):
            nodes.append(("role", role))

    for section in ("tasks", "post_tasks"):
        for task in _iter_tasks(play.get(section)):
            _emit_task(task)

    return nodes


def _index_of_init_unseal(nodes: list[tuple[str, str]]) -> int | None:
    """Return the index of the openbao_init_unseal role/include node, else None."""
    for i, (kind, ident) in enumerate(nodes):
        if kind in ("role", "include") and "openbao_init_unseal" in ident:
            return i
    return None


def _index_of_flush_handlers(nodes: list[tuple[str, str]]) -> int | None:
    """Return the index of a `meta: flush_handlers` node, else None."""
    for i, (kind, ident) in enumerate(nodes):
        if kind == "meta" and "flush_handlers" in ident:
            return i
    return None


# --------------------------------------------------------------------------- #
# Test case 1 — Flush-seam-before-unseal (Play 1).
# Validates: Requirements 2.1 (the fixed structural invariant).
# EXPECTED on unfixed code: FAILS (no meta: flush_handlers before the role).
# --------------------------------------------------------------------------- #
def test_flush_handlers_seam_precedes_init_unseal_in_play1():
    """Bug condition (Property 1) — Validates: Requirement 2.1.

    Play 1 (``hosts: openbao-unsealer``) MUST order a ``meta: flush_handlers``
    seam BEFORE the ``openbao_init_unseal`` role/include, so any
    ``Restart openbao`` handler queued by ``openbao_install``'s first-run
    config/cert/chown tasks is flushed while the instance is still
    uninitialised/sealed — making the unseal the LAST thing that touches the
    unsealer.

    On UNFIXED code there is no such seam (Play 1 is a plain ``roles:`` list),
    so this assertion FAILS — confirming the ordering defect.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    play1 = next((p for p in plays if p.get("hosts") == "openbao-unsealer"), None)
    assert play1 is not None, "Play 1 (hosts: openbao-unsealer) not found in openbao.yml."

    nodes = _play1_ordered_nodes(play1)
    init_idx = _index_of_init_unseal(nodes)
    assert init_idx is not None, (
        "openbao_init_unseal role/include not found in Play 1 — cannot check ordering."
    )

    flush_idx = _index_of_flush_handlers(nodes)
    assert flush_idx is not None, (
        "COUNTEREXAMPLE (bug): no `meta: flush_handlers` seam exists in Play 1 "
        "(hosts: openbao-unsealer). Any `Restart openbao` handler queued by "
        "openbao_install's first-run tasks is only flushed at END OF PLAY — "
        "after openbao_init_unseal has already unsealed the instance — re-sealing "
        "the Shamir-sealed unsealer. The fix must insert a flush_handlers seam "
        "between openbao_install and openbao_init_unseal."
    )
    assert flush_idx < init_idx, (
        "COUNTEREXAMPLE (bug): the `meta: flush_handlers` seam in Play 1 is "
        f"ordered at/after the openbao_init_unseal role/include "
        f"(flush index={flush_idx}, init/unseal index={init_idx}). The flush must "
        "precede init/unseal so the container recreate happens before the unseal, "
        "not after it."
    )


# --------------------------------------------------------------------------- #
# Test case 1b — Flush-seam-runs-under-`--tags install,init` (Play 1).
# Validates: Requirement 2.1 (the seam must actually FIRE under the orchestrator
# invocation, not merely exist).
# EXPECTED on a tag-inert (untagged) seam: FAILS.
# --------------------------------------------------------------------------- #
def _play1_flush_handlers_tasks(play: dict) -> list[dict]:
    """Return every `meta: flush_handlers` task dict in Play 1 (tasks/post_tasks).

    Unlike `_play1_ordered_nodes` (which flattens to (kind, ident) tuples and
    drops the task's other keys), this returns the raw task dicts so a caller can
    inspect the seam's `tags:`.
    """
    seams: list[dict] = []
    for section in ("pre_tasks", "tasks", "post_tasks"):
        for task in _iter_tasks(play.get(section)):
            if str(task.get("meta", "")) == "flush_handlers":
                seams.append(task)
    return seams


def _task_tags(task: dict) -> list[str]:
    """Return a task's `tags:` as a list of strings (str or list forms)."""
    tags = task.get("tags", [])
    if isinstance(tags, str):
        return [tags]
    if isinstance(tags, (list, tuple)):
        return [str(t) for t in tags]
    return []


def test_flush_handlers_seam_runs_under_install_init_tags():
    """Bug condition (Property 1) — Validates: Requirement 2.1.

    The Play-1 (``hosts: openbao-unsealer``) ``meta: flush_handlers`` seam MUST
    carry a ``tags:`` value that causes it to RUN under the orchestrator's
    ``--tags "install,init"`` invocation (svc-07-bootstrap.yml Task 8.2) —
    ``always`` (which the fix uses), or explicitly ``install`` / ``init``.

    WHY this is separate from the ordering test: an UNTAGGED
    ``meta: flush_handlers`` is filtered OUT under a ``--tags`` run, so the seam
    never fires and the ``Restart openbao`` handler falls back to flushing at
    END OF PLAY — recreating the just-unsealed Shamir unsealer as SEALED. The
    ordering test (``test_flush_handlers_seam_precedes_init_unseal_in_play1``)
    asserts the seam EXISTS and is ordered before init/unseal, but a tag-inert
    seam still satisfies that while being a no-op at runtime. This test closes
    that gap: it FAILS on a seam that lacks a run-under-``install,init`` tag.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    play1 = next((p for p in plays if p.get("hosts") == "openbao-unsealer"), None)
    assert play1 is not None, "Play 1 (hosts: openbao-unsealer) not found in openbao.yml."

    seams = _play1_flush_handlers_tasks(play1)
    assert seams, (
        "No `meta: flush_handlers` seam found in Play 1 — the converge-then-unseal "
        "seam is missing entirely (see "
        "test_flush_handlers_seam_precedes_init_unseal_in_play1)."
    )

    # The orchestrator runs `openbao.yml --tags "install,init"`. A seam that runs
    # under that filter carries `always` (unconditional), or one of the invoked
    # tags `install`/`init`. An untagged seam (tags == []) is filtered out and is
    # the exact tag-inert bug this test guards against.
    _RUN_UNDER_INSTALL_INIT = {"always", "install", "init"}
    tagged_seams = [s for s in seams if _RUN_UNDER_INSTALL_INIT & set(_task_tags(s))]
    assert tagged_seams, (
        "COUNTEREXAMPLE (bug): the Play-1 `meta: flush_handlers` seam carries no "
        "tag that runs under the orchestrator's `--tags \"install,init\"` "
        "invocation (svc-07-bootstrap.yml Task 8.2). An untagged flush_handlers is "
        "SKIPPED under `--tags`, so the seam never fires mid-play and the "
        "`Restart openbao` handler flushes at END OF PLAY — recreating the "
        "just-unsealed Shamir unsealer as SEALED. The seam MUST carry "
        f"tags including one of {sorted(_RUN_UNDER_INSTALL_INIT)} (the fix uses "
        f"`always`). Seam tags seen: {[_task_tags(s) for s in seams]}."
    )


# --------------------------------------------------------------------------- #
# Test case 2 — Re-unseal-guard-present (unsealer_bootstrap.yml).
# Validates: Requirement 2.2 (the fixed structural invariant).
# EXPECTED on unfixed code: FAILS (no post-flush re-unseal guard exists).
# --------------------------------------------------------------------------- #
def _argv_tokens(task: dict) -> list[str]:
    """Whitespace-delimited argv/stdin tokens for a command/shell task.

    Token-precise (NOT substring) matching matters here: the unsealer's argv
    interpolates ``openbao_unsealer_*`` variable NAMES whose text contains
    ``unseal`` as a SUBSTRING (e.g. ``openbao_unsealer_container_name``), so a
    naive ``'unseal' in argv`` wrongly flags the ``operator init`` task as an
    unseal. Splitting into tokens and stripping surrounding punctuation makes
    ``operator``/``unseal``/``init``/``status``/``enable`` match the actual bao
    subcommand tokens only.
    """
    raw = _task_argv_text(task)
    toks: list[str] = []
    for piece in raw.replace(",", " ").replace("'", " ").split():
        toks.append(piece.strip("[](){}'\""))
    return toks


def _is_docker_exec_bao(tokens: list[str]) -> bool:
    return "docker" in tokens and "exec" in tokens


def _is_status_recheck(task: dict) -> bool:
    """A `docker exec ... status` command task (a `bao status` re-check)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "status" in toks


def _is_operator_unseal(task: dict) -> bool:
    """A `docker exec ... operator unseal` command task (token-precise).

    Requires the ``operator`` and ``unseal`` argv TOKENS together, so it never
    matches ``operator init`` nor an ``openbao_unsealer_*`` variable name.
    """
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "operator" in toks and "unseal" in toks


def _is_transit_enable(task: dict) -> bool:
    """A STEP-4 `secrets enable ... transit` command task (the Transit engine)."""
    toks = _argv_tokens(task)
    return (
        _is_docker_exec_bao(toks)
        and "secrets" in toks
        and "enable" in toks
        and "transit" in toks
    )


def _references_in_memory_threshold_keys(task: dict) -> bool:
    """True if the task's argv/loop references the in-memory Shamir threshold keys."""
    haystack = _task_argv_text(task) + " " + str(task.get("loop", ""))
    return "openbao_unsealer_unseal_keys" in haystack


def test_post_flush_reunseal_guard_between_step3_and_step4():
    """Bug condition (Property 1) — Validates: Requirement 2.2.

    ``unsealer_bootstrap.yml`` MUST contain a post-flush re-unseal guard — a
    ``bao status`` re-check plus a conditional ``operator unseal`` on the
    in-memory threshold keys (``openbao_unsealer_unseal_keys``) — ordered AFTER
    the STEP-3 post-unseal readiness wait and BEFORE the STEP-4 Transit-engine
    enable, so that ANY restart within the run cannot leave the unsealer sealed
    before the Transit/policy/mint writes. The key-bearing re-unseal task must
    set ``no_log``.

    On UNFIXED code STEP-3 is followed directly by STEP-4 with no such guard, so
    this FAILS — confirming the missing defence-in-depth net.
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)

    # STEP-4 boundary: the first Transit-engine enable. The guard must live
    # strictly BEFORE it (so no Transit/policy/mint write fires against a
    # re-sealed backend).
    transit_idx = next((i for i, t in enumerate(tasks) if _is_transit_enable(t)), None)
    assert transit_idx is not None, (
        "STEP-4 Transit-engine enable task not found in unsealer_bootstrap.yml — "
        "cannot locate the guard window."
    )

    # STEP-3 initial unseal (the threshold loop) — the FIRST operator-unseal task.
    # It is UNCONDITIONAL (no `when:` on sealed status) and loops over the keys.
    step3_unseal_idx = next(
        (i for i, t in enumerate(tasks) if _is_operator_unseal(t)), None
    )
    assert step3_unseal_idx is not None and step3_unseal_idx < transit_idx, (
        "STEP-3 `operator unseal` task not found before the Transit engine — "
        "unexpected role structure."
    )

    # The guard, in the window between the STEP-3 unseal and STEP-4, is the
    # combination of:
    #   * a `bao status` re-check (a `docker exec ... status` task), AND
    #   * a SUBSEQUENT, sealed-CONDITIONAL `operator unseal` on the in-memory
    #     threshold keys — i.e. an unseal carrying a `when:` keyed on `sealed`.
    # The STEP-3 unseal itself is UNCONDITIONAL, so requiring `when: ... sealed`
    # distinguishes the guard's re-unseal from the initial unseal and is exactly
    # what is ABSENT on unfixed code.
    window = list(range(step3_unseal_idx + 1, transit_idx))
    recheck_idx = next((i for i in window if _is_status_recheck(tasks[i])), None)
    guard_unseal_idx = next(
        (
            i
            for i in window
            if _is_operator_unseal(tasks[i])
            and _references_in_memory_threshold_keys(tasks[i])
            and _mentions_sealed(_condition_text(tasks[i]))
        ),
        None,
    )

    assert recheck_idx is not None and guard_unseal_idx is not None, (
        "COUNTEREXAMPLE (bug): no post-flush re-unseal guard in "
        "unsealer_bootstrap.yml between the STEP-3 post-unseal wait and the "
        "STEP-4 Transit-engine enable. There is no `bao status` re-check plus a "
        "sealed-conditional `operator unseal` on the in-memory threshold keys "
        "(openbao_unsealer_unseal_keys, gated on `when: ... sealed`), so a "
        "restart after the initial unseal leaves the Shamir-sealed unsealer "
        "sealed with nothing to re-unseal it. "
        f"(status re-check found={recheck_idx is not None}, "
        f"sealed-conditional guard unseal found={guard_unseal_idx is not None})"
    )
    assert recheck_idx < guard_unseal_idx, (
        "The re-unseal guard's `bao status` re-check must precede its conditional "
        f"`operator unseal` (re-check index={recheck_idx}, "
        f"guard unseal index={guard_unseal_idx})."
    )
    # The key-bearing guard re-unseal must be no_log (secret discipline, Req 3.5).
    assert "no_log" in tasks[guard_unseal_idx], (
        "The re-unseal guard's `operator unseal` carries Shamir threshold keys "
        "and MUST set no_log."
    )


# --------------------------------------------------------------------------- #
# Test case 3 — Already-initialised-sealed-fail-loud (main.yml).
# Validates: Requirement 2.4 (the fixed structural invariant).
# EXPECTED on unfixed code: FAILS (no such fail-closed assert exists).
# --------------------------------------------------------------------------- #
def _condition_text(task: dict) -> str:
    """Flatten a task's `when:` (str or list) into one lowercase string."""
    when = task.get("when", "")
    if isinstance(when, (list, tuple)):
        return " ".join(str(c) for c in when).lower()
    return str(when).lower()


def _mentions_sealed(text: str) -> bool:
    """True if the text references a sealed status (the derived sealed fact)."""
    return "sealed" in text


def _mentions_already_initialized(text: str) -> bool:
    return "openbao_already_initialized" in text


def test_already_initialised_sealed_reruns_fail_loud_in_main():
    """Preservation/Expected (Property 2) — Validates: Requirement 2.4.

    ``main.yml`` MUST fail closed on the already-initialised-AND-sealed re-run
    path (``openbao_already_initialized`` true AND the instance reports
    ``sealed: true``): the role holds NO Shamir keys (the operator holds them
    offline), so it must emit a clear, actionable re-unseal message rather than
    proceeding to a confusing downstream primary ``503``. It must NOT fire on the
    healthy initialised+unsealed no-op case.

    On UNFIXED code there is no such assert/fail, so this FAILS — confirming the
    missing fail-loud diagnostic.
    """
    tasks = _load_task_file(_MAIN_TASKS)

    fail_loud = None
    for task in tasks:
        mods = _task_module_names(task)
        is_gate = ("ansible.builtin.assert" in mods) or ("ansible.builtin.fail" in mods)
        if not is_gate:
            continue
        cond = _condition_text(task)
        body = str(task.get("ansible.builtin.assert", "")) + " " + str(
            task.get("ansible.builtin.fail", "")
        )
        combined = (cond + " " + body).lower()
        # The gate must be keyed on BOTH already-initialised AND a sealed status
        # (so it does not fire on the healthy initialised+unsealed no-op).
        if _mentions_already_initialized(combined) and _mentions_sealed(combined):
            fail_loud = task
            break

    assert fail_loud is not None, (
        "COUNTEREXAMPLE (bug): main.yml has no fail-closed assert/fail for the "
        "already-initialised-AND-sealed re-run path. When "
        "openbao_already_initialized is true AND the unsealer reports sealed:true "
        "(the role holds no offline Shamir keys), the run currently proceeds "
        "silently to a downstream primary 503 instead of failing loudly with an "
        "actionable 're-unseal with the offline-held threshold keys' message "
        "(see INSTALL-RUNBOOK §2)."
    )


# --------------------------------------------------------------------------- #
# Test case 4 — Live-repro stub (requires_infra; deselected by default).
# Validates: Requirements 2.1, 2.2, 2.3 behaviourally (the live acceptance).
# --------------------------------------------------------------------------- #
@pytest.mark.requires_infra
def test_live_from_scratch_unsealer_ends_unsealed_and_primary_healthy():
    """Live from-scratch reproduction (requires_infra — deselected by default).

    DOCUMENTED live gate; not executed offline. On a throwaway cluster, a
    from-scratch Developer-B bring-up of SVC-07:

    UNFIXED code (reproduces the bug):
      * The unsealer (``svc07-unsealer-20-01`` / ``10.0.20.11``) ends the run
        ``Seal Type shamir, Initialized true, Sealed true, Unseal Progress 0/3``
        — the end-of-play ``Restart openbao`` handler recreated the just-unsealed
        Shamir-sealed unsealer, which came back sealed.
      * The primary (``openbao-openbao-1`` / ``10.0.20.10``) crash-loops with
        ``503 Vault is sealed`` on its Transit auto-unseal calls to
        ``PUT https://10.0.20.11:8200/v1/transit/encrypt/openbao-unseal``.

    FIXED code (the acceptance):
      * The unsealer ends the run ``Sealed: false`` (converge-then-unseal +
        in-memory re-unseal guard), the primary auto-unseals and reports healthy,
        and the single-command Developer-B bring-up completes end-to-end.

    This stub is marked ``@pytest.mark.requires_infra`` and is therefore
    DESELECTED by default via the root ``pytest.ini`` (``addopts = -m "not
    requires_infra"``). The authoritative behavioural evidence is captured in the
    spec's validation.md from a real throwaway-cluster run; it must never echo any
    secret value (reference ``bao status`` fields only).
    """
    pytest.skip(
        "requires-infra: live from-scratch SVC-07 bring-up on a throwaway cluster "
        "(unsealer ends Sealed:false, primary healthy). Documented live gate; run "
        "under -m requires_infra with a wired-up cluster. See validation.md."
    )

# ===========================================================================
# ===========================================================================
# TASK 2 — PRESERVATION property tests (Property 2).
# ===========================================================================
# ===========================================================================
#
# These are PRESERVATION guards (design.md "Preservation Checking", Property 2).
# UNLIKE the bug-condition cases above (1, 2, 3 — which FAIL on unfixed code and
# only PASS once the fix lands), the cases below encode the CURRENT committed
# baseline behaviour and therefore **PASS on the UNFIXED code**. They pin the
# invariants the fix must NOT regress:
#
#   P2-1 Idempotency gate preserved — the openbao_init_unseal unsealer bootstrap
#        include in main.yml stays gated on BOTH `openbao_role == "unsealer"` and
#        `not openbao_already_initialized` (Req 3.1).
#   P2-2 Primary flow unchanged — openbao.yml Play 2 (`hosts: openbao`) keeps its
#        role order openbao_install -> openbao_init_unseal -> openbao_project_onboard
#        and its `openbao_role: primary` play var (Req 3.2).
#   P2-3 Two ordered plays, unsealer first — openbao.yml has exactly two plays,
#        Play 1 `hosts: openbao-unsealer`, Play 2 `hosts: openbao` (structural
#        invariant the fix must preserve while it inserts the flush seam).
#   P2-4 Secret discipline preserved — every EXISTING secret-bearing task in
#        unsealer_bootstrap.yml that carries a Shamir key / root token / seal
#        token (operator init, operator unseal, policy write, token create, token
#        revoke, and the in-memory capture/scrub set_facts) sets `no_log`
#        (literal `true` or `"{{ openbao_no_log }}"`) (Req 3.5).
#
# The fix (Task 3) ADDS a flush seam, a new re-unseal-guard task, and a fail-loud
# assert; these preservation cases assert that doing so does not remove or weaken
# any of the four baseline invariants above. They reuse the helpers defined in
# the Task-1 section (`_load_plays`, `_load_task_file`, `_iter_tasks`,
# `_task_module_names`, `_argv_tokens`, `_is_docker_exec_bao`, `_is_operator_unseal`,
# `_condition_text`) — no duplicate helpers are introduced.
#
# Parse-only: these tests read YAML structure; they never embed or print any
# secret value.


def _play2_role_names(play: dict) -> list[str]:
    """Return Play-2 role names in declared order (roles: list of dicts/strs).

    Retained for reference; the POST-fix Play 2 is a ``tasks:`` list (empty
    ``roles:``), so use ``_play2_ordered_nodes`` below to read imported role
    names + the flush seam from the ``tasks:`` structure.
    """
    names: list[str] = []
    for role in play.get("roles", []) or []:
        if isinstance(role, dict):
            names.append(str(role.get("role", role.get("name", ""))))
        elif isinstance(role, str):
            names.append(role)
    return names


def _play2_ordered_nodes(play: dict) -> list[tuple[str, str, list[str]]]:
    """Return an ordered (kind, identifier, tags) node list for Play 2.

    Sibling of ``_play1_ordered_nodes`` (above), generalised to read imported
    role names from ``import_role``/``include_role`` task specs and to carry each
    node's ``tags:`` (so the flush seam's ``tags: ["always"]`` can be asserted).
    Works whether Play 2 is a plain ``roles:`` list (UNFIXED) or a ``tasks:`` list
    of ``import_role`` calls around a ``meta: flush_handlers`` (FIXED — Task 3.1).

    Node kinds:
      * ("role",   <role name>,        <tags>)  for each entry in `roles:`
      * ("meta",   <meta arg>,         <tags>)  for a `meta:` task
      * ("import", <imported role/file>,<tags>) for import_role/include_role/
                                                 include_tasks/import_tasks
      * ("task",   <first module name>,<tags>)  for any other task
    """
    nodes: list[tuple[str, str, list[str]]] = []

    def _emit_task(task: dict) -> None:
        if not isinstance(task, dict):
            return
        tags = _task_tags(task)
        if "meta" in task:
            nodes.append(("meta", str(task["meta"]), tags))
            return
        for inc_key in ("ansible.builtin.include_role", "include_role",
                        "ansible.builtin.import_role", "import_role"):
            spec = task.get(inc_key)
            if isinstance(spec, dict):
                nodes.append(("import", str(spec.get("name", "")), tags))
                return
            if isinstance(spec, str):
                nodes.append(("import", spec, tags))
                return
        for inc_key in ("ansible.builtin.include_tasks", "include_tasks",
                        "ansible.builtin.import_tasks", "import_tasks"):
            spec = task.get(inc_key)
            if spec is not None:
                ident = spec.get("file", "") if isinstance(spec, dict) else str(spec)
                nodes.append(("import", str(ident), tags))
                return
        mods = _task_module_names(task)
        nodes.append(("task", next(iter(sorted(mods)), "<none>"), tags))

    for section in ("pre_tasks",):
        for task in _iter_tasks(play.get(section)):
            _emit_task(task)

    for role in play.get("roles", []) or []:
        if isinstance(role, dict):
            nodes.append(("role", str(role.get("role", role.get("name", ""))),
                          _task_tags(role)))
        elif isinstance(role, str):
            nodes.append(("role", role, []))

    for section in ("tasks", "post_tasks"):
        for task in _iter_tasks(play.get(section)):
            _emit_task(task)

    return nodes


def _play2_imported_role_names(play: dict) -> list[str]:
    """Return Play-2 imported/declared role names in execution order.

    Reads both the FIXED ``tasks:``+``import_role`` form and the UNFIXED
    ``roles:`` form via ``_play2_ordered_nodes`` — role/import nodes only, in
    order (the flush-seam `meta` node is skipped).
    """
    return [
        ident
        for kind, ident, _tags in _play2_ordered_nodes(play)
        if kind in ("role", "import")
    ]


def _no_log_is_set(task: dict) -> bool:
    """True if the task sets no_log to a non-false value.

    Accepts the two idioms used across this role: a literal ``true`` and the
    templated ``"{{ openbao_no_log }}"`` (the role's per-run secret-suppression
    toggle, which defaults true). A missing no_log, or ``no_log: false``, is
    NOT accepted.
    """
    if "no_log" not in task:
        return False
    val = task["no_log"]
    if isinstance(val, bool):
        return val is True
    text = str(val).strip().lower()
    if text in ("false", "no", "0", ""):
        return False
    # A literal "true"/"yes" or any Jinja expression (e.g. "{{ openbao_no_log }}").
    return True


# --------------------------------------------------------------------------- #
# P2-1 — Idempotency gate preserved (main.yml).
# Validates: Requirement 3.1 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_unsealer_bootstrap_include_idempotency_gate():
    """Preservation (Property 2) — Validates: Requirement 3.1.

    The ``include_tasks: unsealer_bootstrap.yml`` in ``main.yml`` MUST stay gated
    on BOTH ``openbao_role == "unsealer"`` and ``not openbao_already_initialized``
    so a re-run against an already-initialised unsealer is a ``changed=0`` no-op.
    This baseline holds on the UNFIXED code; the fix must not remove or loosen it.
    """
    tasks = _load_task_file(_MAIN_TASKS)

    include = None
    for task in tasks:
        for inc_key in ("ansible.builtin.include_tasks", "include_tasks"):
            spec = task.get(inc_key)
            ident = spec.get("file", "") if isinstance(spec, dict) else str(spec or "")
            if "unsealer_bootstrap.yml" in ident:
                include = task
                break
        if include is not None:
            break

    assert include is not None, (
        "The `include_tasks: unsealer_bootstrap.yml` was not found in main.yml — "
        "cannot verify its idempotency gate."
    )

    cond = _condition_text(include)
    assert 'openbao_role == "unsealer"' in cond or "openbao_role == 'unsealer'" in cond, (
        "The unsealer bootstrap include must remain gated on "
        f"openbao_role == \"unsealer\" (its when: was {include.get('when')!r})."
    )
    assert "not openbao_already_initialized" in cond, (
        "PRESERVATION: the unsealer bootstrap include must remain gated on "
        "`not openbao_already_initialized` (the idempotency gate, Req 3.1) — the "
        f"fix must not remove it (its when: was {include.get('when')!r})."
    )


# --------------------------------------------------------------------------- #
# P2-2 — Primary flow unchanged (openbao.yml Play 2).
# Validates: Requirement 3.2 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_primary_play_role_order_and_role_var():
    """Preservation (Property 2, P2-1) — Validates: Requirements 3.2, 3.5.

    INTENTIONAL structure change (Fix B restructures Play 2 to
    ``tasks:``+``import_role``+seam); RED-until-fix, passes after Task 3.1/3.2
    applies the restructure. NOT a weakening — this guard still enforces the same
    three imported roles in the same order
    (``openbao_install`` -> ``openbao_init_unseal`` -> ``openbao_project_onboard``)
    and ``openbao_role: primary`` as a play var, and ADDITIONALLY now asserts the
    converge-then-bootstrap ``meta: flush_handlers`` seam (tagged ``always``) sits
    BETWEEN the install import and the init/unseal import.

    Observation-first: on the UNFIXED tree Play 2 is a plain ``roles:`` list with
    NO flush seam, so this assertion FAILS by design (the old ``roles:``-reading
    assertion this replaces was observed to PASS on the unfixed tree). It PASSES
    only after Task 3.1 restructures Play 2 to a ``tasks:`` list of
    ``import_role`` calls around a ``tags: ["always"]`` ``meta: flush_handlers``
    seam — mirroring the committed Play-1 unsealer seam.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    play2 = next((p for p in plays if p.get("hosts") == "openbao"), None)
    assert play2 is not None, "Play 2 (hosts: openbao) not found in openbao.yml."

    # Invariant carried over from the OLD guard: the play var is unchanged.
    assert str(play2.get("vars", {}).get("openbao_role")) == "primary", (
        "PRESERVATION: Play 2 must keep `openbao_role: primary` as a play var "
        f"(got {play2.get('vars', {}).get('openbao_role')!r})."
    )

    # Invariant carried over: the same three roles, in the same order — but now
    # read as imported role names from the `tasks:`+import_role structure (the
    # OLD guard read `roles:`, which the restructure empties). This works on
    # BOTH forms via `_play2_imported_role_names`.
    role_names = _play2_imported_role_names(play2)
    assert role_names == [
        "openbao_install",
        "openbao_init_unseal",
        "openbao_project_onboard",
    ], (
        "P2-1: Play 2 must import the three roles in order openbao_install -> "
        "openbao_init_unseal -> openbao_project_onboard (via `tasks:`+import_role "
        "post-fix; RED until Task 3.1 restructures Play 2) — got "
        f"{role_names}. On the UNFIXED tree Play 2 is still a `roles:` list, so "
        "this reads them from `roles:`; after the fix it reads them from the "
        "import_role task specs. The invariant (three roles, same order) is "
        "unchanged — only where they are declared changed."
    )

    # NEW, STRONGER invariant added by Fix B: a `meta: flush_handlers` seam,
    # tagged `always`, sits strictly BETWEEN the openbao_install import and the
    # openbao_init_unseal import. RED on the unfixed tree (no seam), green after
    # Task 3.1. This is the converge-then-bootstrap seam that makes the primary's
    # config.hcl / MOCK-TLS recreate flush BEFORE bootstrap.
    nodes = _play2_ordered_nodes(play2)

    def _idx_of(role_name: str) -> int | None:
        for i, (kind, ident, _tags) in enumerate(nodes):
            if kind in ("role", "import") and role_name in ident:
                return i
        return None

    install_idx = _idx_of("openbao_install")
    init_idx = _idx_of("openbao_init_unseal")
    assert install_idx is not None and init_idx is not None, (
        "P2-1: could not locate the openbao_install and openbao_init_unseal "
        f"imports in Play 2 (install={install_idx}, init={init_idx})."
    )

    seam_idx = next(
        (
            i
            for i, (kind, ident, _tags) in enumerate(nodes)
            if kind == "meta" and "flush_handlers" in ident
        ),
        None,
    )
    assert seam_idx is not None, (
        "P2-1 (RED until Task 3.1): Play 2 has NO `meta: flush_handlers` seam. "
        "Fix B restructures Play 2 to a `tasks:` list importing openbao_install "
        "-> (meta: flush_handlers, tagged always) -> openbao_init_unseal -> "
        "openbao_project_onboard, mirroring the committed Play-1 unsealer seam. "
        "On the unfixed tree Play 2 is a plain `roles:` list with no seam — this "
        "failure is EXPECTED and by design."
    )
    assert install_idx < seam_idx < init_idx, (
        "P2-1: the Play-2 `meta: flush_handlers` seam must sit strictly BETWEEN "
        f"the openbao_install import and the openbao_init_unseal import "
        f"(install={install_idx}, seam={seam_idx}, init={init_idx})."
    )
    seam_tags = nodes[seam_idx][2]
    assert "always" in seam_tags, (
        "P2-1: the Play-2 `meta: flush_handlers` seam must carry `tags: "
        '["always"]` so it runs under the orchestrator\'s `--tags "install,init"` '
        f"invocation (svc-07-bootstrap.yml Task 8.4). Seam tags seen: {seam_tags}."
    )


# --------------------------------------------------------------------------- #
# P2-3 — Two ordered plays, unsealer first (openbao.yml).
# Validates: preservation of the two-play structure the fix must keep.
# --------------------------------------------------------------------------- #
def test_preserve_two_ordered_plays_unsealer_first():
    """Preservation (Property 2) — Validates: Requirements 3.2, 3.3.

    ``openbao.yml`` MUST have exactly two plays, Play 1 ``hosts:
    openbao-unsealer`` and Play 2 ``hosts: openbao`` (unsealer first). The fix
    inserts a ``meta: flush_handlers`` seam WITHIN Play 1 but must not add,
    remove, reorder, or retarget the plays. This baseline holds on the UNFIXED
    code.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    assert len(plays) == 2, (
        f"openbao.yml must have exactly two ordered plays; found {len(plays)}."
    )
    assert plays[0].get("hosts") == "openbao-unsealer", (
        "Play 1 must target hosts: openbao-unsealer (unsealer first) — got "
        f"{plays[0].get('hosts')!r}."
    )
    assert plays[1].get("hosts") == "openbao", (
        f"Play 2 must target hosts: openbao (primary second) — got {plays[1].get('hosts')!r}."
    )


@pytest.mark.parametrize("openbao_role", ["primary", "unsealer"])
def test_preserve_two_plays_unsealer_first_holds_for_any_role(openbao_role):
    """Preservation (Property 2) — parametrised over the two valid roles.

    The two-plays / unsealer-first structural invariant is independent of which
    ``openbao_role`` a run selects: for BOTH valid roles (``primary`` and
    ``unsealer``) the parsed ``openbao.yml`` still exposes exactly two plays with
    the unsealer play first. This mirrors the design's "for any ``openbao_role``,
    the preservation invariants hold" property without needing Hypothesis (the
    domain is the closed two-element set validated by ``main.yml``'s assert).
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    hosts_in_order = [p.get("hosts") for p in plays]
    assert hosts_in_order == ["openbao-unsealer", "openbao"], (
        f"For openbao_role={openbao_role!r}, openbao.yml must still be two ordered "
        f"plays unsealer-first; got {hosts_in_order}."
    )


# --------------------------------------------------------------------------- #
# P2-4 — Secret discipline preserved (unsealer_bootstrap.yml).
# Validates: Requirement 3.5 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def _is_operator_init(task: dict) -> bool:
    """A `docker exec ... operator init` command task (token-precise)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "operator" in toks and "init" in toks


def _is_policy_write(task: dict) -> bool:
    """A `docker exec ... policy write` command task."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "policy" in toks and "write" in toks


def _is_token_create(task: dict) -> bool:
    """A `docker exec ... token create` command task (mints the seal token)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "token" in toks and "create" in toks


def _is_token_revoke(task: dict) -> bool:
    """A `docker exec ... token revoke` command task (revokes the root token)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "token" in toks and "revoke" in toks


def _is_secret_capture_set_fact(task: dict) -> bool:
    """A set_fact that binds/scrubs a Shamir-key / root-token / seal-token fact."""
    mods = _task_module_names(task)
    if "ansible.builtin.set_fact" not in mods and "set_fact" not in mods:
        return False
    body = str(task.get("ansible.builtin.set_fact", task.get("set_fact", "")))
    secret_fact_markers = (
        "openbao_unsealer_unseal_keys",
        "openbao_unsealer_init_root_token",
        "openbao_unsealer_effective_token",
        "openbao_seal_token",
    )
    return any(marker in body for marker in secret_fact_markers)


def test_preserve_existing_secret_bearing_tasks_set_no_log():
    """Preservation (Property 2) — Validates: Requirement 3.5.

    Every EXISTING secret-bearing task in ``unsealer_bootstrap.yml`` that carries
    a Shamir key / root token / seal token — the ``operator init``, the
    ``operator unseal`` loop, the ``policy write``, the ``token create`` mint, the
    ``token revoke``, and the in-memory capture/scrub ``set_fact`` tasks — MUST
    set ``no_log`` (literal ``true`` or ``"{{ openbao_no_log }}"``). This baseline
    secret discipline holds on the UNFIXED code; the fix adds a new no_log
    re-unseal task but must not weaken any of these existing ones.
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)

    classifiers = (
        ("operator init", _is_operator_init),
        ("operator unseal", _is_operator_unseal),
        ("policy write", _is_policy_write),
        ("token create", _is_token_create),
        ("token revoke", _is_token_revoke),
        ("secret capture/scrub set_fact", _is_secret_capture_set_fact),
    )

    seen: dict[str, int] = {label: 0 for label, _ in classifiers}
    offenders: list[str] = []
    for task in tasks:
        for label, is_kind in classifiers:
            if is_kind(task):
                seen[label] += 1
                if not _no_log_is_set(task):
                    offenders.append(
                        f"{task.get('name', '<unnamed>')!r} ({label}) is missing no_log"
                    )

    # Sanity: each secret-bearing category must actually be present in the
    # current file (otherwise a rename could make this test vacuously pass).
    missing_kinds = [label for label, count in seen.items() if count == 0]
    assert not missing_kinds, (
        "PRESERVATION sanity: expected secret-bearing task kind(s) not found in "
        f"unsealer_bootstrap.yml: {missing_kinds}. The baseline structure changed "
        "unexpectedly — re-observe before asserting no_log."
    )

    assert not offenders, (
        "PRESERVATION: every existing Shamir-key / root-token / seal-token "
        "task in unsealer_bootstrap.yml must set no_log (literal true or "
        '"{{ openbao_no_log }}") — the fix must not weaken this. Offenders:\n  '
        + "\n  ".join(offenders)
    )
