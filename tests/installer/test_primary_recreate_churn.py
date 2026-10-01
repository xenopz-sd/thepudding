"""Offline structural guard for the SVC-07 primary recreate-churn bugfix.

Bugfix: fix-svc07-primary-recreate-churn (Fix B).

Root cause (see bugfix.md / design.md): on a fresh-provision PRIMARY bring-up,
``ansible/playbooks/openbao.yml`` **Play 2** (``hosts: openbao``,
``openbao_role: primary``) is a plain ``roles:`` list
(``openbao_install`` -> ``openbao_init_unseal`` -> ``openbao_project_onboard``)
with **NO** ``meta: flush_handlers`` seam between the install role and the
init/unseal role. During ``openbao_install`` the primary ``config.hcl`` template
task and the MOCK-TLS cert generate/install tasks each ``notify: Restart
openbao``. With no flush seam those handlers flush at **END OF PLAY 2** — i.e.
AFTER ``openbao_init_unseal`` has already bootstrapped the primary and
minted/revoked its admin token — and the ``Restart openbao`` handler runs
``community.docker.docker_compose_v2 ... recreate: always``, a full destroy +
recreate of the healthy, already-bootstrapped primary. That recreate is slow to
settle (>150s live), so Phase 4's ~150s deadline expires while the container is
absent/not-running and the installer exits non-zero on an otherwise-healthy
primary.

The fix (Task 3) mirrors the committed UNSEALER Play-1 seam: restructure Play 2
into a ``tasks:`` list of ``import_role`` calls with a ``tags: ["always"]``
``meta: flush_handlers`` seam positioned BETWEEN the ``openbao_install`` import
and the ``openbao_init_unseal`` import, so the config.hcl / MOCK-TLS recreate
flushes BEFORE bootstrap and nothing recreates the primary after it is healthy.

This test encodes the STRUCTURAL invariant whose ABSENCE causes the live bug
(design.md "Exploratory Bug Condition Checking"). It is pure-logic (PyYAML parse
of the committed playbook) — no infrastructure needed. It PARSES YAML only; it
never embeds or prints any secret value.

**On the UNFIXED code the bug-condition case FAILS** — Play 2 is a plain
``roles:`` list with no ``meta: flush_handlers`` seam. That failure is the
SUCCESS signal for a bug-condition exploration test: it confirms the ordering
defect's structural cause. The ``requires_infra`` live-repro stub is deselected
by default per the root ``pytest.ini`` (``addopts = -m "not requires_infra"``)
and documents the live from-scratch reproduction; it does not run offline.

A NEW file (rather than extending Fix A's test file) keeps Fix B's guards
cleanly separated from Fix A's. Per the conftest-collision rule, the
play-loading + ordered-node helpers are COPIED locally here (mirroring the
``_play1_ordered_nodes`` idiom in ``test_unsealer_reseal_ordering.py``,
generalised to any play) rather than imported across the test tree.

Test cases:
* 1 — Flush-seam-between-install-and-init (Play 2): Play 2 (``hosts: openbao``)
  has a ``meta: flush_handlers`` node ordered BETWEEN the ``openbao_install``
  import and the ``openbao_init_unseal`` import, tagged ``["always"]`` (FAILS on
  unfixed code — the SUCCESS signal).
* 2 — Live-repro stub (``requires_infra``, deselected by default): documents the
  fresh bring-up reproduction (GREEN Phase-4, installer exit 0, no
  post-bootstrap recreate).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_primary_recreate_churn.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENBAO_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "openbao.yml"


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally from test_unsealer_reseal_ordering.py (conftest
# collision rule: no cross-tree import). The ordered-node builder is generalised
# from `_play1_ordered_nodes` to work on ANY play, and to carry each node's
# `tags:` so the seam's `tags: ["always"]` can be inspected.
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


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_tags(task: dict) -> list[str]:
    """Return a task's `tags:` as a list of strings (str or list forms)."""
    tags = task.get("tags", [])
    if isinstance(tags, str):
        return [tags]
    if isinstance(tags, (list, tuple)):
        return [str(t) for t in tags]
    return []


def _ordered_nodes(play: dict) -> list[tuple[str, str, list[str]]]:
    """Return an ordered list of (kind, identifier, tags) nodes for any play.

    Ansible executes a play's sections in the fixed order pre_tasks -> roles ->
    tasks -> post_tasks (handlers flush at the boundaries / end of play). To
    reason about "is there a flush_handlers node positioned BETWEEN the
    openbao_install import and the openbao_init_unseal import", we flatten those
    sections into one execution-ordered sequence:

      * ("role", <role name>, <tags>)         for each entry in `roles:`
      * ("meta", <meta arg>, <tags>)          for a `meta:` task
      * ("import", <imported role/file>, <tags>) for import_role/include_role/
                                                include_tasks/import_tasks
      * ("task", <first module name>, <tags>) for any other task

    This mirrors `_play1_ordered_nodes` in test_unsealer_reseal_ordering.py,
    generalised to any play and extended to carry each node's tags (so the
    seam's `tags: ["always"]` can be asserted). It works whether the play is a
    plain `roles:` list (unfixed) or a `tasks:` list of import_role calls around
    a `meta: flush_handlers` (fixed).
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
            role_tags = _task_tags(role)
            nodes.append(("role", str(role.get("role", role.get("name", ""))), role_tags))
        elif isinstance(role, str):
            nodes.append(("role", role, []))

    for section in ("tasks", "post_tasks"):
        for task in _iter_tasks(play.get(section)):
            _emit_task(task)

    return nodes


def _index_of_role(nodes, role_name: str) -> int | None:
    """Return the index of a role/import node matching role_name, else None."""
    for i, (kind, ident, _tags) in enumerate(nodes):
        if kind in ("role", "import") and role_name in ident:
            return i
    return None


# --------------------------------------------------------------------------- #
# Test case 1 — Flush-seam-between-install-and-init (Play 2), the bug-condition
# exploration test. Also folds in the corroborating tags:["always"] check so
# Task 1 yields exactly ONE bug-condition failure, failing for the right reason.
# Validates: Requirements 1.1, 1.2, 1.3, 1.4 (the structural invariant whose
# ABSENCE is the bug) and the fixed-side 2.1, 2.4.
# EXPECTED on unfixed code: FAILS (Play 2 is a `roles:` list with no seam).
# --------------------------------------------------------------------------- #
def test_flush_handlers_seam_between_install_and_init_in_play2():
    """Bug condition (Property 1) — Validates: Requirements 1.1-1.4, 2.1, 2.4.

    Play 2 (``hosts: openbao``, ``openbao_role: primary``) MUST order a
    ``meta: flush_handlers`` seam BETWEEN the ``openbao_install`` import and the
    ``openbao_init_unseal`` import, so any ``Restart openbao`` handler queued by
    ``openbao_install``'s first-run config.hcl-template + MOCK-TLS cert tasks is
    flushed while the primary is freshly installed but BEFORE
    init/unseal/bootstrap — making the recreate happen EARLY and leaving nothing
    to recreate the primary after it is healthy. The seam MUST additionally carry
    ``tags: ["always"]`` so it runs under the orchestrator's
    ``--tags "install,init"`` invocation (svc-07-bootstrap.yml Task 8.4); an
    UNTAGGED ``meta: flush_handlers`` is filtered out under ``--tags`` and falls
    back to flushing at END OF PLAY, defeating the fix (same rationale the
    committed Play-1 seam documents).

    On UNFIXED code Play 2 is a plain ``roles:`` list with NO ``meta:
    flush_handlers`` seam, so this assertion FAILS — the SUCCESS signal for a
    bug-condition exploration test. It PASSES after the fix.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    play2 = next((p for p in plays if p.get("hosts") == "openbao"), None)
    assert play2 is not None, "Play 2 (hosts: openbao) not found in openbao.yml."

    # Corroborate this is the primary play (the churn is the primary's).
    assert str(play2.get("vars", {}).get("openbao_role")) == "primary", (
        "Play 2 (hosts: openbao) must set `openbao_role: primary` as a play var "
        f"(got {play2.get('vars', {}).get('openbao_role')!r})."
    )

    nodes = _ordered_nodes(play2)

    install_idx = _index_of_role(nodes, "openbao_install")
    init_idx = _index_of_role(nodes, "openbao_init_unseal")
    assert install_idx is not None, (
        "openbao_install role/import not found in Play 2 — cannot check ordering."
    )
    assert init_idx is not None, (
        "openbao_init_unseal role/import not found in Play 2 — cannot check ordering."
    )

    # Find a meta: flush_handlers node and require it to sit strictly BETWEEN the
    # install import and the init/unseal import.
    seam_idx = next(
        (
            i
            for i, (kind, ident, _tags) in enumerate(nodes)
            if kind == "meta" and "flush_handlers" in ident
        ),
        None,
    )
    assert seam_idx is not None, (
        "COUNTEREXAMPLE (bug): Play 2 (hosts: openbao) has NO `meta: "
        "flush_handlers` seam. openbao_install's first-run config.hcl-template + "
        "MOCK-TLS cert tasks each `notify: Restart openbao`; with no flush seam "
        "those handlers flush at END OF PLAY 2 — AFTER openbao_init_unseal has "
        "bootstrapped the primary — and `docker_compose_v2 recreate: always` "
        "destroys the healthy primary. The slow recreate then outlasts Phase 4's "
        "~150s deadline, so the installer exits non-zero on an otherwise-healthy "
        "primary. The fix must insert a `tags: [\"always\"]` `meta: "
        "flush_handlers` seam between the openbao_install import and the "
        "openbao_init_unseal import (mirroring the committed Play-1 unsealer seam). "
        "On the unfixed tree Play 2 is a plain `roles:` list — no meta node at all."
    )
    assert install_idx < seam_idx < init_idx, (
        "COUNTEREXAMPLE (bug): the Play-2 `meta: flush_handlers` seam is not "
        "positioned BETWEEN the openbao_install import and the openbao_init_unseal "
        f"import (install index={install_idx}, seam index={seam_idx}, "
        f"init/unseal index={init_idx}). The seam must flush openbao_install's "
        "queued `Restart openbao` handlers AFTER install but BEFORE bootstrap, so "
        "the recreate happens early and nothing recreates the primary after it is "
        "healthy."
    )

    # Corroborating check (folded in so Task 1 is a single clean bug-condition
    # failure): the seam must carry `tags: ["always"]`, else it is filtered out
    # under the orchestrator's `--tags "install,init"` run and never fires.
    seam_tags = nodes[seam_idx][2]
    assert "always" in seam_tags, (
        "COUNTEREXAMPLE (bug): the Play-2 `meta: flush_handlers` seam does not "
        "carry `tags: [\"always\"]`. The orchestrator (svc-07-bootstrap.yml Task "
        "8.4) runs this play with `--tags \"install,init\"`; an untagged "
        "flush_handlers is SKIPPED under `--tags`, so the seam never fires mid-play "
        "and the `Restart openbao` handler falls back to flushing at END OF PLAY — "
        "recreating the just-bootstrapped healthy primary and defeating the fix. "
        f"Seam tags seen: {seam_tags}."
    )


# --------------------------------------------------------------------------- #
# Test case 2 — Live-repro stub (requires_infra; deselected by default).
# Validates: Requirements 2.1, 2.2, 2.3 behaviourally (the live acceptance).
# --------------------------------------------------------------------------- #
@pytest.mark.requires_infra
def test_live_from_scratch_primary_no_post_bootstrap_recreate_and_installer_exits_0():
    """Live from-scratch reproduction (requires_infra — deselected by default).

    DOCUMENTED live gate; not executed offline. On a throwaway cluster, a
    from-scratch single-command Developer-B bring-up of SVC-07:

    UNFIXED code (reproduces the bug):
      * Phase 4 TIMES OUT: Fix A's Stage-1 gate (`docker inspect -f
        {{.State.Running}} openbao-openbao-1`) fails all its retries because the
        primary is mid-recreate for the whole ~150s deadline window; the
        installer exits NON-ZERO.
      * `docker events` on the primary show a `kill`/`die`/`destroy` +
        `create`/`start` of `openbao-openbao-1` AFTER the init/unseal bootstrap
        exec sequence — the end-of-play `Restart openbao` recreate.
      * The healthy container (`bao status`: `initialized:true, sealed:false`)
        only appears AFTER the installer has already exited.

    FIXED code (the acceptance — closes the SVC-07 from-scratch milestone):
      * Phase 4 turns GREEN (Fix A's two-stage `docker exec ... bao status` gate
        finds a stable, healthy primary), the installer EXITS 0, and the
        completion-summary banner prints.
      * `docker events` on the primary show NO `kill`/`stop`/`die`/`destroy` of
        `openbao-openbao-1` AFTER the init/unseal bootstrap exec sequence — the
        seam moved the config.hcl / MOCK-TLS recreate to BEFORE bootstrap.
      * The idempotent re-run reports `changed=0`, notifies no `Restart openbao`,
        and performs no recreate (the seam is a no-op on a converged primary).

    This stub is marked ``@pytest.mark.requires_infra`` and is therefore
    DESELECTED by default via the root ``pytest.ini`` (``addopts = -m "not
    requires_infra"``). The authoritative behavioural evidence is captured in the
    spec's validation.md from a real throwaway-cluster run; it must never echo any
    secret value (reference `bao status` fields / container `State` fields /
    `docker events` ACTION names / HTTP status strings only).
    """
    pytest.skip(
        "requires-infra: live from-scratch SVC-07 bring-up on a throwaway cluster "
        "(Phase-4 GREEN, installer exits 0 with the completion banner; `docker "
        "events` show NO post-bootstrap kill/destroy of openbao-openbao-1). "
        "Documented live gate; run under -m requires_infra with a wired-up "
        "cluster. See validation.md."
    )


# ===========================================================================
# ===========================================================================
# TASK 2 — Fix-B-owned PRESERVATION guards (Property 2).
# ===========================================================================
# ===========================================================================
#
# UNLIKE the bug-condition case above (which FAILS on the unfixed tree and only
# PASSES once Fix B lands), the guards below encode the CURRENT committed
# baseline and therefore **PASS on the UNFIXED tree**. They pin the invariants
# Fix B must NOT regress (design.md "Preservation Checking", Property 2):
#
#   P2-2 Two-play + Play-2 targeting preserved — openbao.yml stays exactly two
#        plays, Play 1 `hosts: openbao-unsealer`, Play 2 `hosts: openbao`
#        (Fix B restructures Play 2 internally but must not add/remove/retarget
#        a play). (Req 3.5, 3.6)
#   P2-4 Handler + Fix A + pre-up volume block preserved — the `Restart openbao`
#        handler keeps `docker_compose_v2` + `recreate: always`; Fix A's Phase-4
#        `docker exec ... bao status` poll is intact; the pre-up
#        volume-ownership block is present in openbao_install/tasks/main.yml.
#        (Req 3.1, 3.3, 3.4)
#   P2-5 Secret discipline preserved — the primary config.hcl-template task (the
#        one that carries the seal token and `notify: Restart openbao`) sets
#        `no_log`. (Req 3.7)
#
# These reuse the module-level helpers above (`_load_plays`); the handler /
# tasks / bootstrap files are parsed with a local YAML/text read. Parse-only:
# they never embed or print any secret value.

_HANDLERS_MAIN = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_install" / "handlers" / "main.yml"
)
_INSTALL_TASKS_MAIN = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_install" / "tasks" / "main.yml"
)
_SVC07_BOOTSTRAP = _REPO_ROOT / "ansible" / "playbooks" / "svc-07-bootstrap.yml"
# The Fix-A Phase-4 `docker exec ... bao status` health gate moved out of
# svc-07-bootstrap.yml into the svc07-health.yml CHILD play when the installer
# was thinned to a five-role orchestrator (svc07-installer-simplification, Task
# 6.1/11). The P2-4 preservation guard that pins Fix A is repointed to it.
_SVC07_HEALTH_PLAY = _REPO_ROOT / "ansible" / "playbooks" / "svc07-health.yml"


def _load_task_list(path: Path) -> list[dict]:
    """Return the flat ordered list of task dicts from a role tasks/handlers file."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


# --------------------------------------------------------------------------- #
# P2-2 — Two-play + Play-2 targeting preserved (openbao.yml).
# Validates: Requirements 3.5, 3.6 (preservation — holds on the UNFIXED tree).
# --------------------------------------------------------------------------- #
def test_preserve_two_plays_and_play2_targets_primary():
    """Preservation (Property 2, P2-2) — Validates: Requirements 3.5, 3.6.

    ``openbao.yml`` MUST stay exactly two ordered plays — Play 1
    ``hosts: openbao-unsealer`` (unsealer first), Play 2 ``hosts: openbao``
    (primary second). Fix B restructures Play 2 INTERNALLY (roles: -> tasks:
    with a flush seam) but must not add, remove, reorder, or retarget a play.
    This baseline holds on the UNFIXED tree and must survive the fix.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    assert len(plays) == 2, (
        f"PRESERVATION: openbao.yml must have exactly two ordered plays; found "
        f"{len(plays)}."
    )
    assert plays[0].get("hosts") == "openbao-unsealer", (
        "PRESERVATION: Play 1 must target hosts: openbao-unsealer (unsealer "
        f"first) — got {plays[0].get('hosts')!r}."
    )
    assert plays[1].get("hosts") == "openbao", (
        "PRESERVATION: Play 2 must target hosts: openbao (primary second) — got "
        f"{plays[1].get('hosts')!r}."
    )


# --------------------------------------------------------------------------- #
# P2-4 — Handler + Fix A + pre-up volume block preserved.
# Validates: Requirements 3.1, 3.3, 3.4 (preservation — holds on UNFIXED tree).
# --------------------------------------------------------------------------- #
def test_preserve_restart_openbao_handler_recreate_always():
    """Preservation (Property 2, P2-4) — Validates: Requirement 3.4.

    The ``Restart openbao`` handler in ``openbao_install/handlers/main.yml`` MUST
    keep ``community.docker.docker_compose_v2`` with ``recreate: always`` — Fix B
    only reorders WHEN this handler flushes (via the Play-2 seam), never what it
    does. Baseline holds on the UNFIXED tree.
    """
    handlers = _load_task_list(_HANDLERS_MAIN)
    restart = next(
        (h for h in handlers if str(h.get("name", "")) == "Restart openbao"), None
    )
    assert restart is not None, (
        "PRESERVATION: the `Restart openbao` handler was not found in "
        "openbao_install/handlers/main.yml."
    )
    mods = _task_module_names(restart)
    assert "community.docker.docker_compose_v2" in mods, (
        "PRESERVATION: the `Restart openbao` handler must keep using "
        f"community.docker.docker_compose_v2 (modules seen: {sorted(mods)})."
    )
    spec = restart.get("community.docker.docker_compose_v2", {})
    assert isinstance(spec, dict) and str(spec.get("recreate")) == "always", (
        "PRESERVATION: the `Restart openbao` handler must keep `recreate: always` "
        f"(got recreate={spec.get('recreate')!r})."
    )


def test_preserve_pre_up_volume_ownership_block_present():
    """Preservation (Property 2, P2-4) — Validates: Requirements 3.1, 3.3.

    The pre-up volume-ownership block ("Pre-create + chown the OpenBao named-
    volume mountpoints BEFORE first start") in ``openbao_install/tasks/main.yml``
    MUST remain present — it already covers the primary's [data, audit] set, so
    the post-up chown is a primary no-op and is NOT the primary's churn trigger.
    Fix B does not touch it. Baseline holds on the UNFIXED tree.
    """
    tasks = _load_task_list(_INSTALL_TASKS_MAIN)
    names = [str(t.get("name", "")) for t in tasks]
    assert any(
        "Pre-create + chown the OpenBao named-volume mountpoints BEFORE first start"
        in n
        for n in names
    ), (
        "PRESERVATION: the pre-up volume-ownership block ('Pre-create + chown the "
        "OpenBao named-volume mountpoints BEFORE first start') is missing from "
        "openbao_install/tasks/main.yml — Fix B must not remove it. Task names "
        f"seen: {names}."
    )


def test_preserve_fix_a_phase4_docker_exec_bao_status_check():
    """Preservation (Property 2, P2-4) — Validates: Requirement 3.4.

    Fix A's recreate-robust health gate — a `docker exec ... bao status` poll of
    the primary — MUST remain intact. Fix B (a Play-2 restructure) does not touch
    the health gate. Repointed for svc07-installer-simplification (Task 6.1/11):
    the gate moved from svc-07-bootstrap.yml into the svc07-health.yml CHILD play,
    so this lightweight text guard reads svc07-health.yml. (The authoritative Fix
    A structural assertions live in ``test_primary_health_check_via_bao_status.py``,
    also repointed to svc07-health.yml.) Parse-only: no secret value is read.
    """
    text = _SVC07_HEALTH_PLAY.read_text(encoding="utf-8")
    assert "docker" in text and "exec" in text and "bao status" in text.lower() \
        or ("docker exec" in text and "status" in text), (
        "PRESERVATION: Fix A's `docker exec ... bao status` primary health check "
        "appears to be gone from svc07-health.yml — Fix B must leave Fix A's gate "
        "unchanged."
    )


# --------------------------------------------------------------------------- #
# P2-5 — Secret discipline preserved (openbao_install primary config.hcl task).
# Validates: Requirement 3.7 (preservation — holds on the UNFIXED tree).
# --------------------------------------------------------------------------- #
def test_preserve_primary_config_template_sets_no_log():
    """Preservation (Property 2, P2-5) — Validates: Requirement 3.7.

    The primary config.hcl-template task in ``openbao_install/tasks/main.yml``
    (the one that carries the seal token and `notify: Restart openbao`) MUST set
    ``no_log`` — its rendered config embeds the seal "transit" token. Fix B does
    not touch this task. Baseline holds on the UNFIXED tree.
    """
    tasks = _load_task_list(_INSTALL_TASKS_MAIN)
    primary_cfg = next(
        (t for t in tasks if str(t.get("name", "")) == "Template the primary config.hcl"),
        None,
    )
    assert primary_cfg is not None, (
        "PRESERVATION: the 'Template the primary config.hcl' task was not found "
        "in openbao_install/tasks/main.yml."
    )
    # Corroborate it is the seal-token-bearing, restart-notifying task.
    assert primary_cfg.get("notify") == "Restart openbao", (
        "PRESERVATION: the primary config.hcl task must keep "
        f"`notify: Restart openbao` (got {primary_cfg.get('notify')!r})."
    )
    no_log = primary_cfg.get("no_log")
    assert no_log is True or str(no_log).strip().lower() in ("true", "yes") \
        or "{{" in str(no_log), (
        "PRESERVATION: the primary config.hcl-template task carries the seal "
        f"token and MUST set no_log (got no_log={no_log!r})."
    )
