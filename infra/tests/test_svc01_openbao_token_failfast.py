"""Offline structural test — SVC-01 OpenBao-admin-token fail-fast gate.

Spec: .kiro/specs/fix-svc01-db-install-first-run-transient/ (bugfix.md Task-1
Finding + design.md "Fix Implementation"). This pins the fail-fast / diagnosability
contract the fix introduces for the RECLASSIFIED defect.

Task-1 finding (live clean-repro on the `shrimp` throwaway cluster) DISPROVED the
"cold-first-run transient (race)" framing: the sanctioned orchestrator path with
`OPENBAO_ADMIN_TOKEN` exported converged CLEANLY first-try. The NEGATIVE check —
the same cold run with OpenBao WIRED (`openbao_addr` set) but `OPENBAO_ADMIN_TOKEN`
resolving EMPTY — FAILED in Phase 3 with only a generic, censored "non-zero return
code" message, because the `postgres-server` OpenBao read/write is `no_log`. The
confirmed defect is therefore a DIAGNOSABILITY / fail-fast gap, mirroring the merged
obs-precedent `fix-obs-bootstrap-first-run-transient` (the identical
`lookup('env','OPENBAO_ADMIN_TOKEN')` bridging class).

This test asserts the fail-fast contract the fix must introduce in the SVC-01
bootstrap path (`svc01_preflight` and/or `svc01_bootstrap`):

  1. A fail-fast gate (ansible.builtin.assert or ansible.builtin.fail) exists whose
     surrounding condition references BOTH an OpenBao address var
     (openbao_addr / OPENBAO_ADDR) AND the admin token
     (OPENBAO_ADMIN_TOKEN / openbao_admin_token / openbao_token / *_token_present),
     and whose fail_msg/msg contains the literal "OPENBAO_ADMIN_TOKEN" plus a remedy.
  2. That gate is NOT no_log (a fail-fast diagnostic must be visible) and its
     message contains NO secret value — only the var name + path/remedy (it must
     not interpolate a token/password var).

On the UNFIXED tree this FAILS (the gate does not exist yet). After the fix it PASSES.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc01_openbao_token_failfast.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"

# The two candidate role files the gate may live in (design.md: "best placed at the
# END of svc01_preflight, or at the HEAD of svc01_bootstrap before the DB-install
# task"). The gate in EITHER satisfies the contract.
_CANDIDATE_TASK_FILES = [
    _ROLES / "svc01_bootstrap" / "tasks" / "main.yml",
    _ROLES / "svc01_preflight" / "tasks" / "main.yml",
]

_TOKEN_LITERAL = "OPENBAO_ADMIN_TOKEN"

# Condition tokens: an OpenBao ADDRESS reference AND a TOKEN reference must both be
# in the gate's governing condition (when/that).
_ADDR_TOKENS = ("openbao_addr", "OPENBAO_ADDR")
_TOKEN_TOKENS = (
    "openbao_admin_token",
    "OPENBAO_ADMIN_TOKEN",
    "openbao_token",
    "token_present",
)

# Secret-bearing var names that must NEVER be interpolated into a VISIBLE (non
# no_log) diagnostic message.
_SECRET_VALUE_PATTERNS = (
    r"\{\{[^}]*\bopenbao_admin_token\b[^}]*\}\}",
    r"\{\{[^}]*\bOPENBAO_ADMIN_TOKEN\b[^}]*\}\}",
    r"\{\{[^}]*password[^}]*\}\}",
    r"\{\{[^}]*secret[^}]*\}\}",
)


def _iter_tasks(tasks):
    """Flatten tasks, descending into block/rescue/always, carrying each child's
    enclosing block `when` down so a gate inside a block still sees the condition."""
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        block_when = t.get("when")
        for k in ("block", "rescue", "always"):
            if k in t:
                for child in _iter_tasks(t[k]):
                    if block_when is not None and "when" not in child:
                        # annotate inherited block condition for condition-scan
                        child = {**child, "_inherited_when": block_when}
                    yield child


def _load_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _condition_blob(t: dict) -> str:
    """All condition-ish text governing a gate: its `when`, any inherited block
    `when`, and the `that` list of an assert — lowercased."""
    parts = [str(t.get("when", "")), str(t.get("_inherited_when", ""))]
    for mod in ("ansible.builtin.assert", "assert"):
        spec = t.get(mod)
        if isinstance(spec, dict):
            parts.append(str(spec.get("that", "")))
    return " ".join(parts)


def _fail_message(t: dict) -> str | None:
    """The visible diagnostic text of an assert (fail_msg) or fail (msg)."""
    for mod in ("ansible.builtin.assert", "assert"):
        spec = t.get(mod)
        if isinstance(spec, dict):
            msg = spec.get("fail_msg")
            if msg is not None:
                return str(msg)
    for mod in ("ansible.builtin.fail", "fail"):
        spec = t.get(mod)
        if isinstance(spec, dict):
            msg = spec.get("msg")
            if msg is not None:
                return str(msg)
    return None


def _is_fail_fast_task(t: dict) -> bool:
    return any(k in t for k in ("ansible.builtin.assert", "assert",
                                "ansible.builtin.fail", "fail"))


def _present_candidate_files() -> list[Path]:
    present = [p for p in _CANDIDATE_TASK_FILES if p.is_file()]
    if not present:
        pytest.skip(
            "neither svc01_bootstrap nor svc01_preflight tasks/main.yml exists — "
            "SVC-01 role tree absent"
        )
    return present


def _find_token_gate() -> tuple[Path, dict] | None:
    """Locate the fail-fast gate: a fail/assert task whose governing condition
    references BOTH an OpenBao address var AND the token, whose visible message
    names the literal OPENBAO_ADMIN_TOKEN."""
    for path in _present_candidate_files():
        for t in _load_tasks(path):
            if not _is_fail_fast_task(t):
                continue
            cond = _condition_blob(t)
            if not any(a.lower() in cond.lower() for a in _ADDR_TOKENS):
                continue
            if not any(tok.lower() in cond.lower() for tok in _TOKEN_TOKENS):
                continue
            msg = _fail_message(t) or ""
            if _TOKEN_LITERAL in msg:
                return path, t
    return None


def test_wired_but_empty_token_failfast_gate_exists():
    """A fail-fast gate must HALT when OpenBao is wired (address set) but the
    resolved OPENBAO_ADMIN_TOKEN is empty, naming the env var + a remedy.

    UNFIXED tree: no such gate -> FAILS.
    """
    found = _find_token_gate()
    assert found is not None, (
        "SVC-01 bootstrap path (svc01_preflight or svc01_bootstrap) must contain a "
        "fail-fast gate (ansible.builtin.assert / ansible.builtin.fail) whose "
        "condition references BOTH an OpenBao address var (openbao_addr) AND the "
        "admin token, and whose fail_msg/msg names the literal 'OPENBAO_ADMIN_TOKEN' "
        "plus a remedy — so a wired-but-empty token fails CLOSED at preflight instead "
        "of behind the no_log mask of the Phase-3 OpenBao read/write."
    )


def test_token_gate_names_a_remedy():
    """The gate's message must name a remedy (export / cluster env file), not just
    the var name — so the operator knows how to resolve it."""
    found = _find_token_gate()
    assert found is not None, "token fail-fast gate not found (see prior test)"
    _, task = found
    msg = (_fail_message(task) or "").lower()
    assert ("export" in msg) or ("env file" in msg) or ("cluster" in msg) or (
        ".env" in msg
    ), (
        "the wired-but-empty-token gate must name a remedy (export the var into the "
        "process env, or ensure it is present in the cluster env file)."
    )


def test_token_gate_is_visible_not_no_log():
    """A fail-fast DIAGNOSTIC must be VISIBLE — the gate task must NOT be no_log."""
    found = _find_token_gate()
    assert found is not None, "token fail-fast gate not found (see prior test)"
    _, task = found
    assert task.get("no_log") is not True, (
        "the wired-but-empty-token fail-fast gate must NOT be no_log — its diagnostic "
        "must be visible to the operator (it carries only the var name + remedy, no "
        "secret value)."
    )


def test_token_gate_message_contains_no_secret_value():
    """The gate message must carry only the var NAME + path/remedy, never an
    interpolated token/password value."""
    found = _find_token_gate()
    assert found is not None, "token fail-fast gate not found (see prior test)"
    _, task = found
    msg = _fail_message(task) or ""
    for pat in _SECRET_VALUE_PATTERNS:
        assert not re.search(pat, msg), (
            "the fail-fast gate message must not interpolate a token/password value "
            f"(matched secret-value pattern {pat!r}); name the variable only."
        )
