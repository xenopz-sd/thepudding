"""Offline (structural) tests for fix-svc07-empty-transit-token-handoff (§5c).

Bug: a fresh from-scratch SVC-07 bring-up fails Phase 3 with "No
Bootstrap_Transit_Token is available for the primary" because the unsealer's
STEP-6 seal-token mint (`bao token create ... -field=token`) can return rc==0
with an EMPTY/not-yet-readable token on the first attempt — but STEP-6 has NO
`until:`/`retries:` readiness-retry and NO non-empty guard, so the empty value is
captured, written to the 0600 Handoff_File, and surfaces as the downstream
fail-closed assert. The earlier steps (STEP-1 pre-init poll, STEP-3 post-unseal
wait, STEP-3b re-unseal) DO carry readiness retries — the mint is the one
unguarded producer.

Fix: STEP-6 gets a bounded `until:`/`retries:`/`delay:` (reusing
openbao_readiness_retries/openbao_readiness_delay) keyed on a non-empty token +
rc==0, with `failed_when: false`; a new STEP-6b non-empty assert fails closed
(no_log, named fail_msg) on an exhausted budget; STEP-7's handoff gains a
non-empty-token gate.

These tests are PARSE-ONLY and never embed/print a secret value.

Run: ~/venv/devinfra/bin/pytest infra/tests/test_svc07_empty_transit_token_handoff.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UNSEALER = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal" / "tasks" / "unsealer_bootstrap.yml"
)


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _task_text(t: dict) -> str:
    return str(t).lower()


def _mint_task(tasks):
    """The STEP-6 `bao token create ... -field=token` mint task."""
    for t in tasks:
        body = _task_text(t)
        if "token" in body and "create" in body and "field=token" in body:
            return t
    return None


def _handoff_task(tasks):
    """The STEP-7 Handoff_File copy task."""
    for t in tasks:
        cp = t.get("ansible.builtin.copy")
        if isinstance(cp, dict) and "openbao_seal_token_handoff_path" in str(cp.get("dest", "")):
            return t
    return None


# --------------------------------------------------------------------------- #
# Property 1 — Bug Condition / Expected Behavior.
# On unfixed code these FAIL (no until/retries on the mint; no non-empty guard).
# --------------------------------------------------------------------------- #
def test_mint_task_exists():
    assert _mint_task(_load_tasks(_UNSEALER)) is not None, (
        "the STEP-6 seal-token mint task (bao token create -field=token) must exist"
    )


def test_mint_has_readiness_retry_keyed_on_nonempty_token():
    """Property 1: the mint must retry within the readiness budget until the token
    is non-empty. FAILS on unfixed code (no until/retries)."""
    mint = _mint_task(_load_tasks(_UNSEALER))
    assert mint is not None
    assert "until" in mint, (
        "the STEP-6 mint must carry `until:` keyed on a non-empty token — a "
        "transient first-attempt empty read must be retried, not propagated "
        "(FAILS on unfixed code)"
    )
    until = str(mint["until"])
    # Keyed on the minted token being non-empty AND rc==0.
    assert "length" in until and "stdout" in until, (
        "the `until:` predicate must check the minted token stdout is non-empty"
    )
    assert "rc == 0" in until or "rc==0" in until, (
        "the `until:` predicate must also require rc == 0"
    )
    # Bounded by the REUSED readiness budget (no new/unbounded wait).
    assert "openbao_readiness_retries" in str(mint.get("retries", "")), (
        "retries must reuse openbao_readiness_retries (no new constant)"
    )
    assert "openbao_readiness_delay" in str(mint.get("delay", "")), (
        "delay must reuse openbao_readiness_delay (no new constant)"
    )
    # failed_when: false so an interim empty read is a RETRY, not a hard failure.
    assert mint.get("failed_when") is False, (
        "the retried mint must set failed_when: false — the non-empty outcome is "
        "enforced by the STEP-6b assert, not the task rc"
    )
    # changed_when + no_log preserved.
    assert "rc == 0" in str(mint.get("changed_when", "")), "changed_when: rc==0 must be preserved"
    assert "openbao_no_log" in str(mint.get("no_log", "")), "no_log must be preserved on the mint"


def test_nonempty_assert_before_handoff():
    """Property 1 (fail-closed on exhausted budget): a non-empty assert on
    openbao_seal_token must exist, no_log, naming the unsealer + empty-mint
    condition. FAILS on unfixed code (no such assert)."""
    tasks = _load_tasks(_UNSEALER)
    guard = None
    for t in tasks:
        if "ansible.builtin.assert" not in t:
            continue
        body = _task_text(t)
        if "openbao_seal_token" in body and "length" in body:
            guard = t
            break
    assert guard is not None, (
        "a STEP-6b non-empty assert on openbao_seal_token must fail closed on an "
        "exhausted retry budget (FAILS on unfixed code)"
    )
    assert "openbao_no_log" in str(guard.get("no_log", "")) or guard.get("no_log") is True, (
        "the non-empty assert must be no_log (it references the token fact)"
    )
    fail_msg = str(guard.get("ansible.builtin.assert", {}).get("fail_msg", "")).lower()
    assert "unsealer" in fail_msg or "container" in fail_msg, (
        "the fail_msg must name the unsealer container + the empty-mint condition"
    )


def test_handoff_gated_on_nonempty_token():
    """Property 1 (belt-and-suspenders): STEP-7 must not write an empty token to
    the Handoff_File. FAILS on unfixed code (gated only on the opt-in flags)."""
    handoff = _handoff_task(_load_tasks(_UNSEALER))
    assert handoff is not None
    when = handoff.get("when")
    when_list = [when] if isinstance(when, str) else list(when or [])
    joined = " ".join(str(w) for w in when_list)
    assert "openbao_seal_token" in joined and "length" in joined, (
        "the handoff `when:` must include a non-empty-token condition in addition "
        "to the opt-in flags (FAILS on unfixed code)"
    )


# --------------------------------------------------------------------------- #
# Property 2 — Preservation.
# --------------------------------------------------------------------------- #
def test_earlier_readiness_guards_preserved():
    """STEP-1/STEP-3/STEP-3b readiness `until:` loops remain (Req 3.1)."""
    tasks = _load_tasks(_UNSEALER)
    until_tasks = [t for t in tasks if "until" in t]
    # At least the three earlier readiness waits + the new mint retry.
    assert len(until_tasks) >= 4, (
        f"expected the earlier readiness guards + the new mint retry; found "
        f"{len(until_tasks)} until-loops"
    )
    text = _UNSEALER.read_text(encoding="utf-8")
    assert "initialized" in text and "sealed" in text, (
        "the post-unseal initialized:true/sealed:false wait must be preserved"
    )


def test_handoff_opt_in_gating_preserved():
    """STEP-7 keeps its opt-in-flag gating + 0600 + delegate_to localhost (Req 3.3)."""
    handoff = _handoff_task(_load_tasks(_UNSEALER))
    assert handoff is not None
    joined = " ".join(str(w) for w in (handoff.get("when") or []))
    assert "openbao_mint_seal_token" in joined
    assert "openbao_dev_expose_secrets" in joined
    assert "openbao_seal_token_handoff_path" in joined
    assert str(handoff.get("delegate_to")) == "localhost"
    cp = handoff["ansible.builtin.copy"]
    assert str(cp.get("mode")) == "0600"


def test_root_revoke_and_scrub_preserved():
    """STEP-8 root-revoke + scrub preserved (Req 3.6)."""
    text = _UNSEALER.read_text(encoding="utf-8")
    assert "revoke" in text.lower(), "the unsealer root-token revoke must be preserved"
    assert "openbao_seal_token: \"\"" in text or "openbao_seal_token: ''" in text, (
        "the in-memory seal-token scrub must be preserved"
    )


def test_periodic_orphan_scoped_token_preserved():
    """The mint stays a periodic (-period) orphan token against the scoped
    openbao-seal policy (Req 3.6)."""
    mint = _mint_task(_load_tasks(_UNSEALER))
    body = _task_text(mint)
    assert "-orphan" in body
    assert "period" in body
    assert "openbao_seal_policy_name" in body
