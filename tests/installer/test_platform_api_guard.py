"""Offline structural drift-guard for the platform_api token precondition.

Spec: .kiro/specs/platform-prerequisites-bootstrap/ (requirements.md, design.md,
tasks.md). Implements SPEC TASK 7.2 (Req 4.2, 1.5 / Property 4). Guards the role
implemented in Task 7.1: ``ansible/roles/platform_api/tasks/main.yml``.

WHAT THIS GUARDS — the api bucket applies the platform-foundation SDN VLAN fabric
via a child-process ``terraform apply``. Before that apply runs, the role MUST
resolve the ``terraform@pve`` token from ``cluster.env`` and, when it is ABSENT,
HALT with a fail-closed assert naming the host bucket's PVE-identity step — so an
api-alone run without a resolvable token never touches Proxmox (Req 4.2,
Property 4: "the token-precondition assert precedes the terraform apply").

This is a pure-logic guard (PyYAML parse of the committed role) — no
infrastructure needed. It PARSES YAML only; it never embeds or prints any secret
value. It matches the repo's drift-guard style (see
``tests/installer/test_unsealer_reseal_ordering.py`` /
``test_unsealer_volume_ownership_ordering.py``): helpers are copied locally, no
cross-test-tree imports.

Test cases:
* 1 — Token-precondition-assert-precedes-apply (PRIMARY): the fail-closed
  ``assert``/``fail`` task that halts naming the host bucket when the
  ``terraform@pve`` token is absent is ordered BEFORE the ``terraform apply``
  task in the role's task-list order.
* 2 — Assert-names-the-host-bucket-remedy (corroborating): the precondition
  gate's failure message points the operator at the host bucket's PVE-identity
  step / ``PROXMOX_API_TOKEN`` remedy, so the halt is actionable.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_platform_api_guard.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_API_MAIN = (
    _REPO_ROOT / "ansible" / "roles" / "platform_api" / "tasks" / "main.yml"
)


# --------------------------------------------------------------------------- #
# Helpers — same PyYAML idiom as the sibling ordering drift-guards. COPIED
# locally per the repo's drift-guard convention (no cross-test-tree imports).
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
    """Return a flat string of a command/shell task's argv (+ stdin), else ''."""
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

    Token-precise (NOT substring) matching so ``terraform`` / ``apply`` match the
    actual argv tokens rather than a substring inside a variable name.
    """
    raw = _task_argv_text(task)
    toks: list[str] = []
    for piece in raw.replace(",", " ").replace("'", " ").split():
        toks.append(piece.strip("[](){}'\""))
    return toks


def _is_assert_or_fail(task: dict) -> bool:
    """True if the task is an ``assert`` or ``fail`` gate task."""
    mods = _task_module_names(task)
    return (
        "ansible.builtin.assert" in mods
        or "assert" in mods
        or "ansible.builtin.fail" in mods
        or "fail" in mods
    )


def _gate_message_text(task: dict) -> str:
    """Flatten an assert/fail task's messages into one lowercase string.

    Covers ``assert.fail_msg``/``success_msg``/``that`` and ``fail.msg`` in both
    FQCN and bare forms.
    """
    parts: list[str] = []
    for mod in ("ansible.builtin.assert", "assert"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            for key in ("fail_msg", "success_msg", "msg", "that", "quiet"):
                if key in spec:
                    parts.append(str(spec[key]))
    for mod in ("ansible.builtin.fail", "fail"):
        spec = task.get(mod)
        if isinstance(spec, dict) and "msg" in spec:
            parts.append(str(spec["msg"]))
        elif isinstance(spec, str):
            parts.append(spec)
    parts.append(str(task.get("name", "")))
    return " ".join(parts).lower()


def _is_token_precondition_gate(task: dict) -> bool:
    """True if the task is the fail-closed terraform@pve token precondition.

    It is an ``assert``/``fail`` gate whose message references BOTH the
    terraform@pve token (the thing that is absent) AND the host bucket / PVE
    identity remedy (what to run to obtain it) — the halt-naming-the-host-bucket
    precondition from Req 4.2 / Property 4.
    """
    if not _is_assert_or_fail(task):
        return False
    text = _gate_message_text(task)
    mentions_token = ("terraform@pve" in text) or ("token" in text)
    mentions_host_remedy = (
        "host bucket" in text
        or "pve-identity" in text
        or "pve identity" in text
        or "bootstrap_pve_identity" in text
        or "proxmox_api_token" in text
    )
    return mentions_token and mentions_host_remedy


def _is_terraform_apply(task: dict) -> bool:
    """A ``terraform apply`` command task (token-precise argv match)."""
    toks = _argv_tokens(task)
    return "terraform" in toks and "apply" in toks


# --------------------------------------------------------------------------- #
# Test case 1 — Token-precondition-assert-precedes-apply (PRIMARY).
# Validates: Requirements 4.2, 1.5 / Property 4.
# --------------------------------------------------------------------------- #
def test_token_precondition_assert_precedes_terraform_apply():
    """Drift guard (Property 4) — Validates: Requirements 4.2, 1.5.

    ``platform_api/tasks/main.yml`` MUST order its fail-closed terraform@pve
    token-precondition ``assert`` (which halts naming the host bucket when the
    token is absent) BEFORE the ``terraform apply`` task. This guarantees an
    api-alone run with no resolvable token HALTS before any Terraform runs, so no
    SDN resource is ever created without a token.

    If a future edit moves the apply above the precondition (or drops the
    precondition), this assertion FAILS — catching the drift that would let a
    tokenless apply reach Proxmox.
    """
    tasks = _load_task_file(_PLATFORM_API_MAIN)

    apply_idx = next(
        (i for i, t in enumerate(tasks) if _is_terraform_apply(t)), None
    )
    assert apply_idx is not None, (
        "No `terraform apply` command task found in platform_api/tasks/main.yml "
        "— cannot anchor the token-precondition ordering check."
    )

    gate_idx = next(
        (i for i, t in enumerate(tasks) if _is_token_precondition_gate(t)), None
    )
    assert gate_idx is not None, (
        "DRIFT: no fail-closed terraform@pve token-precondition assert/fail was "
        "found in platform_api/tasks/main.yml. Req 4.2 / Property 4 requires a "
        "gate that halts naming the host bucket's PVE-identity step (or the "
        "PROXMOX_API_TOKEN remedy) when the token is absent, BEFORE any "
        "`terraform apply` — so a tokenless api-alone run never touches Proxmox."
    )

    assert gate_idx < apply_idx, (
        "DRIFT (Property 4): the terraform@pve token-precondition assert is "
        f"ordered at/after the `terraform apply` (precondition index={gate_idx}, "
        f"apply index={apply_idx}). The precondition MUST precede the apply so an "
        "api-alone run with no resolvable token halts BEFORE any Terraform runs "
        "and before any SDN resource is created (Req 4.2)."
    )


# --------------------------------------------------------------------------- #
# Test case 2 — Assert-names-the-host-bucket-remedy (corroborating).
# Validates: Requirement 4.2 (the halt is actionable).
# --------------------------------------------------------------------------- #
def test_token_precondition_gate_names_host_bucket_remedy():
    """Drift guard (corroborating) — Validates: Requirement 4.2.

    The token-precondition gate's failure message MUST name an actionable remedy
    — the host bucket's PVE-identity step (``-e bootstrap_pve_identity=true``)
    and/or supplying ``PROXMOX_API_TOKEN`` in ``cluster.env`` — so an operator
    hitting the halt knows exactly how to obtain a token, rather than seeing a
    bare assertion failure. It must NOT echo any token value (the gate reads a
    presence-only bool, never the secret).
    """
    tasks = _load_task_file(_PLATFORM_API_MAIN)

    gate = next((t for t in tasks if _is_token_precondition_gate(t)), None)
    assert gate is not None, (
        "No terraform@pve token-precondition gate found in "
        "platform_api/tasks/main.yml (see "
        "test_token_precondition_assert_precedes_terraform_apply)."
    )

    text = _gate_message_text(gate)
    assert ("host bucket" in text) or ("bootstrap_pve_identity" in text) or (
        "pve-identity" in text
    ) or ("proxmox_api_token" in text), (
        "DRIFT: the terraform@pve token-precondition gate's message must name an "
        "actionable remedy (the host bucket's PVE-identity step / "
        "bootstrap_pve_identity=true, or supplying PROXMOX_API_TOKEN in "
        f"cluster.env) — got message text: {text!r}."
    )
