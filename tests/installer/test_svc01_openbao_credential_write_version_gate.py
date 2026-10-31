"""Offline structural bug-condition + preservation test for the SVC-01 OpenBao
credential-write version-gate decoupling bugfix.

Spec: .kiro/specs/fix-svc01-openbao-credential-write-version-gate-decoupling/

Methodology: test-before-fix (bugfix bug-condition). This file PyYAML-parses the
touched Ansible sources and asserts BOTH:

  * Property 1 (Bug Condition -> Expected Behavior): the EXPECTED POST-FIX
    structure. These assertions FAIL on the unfixed tree (the version gate is
    still on the write, the consumer default is still "", and there is no
    loud-skip assert) and PASS after the fix. Their pre-fix failure is the
    counterexample that confirms the defect lives in the gate/default/missing
    assert — exactly isBugCondition(X) = openbao_addr_present_now AND
    already_at_pinned_image.

  * Property 2 (Preservation): invariants that must NOT change. These PASS on the
    unfixed tree already (observation-first) and must keep passing after the fix:
    the address gate on the write, the install-step version gate on the Compose
    deploy, the read -> reuse -> generate ordering + address-only gates, the
    no_log / BAO_TOKEN-in-environment secret handling, the no_log-safe loud-skip
    assert (no .stdout), and the MariaDB Change-4 deferral (no OpenBao write).

Run:
  ~/venv/devinfra/bin/pytest \
    tests/installer/test_svc01_openbao_credential_write_version_gate.py \
    -m "not requires_infra"
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PG_TASKS = _REPO_ROOT / "ansible" / "roles" / "postgres-server" / "tasks" / "main.yml"
_MARIADB_TASKS = _REPO_ROOT / "ansible" / "roles" / "mariadb-server" / "tasks" / "main.yml"
_CONSUMER_PLAY = _REPO_ROOT / "ansible" / "playbooks" / "project-consumer-bootstrap.yml"

# Task names (match the role verbatim).
_WRITE_TASK = "Postgres | Persist DB credentials to OpenBao via the bao CLI (no_log)"
_READ_TASK = "Postgres | Read any existing DB credentials from OpenBao (no_log)"
_REUSE_TASK = "Postgres | Reuse existing OpenBao passwords when present (no_log)"
_GENERATE_TASK = "Postgres | Generate DB passwords (no_log)"
_DEPLOY_TASK = "Postgres | Deploy the Compose stack (docker-compose-app)"
_LOUD_ASSERT_TASK = "Postgres | Assert the OpenBao credential write succeeded on a wired run"

# The install/version gate condition that is the defect when it appears on the write.
_VERSION_GATE = "not postgres_already_at_pinned_version"
# A fragment that identifies the OpenBao-address length gate (kept on the write).
_ADDRESS_GATE_FRAGMENT = "OPENBAO_ADDR"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    """Flatten a task list, descending into block/rescue/always."""
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always"):
            if key in t:
                yield from _iter_tasks(t[key])


def _load_role_tasks(path: Path) -> list[dict]:
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _ordered_role_tasks(path: Path) -> list[dict]:
    """Top-level task sequence (NOT flattened) to assert ordering."""
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            return [t for t in doc if isinstance(t, dict)]
    return []


def _find_task(tasks: list[dict], name: str) -> dict | None:
    for t in tasks:
        if t.get("name") == name:
            return t
    return None


def _when_list(task: dict) -> list[str]:
    """Normalise a task's `when:` into a list of string conditions."""
    when = task.get("when")
    if when is None:
        return []
    if isinstance(when, str):
        return [when]
    if isinstance(when, list):
        return [str(c) for c in when]
    return [str(when)]


def _module_names(t: dict) -> set[str]:
    _non_module = {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
    return {k for k in t if k not in _non_module}


def _consumer_play_vars() -> dict:
    docs = list(yaml.safe_load_all(_CONSUMER_PLAY.read_text(encoding="utf-8")))
    for doc in docs:
        if isinstance(doc, list):
            for play in doc:
                if isinstance(play, dict) and "vars" in play:
                    return play["vars"] or {}
    return {}


# --------------------------------------------------------------------------- #
# Property 1 — Bug Condition -> Expected post-fix structure (FAIL before fix)  #
# --------------------------------------------------------------------------- #
def test_write_task_is_not_version_gated():
    """P1: the write task's `when:` must NOT contain the version gate.

    BEFORE FIX: FAILS — the write carries `not postgres_already_at_pinned_version`,
    so a wired re-run against an already-pinned container skips the credential
    write permanently (isBugCondition). AFTER FIX: PASSES.
    Validates: Requirements 2.1, 2.2, 2.3
    """
    tasks = _load_role_tasks(_PG_TASKS)
    write = _find_task(tasks, _WRITE_TASK)
    assert write is not None, f"write task {_WRITE_TASK!r} not found"
    conds = _when_list(write)
    assert _VERSION_GATE not in conds, (
        "BUG: the OpenBao credential-write task is still gated on the install "
        f"version check {_VERSION_GATE!r}. On a wired re-run against an "
        "already-pinned container the write is skipped permanently, so the "
        "credential never reaches secret/proj-<slug>/postgres. The version gate "
        f"must be removed from this task's when:. Current when: {conds}"
    )


def test_loud_skip_assert_task_exists():
    """P1: the loud wired-but-not-persisted assert task must exist.

    BEFORE FIX: FAILS — no such task. AFTER FIX: PASSES.
    Validates: Requirement 2.6
    """
    tasks = _load_role_tasks(_PG_TASKS)
    assert _find_task(tasks, _LOUD_ASSERT_TASK) is not None, (
        f"BUG: the loud-skip signal task {_LOUD_ASSERT_TASK!r} does not exist. A "
        "wired run that failed to persist the credential proceeds silently; the "
        "role must assert the write succeeded on a wired run (Req 2.6)."
    )


def test_consumer_openbao_addr_default_is_wired():
    """P1: the consumer orchestrator defaults openbao_addr to the primary address.

    BEFORE FIX: FAILS — default is "". AFTER FIX: PASSES ("10.0.20.10").
    Validates: Requirement 2.4
    """
    play_vars = _consumer_play_vars()
    assert "openbao_addr" in play_vars, "consumer play has no openbao_addr var"
    assert play_vars["openbao_addr"] == "10.0.20.10", (
        "BUG: project-consumer-bootstrap.yml defaults openbao_addr to "
        f"{play_vars['openbao_addr']!r}, so a standard consumer bring-up is "
        "UNWIRED by default and the first run skips the credential write (setting "
        "up the already-pinned trap). It must default to the deterministic "
        'OpenBao primary "10.0.20.10" (NET-00 host_index 0 on VLAN 20).'
    )


# --------------------------------------------------------------------------- #
# Property 2 — Preservation (PASS before AND after fix)                        #
# --------------------------------------------------------------------------- #
def test_write_task_keeps_address_gate():
    """Preservation: the write's OpenBao-address length gate is retained, so a
    dev/offline (unwired) run still skips the write (Req 3.3)."""
    tasks = _load_role_tasks(_PG_TASKS)
    write = _find_task(tasks, _WRITE_TASK)
    assert write is not None, f"write task {_WRITE_TASK!r} not found"
    conds = _when_list(write)
    assert any(_ADDRESS_GATE_FRAGMENT in c for c in conds), (
        "PRESERVATION: the write task must keep its OpenBao-address length gate "
        f"(the {_ADDRESS_GATE_FRAGMENT}/openbao_addr condition) so an unwired "
        f"dev/offline run still skips the write. Current when: {conds}"
    )


def test_install_deploy_keeps_version_gate():
    """Preservation: the Compose (re)deploy stays version-gated (Req 3.4)."""
    tasks = _load_role_tasks(_PG_TASKS)
    deploy = _find_task(tasks, _DEPLOY_TASK)
    assert deploy is not None, f"deploy task {_DEPLOY_TASK!r} not found"
    conds = _when_list(deploy)
    assert _VERSION_GATE in conds, (
        "PRESERVATION: the Compose deploy must KEEP "
        f"`when: {_VERSION_GATE}` — only the credential write is decoupled from "
        f"the version gate, not the install steps. Current when: {conds}"
    )


def test_never_rotate_read_reuse_generate_order_preserved():
    """Preservation: read -> reuse -> generate ordering is unchanged, so a re-run
    reuses an existing credential and never rotates it (Req 3.1)."""
    ordered = _ordered_role_tasks(_PG_TASKS)
    names = [t.get("name") for t in ordered]
    assert _READ_TASK in names, f"{_READ_TASK!r} missing"
    assert _REUSE_TASK in names, f"{_REUSE_TASK!r} missing"
    assert _GENERATE_TASK in names, f"{_GENERATE_TASK!r} missing"
    i_read = names.index(_READ_TASK)
    i_reuse = names.index(_REUSE_TASK)
    i_gen = names.index(_GENERATE_TASK)
    assert i_read < i_reuse < i_gen, (
        "PRESERVATION: the never-rotate ordering read -> reuse -> generate must "
        f"hold (read@{i_read}, reuse@{i_reuse}, generate@{i_gen}). Reordering "
        "would let the generate step mint fresh passwords before reuse binds the "
        "stored ones, rotating an existing credential and breaking held clients."
    )

    # The read and reuse steps stay address-gated only (never version-gated).
    read = _find_task(_load_role_tasks(_PG_TASKS), _READ_TASK)
    reuse = _find_task(_load_role_tasks(_PG_TASKS), _REUSE_TASK)
    for task, label in ((read, _READ_TASK), (reuse, _REUSE_TASK)):
        conds = _when_list(task)
        assert any(_ADDRESS_GATE_FRAGMENT in c for c in conds), (
            f"PRESERVATION: {label!r} must stay OpenBao-address gated. when: {conds}"
        )
        assert _VERSION_GATE not in conds, (
            f"PRESERVATION: {label!r} must NOT be version-gated. when: {conds}"
        )


def test_secret_handling_preserved():
    """Preservation: the read + write tasks keep no_log: true and carry the admin
    token in the command environment (BAO_TOKEN) on argv-as-env, never bare argv;
    and the loud-skip assert references only .skipped/.rc, never .stdout (Req 3.2)."""
    tasks = _load_role_tasks(_PG_TASKS)
    for name in (_READ_TASK, _WRITE_TASK):
        t = _find_task(tasks, name)
        assert t is not None, f"{name!r} not found"
        assert t.get("no_log") is True, f"PRESERVATION: {name!r} must keep no_log: true"
        # BAO_TOKEN is passed via `docker exec -e BAO_TOKEN=...` (an env var to the
        # container), threaded through the command argv list as an `-e` pair — the
        # admin token value must never appear as a standalone positional argv.
        body = yaml.safe_dump(t)
        assert "BAO_TOKEN=" in body, (
            f"PRESERVATION: {name!r} must carry the admin token in the command "
            "environment (BAO_TOKEN=...), not on a bare argv."
        )

    # The loud-skip assert, once present, must be no_log-safe: inspect only
    # .skipped / .rc on the write register, never the secret-bearing .stdout.
    loud = _find_task(tasks, _LOUD_ASSERT_TASK)
    if loud is not None:
        body = yaml.safe_dump(loud)
        assert "postgres_openbao_write.stdout" not in body, (
            "PRESERVATION: the loud-skip assert must NOT reference "
            "postgres_openbao_write.stdout (it is secret-bearing). Inspect only "
            ".skipped and .rc."
        )
        assert "postgres_openbao_write.skipped" in body, (
            "the loud-skip assert must inspect postgres_openbao_write.skipped"
        )
        assert "postgres_openbao_write.rc" in body, (
            "the loud-skip assert must inspect postgres_openbao_write.rc"
        )
        # Must share the SAME address gate as the write (inert on unwired runs).
        conds = _when_list(loud)
        assert any(_ADDRESS_GATE_FRAGMENT in c for c in conds), (
            "PRESERVATION: the loud-skip assert must be address-gated so it stays "
            f"inert on an unwired dev/offline run. when: {conds}"
        )


def test_mariadb_has_no_openbao_write():
    """Preservation (Change-4 deferral): mariadb-server has NO OpenBao write task.

    Documents the intentional, tested deferred state — there is no symmetric
    version-gated OpenBao write to decouple in the MariaDB engine (Req 3.5).
    """
    tasks = _load_role_tasks(_MARIADB_TASKS)
    for t in tasks:
        modules = _module_names(t)
        for mod in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
            spec = t.get(mod)
            if spec is None:
                continue
            blob = yaml.safe_dump(spec).lower()
            assert "bao kv put" not in blob and "- bao\n" not in blob, (
                "PRESERVATION (Change-4 deferral): mariadb-server unexpectedly has "
                f"an OpenBao write in task {t.get('name')!r}. The deferral assumes "
                "no OpenBao write exists; revisit the design if this changed."
            )
        _ = modules  # modules computed for clarity; not otherwise asserted
