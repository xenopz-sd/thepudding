"""Destroy-isolation test for the two-root state topology (Proxmox SDN).

Task 10.2 (spec: proxmox-network-foundation) — Requirements 6.5, SEC.1, SEC.2;
design.md "Testing Strategy §3 Integration tests" — the destroy-isolation
guarantee: after `terraform destroy` of ONE project root, the foundation
zone/applier and the OTHER project's VNet/subnet remain present and unmodified.

This is the *state-isolation evidence* for the IaC resource layer. It is NOT a
property-based test: per the testing-strategy steering and design.md, the IaC
layer's behaviour does not vary with generated input and is validated by
plan-idempotency, provider resource-shape assertions, and integration
apply/destroy — never by PBT.

=== TWO PARTS ===

1. ALWAYS-RUNNING static structural assertions (no infra needed).

   These prove the state-isolation design holds *structurally* — i.e. why a
   project `terraform destroy` CANNOT remove shared/other-project resources,
   independent of any live cluster:

     * the project root (infra/projects/_TEMPLATE/) declares NO foundation
       resources — no `proxmox_sdn_zone_vlan` and no foundation applier — so
       destroying a project root's state can only ever remove that project's
       own VNet/subnet/applier, never the shared zone (Requirement 6.5, 6.2).
     * the foundation root (infra/platform-foundation/) declares NO PER-PROJECT
       `proxmox_sdn_vnet` / `proxmox_sdn_subnet` — so no project's resources
       live in the foundation state and a project destroy has no path to them
       (Requirement 6.1). Since NET-00 §6 it DOES own exactly one shared-
       services VLAN-20 VNet/subnet (`p20`, 10.0.20.0/24); that is cluster-wide
       shared infra, not a project's own, so it does not weaken isolation (a
       project destroy still touches only that project's own state).

   Combined with each root owning its OWN separate state (asserted lightly here
   via the distinct backend state names, complementing — not duplicating —
   test_state_and_registry.py, which owns the full state-name schema checks),
   these structural facts are the destroy-isolation guarantee expressed as
   inspectable configuration: separate state + disjoint resource ownership =>
   a per-root `destroy` is blast-radius-bounded to that root.

2. INFRA-GATED live destroy-isolation test (CI only).

   WHEN a terraform/tofu binary AND a Proxmox SDN test cluster are available:
   apply foundation + project A + project B (each into its OWN separate state
   file, mirroring test_integration_apply.py), then `terraform destroy`
   project A's root and assert — via each root's separate state — that:
     * project A's VNet + subnet are GONE (destroy succeeded for A);
     * the foundation zone + applier remain present/unmodified;
     * project B's VNet + subnet remain present/unmodified.
   Because each root has its OWN state, destroying A's root cannot touch B's or
   the foundation's state — this test asserts exactly that (Requirement 6.5,
   the per-root state-isolation guarantee; SEC.1/SEC.2 coexistence preserved).

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
to two SEPARATE state files — which is exactly what makes destroying A's state
byte-incapable of touching foundation's or B's state. State lives in a
disposable, caller-owned temp file the test reads back via the
`_state_resources` JSON parsing below. The committed roots are never modified.

Secret hygiene (Req 3.6): the Proxmox endpoint/token are read from the
`PROXMOX_SDN_TEST_*` env vars and threaded into the subprocess `env` by
`live_apply` (`TF_VAR_proxmox_api_token`) ONLY — the token is never placed on a
command line, printed, or interpolated into an assertion message. Every
assertion below prints only `.output` (Terraform's `-no-color` stdout+stderr,
which does not echo `TF_VAR_*` values).

=== ENVIRONMENT GATING — READ THIS ===

terraform/tofu AND a live test Proxmox cluster with SDN enabled are NOT
available in this environment, so the live destroy-isolation portion CANNOT
execute here. Following the SAME gating idiom as test_integration_apply.py
(reusing the SAME env var names `PROXMOX_SDN_TEST_ENDPOINT` +
`PROXMOX_SDN_TEST_TOKEN`) and the documentation-testing steering rule
"absent infra => skipped, not failed", the live class is `skipif`-gated behind
BOTH:

  * the presence of a `terraform`/`tofu` binary, AND
  * `PROXMOX_SDN_TEST_ENDPOINT` plus `PROXMOX_SDN_TEST_TOKEN`.

When any is absent the live class is SKIPPED with an explicit
"requires-infra, skipped: ..." reason.

The live destroy-isolation evidence is therefore **infra-gated / plan-only in
this environment and MUST run in CI** where a terraform/tofu toolchain and an
ephemeral Proxmox SDN test cluster are available. `validation.md` (task 11.3)
references this file and MUST record the infra-gating explicitly rather than
claiming the destroy-isolation behaviour was verified live locally.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_destroy_isolation.py -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from conftest import live_apply

# --------------------------------------------------------------------------- #
# Locate the infra roots relative to this test file (infra/tests/...).
# Mirrors the layout resolution in test_integration_apply.py.
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent
_FOUNDATION_DIR = _INFRA_DIR / "platform-foundation"
_PROJECT_TEMPLATE_DIR = _INFRA_DIR / "projects" / "_TEMPLATE"

# Two registered projects with distinct, adjacent VLAN IDs (must already exist
# in infra/platform-foundation/projects.yaml so each project root can resolve
# its vlan_id via the yamldecode() registry lookup). Same fixture as
# test_integration_apply.py so the two integration tests are consistent.
_PROJECT_A_SLUG = "dronefleet"
_PROJECT_A_VLAN = 100
_PROJECT_A_CIDR = "10.0.100.0/24"

_PROJECT_B_SLUG = "mlvideo"
_PROJECT_B_VLAN = 101
_PROJECT_B_CIDR = "10.0.101.0/24"

# Proxmox SDN test-cluster env vars — the SAME names used by
# test_integration_apply.py (both required for the live gate).
_SDN_TEST_ENDPOINT_ENV = "PROXMOX_SDN_TEST_ENDPOINT"
_SDN_TEST_TOKEN_ENV = "PROXMOX_SDN_TEST_TOKEN"


def _terraform_binary() -> str | None:
    """Return the terraform (or tofu) binary path, or None if neither exists."""
    return shutil.which("terraform") or shutil.which("tofu")


def _live_destroy_gate_reason() -> str | None:
    """Return an explicit skip reason if the live destroy-isolation gate is NOT
    satisfied, else None.

    Same "absent infra => skip, never fail" pattern (and same env-var names) as
    test_integration_apply.py::_live_apply_gate_reason.
    """
    if _terraform_binary() is None:
        return (
            "requires-infra, skipped: no terraform/tofu binary available to "
            "drive an apply + destroy against a Proxmox SDN test cluster"
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


# --------------------------------------------------------------------------- #
# Part 1 — ALWAYS-RUNNING static structural assertions.
#
# These prove the destroy-isolation design holds structurally, with no infra.
# They stay COMPLEMENTARY to test_state_and_registry.py: that file owns the full
# state-name schema (placeholder patterns, uniqueness, negative cases); here we
# only assert the minimal state-name distinctness needed to make the
# destroy-isolation argument, and focus instead on RESOURCE-OWNERSHIP disjointness
# (which test_state_and_registry.py does not cover).
# --------------------------------------------------------------------------- #
_FOUNDATION_SDN_TF = _FOUNDATION_DIR / "sdn.tf"
_PROJECT_SDN_TF = _PROJECT_TEMPLATE_DIR / "sdn.tf"
_FOUNDATION_VERSIONS_TF = _FOUNDATION_DIR / "versions.tf"
_PROJECT_VERSIONS_TF = _PROJECT_TEMPLATE_DIR / "versions.tf"

# A `resource "<type>" "<name>"` declaration for a given resource type.
def _resource_decl_re(resource_type: str) -> re.Pattern:
    return re.compile(
        r'resource\s+"' + re.escape(resource_type) + r'"\s+"[^"]+"\s*\{'
    )


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


def _declares_resource(tf_text: str, resource_type: str) -> bool:
    """True if the HCL text declares at least one `resource "<type>" ...` block.

    Matches only real resource declarations, not mentions inside comments/prose
    (comment references use the bare type name without the `resource "..." "..."`
    declaration syntax, so the regex does not match them).
    """
    return _resource_decl_re(resource_type).search(tf_text) is not None


def _count_resource_decls(tf_text: str, resource_type: str) -> int:
    """Number of `resource "<type>" "<name>" {` declarations in the HCL text."""
    return len(_resource_decl_re(resource_type).findall(tf_text))


def _resource_blocks(tf_text: str, resource_type: str) -> list[str]:
    """Return the brace-balanced body of every `resource "<type>"` block.

    Simple brace-matching scanner — robust to whitespace/formatting. Good
    enough for the well-formed, project-authored .tf files here (no braces
    inside strings in these SDN resource blocks).
    """
    blocks: list[str] = []
    for m in _resource_decl_re(resource_type).finditer(tf_text):
        start = tf_text.index("{", m.start())
        depth = 0
        for i in range(start, len(tf_text)):
            c = tf_text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(tf_text[start : i + 1])
                    break
    return blocks


class TestDestroyIsolationStructure:
    """Static, infra-free proof that a project destroy cannot remove shared or
    other-project SDN resources (Requirements 6.1, 6.2, 6.5).

    Since NET-00 §6 the foundation root owns exactly one SHARED-services VLAN-20
    VNet/subnet (`p20`, 10.0.20.0/24) in addition to the zone + applier; the
    isolation guarantee asserted here is that no PER-PROJECT VNet/subnet lives
    in foundation state, which the tests below enforce by pinning the single
    foundation VNet/subnet to their fixed shared literals."""

    def test_project_root_declares_no_foundation_zone(self):
        """The project root must NOT declare the cluster-wide SDN zone.

        If a project root declared its own `proxmox_sdn_zone_vlan`, a
        `terraform destroy` of that root would tear down the shared zone every
        other project attaches to — the exact cross-project blast radius the
        two-root split exists to prevent (Requirement 6.2, 6.5). Because the
        zone lives ONLY in the foundation root, destroying any project root can
        never remove it.
        """
        text = _read(_PROJECT_SDN_TF)
        assert not _declares_resource(text, "proxmox_sdn_zone_vlan"), (
            "project root must NOT declare a proxmox_sdn_zone_vlan — the zone is "
            "owned solely by the foundation root, so a project destroy cannot "
            "remove it (Requirement 6.2, 6.5)"
        )

    def test_project_root_references_zone_by_id_not_by_declaration(self):
        """Sanity check: the project root references the zone by id (a variable),
        not by declaring/importing it — approach B, no cross-root state coupling.
        """
        text = _read(_PROJECT_SDN_TF)
        # It DOES declare its own VNet/subnet/applier ...
        assert _declares_resource(text, "proxmox_sdn_vnet")
        assert _declares_resource(text, "proxmox_sdn_subnet")
        assert _declares_resource(text, "proxmox_sdn_applier")
        # ... and references the shared zone via the sdn_zone_id input variable
        # rather than a terraform_remote_state data source (which would recouple
        # the project's plan to the foundation state).
        assert "var.sdn_zone_id" in text, (
            "project root should reference the foundation zone by id "
            "(var.sdn_zone_id), keeping its plan decoupled from foundation state"
        )
        # Must not declare a `data "terraform_remote_state" ...` block reading
        # the foundation state (which would recouple the plans). Match only the
        # actual data-source declaration syntax, not a prose mention of the term
        # in a comment (the sdn.tf header explains WHY it avoids remote state).
        remote_state_data_decl = re.compile(
            r'data\s+"terraform_remote_state"\s+"[^"]+"\s*\{'
        )
        assert remote_state_data_decl.search(text) is None, (
            "project root must NOT read foundation state via a "
            'data "terraform_remote_state" block (approach B avoids cross-root '
            "coupling)"
        )

    def test_foundation_root_declares_no_per_project_vnet_or_subnet(self):
        """The foundation root must declare NO PER-PROJECT VNet/subnet.

        The destroy-isolation guarantee is that no *project's* VNet/subnet lives
        in the foundation state, so a project `terraform destroy` (which only
        ever touches that project's OWN state) has no path to any resource in
        the foundation state (Requirement 6.1, 6.5).

        Since NET-00 §6, the foundation root DOES own exactly ONE VNet/subnet —
        the shared-services VLAN-20 `p20` (10.0.20.0/24). That is cluster-wide
        SHARED infrastructure, not a project's own, so it does not break
        destroy-isolation: a project destroy still touches only that project's
        own state, never the foundation state that holds `p20`.

        `_declares_resource` alone can't tell a shared VNet from a per-project
        one, so this test asserts the more precise facts that would still FAIL
        if someone wrongly added a per-project (interpolated / registry-driven)
        VNet or subnet to the foundation root:

          * the foundation root declares exactly ONE `proxmox_sdn_vnet`, and its
            `id` is the LITERAL `"p20"` (tag 20) — not a `p${...}` interpolation
            and not a `local.vlan_id`/`yamldecode`-derived id;
          * the foundation root declares exactly ONE `proxmox_sdn_subnet`, and
            its `cidr` is the LITERAL shared `"10.0.20.0/24"` — not a
            per-project `10.0.${...}.0/24` interpolation.
        """
        text = _read(_FOUNDATION_SDN_TF)

        # --- Exactly one VNet, and it is the literal shared-services p20 ------ #
        vnet_count = _count_resource_decls(text, "proxmox_sdn_vnet")
        assert vnet_count == 1, (
            "foundation root must declare exactly ONE proxmox_sdn_vnet — the "
            "shared-services VLAN-20 `p20` (NET-00 §6); any additional VNet "
            "would be a per-project VNet living in foundation state, breaking "
            f"destroy isolation (Requirement 6.1, 6.5). found {vnet_count}"
        )
        vnet_block = _resource_blocks(text, "proxmox_sdn_vnet")[0]
        assert re.search(r'^\s*id\s*=\s*"p20"', vnet_block, re.MULTILINE), (
            "the foundation VNet's id must be the LITERAL \"p20\" (the shared "
            "VLAN-20 VNet, NET-00 §6) — NOT a per-project interpolated / "
            "registry-driven id (`p${...}`, `local.vlan_id`, `yamldecode(...)`); "
            "a per-project VNet in foundation state would break destroy "
            "isolation (Requirement 6.1, 6.5)"
        )
        # Belt-and-braces: the VNet id must not be interpolated at all.
        assert "${" not in re.search(
            r'id\s*=\s*("[^"]*"|[^\n]*)', vnet_block
        ).group(1), (
            "the foundation VNet id must be a fixed literal, not interpolated — "
            "an interpolated id signals a per-project VNet wrongly placed in the "
            "foundation root (Requirement 6.1, 6.5)"
        )

        # --- Exactly one subnet, and it is the literal shared-services CIDR --- #
        subnet_count = _count_resource_decls(text, "proxmox_sdn_subnet")
        assert subnet_count == 1, (
            "foundation root must declare exactly ONE proxmox_sdn_subnet — the "
            "shared-services 10.0.20.0/24 (NET-00 §6); any additional subnet "
            "would be a per-project subnet living in foundation state, breaking "
            f"destroy isolation (Requirement 6.1, 6.5). found {subnet_count}"
        )
        subnet_block = _resource_blocks(text, "proxmox_sdn_subnet")[0]
        assert re.search(
            r'^\s*cidr\s*=\s*"10\.0\.20\.0/24"', subnet_block, re.MULTILINE
        ), (
            "the foundation subnet's cidr must be the LITERAL shared "
            "\"10.0.20.0/24\" (NET-00 §6) — NOT a per-project "
            "`10.0.${local.vlan_id}.0/24` interpolation; a per-project subnet in "
            "foundation state would break destroy isolation (Requirement 6.1, 6.5)"
        )

    def test_foundation_root_owns_exactly_the_shared_zone_and_applier(self):
        """Sanity check: the foundation root DOES own the shared zone + applier
        (so they are NOT in any project state and survive a project destroy).

        Since NET-00 §6 it also owns the shared-services VLAN-20 `p20` VNet +
        subnet — likewise cluster-wide shared infra that lives ONLY in the
        foundation state and thus survives any project destroy."""
        text = _read(_FOUNDATION_SDN_TF)
        assert _declares_resource(text, "proxmox_sdn_zone_vlan"), (
            "foundation root must declare the single shared SDN zone (Req 4.1)"
        )
        assert _declares_resource(text, "proxmox_sdn_applier"), (
            "foundation root must declare the single foundation applier (Req 4.2)"
        )
        # The shared-services VLAN-20 VNet/subnet are foundation-owned too
        # (NET-00 §6) — cluster-wide shared infra, not a project's own.
        assert _declares_resource(text, "proxmox_sdn_vnet"), (
            "foundation root must declare the shared-services VLAN-20 `p20` VNet "
            "(NET-00 §6)"
        )
        assert _declares_resource(text, "proxmox_sdn_subnet"), (
            "foundation root must declare the shared-services VLAN-20 subnet "
            "10.0.20.0/24 (NET-00 §6)"
        )

    def test_roots_use_distinct_backend_state_names(self):
        """Light distinctness check (full schema owned by test_state_and_registry).

        The two roots must resolve to DIFFERENT GitLab-managed state names, so
        each root's `terraform destroy` operates on its OWN state file and
        cannot mutate the other's. We assert only the minimal fact the
        destroy-isolation argument needs: the foundation documents
        `platform-foundation` and the project template documents a distinct
        `<slug>-infra` pattern. The exhaustive state-name schema/negative tests
        live in test_state_and_registry.py — not duplicated here.
        """
        foundation_versions = _read(_FOUNDATION_VERSIONS_TF)
        project_versions = _read(_PROJECT_VERSIONS_TF)

        addr_re = re.compile(
            r"/terraform/state/(?P<name>[A-Za-z0-9._<>-]+?)(?:/lock)?[\"'\s]"
        )
        foundation_names = {m.group("name") for m in addr_re.finditer(foundation_versions)}
        project_names = {m.group("name") for m in addr_re.finditer(project_versions)}

        assert "platform-foundation" in foundation_names, (
            "foundation root must document the 'platform-foundation' state name"
        )
        project_infra = {n for n in project_names if n.endswith("-infra")}
        assert project_infra, (
            "project template must document a '<slug>-infra' state-name pattern"
        )
        # The destroy-isolation-critical fact: the two roots' state names are
        # disjoint, so neither root's destroy can touch the other's state.
        assert "platform-foundation" not in project_infra
        assert not (foundation_names & project_infra), (
            "foundation and project state-name sets must be disjoint so a "
            "per-root destroy is blast-radius-bounded to that root; overlap="
            f"{sorted(foundation_names & project_infra)}"
        )


# --------------------------------------------------------------------------- #
# Part 2 — INFRA-GATED live destroy-isolation test (CI only).
# --------------------------------------------------------------------------- #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_destroy_gate_reason() is not None,
    reason=_live_destroy_gate_reason() or "live destroy-isolation gate satisfied",
)
class TestLiveDestroyIsolation:
    """Apply foundation + A + B, destroy A, assert B and foundation survive.

    Runs ONLY when a terraform/tofu binary AND a Proxmox SDN test-cluster
    endpoint + token are all present. In THIS environment none are available,
    so the whole class is SKIPPED — this evidence is infra-gated and MUST run in
    CI (see module docstring and validation.md, task 11.3).

    Each root is applied into its OWN separate state file via the shared
    `live_apply` helper (spec: fix-live-apply-backend-init), exactly mirroring
    test_integration_apply.py. Because each `(root, state)` invocation builds its
    OWN sandbox + transient `backend "local" { path }` override -> OWN state
    file, destroying project A's root operates only on A's state file; the
    assertions below confirm the foundation and project-B states are untouched
    (Requirement 6.5).
    """

    def _state_resources(self, state_path: Path) -> list[dict]:
        if not state_path.is_file():
            return []
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            return []
        return state.get("resources", [])

    def _count_type(self, resources: list[dict], type_prefix: str) -> int:
        total = 0
        for res in resources:
            if str(res.get("type", "")).startswith(type_prefix):
                total += len(res.get("instances", []))
        return total

    def test_destroy_project_a_leaves_foundation_and_project_b_intact(self):
        """Destroy project A; foundation zone/applier and project B survive.

        Steps (all against the ephemeral Proxmox SDN test cluster):
          1. apply foundation, project A (vlan 100), project B (vlan 101)
          2. capture foundation + B state BEFORE A's destroy
          3. `terraform destroy` project A's root (only A's state file)
          4. assert A's VNet/subnet are GONE from A's state
          5. assert foundation zone/applier UNMODIFIED (byte-identical state)
          6. assert project B's VNet/subnet UNMODIFIED (byte-identical state)

        Each apply/destroy is driven through `live_apply` into its OWN state
        file, so destroying project A's root operates only on A's own sandbox +
        state — it structurally cannot touch foundation's or B's state.
        """
        endpoint = os.environ[_SDN_TEST_ENDPOINT_ENV]
        token = os.environ[_SDN_TEST_TOKEN_ENV]

        a_slug_var = ("-var", f"project_slug={_PROJECT_A_SLUG}")
        b_slug_var = ("-var", f"project_slug={_PROJECT_B_SLUG}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            foundation_state = tmp_path / "foundation.tfstate"
            a_state = tmp_path / "project_a.tfstate"
            b_state = tmp_path / "project_b.tfstate"

            try:
                # --- 1. Apply foundation + A + B ---------------------------- #
                with live_apply(
                    "foundation", foundation_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as foundation:
                    found_apply = foundation.apply()
                assert found_apply.returncode == 0, (
                    f"foundation apply must succeed; output:\n{found_apply.output}"
                )
                with live_apply(
                    "project_template", a_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_a:
                    a_apply = project_a.apply(*a_slug_var)
                assert a_apply.returncode == 0, (
                    f"project A apply must succeed; output:\n{a_apply.output}"
                )
                with live_apply(
                    "project_template", b_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_b:
                    b_apply = project_b.apply(*b_slug_var)
                assert b_apply.returncode == 0, (
                    f"project B apply must succeed; output:\n{b_apply.output}"
                )

                # Sanity: A and B each provisioned their VNet + subnet.
                assert self._count_type(self._state_resources(a_state),
                                        "proxmox_sdn_vnet") == 1
                assert self._count_type(self._state_resources(b_state),
                                        "proxmox_sdn_vnet") == 1

                # --- 2. Snapshot foundation + B state BEFORE A's destroy ---- #
                foundation_before = foundation_state.read_bytes()
                b_before = b_state.read_bytes()

                # --- 3. Destroy ONLY project A's root ----------------------- #
                with live_apply(
                    "project_template", a_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_a_destroy:
                    a_destroy = project_a_destroy.destroy(*a_slug_var)
                assert a_destroy.returncode == 0, (
                    f"project A destroy must succeed; output:\n{a_destroy.output}"
                )

                # --- 4. A's VNet/subnet are GONE ---------------------------- #
                a_after = self._state_resources(a_state)
                assert self._count_type(a_after, "proxmox_sdn_vnet") == 0, (
                    "project A's VNet must be gone after A's destroy"
                )
                assert self._count_type(a_after, "proxmox_sdn_subnet") == 0, (
                    "project A's subnet must be gone after A's destroy"
                )

                # --- 5. Foundation state UNMODIFIED (Req 6.5) --------------- #
                # A's destroy touched only A's state file; the foundation state
                # file is byte-identical and its zone + applier still present.
                assert foundation_state.read_bytes() == foundation_before, (
                    "foundation state must be byte-identical after project A's "
                    "destroy — a per-root destroy cannot touch the foundation "
                    "state (Requirement 6.5)"
                )
                found_after = self._state_resources(foundation_state)
                assert self._count_type(found_after, "proxmox_sdn_zone_vlan") == 1, (
                    "foundation SDN zone must remain present after A's destroy "
                    "(Requirement 6.5)"
                )
                assert self._count_type(found_after, "proxmox_sdn_applier") == 1, (
                    "foundation applier must remain present after A's destroy "
                    "(Requirement 6.5)"
                )

                # --- 6. Project B state UNMODIFIED (Req 6.5, SEC.2) --------- #
                assert b_state.read_bytes() == b_before, (
                    "project B state must be byte-identical after project A's "
                    "destroy — destroying one project cannot touch another's "
                    "state (Requirement 6.5, SEC.2)"
                )
                b_after = self._state_resources(b_state)
                assert self._count_type(b_after, "proxmox_sdn_vnet") == 1, (
                    "project B's VNet must remain present after A's destroy "
                    "(Requirement 6.5, SEC.2)"
                )
                assert self._count_type(b_after, "proxmox_sdn_subnet") == 1, (
                    "project B's subnet must remain present after A's destroy "
                    "(Requirement 6.5, SEC.2)"
                )
            finally:
                # Ephemeral apply: tear down everything still standing (B and
                # foundation; A is already destroyed) so the cluster is left as
                # found, even on assertion failure. Each destroy drives its OWN
                # state file via its own live_apply sandbox.
                with live_apply(
                    "project_template", b_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as project_b_teardown:
                    project_b_teardown.destroy(*b_slug_var)
                with live_apply(
                    "foundation", foundation_state,
                    endpoint=endpoint, token=token, insecure=True,
                ) as foundation_teardown:
                    foundation_teardown.destroy()
