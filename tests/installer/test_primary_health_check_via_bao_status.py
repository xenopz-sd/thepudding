"""Offline structural guard for the SVC-07 primary health-check reachability bugfix.

Bugfix: fix-svc07-primary-health-check-reachability.

REPOINTED for svc07-installer-simplification (Task 11)
------------------------------------------------------
The Phase-4 health gate this file guards MOVED. In the original bugfix the
two-stage ``docker exec ... bao status`` health check lived inline in the
Phase-4 block of ``ansible/playbooks/svc-07-bootstrap.yml`` and reached the guest
via a ``delegate_to: svc07-20-01`` idiom from a ``connection: local`` play. The
svc07-installer-simplification rework extracted that gate into a dedicated CHILD
configurator play, ``ansible/playbooks/svc07-health.yml``, invoked by the
``svc07_handoff`` role as
``ansible-playbook -i {{ svc07_generated_inventory }} svc07-health.yml``. The
child play runs ``hosts: openbao`` NATIVELY on the guest, so there is NO
``delegate_to`` anywhere (the HARD CONSTRAINT — Req 2.2 of the rework). The
Phase-4 completion banner moved to ``svc07_handoff/tasks/main.yml``.

The assertions below keep their ORIGINAL INTENT — the primary health check uses
``docker exec ... bao status`` (never a ``:8200`` ``/v1/sys/health`` uri poll),
is recreate-robust (Stage-1 container-running gate precedes the Stage-2
``bao status`` poll), is empty-stdout-safe, retains a generous retry deadline,
publishes no primary host port, does not touch the Admin_Token, and keeps the
completion banner — but read them from their NEW homes:
  * the two-stage health gate: ``ansible/playbooks/svc07-health.yml`` (native,
    ``hosts: openbao``, no ``delegate_to``);
  * the completion banner + onboarding command: ``svc07_handoff`` role;
  * the two-ordered-plays / deployment-unit baseline: ``openbao.yml`` /
    ``openbao_install`` (UNCHANGED by this rework).

The obsolete delegate_to / orchestrator-localhost-scope regression guards from
the bugfix era are ADAPTED to the native model: the "no :8200 uri" and
"recreate-robust two-stage gate" invariants still hold and are asserted against
``svc07-health.yml``; the "orchestrator-scope var" and "empty-stdout `| first`"
guards are re-expressed against the child play's ``svc07_*`` health vars and its
empty-safe ``until`` gates (which the new play already implements via
``length > 0`` + ``[0]`` indexing).

This is pure-logic (PyYAML parse of the committed play/role) — no infrastructure.
It PARSES YAML only; it never embeds or prints any secret value.

Test cases (repointed):
* 1 — health-uses-bao-status-not-uri-8200: ``svc07-health.yml`` verifies the
  primary via a ``docker exec ... bao status`` command task and contains NO
  ``ansible.builtin.uri`` ``:8200`` ``/v1/sys/health`` poll.
* 2 — Live-repro stub (``requires_infra``, deselected by default).
* Preservation P2-1..P2-5, live-surfaced regression guards (scope + empty-stdout
  + recreate-robust two-stage) — all repointed to the new homes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# tests/installer/test_primary_health_check_via_bao_status.py -> repo root parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
# The health gate moved into the svc07-health.yml CHILD play (native on guest);
# the banner + onboarding command moved into the svc07_handoff role.
_HEALTH_PLAY = _REPO_ROOT / "ansible" / "playbooks" / "svc07-health.yml"
_HANDOFF_ROLE = _REPO_ROOT / "ansible" / "roles" / "svc07_handoff" / "tasks" / "main.yml"
_HANDOFF_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_handoff" / "defaults" / "main.yml"
)


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally from the sibling structural tests; NO cross-tree
# import (conftest-collision rule). PyYAML safe_load_all parse only.
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
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _health_tasks() -> list[dict]:
    """Return the svc07-health.yml health play's tasks, in document order.

    The whole play IS the health gate now (there is no Phase-4 "block" wrapper —
    that was the pre-extraction in-orchestrator shape). Returns every functional
    + assert task of the single ``hosts: openbao`` play.
    """
    plays = _load_plays(_HEALTH_PLAY)
    assert plays, "svc07-health.yml has no plays."
    tasks: list[dict] = []
    for play in plays:
        for section in ("pre_tasks", "tasks", "post_tasks"):
            tasks.extend(_iter_tasks(play.get(section)))
    return tasks


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
    "block", "rescue", "always", "until", "retries", "delay", "environment",
    "check_mode", "notify", "listen",
}


def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _task_argv_text(task: dict) -> str:
    """Return a flat string of a command/shell task's argv (+ stdin), else ''.

    Covers ``argv`` as a YAML list and as a folded-scalar Jinja expression (the
    fix builds argv from a ``{{ [...] }}`` list), plus any ``stdin``.
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


def _argv_tokens(task: dict) -> list[str]:
    """Whitespace-delimited argv/stdin tokens for a command/shell task.

    Token-precise (NOT substring) matching: splitting into tokens and stripping
    surrounding punctuation makes ``docker``/``exec``/``status``/``bao`` match
    the actual literal argv tokens only.
    """
    raw = _task_argv_text(task)
    toks: list[str] = []
    for piece in raw.replace(",", " ").replace("'", " ").split():
        toks.append(piece.strip("[](){}'\""))
    return toks


def _uri_url(task: dict) -> str | None:
    """Return the ``url`` value of an ``ansible.builtin.uri`` / ``uri`` task."""
    for mod in ("ansible.builtin.uri", "uri"):
        spec = task.get(mod)
        if isinstance(spec, dict) and spec.get("url") is not None:
            return str(spec["url"])
    return None


def _is_docker_exec_bao_status(task: dict) -> bool:
    """A ``docker exec ... status`` command task (a ``bao status`` guest check)."""
    toks = _argv_tokens(task)
    return "docker" in toks and "exec" in toks and "status" in toks


def _is_container_running_wait(task: dict) -> bool:
    """A plain ``docker inspect <container>`` container-exists/running wait task.

    ``inspect`` present, ``exec`` absent — Stage 1. The running check lives in
    the ``until``/``that`` JSON gate (``(stdout | from_json)[0].State.Running``),
    not a Go-template ``-f`` format.
    """
    toks = _argv_tokens(task)
    return "docker" in toks and "inspect" in toks and "exec" not in toks


def _int_or_default_from_template(value, fallback: int) -> int:
    """Resolve an Ansible retries/delay value that may be a Jinja default() form.

    In svc07-health.yml the retry budget is written as a templated string, e.g.
    ``"{{ svc07_health_retries | default(30) | int }}"`` — the child play does
    NOT load svc07_handoff's role defaults, so the effective value at runtime is
    the ``default(N)`` fallback. This resolves:
      * a plain int/str integer -> that int;
      * a ``{{ ... | default(N) | ... }}`` template -> N (the effective value).
    Falls back to ``fallback`` if nothing parses.
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    m = re.search(r"default\(\s*(\d+)\s*\)", text)
    if m:
        return int(m.group(1))
    return fallback


# --------------------------------------------------------------------------- #
# Test case 1 — health-uses-bao-status-not-uri-8200 (repointed to svc07-health.yml).
# Validates: Requirements 1.1, 1.2, 1.3, 1.4 (health reaches the primary via an
# available path — `docker exec ... bao status` on the guest — never a `:8200`
# `/v1/sys/health` uri poll).
# --------------------------------------------------------------------------- #
def test_phase4_primary_health_uses_bao_status_not_uri_port_8200():
    """Health-check mechanism (Property 1) — Validates: Requirements 1.1-1.4.

    The primary health verification MUST reach the primary via a ``docker exec
    {{ svc07_openbao_primary_container_name }} bao status ... -format=json``
    command task (Stage 2 of svc07-health.yml) and MUST NOT use an
    ``ansible.builtin.uri`` poll of a primary-guest-IP ``:8200`` ``/v1/sys/health``
    endpoint — a host port the primary does NOT publish by design (Req 6 crit
    2/4), Traefik (SVC-09) not yet live.

    Repointed from the pre-extraction Phase-4 block of svc-07-bootstrap.yml to
    the svc07-health.yml child play; the invariant is identical.
    """
    health = _health_tasks()
    assert health, "svc07-health.yml has no health tasks to inspect."

    # (a) No :8200 sys/health uri poll anywhere in the health play.
    offending_uri_urls: list[str] = []
    for task in health:
        url = _uri_url(task)
        if url is None:
            continue
        if ":8200" in url and "/v1/sys/health" in url:
            offending_uri_urls.append(url)

    # (b) A `docker exec ... bao status` guest check gates primary health.
    bao_status_tasks = [t for t in health if _is_docker_exec_bao_status(t)]

    problems: list[str] = []
    if offending_uri_urls:
        problems.append(
            "COUNTEREXAMPLE (bug): svc07-health.yml polls a primary-guest-IP "
            ":8200 /v1/sys/health endpoint via ansible.builtin.uri — a host port "
            "the primary does NOT publish by design (Req 6 crit 2/4), and Traefik "
            "(SVC-09) is not deployed. Offending uri url(s): "
            f"{offending_uri_urls}."
        )
    if not bao_status_tasks:
        problems.append(
            "COUNTEREXAMPLE (bug): svc07-health.yml has NO `docker exec ... bao "
            "status` command task gating primary health. The gate must verify the "
            "primary via `docker exec {{ svc07_openbao_primary_container_name }} "
            "bao status ... -format=json` on the guest (native, no delegate_to)."
        )

    assert not problems, (
        "svc07-health.yml primary health check is misdirected:\n  "
        + "\n  ".join(problems)
    )


# --------------------------------------------------------------------------- #
# Test case 2 — Live-repro stub (requires_infra; deselected by default).
# --------------------------------------------------------------------------- #
@pytest.mark.requires_infra
def test_live_from_scratch_phase4_green_and_installer_exit0():
    """Live from-scratch reproduction (requires_infra — deselected by default).

    DOCUMENTED live gate; not executed offline. On a throwaway cluster, a
    from-scratch single-command Developer-B bring-up of SVC-07 must turn Phase 4
    GREEN via the svc07-health.yml child run's ``docker exec ... bao status``
    guest gate (``initialized:true, sealed:false``), and the installer must EXIT
    0 with the completion-summary banner. A direct ``:8200`` poll of the primary
    still yields HTTP 000 by design (no published port). References ``bao
    status`` fields / HTTP status strings ONLY; never echoes a secret value.
    """
    pytest.skip(
        "requires-infra: live from-scratch SVC-07 bring-up (Phase 4 GREEN via "
        "svc07-health.yml `docker exec ... bao status` on the guest; installer "
        "exits 0 with the completion banner). Documented live gate; run under "
        "-m requires_infra with a wired-up cluster. See validation.md."
    )


# =========================================================================== #
# Preservation (Property 2) — repointed to the new homes.                     #
# =========================================================================== #
_COMPOSE_TEMPLATE = (
    _REPO_ROOT / "ansible" / "roles" / "docker-compose-app"
    / "templates" / "compose.yaml.j2"
)
_OPENBAO_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "openbao.yml"
_OPENBAO_INSTALL_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_install" / "tasks" / "main.yml"
)


# --------------------------------------------------------------------------- #
# P2-1 — Deadline preserved (repointed to svc07-health.yml).
# Validates: Requirements 3.2, 3.7.
# --------------------------------------------------------------------------- #
def test_preserve_phase4_health_deadline_generous_and_poll_then_assert_shape():
    """Preservation P2-1 (Property 2) — Validates: Requirements 3.2, 3.7.

    The primary health gate in svc07-health.yml MUST retain a GENEROUS
    retry-until-healthy deadline on its poll task(s), and MUST keep the
    poll-then-assert shape (retrying poll tasks followed by assert tasks). The
    child play writes the budget as ``retries: "{{ svc07_health_retries |
    default(30) | int }}"`` / ``delay: "{{ svc07_health_delay | default(5) | int
    }}"`` — the effective value is the ``default(N)`` fallback because the child
    play does not load svc07_handoff's role defaults.

    This enforces the INVARIANT (a generous deadline is present) without pinning
    the exact value: effective ``delay == 5`` AND effective ``retries >= 24``
    (>= ~120s), comfortably exceeding the live-observed ~30s primary
    restart+unseal+engine-mount window. Repointed from the pre-extraction
    Phase-4 block; the budget moved verbatim into the child play (and its
    default lives in svc07_handoff/defaults/main.yml as 30/5).
    """
    health = _health_tasks()
    assert health, (
        "PRESERVATION P2-1: svc07-health.yml has no tasks — cannot check the "
        "health-poll deadline."
    )

    # The retrying poll tasks (Stage 1 docker inspect + Stage 2 bao status both
    # carry retries/delay).
    retry_tasks = [t for t in health if "retries" in t and "delay" in t]
    assert retry_tasks, (
        "PRESERVATION P2-1: no svc07-health.yml health-poll task with a "
        "retries/delay retry loop was found — the generous deadline was lost."
    )

    def _budget_ok(task: dict) -> bool:
        delay = _int_or_default_from_template(task.get("delay"), 0)
        retries = _int_or_default_from_template(task.get("retries"), 0)
        return delay == 5 and retries >= 24

    matching = [t for t in retry_tasks if _budget_ok(t)]
    assert matching, (
        "PRESERVATION P2-1: a svc07-health.yml poll must retain a GENEROUS "
        "deadline — effective delay: 5 AND retries >= 24 (>= ~120s). Found "
        "poll task(s) with "
        + ", ".join(
            f"retries={t.get('retries')!r}/delay={t.get('delay')!r}"
            for t in retry_tasks
        )
        + "."
    )

    # Poll-then-assert shape: at least one assert task alongside the retrying polls.
    assert any(
        "ansible.builtin.assert" in t or "assert" in t for t in health
    ), (
        "PRESERVATION P2-1: svc07-health.yml must keep the poll-then-assert shape "
        "— no assert task found alongside the retrying polls."
    )


# --------------------------------------------------------------------------- #
# P2-2 — No primary host port. Validates: Requirement 3.1.
# --------------------------------------------------------------------------- #
def test_preserve_no_primary_host_port_template_optin_and_no_health_injection():
    """Preservation P2-2 (Property 2) — Validates: Requirement 3.1.

    Two structural halves:
      (a) ``docker-compose-app``'s ``compose.yaml.j2`` MUST NOT emit ``ports:``
          unconditionally — the block is guarded by ``{% if svc.ports ... %}``
          (opt-in per-service; only the unsealer opts in). The primary sets no
          ``ports:`` -> no host port.
      (b) No task in svc07-health.yml injects a ``ports:`` / ``-p`` /
          published-port mapping for the primary.

    Repointed: the "no Phase-4 host-port injection" half now scans the
    svc07-health.yml child play (where the health tasks moved); the template half
    is UNCHANGED (docker-compose-app was not touched by this rework).
    """
    # --- (a) Template ports block is opt-in. ----------------------------------
    template_text = _COMPOSE_TEMPLATE.read_text(encoding="utf-8")
    assert "ports:" in template_text, (
        "PRESERVATION P2-2: expected the compose template to contain a `ports:` "
        "block (the opt-in exception) — none found; template shape changed."
    )
    assert "{% if svc.ports" in template_text, (
        "PRESERVATION P2-2: the compose template must gate its `ports:` block "
        "behind `{% if svc.ports ... %}` (opt-in per-service)."
    )
    ports_emissions = template_text.count("\n    ports:")
    guard_count = template_text.count("{% if svc.ports")
    assert guard_count >= ports_emissions and ports_emissions <= 1, (
        "PRESERVATION P2-2: found an unguarded `ports:` emission in the compose "
        f"template ({ports_emissions} emission(s), {guard_count} guard(s))."
    )

    # --- (b) No health task injects a primary host port. ----------------------
    health = _health_tasks()
    assert health, (
        "PRESERVATION P2-2: svc07-health.yml has no tasks — cannot check for "
        "injected host ports."
    )
    offenders: list[str] = []
    for task in health:
        name = _task_name(task) or "<unnamed>"
        for value in task.values():
            if isinstance(value, dict) and "ports" in value:
                offenders.append(f"{name}: module carries a `ports:` mapping")
        toks = _argv_tokens(task)
        if "-p" in toks or "--publish" in toks:
            offenders.append(f"{name}: argv publishes a host port (-p/--publish)")
        argv_text = _task_argv_text(task)
        if "docker" in toks and "run" in toks and ("-p " in argv_text or "-p" in toks):
            offenders.append(f"{name}: `docker run -p` publishes a host port")
    assert not offenders, (
        "PRESERVATION P2-2: a svc07-health.yml task injects a primary host port "
        "(would violate Req 6 crit 2):\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# P2-3 — Admin_Token untouched in the health gate. Validates: Requirement 3.3.
# --------------------------------------------------------------------------- #
def test_preserve_admin_token_untouched_in_phase4():
    """Preservation P2-3 (Property 2) — Validates: Requirement 3.3.

    NO svc07-health.yml task may WRITE or SET ``OPENBAO_ADMIN_TOKEN`` — the
    Admin_Token is minted, persisted and scrubbed in Phase 3 (svc07_bootstrap),
    and the Phase-4 health gate leaves it untouched. Repointed: the health play
    replaces the pre-extraction Phase-4 block as the scan scope.
    """
    health = _health_tasks()
    assert health, (
        "PRESERVATION P2-3: svc07-health.yml has no tasks — cannot check "
        "Admin_Token immutability."
    )

    _WRITE_MODULES = (
        "ansible.builtin.lineinfile", "lineinfile",
        "ansible.builtin.blockinfile", "blockinfile",
        "ansible.builtin.copy", "copy",
        "ansible.builtin.set_fact", "set_fact",
        "ansible.builtin.template", "template",
        "ansible.builtin.replace", "replace",
    )
    offenders: list[str] = []
    for task in health:
        name = _task_name(task) or "<unnamed>"
        for mod in _WRITE_MODULES:
            spec = task.get(mod)
            if spec is None:
                continue
            if "OPENBAO_ADMIN_TOKEN" in str(spec):
                offenders.append(f"{name}: `{mod}` references OPENBAO_ADMIN_TOKEN")
        if "OPENBAO_ADMIN_TOKEN" in _task_argv_text(task):
            offenders.append(f"{name}: command/shell argv references OPENBAO_ADMIN_TOKEN")
    assert not offenders, (
        "PRESERVATION P2-3: a svc07-health.yml task writes/sets "
        "OPENBAO_ADMIN_TOKEN — its persistence must stay in Phase 3 "
        "(svc07_bootstrap), not the health gate (Req 3.3):\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# P2-4 — Banner preserved (repointed to the svc07_handoff role).
# Validates: Requirement 3.3.
# --------------------------------------------------------------------------- #
def test_preserve_phase4_completion_banner_present():
    """Preservation P2-4 (Property 2) — Validates: Requirement 3.3.

    The Phase-4 completion-summary banner (an ``ansible.builtin.debug`` task
    whose ``msg`` LIST carries the "Deployed Successfully" header, the service
    summary, the "Credentials Saved" line, and the onboarding command) MUST
    remain present. Repointed to ``svc07_handoff/tasks/main.yml`` — the banner
    moved there with the Phase-4 handoff (it has no guest dependency, so it stays
    on the orchestrator side, not in the svc07-health.yml child play).
    """
    handoff = _load_task_file(_HANDOFF_ROLE)
    assert handoff, (
        "PRESERVATION P2-4: svc07_handoff/tasks/main.yml has no tasks — cannot "
        "check the completion banner."
    )

    banner_tasks: list[list[str]] = []
    for task in handoff:
        for mod in ("ansible.builtin.debug", "debug"):
            spec = task.get(mod)
            if isinstance(spec, dict) and isinstance(spec.get("msg"), list):
                banner_tasks.append([str(line) for line in spec["msg"]])

    assert banner_tasks, (
        "PRESERVATION P2-4: no debug task with a `msg:` LIST found in "
        "svc07_handoff/tasks/main.yml — the completion-summary banner is missing."
    )

    def _joined(lines: list[str]) -> str:
        return "\n".join(lines)

    required_substrings = (
        "Deployed Successfully",
        "Credentials Saved",
        "onboard",
    )
    matched = [
        lines for lines in banner_tasks
        if all(sub in _joined(lines) for sub in required_substrings)
    ]
    assert matched, (
        "PRESERVATION P2-4: the completion-summary banner must retain its "
        "service-summary ('Deployed Successfully'), credentials-saved-by-name "
        "('Credentials Saved'), and onboarding-command ('onboard') lines. Found "
        f"{len(banner_tasks)} debug-list task(s), none carrying all required lines."
    )


# --------------------------------------------------------------------------- #
# P2-5 — Unsealer + structure preserved (UNCHANGED files). Req 3.5, 3.6.
# --------------------------------------------------------------------------- #
def test_preserve_two_ordered_plays_unsealer_first_and_deployment_unit():
    """Preservation P2-5 (Property 2) — Validates: Requirements 3.5, 3.6.

    ``openbao.yml`` MUST remain exactly two ordered plays — Play 1 ``hosts:
    openbao-unsealer``, Play 2 ``hosts: openbao`` — AND the ``docker-compose-app``
    role MUST remain the bring-up path in ``openbao_install/tasks/main.yml``.
    These files are UNCHANGED by the svc07-installer-simplification rework
    (the health gate moved out of svc-07-bootstrap.yml, not out of openbao.yml).
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    assert len(plays) == 2, (
        f"PRESERVATION P2-5: openbao.yml must have exactly two ordered plays; "
        f"found {len(plays)}."
    )
    assert plays[0].get("hosts") == "openbao-unsealer", (
        "PRESERVATION P2-5: Play 1 must target hosts: openbao-unsealer — got "
        f"{plays[0].get('hosts')!r}."
    )
    assert plays[1].get("hosts") == "openbao", (
        "PRESERVATION P2-5: Play 2 must target hosts: openbao — got "
        f"{plays[1].get('hosts')!r}."
    )

    install_tasks = _load_task_file(_OPENBAO_INSTALL_TASKS)
    dca_includes = []
    for task in install_tasks:
        for inc_key in (
            "ansible.builtin.include_role", "include_role",
            "ansible.builtin.import_role", "import_role",
        ):
            spec = task.get(inc_key)
            name = spec.get("name", "") if isinstance(spec, dict) else str(spec or "")
            if "docker-compose-app" in name:
                dca_includes.append(task)
    assert dca_includes, (
        "PRESERVATION P2-5: the `docker-compose-app` role include was not found "
        "in openbao_install/tasks/main.yml (Req 3.5, 3.6)."
    )


# =========================================================================== #
# Regression guard — health vars resolve in the CHILD play scope.             #
# (Repointed from the localhost-orchestrator-scope guard.)                    #
# --------------------------------------------------------------------------- #
# The pre-extraction bug was: the Phase-4 poll argv referenced role-scoped vars
# (openbao_primary_container_name, defined only in openbao_install/defaults) from
# the localhost ORCHESTRATOR play, which never loads that role — so they resolved
# UNDEFINED at runtime. The rework fixes this structurally: the health gate is
# the svc07-health.yml CHILD play (hosts: openbao), and its argv references the
# `svc07_openbao_*` health vars whose defaults live in svc07_handoff/defaults.
# The child play references them with `| default(...)`, so an unset var is not a
# throw. This guard re-expresses the invariant for the new model: the health
# argv must reference the `svc07_openbao_*` health vars (NOT the bare role-scoped
# openbao_install names), and each such var must be resolvable — either declared
# in svc07_handoff/defaults/main.yml OR referenced with a `| default(...)` in the
# play.
# --------------------------------------------------------------------------- #

# The role-scoped names defined ONLY in openbao_install/defaults/main.yml. If the
# health argv referenced any of these bare names it would (as in the original
# bug) be undefined — the child play does not load openbao_install either.
_ROLE_SCOPED_NAMES = {
    "openbao_primary_container_name",
    "openbao_bao_bin",
    "openbao_primary_tls_skip_verify",
}
# The child-play-scoped health var names the argv MUST use instead.
_HEALTH_SCOPED_NAMES = {
    "svc07_openbao_primary_container_name",
    "svc07_openbao_bao_bin",
}

_JINJA_BLOCK_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_VAR_TOKEN_RE = re.compile(r"\b[a-z_][a-z0-9_]*\b")
_JINJA_NON_VARS = {
    "if", "else", "for", "in", "is", "not", "and", "or", "default", "bool",
    "true", "false", "none", "docker", "exec", "status", "format", "json",
    "from_json", "trim", "first", "int", "string", "length", "map", "list",
    "join", "lower", "upper", "tls", "skip", "verify",
}


def _jinja_var_refs_in_argv(task: dict) -> set[str]:
    """Return Ansible-var names referenced inside `{{ ... }}` blocks of a command argv."""
    text = _task_argv_text(task)
    refs: set[str] = set()
    for block in _JINJA_BLOCK_RE.findall(text):
        for tok in _VAR_TOKEN_RE.findall(block):
            if tok in _JINJA_NON_VARS:
                continue
            refs.add(tok)
    return refs


def _handoff_default_keys() -> set[str]:
    """Return the keys declared in svc07_handoff/defaults/main.yml."""
    data = yaml.safe_load(_HANDOFF_DEFAULTS.read_text(encoding="utf-8"))
    return set(data.keys()) if isinstance(data, dict) else set()


def test_health_argv_uses_child_scoped_vars_not_role_defaults():
    """Regression guard (scope) — Validates: Requirements 1.1, 1.2.

    The svc07-health.yml Stage-2 ``docker exec ... bao status`` poll argv must
    reference the child-play-scoped ``svc07_openbao_*`` health vars — NOT the
    bare ``openbao_install`` role-scoped names (which the child play never loads,
    the same undefined-at-runtime trap the original bug hit). And every
    ``svc07_*`` health var the argv references must be resolvable: declared in
    svc07_handoff/defaults/main.yml OR referenced with a ``| default(...)`` in
    the play text.
    """
    health = _health_tasks()
    poll_tasks = [t for t in health if _is_docker_exec_bao_status(t)]
    assert poll_tasks, (
        "REGRESSION (scope): no svc07-health.yml `docker exec ... bao status` "
        "poll task found — cannot check its argv var scope."
    )

    default_keys = _handoff_default_keys()
    play_text = _HEALTH_PLAY.read_text(encoding="utf-8")
    problems: list[str] = []

    for task in poll_tasks:
        refs = _jinja_var_refs_in_argv(task)

        leaked = _ROLE_SCOPED_NAMES & refs
        if leaked:
            problems.append(
                "COUNTEREXAMPLE (bug): the health poll argv references role-scoped "
                f"var(s) {sorted(leaked)} defined ONLY in "
                "openbao_install/defaults/main.yml — UNDEFINED in the svc07-health.yml "
                "child play (it never loads that role). Use the svc07_openbao_* "
                "child-scoped names."
            )

        missing_refs = _HEALTH_SCOPED_NAMES - refs
        if missing_refs:
            problems.append(
                "COUNTEREXAMPLE (bug): the health poll argv does NOT reference the "
                f"child-scoped var(s) {sorted(missing_refs)} — it must use the "
                "svc07_openbao_* names so they resolve in the child play."
            )

        # Every svc07_* var the argv references must be resolvable in the child
        # play: declared in svc07_handoff/defaults OR referenced with default().
        for ref in refs:
            if not ref.startswith("svc07_"):
                continue
            declared = ref in default_keys
            has_default = bool(
                re.search(rf"{re.escape(ref)}\s*\|\s*default\(", play_text)
            )
            if not (declared or has_default):
                problems.append(
                    f"COUNTEREXAMPLE (bug): health var {ref!r} is neither declared "
                    "in svc07_handoff/defaults/main.yml nor referenced with a "
                    "`| default(...)` in svc07-health.yml — it could be undefined "
                    "at render time in the child play."
                )

    assert not problems, (
        "svc07-health.yml health argv references vars that may not resolve in the "
        "child play scope:\n  " + "\n  ".join(problems)
    )


# =========================================================================== #
# Regression guard — empty-stdout-safe `until` gate (repointed).              #
# --------------------------------------------------------------------------- #
def _cond_text(value) -> str:
    """Flatten an Ansible `until:`/`that:` value (scalar or list) into one string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(clause) for clause in value)
    return str(value)


def test_phase4_health_gate_is_empty_stdout_safe_no_throwing_first():
    """Regression guard (empty-stdout safety) — Validates: Requirements 1.1, 1.2.

    Both the svc07-health.yml Stage-2 ``bao status`` poll ``until`` gate AND the
    following assert ``that:`` must be EMPTY-SAFE:
      (a) neither applies the throwing ``| first`` idiom to the stdout, AND
      (b) each includes a ``length > 0`` non-empty guard (so an empty stdout is a
          normal falsy RETRY, not an exception).

    Repointed from the pre-extraction Phase-4 block to the child play; the new
    play already implements the empty-safe idiom (``length > 0`` + ``[0]``
    string indexing before ``from_json``).
    """
    health = _health_tasks()
    poll_tasks = [
        t for t in health if _is_docker_exec_bao_status(t) and "until" in t
    ]
    assert poll_tasks, (
        "REGRESSION (empty-stdout): no svc07-health.yml `docker exec ... bao "
        "status` poll task carrying an `until:` gate was found."
    )
    assert_tasks = [
        t for t in health if ("ansible.builtin.assert" in t or "assert" in t)
    ]
    assert assert_tasks, (
        "REGRESSION (empty-stdout): no svc07-health.yml assert task was found "
        "alongside the primary health poll."
    )

    problems: list[str] = []

    for task in poll_tasks:
        until_text = _cond_text(task.get("until"))
        if "| first" in until_text:
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-2 poll `until:` applies the "
                "throwing `| first` idiom to the stdout — empty stdout would raise "
                "'No first item, sequence was empty' while evaluating the "
                "conditional."
            )
        if "length > 0" not in until_text:
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-2 poll `until:` has NO "
                "`length > 0` non-empty guard before indexing the first char."
            )

    for task in assert_tasks:
        spec = task.get("ansible.builtin.assert") or task.get("assert") or {}
        that_text = _cond_text(spec.get("that") if isinstance(spec, dict) else None)
        if "svc07_primary_status" not in that_text:
            continue
        if "| first" in that_text:
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-2 assert `that:` applies the "
                "throwing `| first` idiom to the stdout."
            )
        if "length > 0" not in that_text:
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-2 assert `that:` has NO "
                "`length > 0` non-empty guard preceding the first-char/`from_json` "
                "checks."
            )

    assert not problems, (
        "svc07-health.yml health gate is not empty-stdout-safe:\n  "
        + "\n  ".join(problems)
    )


# =========================================================================== #
# Regression guard — recreate-robust two-stage gate (repointed).              #
# --------------------------------------------------------------------------- #
def test_phase4_container_running_gate_precedes_bao_status_poll_recreate_robust():
    """Regression guard (recreate-robust two-stage gate) — Validates: Requirements 2.1, 2.2, 2.3, 3.2.

    svc07-health.yml must be RECREATE-ROBUST with a two-stage gate:
      (a) a Stage-1 plain ``docker inspect <container>`` container-running WAIT
          task exists (no ``-f``/``--format``), gating on
          ``(stdout | from_json)[0].State.Running`` in its ``until``, AND
      (b) it PRECEDES (document order) the Stage-2 ``docker exec ... bao status``
          poll, AND
      (c) the Stage-1 wait carries a retries/delay budget.

    Repointed from the pre-extraction Phase-4 block to the svc07-health.yml child
    play. The old ``delegate_to: svc07-20-01`` requirement is DROPPED and REPLACED
    by the native-on-guest model: the child play runs ``hosts: openbao``, so both
    stages run natively on the guest and there is NO ``delegate_to`` (the HARD
    CONSTRAINT — checked positively below).
    """
    health = _health_tasks()
    assert health, (
        "REGRESSION (recreate-robust): svc07-health.yml has no tasks — cannot "
        "check the two-stage gate ordering."
    )

    running_waits = [t for t in health if _is_container_running_wait(t)]
    problems: list[str] = []
    if not running_waits:
        problems.append(
            "COUNTEREXAMPLE (bug): svc07-health.yml has NO Stage-1 `docker inspect "
            "<container>` container-running WAIT task before the `bao status` poll "
            "— the check would race the end-of-play recreate."
        )
    else:
        # NATIVE-on-guest model: the Stage-1 wait must NOT delegate_to (the HARD
        # CONSTRAINT — it runs natively on hosts: openbao).
        if any("delegate_to" in t for t in running_waits):
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-1 container-running wait carries "
                "a delegate_to — svc07-health.yml runs natively on `hosts: openbao` "
                "and must NOT reintroduce the delegate_to idiom (Req 2.2)."
            )
        if not any(("retries" in t and "delay" in t) for t in running_waits):
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-1 container-running wait has no "
                "retries/delay budget — it must retry across the recreate window."
            )
        if any("-f" in _argv_tokens(t) or "--format" in _argv_tokens(t)
               for t in running_waits):
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-1 wait uses a `-f`/`--format` "
                "Go-template (re-evaluated as Jinja -> renders EMPTY). Use a plain "
                "`docker inspect <container>` and parse the JSON in the `until` gate."
            )
        if not any(
            "from_json" in str(t.get("until", "")) and "State.Running" in str(t.get("until", ""))
            for t in running_waits
        ):
            problems.append(
                "COUNTEREXAMPLE (bug): the Stage-1 wait's `until` gate does not "
                "parse the `docker inspect` JSON via `from_json` and read "
                "`State.Running` (empty-safe `(stdout | from_json)[0].State.Running`)."
            )

    first_running_idx = next(
        (i for i, t in enumerate(health) if _is_container_running_wait(t)), None
    )
    first_bao_status_idx = next(
        (i for i, t in enumerate(health)
         if _is_docker_exec_bao_status(t) and "until" in t),
        None,
    )
    if first_bao_status_idx is None:
        problems.append(
            "COUNTEREXAMPLE (bug): no svc07-health.yml `docker exec ... bao status` "
            "poll task (with an `until:` gate) was found — cannot verify ordering."
        )
    elif first_running_idx is None:
        problems.append(
            "COUNTEREXAMPLE (bug): the `bao status` poll has NO preceding "
            "container-running wait — the check is not recreate-robust."
        )
    elif not (first_running_idx < first_bao_status_idx):
        problems.append(
            "COUNTEREXAMPLE (bug): the `docker inspect <container>` container-running "
            "wait does NOT precede the `docker exec ... bao status` poll "
            f"(running-wait index {first_running_idx} >= bao-status index "
            f"{first_bao_status_idx}). Stage 1 must gate Stage 2."
        )

    assert not problems, (
        "svc07-health.yml is not recreate-robust (two-stage gate missing or "
        "mis-ordered):\n  " + "\n  ".join(problems)
    )
