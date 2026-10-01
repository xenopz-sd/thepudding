"""Remaining degraded-mode tests for the Proxmox Network Foundation (task 9.2).

Spec: proxmox-network-foundation — Requirements 4.7, 5.5, FD.1, FD.2, FD.3,
FD.4; design.md "Testing Strategy §4 (Degraded-mode tests)" and the
"Error Handling" table.

This file covers the *four remaining* degraded modes from design.md §4 that are
NOT already covered by the dedicated `SDN.Use` file
(`test_degraded_sdn_use.py`, task 9.1):

  1. **Unregistered slug** (Req 5.5, FD.3) — a project root planned for a slug
     absent from `projects.yaml` must fail on the
     `terraform_data.assert_registered` precondition and provision nothing.
       * terraform-gated: a `terraform plan` proving the precondition fails runs
         OFFLINE via the shared `offline_plan` helper
         (`infra/tests/conftest.py`) — a sandbox copy of both roots, a transient
         `backend "local" {}` override + `init -reconfigure`, and dummy,
         non-contacting `proxmox_endpoint` / `proxmox_api_token` provider vars,
         so no live GitLab HTTP backend / `CI_JOB_TOKEN` / Proxmox credential is
         needed. It is gated behind a terraform/tofu binary and SKIPPED (never
         FAILED) when the binary or the offline prerequisite is absent.
       * static (runs now): asserts the precondition asserting
         `local.vlan_id != null` EXISTS in the project template `main.tf`.

  2. **Applier failure** (Req 4.7, FD.1) — an SDN commit failure must surface
     as an apply failure and NOT report the VLAN as provisioned.
       * terraform+infra-gated: observing a real commit failure needs a live
         cluster; documented + SKIPPED here.
       * static (runs now): asserts each project applier resource exists with a
         `replace_triggered_by` watching its own VNet + subnet, so a failed
         commit is tied to a real, tracked apply step (not silently swallowed).

  3. **Malformed / unreadable / duplicate registry** (FD.2) — the onboarding
     script must abort and report without assigning and without opening an MR.
       * runs NOW in pure Python by importing
         `onboard_project.onboard` / `OnboardingError` and driving corrupt,
         missing, unreadable, and duplicate-slug registries against a fake MR
         opener; asserts `OnboardingError` and ZERO MR calls.
       * NOTE: task 2.3's `test_onboard_project.py` already covers several FD.2
         cases. These are COMPLEMENTARY, degraded-mode-focused cases (an
         unreadable-permissions file, a directory-as-registry, a truly empty
         file, a tab-indented YAML corruption, a duplicate slug that reaches
         the abort *without* an MR), living in this dedicated degraded-mode
         file so the FD.2 evidence is discoverable from one place. They avoid
         re-asserting the identical scenarios in 2.3.

  4. **Trunk precondition** (FD.4) — the physical switch trunk is out-of-band
     (NET-00 §8); Terraform has no visibility into it, so a *correct* SDN apply
     plus a *guest-connectivity* failure is the documented, expected signature
     of an unconfigured trunk — not a Terraform error. FD.4 is validated by
     inspection/documentation, never by an automated switch test.
       * runs NOW as a documentation-presence assertion: the out-of-band trunk
         rationale must be documented in the source (NET-00 §8 and the project
         template `sdn.tf`), plus an explicit `xfail`-style skip carrying the
         FD.4 rationale text so the reasoning is visible in the test report.

Run:  PYTHONPATH=infra/platform-foundation/scripts \\
        ~/venv/devinfra/bin/pytest infra/tests/test_degraded_modes.py -v
"""

from __future__ import annotations

import datetime
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Locate the infra roots relative to this test file (infra/tests/...).
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _INFRA_DIR.parent
_FOUNDATION_DIR = _INFRA_DIR / "platform-foundation"
_PROJECT_TEMPLATE_DIR = _INFRA_DIR / "projects" / "_TEMPLATE"

_PROJECT_MAIN_TF = _PROJECT_TEMPLATE_DIR / "main.tf"
_PROJECT_SDN_TF = _PROJECT_TEMPLATE_DIR / "sdn.tf"
_FOUNDATION_SDN_TF = _FOUNDATION_DIR / "sdn.tf"
_NET00_DOC = _REPO_ROOT / "requirements" / "NET-00-vlan-ip-addressing-plan.md"

# Make the onboarding script + derivation package importable regardless of the
# caller's PYTHONPATH (mirrors scripts/tests/conftest.py). The task also permits
# invoking pytest with PYTHONPATH=infra/platform-foundation/scripts; this belt
# guarantees the import works either way.
_SCRIPTS_DIR = _FOUNDATION_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import onboard_project  # noqa: E402  (import after sys.path shim)
from onboard_project import Allocation, OnboardingError, onboard  # noqa: E402

# Shared offline-plan helper (infra/tests/conftest.py, Change 1). Used by the
# unregistered-slug gated test to run `terraform plan` offline against a
# transient local backend, without a live GitLab HTTP backend / credentials.
from conftest import offline_plan  # noqa: E402

#: A fixed onboarding date so any computed Allocation is deterministic.
_FIXED_TODAY = datetime.date(2024, 8, 15)


# --------------------------------------------------------------------------- #
# Shared helpers.
# --------------------------------------------------------------------------- #
def _read(path: Path) -> str:
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


def _terraform_binary() -> str | None:
    """Return the terraform (or tofu) binary path, or None if neither exists."""
    return shutil.which("terraform") or shutil.which("tofu")


requires_terraform = pytest.mark.skipif(
    _terraform_binary() is None,
    reason=(
        "requires-infra, skipped: no terraform/tofu binary available to drive "
        "a live plan/apply"
    ),
)


class _FakeMergeRequestOpener:
    """A recording fake MR seam — never performs any network I/O.

    Implements the ``onboard_project.MergeRequestOpener`` protocol. If a
    degraded-mode abort ever (wrongly) reached the MR step, ``calls`` would be
    non-empty and the assertions below would catch the leaked side effect.
    """

    def __init__(self) -> None:
        self.calls: list[Allocation] = []

    def open_or_update(self, allocation: Allocation) -> dict[str, Any]:
        self.calls.append(allocation)
        return {"iid": 0, "web_url": "https://gitlab.example/mr/0"}


@pytest.fixture
def fake_opener() -> _FakeMergeRequestOpener:
    return _FakeMergeRequestOpener()


# =========================================================================== #
# 1. UNREGISTERED SLUG — Requirements 5.5, FD.3
# =========================================================================== #
class TestUnregisteredSlugPrecondition:
    """A project root planned for an unregistered slug must fail and provision
    nothing (Requirements 5.5, FD.3)."""

    # ---- Static assertion (RUNS NOW) ------------------------------------- #
    def test_project_main_tf_has_vlan_id_not_null_precondition(self):
        """The `local.vlan_id != null` precondition EXISTS in the project
        template `main.tf` (Requirement 5.5, FD.3).

        This is the guard that makes an unregistered slug fail *before* any SDN
        resource is created (design.md "How a Project Root reads its vlan_id").
        We assert on the committed source so the guard's presence is proven even
        where terraform cannot run.
        """
        text = _read(_PROJECT_MAIN_TF)

        # There must be a `terraform_data.assert_registered` precondition anchor.
        assert re.search(
            r'resource\s+"terraform_data"\s+"assert_registered"', text
        ), (
            "project template main.tf must declare the "
            "`terraform_data.assert_registered` precondition anchor "
            "(Requirement 5.5, FD.3)"
        )

        # It must carry a `precondition` block ...
        assert re.search(r"precondition\s*\{", text), (
            "project template main.tf must declare a `precondition` block "
            "gating provisioning on registry membership (Requirement 5.5)"
        )

        # ... whose condition asserts `local.vlan_id != null`. Tolerate arbitrary
        # inner whitespace around the operator.
        assert re.search(r"local\.vlan_id\s*!=\s*null", text), (
            "the registration precondition must assert `local.vlan_id != null` "
            "so an unregistered slug fails the plan/apply (Requirement 5.5, "
            "FD.3); condition not found in main.tf"
        )

        # And it must surface an actionable error message (not a silent fail).
        assert re.search(r"error_message\s*=", text), (
            "the registration precondition must carry an `error_message` "
            "(surfaced, non-silent failure — FD.3)"
        )

    def test_unregistered_slug_is_actually_absent_from_registry(self):
        """Sanity: the slug we use for the (gated) plan is genuinely absent.

        Guards against the terraform-gated test below silently passing because
        the 'unregistered' slug was accidentally added to the registry.
        """
        registry = onboard_project.load_registry(
            _FOUNDATION_DIR / "projects.yaml"
        )
        slugs = set(onboard_project.existing_slugs(registry))
        assert "ghost-proj" not in slugs

    # ---- Live plan (terraform-gated, runs offline via the shared helper) - #
    @requires_terraform
    def test_plan_fails_for_unregistered_slug_and_provisions_nothing(self):
        """`terraform plan` for an unregistered slug fails on the precondition
        and provisions nothing (Requirements 5.5, FD.3).

        Gated behind a terraform/tofu binary; SKIPPED where absent (per
        documentation-testing steering: absent infra => skipped, not failed).

        Runs entirely offline via the shared `offline_plan` helper
        (infra/tests/conftest.py): a sandbox copy of both roots, a transient
        `backend "local" {}` override + `init -reconfigure`, and dummy
        (non-contacting) `proxmox_endpoint` / `proxmox_api_token` provider vars.
        No live GitLab HTTP backend, `CI_JOB_TOKEN`, or Proxmox credential is
        needed — the plan fails on the `assert_registered` registration
        precondition (and the null `local.vlan_id` locals evaluation), before
        any real provider call.

        Uses the slug `ghost-proj` — valid `project_slug` format (lowercase,
        hyphenated, 10 chars <= 20 per PF §4) but absent from `projects.yaml`,
        so the failure is the *registration* guard rather than variable
        validation.
        """
        with offline_plan(
            "project_template", "-var", "project_slug=ghost-proj"
        ) as result:
            # (1) The plan MUST fail — an unregistered slug is never a success.
            assert result.returncode != 0, (
                "plan for an unregistered slug must FAIL on the "
                "assert_registered precondition (Requirements 5.5, FD.3), but "
                f"it exited 0.\noutput:\n{result.output}"
            )

            # (2) The failure must name the precondition / registry, not be an
            # unrelated error. Keep the verified marker set; `is null` is a
            # tolerant extra for Terraform versions where only the
            # null-interpolation error surfaces.
            combined = result.output.lower()
            assert any(
                marker in combined
                for marker in ("precondition", "not present", "projects.yaml",
                               "onboard", "is null")
            ), (
                "the surfaced failure must be the registration precondition "
                f"(Req 5.5/FD.3). Got:\noutput:\n{result.output}"
            )

            # (3) Nothing provisioned: version-robust signal. A failed plan on
            # Terraform 1.16.0 still writes the `-out` file, but
            # `terraform show -json` on it reports zero resource_changes, which
            # is the reliable "provisions nothing" evidence (design.md Change 4).
            assert result.resource_changes == [], (
                "a failed plan for an unregistered slug must provision nothing "
                "— `terraform show -json` on the emitted plan must report zero "
                f"resource_changes (FD.3). Got: {result.resource_changes!r}"
            )


# =========================================================================== #
# 2. APPLIER FAILURE — Requirements 4.7, FD.1
# =========================================================================== #
class TestApplierFailureIsTracked:
    """An SDN commit failure must surface as an apply failure and not report the
    VLAN provisioned (Requirements 4.7, FD.1).

    Observing a *real* commit failure requires a live cluster, so the live
    portion is documented + SKIPPED. The static portion asserts the applier
    resource exists and is wired via `replace_triggered_by` to this project's
    VNet + subnet, so a commit is always a tracked, fail-surfacing apply step.
    """

    # ---- Static assertion (RUNS NOW) ------------------------------------- #
    def test_project_applier_exists_with_replace_triggered_by(self):
        """The project applier exists and watches its own VNet + subnet via
        `replace_triggered_by` (Requirement 4.7, FD.1)."""
        text = _read(_PROJECT_SDN_TF)

        assert re.search(
            r'resource\s+"proxmox_sdn_applier"\s+"project"', text
        ), (
            "project template sdn.tf must declare exactly one project applier "
            "(`proxmox_sdn_applier.project`) so the SDN commit is a real, "
            "tracked apply step (Requirement 4.7, FD.1)"
        )

        assert re.search(r"replace_triggered_by\s*=\s*\[", text), (
            "the project applier must use `replace_triggered_by` so a change to "
            "the VNet/subnet re-commits it (Requirement 4.7)"
        )

        # The trigger list must reference this project's VNet and subnet, so an
        # apply that changes them re-runs (and can surface a commit failure).
        assert re.search(r"proxmox_sdn_vnet\.project", text), (
            "applier `replace_triggered_by` must watch proxmox_sdn_vnet.project"
        )
        assert re.search(r"proxmox_sdn_subnet\.project", text), (
            "applier `replace_triggered_by` must watch proxmox_sdn_subnet.project"
        )

    # ---- Live apply (terraform+infra-gated, SKIPPED here) ---------------- #
    # Marked requires_infra for consistency with the other live-cluster tests:
    # the canonical offline command deselects it (it is a live-cluster test),
    # and it stays unconditionally skipped when opted into (its own skip reason
    # is unchanged). See pytest.ini for the deselection rationale.
    @pytest.mark.requires_infra
    @pytest.mark.skip(
        reason=(
            "requires-infra, skipped: observing a real SDN applier commit "
            "failure (Req 4.7/FD.1) needs a live Proxmox cluster whose SDN "
            "commit can be made to fail. Documented here; exercised in CI "
            "against an ephemeral cluster per validation.md (task 11.3). The "
            "expected behaviour: `terraform apply` exits non-zero and no "
            "proxmox_sdn_* resource is reported provisioned in state."
        )
    )
    def test_applier_commit_failure_surfaces_and_provisions_nothing(self):
        # Intentionally unimplemented body — this is a documented, infra-gated
        # placeholder whose skip reason carries the Req 4.7 / FD.1 rationale.
        raise AssertionError("must run against a live cluster; see skip reason")


# =========================================================================== #
# 3. MALFORMED / UNREADABLE / DUPLICATE REGISTRY — FD.2  (RUNS NOW)
# =========================================================================== #
class TestRegistryDegradedModes:
    """The onboarding script aborts and reports (never assigns, never opens an
    MR) on a corrupt / unreadable / missing / duplicate registry (FD.2).

    These are COMPLEMENTARY to task 2.3's `test_onboard_project.py`: they add
    degraded-mode-focused cases (unreadable permissions, directory-as-registry,
    empty file, tab-corrupted YAML) rather than re-asserting the identical
    scenarios already covered there.
    """

    def test_unreadable_registry_permissions_aborts_no_mr(
        self, tmp_path: Path, fake_opener: _FakeMergeRequestOpener
    ):
        """A registry file that exists but is not readable aborts with
        OnboardingError and opens no MR (FD.2).

        Complements 2.3's *missing*-file case with a present-but-unreadable
        file — a distinct OS failure mode (PermissionError, mapped to an
        OnboardingError via the OSError branch in load_registry).
        """
        reg = tmp_path / "projects.yaml"
        reg.write_text(
            "projects:\n"
            "  - slug: a\n"
            "    vlan_id: 100\n"
            '    onboarding_date: "2024-01-01"\n',
            encoding="utf-8",
        )
        reg.chmod(0o000)
        try:
            # Running as root can bypass file-permission bits; skip cleanly if
            # the chmod did not actually make the file unreadable.
            try:
                reg.read_text(encoding="utf-8")
                pytest.skip(
                    "file permissions not enforced for this user (likely "
                    "root); cannot simulate an unreadable registry"
                )
            except PermissionError:
                pass

            with pytest.raises(OnboardingError):
                onboard("anyslug", reg, fake_opener, today=_FIXED_TODAY)
            assert fake_opener.calls == [], "no MR may be opened on abort (FD.2)"
        finally:
            # Restore perms so tmp_path cleanup can remove the file.
            reg.chmod(0o644)

    def test_directory_instead_of_registry_file_aborts_no_mr(
        self, tmp_path: Path, fake_opener: _FakeMergeRequestOpener
    ):
        """A path that is a directory, not a file, aborts with OnboardingError
        and opens no MR (FD.2 — unreadable registry)."""
        reg_dir = tmp_path / "projects.yaml"
        reg_dir.mkdir()

        with pytest.raises(OnboardingError):
            onboard("anyslug", reg_dir, fake_opener, today=_FIXED_TODAY)
        assert fake_opener.calls == []

    def test_empty_registry_file_aborts_or_starts_clean_no_partial_mr(
        self, tmp_path: Path, fake_opener: _FakeMergeRequestOpener
    ):
        """A truly EMPTY registry file (zero bytes) is handled deterministically.

        An empty file parses to YAML `None`; the loader normalises that to an
        empty projects list, so onboarding a first slug legitimately succeeds at
        VLAN 100 (design.md: empty registry -> 100). The degraded-mode contract
        we assert is that the outcome is deterministic and consistent — either a
        clean allocation with exactly one MR, or a reported abort with none —
        never a partial write that leaves the MR half-done.
        """
        reg = tmp_path / "projects.yaml"
        reg.write_text("", encoding="utf-8")

        try:
            result = onboard("first", reg, fake_opener, today=_FIXED_TODAY)
        except OnboardingError:
            # Acceptable degraded outcome: reported abort, no MR side effect.
            assert fake_opener.calls == []
            return
        # Clean outcome: floor allocation + exactly one MR call, consistent.
        # onboard() returns an OnboardingResult; the Allocation is result.allocation.
        assert result.allocation.vlan_id == 100
        assert fake_opener.calls == [result.allocation]

    def test_tab_indented_yaml_registry_aborts_no_mr(
        self, tmp_path: Path, fake_opener: _FakeMergeRequestOpener
    ):
        """A registry using tab indentation is invalid YAML and must abort.

        YAML forbids tabs for indentation; this is a realistic hand-edit
        corruption distinct from 2.3's unbalanced-brace case. It must surface an
        OnboardingError and open no MR (FD.2).
        """
        reg = tmp_path / "projects.yaml"
        reg.write_text(
            "projects:\n"
            "\t- slug: tabbed\n"
            "\t  vlan_id: 100\n",
            encoding="utf-8",
        )

        with pytest.raises(OnboardingError):
            onboard("anyslug", reg, fake_opener, today=_FIXED_TODAY)
        assert fake_opener.calls == []

    def test_duplicate_slug_aborts_before_mr(
        self, tmp_path: Path, fake_opener: _FakeMergeRequestOpener
    ):
        """A slug already present aborts before the MR step, leaving the file
        byte-for-byte unchanged (FD.2 / Requirement 5.3).

        Degraded-mode angle (complementary to 2.3): we additionally assert the
        registry bytes are unchanged AND no MR fired — proving the abort happens
        with zero side effects, not just that an exception is raised.
        """
        reg = tmp_path / "projects.yaml"
        reg.write_text(
            "projects:\n"
            "  - slug: existing\n"
            "    vlan_id: 100\n"
            '    onboarding_date: "2024-01-01"\n',
            encoding="utf-8",
        )
        before = reg.read_text(encoding="utf-8")

        with pytest.raises(OnboardingError, match="already present"):
            onboard("existing", reg, fake_opener, today=_FIXED_TODAY)

        assert reg.read_text(encoding="utf-8") == before, (
            "duplicate-slug abort must not mutate the registry (FD.2)"
        )
        assert fake_opener.calls == [], "no MR may be opened on abort (FD.2)"

    def test_missing_registry_reports_clear_message_no_mr(
        self, tmp_path: Path, fake_opener: _FakeMergeRequestOpener
    ):
        """A missing registry reports a clear, actionable message and no MR.

        Complements 2.3's missing-file assertion by checking the *reported*
        message is human-actionable (FD.2 requires abort AND report), not just
        that an exception type was raised.
        """
        missing = tmp_path / "no-such-registry.yaml"

        with pytest.raises(OnboardingError) as excinfo:
            onboard("anyslug", missing, fake_opener, today=_FIXED_TODAY)

        message = str(excinfo.value).lower()
        assert "not found" in message or "registry" in message, (
            "the abort must REPORT the problem (FD.2), not fail opaquely; "
            f"got: {excinfo.value!r}"
        )
        assert fake_opener.calls == []


# =========================================================================== #
# 4. TRUNK PRECONDITION — FD.4  (inspection / documentation only)
# =========================================================================== #
class TestTrunkPreconditionIsDocumented:
    """FD.4 is validated by inspection/documentation, never an automated switch
    test.

    Terraform has no visibility into the physical switch trunk (NET-00 §8), so a
    correct SDN apply combined with a guest-connectivity failure is the expected,
    documented signature of an unconfigured trunk — attributable to the
    out-of-band trunk precondition, not to a Terraform error. These tests assert
    that this rationale is actually documented in the source of truth, so the
    assumption cannot silently disappear.
    """

    def test_net00_documents_out_of_band_trunk_precondition(self):
        """NET-00 §8 documents the manual, out-of-band physical-trunk decision
        that Terraform cannot see (FD.4)."""
        text = _read(_NET00_DOC)
        assert "trunk" in text.lower(), (
            "NET-00 must document the physical trunk precondition (FD.4, §8)"
        )
        # The essence of FD.4: Terraform has no visibility into the trunk, and
        # it is explicitly out of scope / out-of-band.
        low = text.lower()
        assert "out-of-band" in low or "out of scope" in low, (
            "NET-00 §8 must state the trunk step is out-of-band / out of scope "
            "(FD.4)"
        )
        assert re.search(r"terraform has no visibility", low), (
            "NET-00 §8 must state Terraform has no visibility into the trunk "
            "(the crux of why FD.4 is inspection-only)"
        )

    def test_foundation_sdn_tf_notes_out_of_band_trunk(self):
        """The foundation `sdn.tf` references the out-of-band trunk precondition
        (FD.4), so a reader of the IaC sees the bridge/trunk feeding the SDN
        zone is an out-of-band step Terraform does not manage — which is why a
        correct apply can still yield a guest-connectivity failure.

        This is anchored on the FOUNDATION root (not the project template),
        because that is where the zone attaches to the physical bridge/trunk
        (`sdn_zone_bridge`); the project VNet/subnet only tag onto that zone.
        """
        text = _read(_FOUNDATION_SDN_TF).lower()
        assert "trunk" in text, (
            "foundation sdn.tf should note the out-of-band physical trunk "
            "precondition (FD.4, NET-00 §8)"
        )
        assert "out-of-band" in text or "out of band" in text, (
            "the trunk note in foundation sdn.tf must mark it as an out-of-band "
            "precondition (FD.4)"
        )
        assert re.search(r"net-00\s*§?\s*8", text), (
            "the trunk note should cite NET-00 §8, the source of the FD.4 "
            "out-of-band rationale"
        )

    @pytest.mark.skip(
        reason=(
            "FD.4 rationale (inspection-only, NOT an automated test): the "
            "physical switch trunk is out-of-band and Terraform has no "
            "visibility into it (NET-00 §8). A CORRECT SDN apply plus a "
            "guest-connectivity failure is the expected signature of an "
            "unconfigured trunk — attributable to the documented out-of-band "
            "trunk precondition, not to a Terraform error. There is therefore "
            "deliberately no automated switch test; the assumption is validated "
            "by the documentation-presence assertions in this class."
        )
    )
    def test_trunk_connectivity_is_validated_by_inspection_only(self):
        # Deliberately never executed. This carries the FD.4 rationale in its
        # skip reason so the report shows *why* no automated switch test exists.
        raise AssertionError(
            "unreachable: FD.4 is documentation/inspection-only"
        )
