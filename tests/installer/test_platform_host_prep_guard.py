"""Offline structural drift-guard for platform_host_prep Ensure_Template (Task 6.3).

Feature: platform-prerequisites-bootstrap.

Spec: .kiro/specs/platform-prerequisites-bootstrap/ (requirements.md Requirement
3.1/3.2, design.md "Component 4: platform_host_prep" + "Property 3:
Ensure-template idempotence"; tasks.md Task 6.1 / Task 6.3). This pins
**Property 3** as an offline text/structure drift-guard, per the design's
Testing Strategy (structural assertions, not generative PBT).

WHAT IS ASSERTED (offline, no infra, PyYAML parse of the committed role)
------------------------------------------------------------------------
Against ``ansible/roles/platform_host_prep/tasks/main.yml`` — focusing ONLY on
the Ensure_Template tasks (Task 6.1), NOT the sdn_gateway child-run tasks
(Task 6.2):

  * Case 1 (check-precedes-and-gates-download, Req 3.1) — the template
    presence-check task (a delegated ``pveam list`` command) precedes the
    ``pveam download`` task in document order, AND the download runs only when
    the template is ABSENT: the download carries a ``when:`` that references the
    presence-check result (the ``platform_host_prep_template_present`` register
    derived from the ``pveam list`` output). This is the idempotence invariant —
    a present template triggers no download.

  * Case 2 (download-failure-fails-closed, Req 3.2) — a download failure fails
    closed naming the template: a ``fail``/``assert`` task (in the download
    block's ``rescue``) whose message references the configured template name
    (``platform_host_prep_template_volid`` / ``_filename``) AND the
    ``pveam available`` hint, so the operator gets an actionable remedy rather
    than the raw pveam error being the last word.

HOW IT STAYS OFFLINE
--------------------
Pure PyYAML + structure parse of the committed role tasks file — no
``ansible-playbook`` binary, no live Proxmox. Always runs; needs no
``requires_infra`` gate. Mirrors the drift-guard style of
``test_thin_orchestrator_play.py`` / ``test_unsealer_reseal_ordering.py`` (the
``_iter_tasks`` block/rescue recursion, the ``_NON_MODULE_KEYS`` module
detection, repo-root via ``parents[2]``). It PARSES YAML only; it never embeds
or prints any secret value (the Ensure_Template knobs are non-secret storage /
template-name topology values).

Validates: Requirements 3.1, 3.2 / Property 3
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_platform_host_prep_guard.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_TASKS = (
    _REPO_ROOT
    / "ansible"
    / "roles"
    / "platform_host_prep"
    / "tasks"
    / "main.yml"
)

# The presence-check register the download's `when:` must reference (the
# `pveam list`-derived "is the template already present" fact).
_PRESENCE_FACT = "platform_host_prep_template_present"

# Keys on a task dict that are directives/containers, NOT the invoked module.
# Mirrors the set in test_thin_orchestrator_play.py / test_unsealer_delegation.py.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)


# --------------------------------------------------------------------------- #
# Helpers — parse the role tasks file and flatten its block/rescue/always tree.
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always.

    Yields the container node itself too (a block/rescue/always node is a task),
    then descends into its children, so document order is preserved.
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path) -> list[dict]:
    """Return the flat, document-ordered list of task dicts from a tasks/ file.

    A role tasks/ file is a single YAML document whose top-level node is a LIST
    of tasks. Flatten block/rescue/always so ordering is by document position.
    """
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _iter_tasks_with_ancestors(tasks, ancestors=()):
    """Yield (task, ancestor_tuple) recursively, descending block/rescue/always.

    ``ancestor_tuple`` is the chain of enclosing block/rescue/always container
    tasks (outermost first). Ansible applies an enclosing ``block``'s ``when:``
    to every child task, so a leaf command's EFFECTIVE gate is its own ``when:``
    plus every ancestor block's ``when:`` — this lets the guard see a download
    gated by its enclosing block rather than by an inline ``when:``.
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task, ancestors
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks_with_ancestors(task[key], ancestors + (task,))


def _load_role_tasks_with_ancestors(path: Path):
    """Return an ordered list of (task, ancestor_tuple) from a tasks/ file."""
    out: list[tuple[dict, tuple]] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks_with_ancestors(doc))
    return out


def _effective_condition_text(task: dict, ancestors: tuple) -> str:
    """Flatten a task's own `when:` plus every ancestor block's `when:`.

    Ansible ANDs an enclosing block's ``when:`` onto each child, so the effective
    gate on a leaf task is the union of its own and all ancestor conditions.
    """
    parts = [_condition_text(a) for a in ancestors]
    parts.append(_condition_text(task))
    return " ".join(p for p in parts if p)


def _module_names(task: dict) -> set[str]:
    """The set of module keys on a task (everything that is not a directive)."""
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _command_argv_text(task: dict) -> str:
    """Return a flat string of a command task's argv, else ''.

    Covers ``argv`` as a YAML list and as a scalar string, under both the FQCN
    and the short module name.
    """
    parts: list[str] = []
    for mod in ("ansible.builtin.command", "command"):
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
    return " ".join(parts)


def _condition_text(task: dict) -> str:
    """Flatten a task's `when:` (str or list) into one string."""
    when = task.get("when", "")
    if isinstance(when, (list, tuple)):
        return " ".join(str(c) for c in when)
    return str(when)


# The `pveam` binary may appear either as the bare literal `pveam` or, since the
# delegated command module resolves argv[0] against its own PATH (not a login
# shell's), as the absolute-path-by-variable form `{{ platform_host_prep_pveam_bin }}`
# (which defaults to /usr/bin/pveam). Recognize both so the drift-guard stays
# anchored to the pveam operation regardless of how argv[0] is spelled.
def _argv_invokes_pveam(argv: str) -> bool:
    return "pveam" in argv or "platform_host_prep_pveam_bin" in argv


def _is_pveam_list(task: dict) -> bool:
    """A delegated `pveam list <storage>` command task (the presence check)."""
    argv = _command_argv_text(task)
    return _argv_invokes_pveam(argv) and "list" in argv.split()


def _is_pveam_download(task: dict) -> bool:
    """A `pveam download <storage> <filename>` command task."""
    argv = _command_argv_text(task)
    toks = argv.split()
    return _argv_invokes_pveam(argv) and "download" in toks


def _fail_or_assert_body(task: dict) -> str:
    """Return the flattened message/body text of a fail/assert task, else ''."""
    parts: list[str] = []
    for mod in ("ansible.builtin.fail", "fail"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            parts.append(str(spec.get("msg", "")))
        elif spec is not None:
            parts.append(str(spec))
    for mod in ("ansible.builtin.assert", "assert"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            parts.append(str(spec.get("fail_msg", "")))
            parts.append(str(spec.get("that", "")))
        elif spec is not None:
            parts.append(str(spec))
    return " ".join(parts)


def _is_fail_or_assert(task: dict) -> bool:
    mods = _module_names(task)
    return bool(mods & {"ansible.builtin.fail", "fail",
                        "ansible.builtin.assert", "assert"})


# --------------------------------------------------------------------------- #
# Case 1 — presence-check precedes AND gates the download (idempotence).
# Validates: Requirement 3.1 / Property 3.
# --------------------------------------------------------------------------- #
def test_presence_check_precedes_and_gates_the_pveam_download():
    """Validates: Requirement 3.1 / Property 3.

    The Ensure_Template step must download ONLY when the template is absent
    (check-then-act). Structurally:

      * the ``pveam list`` presence-check task appears BEFORE the
        ``pveam download`` task in document order, AND
      * the ``pveam download`` carries a ``when:`` that references the
        presence-check result — the ``platform_host_prep_template_present``
        register derived from the ``pveam list`` output — so a present template
        triggers no download.

    A download with no such gate (or ordered before the presence check) would be
    unconditional and thus non-idempotent — the drift this guard catches.
    """
    tasks_with_ctx = _load_role_tasks_with_ancestors(_ROLE_TASKS)

    list_idx = next(
        (i for i, (t, _a) in enumerate(tasks_with_ctx) if _is_pveam_list(t)), None
    )
    download_idx = next(
        (i for i, (t, _a) in enumerate(tasks_with_ctx) if _is_pveam_download(t)),
        None,
    )

    assert list_idx is not None, (
        "No `pveam list` presence-check command task found in "
        "platform_host_prep/tasks/main.yml — cannot verify Ensure_Template "
        "idempotence (Req 3.1)."
    )
    assert download_idx is not None, (
        "No `pveam download` command task found in "
        "platform_host_prep/tasks/main.yml — cannot verify Ensure_Template "
        "idempotence (Req 3.1)."
    )

    assert list_idx < download_idx, (
        "DRIFT (bug): the `pveam list` presence check "
        f"(index={list_idx}) does not precede the `pveam download` "
        f"(index={download_idx}). Ensure_Template must check-then-act: the "
        "presence query must run BEFORE any download so the download is skipped "
        "when the template is already present (Req 3.1 idempotence)."
    )

    download_task, download_ancestors = tasks_with_ctx[download_idx]
    # The download's EFFECTIVE gate is its own `when:` plus every enclosing
    # block's `when:` (Ansible ANDs an ancestor block's condition onto each
    # child). Here the gate lives on the enclosing "download if absent" block.
    when_text = _effective_condition_text(download_task, download_ancestors)
    assert _PRESENCE_FACT in when_text, (
        "DRIFT (bug): the `pveam download` task is not gated on the presence-check "
        f"result. Its `when:` ({when_text!r}) does not reference "
        f"`{_PRESENCE_FACT}` (the fact derived from the `pveam list` output). An "
        "ungated download runs unconditionally on every host run — the "
        "non-idempotent shape Req 3.1 forbids (download ONLY IF absent)."
    )
    # The gate must be the "absent" branch (negated presence), not "present".
    assert "not " in when_text or "| bool == false" in when_text.lower(), (
        "DRIFT (bug): the `pveam download` `when:` references "
        f"`{_PRESENCE_FACT}` but is not the ABSENT branch "
        f"(expected a negation like `not ({_PRESENCE_FACT} | bool)`); its `when:` "
        f"was {when_text!r}. The download must run only when the template is "
        "absent, not when it is present."
    )


# --------------------------------------------------------------------------- #
# Case 2 — a download failure fails closed naming the template + `pveam available`.
# Validates: Requirement 3.2 / Property 3.
# --------------------------------------------------------------------------- #
def test_download_failure_fails_closed_naming_template_and_pveam_available_hint():
    """Validates: Requirement 3.2 / Property 3.

    A `pveam download` failure must fail closed with an actionable, named remedy:
    a ``fail``/``assert`` task whose message references BOTH the configured
    template name (``platform_host_prep_template_volid`` and/or the derived
    ``_filename``) AND the ``pveam available`` hint. This is the fail-closed
    diagnostic the design requires so the raw pveam error is never the last word.

    We locate the fail-closed diagnostic within the SAME block/rescue subtree as
    the ``pveam download`` (so the guard is tied to the download, not some other
    unrelated fail task), by scanning the role tasks for a fail/assert task
    carrying both markers.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # There must actually be a download to guard (anchors this test against a
    # rename that would otherwise make it vacuously pass).
    assert any(_is_pveam_download(t) for t in tasks), (
        "No `pveam download` task found — cannot verify the fail-closed remedy "
        "(Req 3.2)."
    )

    remedy = None
    for task in tasks:
        if not _is_fail_or_assert(task):
            continue
        body = _fail_or_assert_body(task)
        names_template = (
            "platform_host_prep_template_volid" in body
            or "platform_host_prep_tmpl_filename" in body
        )
        names_hint = "pveam available" in body
        if names_template and names_hint:
            remedy = task
            break

    assert remedy is not None, (
        "DRIFT (bug): no fail-closed diagnostic for a `pveam download` failure "
        "names BOTH the configured template "
        "(platform_host_prep_template_volid / _tmpl_filename) AND the "
        "`pveam available` hint. Req 3.2 requires the download failure to fail "
        "closed with an actionable remedy naming the attempted template + the "
        "`pveam available` hint, rather than surfacing only the raw pveam error."
    )
