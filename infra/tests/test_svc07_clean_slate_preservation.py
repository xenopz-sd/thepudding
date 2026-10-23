"""Offline (Tier-1) preservation guards for the service clean-slate residue fix.

Spec: .kiro/specs/fix-svc07-clean-slate-terraform-data-residue/
(bugfix.md Req 3.1-3.6, design.md "Preservation Requirements" / Property 2).
This file is **Task 2**: observe the UNFIXED clean-slate behavior and encode it,
so the residue fix (Task 3) can be proven to preserve it (Task 3.6).

Property 2 (Preservation) is a universal claim over the NON-bug input domain:
for any input that does NOT trigger the residue (a run that should be
refused/aborted, a re-run against an empty baseline, a normal apply), the fixed
code must behave exactly as the original. The behaviors below are STRUCTURAL
invariants of the committed roles, so they are pinned by parsing the role
artifacts (PyYAML) — no live cluster — mirroring the repo's drift-guard style
(``test_terraform_clean_slate.py``, ``test_platform_clean_slate_wiring.py``).

These tests MUST PASS on the unfixed code (they capture the baseline to
preserve) and MUST STILL PASS after the fix.

Covered preservation requirements:
  3.1 Guest-destroy ordering: ``pct destroy --purge`` THEN ``terraform state rm``.
  3.2 Confirmation gate: declined/empty/timed-out prompt aborts, destroys nothing.
  3.3 Agent-live throwaway guard: consulted FIRST, fail-closed, before any destroy.
  3.4 Idempotent re-run: already-absent guest/state entry tolerated.
  3.5 Apply-time anchor recreation: the inert terraform_data anchors are normal
      plan resources (a normal apply recreates them).
  3.6 Platform state_ignore_patterns tolerance NOT weakened.

Run:
  PYTHONPATH=infra/platform-foundation/scripts \\
    ~/venv/devinfra/bin/pytest infra/tests/test_svc07_clean_slate_preservation.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"
_RESET = _ROLES / "terraform_clean_slate" / "tasks" / "reset_target.yml"
_PLATFORM_CS_DEFAULTS = _ROLES / "platform_clean_slate" / "defaults" / "main.yml"

# The per-service clean-slate roles the fix touches.
_SERVICE_ROLES = ["svc07_clean_slate", "svc09_clean_slate", "svc01_clean_slate", "svc06_clean_slate"]


def _role_main(role_name: str) -> list[dict]:
    text = (_ROLES / role_name / "tasks" / "main.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _role_text(role_name: str) -> str:
    return (_ROLES / role_name / "tasks" / "main.yml").read_text(encoding="utf-8")


def _when_text(task: dict) -> str:
    w = task.get("when")
    if w is None:
        return ""
    if isinstance(w, (list, tuple)):
        return " ".join(str(x) for x in w)
    return str(w)


def _argv_text(task: dict) -> str:
    parts: list[str] = []
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if isinstance(spec, str):
            parts.append(spec)
        elif isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                parts.append(" ".join(str(a) for a in argv))
            elif isinstance(argv, str):
                parts.append(argv)
            if isinstance(spec.get("cmd"), str):
                parts.append(spec["cmd"])
    return " ".join(parts)


def _is_module(task: dict, *names: str) -> bool:
    return any(n in task for n in names)


def _assert_that(task: dict) -> str:
    spec = task.get("ansible.builtin.assert") or task.get("assert") or {}
    if not isinstance(spec, dict):
        return str(spec)
    that = spec.get("that")
    if isinstance(that, (list, tuple)):
        return " ".join(str(t) for t in that)
    return str(that)


# =============================================================================
# 3.1 — Guest-destroy ordering: pct destroy --purge BEFORE terraform state rm.
# Observed in terraform_clean_slate/reset_target.yml (the canonical destroy).
# Validates: Requirements 3.1 / Property 2.
# =============================================================================
def test_guest_destroy_ordering_pct_destroy_before_state_rm():
    """Validates: Requirements 3.1 / Property 2.

    The canonical per-target reset must ``pct destroy --purge`` the guest BEFORE
    ``terraform state rm`` of its container address (fail-safe: a pct failure
    must not leave a state-rm'd-but-running orphan). This is the shared
    ``terraform_clean_slate`` role the fix must not disturb.
    """
    text = _RESET.read_text(encoding="utf-8")
    assert "pct destroy" in text and "--purge" in text, (
        "reset_target.yml must `pct destroy --purge` the guest"
    )
    pct_i = text.index("pct destroy")
    rm_i = text.index("terraform state rm")
    assert pct_i < rm_i, (
        "pct destroy must run BEFORE terraform state rm (destroy-first fail-safe)"
    )


# =============================================================================
# 3.2 — Confirmation gate: declined/empty/timed-out prompt aborts, destroys
# nothing, before any delegated destroy. Observed in each service role.
# Validates: Requirements 3.2 / Property 2.
# =============================================================================
@pytest.mark.parametrize("role_name", _SERVICE_ROLES)
def test_confirmation_gate_aborts_before_any_destroy(role_name: str):
    """Validates: Requirements 3.2 / Property 2.

    Each service clean-slate must prompt (unless ``clean_slate_confirm=force``),
    resolve an affirmative, and FAIL (abort, non-zero) on a non-affirmative/empty
    response BEFORE the delegated reset. The not-confirmed ``fail`` must precede
    the first child ``clean-slate.yml`` delegate run.
    """
    tasks = _role_main(role_name)

    pause_idx = next(
        (i for i, t in enumerate(tasks) if _is_module(t, "ansible.builtin.pause", "pause")),
        None,
    )
    assert pause_idx is not None, f"{role_name}: no confirmation pause task"
    assert "clean_slate_confirm" in _when_text(tasks[pause_idx]) and "force" in _when_text(tasks[pause_idx]), (
        f"{role_name}: the pause must be gated `when: clean_slate_confirm != 'force'`"
    )

    fail_idx = next(
        (i for i, t in enumerate(tasks)
         if _is_module(t, "ansible.builtin.fail", "fail") and "confirmed" in _when_text(t)),
        None,
    )
    assert fail_idx is not None, f"{role_name}: no not-confirmed abort `fail`"

    delegate_idx = next(
        (i for i, t in enumerate(tasks) if "clean-slate.yml" in _argv_text(t)),
        None,
    )
    assert delegate_idx is not None, f"{role_name}: no delegated clean-slate.yml run"
    assert fail_idx < delegate_idx, (
        f"{role_name}: the not-confirmed abort (index={fail_idx}) must precede the "
        f"delegated destroy (index={delegate_idx}) — a declined/timed-out prompt "
        "must abort destroying nothing (Req 3.2)"
    )


# =============================================================================
# 3.3 — Agent-live throwaway guard: consulted FIRST, fail-closed, before any
# destroy, gated on agent_live_run. Observed in each service role.
# Validates: Requirements 3.3 / Property 2.
# =============================================================================
@pytest.mark.parametrize("role_name", _SERVICE_ROLES)
def test_agent_live_guard_consulted_first_and_fail_closed(role_name: str):
    """Validates: Requirements 3.3 / Property 2.

    When ``AGENT_LIVE_AUTHORIZED`` is armed (``agent_live_run``), the throwaway
    guard must be consulted FIRST and the reset REFUSED unless the guard
    authorized it (``*throwaway_decision.rc == 0``) — before the confirmation
    prompt and before any destroy. An ordinary operator run skips it.
    """
    tasks = _role_main(role_name)

    consult_idx = next(
        (i for i, t in enumerate(tasks) if "throwaway_guard" in _argv_text(t)),
        None,
    )
    assert consult_idx is not None, f"{role_name}: no throwaway-guard consult"
    assert "agent_live_run" in _when_text(tasks[consult_idx]), (
        f"{role_name}: the guard consult must be gated `when: agent_live_run`"
    )

    rc_assert_idx = next(
        (i for i, t in enumerate(tasks)
         if (t.get("ansible.builtin.assert") or t.get("assert"))
         and "throwaway_decision.rc" in _assert_that(t)),
        None,
    )
    assert rc_assert_idx is not None, f"{role_name}: no guard rc-assert (fail-closed REFUSED)"
    assert "agent_live_run" in _when_text(tasks[rc_assert_idx]), (
        f"{role_name}: the guard rc-assert must be gated `when: agent_live_run`"
    )

    # The guard consult + rc-assert must precede the first destructive marker
    # (the delegated clean-slate.yml run).
    delegate_idx = next(
        (i for i, t in enumerate(tasks) if "clean-slate.yml" in _argv_text(t)),
        None,
    )
    assert delegate_idx is not None, f"{role_name}: no delegated clean-slate.yml run"
    assert consult_idx < delegate_idx and rc_assert_idx < delegate_idx, (
        f"{role_name}: the throwaway-guard consult + rc-assert must precede any "
        "destroy (Req 3.3)"
    )


# =============================================================================
# 3.4 — Idempotent re-run: already-absent guest + already-absent state entry
# tolerated. Observed in reset_target.yml failed_when guards.
# Validates: Requirements 3.4 / Property 2.
# =============================================================================
def test_idempotent_reset_tolerates_absent_guest_and_state():
    """Validates: Requirements 3.4 / Property 2.

    The canonical reset must tolerate an already-absent guest (``pct destroy``
    'does not exist') and an already-absent state entry (``terraform state rm``
    'No matching objects'), so a re-run against an empty/partial baseline
    converges rather than erroring.
    """
    tasks = yaml.safe_load(_RESET.read_text(encoding="utf-8"))

    destroy = next(
        (t for t in tasks if "pct destroy" in _argv_text(t)), None
    )
    assert destroy is not None, "no pct destroy task"
    destroy_fw = " ".join(str(x) for x in (destroy.get("failed_when") or []))
    assert "does not exist" in destroy_fw, (
        "pct destroy must tolerate an already-absent guest ('does not exist')"
    )

    state_rm = next(
        (t for t in tasks if "terraform state rm" in _argv_text(t)), None
    )
    assert state_rm is not None, "no terraform state rm task"
    rm_fw = " ".join(str(x) for x in (state_rm.get("failed_when") or []))
    assert "No matching objects" in rm_fw, (
        "terraform state rm must tolerate an already-absent entry "
        "('No matching objects') — idempotent re-run (Req 3.4)"
    )


# =============================================================================
# 3.5 — Apply-time anchor recreation: the inert terraform_data anchors are
# ordinary plan resources (no prevent_destroy, no clean-slate coupling), so a
# normal apply recreates them. Observed in the affected roots + the module.
# Validates: Requirements 3.5 / Property 2.
# =============================================================================
def test_terraform_data_anchors_are_plain_recreatable_resources():
    """Validates: Requirements 3.5 / Property 2.

    The ledger-check and input_guards ``terraform_data`` resources must be
    ordinary resources with NO ``prevent_destroy`` — so removing them at
    clean-slate time changes nothing about their apply-time behavior: a normal
    ``terraform apply`` recreates them as part of the plan.
    """
    svc07 = (_REPO_ROOT / "infra" / "projects" / "svc-07-secrets-manager" / "main.tf").read_text(encoding="utf-8")
    assert 'resource "terraform_data" "svc07_host_index_ledger_check"' in svc07

    # Isolate the ledger-check block and assert it carries no prevent_destroy.
    start = svc07.index('resource "terraform_data" "svc07_host_index_ledger_check"')
    # crude brace-balance to the end of the block
    depth = 0
    body_start = svc07.index("{", start)
    end = body_start
    for i in range(body_start, len(svc07)):
        if svc07[i] == "{":
            depth += 1
        elif svc07[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    ledger_block = svc07[start:end + 1]
    # Check for the HCL directive, not the bare substring — the block's
    # error_message prose legitimately mentions the words "prevent_destroy
    # anchor" when describing the stateful CONTAINERS.
    assert "prevent_destroy = true" not in ledger_block, (
        "the ledger-check terraform_data must NOT carry `prevent_destroy = true` "
        "— it is an inert plan anchor that reappears on the next apply (Req 3.5)"
    )

    preconditions = (_REPO_ROOT / "infra" / "modules" / "proxmox-compute" / "preconditions.tf").read_text(encoding="utf-8")
    assert 'resource "terraform_data" "input_guards"' in preconditions
    assert "prevent_destroy = true" not in preconditions, (
        "the input_guards terraform_data anchor must not carry "
        "`prevent_destroy = true` (Req 3.5)"
    )


# =============================================================================
# 3.6 — Platform state_ignore_patterns tolerance NOT weakened by this fix.
# The platform clean-slate catalog must still tolerate the inert anchors.
# Validates: Requirements 3.6 / Property 2.
# =============================================================================
def test_platform_state_ignore_patterns_tolerance_preserved():
    """Validates: Requirements 3.6 / Property 2.

    This fix removes the residue at the SERVICE clean-slate layer; it must NOT
    weaken the platform clean-slate's existing ``state_ignore_patterns``
    tolerance of the inert ``terraform_data.input_guards`` / ``registry_watch``
    anchors. Assert the catalog still carries those tolerances (the platform
    guard's behavior is unchanged by this fix).
    """
    defaults = yaml.safe_load(_PLATFORM_CS_DEFAULTS.read_text(encoding="utf-8")) or {}

    services = defaults.get("platform_clean_slate_services") or []
    svc07 = next((r for r in services if isinstance(r, dict) and r.get("service") == "svc07"), None)
    assert svc07 is not None, "no svc07 row in platform_clean_slate_services"
    ignore = svc07.get("state_ignore_patterns") or []
    assert any("terraform_data.input_guards" in str(p) for p in ignore), (
        "the platform SVC-07 row must STILL tolerate terraform_data.input_guards "
        "(this fix must not weaken the platform state_ignore_patterns — Req 3.6)"
    )

    foundation_ignore = defaults.get("platform_clean_slate_foundation_state_ignore_patterns") or []
    assert any("terraform_data.registry_watch" in str(p) for p in foundation_ignore), (
        "the platform-foundation ignore patterns must STILL tolerate "
        "terraform_data.registry_watch (Req 3.6)"
    )
