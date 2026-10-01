"""Offline structural guards for the SVC-07 unsealer-bootstrap-delegation refactor.

Feature: svc07-unsealer-bootstrap-delegation — the orchestrator
(``ansible/playbooks/svc-07-bootstrap.yml``) must DELEGATE the unsealer bootstrap
to a single child ``openbao.yml`` run driving the ``openbao_init_unseal`` role,
instead of hand-rolling the unsealer's ``operator init`` / ``operator unseal`` /
``openbao-seal`` ``policy write`` / seal-token ``token create`` / unsealer
``token revoke`` as raw ``docker exec bao ...`` tasks.

These tests are pure-logic (PyYAML + text parse of the committed playbooks) —
they need no infrastructure. The AUTHORITATIVE behavioural acceptance is the
``requires-infra`` from-scratch bring-up in ``test_installer_integration.py``;
these guards pin the STRUCTURE of the refactor so it cannot silently regress:

* Property 3 (Req 2.1/2.2): no hand-rolled unsealer init/unseal/policy/mint/revoke
  raw ``docker exec`` task survives in the orchestrator, and the Task-8.2
  delegation (child ``openbao.yml`` ``--limit openbao-unsealer`` ``--tags
  install,init`` with ``openbao_mint_seal_token=true``) exists.
* Handoff capture (Req 5.3/6.4): the slurp -> set_fact(b64decode|trim) ->
  file:absent sequence for the Handoff_File exists, guarded by a fail-closed
  assert.
* Ordering (Req 4.1): the Task-8.2 delegation + capture precede the Task-8.4
  primary bootstrap, and ``openbao.yml`` retains two ordered plays (unsealer
  first).
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_unsealer_delegation.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
# The Phase-3 unsealer-bootstrap delegation (Task 8.2), the seal-token
# Handoff_File capture, and the primary-bootstrap child run (Task 8.4) all moved
# out of the ~1900-line svc-07-bootstrap.yml when it was thinned to a ~150-line
# orchestrator of five import_role calls (svc07-installer-simplification, Task
# 5.1/11). They now live VERBATIM in the svc07_bootstrap ROLE's tasks/main.yml —
# a bare task LIST (no play wrapper), not the single-play orchestrator. The
# guards below therefore read the ROLE task file; their INTENT is unchanged. The
# openbao.yml two-play checks are UNCHANGED by this rework (that playbook was not
# touched).
_BOOTSTRAP_ROLE_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_bootstrap" / "tasks" / "main.yml"
)
_OPENBAO_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "openbao.yml"


# --------------------------------------------------------------------------- #
# Helpers — flatten the deeply nested block/rescue task tree into a flat list
# of (name, task_dict) pairs, in document order.
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        # A block/rescue/always container is itself a "task" node.
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


def _load_role_tasks(path: Path):
    """Return the flat ordered task list of a role ``tasks/main.yml``.

    A role tasks file is a single YAML document whose top-level node is a LIST of
    tasks (no play wrapper). Flatten block/rescue/always in document order — the
    Phase-3 capture block nests its slurp/set_fact/delete/assert inside a
    ``block:``, so descending it is required to reach those tasks.
    """
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _orchestrator_tasks():
    """Return the Phase-3 bootstrap tasks (now the svc07_bootstrap role's tasks).

    Named ``_orchestrator_tasks`` for continuity with the pre-extraction test;
    it now reads the svc07_bootstrap ROLE task list where the delegation + capture
    + primary-bootstrap tasks moved (svc07-installer-simplification, Task 5.1).
    """
    return _load_role_tasks(_BOOTSTRAP_ROLE_TASKS)


def _task_argv_text(task: dict) -> str:
    """Return a flat string of a command task's argv (+ stdin), else ''.

    Covers both ``argv`` as a YAML list and as a folded-scalar Jinja expression
    (the hand-rolled tasks built argv from a ``{{ [...] }}`` list), plus any
    ``stdin`` — so a ``policy write ... -`` with the HCL in stdin is visible.
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


# --------------------------------------------------------------------------- #
# Property 3 — orchestrator no longer hand-rolls the unsealer bootstrap.
# Validates: Requirements 2.1, 2.2
# --------------------------------------------------------------------------- #
# Each entry: a human label + the ordered tokens that, appearing together in a
# single command task's argv/stdin, identify a hand-rolled unsealer op. We match
# on co-occurrence within one task (not the whole file) so an incidental mention
# in a comment or a child-run flag never trips it.
_FORBIDDEN_RAW_OPS = [
    ("operator init", ["docker", "exec", "operator", "init"]),
    ("operator unseal", ["docker", "exec", "operator", "unseal"]),
    ("openbao-seal policy write", ["docker", "exec", "policy", "write", "openbao-seal"]),
    ("seal token create", ["docker", "exec", "token", "create", "openbao-seal"]),
    ("unsealer token revoke", ["docker", "exec", "token", "revoke"]),
]


def test_orchestrator_has_no_handrolled_unsealer_ops():
    """Property 3 (Validates: Requirements 2.1, 2.2).

    No orchestrator command task may hand-roll the unsealer's
    init/unseal/seal-policy/mint/revoke as a raw ``docker exec bao ...`` — the
    ``openbao_init_unseal`` role owns them now.
    """
    tasks = _orchestrator_tasks()
    offenders: list[str] = []
    for task in tasks:
        argv = _task_argv_text(task)
        if not argv:
            continue
        for label, tokens in _FORBIDDEN_RAW_OPS:
            if all(tok in argv for tok in tokens):
                offenders.append(f"{task.get('name', '<unnamed>')} -> {label}")
    assert not offenders, (
        "The orchestrator still hand-rolls unsealer bootstrap op(s) that the "
        "openbao_init_unseal role must own now:\n  " + "\n  ".join(offenders)
    )


def test_task_8_2_delegates_unsealer_bootstrap_to_openbao_role():
    """Property 3 (Validates: Requirements 2.1, 2.2).

    Task 8.2 must be a single child ``openbao.yml`` run limited to
    ``openbao-unsealer`` with ``--tags install,init`` and the role opt-ins
    (``openbao_mint_seal_token=true``, dev-expose, the Handoff_File path).
    """
    tasks = _orchestrator_tasks()
    delegation = None
    for task in tasks:
        argv = _task_argv_text(task)
        if (
            "ansible-playbook" in argv
            and "ansible/playbooks/openbao.yml" in argv
            and "openbao-unsealer" in argv
        ):
            delegation = argv
            break
    assert delegation is not None, (
        "No child openbao.yml run limited to openbao-unsealer found — the unsealer "
        "bootstrap delegation (Task 8.2) is missing."
    )
    # --tags install,init (Play 1 install + the role's init tag).
    assert "install,init" in delegation, (
        "The unsealer delegation must run --tags install,init (install brings the "
        "Compose stack up; init runs the role's guarded unsealer_bootstrap)."
    )
    # The role opt-ins that make the role mint + hand off the seal token.
    assert "openbao_mint_seal_token=true" in delegation, (
        "The delegation must pass openbao_mint_seal_token=true so the role mints "
        "the Bootstrap_Transit_Token."
    )
    assert "openbao_seal_token_handoff_path" in delegation, (
        "The delegation must pass openbao_seal_token_handoff_path so the role can "
        "hand the minted token back across the child-process boundary."
    )


# --------------------------------------------------------------------------- #
# Handoff capture — slurp -> set_fact(b64decode|trim) -> file:absent, fail-closed.
# Validates: Requirements 5.3, 6.4
# --------------------------------------------------------------------------- #
def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def test_handoff_capture_sequence_exists():
    """Validates: Requirements 5.3, 6.4.

    The consumer-side handoff must be a slurp of the Handoff_File, a
    set_fact binding it via ``b64decode`` + ``trim``, and a ``file: state=absent``
    delete — all referencing ``svc07_seal_token_handoff_path``.
    """
    tasks = _orchestrator_tasks()
    slurp_idx = set_fact_idx = delete_idx = None
    for i, task in enumerate(tasks):
        mods = _task_module_names(task)

        if "ansible.builtin.slurp" in mods:
            spec = task["ansible.builtin.slurp"]
            if isinstance(spec, dict) and "svc07_seal_token_handoff_path" in str(spec.get("src", "")):
                slurp_idx = i

        if "ansible.builtin.set_fact" in mods:
            spec = task["ansible.builtin.set_fact"]
            body = str(spec)
            if (
                isinstance(spec, dict)
                and "svc07_bootstrap_transit_token" in spec
                and "b64decode" in body
                and "trim" in body
            ):
                set_fact_idx = i

        if "ansible.builtin.file" in mods:
            spec = task["ansible.builtin.file"]
            if (
                isinstance(spec, dict)
                and "svc07_seal_token_handoff_path" in str(spec.get("path", ""))
                and str(spec.get("state")) == "absent"
            ):
                delete_idx = i

    assert slurp_idx is not None, "Missing slurp of the seal-token Handoff_File."
    assert set_fact_idx is not None, (
        "Missing set_fact binding svc07_bootstrap_transit_token via b64decode|trim."
    )
    assert delete_idx is not None, (
        "Missing file: state=absent delete of the Handoff_File after capture."
    )
    # Ordering: slurp -> bind -> delete.
    assert slurp_idx < set_fact_idx < delete_idx, (
        "Handoff capture must be ordered slurp -> set_fact(b64decode|trim) -> "
        f"delete (got slurp={slurp_idx}, set_fact={set_fact_idx}, delete={delete_idx})."
    )


def test_handoff_capture_is_fail_closed():
    """Validates: Requirement 6.4.

    A fresh-mint Handoff_File that is present-but-empty must fail closed — there
    must be an assert on a non-empty ``svc07_bootstrap_transit_token`` so an empty
    seal token is never bridged to the primary.
    """
    tasks = _orchestrator_tasks()
    found = False
    for task in tasks:
        if "ansible.builtin.assert" not in _task_module_names(task):
            continue
        body = str(task["ansible.builtin.assert"])
        if "svc07_bootstrap_transit_token" in body and "length" in body:
            found = True
            break
    assert found, (
        "No fail-closed assert on a non-empty svc07_bootstrap_transit_token — an "
        "empty fresh-mint Handoff_File could silently bridge an empty seal token "
        "(Req 6.4)."
    )


def test_secret_bearing_capture_tasks_set_no_log():
    """Validates: Requirement 5.3.

    Every capture task that touches the seal token (slurp, the b64decode set_fact,
    the delete) must set ``no_log`` so the token never leaks to output.
    """
    tasks = _orchestrator_tasks()
    for task in tasks:
        mods = _task_module_names(task)
        touches_token = False
        if "ansible.builtin.slurp" in mods and "svc07_seal_token_handoff_path" in str(
            task["ansible.builtin.slurp"]
        ):
            touches_token = True
        if "ansible.builtin.set_fact" in mods and "svc07_bootstrap_transit_token" in str(
            task["ansible.builtin.set_fact"]
        ):
            touches_token = True
        if "ansible.builtin.file" in mods and "svc07_seal_token_handoff_path" in str(
            task["ansible.builtin.file"]
        ):
            touches_token = True
        if touches_token:
            assert task.get("no_log") is True, (
                f"Seal-token capture task {task.get('name')!r} must set no_log: true."
            )


# --------------------------------------------------------------------------- #
# Ordering — Task 8.2 (+ capture) precedes Task 8.4; openbao.yml stays two plays.
# Validates: Requirement 4.1
# --------------------------------------------------------------------------- #
def test_unsealer_delegation_and_capture_precede_primary_bootstrap():
    """Validates: Requirement 4.1.

    The unsealer delegation (Task 8.2) and its Handoff_File capture must both
    come BEFORE the primary bootstrap child run (Task 8.4, ``--limit openbao``),
    so the unsealer is Transit-ready before the primary's Raft init.
    """
    tasks = _orchestrator_tasks()
    unsealer_idx = capture_delete_idx = primary_idx = None
    for i, task in enumerate(tasks):
        argv = _task_argv_text(task)
        if "ansible/playbooks/openbao.yml" in argv and "openbao-unsealer" in argv:
            unsealer_idx = i
        # Primary run: openbao.yml limited to `openbao` (NOT openbao-unsealer).
        if (
            "ansible/playbooks/openbao.yml" in argv
            and " openbao " in f" {argv} "
            and "openbao-unsealer" not in argv
            and "install,init" in argv
        ):
            primary_idx = i
        if "ansible.builtin.file" in _task_module_names(task):
            spec = task["ansible.builtin.file"]
            if (
                isinstance(spec, dict)
                and "svc07_seal_token_handoff_path" in str(spec.get("path", ""))
                and str(spec.get("state")) == "absent"
            ):
                capture_delete_idx = i

    assert unsealer_idx is not None, "Unsealer delegation task not found."
    assert primary_idx is not None, "Primary bootstrap task not found."
    assert capture_delete_idx is not None, "Handoff capture delete task not found."
    assert unsealer_idx < primary_idx, (
        "The unsealer delegation (Task 8.2) must precede the primary bootstrap "
        "(Task 8.4) — Req 4.1 ordering."
    )
    assert capture_delete_idx < primary_idx, (
        "The Handoff_File capture must complete before the primary bootstrap so "
        "the captured token can be bridged in."
    )


def test_openbao_playbook_retains_two_ordered_plays_unsealer_first():
    """Validates: Requirement 4.1.

    ``openbao.yml`` must keep its two ordered plays with the unsealer FIRST, so
    a ``--limit openbao-unsealer`` child run executes only Play 1.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    assert len(plays) == 2, (
        f"openbao.yml must have exactly two ordered plays; found {len(plays)}."
    )
    assert plays[0].get("hosts") == "openbao-unsealer", (
        "Play 1 must target hosts: openbao-unsealer (unsealer first)."
    )
    assert plays[1].get("hosts") == "openbao", (
        "Play 2 must target hosts: openbao (primary second)."
    )
    # Play 1 must select the unsealer role via the play var.
    assert str(plays[0].get("vars", {}).get("openbao_role")) == "unsealer", (
        "Play 1 must set openbao_role: unsealer as a play var."
    )
