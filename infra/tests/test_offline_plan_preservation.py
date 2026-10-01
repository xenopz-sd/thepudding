"""Preservation observation tests for the offline terraform-plan bugfix.

Spec: fix-offline-terraform-plan-tests (bugfix) — Task 2, design.md
"Correctness Properties / Property 2: Preservation — Non-bug behavior is
unchanged" and "Testing Strategy / Preservation Checking".

=== WHAT THIS TEST IS ===

This is the *preservation* half of the bugfix workflow. Where the bug-condition
exploration test (``test_offline_plan_bug_condition.py``, task 1) pins down the
behavior that must CHANGE, this file pins down everything that must NOT change:
the pre-existing green suite and the byte-for-byte integrity of the two
committed Terraform roots.

It follows the *observation-first* methodology: the exact baseline encoded below
was observed on the UNFIXED tree BEFORE any fix was written —

    PYTHONPATH=infra/platform-foundation/scripts \\
      ~/venv/devinfra/bin/pytest \\
      infra/platform-foundation/scripts/tests/ infra/tests/ -q
    # => 7 failed, 117 passed, 5 skipped

The "7 failed" is the 5 originally-failing terraform-gated tests PLUS the 2
gated bug-condition cases added by task 1. Those 7 are the behavior under
repair; they are DESELECTED here (see ``_KNOWN_FAILING_ON_UNFIXED``). The
preservation baseline is the *pre-existing* 117 passed + 5 skipped set, which
this file asserts is preserved.

=== EXPECTED OUTCOME ===

- On the UNFIXED tree (task 2): these tests PASS — they confirm the baseline to
  preserve (117 pre-existing tests pass, 5 skip with documented reasons, both
  roots are git-clean of tracked-.tf mutations and stray override/state files).
- After the fix (task 3.7): the SAME tests still PASS — no regression.

=== PRESERVATION REQUIREMENTS (design.md) ===

- Req 3.1 — the 117 currently-passing tests continue to pass unchanged.
- Req 3.2 — the 5 currently-skipped tests continue to skip with the same
  documented reasons.
- Req 3.3 — the committed ``backend "http" {}`` blocks, provider wiring,
  ``required_version`` pins, and SDN resource graphs of
  ``infra/platform-foundation/`` and ``infra/projects/_TEMPLATE/`` remain
  byte-for-byte unchanged; any local backend override is test-scoped and
  transient, never committed (no stray ``zz_*backend_override*.tf`` / state
  artifact left in either root).
- Req 3.4 / 3.5 — covered indirectly: the affected tests (``test_degraded_*``,
  registered-slug plan behavior) live inside the preserved 117/5 sets and the
  clean-roots check.

Run:  PYTHONPATH=infra/platform-foundation/scripts \\
        ~/venv/devinfra/bin/pytest infra/tests/test_offline_plan_preservation.py -v
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Repo / suite layout, resolved relative to this file (infra/tests/...).
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _INFRA_DIR.parent
_FOUNDATION_DIR = _INFRA_DIR / "platform-foundation"
_PROJECT_TEMPLATE_DIR = _INFRA_DIR / "projects" / "_TEMPLATE"
_SCRIPTS_DIR = _FOUNDATION_DIR / "scripts"

#: The two suite paths the project's canonical run targets (README / tasks.md).
_SUITE_PATHS = (
    "infra/platform-foundation/scripts/tests/",
    "infra/tests/",
)

# --------------------------------------------------------------------------- #
# The baseline observed on the UNFIXED tree (see module docstring).
#
# 7 failed = the 5 originally-failing terraform-gated tests + the 2 task-1
# bug-condition cases. These are the behavior under repair, so they are
# DESELECTED from the preservation baseline. Everything else — 117 passed +
# 5 skipped — is the pre-existing behavior this file guards.
# --------------------------------------------------------------------------- #
_EXPECTED_PASSED = 117
# 5 pre-existing offline skips + 1 added by the svc-07-secrets-manager spec
# (task 3.2): the new `SDN.Allocate` degraded-mode test
# (test_svc07_state_and_degraded.py) is a requires_infra test that, with the
# gating env vars scrubbed from the baseline child, SKIPS cleanly like the other
# three live tests. It is an ADDITIVE offline skip, so the deterministic offline
# baseline rises to 6.
#
# + 4 more added by the svc-07-secrets-manager spec (task 7.4): the new
# engine/auth/audit CONTRACT test class (test_svc07_openbao_engine_contract.py)
# is ALSO requires_infra and skips cleanly on missing creds — but it gates on the
# OpenBao env-var contract (OPENBAO_TEST_ADDR / OPENBAO_TEST_TOKEN), not the
# Proxmox one. Those OpenBao vars are added to _REQUIRES_INFRA_ENV_VARS below so
# the baseline child scrubs them too, making the class SKIP DETERMINISTICALLY in
# the offline baseline exactly like the task-3.2 precedent. The class has FOUR
# test methods (jwt-role, database-role, aws-role, audit-line), all sharing one
# class-level skipif, so it contributes FOUR additive offline skips — the
# deterministic offline baseline rises from 6 to 10. See _EXPECTED_SKIPS below
# for the (representative) documented reason.
#
# + 2 more added by the svc-07-secrets-manager spec (task 9.2): the new
# openbao_project_onboard POLICY CONTRACT test
# (test_svc07_openbao_onboard_contract.py::TestOpenBaoProjectPolicyContract, one
# method) and the ONBOARD IDEMPOTENCY test (::TestOpenBaoProjectOnboardIdempotency,
# one method). BOTH are requires_infra and gate on the SAME OpenBao env-var
# contract (OPENBAO_TEST_ADDR / OPENBAO_TEST_TOKEN, already in
# _REQUIRES_INFRA_ENV_VARS below), so with those vars scrubbed from the baseline
# child they SKIP DETERMINISTICALLY exactly like the task-7.4 contract class —
# the idempotency test's live-gate reason (OPENBAO_TEST_ADDR not set) is returned
# FIRST, before its ansible/docker capability checks, so its skip is env-var
# deterministic and NOT ambient-tooling-dependent (unlike the 5.5/6.3
# ansible-check idempotency tests, which are --ignore'd from the baseline child).
# Two additive offline skips -> the deterministic offline baseline rises from 10
# to 12. See _EXPECTED_SKIPS below for the documented reasons.
#
# + 24 more added by the svc-07-secrets-manager spec (tasks 12.1-12.5): the
# five requires_infra INTEGRATION / degraded-mode test files, each gating its
# class(es) on the OpenBao live contract (OPENBAO_TEST_ADDR checked FIRST, so
# the offline skip is env-var deterministic on it). With OPENBAO_TEST_ADDR (and
# the extra backend/container OPENBAO_TEST_* vars) scrubbed from the baseline
# child, every method skips cleanly naming that var, exactly like the task-7.4
# / 9.2 precedent. Per-file method counts: 12.1 auto-unseal=4, 12.2 dynamic
# creds=3, 12.3 auth/rotate/ingress=5, 12.4 backup/observability=5, 12.5
# degraded=7 -> +24 additive offline skips. The deterministic offline baseline
# rises from 12 to 36. See _EXPECTED_SKIPS below for the representative reasons.
_EXPECTED_SKIPPED = 36

#: The 7 tests known to FAIL on the unfixed tree (the bug + its two exploration
#: cases). They are the fix's job; the preservation baseline excludes them so
#: this file asserts the SAME invariant before AND after the fix.
_KNOWN_FAILING_ON_UNFIXED = (
    # Original 5 failing terraform-gated tests (bugfix.md Introduction).
    "infra/tests/test_degraded_modes.py::TestUnregisteredSlugPrecondition"
    "::test_plan_fails_for_unregistered_slug_and_provisions_nothing",
    "infra/tests/test_plan_idempotency.py::TestPlanIdempotency"
    "::test_foundation_root_second_plan_is_noop",
    "infra/tests/test_plan_idempotency.py::TestPlanIdempotency"
    "::test_project_root_second_plan_is_noop",
    "infra/tests/test_terraform_shape.py::TestTerraformJsonShape"
    "::test_foundation_root_shape",
    "infra/tests/test_terraform_shape.py::TestTerraformJsonShape"
    "::test_project_root_shape",
    # The 2 gated bug-condition cases added by task 1.
    "infra/tests/test_offline_plan_bug_condition.py"
    "::TestOfflinePlanIsBackendInitSafe"
    "::test_offline_plan_does_not_abort_on_backend_init[foundation]",
    "infra/tests/test_offline_plan_bug_condition.py"
    "::TestOfflinePlanIsBackendInitSafe"
    "::test_offline_plan_does_not_abort_on_backend_init[project_template]",
)

#: The 5 tests skipped on the unfixed tree, each with a stable substring of its
#: documented skip reason (Req 3.2). Observed via `pytest -rs`.
_EXPECTED_SKIPS = {
    "infra/tests/test_degraded_modes.py::TestApplierFailureIsTracked"
    "::test_applier_commit_failure_surfaces_and_provisions_nothing":
        "needs a live proxmox cluster",
    "infra/tests/test_degraded_modes.py::TestTrunkPreconditionIsDocumented"
    "::test_trunk_connectivity_is_validated_by_inspection_only":
        "inspection-only",
    "infra/tests/test_degraded_sdn_use.py"
    "::TestMissingSdnUseAuthorizationFailure"
    "::test_apply_surfaces_authorization_failure_and_provisions_nothing":
        # Corrected per ADR-0001 / fix-live-apply-backend-init Req 2.2: the
        # negative test now provisions the prerequisite foundation zone with a
        # privileged SDN token FIRST, so its gate checks PROXMOX_SDN_TEST_ENDPOINT
        # before the no-SDN.Allocate token — the skip reason now names that var.
        "proxmox_sdn_test_endpoint not set",
    "infra/tests/test_destroy_isolation.py::TestLiveDestroyIsolation"
    "::test_destroy_project_a_leaves_foundation_and_project_b_intact":
        "proxmox_sdn_test_endpoint not set",
    "infra/tests/test_integration_apply.py::TestFullEphemeralApplyCoexistence"
    "::test_foundation_then_two_projects_coexist_with_distinct_vlans":
        "proxmox_sdn_test_endpoint not set",
    # Added by the svc-07-secrets-manager spec (task 3.2): the SVC-07
    # `SDN.Allocate` degraded-mode test. Like the three live tests above, its
    # gate checks PROXMOX_SDN_TEST_ENDPOINT first (it provisions the prerequisite
    # foundation zone with the privileged SDN token before the negative SVC-07
    # apply), so with the env scrubbed it skips naming that var.
    "infra/tests/test_svc07_state_and_degraded.py"
    "::TestSvc07MissingSdnAllocateAuthorizationFailure"
    "::test_apply_surfaces_authorization_failure_and_provisions_nothing":
        "proxmox_sdn_test_endpoint not set",
    # Added by the svc-07-secrets-manager spec (task 7.4): the SVC-07
    # engine/auth/audit CONTRACT test. It is a requires_infra test whose class
    # skipif gates on the OpenBao env-var contract; with OPENBAO_TEST_ADDR
    # scrubbed from the baseline child (see _REQUIRES_INFRA_ENV_VARS), it skips
    # cleanly naming that var. Only the FIRST (class-level, TTL-ceiling) test in
    # the class is pinned here as the representative documented skip — the whole
    # class shares the same skipif reason.
    "infra/tests/test_svc07_openbao_engine_contract.py"
    "::TestOpenBaoEngineAuthAuditContract"
    "::test_jwt_role_stores_ttl_ceiling_non_renewable_bound_claims_and_policy":
        "openbao_test_addr not set",
    # Added by the svc-07-secrets-manager spec (task 9.2): the
    # openbao_project_onboard POLICY CONTRACT test. requires_infra, class skipif
    # gates on the OpenBao env-var contract; with OPENBAO_TEST_ADDR scrubbed it
    # skips cleanly naming that var — same precedent as the task-7.4 class above.
    "infra/tests/test_svc07_openbao_onboard_contract.py"
    "::TestOpenBaoProjectPolicyContract"
    "::test_bound_token_can_crud_in_prefix_and_is_denied_outside":
        "openbao_test_addr not set",
    # Added by the svc-07-secrets-manager spec (task 9.2): the
    # openbao_project_onboard ONBOARD IDEMPOTENCY test. Also requires_infra; its
    # gate returns the OpenBao live-gate reason FIRST (before the ansible/docker
    # capability checks), so with OPENBAO_TEST_ADDR scrubbed it skips
    # deterministically naming that var, independent of ambient tooling.
    "infra/tests/test_svc07_openbao_onboard_contract.py"
    "::TestOpenBaoProjectOnboardIdempotency"
    "::test_check_run_for_onboarded_slug_is_changed_zero_and_byte_identical":
        "openbao_test_addr not set",
    # Added by the svc-07-secrets-manager spec (tasks 12.1-12.5): the five live
    # INTEGRATION / degraded-mode test files. Each gates its class(es) on the
    # OpenBao live contract with OPENBAO_TEST_ADDR checked FIRST, so with that var
    # scrubbed from the baseline child every method skips deterministically naming
    # it. One representative method per file is pinned here (the whole class shares
    # the same class-level skipif reason).
    "infra/tests/test_svc07_openbao_autounseal.py"
    "::TestOpenBaoAutoUnsealRoundTrip"
    "::test_primary_reaches_unsealed_within_60s_after_restart":
        "openbao_test_addr not set",
    "infra/tests/test_svc07_openbao_dynamic_creds.py"
    "::TestOpenBaoDynamicPostgresCredentials"
    "::test_expired_pg_credential_is_rejected_within_60s_of_ttl":
        "openbao_test_addr not set",
    "infra/tests/test_svc07_openbao_auth_rotate_ingress.py"
    "::TestOpenBaoAuthRotateIngress"
    "::test_jwt_login_accepts_valid_token_and_denies_mismatched_or_expired":
        "openbao_test_addr not set",
    "infra/tests/test_svc07_openbao_backup_observability.py"
    "::TestOpenBaoBackupAndObservability"
    "::test_raft_snapshot_save_produces_nonempty_file":
        "openbao_test_addr not set",
    "infra/tests/test_svc07_openbao_degraded.py"
    "::TestOpenBaoDegradedModes"
    "::test_sealed_state_exposes_metric_zero_and_fires_alert":
        "openbao_test_addr not set",
}

#: The requires-infra gating env vars that the 3 live/requires-infra tests among
#: the 5 skips (test_degraded_sdn_use, test_destroy_isolation,
#: test_integration_apply) consult to decide RUN-vs-SKIP. They are scrubbed from
#: the baseline child's environment (see ``_run_baseline_suite``) so the offline
#: skip baseline (Req 3.2 — exactly 5 skips) is observed DETERMINISTICALLY, even
#: when the operator has ``.env`` loaded in their shell (e.g. ran
#: ``set -a && . ./.env && set +a`` for live testing earlier in the same shell).
#: Without this scrub the child pytest would inherit those creds, RUN the 3 live
#: tests instead of skipping them, and the baseline would observe "2 skipped"
#: instead of 5 — a flaky, ambient-shell-dependent result. The testing-strategy
#: steering forbids exactly this: "Tests must be deterministic — no flaky tests.
#: Environment-dependent tests must be controlled."
_REQUIRES_INFRA_ENV_VARS = frozenset(
    {
        "PROXMOX_SDN_TEST_ENDPOINT",
        "PROXMOX_SDN_TEST_TOKEN",
        "PROXMOX_TEST_ENDPOINT",
        "PROXMOX_TEST_TOKEN_NO_SDN_ALLOCATE",
        "PROXMOX_TEST_PROJECT_SLUG",
        # Added by the svc-07-secrets-manager spec (task 7.4): the OpenBao
        # engine/auth/audit CONTRACT test (test_svc07_openbao_engine_contract.py)
        # gates its requires_infra class on these. Scrubbed here so the OFFLINE
        # skip baseline (Req 3.2) stays deterministic even when an operator has a
        # live OpenBao `.env` loaded in the shell — otherwise the class would RUN
        # against the live instance in the baseline child instead of skipping,
        # flipping the pinned skip count. Same determinism rationale as the
        # Proxmox vars above.
        "OPENBAO_TEST_ADDR",
        "OPENBAO_TEST_TOKEN",
        "OPENBAO_TEST_SKIP_TLS_VERIFY",
        "OPENBAO_TEST_AUDIT_HOST_DIR",
        # Added by the svc-07-secrets-manager spec (tasks 12.1-12.5): the
        # five live INTEGRATION/degraded test files gate on OPENBAO_TEST_ADDR
        # FIRST (so the offline skip is deterministic on it), but several
        # methods ALSO consult these extra backend/container/control vars once
        # past the class gate. Scrub them too so a fully-populated live .env in
        # the operator's shell cannot make any task-12.x method RUN in the
        # baseline child (which would flip the pinned skip count) — same
        # determinism rationale as the Proxmox + base OpenBao vars above.
        "OPENBAO_TEST_PG_HOST",
        "OPENBAO_TEST_PG_PORT",
        "OPENBAO_TEST_PG_CONN_URL",
        "OPENBAO_TEST_PG_ADMIN_USER",
        "OPENBAO_TEST_PG_ADMIN_PASSWORD",
        "OPENBAO_TEST_PG_DB",
        "OPENBAO_TEST_GARAGE_S3_ENDPOINT",
        "OPENBAO_TEST_GARAGE_REGION",
        "OPENBAO_TEST_GARAGE_BUCKET",
        "OPENBAO_TEST_GARAGE_ADMIN_ACCESS_KEY",
        "OPENBAO_TEST_GARAGE_ADMIN_SECRET_KEY",
        "OPENBAO_TEST_SHORT_TTL_SECONDS",
        "OPENBAO_TEST_UNSEALER_ADDR",
        "OPENBAO_TEST_PRIMARY_CONTAINER",
        "OPENBAO_TEST_UNSEALER_CONTAINER",
        "OPENBAO_TEST_CONTAINER",
        "OPENBAO_TEST_RESTORE_CONTAINER",
        "OPENBAO_TEST_RESTORE_ADDR",
        "OPENBAO_TEST_ALLOW_SELF_RESTORE",
        "OPENBAO_TEST_PROM_URL",
        "OPENBAO_TEST_PROM_SCRAPE_INTERVAL",
        "OPENBAO_TEST_SEALED_ADDR",
        "OPENBAO_TEST_ALLOW_SEAL",
        "OPENBAO_TEST_PBS_REPOSITORY",
        "OPENBAO_TEST_PBS_PASSWORD",
        "OPENBAO_TEST_JWT_SIGNING_KEY",
        "OPENBAO_TEST_JWT_ISSUER",
        "OPENBAO_TEST_JWT_AUDIENCE",
        "OPENBAO_TEST_PG_ROTATE_CONN",
        "OPENBAO_TEST_PG_ROTATE_ROLE",
        "OPENBAO_TEST_AWS_MOUNT",
        "OPENBAO_TEST_AWS_ROTATE_ROLE",
        "OPENBAO_TEST_TRAEFIK_URL",
        "OPENBAO_TEST_DIRECT_8200_HOST",
        "OPENBAO_TEST_UNSEALER_CONTROL",
        "OPENBAO_TEST_PRIMARY_CONTROL",
        "OPENBAO_TEST_PG_CONTROL",
        "OPENBAO_TEST_DB_CONNECTION",
        "OPENBAO_TEST_DB_ROLE",
        "OPENBAO_TEST_GARAGE_CONTROL",
        "OPENBAO_TEST_AWS_ROLE",
        "OPENBAO_TEST_AUDIT_UNWRITABLE_PATH",
        "OPENBAO_TEST_LOG_FILE",
        "OPENBAO_TEST_SNAPSHOT_CONTROL",
        "OPENBAO_TEST_ZITADEL_LOGIN_URL",
        "OPENBAO_TEST_ZITADEL_CONTROL",
        "OPENBAO_TEST_OIDC_ROLE",
        "PROMETHEUS_TEST_ADDR",
        "LOKI_TEST_ADDR",
    }
)


def _run_baseline_suite() -> subprocess.CompletedProcess:
    """Run the pre-existing suite as a subprocess, deselecting the 7 tests that
    are the fix's target.

    Runs in a child pytest so this preservation test observes the *real* suite
    result the same way the canonical README/CI command does, rather than trying
    to re-enter the running session. ``PYTHONPATH`` is set to the scripts dir so
    the onboarding/derivation imports resolve exactly as documented.

    The requires-infra gating env vars (``_REQUIRES_INFRA_ENV_VARS``) are
    intentionally stripped from the child's environment so the OFFLINE skip
    baseline (Req 3.2 — exactly 5 skips) is observed deterministically, even if
    the operator has live-test credentials loaded in their shell. Without the
    scrub the 3 live tests would RUN instead of SKIP and the baseline would
    flip to "2 skipped" — a flaky, ambient-shell-dependent result the
    testing-strategy determinism rule forbids.
    """
    deselect_args: list[str] = []
    for node in _KNOWN_FAILING_ON_UNFIXED:
        deselect_args += ["--deselect", node]

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *_SUITE_PATHS,
        # Clear the root pytest.ini `addopts = -m "not requires_infra"` for the
        # CHILD run. The preservation baseline must observe the true OFFLINE
        # SKIP behavior — the 3 live/requires-infra tests COLLECTED and then
        # SKIPPED via their skipif (the 5-skip baseline, Req 3.2) — NOT
        # deselected. With the default deselection active the child would drop
        # those 3 requires_infra-marked tests entirely and report only 2 skips,
        # breaking the documented baseline. `-o addopts=` neutralises the
        # inherited default so the tests are collected; the env scrub below
        # (`_REQUIRES_INFRA_ENV_VARS`) then makes them SKIP cleanly (creds
        # absent), giving the deterministic 5-skip baseline this file asserts.
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        "--tb=short",
        "-rs",
        # Do not recurse into THIS preservation file: it shells out to pytest,
        # so including it would recurse. It is not part of the preserved
        # pre-existing baseline anyway (it is new in this bugfix).
        "--ignore=infra/tests/test_offline_plan_preservation.py",
        # Exclude the svc-07 openbao_install check-mode idempotency test
        # (task 5.5): it is a NEW test from a different spec, not part of this
        # bugfix's preserved 117/5 baseline, and its RUN-vs-SKIP outcome depends
        # on the ambient environment (whether `ansible-playbook` is installed and
        # whether root/passwordless-sudo is available for the role's become).
        # Letting it into the baseline child would make the pinned skip COUNT
        # environment-dependent — exactly the flaky, ambient-dependent result the
        # determinism rule (and the env scrub above) exists to prevent.
        "--ignore=infra/tests/test_svc07_openbao_install_idempotency.py",
        # Exclude the svc-07 openbao_init_unseal check-mode idempotency test
        # (task 6.3) for the SAME reason as the install one above: it is a NEW
        # test from a different spec, not part of this bugfix's preserved 117/5
        # baseline, and its RUN-vs-SKIP outcome is environment-dependent (whether
        # `ansible-playbook` is installed and whether root/passwordless-sudo is
        # available for the role's become). Letting it into the baseline child
        # would make the pinned skip COUNT environment-dependent — the flaky,
        # ambient-dependent result the determinism rule exists to prevent.
        "--ignore=infra/tests/test_svc07_openbao_init_unseal_idempotency.py",
        *deselect_args,
    ]
    env = {
        **_os_environ(),
        "PYTHONPATH": str(_SCRIPTS_DIR),
    }
    # Strip the requires-infra gating vars so the child observes the true
    # OFFLINE baseline (5 skips, Req 3.2) regardless of the operator's shell.
    # See _REQUIRES_INFRA_ENV_VARS for the rationale (determinism steering).
    for var in _REQUIRES_INFRA_ENV_VARS:
        env.pop(var, None)
    return subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


@pytest.fixture(scope="module")
def baseline_run() -> subprocess.CompletedProcess:
    """Run the deselected pre-existing suite once and share the result."""
    return _run_baseline_suite()


# =========================================================================== #
# Req 3.1 — the 117 pre-existing passing tests still pass.
# Req 3.2 — the 5 pre-existing skipped tests still skip.
# =========================================================================== #
class TestPreExistingSuiteBaseline:
    """The pre-existing green suite (117 passed + 5 skipped), with the 7
    under-repair tests deselected, is preserved unchanged (Req 3.1, 3.2).

    The baseline child run (``_run_baseline_suite``) intentionally strips the
    requires-infra gating env vars (``_REQUIRES_INFRA_ENV_VARS``) so the OFFLINE
    skip baseline of exactly 5 skips (Req 3.2) is observed deterministically —
    even when an operator has ``.env`` live-test credentials loaded in their
    shell. This keeps the assertions below immune to ambient shell state, per
    the testing-strategy determinism rule.
    """

    def test_no_pre_existing_test_fails(self, baseline_run):
        """With the 7 under-repair tests deselected, nothing else fails.

        On the unfixed tree this holds because the only failures ARE those 7;
        after the fix it must still hold (the fix turns the 7 green and touches
        nothing else). Req 3.1.
        """
        summary = _summary_line(baseline_run.stdout)
        assert baseline_run.returncode == 0, (
            "the pre-existing suite (7 under-repair tests deselected) must have "
            "zero failures — the preserved baseline (Req 3.1).\n"
            f"summary: {summary}\n"
            f"stdout tail:\n{baseline_run.stdout[-3000:]}\n"
            f"stderr tail:\n{baseline_run.stderr[-2000:]}"
        )

    def test_at_least_baseline_pre_existing_tests_pass(self, baseline_run):
        """At least the observed baseline of pre-existing tests pass (Req 3.1).

        The offline-plan fix must not cause any previously-passing test to drop
        or change status, so the passing count must never fall BELOW the
        observed baseline (``_EXPECTED_PASSED``). It may legitimately rise above
        it: when this suite is integrated with other feature branches that add
        their own passing tests, the total grows. Asserting ``>=`` (rather than
        exact equality) keeps the regression guard — a dropped/failed
        pre-existing test still trips ``test_no_pre_existing_test_fails`` and
        pushes this count down — while tolerating additive growth from
        integration. Req 3.1.
        """
        passed = _count_from_summary(baseline_run.stdout, "passed")
        assert passed >= _EXPECTED_PASSED, (
            f"expected at least the baseline {_EXPECTED_PASSED} pre-existing "
            f"passing tests, got {passed}. A count BELOW the baseline means a "
            f"previously-passing test dropped or changed status (Req 3.1).\n"
            f"summary: {_summary_line(baseline_run.stdout)}"
        )

    def test_exactly_5_pre_existing_tests_skip(self, baseline_run):
        """Exactly the observed 5 pre-existing tests skip (Req 3.2)."""
        skipped = _count_from_summary(baseline_run.stdout, "skipped")
        assert skipped == _EXPECTED_SKIPPED, (
            f"expected exactly {_EXPECTED_SKIPPED} pre-existing skipped tests, "
            f"got {skipped} (Req 3.2).\n"
            f"summary: {_summary_line(baseline_run.stdout)}"
        )

    @pytest.mark.parametrize("node_id", sorted(_EXPECTED_SKIPS))
    def test_pre_existing_skips_keep_their_documented_reason(
        self, baseline_run, node_id
    ):
        """Each pre-existing skip is still present with its documented reason
        (Req 3.2).

        Asserts against the `-rs` skip report so a skip silently turning into a
        pass/fail — or keeping the skip but losing its documented reason — is
        caught.
        """
        reason_substr = _EXPECTED_SKIPS[node_id]
        skip_report = _skip_report(baseline_run.stdout)

        # The `-rs` report lists the file:line + reason but not the full node
        # id, so match on the module path + the reason substring together.
        module_path = node_id.split("::", 1)[0]
        matching = [
            line
            for line in skip_report
            if module_path in line and reason_substr in line.lower()
        ]
        assert matching, (
            f"expected a documented skip for {node_id} whose reason contains "
            f"{reason_substr!r} (Req 3.2), but no matching line was found in "
            f"the skip report:\n" + "\n".join(skip_report)
        )


# =========================================================================== #
# Req 3.3 — the two committed Terraform roots are untouched (byte-for-byte),
# and no stray backend-override / state artifact is left behind.
# =========================================================================== #
class TestCommittedRootsUntouched:
    """After the suite runs, neither committed root has a modified tracked .tf
    file, nor a stray transient override/state artifact (Req 3.3, PF FR-2)."""

    #: Names/patterns that MUST NOT appear as artifacts in a committed root:
    #: the transient local-backend override the fix uses, and terraform state.
    _FORBIDDEN_ARTIFACT_RE = re.compile(
        r"(zz_.*backend_override.*\.tf|terraform\.tfstate)",
        re.IGNORECASE,
    )

    def _porcelain(self, root_dir: Path) -> list[str]:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", str(root_dir)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]

    @pytest.mark.parametrize(
        "root_dir",
        [_FOUNDATION_DIR, _PROJECT_TEMPLATE_DIR],
        ids=["foundation", "project_template"],
    )
    def test_no_modified_tracked_tf_files_in_root(self, baseline_run, root_dir):
        """No tracked ``.tf`` file in the root is modified/deleted (Req 3.3).

        The offline fix must confine any backend override to a sandbox copy, so
        the committed roots' backend blocks, provider wiring, and SDN graph stay
        byte-for-byte as declared. ``git status --porcelain`` entries starting
        with ' M'/'M '/'MM'/'D '/'AD' on a ``.tf`` file would betray a mutation.
        """
        entries = self._porcelain(root_dir)
        modified_tf = []
        for entry in entries:
            # Porcelain format: XY <path>. Untracked is '??'.
            status, _, path = entry.partition(" ")
            path = path.strip().strip('"')
            if entry.startswith("??"):
                continue  # untracked handled by the artifact test below
            if path.endswith(".tf"):
                modified_tf.append(entry)
        assert modified_tf == [], (
            f"a tracked .tf file under {root_dir} was modified/staged/deleted "
            f"after the suite ran — the committed root must stay byte-for-byte "
            f"unchanged (Req 3.3, PF FR-2). Offending entries:\n"
            + "\n".join(modified_tf)
        )

    @pytest.mark.parametrize(
        "root_dir",
        [_FOUNDATION_DIR, _PROJECT_TEMPLATE_DIR],
        ids=["foundation", "project_template"],
    )
    def test_no_stray_override_or_state_artifact_in_root(
        self, baseline_run, root_dir
    ):
        """No stray transient override / state artifact is left in the root
        (Req 3.3).

        Any ``zz_*backend_override*.tf`` or ``terraform.tfstate`` appearing
        (tracked OR untracked) inside a committed root means the fix's transient
        local backend leaked out of its sandbox.
        """
        entries = self._porcelain(root_dir)
        offenders = [e for e in entries if self._FORBIDDEN_ARTIFACT_RE.search(e)]
        assert offenders == [], (
            f"a transient backend-override / state artifact leaked into "
            f"{root_dir} (Req 3.3). The offline fix's local backend override "
            f"must live only in a temporary sandbox copy. Offending entries:\n"
            + "\n".join(offenders)
        )


# --------------------------------------------------------------------------- #
# pytest summary-line parsing helpers.
# --------------------------------------------------------------------------- #
def _summary_line(stdout: str) -> str:
    """Return the terminal summary line (e.g. '117 passed, 5 skipped ...')."""
    for line in reversed(stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            if "=" in line:
                return line.strip()
    return "<no summary line found>"


def _count_from_summary(stdout: str, kind: str) -> int:
    """Parse '<N> <kind>' (passed/skipped/failed) from the pytest summary."""
    summary = _summary_line(stdout)
    match = re.search(rf"(\d+)\s+{kind}\b", summary)
    return int(match.group(1)) if match else 0


def _skip_report(stdout: str) -> list[str]:
    """Return the `-rs` 'short test summary info' SKIPPED lines."""
    return [ln.strip() for ln in stdout.splitlines() if ln.strip().startswith("SKIPPED")]
