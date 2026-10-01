"""Bug-condition exploration test for the offline terraform-plan failure.

Spec: fix-offline-terraform-plan-tests (bugfix) — Task 1, design.md
"Correctness Properties / Property 1: Bug Condition — Terraform plan tests are
offline-safe".

=== WHAT THIS TEST IS ===

This is the *exploration* test of the bugfix workflow. It encodes the Bug
Condition from design.md and asserts the Expected Behavior (Property 1). It is
DELIBERATELY written to FAIL on the UNFIXED helpers — that failure is the
success signal that confirms the bug exists. Once the fix (tasks 3.x) lands,
this SAME test is re-run (task 3.6) and MUST pass.

=== BUG CONDITION (design.md) ===

    isBugCondition(X) =
        terraform_binary_present(X)
        AND NOT live_gitlab_http_backend_available(X)
        AND test_runs_init_backend_false_then_plan_without_offline_backend(X)

This is a *deterministic environment* bug, not an input-varying one. There is
therefore no Hypothesis generator: the property is scoped to the concrete
failing conditions (a terraform/tofu binary present; no live GitLab HTTP
backend / CI_JOB_TOKEN / Proxmox credentials) and asserted over the two
concrete cases — the foundation root (``infra/platform-foundation/``) and the
project template root (``infra/projects/_TEMPLATE/``).

=== EXPECTED BEHAVIOR (design.md Property 1) ===

For each root, an offline ``terraform plan`` SHALL either complete (exit 0) or
skip cleanly — it MUST NOT abort with
``Error: Backend initialization required ... backend "http"``.

=== HOW IT EXERCISES THE BEHAVIOR ===

The test drives an offline ``terraform plan`` for each root and asserts the
Expected Behavior: the plan MUST NOT abort with the backend-init error — it
either completes or skips cleanly.

On the UNFIXED helpers this originally drove the raw ``terraform init
-backend=false`` then ``terraform plan`` sequence directly against the committed
roots (the exact inconsistent sequence the five failing tests used), which
FAILED with ``Error: Backend initialization required ... backend "http"`` and so
confirmed the bug existed (design Bug Details §Examples §1).

Once the fix landed (tasks 3.1–3.5), that raw sequence became a dead code path:
the fix lives entirely inside the new ``offline_plan`` helper
(``infra/tests/conftest.py``), so a test that never calls the helper could
never flip from FAIL to PASS. Per the exploratory-bugfix workflow, THIS Task-1
exploration test must be the one that flips once the fix lands, so its plan
invocation is now pointed at the fixed ``offline_plan`` helper — the exact
mechanism the fix introduces (transient ``backend "local" {}`` override +
``init -reconfigure`` in a sandbox copy, dummy provider vars). The behavioral
assertion is UNCHANGED and NOT weakened: an offline plan must never abort on the
partial ``backend "http" {}``. See the reconciliation note on
``_run_offline_plan`` below.

=== EXPECTED OUTCOME ===

- On the UNFIXED tree (before the ``offline_plan`` helper existed / against the
  raw sequence): FAIL — ``plan`` exits non-zero with the backend-init error,
  confirming the bug.
- On the FIXED tree (via the ``offline_plan`` helper, task 3.6): PASS — the
  offline plan completes against the transient local backend (or skips cleanly
  if the offline prerequisite is genuinely unavailable), never a backend-init
  abort.

Run:  ~/venv/devinfra/bin/pytest \\
        infra/tests/test_offline_plan_bug_condition.py -v
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

# The fix lives in the sibling ``conftest.py`` helper. Driving the exploration
# test through it is what lets this Task-1 test flip from FAIL (unfixed) to PASS
# (fixed) — see the module docstring "HOW IT EXERCISES THE BEHAVIOR".
from conftest import _terraform_binary, offline_plan, requires_terraform

#: The two concrete cases the bug-condition property ranges over. For the
#: project template root the offline plan needs a *registered* slug so the
#: `assert_registered` precondition passes and the plan can complete; the
#: foundation root takes no slug. Either way the ASSERTION only cares that the
#: plan never aborts on backend init.
_ROOT_VAR_ARGS: dict[str, tuple[str, ...]] = {
    "foundation": (),
    "project_template": ("-var", "project_slug=mlvideo"),
}

#: Substrings that identify the backend-init abort this bug produces
#: (design Bug Details §Examples §1). Matched case-insensitively.
_BACKEND_INIT_MARKERS = (
    "backend initialization required",
    'backend "http"',
    "initial configuration of the requested backend",
)


def _live_gitlab_http_backend_available() -> bool:
    """Best-effort detection of a live GitLab-managed HTTP backend / creds.

    The roots' partial ``backend "http" {}`` is configured in CI via
    ``-backend-config`` using ``CI_JOB_TOKEN`` against a GitLab state address.
    Offline (the bug condition) none of those exist. If any of these appear in
    the environment we are NOT in the bug condition and skip cleanly.
    """
    return any(
        os.environ.get(var)
        for var in ("CI_JOB_TOKEN", "TF_HTTP_ADDRESS", "TF_HTTP_PASSWORD")
    )


# The outer gate mirrors the existing helpers: absent binary => skip (never
# fail), per the documentation-testing steering ("absent infra => skipped").
requires_terraform = pytest.mark.skipif(
    _terraform_binary() is None,
    reason="requires terraform toolchain",
)


@requires_terraform
class TestOfflinePlanIsBackendInitSafe:
    """Property 1 (Bug Condition): an offline plan must not abort on the partial
    ``backend "http" {}``.

    Encodes ``isBugCondition`` and asserts the Expected Behavior over both
    roots. Originally FAILED on the unfixed helpers (backend-init abort) to
    confirm the bug existed; now exercises the fixed ``offline_plan`` helper
    (transient ``backend "local" {}`` override + ``init -reconfigure`` in a
    sandbox copy, dummy provider vars) and PASSES when the fix is correct.

    The behavioral assertion is UNCHANGED from the Task-1 original: for each
    root, the offline plan must NOT abort with a backend-initialization error.
    The only change is that the plan invocation goes through the fix's actual
    mechanism instead of the raw unfixed sequence. See the module docstring's
    "HOW IT EXERCISES THE BEHAVIOR" section for the rationale.
    """

    @pytest.mark.parametrize("root_name", sorted(_ROOT_VAR_ARGS))
    def test_offline_plan_does_not_abort_on_backend_init(self, root_name: str):
        """For each root, an offline ``terraform plan`` must NOT abort with the
        backend-init error (design Property 1).

        Bug Condition: terraform binary present AND no live GitLab HTTP backend.
        Expected Behavior: plan completes (exit 0) or the test skips cleanly —
        never a backend-init abort.
        """
        # Confirm the bug condition actually holds; otherwise skip cleanly
        # (we are in CI/production with a real backend — not the buggy input).
        if _live_gitlab_http_backend_available():
            pytest.skip(
                "requires-infra, skipped: a live GitLab HTTP backend / "
                "CI_JOB_TOKEN is present, so the offline bug condition does "
                "not hold here"
            )

        var_args = _ROOT_VAR_ARGS[root_name]
        with offline_plan(root_name, *var_args, refresh=False) as result:
            combined = result.output.lower()

            aborted_on_backend_init = result.returncode != 0 and any(
                marker in combined for marker in _BACKEND_INIT_MARKERS
            )

            assert not aborted_on_backend_init, (
                f"offline `terraform plan` for the {root_name} root "
                f"aborted with a backend-initialization error "
                f"instead of completing or skipping (design Property 1). "
                f"This is the bug (design Bug Details §Examples §1): "
                f"the offline mechanism (transient `backend \"local\" {{}}` "
                f"override + `init -reconfigure`) did not prevent the partial "
                f"`backend \"http\" {{}}` from blocking the plan.\n"
                f"exit code: {result.returncode}\n"
                f"output:\n{result.output}"
            )
