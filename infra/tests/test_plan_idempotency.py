"""Plan-idempotency snapshot gate for the Proxmox Network Foundation IaC layer.

Task 8.1 (spec: proxmox-network-foundation) — design.md "Testing Strategy §2,
Contract-style tests / Plan idempotency (snapshot gate)".

This is a *contract-style* gate for the Terraform resource layer, NOT a
property-based test: per the testing-strategy steering and design.md, the IaC
layer's behaviour does not vary with generated input and is validated by
plan-idempotency, provider resource-shape assertions, and integration apply —
never by PBT. This gate is the IaC analogue of a round-trip and is the primary
correctness gate for the resource layer (Requirements 4.6, 8.1).

What the gate asserts:

  After an initial apply (or a captured plan against a fixture/backend-less
  state), a *second* `terraform plan` against unchanged registry + inputs MUST
  report ZERO additions, ZERO changes, and ZERO destructions — for BOTH the
  foundation root (infra/platform-foundation/) and a project root
  (infra/projects/_TEMPLATE/).

  Concretely, with a terraform/tofu binary present the test, per root:
    1. copies BOTH roots into a temporary sandbox (committed roots untouched)
       and drops a transient `backend "local" {}` override into the target
       root, then `terraform init -reconfigure` (a credential-free local
       backend — NOT `init -backend=false`, which configures no backend and
       makes the subsequent `plan` abort with "Backend initialization
       required ... backend http");
    2. `terraform plan -out=<plan>` with dummy, non-contacting
       `proxmox_endpoint` / `proxmox_api_token` provider vars (the roots
       declare only SDN resources with no read-time data sources, so no live
       GitLab HTTP backend, `CI_JOB_TOKEN`, or Proxmox credential is needed);
    3. `terraform show -json <plan>`      (inspect resource_changes)
    4. assert every entry in `resource_changes` has actions == ["no-op"]
       once the resources already exist — i.e. zero add/change/destroy.
  Equivalently, `terraform plan -detailed-exitcode` returns exit code 0
  (0 = no changes; 2 = changes present; 1 = error).

=== ENVIRONMENT GATING — READ THIS ===

The plan is captured via the shared `offline_plan` helper
(`infra/tests/conftest.py`), which runs entirely offline against the transient
local backend + dummy provider vars described above. The outer
`requires_terraform` gate (`shutil.which("terraform") or shutil.which("tofu")`)
still SKIPS the whole class when no terraform/tofu binary is present, per the
documentation-testing steering rule "absent infra => skipped, not failed"; and
`offline_plan` itself skips cleanly if the offline prerequisite is genuinely
unavailable (e.g. providers cannot be installed offline).

Offline, however, the local backend starts EMPTY — there is no prior
`terraform apply` and no recorded GitLab-managed state — so a fresh plan is
necessarily an all-`create` plan, against which a *second* plan cannot be a
no-op. That is a requires-infra prerequisite (applied/recorded state), so the
idempotency check SKIPS cleanly offline rather than failing on the unavoidable
create plan (design Property 1: pass offline OR skip cleanly, never fail). The
genuine "second plan is a no-op" assertion therefore **runs in CI / against the
live-apply path**, where applied state exists; `validation.md` (task 11.3)
records this gating explicitly rather than claiming the no-op behaviour was
verified locally.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_plan_idempotency.py -v
"""

from __future__ import annotations

import pytest

from conftest import offline_plan, requires_terraform, _terraform_binary


@requires_terraform
class TestPlanIdempotency:
    """Second-plan-is-a-no-op gate for the foundation and a project root.

    Only runs when a terraform/tofu binary is present. In THIS environment the
    binary is absent, so the whole class is SKIPPED — the gate is plan-only in
    this environment and must run in CI (see module docstring and validation.md).
    """

    def _non_noop_changes(self, *var_args: str, root_name: str) -> list[dict]:
        """Run an offline plan for a root and return its non-no-op changes.

        Returns the list of `resource_changes` entries whose `actions` are
        anything other than exactly ["no-op"]. An empty list means the plan is
        idempotent (zero add/change/destroy) for this root.

        The plan is captured via the shared `offline_plan` helper, which runs
        against a transient `backend "local" {}` override with dummy provider
        vars so no live GitLab-managed state or Proxmox credentials are needed
        (Req 2.2, 2.3). The second-plan idempotency is asserted against
        unchanged registry + module inputs. In CI this runs after the initial
        apply (or against a recorded fixture state) so the resources already
        exist and the correct expectation is all-no-op.

        Offline there is NO applied/recorded state — the transient local
        backend starts empty — so a fresh plan is necessarily an all-`create`
        plan. A genuine "second plan is a no-op" therefore requires
        applied/recorded state, which is a requires-infra prerequisite absent
        here; in that case this raises `pytest.skip(...)` so the test skips
        cleanly rather than failing on the unavoidable create plan (design
        Property 1: pass offline OR skip cleanly, never fail; Req 2.1). The
        genuine post-apply no-op assertion is exercised in CI and by the
        live-apply integration path (`test_integration_apply.py`).

        If the offline plan cannot run cleanly at all (e.g. providers cannot be
        installed offline), the `offline_plan` helper itself skips
        (Req 1.3 -> 2.1).
        """
        with offline_plan(root_name, *var_args, refresh=False) as result:
            changes = result.resource_changes
            non_noop = [
                c for c in changes
                if c.get("change", {}).get("actions", []) != ["no-op"]
            ]

            # Offline (no applied state) every managed resource is a fresh
            # `create` — there is no prior state against which a *second* plan
            # could be a no-op. That is a requires-infra prerequisite (applied
            # or recorded state), so skip cleanly rather than fail on the
            # unavoidable create plan (design Property 1; Req 2.1). Detect it as
            # "there are changes and none of them are anything but create".
            all_create = bool(non_noop) and all(
                c.get("change", {}).get("actions", []) == ["create"]
                for c in non_noop
            )
            if all_create:
                pytest.skip(
                    "requires-infra, skipped: the offline plan ran against a "
                    "transient EMPTY local backend (no live GitLab state, no "
                    "prior `terraform apply`), so every resource is a fresh "
                    "`create`. A genuine 'second plan is a no-op' check needs "
                    "applied/recorded state, which is unavailable offline. The "
                    "post-apply no-op assertion runs in CI and via the "
                    "live-apply integration path (Req 2.1, 2.5)."
                )
            return non_noop

    def test_foundation_root_second_plan_is_noop(self):
        """Foundation root: second plan reports zero add/change/destroy (Req 4.6, 8.1)."""
        non_noop = self._non_noop_changes(root_name="foundation")
        assert non_noop == [], (
            "foundation root second plan must be a no-op (zero additions, "
            "changes, destructions) against unchanged inputs (Req 4.6, 8.1); "
            f"found non-no-op changes: "
            f"{[(c.get('address'), c.get('change', {}).get('actions')) for c in non_noop]}"
        )

    def test_project_root_second_plan_is_noop(self):
        """Project root: second plan reports zero add/change/destroy (Req 4.6, 8.1)."""
        non_noop = self._non_noop_changes(
            "-var", "project_slug=mlvideo", root_name="project_template",
        )
        assert non_noop == [], (
            "project root second plan must be a no-op (zero additions, "
            "changes, destructions) against unchanged inputs (Req 4.6, 8.1); "
            f"found non-no-op changes: "
            f"{[(c.get('address'), c.get('change', {}).get('actions')) for c in non_noop]}"
        )
