"""Offline META-drift-guard: every destructive-live orchestrator consults the guard.

Feature: agent-live-test-authorization.

Spec: .kiro/specs/agent-live-test-authorization/ (design.md "Component 4" — the
SVC-07 reference wiring — and its "Reference-consumer only" Open-Risk note;
requirements.md Requirement 5, esp. **Req 5.6**: "THE reference wiring (SVC-07)
SHALL be documented so future service installers adopt the same
guard-consultation pattern uniformly").

WHY THIS FILE EXISTS (the risk it closes)
-----------------------------------------
``test_agent_live_wiring.py`` (Properties 5/6) pins the guard-consult wiring in
the TWO roles that exist TODAY (``svc07_clean_slate`` + ``svc07_preflight``). But
the design's own Open-Risk register names the residual gap: *"the risk is a
future service forgetting to wire it — mitigated by documenting the pattern ...
so adoption is copy-the-three-tasks."* A hand-maintained list of "roles that must
carry the guard" would rot the moment a new service is added.

This is a TRUE drift-guard (not a hand-maintained list): it SCANS every Ansible
role tasks file and, for each one that is itself a **destructive-live orchestrator
entry point** (it runs the child clean-slate playbook, ``pct destroy``, or a
mutating/destroying ``terraform`` command), ASSERTS that the SAME file also
carries the agent-live throwaway-guard consult (the "copy-the-three-tasks"
pattern). So a FUTURE service that adds a destructive orchestrator without the
guard fails THIS test at commit time — enforcing Req 5.6 mechanically rather than
by prose alone.

"is a destructive orchestrator" and "has the guard consult" are TWO INDEPENDENT
scans over the same file. ``svc07_clean_slate`` is BOTH — it has the guard
consult early (three gated tasks) and the ``clean-slate.yml`` marker later — which
is exactly the PASS case.

The helpers below are COPIED locally (NOT imported across test trees — the
conftest-collision rule), mirroring the idiom of the sibling
``test_agent_live_wiring.py`` (PyYAML ``safe_load_all``, the ``_iter_tasks``
block/rescue recursion, ``_NON_MODULE_KEYS`` module detection, repo-root via
``Path(__file__).resolve().parents[2]``, ``_task_argv_text`` / ``_when_text`` /
``_module_names`` / ``_is_assert`` / ``_assert_that_text`` and the guard-consult
detectors ``_references_guard`` / ``_is_guard_consult`` / ``_is_rc_assert``).

Validates: Requirement 5.6 / drift-guard intent (design "Component 4" +
"Reference-consumer only" open risk).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# tests/installer/test_agent_live_wiring_coverage.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_ROLE_TASKS_GLOB = "ansible/roles/*/tasks/*.yml"

# CROSS-SERVICE EXTENSION (platform-prerequisites-bootstrap spec, task 10):
# the meta-guard originally scanned ONLY role tasks files. That misses a
# destructive orchestrator that lives as a PLAYBOOK and drives its mutations via
# `import_role` (e.g. ansible/playbooks/platform-bootstrap.yml imports
# platform_api, whose `terraform apply` is the destructive marker) while carrying
# the throwaway-guard consult at the PLAYBOOK level (design.md Component 8: "wire
# the consult into both destructive buckets directly rather than leaf-exempting
# them"). So the scan is widened to also cover the orchestrator playbooks, with a
# destructive-detector that recognises a playbook that `import_role`s / `roles:`-
# lists a destructive role. This is the first CROSS-SERVICE proof the meta-guard
# generalises past SVC-07.
_PLAYBOOK_GLOB = "ansible/playbooks/*.yml"

# The one role tasks file the meta-guard MUST classify as a destructive
# orchestrator (so the guard is provably non-vacuous). svc07_preflight is
# deliberately NOT required here: it runs no destructive command itself; it is
# guard-wired as defense-in-depth for the from-scratch path, so the
# destructive-marker scan legitimately does not flag it.
_SVC07_CLEAN_SLATE_REL = "ansible/roles/svc07_clean_slate/tasks/main.yml"

# --------------------------------------------------------------------------- #
# LEAF-EXEMPT SET — downstream destroy/apply MECHANISMS reached ONLY through an
# already-guarded orchestrator (never an agent entry point in their own right).
# --------------------------------------------------------------------------- #
# terraform_clean_slate is the leaf DESTROY mechanism that svc07_clean_slate
# invokes THROUGH the child clean-slate.yml playbook. It is NOT an agent entry
# point: it never runs unless (a) an orchestrator already passed the agent-live
# guard AND (b) its own `clean_slate_enabled` flag is true AND (c) a per-target
# `clean_slate_confirm` token matches the exact vmid being destroyed. Requiring
# it to ALSO carry the agent-live guard would be redundant (the orchestrator
# above it already gates the agent path) and wrong (it has no cluster_env /
# --env-file context of its own).
#
# svc07_provision is the Phase-2 APPLY mechanism (`terraform init`/`apply`) for
# the from-scratch bring-up. Its `terraform apply` marker makes it look like a
# destructive-live orchestrator, but — exactly like terraform_clean_slate — it is
# guarded UPSTREAM: in ansible/playbooks/svc-07-bootstrap.yml the svc07_preflight
# role (Phase 1) runs BEFORE svc07_provision (Phase 2) in the SAME play, and it
# is preflight that carries the gated guard consult for the armed from-scratch
# path (design.md Component 4 (b): "an armed from-scratch run against a
# non-allowlisted identity is refused before any resource is touched"). Provision
# runs only after preflight authorized (or, on an unarmed operator run, the
# consult is skipped and provision is byte-for-byte unchanged). Provision has no
# cluster-identity/guard context distinct from preflight, so it is exempted here.
#
# platform_api is the platform-prerequisites-bootstrap api-bucket APPLY mechanism
# (`terraform apply` of the platform-foundation SDN root). Its `terraform apply`
# marker makes it look like a destructive-live orchestrator, but — exactly like
# svc07_provision — it is guarded UPSTREAM: in ansible/playbooks/platform-bootstrap.yml
# the api bucket's throwaway-guard consult (command -> INFO -> rc-assert, all
# `when: agent_live_run`) sits at the HEAD of the api block, BEFORE the
# `import_role: platform_api` call, so any `terraform apply` is refused before it
# runs on an armed non-throwaway run (design.md Component 8; Req 7.1). The consult
# lives in the PLAYBOOK (not the role), so the playbook scan below is what proves
# platform-bootstrap.yml carries it — platform_api itself has no cluster-identity/
# guard context of its own and is exempted here, mirroring svc07_provision.
#
# All exempt files are still asserted (test #3) to EXIST and to actually contain a
# destructive marker, so this allowlist cannot silently rot.
_LEAF_EXEMPT = {
    "ansible/roles/terraform_clean_slate/tasks/main.yml",
    "ansible/roles/terraform_clean_slate/tasks/reset_target.yml",
    "ansible/roles/svc07_provision/tasks/main.yml",
    "ansible/roles/platform_api/tasks/main.yml",
}

# --------------------------------------------------------------------------- #
# PLAYBOOK LEAF-EXEMPT SET — destructive-live orchestrator PLAYBOOKS whose guard
# consult legitimately lives UPSTREAM in the roles they import, not in the
# playbook file itself. These are NOT unguarded: the guarding role is itself
# role-scanned (and carries the consult) above.
# --------------------------------------------------------------------------- #
# svc-07-bootstrap.yml drives its mutations via import_role (svc07_provision's
# `terraform apply`, svc07_clean_slate's child clean-slate.yml). Its guard consult
# is NOT in the playbook — it is carried by svc07_preflight (Phase 1, the
# from-scratch guard) and svc07_clean_slate (the reset branch), BOTH of which the
# role scan already flags/checks. So the playbook is exempt: requiring the consult
# ALSO in the playbook file would duplicate what the role scan already enforces.
#
# clean-slate.yml is the CHILD destroy playbook: it `roles:`-lists
# terraform_clean_slate (the leaf `pct destroy`/`terraform` destroy mechanism,
# itself leaf-exempt above). It is never an agent entry point in its own right —
# it runs only THROUGH svc07_clean_slate (which carries the consult) — and has no
# cluster-identity/guard context of its own, so it is exempt for the same reason
# terraform_clean_slate is.
#
# platform-bootstrap.yml is deliberately NOT exempt: it carries the throwaway-
# guard consult in the PLAYBOOK (host + api buckets), so it must PASS the scan
# (design.md Component 8 — "wire the consult ... directly rather than leaf-
# exempting"). It is the reference case proving the playbook scan has teeth.
#
# Every playbook-exempt file is asserted (test #3) to EXIST and to actually be a
# destructive orchestrator, so this allowlist cannot silently rot.
_PLAYBOOK_LEAF_EXEMPT = {
    "ansible/playbooks/svc-07-bootstrap.yml",
    "ansible/playbooks/clean-slate.yml",
}


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally (no cross-tree import; conftest-collision rule).
# PyYAML safe_load_all parse only. Mirrors test_agent_live_wiring.py.
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
    """Return the flat, document-ordered task list of a role tasks/ file."""
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


# --- Guard-consult detectors (copied from test_agent_live_wiring.py) ------- #
# GENERALISED for the cross-service extension: the SVC-07 wiring templates the
# guard/allowlist paths through vars named `svc07_throwaway_guard` /
# `svc07_throwaway_allowlist`; the platform-prerequisites-bootstrap wiring uses
# `platform_throwaway_guard` / `platform_throwaway_allowlist`. Rather than hard-
# code each service's var names, match the literal script/allowlist filename OR
# ANY `*throwaway_guard` / `*throwaway_allowlist` var reference — so a future
# service's consult (whatever its var prefix) is recognised uniformly.
def _references_guard(argv: str) -> bool:
    return "throwaway_guard.py" in argv or bool(
        re.search(r"throwaway_guard\b", argv)
    )


def _references_allowlist(argv: str) -> bool:
    return "throwaway-clusters.yml" in argv or bool(
        re.search(r"throwaway_allowlist\b", argv)
    )


def _is_guard_consult(task: dict) -> bool:
    """The guard-consult command task: argv references the guard + its flags."""
    argv = _task_argv_text(task)
    if not _references_guard(argv):
        return False
    return (
        "--service" in argv
        and "--allowlist" in argv
        and _references_allowlist(argv)
    )


def _is_rc_assert(task: dict) -> bool:
    """The rc gate: an ``assert`` on a ``*throwaway_decision.rc`` register var.

    GENERALISED for the cross-service extension: SVC-07 registers the guard
    decision as ``svc07_throwaway_decision`` and asserts on its ``.rc``; the
    platform playbook registers ``platform_host_throwaway_decision`` /
    ``platform_api_throwaway_decision``. Match any ``*throwaway_decision.rc`` so
    the rc gate is recognised regardless of the service's register-var prefix.
    """
    if not _is_assert(task):
        return False
    that = _assert_that_text(task)
    return bool(re.search(r"throwaway_decision\.rc", that))


# --- Destructive-marker detectors ------------------------------------------ #
# A "destructive marker" is one of:
#   * "clean-slate.yml" — invokes the child clean-slate playbook
#   * "pct destroy"     — destroys a Proxmox guest
#   * "terraform" together with ("apply" OR "destroy") — mutates/destroys via TF
# Two flavours: a COMMAND-argv scan (used to classify a file as an ORCHESTRATOR
# entry point) and a whole-file TEXT scan (used only for the leaf-exempt
# meaningfulness assertion, so a file whose destroy is delegated via
# include_tasks still counts as "would be flagged").
def _argv_has_destructive_marker(argv: str) -> bool:
    if not argv:
        return False
    if "clean-slate.yml" in argv:
        return True
    if "pct destroy" in argv:
        return True
    if "terraform" in argv and ("apply" in argv or "destroy" in argv):
        return True
    return False


def _is_destructive_orchestrator(tasks: list[dict]) -> bool:
    """True iff ANY command/shell task's argv carries a destructive marker."""
    return any(_argv_has_destructive_marker(_task_argv_text(t)) for t in tasks)


def _text_has_destructive_marker(text: str) -> bool:
    """Whole-file text scan for a destructive marker (leaf-exempt check only)."""
    if "clean-slate.yml" in text:
        return True
    if "pct destroy" in text:
        return True
    if "terraform" in text and ("apply" in text or "destroy" in text):
        return True
    return False


def _has_guard_consult(tasks: list[dict]) -> bool:
    """The full gated guard-consult pattern is present in this file.

    Requires BOTH, each gated ``when: agent_live_run``:
      * a guard-consult command task (argv -> throwaway_guard.py + --service +
        --allowlist referencing the allowlist), AND
      * an rc-assert on ``svc07_throwaway_decision.rc``.
    """
    consult_gated = any(
        _is_guard_consult(t) and "agent_live_run" in _when_text(t) for t in tasks
    )
    rc_gated = any(
        _is_rc_assert(t) and "agent_live_run" in _when_text(t) for t in tasks
    )
    return consult_gated and rc_gated


def _iter_role_tasks_files():
    """Yield (relpath_str, abspath) for every ansible/roles/*/tasks/*.yml."""
    for path in sorted((_REPO_ROOT).glob(_ROLE_TASKS_GLOB)):
        yield path.relative_to(_REPO_ROOT).as_posix(), path


# --------------------------------------------------------------------------- #
# PLAYBOOK scan helpers (cross-service extension) — recognise a destructive
# orchestrator that lives as a PLAYBOOK and drives its mutations via import_role.
# --------------------------------------------------------------------------- #
# A playbook is a LIST of plays; each play is a dict that may carry tasks in
# `pre_tasks`/`tasks`/`post_tasks`/`handlers` and roles in a play-level `roles:`
# list. We flatten the task sections (recursing block/rescue/always via the same
# _iter_tasks) so the guard-consult detectors work unchanged on a playbook's own
# tasks, and separately collect the role names the play pulls in (both the
# play-level `roles:` list and any `import_role`/`include_role` task) so we can
# tell whether the playbook drives a DESTRUCTIVE role.
_ROLE_INCLUDE_KEYS = (
    "ansible.builtin.import_role",
    "import_role",
    "ansible.builtin.include_role",
    "include_role",
)


def _iter_playbook_tasks(play: dict):
    """Yield every task dict from a play's task sections, recursing blocks."""
    for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
        block = play.get(section)
        if isinstance(block, list):
            yield from _iter_tasks(block)


def _load_playbook_tasks(path: Path) -> list[dict]:
    """Return the flat, document-ordered own-task list of a playbook file."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(doc, list):
            continue
        for play in doc:
            if isinstance(play, dict):
                tasks.extend(_iter_playbook_tasks(play))
    return tasks


def _playbook_role_names(path: Path) -> set[str]:
    """Role names a playbook pulls in via `roles:` or import/include_role."""
    names: set[str] = set()
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(doc, list):
            continue
        for play in doc:
            if not isinstance(play, dict):
                continue
            for entry in play.get("roles") or []:
                if isinstance(entry, str):
                    names.add(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("role"), str):
                    names.add(entry["role"])
            for task in _iter_playbook_tasks(play):
                for key in _ROLE_INCLUDE_KEYS:
                    spec = task.get(key)
                    if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                        names.add(spec["name"])
    return names


def _destructive_role_names() -> set[str]:
    """The set of role NAMES whose tasks make them a destructive orchestrator.

    A role name here is the ``<name>`` in ``ansible/roles/<name>/tasks/*.yml``.
    Used to decide whether a playbook that import_roles it is itself driving a
    destructive mutation.
    """
    names: set[str] = set()
    for rel, path in _iter_role_tasks_files():
        if _is_destructive_orchestrator(_load_role_tasks(path)):
            # rel == "ansible/roles/<name>/tasks/<file>.yml"
            names.add(rel.split("/")[2])
    return names


def _is_destructive_playbook(path: Path, destructive_roles: set[str]) -> bool:
    """True iff the playbook has its own destructive argv OR drives a destructive role."""
    if _is_destructive_orchestrator(_load_playbook_tasks(path)):
        return True
    return bool(_playbook_role_names(path) & destructive_roles)


def _iter_playbook_files():
    """Yield (relpath_str, abspath) for every ansible/playbooks/*.yml."""
    for path in sorted((_REPO_ROOT).glob(_PLAYBOOK_GLOB)):
        yield path.relative_to(_REPO_ROOT).as_posix(), path


# --------------------------------------------------------------------------- #
# Test 1 — the core meta-guard.
# --------------------------------------------------------------------------- #
def test_every_destructive_orchestrator_role_has_the_guard_consult():
    """Every destructive-live ORCHESTRATOR entry point consults the guard.

    Validates: Requirement 5.6 (the reference wiring SHALL be documented so
    future service installers adopt the same guard-consultation pattern
    uniformly) — enforced MECHANICALLY here rather than by prose.

    Scans every ``ansible/roles/*/tasks/*.yml``. For each file that is a
    destructive-live orchestrator entry point (a command/shell task whose argv
    invokes ``clean-slate.yml`` / ``pct destroy`` / a mutating-or-destroying
    ``terraform`` command) and is NOT in the leaf-exempt set, asserts the SAME
    file also carries the gated guard consult (command + rc-assert, both
    ``when: agent_live_run``). A future service that adds a destructive
    orchestrator without the guard fails HERE.
    """
    offenders: list[str] = []
    checked_any = False

    for rel, path in _iter_role_tasks_files():
        if rel in _LEAF_EXEMPT:
            continue
        tasks = _load_role_tasks(path)
        if not _is_destructive_orchestrator(tasks):
            continue
        checked_any = True
        if not _has_guard_consult(tasks):
            offenders.append(rel)

    # CROSS-SERVICE EXTENSION: also scan the orchestrator PLAYBOOKS. A playbook
    # that drives a destructive role via import_role (e.g. platform-bootstrap.yml
    # -> platform_api's `terraform apply`) is a destructive orchestrator entry
    # point in its own right, and — per design.md Component 8 — carries the guard
    # consult in the PLAYBOOK. Playbooks whose consult lives upstream in imported
    # roles (svc-07-bootstrap.yml, clean-slate.yml) are playbook-leaf-exempt.
    destructive_roles = _destructive_role_names()
    for rel, path in _iter_playbook_files():
        if rel in _PLAYBOOK_LEAF_EXEMPT:
            continue
        if not _is_destructive_playbook(path, destructive_roles):
            continue
        checked_any = True
        if not _has_guard_consult(_load_playbook_tasks(path)):
            offenders.append(rel)

    assert checked_any, (
        "The meta-guard classified NO non-exempt role tasks file or playbook as "
        "a destructive-live orchestrator — the scan is vacuous. Expected at least "
        f"{_SVC07_CLEAN_SLATE_REL!r} to be flagged; the role/playbook layout may "
        "have moved or the destructive-marker set may need updating."
    )

    assert not offenders, (
        "The following destructive-live orchestrator entry point(s) do NOT "
        f"consult the throwaway guard: {offenders}. A destructive-live "
        "orchestrator entry point (role OR playbook) must consult the throwaway "
        "guard (copy the three tasks from svc07_clean_slate / svc07_preflight, "
        "or wire the consult into the destructive playbook's own tasks as "
        "platform-bootstrap.yml does) — see testing-strategy.md 'Agent Live-Test "
        "Authorization'."
    )


# --------------------------------------------------------------------------- #
# Test 2 — the meta-guard has teeth (non-vacuous).
# --------------------------------------------------------------------------- #
def test_meta_guard_is_not_vacuous():
    """The scan DID flag svc07_clean_slate as a destructive orchestrator + guarded.

    Validates: Requirement 5.6 / drift-guard intent. Guards against a silent
    no-op: asserts the destructive-marker scan classifies
    ``svc07_clean_slate/tasks/main.yml`` as a destructive orchestrator (it
    invokes the child ``clean-slate.yml``) AND that the same file passes the
    guard-consult check — so the core meta-guard above cannot pass vacuously.

    svc07_preflight is intentionally NOT required to be flagged: preflight runs
    no destructive command itself (it is guard-wired as defense-in-depth for the
    from-scratch path), so the destructive-marker scan legitimately does not
    flag it.
    """
    path = _REPO_ROOT / _SVC07_CLEAN_SLATE_REL
    assert path.is_file(), (
        f"Expected {_SVC07_CLEAN_SLATE_REL} — not found; the role layout may "
        "have moved, repoint this guard."
    )
    tasks = _load_role_tasks(path)

    assert _is_destructive_orchestrator(tasks), (
        f"{_SVC07_CLEAN_SLATE_REL} was NOT classified as a destructive "
        "orchestrator — it should be (it delegates to the child clean-slate.yml "
        "playbook). If this fails the destructive-marker scan has lost its "
        "teeth and the core meta-guard would pass vacuously."
    )
    assert _has_guard_consult(tasks), (
        f"{_SVC07_CLEAN_SLATE_REL} is a destructive orchestrator but does NOT "
        "carry the gated guard consult — the reference wiring itself regressed."
    )


# --------------------------------------------------------------------------- #
# Test 3 — the leaf-exempt allowlist is real and meaningful.
# --------------------------------------------------------------------------- #
def test_leaf_exempt_files_exist_and_would_be_flagged():
    """Each leaf-exempt file exists and actually contains a destructive marker.

    Validates: Requirement 5.6 / drift-guard intent. The exemption must not rot
    (pointing at a moved/renamed file) and must be MEANINGFUL: each exempt file
    is asserted to EXIST and to actually contain a destructive marker (via a
    whole-file text scan), proving it WOULD be flagged by the core meta-guard if
    it were not exempt. If a leaf-exempt file no longer contains a destructive
    marker, the exemption is stale and should be removed.
    """
    for rel in sorted(_LEAF_EXEMPT):
        path = _REPO_ROOT / rel
        assert path.is_file(), (
            f"Leaf-exempt path {rel} does not exist — the _LEAF_EXEMPT "
            "allowlist has rotted (the leaf destroy mechanism was moved or "
            "renamed). Repoint or remove the exemption."
        )
        text = path.read_text(encoding="utf-8")
        assert _text_has_destructive_marker(text), (
            f"Leaf-exempt path {rel} no longer contains a destructive marker "
            "(clean-slate.yml / pct destroy / terraform apply|destroy) — the "
            "exemption is no longer meaningful and should be removed, so the "
            "meta-guard's exempt set stays honest."
        )


# --------------------------------------------------------------------------- #
# Test 4 — the PLAYBOOK scan flags platform-bootstrap.yml + it is guarded.
# --------------------------------------------------------------------------- #
def test_platform_bootstrap_playbook_is_flagged_destructive_and_guarded():
    """The cross-service proof: platform-bootstrap.yml is destructive + guarded.

    Validates: platform-prerequisites-bootstrap Requirement 7.3 / Property 6 —
    the destructive buckets' guard consult satisfies the meta-drift-guard. This
    is the FIRST cross-service proof the meta-guard generalises past SVC-07:

      * platform-bootstrap.yml drives a destructive mutation via
        ``import_role: platform_api`` (whose `terraform apply` is the marker), so
        the playbook scan MUST classify it as a destructive orchestrator, AND
      * the SAME playbook carries the gated throwaway-guard consult (host + api
        buckets: command + rc-assert, both ``when: agent_live_run``) IN THE
        PLAYBOOK — so it passes WITHOUT a leaf-exemption (design.md Component 8).

    If either half fails, the playbook scan added for this feature has lost its
    teeth (the guard would pass vacuously for the platform feature) or the
    platform playbook's guard wiring regressed.
    """
    rel = "ansible/playbooks/platform-bootstrap.yml"
    path = _REPO_ROOT / rel
    assert path.is_file(), (
        f"Expected {rel} — not found; the playbook may have moved, repoint "
        "this guard."
    )
    assert rel not in _PLAYBOOK_LEAF_EXEMPT, (
        f"{rel} must NOT be playbook-leaf-exempt: it carries the guard consult "
        "directly (design.md Component 8), so it is the reference case proving "
        "the playbook scan has teeth."
    )

    destructive_roles = _destructive_role_names()
    assert "platform_api" in destructive_roles, (
        "platform_api was NOT classified as a destructive role (its "
        "`terraform apply` marker). Without it the playbook scan would not flag "
        "platform-bootstrap.yml and the guard would pass vacuously."
    )
    assert _is_destructive_playbook(path, destructive_roles), (
        f"{rel} was NOT classified as a destructive orchestrator — it should be "
        "(it import_roles platform_api, whose `terraform apply` is destructive). "
        "The playbook destructive scan has lost its teeth."
    )
    assert _has_guard_consult(_load_playbook_tasks(path)), (
        f"{rel} is a destructive orchestrator but does NOT carry the gated guard "
        "consult in the playbook — the platform host+api guard wiring (spec task "
        "9.2) regressed."
    )


# --------------------------------------------------------------------------- #
# Test 5 — the playbook leaf-exempt allowlist is real and meaningful.
# --------------------------------------------------------------------------- #
def test_playbook_leaf_exempt_files_exist_and_would_be_flagged():
    """Each playbook-leaf-exempt file exists and IS a destructive orchestrator.

    Validates: Requirement 5.6 / drift-guard intent, extended to the playbook
    scan. The playbook exemption must not rot and must be MEANINGFUL: each exempt
    playbook is asserted to EXIST and to actually be a destructive orchestrator
    (own destructive argv OR it drives a destructive role) — proving it WOULD be
    flagged by the playbook scan if it were not exempt. If an exempt playbook is
    no longer destructive, the exemption is stale and should be removed.
    """
    destructive_roles = _destructive_role_names()
    for rel in sorted(_PLAYBOOK_LEAF_EXEMPT):
        path = _REPO_ROOT / rel
        assert path.is_file(), (
            f"Playbook-leaf-exempt path {rel} does not exist — the "
            "_PLAYBOOK_LEAF_EXEMPT allowlist has rotted (the playbook was moved "
            "or renamed). Repoint or remove the exemption."
        )
        assert _is_destructive_playbook(path, destructive_roles), (
            f"Playbook-leaf-exempt path {rel} is no longer a destructive "
            "orchestrator (no destructive argv and it drives no destructive "
            "role) — the exemption is no longer meaningful and should be "
            "removed, so the meta-guard's exempt set stays honest."
        )
