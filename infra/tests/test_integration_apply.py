"""Full ephemeral-apply + coexistence integration test (Proxmox SDN).

Task 10.1 (spec: proxmox-network-foundation) — Requirements 4.7, SEC.1, SEC.2;
design.md "Testing Strategy §3 Integration tests — Full apply (preferred, when a
test Proxmox with SDN is available)".

This is the *live-apply coexistence/isolation evidence* for the IaC resource
layer. It is NOT a property-based test: per the testing-strategy steering and
design.md, the IaC layer's behaviour does not vary with generated input and is
validated by plan-idempotency, provider resource-shape assertions, and
integration apply — never by PBT.

What the test asserts, end-to-end, WHEN a test Proxmox with SDN is available:

  1. Apply the FOUNDATION root (infra/platform-foundation/): the single
     `proxmox_sdn_zone_vlan` + the single `proxmox_sdn_applier`. Assert the
     zone exists cluster-wide and the applier committed (Requirements 4.1, 4.2,
     4.7).
  2. Apply PROJECT ROOT A (slug `dronefleet` -> vlan_id 100 from projects.yaml):
     one `proxmox_sdn_vnet` (tag 100) + one `proxmox_sdn_subnet`
     (10.0.100.0/24, gw 10.0.100.1) + its own applier. Assert VNetA/SubnetA
     exist and the applier committed (Requirements 4.3, 4.4, 4.7).
  3. Apply PROJECT ROOT B (slug `mlvideo` -> vlan_id 101): assert VNetB/SubnetB
     coexist ALONGSIDE A with DISTINCT vlan tags (100 vs 101) and DISTINCT
     CIDRs (10.0.100.0/24 vs 10.0.101.0/24). This is the coexistence/isolation
     evidence: two projects' dedicated VLANs live side by side, each isolated
     to its own VLAN with no shared VLAN carrying both projects' workloads
     (SEC.1, SEC.2).

Assertions are made against the terraform state file each apply/destroy writes.

=== LIVE-APPLY MECHANISM (spec: fix-live-apply-backend-init) ===

Each apply/destroy is driven through the shared
`infra/tests/conftest.py::live_apply` helper. Unlike the earlier
`init -backend=false` + deprecated `-state=<tmp>` sequence — which aborted at
backend init (`Backend initialization required ... backend "http"`) before ever
reaching Proxmox — `live_apply` copies both roots into a throwaway sandbox,
writes a transient `backend "local" { path = <disposable temp state file> }`
override into the sandboxed target root only, `init -reconfigure`s that
credential-free local backend, and runs `apply`/`destroy` (NO `-state=` flag)
against the real cluster. Each `(root, state)` invocation gets its OWN sandbox +
override -> OWN state file, so applying the SAME project root for A and B writes
to two SEPARATE state files (the per-project isolation this test depends on).
State lives in a disposable, caller-owned temp file the test reads back via the
`_state_resources` JSON parsing below. The committed roots are never modified.

Secret hygiene (Req 3.6): the Proxmox endpoint/token are read from the
`PROXMOX_SDN_TEST_*` env vars and threaded into the subprocess `env` by
`live_apply` (`TF_VAR_proxmox_api_token`) ONLY — the token is never placed on a
command line, printed, or interpolated into an assertion message. Every
assertion below prints only `.output` (Terraform's `-no-color` stdout+stderr,
which does not echo `TF_VAR_*` values).

=== ENVIRONMENT GATING — READ THIS ===

terraform/tofu AND a live test Proxmox cluster with SDN enabled are NOT
available in this environment, so the full ephemeral apply CANNOT execute here.
Following the SAME gating idiom as `test_plan_idempotency.py` and
`test_degraded_sdn_use.py` (and the documentation-testing steering rule
"absent infra => skipped, not failed"), the whole class is `skipif`-gated behind
BOTH:

  * the presence of a `terraform`/`tofu` binary, AND
  * a Proxmox SDN test-cluster endpoint env var
    (`PROXMOX_SDN_TEST_ENDPOINT`) plus its token (`PROXMOX_SDN_TEST_TOKEN`).

When any of these is absent the class is SKIPPED with an explicit
"requires-infra, skipped: ..." reason.

This live-apply evidence is therefore **infra-gated and, in this environment,
plan-only/gated — it MUST run in CI** where a terraform/tofu toolchain and an
ephemeral Proxmox SDN test cluster are available. `validation.md` (task 11.3)
references this file and MUST record the infra-gating explicitly rather than
claiming the coexistence/isolation behaviour was verified locally.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_integration_apply.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from conftest import live_apply

# --------------------------------------------------------------------------- #
# Coexistence fixture: two registered projects with distinct, adjacent VLAN IDs.
# These MUST already exist in infra/platform-foundation/projects.yaml so each
# project root can resolve its vlan_id via the yamldecode() registry lookup.
# --------------------------------------------------------------------------- #
_PROJECT_A_SLUG = "dronefleet"
_PROJECT_A_VLAN = 100
_PROJECT_A_CIDR = "10.0.100.0/24"
_PROJECT_A_GW = "10.0.100.1"

_PROJECT_B_SLUG = "mlvideo"
_PROJECT_B_VLAN = 101
_PROJECT_B_CIDR = "10.0.101.0/24"
_PROJECT_B_GW = "10.0.101.1"

# Proxmox SDN test-cluster env vars (both required for the live gate).
_SDN_TEST_ENDPOINT_ENV = "PROXMOX_SDN_TEST_ENDPOINT"
_SDN_TEST_TOKEN_ENV = "PROXMOX_SDN_TEST_TOKEN"


def _terraform_binary() -> str | None:
    """Return the terraform (or tofu) binary path, or None if neither exists."""
    return shutil.which("terraform") or shutil.which("tofu")


def _live_apply_gate_reason() -> str | None:
    """Return an explicit skip reason if the live-apply gate is NOT satisfied,
    else None.

    Same "absent infra => skip, never fail" pattern as
    test_degraded_sdn_use.py::_live_degraded_gate_reason.
    """
    if _terraform_binary() is None:
        return (
            "requires-infra, skipped: no terraform/tofu binary available to "
            "drive a full ephemeral apply against a Proxmox SDN test cluster"
        )
    if not os.environ.get(_SDN_TEST_ENDPOINT_ENV):
        return (
            f"requires-infra, skipped: {_SDN_TEST_ENDPOINT_ENV} not set "
            "(an ephemeral Proxmox test cluster with SDN enabled is required)"
        )
    if not os.environ.get(_SDN_TEST_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_SDN_TEST_TOKEN_ENV} not set "
            "(an API token for the Proxmox SDN test cluster is required)"
        )
    return None


@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_apply_gate_reason() is not None,
    reason=_live_apply_gate_reason() or "live-apply gate satisfied",
)
class TestFullEphemeralApplyCoexistence:
    """Full ephemeral apply of foundation + two project roots, coexistence gate.

    Runs ONLY in CI (or locally) when a terraform/tofu binary AND a Proxmox SDN
    test-cluster endpoint + token are all present. In THIS environment none of
    those are available, so the whole class is SKIPPED — this evidence is
    infra-gated and must run in CI (see module docstring and validation.md).

    The test tears down everything it applies in reverse order at the end, so
    the target cluster is left as it was found (ephemeral apply).
    """

    # ----------------------------------------------------------------- #
    # Helpers
    # ----------------------------------------------------------------- #
    def _state_resources(self, state_path: Path) -> list[dict]:
        """Return the resource records from a terraform state file."""
        if not state_path.is_file():
            return []
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            return []
        return state.get("resources", [])

    def _count_type(self, resources: list[dict], type_prefix: str) -> int:
        """Count provisioned instances of resources whose type starts with prefix."""
        total = 0
        for res in resources:
            if str(res.get("type", "")).startswith(type_prefix):
                total += len(res.get("instances", []))
        return total

    def _sdn_attr_values(self, resources: list[dict], type_name: str,
                         attr: str) -> list:
        """Collect a given attribute value across all instances of a type."""
        values: list = []
        for res in resources:
            if res.get("type") == type_name:
                for inst in res.get("instances", []):
                    attrs = inst.get("attributes", {})
                    if attr in attrs:
                        values.append(attrs[attr])
        return values

    # ----------------------------------------------------------------- #
    # The test
    # ----------------------------------------------------------------- #
    def test_foundation_then_two_projects_coexist_with_distinct_vlans(self):
        """Foundation + project A + project B: coexistence/isolation evidence.

        Steps (all against the ephemeral Proxmox SDN test cluster):
          1. apply foundation root  -> zone + applier committed (Req 4.1/4.2/4.7)
          2. apply project root A   -> VNetA(100) + SubnetA(10.0.100.0/24) + applier
          3. apply project root B   -> VNetB(101) + SubnetB(10.0.101.0/24) + applier
          4. assert A and B coexist with DISTINCT vlan tags and CIDRs (SEC.1/SEC.2)

        Each apply/destroy is driven through `live_apply` into its OWN state
        file, so applying the shared project-template root for A and B writes to
        two SEPARATE state files (no collision) — the per-project isolation this
        test depends on.
        """
        endpoint = os.environ[_SDN_TEST_ENDPOINT_ENV]
        token = os.environ[_SDN_TEST_TOKEN_ENV]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            foundation_state = tmp_path / "foundation.tfstate"
            a_state = tmp_path / "project_a.tfstate"
            b_state = tmp_path / "project_b.tfstate"

            a_slug_var = ("-var", f"project_slug={_PROJECT_A_SLUG}")
            b_slug_var = ("-var", f"project_slug={_PROJECT_B_SLUG}")

            try:
                # --- 1. Foundation: zone + applier -------------------------- #
                with live_apply(
                    "foundation", foundation_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as foundation:
                    found_apply = foundation.apply()
                assert found_apply.returncode == 0, (
                    "foundation apply (zone + applier) must succeed "
                    f"(Req 4.1, 4.2, 4.7); output:\n{found_apply.output}"
                )
                found_res = self._state_resources(foundation_state)
                assert self._count_type(found_res, "proxmox_sdn_zone_vlan") == 1, (
                    "foundation must provision exactly one SDN VLAN zone (Req 4.1)"
                )
                assert self._count_type(found_res, "proxmox_sdn_applier") == 1, (
                    "foundation must provision exactly one SDN applier (Req 4.2)"
                )
                # Foundation declares ZERO per-project VNets/subnets (Req 6.1).
                assert self._count_type(found_res, "proxmox_sdn_vnet") == 0
                assert self._count_type(found_res, "proxmox_sdn_subnet") == 0

                # --- 2. Project A: VNet(100) + subnet + applier ------------- #
                with live_apply(
                    "project_template", a_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_a:
                    a_apply = project_a.apply(*a_slug_var)
                assert a_apply.returncode == 0, (
                    f"project A ({_PROJECT_A_SLUG}) apply must succeed "
                    f"(Req 4.3, 4.4, 4.7); output:\n{a_apply.output}"
                )
                a_res = self._state_resources(a_state)
                assert self._count_type(a_res, "proxmox_sdn_vnet") == 1, (
                    "project A must provision exactly one VNet (Req 4.3)"
                )
                assert self._count_type(a_res, "proxmox_sdn_subnet") == 1, (
                    "project A must provision exactly one subnet (Req 4.4)"
                )
                assert self._count_type(a_res, "proxmox_sdn_applier") == 1, (
                    "project A must have its own applier (design approach B)"
                )
                # Project root declares no zone (Req 6.2).
                assert self._count_type(a_res, "proxmox_sdn_zone_vlan") == 0

                # --- 3. Project B: VNet(101) + subnet + applier ------------- #
                with live_apply(
                    "project_template", b_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_b:
                    b_apply = project_b.apply(*b_slug_var)
                assert b_apply.returncode == 0, (
                    f"project B ({_PROJECT_B_SLUG}) apply must succeed while A "
                    f"remains provisioned (Req 4.3, 4.4, 4.7); "
                    f"output:\n{b_apply.output}"
                )
                b_res = self._state_resources(b_state)
                assert self._count_type(b_res, "proxmox_sdn_vnet") == 1, (
                    "project B must provision exactly one VNet (Req 4.3)"
                )
                assert self._count_type(b_res, "proxmox_sdn_subnet") == 1, (
                    "project B must provision exactly one subnet (Req 4.4)"
                )

                # --- 4. Coexistence / isolation evidence (SEC.1, SEC.2) ---- #
                # A's VNet/subnet remain provisioned after B's apply — the two
                # projects' VLANs coexist rather than one clobbering the other.
                a_res_after = self._state_resources(a_state)
                assert self._count_type(a_res_after, "proxmox_sdn_vnet") == 1, (
                    "project A's VNet must still be provisioned after project B "
                    "applies — both VLANs coexist (SEC.2)"
                )

                # Distinct VLAN tags: A carries 100, B carries 101.
                a_tags = self._sdn_attr_values(a_res_after, "proxmox_sdn_vnet", "tag")
                b_tags = self._sdn_attr_values(b_res, "proxmox_sdn_vnet", "tag")
                assert _PROJECT_A_VLAN in a_tags, (
                    f"project A VNet must carry VLAN tag {_PROJECT_A_VLAN}; "
                    f"got {a_tags}"
                )
                assert _PROJECT_B_VLAN in b_tags, (
                    f"project B VNet must carry VLAN tag {_PROJECT_B_VLAN}; "
                    f"got {b_tags}"
                )
                assert set(a_tags).isdisjoint(set(b_tags)), (
                    "project A and B VNets must carry DISTINCT VLAN tags — no "
                    f"shared VLAN across projects (SEC.2); A={a_tags} B={b_tags}"
                )

                # Distinct CIDRs: 10.0.100.0/24 vs 10.0.101.0/24.
                a_cidrs = self._sdn_attr_values(a_res_after, "proxmox_sdn_subnet", "cidr")
                b_cidrs = self._sdn_attr_values(b_res, "proxmox_sdn_subnet", "cidr")
                assert _PROJECT_A_CIDR in a_cidrs, (
                    f"project A subnet CIDR must be {_PROJECT_A_CIDR}; got {a_cidrs}"
                )
                assert _PROJECT_B_CIDR in b_cidrs, (
                    f"project B subnet CIDR must be {_PROJECT_B_CIDR}; got {b_cidrs}"
                )
                assert set(a_cidrs).isdisjoint(set(b_cidrs)), (
                    "project A and B subnets must have DISTINCT CIDRs — each "
                    f"project isolated to its own VLAN (SEC.1, SEC.2); "
                    f"A={a_cidrs} B={b_cidrs}"
                )
            finally:
                # Ephemeral apply: tear everything down in reverse order (B, A,
                # foundation) so the test cluster is left as it was found, even
                # on assertion failure. Each destroy drives its OWN state file.
                with live_apply(
                    "project_template", b_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_b_teardown:
                    project_b_teardown.destroy(*b_slug_var)
                with live_apply(
                    "project_template", a_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_a_teardown:
                    project_a_teardown.destroy(*a_slug_var)
                with live_apply(
                    "foundation", foundation_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as foundation_teardown:
                    foundation_teardown.destroy()
