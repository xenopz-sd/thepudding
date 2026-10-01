"""Offline swap-gate-style drift guard for the SVC-07 Phase-4 health gate (Task 6.4).

Feature: svc07-installer-simplification.

Spec: .kiro/specs/svc07-installer-simplification/ (design.md "Role 4:
svc07_handoff" + "svc07-health.yml"; Requirement 2, Properties 4 & 6). This is
the health gate's analogue of ``test_swap_gate.py``'s composition-root drift
guard: it pins the SELECTION of the health check — a child ``ansible-playbook``
configurator run against the generated inventory — and the HARD CONSTRAINT that
the gate never reaches the guest via a ``delegate_to`` idiom.

WHAT IS ASSERTED (offline, no live apply, no infra)
---------------------------------------------------
* Health-check child-run selection (Req 2.1, P6): the ``svc07_handoff`` role
  (``ansible/roles/svc07_handoff/tasks/main.yml``) invokes the child health run
  ``ansible-playbook -i {{ svc07_generated_inventory }} ... svc07-health.yml`` —
  the command task's argv references BOTH ``svc07-health.yml`` and the generated
  inventory (``svc07_generated_inventory``).

* No ``delegate_to`` health task anywhere (Req 2.2, P4): NEITHER the handoff role,
  NOR ``ansible/playbooks/svc07-health.yml``, NOR
  ``ansible/playbooks/tasks/svc07-health-debug-probes.yml`` contains a task that
  carries a ``delegate_to:`` KEY combined with a ``docker inspect`` /
  ``docker exec ... bao status`` health command. ``delegate_to`` appearing only
  in comment/prose text is fine — we parse the YAML task tree and assert there is
  no ``delegate_to`` KEY on any task, so a mention in a comment never trips it.

* ``svc07-health.yml`` runs ``hosts: openbao`` and its functional health tasks
  (Stage 1 ``docker inspect``, Stage 2 ``docker exec ... bao status``) run
  NATIVELY on the guest — no ``delegate_to`` on the play or on those tasks
  (Req 2.2, 2.3).

HOW IT STAYS OFFLINE
--------------------
Pure PyYAML + text parse of the committed role/playbook files (the same idiom as
``test_unsealer_delegation.py``). No live Proxmox, no ``ansible-playbook``
execution required for the primary drift-guard assertions.

An OPTIONAL harness-run variant (a hermetic child-stub check) is intentionally
NOT added here: exercising the real ``svc07_handoff`` child-run invocation
requires a populated generated inventory + a reachable guest, which is
``requires-infra`` — outside this offline task's scope. The drift-guard
text/structure assertions are the primary and sufficient mechanism for Task 6.4;
if ``ansible-playbook`` is absent the tests below do not depend on it and still
run (they never shell out).
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_health_child_run.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HANDOFF_ROLE = _REPO_ROOT / "ansible" / "roles" / "svc07_handoff" / "tasks" / "main.yml"
_HEALTH_PLAY = _REPO_ROOT / "ansible" / "playbooks" / "svc07-health.yml"
_HEALTH_DEBUG_PROBES = (
    _REPO_ROOT / "ansible" / "playbooks" / "tasks" / "svc07-health-debug-probes.yml"
)


# --------------------------------------------------------------------------- #
# Helpers — mirror test_unsealer_delegation.py: flatten the nested
# block/rescue/always task tree into a flat list of task dicts (document order),
# and pull a command task's argv into a flat searchable string.
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
    """Return the list of play dicts from an Ansible playbook document."""
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


def _load_role_tasks(path: Path):
    """Return the flat task list of a role ``tasks/main.yml`` (a bare task list).

    A role tasks file is a single YAML document whose top-level node is a LIST of
    tasks (no play wrapper). Flatten block/rescue/always in document order.
    """
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
        elif isinstance(doc, dict):
            tasks.extend(_iter_tasks([doc]))
    return tasks


def _load_task_file(path: Path):
    """Return the flat task list of a plain tasks include file (a bare list)."""
    return _load_role_tasks(path)


def _task_argv_text(task: dict) -> str:
    """Return a flat string of a command/shell task's argv (+ stdin), else ''.

    Covers ``argv`` as a YAML list and as a folded-scalar Jinja expression, a
    plain string ``command:``/``shell:`` free-form, and any ``stdin`` — so a
    ``docker exec ... bao status`` expressed any of those ways is visible.
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
            cmd = spec.get("cmd")
            if isinstance(cmd, str):
                parts.append(cmd)
            if isinstance(spec.get("stdin"), str):
                parts.append(spec["stdin"])
    return " ".join(parts)


def _is_docker_health_command(argv: str) -> bool:
    """True iff argv is a docker-inspect or docker-exec-bao-status health probe.

    Matches the two guest health commands the HARD CONSTRAINT is about:
      * ``docker inspect <container>``  (Stage 1 container-running probe), and
      * ``docker exec <container> bao status ...``  (Stage 2 seal/init probe).
    """
    has_docker_inspect = "docker" in argv and "inspect" in argv
    has_docker_exec_bao_status = (
        "docker" in argv and "exec" in argv and "bao" in argv and "status" in argv
    )
    return has_docker_inspect or has_docker_exec_bao_status


# =============================================================================
# Property 6 — Health-check child-run selection (Validates: Requirements 2.1)
# =============================================================================
def test_handoff_invokes_health_child_run_against_generated_inventory():
    """P6 (Validates: Requirement 2.1).

    The ``svc07_handoff`` role must invoke the Phase-4 health gate as a CHILD
    ``ansible-playbook`` configurator run whose argv references BOTH the health
    play ``svc07-health.yml`` and the generated inventory
    (``svc07_generated_inventory``) via ``-i``.
    """
    tasks = _load_role_tasks(_HANDOFF_ROLE)

    child_run = None
    for task in tasks:
        argv = _task_argv_text(task)
        if (
            "ansible-playbook" in argv
            and "svc07-health.yml" in argv
            and "svc07_generated_inventory" in argv
        ):
            child_run = argv
            break

    assert child_run is not None, (
        "svc07_handoff/tasks/main.yml must invoke the health gate as a child "
        "`ansible-playbook -i {{ svc07_generated_inventory }} ... "
        "svc07-health.yml` run — no such command task found."
    )
    # The generated inventory must be supplied via -i (the child-run inventory).
    assert "-i" in child_run, (
        "The health child run must pass the generated inventory via `-i "
        "{{ svc07_generated_inventory }}`."
    )


def test_handoff_bridges_health_knobs_the_child_play_reads_but_cannot_default():
    """Regression (Validates: Requirements 2.1, 2.4) — the child-play var-scope bug.

    svc07-health.yml is a STANDALONE child play; it does NOT load
    svc07_handoff's role defaults. The child play's Stage-1/Stage-2 argv
    references svc07_openbao_primary_container_name and svc07_openbao_bao_bin
    with NO ``| default(...)`` fallback, so unless the handoff role bridges them
    across the child-process boundary via ``-e`` they render UNDEFINED and the
    child play throws at argv finalization:
        "'svc07_openbao_primary_container_name' is undefined"
    (surfaced by the live Task-15 from-scratch acceptance). This guard pins that
    the handoff child-run invocation passes EVERY health knob the child play
    reads without an internal default — so the offline suite catches the scope
    regression that --syntax-check cannot.
    """
    tasks = _load_role_tasks(_HANDOFF_ROLE)

    child_run = ""
    for task in tasks:
        argv = _task_argv_text(task)
        if "ansible-playbook" in argv and "svc07-health.yml" in argv:
            child_run = argv
            break
    assert child_run, (
        "svc07_handoff/tasks/main.yml must invoke the svc07-health.yml child run."
    )

    # The knobs the child play reads WITHOUT an internal `default()` fallback —
    # these MUST be bridged via -e or the child play throws at render time.
    required_passthrough = (
        "svc07_openbao_primary_container_name",
        "svc07_openbao_bao_bin",
        "svc07_openbao_primary_tls_skip_verify",
    )
    missing = [name for name in required_passthrough if name not in child_run]
    assert not missing, (
        "svc07_handoff must bridge these health-gate knob(s) into the "
        "svc07-health.yml child run via `-e` (the child play does not load this "
        "role's defaults and they carry no `| default()`, so they render "
        f"UNDEFINED otherwise): {missing}. This is the live Task-15 regression."
    )


# =============================================================================
# Property 4 — No delegate_to health task anywhere (Validates: Requirements 2.2)
# =============================================================================
def _delegate_to_health_offenders(tasks, source_label: str) -> list[str]:
    """Return labels for tasks carrying a delegate_to KEY on a docker health cmd."""
    offenders: list[str] = []
    for task in tasks:
        if "delegate_to" not in task:
            continue
        argv = _task_argv_text(task)
        if argv and _is_docker_health_command(argv):
            offenders.append(
                f"{source_label}: {task.get('name', '<unnamed>')!r} "
                f"(delegate_to: {task['delegate_to']!r})"
            )
    return offenders


def test_no_delegate_to_docker_health_task_in_handoff_role():
    """P4 (Validates: Requirement 2.2).

    The ``svc07_handoff`` role must NOT reach the guest via a ``delegate_to``
    ``docker inspect`` / ``docker exec ... bao status`` health task — the gate is
    the child play only.
    """
    tasks = _load_role_tasks(_HANDOFF_ROLE)
    offenders = _delegate_to_health_offenders(tasks, "svc07_handoff")
    assert not offenders, (
        "svc07_handoff/tasks/main.yml carries a delegate_to-based docker health "
        "task (forbidden — Req 2.2):\n  " + "\n  ".join(offenders)
    )


def test_no_delegate_to_docker_health_task_in_health_play():
    """P4 (Validates: Requirements 2.2, 2.3).

    ``svc07-health.yml`` must run its functional health tasks NATIVELY — no
    ``delegate_to`` docker-inspect / docker-exec-bao-status task, and no
    play-level ``delegate_to`` on the health play.
    """
    plays = _load_plays(_HEALTH_PLAY)
    assert plays, "svc07-health.yml has no plays."

    for play in plays:
        assert "delegate_to" not in play, (
            "svc07-health.yml must not carry a play-level delegate_to — the health "
            "gate runs natively on `hosts: openbao`."
        )
        offenders = _delegate_to_health_offenders(
            _iter_tasks(play.get("tasks")), "svc07-health.yml"
        )
        assert not offenders, (
            "svc07-health.yml carries a delegate_to-based docker health task "
            "(forbidden — Req 2.2):\n  " + "\n  ".join(offenders)
        )


def test_no_delegate_to_docker_health_task_in_health_debug_probes():
    """P4 (Validates: Requirement 2.2).

    The consolidated Phase-4 debug include
    (``tasks/svc07-health-debug-probes.yml``) must also carry no ``delegate_to``
    docker-inspect / docker-exec-bao-status task — the native-on-guest model must
    hold for the debug probes too.
    """
    tasks = _load_task_file(_HEALTH_DEBUG_PROBES)
    offenders = _delegate_to_health_offenders(tasks, "svc07-health-debug-probes")
    assert not offenders, (
        "tasks/svc07-health-debug-probes.yml carries a delegate_to-based docker "
        "health task (forbidden — Req 2.2):\n  " + "\n  ".join(offenders)
    )


def test_no_delegate_to_key_on_any_task_across_health_gate_files():
    """P4 (Validates: Requirement 2.2).

    The strongest form of the HARD CONSTRAINT: across all three health-gate files
    (handoff role, health play, health debug probes) there is NO ``delegate_to``
    KEY on ANY task — the gate never reaches the guest via delegation, only via
    the native child run. (``delegate_to`` in comment text is fine; the YAML
    parse ignores comments, so this asserts against real task keys only.)
    """
    sources = {
        "svc07_handoff": _load_role_tasks(_HANDOFF_ROLE),
        "svc07-health-debug-probes": _load_task_file(_HEALTH_DEBUG_PROBES),
    }
    for play in _load_plays(_HEALTH_PLAY):
        sources.setdefault("svc07-health.yml", [])
        sources["svc07-health.yml"].extend(_iter_tasks(play.get("tasks")))

    offenders: list[str] = []
    for label, tasks in sources.items():
        for task in tasks:
            if "delegate_to" in task:
                offenders.append(f"{label}: {task.get('name', '<unnamed>')!r}")

    assert not offenders, (
        "A delegate_to KEY appears on a task in the health-gate files — the "
        "health gate must run natively via the child play, never via delegate_to "
        "(Req 2.2):\n  " + "\n  ".join(offenders)
    )


# =============================================================================
# svc07-health.yml runs `hosts: openbao` with native functional health tasks
# (Validates: Requirements 2.2, 2.3)
# =============================================================================
def test_health_play_targets_hosts_openbao():
    """Validates: Requirement 2.2.

    ``svc07-health.yml`` is a single play targeting ``hosts: openbao`` (the guest
    group in the generated inventory), so the gate runs natively on the guest.
    """
    plays = _load_plays(_HEALTH_PLAY)
    assert len(plays) == 1, (
        f"svc07-health.yml must be a single health play; found {len(plays)}."
    )
    assert plays[0].get("hosts") == "openbao", (
        "svc07-health.yml must run `hosts: openbao` so the health gate executes "
        f"natively on the guest; found hosts={plays[0].get('hosts')!r}."
    )


def test_health_play_has_native_stage1_and_stage2_health_commands():
    """Validates: Requirements 2.3.

    The two functional health commands — Stage 1 ``docker inspect`` and Stage 2
    ``docker exec ... bao status`` — must be present as native (non-delegated)
    command tasks in ``svc07-health.yml``.
    """
    plays = _load_plays(_HEALTH_PLAY)
    tasks = list(_iter_tasks(plays[0].get("tasks")))

    stage1_native = False
    stage2_native = False
    for task in tasks:
        if "delegate_to" in task:
            continue  # a delegated task cannot be the native gate
        argv = _task_argv_text(task)
        if not argv:
            continue
        if "docker" in argv and "inspect" in argv:
            stage1_native = True
        if "docker" in argv and "exec" in argv and "bao" in argv and "status" in argv:
            stage2_native = True

    assert stage1_native, (
        "svc07-health.yml must run a NATIVE Stage-1 `docker inspect <container>` "
        "health task (no delegate_to)."
    )
    assert stage2_native, (
        "svc07-health.yml must run a NATIVE Stage-2 `docker exec <container> bao "
        "status ...` health task (no delegate_to)."
    )
