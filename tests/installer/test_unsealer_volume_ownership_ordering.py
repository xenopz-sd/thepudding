"""Offline structural guard for the SVC-07 unsealer volume-ownership-recreate bugfix.

Bugfix: fix-svc07-unsealer-volume-ownership-recreate.

Root cause (see bugfix.md / design.md, CONFIRMED live): on a fresh provision the
``openbao_install`` role establishes correct named-volume ownership only AFTER
the Compose stack is already up, via the post-Compose task "Chown volume
mountpoints to the openbao uid/gid when they differ"
(``ansible/roles/openbao_install/tasks/main.yml``). Docker creates the named
data volume ``root:root`` (uid 0) while OpenBao runs as
``openbao_container_uid=100`` / ``openbao_container_gid=1000``, so that task
chowns and — via its ``notify: Restart openbao`` — queues the ``Restart openbao``
handler, which runs ``community.docker.docker_compose_v2 ... recreate: always``
(a container DESTROY + CREATE). Ansible flushes handlers AT END OF PLAY, AFTER
``openbao_init_unseal`` has already ``operator init`` + ``operator unseal``-ed the
unsealer. The recreated Shamir-sealed container boots fresh against the
now-initialised Raft volume and comes up SEALED. ``RestartCount=0`` because a
Compose recreate is a new container, not a restart.

This test encodes the STRUCTURAL invariant whose ABSENCE causes the live bug
(design.md "Exploratory Bug Condition Checking", Property 1). It is pure-logic
(PyYAML parse of the committed role) — no infrastructure needed. It PARSES YAML
only; it never embeds or prints any secret value.

**On the UNFIXED code the primary structural case (1) FAILS** — there is NO
pre-up ownership task (a chown to the openbao uid/gid of a resolved volume
mountpoint, and/or a ``community.docker.docker_volume`` pre-create for the
role's data volume) ordered BEFORE the first ``docker-compose-app`` include; the
only ownership task is the POST-include "Chown … when they differ" that
``notify: Restart openbao``. That failure is the SUCCESS signal for a
bug-condition exploration test: it confirms the ordering defect's structural
cause.

The corroborating case (2) is DESCRIPTIVE — it documents that today the ONLY
ownership/chown task carrying ``notify: Restart openbao`` is the post-include
one; it PASSES on the unfixed tree. The ``requires_infra`` live-repro stub (3)
is deselected by default per the root ``pytest.ini`` (``addopts = -m "not
requires_infra"``) and documents the live from-scratch reproduction; it does not
run offline.

Test cases:
* 1 — Pre-up-ownership-precedes-reconcile (structural, PRIMARY): a pre-up
  ownership task (chown to the openbao uid/gid of a volume mountpoint, and/or a
  ``docker_volume`` pre-create) is ordered BEFORE the first
  ``include_role: docker-compose-app`` node (FAILS on unfixed code).
* 2 — Post-up-chown-is-the-only-notify-owner (structural, corroborating): the
  ONLY ownership/chown task carrying ``notify: Restart openbao`` is the
  post-include one (PASSES on unfixed code, documents the defect).
* 3 — Live-repro stub (``requires_infra``, deselected by default): documents the
  ``docker events`` recreate-tail reproduction + the pre-owned-volume fix proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_unsealer_volume_ownership_ordering.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENBAO_INSTALL_MAIN = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_install" / "tasks" / "main.yml"
)

# The openbao user the container runs as (openbao_install/defaults/main.yml).
_OPENBAO_UID = "openbao_container_uid"
_OPENBAO_GID = "openbao_container_gid"
_OPENBAO_UID_LITERAL = "100"
_OPENBAO_GID_LITERAL = "1000"


# --------------------------------------------------------------------------- #
# Helpers — same PyYAML idiom as test_unsealer_reseal_ordering.py. COPIED
# locally per the spec (no cross-test-tree imports).
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


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_argv_text(task: dict) -> str:
    """Return a flat string of a task's module spec (recursively), for text scans.

    Broad by design: we want to detect whether a `file`/`docker_volume` task
    references the openbao uid/gid or a role data-volume name, so we flatten the
    module args of the task into one searchable string. It never embeds secrets
    (ownership/volume tasks carry none).
    """
    parts: list[str] = []
    for mod in _task_module_names(task):
        spec = task.get(mod)
        parts.append(_flatten(spec))
    # Also fold in vars / loop so a data-volume name referenced there is seen.
    for key in ("vars", "loop", "loop_control"):
        if key in task:
            parts.append(_flatten(task[key]))
    return " ".join(p for p in parts if p)


def _flatten(node) -> str:
    """Recursively flatten a YAML node (dict/list/scalar) into one string."""
    if node is None:
        return ""
    if isinstance(node, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in node.items())
    if isinstance(node, (list, tuple)):
        return " ".join(_flatten(v) for v in node)
    return str(node)


# --------------------------------------------------------------------------- #
# Node classifiers.
# --------------------------------------------------------------------------- #
def _is_compose_app_include(task: dict) -> bool:
    """True if the task is an include_role/import_role of `docker-compose-app`.

    Covers `ansible.builtin.include_role`, bare `include_role`, and the import
    variants, matching name == 'docker-compose-app'.
    """
    for inc_key in (
        "ansible.builtin.include_role", "include_role",
        "ansible.builtin.import_role", "import_role",
    ):
        spec = task.get(inc_key)
        name = None
        if isinstance(spec, dict):
            name = spec.get("name")
        elif isinstance(spec, str):
            name = spec
        if name is not None and str(name).strip() == "docker-compose-app":
            return True
    return False


def _is_file_module(task: dict) -> bool:
    mods = _task_module_names(task)
    return "ansible.builtin.file" in mods or "file" in mods


def _is_docker_volume_create(task: dict) -> bool:
    """A `community.docker.docker_volume` (create-if-absent) pre-create task."""
    mods = _task_module_names(task)
    if not ("community.docker.docker_volume" in mods or "docker_volume" in mods):
        return False
    spec = task.get("community.docker.docker_volume", task.get("docker_volume"))
    # A create task is state: present (or unspecified, which defaults to present)
    # — NOT state: absent.
    if isinstance(spec, dict):
        state = str(spec.get("state", "present")).strip().lower()
        return state != "absent"
    return True


def _references_openbao_ownership(task: dict) -> bool:
    """True if a `file` task chowns to the openbao uid/gid (owner/group)."""
    spec = task.get("ansible.builtin.file", task.get("file"))
    if not isinstance(spec, dict):
        return False
    owner = str(spec.get("owner", ""))
    group = str(spec.get("group", ""))
    combined = owner + " " + group
    owner_hit = _OPENBAO_UID in combined or _OPENBAO_UID_LITERAL in {owner, group}
    group_hit = _OPENBAO_GID in combined or _OPENBAO_GID_LITERAL in {owner, group}
    return owner_hit or group_hit


def _looks_like_volume_mountpoint_chown(task: dict) -> bool:
    """True if a `file` chown clearly targets a resolved volume MOUNTPOINT.

    A volume-mountpoint chown references a docker_volume_info Mountpoint (the
    post-up idiom is `path: "{{ item.volume.Mountpoint }}"` /
    `path: "{{ item.stat.path }}"`), or otherwise references a role data/audit
    volume by name. This is deliberately permissive so a slightly different but
    genuinely-a-volume-chown fix still matches; it must, however, still be a
    chown to the openbao uid/gid (checked by the caller).
    """
    text = _task_argv_text(task).lower()
    return (
        "mountpoint" in text
        or "openbao_data_volume" in text
        or "openbao_audit_volume" in text
        or "volume" in text
    )


def _notifies_restart_openbao(task: dict) -> bool:
    notify = task.get("notify", "")
    if isinstance(notify, (list, tuple)):
        return any("Restart openbao" in str(n) for n in notify)
    return "Restart openbao" in str(notify)


def _is_ownership_or_chown_task(task: dict) -> bool:
    """True if the task establishes volume ownership (a chown or docker_volume)."""
    if _is_docker_volume_create(task):
        return True
    if _is_file_module(task) and _references_openbao_ownership(task):
        return True
    return False


def _first_compose_app_include_index(tasks: list[dict]) -> int | None:
    for i, task in enumerate(tasks):
        if _is_compose_app_include(task):
            return i
    return None


# --------------------------------------------------------------------------- #
# Test case 1 — Pre-up-ownership-precedes-reconcile (PRIMARY, structural).
# Validates: Requirements 1.1, 1.2 (the fixed structural invariant).
# EXPECTED on unfixed code: FAILS (no pre-up ownership task before the include).
# --------------------------------------------------------------------------- #
def test_preup_volume_ownership_precedes_first_compose_app_include():
    """Bug condition (Property 1) — Validates: Requirements 1.1, 1.2.

    ``openbao_install/tasks/main.yml`` MUST contain a PRE-UP volume-ownership
    task — an ``ansible.builtin.file`` chown of a resolved volume mountpoint to
    the openbao uid/gid (``openbao_container_uid``/``openbao_container_gid``,
    100/1000), and/or a ``community.docker.docker_volume`` pre-create of the
    role's data volume — ordered BEFORE the FIRST
    ``ansible.builtin.include_role: docker-compose-app`` node, so the container's
    first start is already writable and the existing post-up
    "Chown … when they differ" task finds ownership already correct
    (``changed=0``) and does NOT ``notify: Restart openbao``.

    On UNFIXED code there is NO such pre-up ownership task before the reconcile
    (the only ownership is the POST-up chown), so this assertion FAILS —
    confirming the ordering defect.
    """
    tasks = _load_task_file(_OPENBAO_INSTALL_MAIN)

    first_include_idx = _first_compose_app_include_index(tasks)
    assert first_include_idx is not None, (
        "No `include_role: docker-compose-app` node found in "
        "openbao_install/tasks/main.yml — cannot anchor the ordering check."
    )

    # Scan STRICTLY the tasks BEFORE the first docker-compose-app include for a
    # pre-up ownership task. Anchoring on task-index-before-first-include is what
    # ensures we do NOT accidentally match the POST-up chown (which is AFTER the
    # include).
    preup_ownership_idx = None
    for i in range(first_include_idx):
        task = tasks[i]
        if _is_docker_volume_create(task):
            preup_ownership_idx = i
            break
        if (
            _is_file_module(task)
            and _references_openbao_ownership(task)
            and _looks_like_volume_mountpoint_chown(task)
        ):
            preup_ownership_idx = i
            break

    assert preup_ownership_idx is not None, (
        "COUNTEREXAMPLE (bug): no pre-up volume-ownership task (chown to the "
        "openbao uid/gid, or a community.docker.docker_volume pre-create) "
        "precedes the first docker-compose-app include in "
        "openbao_install/tasks/main.yml (first include at index "
        f"{first_include_idx}); the only ownership task is the post-up "
        "'Chown … when they differ' which notify: Restart openbao -> end-of-play "
        "recreate -> sealed unsealer. The fix must add a pre-first-start "
        "ownership step BEFORE the docker-compose-app reconcile."
    )
    assert preup_ownership_idx < first_include_idx, (
        "The pre-up volume-ownership task must be ordered BEFORE the first "
        f"docker-compose-app include (ownership index={preup_ownership_idx}, "
        f"first include index={first_include_idx})."
    )


# --------------------------------------------------------------------------- #
# Test case 2 — Post-up-chown-is-the-only-notify-owner (corroborating).
# Validates: Requirements 1.1, 1.3 (documents the defect precisely).
# EXPECTED on unfixed code: PASSES (descriptive of the current defect).
# --------------------------------------------------------------------------- #
def test_only_ownership_task_that_notifies_restart_is_postup():
    """Corroborating (documents the defect) — Validates: Requirements 1.1, 1.3.

    On the tree, the ONLY ownership/chown task carrying ``notify: Restart
    openbao`` MUST be the POST-include one (ordered AFTER the first
    ``docker-compose-app`` include). This documents that the sole trigger of the
    end-of-play recreate is the post-up chown. It PASSES on unfixed code (there
    is exactly one such task, after the include) and continues to PASS after the
    fix (the fix adds a pre-up ownership task that does NOT notify).
    """
    tasks = _load_task_file(_OPENBAO_INSTALL_MAIN)

    first_include_idx = _first_compose_app_include_index(tasks)
    assert first_include_idx is not None, (
        "No `include_role: docker-compose-app` node found — cannot anchor."
    )

    notifying_owner_indices = [
        i
        for i, task in enumerate(tasks)
        if _is_ownership_or_chown_task(task) and _notifies_restart_openbao(task)
    ]

    assert notifying_owner_indices, (
        "Expected at least one ownership/chown task carrying "
        "`notify: Restart openbao` (the post-up 'Chown … when they differ') — "
        "none found; the baseline defect structure changed unexpectedly."
    )
    # Every ownership task that notifies a restart must be AFTER the first
    # compose-app include (i.e. it is the post-up chown, never a pre-up one).
    for idx in notifying_owner_indices:
        assert idx > first_include_idx, (
            "An ownership/chown task carrying `notify: Restart openbao` is "
            f"ordered at/before the first docker-compose-app include (task "
            f"index={idx}, first include index={first_include_idx}). A pre-up "
            "ownership step must NOT notify a restart — only the post-up "
            "safety-net chown may."
        )


# --------------------------------------------------------------------------- #
# Test case 3 — Live-repro stub (requires_infra; deselected by default).
# Validates: Requirements 1.1, 1.2 behaviourally (the live acceptance).
# --------------------------------------------------------------------------- #
@pytest.mark.requires_infra
def test_live_from_scratch_unsealer_no_recreate_tail_and_ends_unsealed():
    """Live from-scratch reproduction (requires_infra — deselected by default).

    DOCUMENTED live gate; not executed offline. On a throwaway cluster, a
    from-scratch Developer-B bring-up of SVC-07:

    UNFIXED code (reproduces the bug):
      * ``docker events`` on the unsealer guest ends the bootstrap exec sequence
        (``operator init`` -> ``operator unseal`` x3 -> ``secrets enable transit``
        -> ``write transit/keys/...`` -> ``policy write`` -> ``token create`` ->
        ``token revoke``) and is IMMEDIATELY followed by
        ``kill`` / ``stop`` / ``die`` / ``destroy`` / ``rename`` / ``start`` —
        the Compose ``recreate: always`` destroying the just-bootstrapped
        container and starting a fresh SEALED one. (Events referred to by ACTION
        NAME only; no unseal-key strings from the raw output are reproduced.)
      * The unsealer (``svc07-unsealer-20-01`` / ``10.0.20.11``) ends the run
        ``Initialized: true, Sealed: true, Unseal Progress 0/3``,
        ``RestartCount=0`` (a recreate is a new container, not a restart).
      * The primary (``10.0.20.10``) crash-loops on ``503 Vault is sealed``.

    FIXED code (the acceptance):
      * Pre-owning the volume mountpoint to 100:1000 BEFORE first start makes the
        post-up "Chown … when they differ" task a no-op (owner already correct →
        it does NOT notify): the ``docker events`` recreate tail is ABSENT after
        the bootstrap exec sequence, the unsealer ends ``Sealed: false`` with the
        SAME container ``Id``, and the primary auto-unseals and reports healthy.

    This stub is marked ``@pytest.mark.requires_infra`` and is therefore
    DESELECTED by default via the root ``pytest.ini`` (``addopts = -m "not
    requires_infra"``). The authoritative behavioural evidence is captured in the
    spec's validation.md from a real throwaway-cluster run; it must never echo
    any secret value (reference ``bao status`` fields only).
    """
    pytest.skip(
        "requires-infra: live from-scratch SVC-07 bring-up on a throwaway cluster "
        "(no docker-events recreate tail after the unsealer bootstrap; unsealer "
        "ends Sealed:false, same container Id; primary healthy). Documented live "
        "gate; run under -m requires_infra with a wired-up cluster. See "
        "validation.md."
    )

# ===========================================================================
# ===========================================================================
# TASK 2 — PRESERVATION property tests (Property 2).
# ===========================================================================
# ===========================================================================
#
# These are PRESERVATION guards (design.md "Preservation Checking", Property 2).
# UNLIKE the bug-condition case above
# (``test_preup_volume_ownership_precedes_first_compose_app_include`` — which
# FAILS on unfixed code and only PASSES once the fix lands), the cases below
# encode the CURRENT committed baseline the volume-ownership fix must NOT
# regress and therefore **PASS on the UNFIXED code**. They pin the invariants:
#
#   P2-1 Safety-net preserved — the post-up "Chown volume mountpoints … when
#        they differ" task still exists in openbao_install/tasks/main.yml AND
#        still carries ``notify: Restart openbao``, ordered AFTER the first
#        docker-compose-app include (Req 3.1). This is the safety net the fix
#        must KEEP (make a no-op), never delete.
#   P2-2 Idempotency preserved — the version-check guard fact
#        ``openbao_already_at_pinned_version`` is still referenced in main.yml,
#        and the declarative ``docker-compose-app`` reconcile include is still
#        present (Req 3.2).
#   P2-5 Two-play + deployment-unit preserved — openbao.yml has exactly two
#        plays, Play 1 ``hosts: openbao-unsealer`` and Play 2 ``hosts: openbao``
#        (unsealer first); and the ``docker-compose-app`` include remains the
#        bring-up path in openbao_install/tasks/main.yml (Req 3.4, 3.6).
#
# P2-3 (secret discipline / no_log) and P2-4 (bug #9 flush_handlers seam +
# re-unseal guard) are ALREADY fully asserted by
# ``tests/installer/test_unsealer_reseal_ordering.py`` — which runs in the same
# installer suite (P2-4 by that file's flush-seam + re-unseal-guard cases; P2-3
# by its ``test_preserve_existing_secret_bearing_tasks_set_no_log``). Per the
# spec, this file does NOT duplicate those assertions (matching how bug #10's
# test file handled cross-coverage); it relies on that file staying green
# (Task 3.4 / Task 4 confirm). Duplicating them here would be low-value.
#
# These preservation cases reuse the Task-1 helpers (``_load_task_file``,
# ``_iter_tasks``, ``_task_module_names``, ``_is_compose_app_include``,
# ``_is_file_module``, ``_references_openbao_ownership``,
# ``_notifies_restart_openbao``, ``_first_compose_app_include_index``) — no
# duplicate helpers are introduced. A local ``_load_plays`` helper (the same
# PyYAML idiom the reseal-ordering file uses) is added below because the Task-1
# section had no playbook parser (it only parses the role tasks file).
#
# Parse-only: these tests read YAML structure; they never embed or print any
# secret value.


def _load_plays(path: Path):
    """Return the list of play dicts from an Ansible playbook.

    An Ansible playbook is a single YAML document whose top-level node is a LIST
    of plays; ``safe_load_all`` yields that one list. Flatten any list documents
    and keep the play dicts. (Same idiom as
    ``tests/installer/test_unsealer_reseal_ordering.py``; copied locally per the
    spec — no cross-test-tree imports.)
    """
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


# --------------------------------------------------------------------------- #
# P2-1 — Safety-net preserved (openbao_install/tasks/main.yml).
# Validates: Requirement 3.1 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_postup_chown_safety_net_still_notifies_restart():
    """Preservation (Property 2) — Validates: Requirement 3.1.

    ``openbao_install/tasks/main.yml`` MUST still contain the post-up "Chown
    volume mountpoints … when they differ" task — a ``file`` chown to the
    openbao uid/gid — that still carries ``notify: Restart openbao`` AND is
    ordered AFTER the first ``docker-compose-app`` include. This is the
    idempotent safety net (a root-owned volume crash-loops OpenBao on
    ``/openbao/data/vault.db: permission denied``); the fix makes it a
    ``changed=0`` no-op on the happy path but MUST NOT delete it.

    This baseline holds on the UNFIXED code (the safety-net chown exists,
    notifies, and is post-include) and must survive the fix.
    """
    tasks = _load_task_file(_OPENBAO_INSTALL_MAIN)

    first_include_idx = _first_compose_app_include_index(tasks)
    assert first_include_idx is not None, (
        "No `include_role: docker-compose-app` node found in "
        "openbao_install/tasks/main.yml — cannot anchor the ordering check."
    )

    # The safety-net chown: a `file` task chowning to the openbao uid/gid that
    # carries `notify: Restart openbao`, ordered AFTER the first compose-app
    # include (it is the post-up chown, never a pre-up one).
    postup_chown_indices = [
        i
        for i, task in enumerate(tasks)
        if _is_file_module(task)
        and _references_openbao_ownership(task)
        and _notifies_restart_openbao(task)
    ]

    assert postup_chown_indices, (
        "PRESERVATION: the post-up 'Chown volume mountpoints … when they differ' "
        "safety-net task (a `file` chown to the openbao uid/gid carrying "
        "`notify: Restart openbao`) must still exist in "
        "openbao_install/tasks/main.yml (Req 3.1) — it is the safety net the fix "
        "must KEEP, not delete."
    )
    # Every such notifying chown must be AFTER the first compose-app include (it
    # is the post-up safety net). On unfixed code there is exactly one, post-up.
    for idx in postup_chown_indices:
        assert idx > first_include_idx, (
            "PRESERVATION: the safety-net chown carrying `notify: Restart "
            f"openbao` must remain ordered AFTER the first docker-compose-app "
            f"include (chown index={idx}, first include index={first_include_idx})."
        )


# --------------------------------------------------------------------------- #
# P2-2 — Idempotency preserved (openbao_install/tasks/main.yml).
# Validates: Requirement 3.2 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_version_check_guard_and_declarative_reconcile():
    """Preservation (Property 2) — Validates: Requirement 3.2.

    ``openbao_install/tasks/main.yml`` MUST still reference the version-check
    idempotency guard fact ``openbao_already_at_pinned_version`` AND still
    contain a ``docker-compose-app`` include (the declarative reconcile). Both
    are what make a re-run against a converged host a ``changed=0`` no-op with no
    spurious recreate; the volume-ownership fix must not remove either.

    Light structural check per the spec: assert the guard-fact string is present
    in the file and that at least one ``docker-compose-app`` include exists. This
    baseline holds on the UNFIXED code.
    """
    # Guard-fact reference: a raw-text presence check is the "light structural
    # check" the spec asks for (the fact is set and consumed via `when:` /
    # Jinja, so it appears as a bare string in the YAML).
    raw = _OPENBAO_INSTALL_MAIN.read_text(encoding="utf-8")
    assert "openbao_already_at_pinned_version" in raw, (
        "PRESERVATION: the version-check idempotency guard fact "
        "`openbao_already_at_pinned_version` must still be referenced in "
        "openbao_install/tasks/main.yml (Req 3.2) — the fix must not remove it."
    )

    # Declarative reconcile: at least one docker-compose-app include must remain
    # the bring-up path.
    tasks = _load_task_file(_OPENBAO_INSTALL_MAIN)
    compose_includes = [t for t in tasks if _is_compose_app_include(t)]
    assert compose_includes, (
        "PRESERVATION: at least one `include_role: docker-compose-app` "
        "(the declarative reconcile) must remain in "
        "openbao_install/tasks/main.yml (Req 3.2) — the fix reorders when "
        "ownership is established relative to this include, never removes it."
    )


# --------------------------------------------------------------------------- #
# P2-5 — Two-play + deployment-unit preserved (openbao.yml + main.yml).
# Validates: Requirements 3.4, 3.6 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_two_ordered_plays_unsealer_first_and_deployment_unit():
    """Preservation (Property 2) — Validates: Requirements 3.4, 3.6.

    ``ansible/playbooks/openbao.yml`` MUST have exactly two plays — Play 1
    ``hosts: openbao-unsealer`` and Play 2 ``hosts: openbao`` (unsealer first) —
    and the ``docker-compose-app`` include MUST remain the bring-up path in
    ``openbao_install/tasks/main.yml``. The volume-ownership fix reorders when
    ownership is established relative to that same include; it must not add,
    remove, reorder, or retarget the plays, nor change the deployment unit.

    This baseline holds on the UNFIXED code.
    """
    openbao_playbook = _REPO_ROOT / "ansible" / "playbooks" / "openbao.yml"
    plays = _load_plays(openbao_playbook)
    assert len(plays) == 2, (
        f"PRESERVATION: openbao.yml must have exactly two ordered plays; "
        f"found {len(plays)}."
    )
    assert plays[0].get("hosts") == "openbao-unsealer", (
        "PRESERVATION: Play 1 must target hosts: openbao-unsealer (unsealer "
        f"first) — got {plays[0].get('hosts')!r}."
    )
    assert plays[1].get("hosts") == "openbao", (
        "PRESERVATION: Play 2 must target hosts: openbao (primary second) — got "
        f"{plays[1].get('hosts')!r}."
    )

    # Deployment unit: docker-compose-app remains the bring-up path.
    tasks = _load_task_file(_OPENBAO_INSTALL_MAIN)
    assert any(_is_compose_app_include(t) for t in tasks), (
        "PRESERVATION: the `docker-compose-app` include must remain the bring-up "
        "path in openbao_install/tasks/main.yml (Req 3.6) — the sole sanctioned "
        "deployment unit; the fix must not replace it."
    )
