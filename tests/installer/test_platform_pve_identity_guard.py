"""Offline drift-guard for platform_pve_identity opt-in + non-secrecy (Property 2).

Feature: platform-prerequisites-bootstrap, Task 5.3 — Property 2 (PVE identity
opt-in and non-secrecy).

Spec: .kiro/specs/platform-prerequisites-bootstrap/ (design.md "Component 3:
platform_pve_identity" + "Property 2", requirements.md Requirements 2.1, 2.3,
2.4). This is the structural, offline pin for the OPT-IN + secret-hygiene
invariants of the PVE identity bootstrap role implemented in Tasks 5.1/5.2:

WHAT IS ASSERTED (offline, PyYAML parse only, no infra)
-------------------------------------------------------
1. GATING (Req 2.1) — every PVE-identity task is gated `when:
   bootstrap_pve_identity` (directly, or by inheritance from a block-level
   `when` that references the flag). The role wraps its whole body in one
   top-level block gated on `bootstrap_pve_identity | default(false) | bool`,
   so every task inherits the opt-in gate.

2. no_log ON SECRET-HANDLING TASKS (Req 2.3, 2.4) — every task that handles the
   Admin_Bootstrap_Credential OR the minted token value is `no_log: true`
   (NOT false, NOT missing). "Handles a secret value" := reads / registers /
   templates / persists the admin credential or the composed/one-time token
   secret. NON-secret tasks (pveum role/user/acl/token-LIST, INFO debug logs
   that name only the token id, asserts carrying no secret) are exempt.

3. 0600 PERSIST (Req 2.3) — the persist task that writes PROXMOX_API_TOKEN to
   cluster.env via lineinfile sets `mode: "0600"`.

4. NO SECRET ECHO (Req 2.3, 2.4) — no task echoes the admin credential or the
   token value. Concretely: no debug/`msg`/fail task prints the composed token
   value, the one-time secret uuid, or the admin password/token value. The INFO
   decision logs may reference the token *id* + "value not shown" only.

This is a pure-structure drift guard (PyYAML `safe_load_all` of the committed
role file). It PARSES YAML only; it never embeds or prints any secret value.
The authoritative end-to-end confirmation remains the live acceptance (Task 14);
this guard pins the invariant so it cannot silently regress.

The helpers below are COPIED locally (NOT imported across test trees — the
conftest-collision rule) from the sibling structural test
(`test_secret_hygiene_no_log.py`).
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_platform_pve_identity_guard.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_MAIN = (
    _REPO_ROOT
    / "ansible"
    / "roles"
    / "platform_pve_identity"
    / "tasks"
    / "main.yml"
)
_ROLE_DEFAULTS = (
    _REPO_ROOT
    / "ansible"
    / "roles"
    / "platform_pve_identity"
    / "defaults"
    / "main.yml"
)

# The opt-in flag the whole role must be gated on (Req 2.1).
_GATE_FLAG = "bootstrap_pve_identity"


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally (no cross-tree import; conftest-collision rule).
# PyYAML safe_load_all parse only.
# --------------------------------------------------------------------------- #
_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when",
    "loop", "loop_control", "delegate_to", "run_once", "become", "vars",
    "tags", "args", "block", "rescue", "always", "until", "retries", "delay",
    "environment", "check_mode", "notify", "listen",
}


def _iter_tasks_with_inherited_when(tasks, inherited_when):
    """Yield (task, effective_when_texts) for every leaf task, recursively.

    ``effective_when_texts`` is the list of stringified ``when`` clauses that
    apply to the task — its own ``when`` plus every enclosing block's ``when``.
    Descends block/rescue/always so a block-level ``when`` is correctly seen as
    gating every child task (Ansible's real inheritance semantics).
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue

        own_when = task.get("when")
        when_texts = list(inherited_when)
        if own_when is not None:
            if isinstance(own_when, list):
                when_texts.extend(str(w) for w in own_when)
            else:
                when_texts.append(str(own_when))

        is_container = any(k in task for k in ("block", "rescue", "always"))
        if is_container:
            for key in ("block", "rescue", "always"):
                if key in task:
                    yield from _iter_tasks_with_inherited_when(
                        task[key], when_texts
                    )
        else:
            yield task, when_texts


def _load_leaf_tasks_with_when(path: Path):
    """Return [(leaf_task_dict, effective_when_texts), ...] for the role file."""
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    out: list[tuple[dict, list[str]]] = []
    for doc in docs:
        if isinstance(doc, list):
            out.extend(_iter_tasks_with_inherited_when(doc, []))
    return out


def _task_module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _no_log_value(task: dict):
    """Return the task's ``no_log`` as ``True`` / ``False`` / ``None`` (absent)."""
    if "no_log" not in task:
        return None
    val = task["no_log"]
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in ("false", "no", "0", ""):
        return False
    return True


def _task_body_text(task: dict) -> str:
    """A flat lowercase string of everything the task DOES + how it is named.

    Concatenates the task ``name`` and every module-arg spec (module name + its
    args, incl. ``argv`` / ``line`` / ``msg`` / templated values). Used to
    decide whether a task handles / echoes a secret value.
    """
    parts: list[str] = [_task_name(task)]
    for mod in _task_module_names(task):
        spec = task.get(mod)
        parts.append(str(mod))
        if isinstance(spec, dict):
            for key, value in spec.items():
                parts.append(str(key))
                parts.append(str(value))
        else:
            parts.append(str(spec))
    return " ".join(parts).lower()


# Ansible fact/register names holding a SECRET VALUE (the admin credential, the
# one-time token secret, or the composed full token). A task that references any
# of these in its args is handling the secret VALUE and must be no_log: true.
# NOTE: these are the concrete variable names the role binds (design "Component
# 3" / tasks/main.yml). The token *id* var is deliberately EXCLUDED — naming the
# id is non-secret ("value not shown").
_SECRET_VALUE_VARS = (
    "platform_pve_identity_full_token",
    "platform_pve_identity_secret_uuid",
    "platform_pve_identity_token_add",       # register carrying the JSON w/ .value
    "platform_pve_identity_token_removed",   # force-rotate remove (kept hygienic)
    "proxmox_admin_password",
    "proxmox_admin_token",
    "platform_pve_identity_admin",
)

# The persisted credential key (its LINE carries the token value).
_PERSISTED_TOKEN_KEY = "proxmox_api_token"


def _handles_secret_value(task: dict) -> bool:
    """True if the task reads/registers/templates/persists a secret VALUE.

    Token-id-only references (INFO logs naming the id) are NOT secret-handling —
    they are excluded because _SECRET_VALUE_VARS lists only value-bearing vars,
    and the persist line is matched separately by its PROXMOX_API_TOKEN write.
    """
    body = _task_body_text(task)

    # The register of a secret-bearing command counts as handling the value.
    reg = str(task.get("register", "")).lower()
    if reg in _SECRET_VALUE_VARS:
        return True

    if any(var in body for var in _SECRET_VALUE_VARS):
        return True

    # The persist task writes a line `PROXMOX_API_TOKEN=<token>` — its line
    # carries the value even though the var is templated inside.
    if _is_token_persist_task(task):
        return True

    return False


def _is_token_persist_task(task: dict) -> bool:
    """Identify the lineinfile task that writes PROXMOX_API_TOKEN to cluster.env.

    Located by: a lineinfile module whose args reference PROXMOX_API_TOKEN.
    """
    mods = _task_module_names(task)
    is_lineinfile = "ansible.builtin.lineinfile" in mods or "lineinfile" in mods
    if not is_lineinfile:
        return False
    body = _task_body_text(task)
    return _PERSISTED_TOKEN_KEY in body


def _echoes_secret_value(task: dict) -> bool:
    """True if a debug/fail/msg task would PRINT a secret value.

    A debug/fail/assert whose printed text (``msg`` / ``that`` / ``debug var``)
    references a secret-VALUE variable echoes the secret. Referencing only the
    token *id* var is fine (the sanctioned "value not shown" INFO logs). A
    no_log: true task cannot leak regardless, so it is never an offender here.
    """
    if _no_log_value(task) is True:
        return False

    mods = _task_module_names(task)
    is_echo_module = any(
        m in mods
        for m in (
            "ansible.builtin.debug", "debug",
            "ansible.builtin.fail", "fail",
        )
    )
    if not is_echo_module:
        return False

    # Inspect only the text this task would actually PRINT.
    printed_parts: list[str] = []
    for mod in ("ansible.builtin.debug", "debug"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            for key in ("msg", "var"):
                if key in spec:
                    printed_parts.append(str(spec[key]))
    for mod in ("ansible.builtin.fail", "fail"):
        spec = task.get(mod)
        if isinstance(spec, dict) and "msg" in spec:
            printed_parts.append(str(spec["msg"]))

    printed = " ".join(printed_parts).lower()
    return any(var in printed for var in _SECRET_VALUE_VARS)


def _when_gated_on_flag(when_texts) -> bool:
    """True if any effective ``when`` clause references the opt-in flag."""
    return any(_GATE_FLAG in text for text in when_texts)


# =============================================================================
# Sanity — the scan actually reached the role file and parsed real tasks.
# =============================================================================
def test_role_file_parsed_and_nontrivial():
    """Guard against a silent no-op: the role file must parse to real tasks.

    If the role path moves and the parse yields nothing, the assertions below
    would pass vacuously. Assert the file exists and a non-trivial number of
    leaf tasks were parsed, so an empty scan can never masquerade as 'clean'.
    """
    assert _ROLE_MAIN.is_file(), (
        f"platform_pve_identity role tasks file not found at "
        f"{_ROLE_MAIN.relative_to(_REPO_ROOT)} — the role layout changed; "
        "repoint this guard."
    )
    leaves = _load_leaf_tasks_with_when(_ROLE_MAIN)
    assert len(leaves) > 8, (
        "The platform_pve_identity task scan parsed suspiciously few leaf tasks "
        f"({len(leaves)}); a parse regression may be hiding tasks from the "
        "opt-in / secret-hygiene checks."
    )


# =============================================================================
# Assertion 1 — every PVE-identity task is gated on bootstrap_pve_identity.
# =============================================================================
def test_every_task_gated_on_opt_in_flag():
    """Property 2 — Validates: Requirement 2.1.

    Every PVE-identity task MUST be gated ``when: bootstrap_pve_identity``
    (directly or by inheriting an enclosing block's ``when``). The role wraps
    its whole body in one top-level block gated on the flag, so every leaf task
    inherits it. A task that is NOT gated (e.g. a new task added outside the
    opt-in block) would run even when the operator did not opt in — a Req 2.1
    regression, since routine host runs must neither require nor read an
    Admin_Bootstrap_Credential.
    """
    ungated: list[str] = []
    for task, when_texts in _load_leaf_tasks_with_when(_ROLE_MAIN):
        if not _when_gated_on_flag(when_texts):
            ungated.append(f"  - {_task_name(task)!r}")

    assert not ungated, (
        "OPT-IN REGRESSION: found platform_pve_identity task(s) NOT gated on "
        f"`when: {_GATE_FLAG}` (directly or via an enclosing block's `when`). "
        "Every task in this role must be reachable ONLY when the operator opts "
        "in with -e bootstrap_pve_identity=true (Req 2.1); an ungated task "
        "would run on a routine host bucket that supplied no admin context. "
        "Offending task(s):\n" + "\n".join(ungated)
    )


# =============================================================================
# Assertion 2 — every secret-value-handling task is no_log: true.
# =============================================================================
def test_secret_handling_tasks_are_no_log_true():
    """Property 2 — Validates: Requirements 2.3, 2.4.

    Every task that reads / registers / templates / persists the
    Admin_Bootstrap_Credential OR the minted token value MUST be
    ``no_log: true`` (not false, not missing). This covers: the token-add child
    run (register carries the one-time secret in its JSON), the compose
    set_fact (binds the full token + secret uuid), the capture assert, the
    force-rotate remove, and the lineinfile that persists PROXMOX_API_TOKEN.
    Non-secret tasks (role/user/acl/token-LIST, INFO debug naming only the id)
    are exempt because they never touch a secret VALUE.
    """
    offenders: list[str] = []
    for task, _when in _load_leaf_tasks_with_when(_ROLE_MAIN):
        if not _handles_secret_value(task):
            continue
        if _no_log_value(task) is not True:
            state = "missing no_log" if _no_log_value(task) is None else "no_log: false"
            offenders.append(f"  - {_task_name(task)!r} ({state})")

    assert not offenders, (
        "SECRET-HYGIENE REGRESSION: found task(s) that handle the admin "
        "credential or the minted token VALUE but are not `no_log: true` "
        "(Req 2.3, 2.4). Every such task must suppress its output so the "
        "credential/token never reaches stdout/stderr/log. Offending task(s):\n"
        + "\n".join(offenders)
    )


# =============================================================================
# Assertion 3 — the persist task writes mode 0600.
# =============================================================================
def test_token_persist_task_writes_mode_0600():
    """Property 2 — Validates: Requirement 2.3.

    The persist task that writes PROXMOX_API_TOKEN to cluster.env via
    ``lineinfile`` MUST set ``mode: "0600"``, mirroring how the SVC-07 installer
    persists OPENBAO_ADMIN_TOKEN. A world-/group-readable persisted credential
    is a Req 2.3 regression.
    """
    persist_tasks = [
        task
        for task, _when in _load_leaf_tasks_with_when(_ROLE_MAIN)
        if _is_token_persist_task(task)
    ]

    assert persist_tasks, (
        "Could not locate the PROXMOX_API_TOKEN persist task in "
        f"{_ROLE_MAIN.relative_to(_REPO_ROOT)} — expected a `lineinfile` task "
        "whose args reference PROXMOX_API_TOKEN. If it was renamed/moved, update "
        "this guard AND confirm the persist still writes mode 0600."
    )

    for task in persist_tasks:
        mods = _task_module_names(task)
        spec = None
        for mod in ("ansible.builtin.lineinfile", "lineinfile"):
            if mod in mods:
                spec = task.get(mod)
                break
        mode = None
        if isinstance(spec, dict):
            mode = spec.get("mode")
        assert str(mode) == "0600", (
            "PERSIST-MODE REGRESSION: the PROXMOX_API_TOKEN persist task "
            f"({_task_name(task)!r}) must write `mode: \"0600\"` — found "
            f"{mode!r} (Req 2.3). A minted credential persisted at a laxer mode "
            "is world-/group-readable."
        )


# =============================================================================
# Assertion 5 — the least-privilege priv set is correct (SDN.Use present,
#               never Administrator). Reads defaults/main.yml.
# =============================================================================
def test_privilege_set_includes_sdn_use_and_never_administrator():
    """Property 2 — Validates: Requirement 2.2 (least-privilege priv set).

    The platform-minted `terraform@pve` token must PLACE guests onto the shared
    VLAN VNet, so its role's priv set MUST include `SDN.Use` (guest-NIC attach,
    NET-00 §6) in ADDITION to `SDN.Allocate` (SDN create/manage, ADR-0001) and
    `SDN.Audit` (view-only). Without `SDN.Use`, a service apply (SVC-07) fails at
    container create with HTTP 403 (.../p20, SDN.Use) — the gap surfaced by the
    live 14.2 SVC-07 apply and recorded in ADR-0009.

    Least-privilege still holds: the set must NEVER contain the built-in
    `Administrator` role (security-standards.md "Least-privilege service
    accounts", PF FR-1).
    """
    assert _ROLE_DEFAULTS.is_file(), (
        f"platform_pve_identity defaults file not found at "
        f"{_ROLE_DEFAULTS.relative_to(_REPO_ROOT)} — the role layout changed; "
        "repoint this guard."
    )
    data = yaml.safe_load(_ROLE_DEFAULTS.read_text(encoding="utf-8"))
    privs_raw = str(data.get("platform_pve_identity_privs", ""))
    privs = set(privs_raw.split())

    for required in ("SDN.Allocate", "SDN.Audit", "SDN.Use"):
        assert required in privs, (
            f"PRIV-SET REGRESSION: `platform_pve_identity_privs` must include "
            f"{required!r} (Req 2.2, NET-00 §6, ADR-0001/ADR-0009). SDN.Use is "
            "required so the platform-minted token can attach a guest NIC to the "
            "existing shared VNet (p20); without it the SVC-07 service apply is "
            f"denied with HTTP 403 at container create. Found privs: {sorted(privs)}"
        )

    assert "Administrator" not in privs, (
        "LEAST-PRIVILEGE REGRESSION: `platform_pve_identity_privs` must NEVER "
        "contain the built-in `Administrator` role (security-standards.md "
        "'Least-privilege service accounts', PF FR-1). Grant only the narrow "
        f"VM/LXC/Datastore/SDN/Sys/Pool privileges. Found privs: {sorted(privs)}"
    )


# =============================================================================
# Assertion 4 — no task echoes the admin credential or the token value.
# =============================================================================
def test_no_task_echoes_the_admin_cred_or_token_value():
    """Property 2 — Validates: Requirements 2.3, 2.4.

    No debug/fail/msg task may PRINT the admin credential, the composed token,
    or the one-time secret uuid. The INFO decision logs are allowed to reference
    the token *id* + "value not shown" only. A debug that printed
    ``platform_pve_identity_full_token`` / ``...secret_uuid`` / the admin
    password would leak the secret into the run log — a Req 2.3/2.4 regression.
    (A `no_log: true` echo task cannot leak and is not an offender.)
    """
    offenders: list[str] = []
    for task, _when in _load_leaf_tasks_with_when(_ROLE_MAIN):
        if _echoes_secret_value(task):
            offenders.append(f"  - {_task_name(task)!r}")

    assert not offenders, (
        "SECRET-ECHO REGRESSION: found debug/fail task(s) that print the admin "
        "credential or the token VALUE (Req 2.3, 2.4). INFO logs may reference "
        "the token id + 'value not shown' only, never a secret value. Offending "
        "task(s):\n" + "\n".join(offenders)
    )
