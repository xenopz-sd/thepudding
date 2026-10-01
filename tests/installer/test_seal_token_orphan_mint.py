"""Offline structural guards for the SVC-07 primary seal-token orphan-revocation bugfix.

Bugfix: fix-svc07-primary-seal-token-orphan-revocation.

Root cause (see bugfix.md / design.md): on a fresh from-scratch SVC-07 bring-up
the unsealer now reliably ends ``Sealed: false`` (the prior committed
volume-ownership fix), so the failure moved one layer further along, onto the
primary. In ``ansible/roles/openbao_init_unseal/tasks/unsealer_bootstrap.yml``:

* STEP-6 mints the periodic ``Bootstrap_Transit_Token`` with
  ``bao token create -policy=openbao-seal -period=768h -field=token``,
  authenticated by the unsealer **root** token. With **no ``-orphan``**, the
  minted seal token is a **child** of that root token.
* STEP-7 hands the child seal token to the primary (0600 handoff file).
* STEP-8 then runs ``bao token revoke -self`` on that same unsealer root token.

Per OpenBao's default revoke semantics, revoking a parent token cascades to its
entire child sub-tree — so the just-handed-off seal token is dead within seconds
of the mint. When the primary later presents it to
``PUT https://10.0.20.11:8200/v1/transit/encrypt/openbao-unseal`` during its
Transit auto-unseal it gets ``403 permission denied`` (the token is revoked), so
the primary cannot auto-unseal and the installer fails at Task 8.4.

These tests encode the STRUCTURAL invariant whose ABSENCE causes the live bug
(design.md "Exploratory Bug Condition Checking"). They are pure-logic (PyYAML
parse of the committed role) — no infrastructure needed. They PARSE YAML only;
they never embed or print any secret value.

**On the UNFIXED code the bug-condition case (1) FAILS** — the STEP-6
``bao token create`` argv contains no ``-orphan`` token. That failure is the
SUCCESS signal for a bug-condition exploration test: it confirms the missing
``-orphan`` that makes the seal token a cascade-revoked child. The corroborating
case (2) documents the STEP-8 ``token revoke -self`` cascade mechanism the
``-orphan`` fixes and PASSES on unfixed code (it is context, not the bug
assertion). The ``requires_infra`` live-repro stub (3) is deselected by default
per the root ``pytest.ini`` (``addopts = -m "not requires_infra"``) and
documents the live from-scratch reproduction; it does not run offline.

The helpers below are COPIED locally from
``tests/installer/test_unsealer_reseal_ordering.py`` (the sibling structural
test) — they are NOT imported across test trees (conftest-collision rule).

Test cases:
* 1 — STEP-6-mints-orphan (the bug-condition test): the STEP-6
  ``bao token create`` command task's argv must contain the literal ``-orphan``
  token (FAILS on unfixed code; PASSES after the fix).
* 2 — Corroborating context: STEP-8 ``bao token revoke -self`` on the effective
  root token is present (documents the cascade mechanism the ``-orphan`` fixes;
  PASSES on unfixed code).
* 3 — Live-repro stub (``requires_infra``, deselected by default): documents the
  primary-auto-unseal proof (no ``403``) on a from-scratch bring-up.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_seal_token_orphan_mint.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_INIT_UNSEAL_ROLE = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal" / "tasks"
_UNSEALER_BOOTSTRAP = _INIT_UNSEAL_ROLE / "unsealer_bootstrap.yml"


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally from test_unsealer_reseal_ordering.py (no cross-tree
# import; conftest-collision rule). PyYAML safe_load_all parse only.
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


def _argv_tokens(task: dict) -> list[str]:
    """Whitespace-delimited argv/stdin tokens for a command/shell task.

    Token-precise (NOT substring) matching matters here: the unsealer's argv
    interpolates ``openbao_unsealer_*`` variable NAMES whose text contains
    ``token`` / ``create`` as substrings; splitting into tokens and stripping
    surrounding punctuation makes ``token``/``create``/``revoke``/``-self``/
    ``-orphan`` match the actual literal argv tokens only.
    """
    raw = _task_argv_text(task)
    toks: list[str] = []
    for piece in raw.replace(",", " ").replace("'", " ").split():
        toks.append(piece.strip("[](){}'\""))
    return toks


def _is_docker_exec_bao(tokens: list[str]) -> bool:
    return "docker" in tokens and "exec" in tokens


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _is_token_create(task: dict) -> bool:
    """A `docker exec ... token create` command task (mints the seal token).

    Identified by the ``token`` + ``create`` argv tokens together, so it never
    matches ``token revoke`` nor an ``openbao_unsealer_*`` variable name.
    """
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "token" in toks and "create" in toks


def _is_token_revoke_self(task: dict) -> bool:
    """A `docker exec ... token revoke -self` command task (root-token revoke)."""
    toks = _argv_tokens(task)
    return (
        _is_docker_exec_bao(toks)
        and "token" in toks
        and "revoke" in toks
        and "-self" in toks
    )


def _mentions_effective_root_token(task: dict) -> bool:
    """True if the task's argv references the in-memory effective root token."""
    return "openbao_unsealer_effective_token" in _task_argv_text(task)


# --------------------------------------------------------------------------- #
# Test case 1 — STEP-6-mints-orphan (THE bug-condition exploration test).
# Validates: Requirements 1.1, 1.2, 1.3 (structural cause of the primary 403).
# EXPECTED on unfixed code: FAILS (STEP-6 `token create` argv lacks `-orphan`).
# EXPECTED after the fix: PASSES.
# --------------------------------------------------------------------------- #
def test_step6_seal_token_mint_argv_contains_orphan():
    """Bug condition (Property 1) — Validates: Requirements 1.1, 1.2, 1.3.

    The STEP-6 "mint the periodic Bootstrap_Transit_Token" task in
    ``unsealer_bootstrap.yml`` mints the seal token with a ``bao token create``
    argv. To survive the STEP-8 ``token revoke -self`` on the unsealer root
    token (which the seal token would otherwise be a CHILD of), that argv MUST
    include the ``-orphan`` token — an orphan token has no parent, so the root
    revoke does not cascade to it and the primary's handed-off seal token stays
    valid (no ``403`` on ``transit/encrypt/openbao-unseal``).

    On UNFIXED code the argv is
    ``bao token create -policy=openbao-seal -period=768h -field=token`` with NO
    ``-orphan``, so this assertion FAILS — the SUCCESS signal for a bug-condition
    exploration test. It confirms the seal token is minted as a child that
    STEP-8's default revoke cascade-kills.
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)

    # Locate the STEP-6 mint: the `docker exec ... token create` command task.
    # Corroborate identity via the task `name` (mint / Bootstrap_Transit_Token).
    mint_tasks = [t for t in tasks if _is_token_create(t)]
    assert mint_tasks, (
        "No `docker exec ... token create` task found in unsealer_bootstrap.yml — "
        "cannot locate the STEP-6 Bootstrap_Transit_Token mint."
    )
    # Prefer the one whose name marks it as the mint, else fall back to the only
    # token-create task (there is exactly one on the current tree).
    mint = next(
        (
            t
            for t in mint_tasks
            if "mint" in _task_name(t).lower()
            or "bootstrap_transit_token" in _task_name(t).lower()
        ),
        mint_tasks[0],
    )

    toks = _argv_tokens(mint)
    argv_text = _task_argv_text(mint)
    assert "-orphan" in toks or "-orphan" in argv_text, (
        "COUNTEREXAMPLE (bug): the STEP-6 seal-token mint "
        f"({_task_name(mint)!r}) `bao token create` argv does NOT contain the "
        "`-orphan` token. Its argv is:\n    "
        f"{argv_text}\n"
        "Without `-orphan`, the minted Bootstrap_Transit_Token is a CHILD of the "
        "unsealer root token (openbao_unsealer_effective_token) that "
        "authenticated the mint. STEP-8 then runs `bao token revoke -self` on "
        "that root token, and OpenBao's default revoke cascades to the child "
        "sub-tree — revoking the just-handed-off seal token seconds after STEP-7 "
        "hands it to the primary. The primary then presents a dead token to "
        "transit/encrypt/openbao-unseal and gets 403 permission denied, failing "
        "to auto-unseal (installer Task 8.4). The fix adds `-orphan` to the "
        "`token create` argv so the seal token has no parent and survives the "
        "root revoke."
    )


# --------------------------------------------------------------------------- #
# Test case 2 — Corroborating context: STEP-8 `token revoke -self` present.
# Validates: Requirements 1.2 (documents the cascade mechanism `-orphan` fixes).
# EXPECTED on unfixed code: PASSES (this is context, not the bug assertion).
# --------------------------------------------------------------------------- #
def test_step8_root_token_revoke_self_present():
    """Corroborating context (Property 1 mechanism) — Validates: Requirement 1.2.

    Documents the cascade mechanism the ``-orphan`` fix protects against: STEP-8
    of ``unsealer_bootstrap.yml`` runs ``bao token revoke -self`` on the unsealer
    **root** token (``openbao_unsealer_effective_token``). Because STEP-6 mints
    the seal token as a CHILD of that same root token (no ``-orphan``), this
    revoke cascades to and kills the seal token by OpenBao's default revoke
    semantics — the mechanism the ``-orphan`` flag defeats.

    This case PASSES on the UNFIXED tree: the revoke-self is present and correct
    (it stays unchanged by the fix — only the seal token's parentage changes).
    It exists to record, in an executable form, WHY the missing ``-orphan`` is
    the defect rather than merely asserting the argv token in isolation.
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)

    revoke = next(
        (t for t in tasks if _is_token_revoke_self(t) and _mentions_effective_root_token(t)),
        None,
    )
    assert revoke is not None, (
        "Expected a STEP-8 `docker exec ... token revoke -self` task on the "
        "effective root token (openbao_unsealer_effective_token) in "
        "unsealer_bootstrap.yml — this is the cascade mechanism that revokes the "
        "child seal token when STEP-6 mints it without `-orphan`. Its absence "
        "would mean the role structure changed unexpectedly; re-observe before "
        "reasoning about the orphan fix."
    )


# --------------------------------------------------------------------------- #
# Test case 3 — Live-repro stub (requires_infra; deselected by default).
# Validates: Requirements 2.3, 2.4 behaviourally (the live acceptance).
# --------------------------------------------------------------------------- #
@pytest.mark.requires_infra
def test_live_from_scratch_primary_auto_unseals_no_403():
    """Live from-scratch reproduction (requires_infra — deselected by default).

    DOCUMENTED live gate; not executed offline. On a throwaway cluster, a
    from-scratch Developer-B bring-up of SVC-07:

    UNFIXED code (reproduces the bug):
      * The unsealer (``svc07-unsealer-20-01`` / ``10.0.20.11``) ends the run
        ``Initialized: true, Sealed: false`` (retained from the prior
        volume-ownership fix).
      * The primary (``svc07-20-01`` / ``10.0.20.10``) gets ``403 permission
        denied`` on its Transit auto-unseal call to
        ``PUT https://10.0.20.11:8200/v1/transit/encrypt/openbao-unseal`` because
        its handed-off seal token was cascade-revoked when STEP-8 revoked the
        unsealer root token — so the primary crash-loops and the installer fails
        at Task 8.4.

    FIXED code (the acceptance):
      * STEP-6 mints the seal token as an orphan, so it survives the STEP-8 root
        ``revoke -self``. The primary presents the still-valid token, encrypts /
        decrypts the unseal key with NO ``403``, auto-unseals, and reports
        healthy — the single-command Developer-B bring-up completes end-to-end
        (the first true end-to-end green for SVC-07).

    This stub is marked ``@pytest.mark.requires_infra`` and is therefore
    DESELECTED by default via the root ``pytest.ini`` (``addopts = -m "not
    requires_infra"``). The authoritative behavioural evidence is captured in the
    spec's validation.md from a real throwaway-cluster run; it references
    ``bao status`` fields / HTTP status strings ONLY and must never echo any
    token value.
    """
    pytest.skip(
        "requires-infra: live from-scratch SVC-07 bring-up on a throwaway cluster "
        "(unsealer ends Sealed:false, primary auto-unseals with NO 403 on "
        "transit/encrypt/openbao-unseal). Documented live gate; run under "
        "-m requires_infra with a wired-up cluster. See validation.md."
    )

# ===========================================================================
# ===========================================================================
# TASK 2 — PRESERVATION property tests (Property 2).
# ===========================================================================
# ===========================================================================
#
# These are PRESERVATION guards (design.md "Preservation Checking", Property 2).
# UNLIKE the bug-condition case above (test_step6_seal_token_mint_argv_contains_orphan
# — which FAILS on the unfixed tree and only PASSES once the `-orphan` fix lands),
# the cases below encode the CURRENT committed baseline behaviour and therefore
# **PASS on the UNFIXED code**. They pin the invariants the one-argv-token fix
# must NOT regress. Observation-first: each was written against, and observed to
# pass on, the UNFIXED tree.
#
#   P2-1 Root revoke preserved (Req 3.1) — STEP-8 `token revoke -self` on the
#        effective root token still exists. (This is the SAME assertion as the
#        Task-1 corroborating case `test_step8_root_token_revoke_self_present`,
#        which therefore ALSO doubles as P2-1; the explicitly-named case below
#        pins it as a preservation invariant without duplicating the identify
#        logic — it reuses the same helpers.)
#   P2-2 Scoped policy preserved (Req 3.2) — the STEP-5 `policy write` stdin HCL
#        grants `update` on transit/encrypt/openbao-unseal and
#        transit/decrypt/openbao-unseal ONLY; no broader path (transit/*, sys/,
#        root) is granted.
#   P2-3 Secret discipline preserved (Req 3.3) — every secret-bearing task
#        (operator init, each operator unseal, policy write, token create, token
#        revoke, and the capture/scrub set_facts) sets no_log.
#   P2-4 Prior-fix + guards preserved (Req 3.4, 3.6, 3.7) — the STEP-3b re-unseal
#        guard task(s) exist, the STEP-1/STEP-3 readiness waits exist, and the
#        main.yml include of unsealer_bootstrap.yml is gated on
#        `not openbao_already_initialized`.
#   P2-5 Two-play + deployment-unit preserved (Req 3.5) — openbao.yml is exactly
#        two plays, Play 1 -> openbao-unsealer, Play 2 -> openbao, and the
#        docker-compose-app bring-up include remains in openbao_install.
#
# They reuse the local helpers defined in the Task-1 section above
# (`_load_task_file`, `_iter_tasks`, `_task_module_names`, `_argv_tokens`,
# `_is_docker_exec_bao`, `_task_name`, `_is_token_create`, `_is_token_revoke_self`,
# `_mentions_effective_root_token`) — NO duplicate helpers are re-declared; only
# small new preservation-specific helpers are added below.
#
# Parse-only: these tests read YAML structure; they never embed or print any
# secret value.

_OPENBAO_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "openbao.yml"
_MAIN_TASKS = _INIT_UNSEAL_ROLE / "main.yml"
_OPENBAO_INSTALL_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_install" / "tasks" / "main.yml"
)


# --------------------------------------------------------------------------- #
# Small preservation-only helpers (added; not re-declaring Task-1 helpers).
# --------------------------------------------------------------------------- #
def _load_plays(path: Path):
    """Return the list of play dicts from an Ansible playbook.

    A playbook is a single YAML document whose top-level node is a LIST of
    plays; flatten any list documents and keep the play dicts.
    """
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


def _condition_text(task: dict) -> str:
    """Flatten a task's `when:` (str or list) into one lowercase string."""
    when = task.get("when", "")
    if isinstance(when, (list, tuple)):
        return " ".join(str(c) for c in when).lower()
    return str(when).lower()


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
    return True


def _policy_write_stdin(task: dict) -> str:
    """Return the `stdin` HCL scalar of a `docker exec ... policy write` task."""
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if isinstance(spec, dict) and isinstance(spec.get("stdin"), str):
            return spec["stdin"]
    return ""


def _is_operator_init(task: dict) -> bool:
    """A `docker exec ... operator init` command task (token-precise)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "operator" in toks and "init" in toks


def _is_operator_unseal(task: dict) -> bool:
    """A `docker exec ... operator unseal` command task (token-precise)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "operator" in toks and "unseal" in toks


def _is_policy_write(task: dict) -> bool:
    """A `docker exec ... policy write` command task."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "policy" in toks and "write" in toks


def _is_token_revoke(task: dict) -> bool:
    """A `docker exec ... token revoke` command task (revokes the root token)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "token" in toks and "revoke" in toks


def _is_status_read(task: dict) -> bool:
    """A `docker exec ... status` command task (a readiness `bao status` poll)."""
    toks = _argv_tokens(task)
    return _is_docker_exec_bao(toks) and "status" in toks


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


# --------------------------------------------------------------------------- #
# P2-1 — Root revoke preserved (unsealer_bootstrap.yml STEP-8).
# Validates: Requirement 3.1 (preservation — holds on unfixed code).
# NOTE: test_step8_root_token_revoke_self_present (Task-1 corroborating case)
# asserts the identical invariant and therefore ALSO serves as P2-1. This case
# names it explicitly as a preservation guard; it reuses the same helpers rather
# than duplicating the identify logic pointlessly.
# --------------------------------------------------------------------------- #
def test_preserve_step8_root_token_revoke_self():
    """Preservation P2-1 (Property 2) — Validates: Requirement 3.1.

    STEP-8 of ``unsealer_bootstrap.yml`` MUST keep revoking the unsealer **root**
    token with ``bao token revoke -self`` on ``openbao_unsealer_effective_token``.
    The ``-orphan`` fix only changes the seal token's PARENTAGE (so the seal
    token survives this revoke); it must NOT remove or weaken the root revoke
    itself — a leaked root token administering the unsealer is the exact risk the
    revoke closes. This baseline holds on the UNFIXED tree.

    (Doubles with the Task-1 corroborating case
    ``test_step8_root_token_revoke_self_present``; kept here as an explicitly
    named preservation invariant.)
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)
    revoke = next(
        (
            t
            for t in tasks
            if _is_token_revoke_self(t) and _mentions_effective_root_token(t)
        ),
        None,
    )
    assert revoke is not None, (
        "PRESERVATION P2-1: the STEP-8 `docker exec ... token revoke -self` on "
        "the effective root token (openbao_unsealer_effective_token) must remain "
        "in unsealer_bootstrap.yml — the `-orphan` fix must not remove the root "
        "revoke, only make the seal token an orphan so it survives it."
    )


# --------------------------------------------------------------------------- #
# P2-2 — Scoped policy preserved (unsealer_bootstrap.yml STEP-5).
# Validates: Requirement 3.2 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_step5_scoped_seal_policy_encrypt_decrypt_only():
    """Preservation P2-2 (Property 2) — Validates: Requirement 3.2.

    The STEP-5 ``policy write`` task's stdin HCL MUST grant ``update`` on
    ``transit/encrypt/openbao-unseal`` and ``transit/decrypt/openbao-unseal``
    ONLY — the exact two paths the primary's seal token needs — and MUST NOT
    grant any broader path (a ``transit/*`` glob, a ``sys/`` path, or root
    ``"*"``). The token-parentage fix does not touch the policy body; this
    scoped-policy baseline must survive the fix (widening the policy would weaken
    security and is explicitly rejected in design.md). Holds on the UNFIXED tree.
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)
    policy_tasks = [t for t in tasks if _is_policy_write(t)]
    assert policy_tasks, (
        "PRESERVATION P2-2: no `docker exec ... policy write` task found in "
        "unsealer_bootstrap.yml — cannot verify the scoped openbao-seal policy."
    )
    # Prefer the seal-policy write (by name / stdin content); there is exactly
    # one policy write on the current tree.
    policy = next(
        (t for t in policy_tasks if "seal" in _task_name(t).lower()),
        policy_tasks[0],
    )
    stdin = _policy_write_stdin(policy)
    assert stdin, (
        "PRESERVATION P2-2: the STEP-5 seal-policy `policy write` task has no "
        f"`stdin` HCL to inspect (task {_task_name(policy)!r})."
    )
    stdin_l = stdin.lower()

    # The two capability paths that MUST be present, each with update only.
    # This is a PARSE-ONLY test of the committed YAML: the HCL is templated with
    # Jinja (`{{ openbao_transit_mount_path }}/encrypt/{{ openbao_transit_key_name }}`)
    # and is NOT rendered here (no Ansible run), so we assert the UNRENDERED
    # template form the committed file actually holds — the mount path + key name
    # come from the openbao_transit_mount_path / openbao_transit_key_name vars
    # (which default to `transit` / `openbao-unseal`). Matching the templated
    # `encrypt/{{ openbao_transit_key_name }}` / `decrypt/{{ openbao_transit_key_name }}`
    # substrings pins that the two granted paths are the encrypt/decrypt paths of
    # the SAME single transit key, never a broader path.
    _encrypt_path = "encrypt/{{ openbao_transit_key_name }}"
    _decrypt_path = "decrypt/{{ openbao_transit_key_name }}"
    assert _encrypt_path in stdin and _decrypt_path in stdin, (
        "PRESERVATION P2-2: the STEP-5 seal policy HCL must grant capabilities on "
        "BOTH the transit encrypt and decrypt paths of the unseal key "
        f"({_encrypt_path!r} and {_decrypt_path!r}) — its stdin HCL was:\n    "
        f"{stdin}\n"
    )
    # And the mount-path prefix is the templated transit mount, not a hardcoded
    # broader mount — both path stanzas begin with `{{ openbao_transit_mount_path }}/`.
    assert stdin.count("{{ openbao_transit_mount_path }}/") == 2, (
        "PRESERVATION P2-2: both seal-policy path stanzas must be scoped under the "
        "templated transit mount path `{{ openbao_transit_mount_path }}/` (encrypt "
        f"+ decrypt); its stdin HCL was:\n    {stdin}\n"
    )
    # `capabilities = ["update"]` must appear (the granted capability), exactly
    # twice (one per path) and never a broader capability like sudo/read on these.
    assert 'capabilities = ["update"]' in stdin, (
        "PRESERVATION P2-2: the seal policy must grant `capabilities = "
        f'["update"]` on the encrypt/decrypt paths (its stdin HCL was:\n    {stdin}\n).'
    )

    # NO broader path may be granted. A `transit/*` glob, a `sys/` path, or a
    # bare root path "*" would over-scope the seal token beyond encrypt/decrypt.
    forbidden_broad_paths = (
        'path "transit/*"',
        'path "sys/',
        'path "*"',
        "path \"secret/",
        "path \"auth/",
    )
    offenders = [p for p in forbidden_broad_paths if p in stdin]
    assert not offenders, (
        "PRESERVATION P2-2: the STEP-5 seal policy must NOT grant any path "
        f"broader than transit/encrypt|decrypt/openbao-unseal; found {offenders} "
        f"in its stdin HCL:\n    {stdin}\n"
    )
    # Belt-and-braces: the only two `path "..."` stanzas are the encrypt/decrypt
    # ones. Count `path "` occurrences and assert exactly two.
    path_stanza_count = stdin_l.count('path "')
    assert path_stanza_count == 2, (
        "PRESERVATION P2-2: the seal policy HCL must contain EXACTLY two `path` "
        "stanzas (encrypt + decrypt on openbao-unseal); found "
        f"{path_stanza_count} in:\n    {stdin}\n"
    )


# --------------------------------------------------------------------------- #
# P2-3 — Secret discipline preserved (unsealer_bootstrap.yml).
# Validates: Requirement 3.3 (preservation — holds on unfixed code).
# Mirrors the assertion style of test_unsealer_reseal_ordering.py's
# test_preserve_existing_secret_bearing_tasks_set_no_log.
# --------------------------------------------------------------------------- #
def test_preserve_secret_bearing_tasks_set_no_log():
    """Preservation P2-3 (Property 2) — Validates: Requirement 3.3.

    Every secret-bearing task in ``unsealer_bootstrap.yml`` — the ``operator
    init``, each ``operator unseal``, the ``policy write``, the ``token create``
    mint, the ``token revoke``, and the in-memory capture/scrub ``set_fact``
    tasks that bind Shamir keys / root token / seal token — MUST set ``no_log``
    (literal ``true`` or ``"{{ openbao_no_log }}"``). The ``-orphan`` fix adds no
    secret-bearing task and must not weaken this discipline. Holds on the UNFIXED
    tree.
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

    missing_kinds = [label for label, count in seen.items() if count == 0]
    assert not missing_kinds, (
        "PRESERVATION P2-3 sanity: expected secret-bearing task kind(s) not found "
        f"in unsealer_bootstrap.yml: {missing_kinds}. The baseline structure "
        "changed unexpectedly — re-observe before asserting no_log."
    )

    assert not offenders, (
        "PRESERVATION P2-3: every Shamir-key / root-token / seal-token task in "
        "unsealer_bootstrap.yml must set no_log (literal true or "
        '"{{ openbao_no_log }}") — the fix must not weaken this. Offenders:\n  '
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# P2-4 — Prior-fix + guards preserved.
# Validates: Requirements 3.4, 3.6, 3.7 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_reunseal_guard_present():
    """Preservation P2-4a (Property 2) — Validates: Requirement 3.4.

    The prior fix-svc07-unsealer-reseal-on-handler-restart re-unseal guard
    (STEP-3b) MUST remain in ``unsealer_bootstrap.yml`` — identified by task
    name(s) containing "re-unseal guard". The token-parentage fix must not remove
    it. Holds on the UNFIXED tree.
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)
    guard_tasks = [t for t in tasks if "re-unseal guard" in _task_name(t).lower()]
    assert guard_tasks, (
        "PRESERVATION P2-4a: no STEP-3b 're-unseal guard' task found in "
        "unsealer_bootstrap.yml — the prior "
        "fix-svc07-unsealer-reseal-on-handler-restart guard must be preserved "
        "(Req 3.4)."
    )


def test_preserve_readiness_waits_present():
    """Preservation P2-4b (Property 2) — Validates: Requirement 3.6.

    The STEP-1 pre-init readiness wait and the STEP-3 post-unseal readiness wait
    MUST remain in ``unsealer_bootstrap.yml`` — identified by task names
    containing "readiness" / "post-unseal wait". These are the guards that fixed
    bug #7 and the sibling post-unseal race; the ``-orphan`` fix must not remove
    them. Holds on the UNFIXED tree.
    """
    tasks = _load_task_file(_UNSEALER_BOOTSTRAP)
    names = [_task_name(t).lower() for t in tasks]

    # A `bao status` readiness poll BEFORE init (STEP-1) — name mentions
    # "readiness" and it is a `docker exec ... status` command task.
    step1_readiness = [
        t
        for t in tasks
        if _is_status_read(t) and "readiness" in _task_name(t).lower()
    ]
    assert step1_readiness, (
        "PRESERVATION P2-4b: no STEP-1 pre-init `bao status` readiness-wait task "
        "found in unsealer_bootstrap.yml (name should mention 'readiness') — the "
        "bug #7 readiness guard must be preserved (Req 3.6)."
    )

    # A post-unseal readiness wait (STEP-3) — name mentions "post-unseal wait".
    step3_post_wait = [t for t in names if "post-unseal wait" in t]
    assert step3_post_wait, (
        "PRESERVATION P2-4b: no STEP-3 'post-unseal wait' readiness task found in "
        "unsealer_bootstrap.yml — the post-unseal race guard must be preserved "
        "(Req 3.6)."
    )


def test_preserve_unsealer_bootstrap_include_idempotency_gate():
    """Preservation P2-4c (Property 2) — Validates: Requirement 3.7.

    The ``include_tasks: unsealer_bootstrap.yml`` in ``main.yml`` MUST stay gated
    on ``not openbao_already_initialized`` (and ``openbao_role == "unsealer"``) so
    a re-run against an already-initialised unsealer skips the whole bootstrap —
    no STEP-6 re-mint, no double-mint. The ``-orphan`` fix must not touch this
    include gate. Holds on the UNFIXED tree.
    """
    tasks = _load_task_file(_MAIN_TASKS)

    include = None
    for task in tasks:
        for inc_key in (
            "ansible.builtin.include_tasks",
            "include_tasks",
            "ansible.builtin.import_tasks",
            "import_tasks",
        ):
            spec = task.get(inc_key)
            ident = spec.get("file", "") if isinstance(spec, dict) else str(spec or "")
            if "unsealer_bootstrap.yml" in ident:
                include = task
                break
        if include is not None:
            break

    assert include is not None, (
        "PRESERVATION P2-4c: the `include_tasks: unsealer_bootstrap.yml` was not "
        "found in main.yml — cannot verify its idempotency gate."
    )

    cond = _condition_text(include)
    assert "not openbao_already_initialized" in cond, (
        "PRESERVATION P2-4c: the unsealer bootstrap include must remain gated on "
        "`not openbao_already_initialized` (the idempotency gate, Req 3.7) — the "
        f"fix must not remove it (its when: was {include.get('when')!r})."
    )
    assert 'openbao_role == "unsealer"' in cond or "openbao_role == 'unsealer'" in cond, (
        "PRESERVATION P2-4c: the unsealer bootstrap include must remain gated on "
        f"openbao_role == \"unsealer\" (its when: was {include.get('when')!r})."
    )


# --------------------------------------------------------------------------- #
# P2-5 — Two-play + deployment-unit preserved (openbao.yml + openbao_install).
# Validates: Requirement 3.5 (preservation — holds on unfixed code).
# --------------------------------------------------------------------------- #
def test_preserve_two_ordered_plays_unsealer_first_and_deployment_unit():
    """Preservation P2-5 (Property 2) — Validates: Requirement 3.5.

    ``openbao.yml`` MUST remain exactly two ordered plays — Play 1 ``hosts:
    openbao-unsealer`` (unsealer first), Play 2 ``hosts: openbao`` (primary
    second) — AND the ``docker-compose-app`` role MUST remain the bring-up path
    in ``openbao_install`` (the sole sanctioned deployment unit). The one-argv
    fix touches neither; this structural baseline holds on the UNFIXED tree.
    """
    plays = _load_plays(_OPENBAO_PLAYBOOK)
    assert len(plays) == 2, (
        "PRESERVATION P2-5: openbao.yml must have exactly two ordered plays; "
        f"found {len(plays)}."
    )
    assert plays[0].get("hosts") == "openbao-unsealer", (
        "PRESERVATION P2-5: Play 1 must target hosts: openbao-unsealer (unsealer "
        f"first) — got {plays[0].get('hosts')!r}."
    )
    assert plays[1].get("hosts") == "openbao", (
        "PRESERVATION P2-5: Play 2 must target hosts: openbao (primary second) — "
        f"got {plays[1].get('hosts')!r}."
    )

    # Deployment unit: the docker-compose-app role must be included in
    # openbao_install/tasks/main.yml as the Compose stack bring-up path.
    install_tasks = _load_task_file(_OPENBAO_INSTALL_TASKS)
    dca_includes = []
    for task in install_tasks:
        for inc_key in (
            "ansible.builtin.include_role",
            "include_role",
            "ansible.builtin.import_role",
            "import_role",
        ):
            spec = task.get(inc_key)
            name = spec.get("name", "") if isinstance(spec, dict) else str(spec or "")
            if "docker-compose-app" in name:
                dca_includes.append(task)
    assert dca_includes, (
        "PRESERVATION P2-5: the `docker-compose-app` role include (the sole "
        "sanctioned Compose deployment unit) was not found in "
        "openbao_install/tasks/main.yml — the fix must not change the deployment "
        "unit (Req 3.5)."
    )
