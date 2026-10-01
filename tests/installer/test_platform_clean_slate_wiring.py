"""Offline structural drift-guard for the platform_clean_slate teardown role.

Spec: .kiro/specs/platform-clean-slate/ (requirements.md, design.md, tasks.md).
Implements SPEC TASK 9.2 — the SINGLE consolidated offline wiring drift-guard the
design names. Its assertions ALSO satisfy the ``*``-marked drift-guard sub-tasks
3.1, 4.1, 5.1, 6.1, 7.1 and 8.1 (the design specifies ONE wiring test file
pinning all of the Correctness Properties P1-P6).

WHAT THIS GUARDS — the ``platform_clean_slate`` role
(``ansible/roles/platform_clean_slate/tasks/main.yml``, with its
``defaults/main.yml``) owns the destroy-safe REVERSE-order body of the platform
prerequisites tear-DOWN branch. This file pins the fixed-sequence structural
invariants P1-P6 from design.md's Correctness Properties, per that section's
Testing Strategy (structural drift-guards, not generative PBT — a teardown
SEQUENCE has no universal input->output invariant to quantify over):

  * P1 (sub-task 3.1) — the throwaway-guard consult + its rc-assert (both gated
    ``when: agent_live_run``) appear BEFORE the FIRST destructive marker in
    document order. BITES if the guard is moved after any destruction.
  * P3 (sub-task 3.1) — the confirmation ``pause`` is gated
    ``when: (clean_slate_confirm ...) != 'force'`` and a non-affirmative ``fail``
    (abort) precedes the first destructive marker.
  * P6-discovery (sub-task 4.1) — discovery is file-mediated: ``stat`` on a
    catalog entry's ``generated_inventory`` + ``registry_status.py``; NO task in
    the role parses ``.tfstate``, and the first ``stat`` (discovery) precedes the
    first child clean-slate run.
  * P2 (sub-tasks 5.1, 6.1) — the child service clean-slate run
    (``svc-07-bootstrap.yml`` + ``clean_slate=true``) appears BEFORE the
    ``platform-foundation`` ``terraform destroy``. BITES on a
    fabric-before-service arrangement. BOUNDARY (P6): no ``terraform
    destroy``/``state rm`` in the role targets a SERVICE tf root, and no ``pct
    destroy`` task exists in the role.
  * P4 (sub-task 7.1) — the reboot task is gated on ``platform_clean_slate_reboot``,
    which defaults false; and a no-false-clean ``fail`` is reachable when the SDN
    zone survives WITHOUT the opt-in.
  * P5 (sub-task 8.1) — the final three-way gate ``assert`` references all three
    survivor facts: a ``pct list``-derived VMID survivor var, a ``terraform state
    list``-derived survivor var (service roots + platform-foundation), and an
    SDN-absence (``p20``) survivor var.

HOW IT STAYS OFFLINE — pure PyYAML + structure parse of the committed role (and,
for the branch wiring, the play). No ``ansible-playbook`` binary, no live
Proxmox. Always runs; needs no ``requires_infra`` gate. It PARSES YAML only and
never embeds/prints a secret. Mirrors the repo's drift-guard style (see
``test_platform_bootstrap_wiring.py`` + ``test_agent_live_wiring_coverage.py``):
helpers are COPIED locally, no cross-test-tree imports (the conftest-collision
rule), repo root via ``parents[2]``.

Validates: Requirements 5.1, 5.2, 6.1, 6.3 (P1/P3); 2.3 (P6-discovery);
2.1, 2.2, 2.4, 3.3 (P2/P6); 4.1, 4.3, 4.4 (P4); 7.1, 7.2, 7.3 (P5) /
Properties 1, 2, 3, 4, 5, 6.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_platform_clean_slate_wiring.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "platform_clean_slate" / "tasks" / "main.yml"
)
_ROLE_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "platform_clean_slate" / "defaults" / "main.yml"
)

# Keys on a task dict that are directives/containers, NOT the invoked module.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally per the repo drift-guard convention (no cross-tree
# import; the conftest-collision rule). Mirrors test_platform_bootstrap_wiring.py
# + test_agent_live_wiring_coverage.py.
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

    The platform_clean_slate role body is a FLAT task list (the enclosing
    ``when: clean_slate`` block/rescue lives in the orchestrator play), so the
    document order of this list IS the teardown execution order the P1/P2/P3
    ordering assertions rely on.
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


def _all_text(task: dict) -> str:
    """Whole-task text (argv + name + when + fail/assert msg) for marker scans."""
    parts = [str(task.get("name", "")), _when_text(task), _task_argv_text(task)]
    for key in ("ansible.builtin.fail", "fail"):
        spec = task.get(key)
        if isinstance(spec, dict):
            parts.append(str(spec.get("msg", "")))
    return " ".join(parts)


def _set_fact_text(task: dict, fact_name: str) -> str | None:
    """Return the string body assigned to ``fact_name`` by a ``set_fact`` task.

    Returns ``None`` if the task is not a ``set_fact`` that assigns ``fact_name``.
    Used to inspect HOW a survivor fact is COMPUTED (not just that it is
    referenced), so a regression to a "zero state lines" definition is caught.
    """
    spec = task.get("ansible.builtin.set_fact") or task.get("set_fact")
    if not isinstance(spec, dict) or fact_name not in spec:
        return None
    return str(spec[fact_name])


# --------------------------------------------------------------------------- #
# Marker detectors.
# --------------------------------------------------------------------------- #
def _is_guard_consult(task: dict) -> bool:
    """True if the task is the throwaway_guard consult scoped to --service platform."""
    argv = _task_argv_text(task)
    return (
        "throwaway_guard" in argv
        and "--service" in argv
        and "platform" in argv
    )


def _is_guard_rc_assert(task: dict) -> bool:
    """The rc gate: an ``assert`` on the guard's ``*throwaway_decision.rc``."""
    return _is_assert(task) and "throwaway_decision.rc" in _assert_that_text(task)


def _is_child_clean_slate_run(task: dict) -> bool:
    """The child per-service clean-slate run: a bootstrap playbook + clean_slate=true."""
    argv = _task_argv_text(task)
    return "bootstrap_playbook" in argv and "clean_slate=true" in argv


def _is_foundation_destroy(task: dict) -> bool:
    """A ``terraform destroy`` whose chdir is the platform-foundation root."""
    argv = _task_argv_text(task)
    if not ("terraform" in argv and "destroy" in argv):
        return False
    chdir = str((task.get("args") or {}).get("chdir", ""))
    return "platform_clean_slate_foundation_root" in chdir


def _is_reboot_task(task: dict) -> bool:
    """A host reboot: an ``ansible.builtin.reboot`` module, or the ad-hoc reboot
    module driven through a child ``ansible`` command argv."""
    if _is_module(task, "ansible.builtin.reboot", "reboot"):
        return True
    argv = _task_argv_text(task)
    return "ansible.builtin.reboot" in argv


def _is_gateway_teardown_run(task: dict) -> bool:
    """The host L3 gateway teardown: the child ``sdn-gateway.yml`` play run with
    the shared VLAN moved into the DECOMMISSIONED set (served set empty).

    This is the step whose sdn_gateway decommission branch removes the
    ``sdn-gw-<vlan>.cfg`` stanza (the ``auto p<vlan>`` line). It MUST complete —
    including that file removal — BEFORE the reboot, or the reboot clears only
    the running bridge while the surviving file recreates it on the next boot
    (the live Task-15.1 Issue-#3 defect)."""
    argv = _task_argv_text(task)
    # The child play is referenced via the `platform_clean_slate_gateway_playbook`
    # var (not the literal filename), and the decommissioned-set --extra-vars is
    # the distinguishing signal of the teardown direction.
    return (
        "sdn_gateway_decommissioned_vlan_ids" in argv
        and (
            "gateway_playbook" in argv
            or "sdn-gateway.yml" in argv
        )
    )


def _is_destructive_marker(task: dict) -> bool:
    """True if the task is a destructive teardown marker (P1 ordering anchor).

    Destructive markers, per the task spec:
      * a child ``ansible-playbook`` run carrying ``clean_slate=true`` (the
        service clean-slate), OR
      * a ``terraform destroy``, OR
      * a ``pct`` command (host guest listing/mutation on the node), OR
      * an ``ansible.builtin.reboot`` (module or ad-hoc argv).
    """
    if _is_child_clean_slate_run(task):
        return True
    argv = _task_argv_text(task)
    if "terraform" in argv and "destroy" in argv:
        return True
    if "pct" in argv:
        return True
    if _is_reboot_task(task):
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
        f"Expected the platform_clean_slate role tasks at {_ROLE_TASKS} — not "
        "found; the role layout may have moved, repoint this guard."
    )
    tasks = _load_role_tasks(_ROLE_TASKS)
    assert len(tasks) >= 20, (
        "DRIFT / vacuous-parse guard: the platform_clean_slate role must parse to "
        f"a substantial task list (>=20 tasks); parsed only {len(tasks)}. A "
        "near-empty parse would make the ordering/gating assertions pass "
        "vacuously."
    )
    # And it must contain the anchors the property tests rely on.
    assert _first_index(tasks, _is_guard_consult) is not None, (
        "DRIFT: no throwaway-guard consult (--service platform) task found in the "
        "role."
    )
    assert _first_index(tasks, _is_destructive_marker) is not None, (
        "DRIFT: no destructive marker (child clean-slate / terraform destroy / "
        "pct / reboot) found in the role — the ordering anchors are missing."
    )


# =============================================================================
# P1 (sub-task 3.1) — Guard-consult precedes any destruction.
# Validates: Requirements 6.1, 6.3 / Property 1.
# =============================================================================
def test_guard_consult_and_rc_assert_precede_first_destruction():
    """Validates: Requirements 6.1, 6.3 / Property 1.

    The throwaway-guard consult AND its rc-assert (both gated
    ``when: agent_live_run``) must appear BEFORE the first destructive marker in
    the role's document order — so an armed non-throwaway run is REFUSED before
    ANY child clean-slate, ``terraform destroy``, ``pct``, or reboot runs. BITES
    if the guard is moved after any destructive marker.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    consult_idx = _first_index(tasks, _is_guard_consult)
    rc_assert_idx = _first_index(tasks, _is_guard_rc_assert)
    destroy_idx = _first_index(tasks, _is_destructive_marker)

    assert consult_idx is not None, (
        "DRIFT (Property 1): no throwaway-guard consult (argv -> throwaway_guard "
        "--service platform) found in the role."
    )
    assert rc_assert_idx is not None, (
        "DRIFT (Property 1): no rc-assert on the guard decision "
        "(*throwaway_decision.rc) found in the role."
    )
    assert destroy_idx is not None, (
        "DRIFT (Property 1): no destructive marker found — cannot anchor the "
        "guard-before-destruction ordering check."
    )

    # Both guard tasks must be gated on the armed interlock so an ordinary
    # operator run skips them (Req 6.2) while an armed run is gated fail-closed.
    assert "agent_live_run" in _when_text(tasks[consult_idx]), (
        "DRIFT (Property 1): the throwaway-guard consult must be gated "
        f"`when: agent_live_run`; its when is {_when_text(tasks[consult_idx])!r}."
    )
    assert "agent_live_run" in _when_text(tasks[rc_assert_idx]), (
        "DRIFT (Property 1): the guard rc-assert must be gated "
        f"`when: agent_live_run`; its when is {_when_text(tasks[rc_assert_idx])!r}."
    )

    assert consult_idx < destroy_idx, (
        "DRIFT (Property 1 — the drift this guard BITES on): the throwaway-guard "
        f"consult (index={consult_idx}) must precede the FIRST destructive marker "
        f"(index={destroy_idx}). Moving the guard after any child clean-slate / "
        "terraform destroy / pct / reboot fails HERE — a non-throwaway target "
        "must be refused before anything is mutated."
    )
    assert rc_assert_idx < destroy_idx, (
        "DRIFT (Property 1): the guard rc-assert (the fail-closed REFUSED halt, "
        f"index={rc_assert_idx}) must precede the first destructive marker "
        f"(index={destroy_idx})."
    )


# =============================================================================
# P3 (sub-task 3.1) — Confirmation-unless-force.
# Validates: Requirements 5.1, 5.2, 5.3 / Property 3.
# =============================================================================
def test_confirmation_pause_gated_unless_force_and_abort_before_destruction():
    """Validates: Requirements 5.1, 5.2, 5.3 / Property 3.

    The confirmation ``ansible.builtin.pause`` must be gated
    ``when: (clean_slate_confirm ...) != 'force'`` (so ``force`` skips the prompt,
    Req 5.3), and a non-affirmative ``fail`` (the not-confirmed abort, Req 5.2)
    must appear BEFORE the first destructive marker — so a declined/timed-out
    prompt aborts having touched nothing.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    pause_idx = _first_index(tasks, lambda t: _is_module(t, "ansible.builtin.pause", "pause"))
    assert pause_idx is not None, (
        "DRIFT (Property 3): no confirmation `pause` task found in the role."
    )
    pause_when = _when_text(tasks[pause_idx])
    assert "clean_slate_confirm" in pause_when and "!= 'force'" in pause_when, (
        "DRIFT (Property 3): the confirmation pause must be gated "
        "`when: (clean_slate_confirm ...) != 'force'` so a force token skips the "
        f"prompt (Req 5.3); its when is {pause_when!r}."
    )

    # The pause MUST be prompt-only — it MUST NOT set `seconds:`. With `seconds:`
    # set, `ansible.builtin.pause` runs in TIMED mode and never reads the typed
    # line into `user_input`, so a "type 'yes'" prompt can never be confirmed
    # interactively (the operator-reported defect). Option-B fix: prompt-only.
    pause_module = tasks[pause_idx].get("ansible.builtin.pause",
                                        tasks[pause_idx].get("pause")) or {}
    assert isinstance(pause_module, dict), (
        "DRIFT (Property 3): the confirmation pause module args must be a mapping."
    )
    assert "prompt" in pause_module, (
        "DRIFT (Property 3): the confirmation pause must carry a `prompt:` "
        "(prompt-only mode) so the typed confirmation is read."
    )
    assert "seconds" not in pause_module and "minutes" not in pause_module, (
        "DRIFT (Property 3): the confirmation pause MUST NOT set `seconds:`/"
        "`minutes:`. A timed pause discards the typed line — `user_input` stays "
        "empty and interactive confirmation becomes impossible (the bug this "
        f"fix removed). pause args were {pause_module!r}."
    )

    # The not-confirmed abort: a `fail` gated on `not ... confirmed`.
    def _is_not_confirmed_fail(task: dict) -> bool:
        if not _is_module(task, "ansible.builtin.fail", "fail"):
            return False
        return "confirmed" in _when_text(task)

    abort_idx = _first_index(tasks, _is_not_confirmed_fail)
    assert abort_idx is not None, (
        "DRIFT (Property 3): no not-confirmed `fail` abort (a fail gated on the "
        "resolved-confirmed fact) found in the role."
    )

    destroy_idx = _first_index(tasks, _is_destructive_marker)
    assert destroy_idx is not None, (
        "DRIFT (Property 3): no destructive marker found — cannot anchor the "
        "confirmation-before-destruction ordering check."
    )
    assert abort_idx < destroy_idx, (
        "DRIFT (Property 3): the not-confirmed abort `fail` "
        f"(index={abort_idx}) must precede the first destructive marker "
        f"(index={destroy_idx}) — a non-affirmative/timed-out response must "
        "abort destroying nothing (Req 5.2)."
    )


# =============================================================================
# P6-discovery (sub-task 4.1) — file-mediated discovery.
# Validates: Requirements 2.3 / Property 6.
# =============================================================================
def test_discovery_is_file_mediated_no_tfstate_no_host_enumeration():
    """Validates: Requirements 2.3 / Property 6.

    Discovery must be FILE-MEDIATED (integration-boundaries.md §4): it ``stat``s a
    catalog entry's ``generated_inventory`` and runs ``registry_status.py``. NO
    task in the role may parse ``.tfstate`` (nowhere in the role), and the first
    ``stat`` (discovery) must precede the first child clean-slate run — so
    discovery reads a file, it does not enumerate hosts to find services.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # `.tfstate` appears NOWHERE in the role (no raw state parsing).
    tfstate_tasks = [t for t in tasks if ".tfstate" in _all_text(t)]
    assert not tfstate_tasks, (
        "DRIFT (Property 6): the role must NOT parse `.tfstate` anywhere "
        "(integration-boundaries.md §4 — discovery is file-mediated via the "
        f"generated inventory, not raw Terraform state); found {len(tfstate_tasks)} "
        "task(s) referencing .tfstate."
    )

    # Discovery uses `stat` on generated_inventory.
    def _is_discovery_stat(task: dict) -> bool:
        if not _is_module(task, "ansible.builtin.stat", "stat"):
            return False
        spec = task.get("ansible.builtin.stat") or task.get("stat") or {}
        path = str(spec.get("path", "")) if isinstance(spec, dict) else ""
        return "generated_inventory" in path

    stat_idx = _first_index(tasks, _is_discovery_stat)
    assert stat_idx is not None, (
        "DRIFT (Property 6): no discovery `stat` on a catalog entry's "
        "`generated_inventory` found — file-mediated presence detection is the "
        "authoritative discovery signal."
    )

    # registry_status.py is consulted (context signal).
    assert any("registry_status" in _task_argv_text(t) for t in tasks), (
        "DRIFT (Property 6): the role must consult `registry_status.py` for "
        "served-VLAN context during discovery."
    )

    # The first stat (discovery) precedes the first child clean-slate run.
    child_idx = _first_index(tasks, _is_child_clean_slate_run)
    assert child_idx is not None, (
        "DRIFT (Property 6): no child service clean-slate run found — cannot "
        "anchor the discovery-before-destruction ordering check."
    )
    assert stat_idx < child_idx, (
        f"DRIFT (Property 6): the discovery `stat` (index={stat_idx}) must "
        f"precede the first child clean-slate run (index={child_idx}) — services "
        "are discovered from files BEFORE any service destruction."
    )


# =============================================================================
# P2 (sub-tasks 5.1, 6.1) — Service-clean-slate BEFORE fabric-teardown.
# Validates: Requirements 2.4, 3.3 / Property 2.
# =============================================================================
def test_service_clean_slate_precedes_fabric_terraform_destroy():
    """Validates: Requirements 2.4, 3.3 / Property 2.

    The child service clean-slate run (argv -> service bootstrap playbook +
    ``clean_slate=true``) must appear BEFORE the ``platform-foundation``
    ``terraform destroy`` (chdir -> the foundation root). This is the destroy-safe
    reverse-order invariant: service guests come off the fabric FIRST. BITES on a
    fabric-before-service arrangement (swap the two -> this fails).
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    service_idx = _first_index(tasks, _is_child_clean_slate_run)
    fabric_idx = _first_index(tasks, _is_foundation_destroy)

    assert service_idx is not None, (
        "DRIFT (Property 2): no child service clean-slate run "
        "(bootstrap playbook + clean_slate=true) found."
    )
    assert fabric_idx is not None, (
        "DRIFT (Property 2): no platform-foundation `terraform destroy` "
        "(chdir -> platform_clean_slate_foundation_root) found."
    )
    assert service_idx < fabric_idx, (
        "DRIFT (Property 2 — the drift this guard BITES on): the child service "
        f"clean-slate run (index={service_idx}) must precede the "
        f"platform-foundation `terraform destroy` (index={fabric_idx}). "
        "Destroying the shared VNet/bridge while a service guest is still "
        "attached is unsafe; a fabric-before-service arrangement fails HERE."
    )


# =============================================================================
# P2 (sub-tasks 5.1, 6.1) — the child run threads the Proxmox host address.
# Validates: Requirements 2.1, 2.2 / Property 2.  (Live Task 15.1 defect guard.)
# =============================================================================
def test_child_clean_slate_run_threads_proxmox_host_address():
    """Validates: Requirements 2.1, 2.2 / Property 2 (live Task 15.1 defect guard).

    Each discovered service's OWN clean-slate runs ``pct`` on the Proxmox node
    over SSH and fail-closes if it does not receive the Proxmox host address
    under the name IT expects (SVC-07: ``svc07_proxmox_host``). The platform
    teardown holds that address as ``platform_clean_slate_proxmox_host``; the
    child-run task MUST thread it into the child argv under the catalog row's
    ``proxmox_host_var`` name, and a fail-closed assert on
    ``platform_clean_slate_proxmox_host`` MUST precede the child run.

    BITES on a regression of the live Task 15.1 defect where the child was
    invoked with only ``clean_slate=true`` / ``clean_slate_confirm=force`` /
    ``cluster_env_file`` and never received the host address — so the child
    fail-closed and the teardown could never reach the fabric step.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    child_idx = _first_index(tasks, _is_child_clean_slate_run)
    assert child_idx is not None, (
        "DRIFT (Property 2): no child service clean-slate run "
        "(bootstrap playbook + clean_slate=true) found."
    )

    # The child argv must thread the Proxmox host address under the per-row var
    # name: it references BOTH item.proxmox_host_var (the child's expected -e
    # name) and platform_clean_slate_proxmox_host (the platform's resolved value).
    child_argv = _task_argv_text(tasks[child_idx])
    assert "proxmox_host_var" in child_argv, (
        "DRIFT (Property 2 / live Task 15.1 defect): the child clean-slate run "
        "argv must thread the Proxmox host address under the catalog row's "
        "`item.proxmox_host_var` name; the argv does not reference "
        f"`proxmox_host_var`. argv={child_argv!r}"
    )
    assert "platform_clean_slate_proxmox_host" in child_argv, (
        "DRIFT (Property 2 / live Task 15.1 defect): the child clean-slate run "
        "argv must pass the platform-resolved Proxmox host address "
        "(`platform_clean_slate_proxmox_host`) into the child; the argv does not "
        f"reference it. argv={child_argv!r}"
    )

    # A fail-closed assert on the host address must precede the child run, so a
    # reset requested without it halts having destroyed nothing.
    def _is_host_addr_assert(task: dict) -> bool:
        return (
            _is_assert(task)
            and "platform_clean_slate_proxmox_host" in _assert_that_text(task)
        )

    host_assert_idx = _first_index(tasks, _is_host_addr_assert)
    assert host_assert_idx is not None, (
        "DRIFT (Property 2 / live Task 15.1 defect): no fail-closed assert on "
        "`platform_clean_slate_proxmox_host` found; the child clean-slate run "
        "needs the host address and must be guarded by an assert."
    )
    assert host_assert_idx < child_idx, (
        "DRIFT (Property 2 / live Task 15.1 defect): the fail-closed host-address "
        f"assert (index={host_assert_idx}) must precede the child clean-slate run "
        f"(index={child_idx}), so a reset requested without "
        "`platform_clean_slate_proxmox_host` halts before any child runs."
    )

    # And the catalog row for SVC-07 must actually name svc07_proxmox_host, the
    # -e var its child clean-slate expects, so the threaded value lands correctly.
    defaults = yaml.safe_load(_ROLE_DEFAULTS.read_text(encoding="utf-8")) or {}
    services = defaults.get("platform_clean_slate_services") or []
    svc07 = next((r for r in services if isinstance(r, dict) and r.get("service") == "svc07"), None)
    assert svc07 is not None, (
        "DRIFT: no svc07 row in platform_clean_slate_services (defaults/main.yml)."
    )
    assert svc07.get("proxmox_host_var") == "svc07_proxmox_host", (
        "DRIFT (live Task 15.1 defect): the SVC-07 catalog row must set "
        "`proxmox_host_var: svc07_proxmox_host` — the exact -e var name the "
        "SVC-07 child clean-slate fail-closes on; found "
        f"{svc07.get('proxmox_host_var')!r}."
    )


# =============================================================================
# P6 (sub-tasks 5.1, 6.1) — Boundary invariant.
# Validates: Requirements 2.1, 2.2 / Property 6.
# =============================================================================
def test_no_service_terraform_root_destroy_and_no_pct_destroy():
    """Validates: Requirements 2.1, 2.2 / Property 6.

    The boundary invariant: the platform role NEVER reaches across a per-service
    state boundary. Asserts:

      * NO ``terraform destroy`` / ``terraform state rm`` task in the role has a
        chdir pointing at a SERVICE tf root (``infra/projects/...``) — the ONLY
        ``terraform destroy`` chdir is the platform-foundation root; and
      * NO ``pct destroy`` task exists in the role — service guest destruction is
        ONLY ever the child service-bootstrap clean-slate run.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # No terraform destroy / state rm against a service TF root.
    for task in tasks:
        argv = _task_argv_text(task)
        is_destroy = "terraform" in argv and "destroy" in argv
        is_state_rm = "terraform" in argv and "state" in argv and "rm" in argv
        if not (is_destroy or is_state_rm):
            continue
        chdir = str((task.get("args") or {}).get("chdir", ""))
        # A service TF root is infra/projects/... — the role's foundation root is
        # infra/platform-foundation, referenced via the foundation-root var.
        assert "infra/projects" not in chdir, (
            "DRIFT (Property 6): a `terraform destroy`/`state rm` task targets a "
            f"SERVICE tf root (chdir={chdir!r}). The platform role must NEVER "
            "destroy a service's per-service state directly — only "
            "infra/platform-foundation is a destroy chdir; service destruction is "
            "the child clean-slate run."
        )

    # No `pct destroy` anywhere in the role (only `pct list` to CONFIRM absence).
    pct_destroy = [t for t in tasks if "pct destroy" in _task_argv_text(t)]
    assert not pct_destroy, (
        "DRIFT (Property 6): the role must NOT run `pct destroy` on a service "
        "guest (service guest destruction is only ever the child "
        f"service-bootstrap clean-slate run); found {len(pct_destroy)} such "
        "task(s). Only `pct list` (to CONFIRM absence of known VMIDs) is allowed."
    )


# =============================================================================
# P4 (sub-task 7.1) — Reboot opt-in default OFF + no-false-clean.
# Validates: Requirements 4.1, 4.3, 4.4 / Property 4.
# =============================================================================
def test_reboot_task_gated_on_optin_which_defaults_off():
    """Validates: Requirements 4.1, 4.4 / Property 4.

    The reboot task must be gated on ``platform_clean_slate_reboot``, and that
    opt-in must default false — so a stuck-zone recovery reboot never fires
    silently and never against a host the operator did not explicitly opt in.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    reboot_idx = _first_index(tasks, _is_reboot_task)
    assert reboot_idx is not None, (
        "DRIFT (Property 4): no reboot task (ansible.builtin.reboot module or the "
        "ad-hoc reboot module) found in the role."
    )
    reboot_when = _when_text(tasks[reboot_idx])
    assert "platform_clean_slate_reboot" in reboot_when, (
        "DRIFT (Property 4): the reboot task must be gated on the "
        "`platform_clean_slate_reboot` opt-in; its when is "
        f"{reboot_when!r}."
    )

    # Default OFF — in the role defaults (or, failing that, the play vars).
    default = _reboot_default()
    assert default is False, (
        "DRIFT (Property 4): `platform_clean_slate_reboot` must default to false "
        "(opt-in, default OFF, never a committed default true); resolved default "
        f"is {default!r}."
    )


def test_no_false_clean_fail_when_sdn_survives_without_optin():
    """Validates: Requirements 4.3 / Property 4.

    There must be a ``fail`` (the no-false-clean halt) reachable when the SDN
    zone/bridge SURVIVES the teardown WITHOUT the reboot opt-in — i.e. a fail
    gated on the SDN-present fact AND ``not platform_clean_slate_reboot``. A
    surviving zone must never be reported as a clean baseline.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    def _is_no_false_clean_fail(task: dict) -> bool:
        if not _is_module(task, "ansible.builtin.fail", "fail"):
            return False
        w = _when_text(task)
        return (
            "platform_cs_sdn_present" in w
            and "platform_clean_slate_reboot" in w
            and "not" in w
        )

    matches = [t for t in tasks if _is_no_false_clean_fail(t)]
    assert matches, (
        "DRIFT (Property 4): no no-false-clean `fail` found — there must be a "
        "fail gated on the SDN-present fact (`platform_cs_sdn_present`) AND "
        "`not platform_clean_slate_reboot`, so a surviving SDN zone/bridge "
        "without the reboot opt-in HALTS naming the survivor rather than "
        "reporting a clean baseline (Req 4.3)."
    )


def test_gateway_teardown_precedes_reboot():
    """Validates: Requirements 3.1, 4.1 / Property 4 (live Task-15.1 Issue-#3).

    The host L3 gateway teardown (the child ``sdn-gateway.yml`` run with the
    shared VLAN in the DECOMMISSIONED set) removes the ``sdn-gw-<vlan>.cfg``
    stanza — the ``auto p<vlan>`` line ifupdown replays on every boot. It MUST
    run BEFORE the reboot step: the reboot is only the CLEARANCE mechanism
    (ADR-0010), so if the file removal ran after (or not at all) the reboot would
    clear the running bridge while the surviving file recreated it on the next
    boot. This drift-guard pins the gateway teardown ahead of the reboot in the
    role's document (execution) order, so clearing ``p20`` reliably needs BOTH the
    file removal AND the reboot — never the reboot alone.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    gateway_idx = _first_index(tasks, _is_gateway_teardown_run)
    reboot_idx = _first_index(tasks, _is_reboot_task)

    assert gateway_idx is not None, (
        "DRIFT (Issue #3): no host L3 gateway teardown run found — expected the "
        "child `sdn-gateway.yml` play invoked with "
        "`sdn_gateway_decommissioned_vlan_ids` (the shared VLAN moved into the "
        "decommissioned set) so its teardown branch removes the "
        "`sdn-gw-<vlan>.cfg` `auto p<vlan>` stanza."
    )
    assert reboot_idx is not None, (
        "DRIFT (Property 4): no reboot task found (ansible.builtin.reboot module "
        "or ad-hoc argv)."
    )
    assert gateway_idx < reboot_idx, (
        "DRIFT (Issue #3 — the drift this guard BITES on): the host L3 gateway "
        f"teardown (index={gateway_idx}) must precede the reboot "
        f"(index={reboot_idx}). The gateway teardown removes the "
        "`sdn-gw-<vlan>.cfg` `auto p<vlan>` stanza; if the reboot runs first, it "
        "clears only the running bridge and the surviving file recreates it on "
        "the next boot — the live Task-15.1 Issue-#3 defect."
    )


# =============================================================================
# P5 (sub-task 8.1) — three-way verification gate.
# Validates: Requirements 7.1, 7.2, 7.3 / Property 5.
# =============================================================================
def test_verification_gate_references_all_three_survivor_signals():
    """Validates: Requirements 7.1, 7.2, 7.3 / Property 5.

    The final gate ``assert`` must reference ALL THREE survivor facts in its
    ``that:`` clause: the ``pct list``-derived VMID survivor var, the
    ``terraform state list``-derived survivor var (covering service roots +
    platform-foundation), and the SDN-absence (``p20``) survivor var.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # The three survivor facts the gate must reference (from design "Data Models"
    # / the role implementation).
    vmid_fact = "platform_cs_surviving_vmids"
    state_fact = "platform_cs_surviving_state_addrs"
    sdn_fact = "platform_cs_surviving_sdn"

    gate_asserts = [
        t for t in tasks
        if _is_assert(t)
        and vmid_fact in _assert_that_text(t)
        and state_fact in _assert_that_text(t)
        and sdn_fact in _assert_that_text(t)
    ]
    assert gate_asserts, (
        "DRIFT (Property 5): no three-way verification-gate `assert` found whose "
        f"`that:` references ALL THREE survivor facts ({vmid_fact}, {state_fact}, "
        f"{sdn_fact}). The gate must prove zero surviving guests (pct list), zero "
        "state addresses (service roots + platform-foundation), AND no surviving "
        "SDN object before reporting a clean baseline."
    )


# =============================================================================
# Fix 1 — the survivor definition tolerates inert module-internal bookkeeping
# anchors (terraform_data.*) and counts ONLY real-resource state lines.
# Guards that this fix cannot regress to "zero state lines".
# Validates: Requirements 2.4 (halt-before-fabric) + 7.2 (gate) / Properties 2, 5.
# =============================================================================
def test_catalog_rows_carry_state_ignore_patterns_for_inert_anchors():
    """Validates: Requirements 2.4, 7.2 / Properties 2, 5 (Fix 1 catalog field).

    Each catalog service row must carry a ``state_ignore_patterns`` list naming
    the inert module-internal bookkeeping anchors to TOLERATE (not count as
    survivors). SVC-07's row must list ``terraform_data.input_guards`` — the
    proxmox-compute module's triggerless precondition anchor that has no real
    Proxmox object and reappears on the next apply, so the child clean-slate
    (which removes the real container resources) legitimately leaves it behind.
    And the platform-foundation ignore-pattern default must list
    ``terraform_data.registry_watch`` — the foundation root's own inert
    replace-trigger sentinel that survives the SDN `terraform destroy`.
    """
    defaults = yaml.safe_load(_ROLE_DEFAULTS.read_text(encoding="utf-8")) or {}

    services = defaults.get("platform_clean_slate_services") or []
    svc07 = next((r for r in services if isinstance(r, dict) and r.get("service") == "svc07"), None)
    assert svc07 is not None, (
        "DRIFT: no svc07 row in platform_clean_slate_services (defaults/main.yml)."
    )
    ignore = svc07.get("state_ignore_patterns")
    assert isinstance(ignore, list) and ignore, (
        "DRIFT (Fix 1): the SVC-07 catalog row must carry a non-empty "
        "`state_ignore_patterns` list so the halt-before-fabric + gate checks "
        "judge emptiness by REAL resources, not zero state lines; found "
        f"{ignore!r}."
    )
    assert any("terraform_data.input_guards" in str(p) for p in ignore), (
        "DRIFT (Fix 1): the SVC-07 `state_ignore_patterns` must tolerate the "
        "inert `terraform_data.input_guards` proxmox-compute anchors (the live "
        f"Task 15.1 survivors); found {ignore!r}."
    )

    foundation_ignore = defaults.get("platform_clean_slate_foundation_state_ignore_patterns")
    assert isinstance(foundation_ignore, list) and foundation_ignore, (
        "DRIFT (Fix 1): `platform_clean_slate_foundation_state_ignore_patterns` "
        "must be a non-empty list so the gate's platform-foundation state check "
        "tolerates the foundation's own inert anchor; found "
        f"{foundation_ignore!r}."
    )
    assert any("terraform_data.registry_watch" in str(p) for p in foundation_ignore), (
        "DRIFT (Fix 1): the platform-foundation ignore-pattern default must "
        "tolerate the inert `terraform_data.registry_watch` sentinel that "
        f"survives the SDN destroy; found {foundation_ignore!r}."
    )


def test_survivor_state_computation_filters_inert_anchors_not_zero_lines():
    """Validates: Requirements 2.4, 7.2 / Properties 2, 5 (Fix 1 no-regression).

    BITES if the survivor-state computation regresses to a "any non-blank state
    line is a survivor" (zero-state-lines) definition. Both the halt-before-
    fabric survivor set AND the gate's state-survivor set MUST be computed with a
    ``reject('regex', ...)`` over the ignore-pattern list — i.e. they filter out
    inert anchors and count only REAL resource addresses. The presence of
    ``reject`` + the ignore-pattern var in each computing ``set_fact`` body is the
    structural signal that the refined (not zero-line) definition is in force.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # (a) Halt-before-fabric per-service survivor computation.
    prefabric_bodies = [
        _set_fact_text(t, "platform_cs_prefabric_survivors")
        for t in tasks
    ]
    prefabric_bodies = [b for b in prefabric_bodies if b is not None]
    assert prefabric_bodies, (
        "DRIFT (Fix 1): no set_fact computing `platform_cs_prefabric_survivors` "
        "found — the halt-before-fabric survivor computation is missing."
    )
    for body in prefabric_bodies:
        assert "reject(" in body and "state_ignore_patterns" in body, (
            "DRIFT (Fix 1 — regression to zero-state-lines): the halt-before-"
            "fabric `surviving_state` must be computed by rejecting the row's "
            "`state_ignore_patterns` (tolerating inert terraform_data anchors), "
            "NOT by treating any non-blank state line as a survivor. Its body "
            f"lacks a reject over state_ignore_patterns: {body!r}"
        )

    # (b) Gate service-root + foundation survivor-state computations.
    service_state_bodies = [
        b for t in tasks
        if (b := _set_fact_text(t, "platform_cs_surviving_service_state")) is not None
    ]
    assert service_state_bodies, (
        "DRIFT (Fix 1): no set_fact computing `platform_cs_surviving_service_state` "
        "found — the gate's per-service real-resource state computation is missing."
    )
    for body in service_state_bodies:
        assert "reject(" in body and "state_ignore_patterns" in body, (
            "DRIFT (Fix 1 — regression to zero-state-lines): the gate's "
            "per-service `surviving_service_state` must reject the row's "
            f"`state_ignore_patterns`; body lacks it: {body!r}"
        )

    foundation_state_bodies = [
        b for t in tasks
        if (b := _set_fact_text(t, "platform_cs_surviving_foundation_state")) is not None
    ]
    assert foundation_state_bodies, (
        "DRIFT (Fix 1): no set_fact computing "
        "`platform_cs_surviving_foundation_state` found — the gate's "
        "platform-foundation real-resource state computation is missing."
    )
    for body in foundation_state_bodies:
        assert (
            "reject(" in body
            and "platform_clean_slate_foundation_state_ignore_patterns" in body
        ), (
            "DRIFT (Fix 1 — regression to zero-state-lines): the gate's "
            "platform-foundation `surviving_foundation_state` must reject "
            "`platform_clean_slate_foundation_state_ignore_patterns` (tolerating "
            f"the inert registry_watch anchor); body lacks it: {body!r}"
        )


# --------------------------------------------------------------------------- #
# defaults helper — resolve the reboot opt-in default (role defaults first).
# --------------------------------------------------------------------------- #
def _reboot_default():
    """Return the resolved default of ``platform_clean_slate_reboot``.

    Checks the role ``defaults/main.yml`` first; if absent there (the design
    keeps it as a play-level var), falls back to the orchestrator play vars.
    Returns the bool default, or the raw value if it is not a plain bool.
    """
    key = "platform_clean_slate_reboot"

    if _ROLE_DEFAULTS.is_file():
        data = yaml.safe_load(_ROLE_DEFAULTS.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and key in data:
            return data[key]

    play = _REPO_ROOT / "ansible" / "playbooks" / "platform-bootstrap.yml"
    for doc in yaml.safe_load_all(play.read_text(encoding="utf-8")):
        if not isinstance(doc, list):
            continue
        for p in doc:
            if isinstance(p, dict) and isinstance(p.get("vars"), dict) and key in p["vars"]:
                return p["vars"][key]
    return None
