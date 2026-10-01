"""Degraded-mode + precondition tests for the `SDN.Use` privilege.

Task 9.1 (spec: proxmox-network-foundation) — Requirements 7.1, 7.2, 7.3;
design.md "Error Handling" (SDN.Use row) and "Testing Strategy §4 Degraded-mode
tests / Missing SDN.Use".

This file has TWO portions:

1. **Static assertions (primary — always run, and PASS in this environment).**
   The `SDN.Allocate` privilege is a *precondition* owned by the Platform
   Foundation layer, referenced here only as a documented dependency. Corrected
   per ADR-0001: `SDN.Allocate` — not `SDN.Use` — is the privilege Proxmox
   checks on SDN object create/modify/delete. Requirement 7.3 forbids this
   feature from declaring, re-granting, or modifying the `terraform@pve` role or
   that privilege. These tests therefore assert, by source inspection, that:

     * NO Terraform resource in the infra tree grants/declares a Proxmox role
       or ACL (`proxmox_*_role` / `proxmox_*_acl`) — i.e. the privilege is never
       created here, only assumed (Requirement 7.3);
     * `SDN.Allocate` appears ONLY inside comments (as a documented
       precondition), never in an executable HCL statement (Requirement 7.1,
       7.3);
     * the precondition IS in fact documented next to the SDN resources
       (Requirement 7.1) — a missing doc note would be a silent precondition.

2. **Live degraded-mode apply (secondary — infra-gated, SKIPPED here).**
   Applies a Terraform root against a Proxmox *test identity that lacks*
   `SDN.Allocate` and asserts the authorization failure surfaces and NO SDN
   resource is reported provisioned (Requirement 7.2, design.md "Missing
   SDN.Use"). Corrected per ADR-0001: withholding `SDN.Allocate` (the
   create/manage privilege), NOT `SDN.Use` (the later guest-NIC-attach
   privilege), is what causes Proxmox to DENY SDN object creation.

   CRITICAL SEQUENCING (ADR-0001): the project-template root's VNet/subnet
   reference a zone that must already exist, so to get a clean AUTHORIZATION
   failure (403) rather than an HTTP 500 "undefined zone" sequencing error, the
   test FIRST provisions the foundation SDN zone with a SUFFICIENTLY-privileged
   token (the `PROXMOX_SDN_TEST_*` credentials that DO have `SDN.Allocate`), THEN
   attempts the project apply with the under-privileged (no-`SDN.Allocate`)
   token, THEN tears the foundation zone down in a `finally` block.

   The gate therefore requires BOTH credential sets — the privileged SDN token
   (`PROXMOX_SDN_TEST_ENDPOINT` / `PROXMOX_SDN_TEST_TOKEN`) to create the
   prerequisite zone, AND the no-`SDN.Allocate` token
   (`PROXMOX_TEST_ENDPOINT` / `PROXMOX_TEST_TOKEN_NO_SDN_ALLOCATE`) for the
   negative apply — plus a `terraform`/`tofu` binary; when any is absent the
   test is SKIPPED (per documentation-testing steering: absent infra => skipped,
   not failed). It is intended to run in CI against purpose-built, scoped test
   identities — see validation.md (task 11.3) for the gating record.

   Both live apply/destroy runs are driven through the shared
   ``infra/tests/conftest.py::live_apply`` helper (spec:
   fix-live-apply-backend-init), which writes a transient
   ``backend "local" { path = <disposable temp state file> }`` override into a
   throwaway SANDBOX COPY of the roots and runs ``terraform init -reconfigure``
   before the apply — so the apply actually REACHES Proxmox and surfaces the
   under-privileged identity's authorization error, instead of aborting at
   backend init the way the previous ``init -backend=false`` + deprecated
   ``-state=<path>`` sequence did. The committed roots are never written to, and
   both tokens are threaded ONLY into the subprocess env by the helper (never
   logged, never on a command line).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_degraded_sdn_use.py -v
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Locate the infra roots relative to this test file (infra/tests/...).
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent
_FOUNDATION_DIR = _INFRA_DIR / "platform-foundation"
_PROJECT_TEMPLATE_DIR = _INFRA_DIR / "projects" / "_TEMPLATE"

_FOUNDATION_SDN_TF = _FOUNDATION_DIR / "sdn.tf"
_PROJECT_SDN_TF = _PROJECT_TEMPLATE_DIR / "sdn.tf"

# The privilege whose grant is a PF-owned precondition (never granted here).
# Corrected per ADR-0001: SDN.Allocate gates SDN create/modify/delete — it is the
# create/manage precondition the sdn.tf comment blocks now document. (SDN.Use is
# the separate, later guest-NIC-attach privilege; the deferred behavioral /
# env-read parts of this file that still reference it are owned by
# fix-live-apply-backend-init.)
_SDN_ALLOCATE = "SDN.Allocate"

# bpg/proxmox resource types that could grant a role/privilege or bind an ACL.
# If any of these ever appears in the infra tree, this feature would be
# re-granting the privilege and violating Requirement 7.3.
_FORBIDDEN_GRANT_RESOURCE_RE = re.compile(
    r'resource\s+"(proxmox_[A-Za-z0-9_]*(?:role|acl)[A-Za-z0-9_]*)"',
)


# --------------------------------------------------------------------------- #
# Comment-stripping helpers (shared style with test_terraform_shape.py).
# --------------------------------------------------------------------------- #
def _strip_hcl_comments(text: str) -> str:
    """Remove `#`/`//` line comments and `/* ... */` block comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out_lines = []
    for line in text.splitlines():
        no_hash = line.split("#", 1)[0]
        no_slash = no_hash.split("//", 1)[0]
        out_lines.append(no_slash)
    return "\n".join(out_lines)


def _all_tf_files() -> list[Path]:
    files = sorted(_INFRA_DIR.rglob("*.tf"))
    assert files, f"expected at least one .tf file under {_INFRA_DIR}"
    return files


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Static assertions — ALWAYS RUN (Requirement 7.1, 7.3)
# --------------------------------------------------------------------------- #
class TestSdnUseIsPreconditionNotResource:
    """SDN.Use is only ever a documented precondition, never granted (Req 7.3)."""

    def test_no_proxmox_role_or_acl_resource_anywhere(self):
        """No `proxmox_*_role` / `proxmox_*_acl` resource exists in the infra tree.

        Requirement 7.3: this feature must NOT declare, re-grant, or modify the
        `terraform@pve` role or its `SDN.Use` privilege. The mechanism for
        granting a Proxmox privilege via bpg/proxmox is a role/ACL resource; the
        absence of any such resource is the machine-checkable form of "we only
        assume the grant, we never create it."
        """
        offenders: list[str] = []
        for tf in _all_tf_files():
            hcl = _strip_hcl_comments(_read(tf))
            for m in _FORBIDDEN_GRANT_RESOURCE_RE.finditer(hcl):
                offenders.append(f"{tf.relative_to(_INFRA_DIR)} -> resource {m.group(1)!r}")
        assert not offenders, (
            "Requirement 7.3 forbids declaring/granting the terraform@pve role "
            "or the SDN.Use privilege here; found role/ACL grant resource(s): "
            + "; ".join(offenders)
        )

    def test_sdn_allocate_never_appears_in_executable_hcl(self):
        """`SDN.Allocate` may appear ONLY in comments, never in executable HCL.

        After stripping comments, the literal string `SDN.Allocate` must not
        survive anywhere in the infra `.tf` files — if it did, it would mean the
        privilege is referenced in an actual attribute/argument/resource rather
        than documented as a precondition (Requirement 7.1, 7.3). This preserves
        the Static_Comment_Test invariant against the corrected privilege string
        (ADR-0001).
        """
        offenders: list[str] = []
        for tf in _all_tf_files():
            code_only = _strip_hcl_comments(_read(tf))
            if _SDN_ALLOCATE in code_only:
                offenders.append(str(tf.relative_to(_INFRA_DIR)))
        assert not offenders, (
            f"'{_SDN_ALLOCATE}' must appear only in comments (as a documented "
            "precondition), never in executable HCL; found it in code of: "
            + ", ".join(offenders)
        )

    def test_sdn_allocate_precondition_is_documented_in_foundation_sdn(self):
        """The `SDN.Allocate` precondition is documented next to the foundation
        SDN resources (Requirement 7.1)."""
        text = _read(_FOUNDATION_SDN_TF)
        assert _SDN_ALLOCATE in text, (
            f"foundation sdn.tf must document the '{_SDN_ALLOCATE}' precondition "
            "next to the SDN zone/applier resources (Requirement 7.1)"
        )
        # It must be documented as a precondition owned elsewhere, not re-granted.
        assert re.search(r"precondition", text, re.IGNORECASE), (
            "foundation sdn.tf must describe SDN.Use as a PRECONDITION (Req 7.1)"
        )
        assert re.search(r"7\.3", text), (
            "foundation sdn.tf should cite Requirement 7.3 (not re-granted here)"
        )

    def test_sdn_allocate_precondition_is_documented_in_project_sdn(self):
        """The `SDN.Allocate` precondition is documented next to the project SDN
        resources (Requirement 7.1)."""
        text = _read(_PROJECT_SDN_TF)
        assert _SDN_ALLOCATE in text, (
            f"project template sdn.tf must document the '{_SDN_ALLOCATE}' precondition "
            "next to the VNet/subnet/applier resources (Requirement 7.1)"
        )
        assert re.search(r"precondition", text, re.IGNORECASE), (
            "project sdn.tf must describe SDN.Use as a PRECONDITION (Req 7.1)"
        )
        assert re.search(r"7\.3", text), (
            "project sdn.tf should cite Requirement 7.3 (not re-granted here)"
        )


# --------------------------------------------------------------------------- #
# 2. Live degraded-mode apply — INFRA-GATED (Requirement 7.2), SKIPPED here.
# --------------------------------------------------------------------------- #
def _terraform_binary() -> str | None:
    return shutil.which("terraform") or shutil.which("tofu")


# A scoped Proxmox test identity that intentionally LACKS the SDN.Allocate
# privilege — the create/manage privilege Proxmox actually checks on SDN object
# creation (ADR-0001). Withholding SDN.Allocate (NOT SDN.Use, which governs the
# later guest-NIC-attach step) is what now causes Proxmox to DENY SDN object
# creation with a 403. CI must set the no-SDN.Allocate token (below) for the
# negative apply.
_NO_SDN_ALLOCATE_ENDPOINT_ENV = "PROXMOX_TEST_ENDPOINT"
_NO_SDN_ALLOCATE_TOKEN_ENV = "PROXMOX_TEST_TOKEN_NO_SDN_ALLOCATE"

# A SUFFICIENTLY-privileged Proxmox SDN test identity (token WITH SDN.Allocate),
# reusing the SAME env var names as the apply-coexistence / destroy-isolation
# tests. Required here to FIRST provision the foundation SDN zone so the project
# apply's VNet/subnet reference an existing zone — otherwise the under-privileged
# apply would fail with an HTTP 500 "undefined zone" (a sequencing error) rather
# than the intended HTTP 403 authorization denial (ADR-0001).
_SDN_TEST_ENDPOINT_ENV = "PROXMOX_SDN_TEST_ENDPOINT"
_SDN_TEST_TOKEN_ENV = "PROXMOX_SDN_TEST_TOKEN"


def _live_degraded_gate_reason() -> str | None:
    """Return a skip reason if the live degraded-mode gate is not satisfied,
    else None.

    The negative test now needs BOTH credential sets: the SUFFICIENTLY-privileged
    SDN token (to create the prerequisite foundation zone) AND the
    under-privileged no-SDN.Allocate token (for the negative project apply), plus
    a terraform/tofu binary. If ANY is absent the test SKIPS cleanly (never
    fails), per the documentation-testing steering rule "absent infra =>
    skipped, not failed".
    """
    if _terraform_binary() is None:
        return (
            "requires-infra, skipped: no terraform/tofu binary available to "
            "drive a live apply against a no-SDN.Allocate Proxmox test identity"
        )
    if not os.environ.get(_SDN_TEST_ENDPOINT_ENV):
        return (
            f"requires-infra, skipped: {_SDN_TEST_ENDPOINT_ENV} not set "
            "(a privileged Proxmox SDN test-cluster endpoint is required to "
            "provision the prerequisite foundation zone)"
        )
    if not os.environ.get(_SDN_TEST_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_SDN_TEST_TOKEN_ENV} not set "
            "(a token WITH SDN.Allocate is required to provision the "
            "prerequisite foundation zone)"
        )
    if not os.environ.get(_NO_SDN_ALLOCATE_ENDPOINT_ENV):
        return (
            f"requires-infra, skipped: {_NO_SDN_ALLOCATE_ENDPOINT_ENV} not set "
            "(a Proxmox test-cluster endpoint is required)"
        )
    if not os.environ.get(_NO_SDN_ALLOCATE_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_NO_SDN_ALLOCATE_TOKEN_ENV} not set "
            "(a scoped API token for an identity LACKING SDN.Allocate is "
            "required)"
        )
    return None


@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_degraded_gate_reason() is not None,
    reason=_live_degraded_gate_reason() or "live degraded-mode gate satisfied",
)
class TestMissingSdnUseAuthorizationFailure:
    """Live degraded-mode: apply with an identity lacking SDN.Allocate (Req 7.2).

    Corrected per ADR-0001: ``SDN.Allocate`` — not ``SDN.Use`` — is the privilege
    Proxmox checks on SDN object CREATE/MODIFY/DELETE. This test therefore
    reproduces the authorization denial by withholding ``SDN.Allocate``.

    This class runs ONLY in CI (or locally) when a terraform/tofu binary AND
    BOTH Proxmox test identities are present:

      * a SUFFICIENTLY-privileged SDN token (``PROXMOX_SDN_TEST_*``, WITH
        ``SDN.Allocate``) used to FIRST provision the foundation SDN zone, and
      * a scoped ``no-SDN.Allocate`` token (``PROXMOX_TEST_TOKEN_NO_SDN_ALLOCATE``)
        used for the negative project apply.

    CRITICAL SEQUENCING (ADR-0001): the project-template root's VNet/subnet
    reference a zone that must already exist. To get a clean AUTHORIZATION
    failure (403) rather than an HTTP 500 "undefined zone" sequencing error, the
    test FIRST provisions the foundation zone with the privileged token, THEN
    attempts the project apply with the under-privileged (no-``SDN.Allocate``)
    token, THEN tears the foundation zone down in a ``finally`` block. Both runs
    are driven through the shared ``conftest.py::live_apply`` helper — which uses
    a transient ``backend "local"`` override + ``init -reconfigure`` so each
    apply/destroy reaches Proxmox instead of aborting at backend init.

    The test asserts, for the negative project apply:

      * it FAILS (non-zero exit) with a Proxmox authorization error, and
      * NO SDN resource is reported as provisioned (Requirement 7.2).

    The scoped no-``SDN.Allocate`` identity MUST have every other privilege the
    apply needs (VM/LXC/storage/network, plus ``SDN.Use``/``SDN.Audit`` if any)
    but MUST be missing exactly ``SDN.Allocate``, so the only reason for the
    failure is the deliberately-withheld create/manage privilege. It must NEVER
    be an over-privileged or production identity. Both tokens are threaded ONLY
    into the subprocess env by ``live_apply`` (never logged, never on a command
    line, never in an assertion message).

    See validation.md (task 11.3) for the record that this evidence is
    infra-gated and where it is exercised in CI.
    """

    def test_apply_surfaces_authorization_failure_and_provisions_nothing(self):
        import json
        import tempfile

        from conftest import live_apply

        # Privileged SDN identity — provisions the prerequisite foundation zone.
        sdn_endpoint = os.environ[_SDN_TEST_ENDPOINT_ENV]
        sdn_token = os.environ[_SDN_TEST_TOKEN_ENV]

        # Under-privileged identity — LACKS SDN.Allocate; used for the negative
        # project apply that must be DENIED.
        endpoint = os.environ[_NO_SDN_ALLOCATE_ENDPOINT_ENV]
        token = os.environ[_NO_SDN_ALLOCATE_TOKEN_ENV]

        # This template resolves vlan_id from projects.yaml for the slug; a
        # registered fixture slug must exist (CI supplies it, default dronefleet).
        project_slug = os.environ.get("PROXMOX_TEST_PROJECT_SLUG", "dronefleet")

        with tempfile.TemporaryDirectory() as tmp:
            foundation_state = Path(tmp) / "foundation.tfstate"
            state_path = Path(tmp) / "terraform.tfstate"

            try:
                # --- Prerequisite: provision the foundation SDN zone with a
                # SUFFICIENTLY-privileged token, so the project apply's
                # VNet/subnet reference an EXISTING zone. Without this, the
                # under-privileged apply fails with HTTP 500 "undefined zone"
                # (a sequencing error) instead of the intended 403 (ADR-0001).
                with live_apply(
                    "foundation",
                    foundation_state,
                    endpoint=sdn_endpoint,
                    token=sdn_token,
                    insecure=True,
                ) as foundation:
                    found_apply = foundation.apply()
                assert found_apply.returncode == 0, (
                    "prerequisite foundation zone apply (privileged token) must "
                    "succeed so the negative apply fails on AUTHORIZATION, not "
                    "on a missing zone (ADR-0001).\n"
                    f"output:\n{found_apply.output}"
                )

                # --- Negative apply: attempt the project SDN root with the
                # under-privileged (no-SDN.Allocate) token. live_apply writes a
                # transient `backend "local" { path = <state_path> }` override
                # into a SANDBOX COPY, runs `init -reconfigure`, then applies —
                # so the apply actually REACHES Proxmox instead of aborting at
                # backend init. The token is threaded ONLY into the subprocess
                # env by the helper (never logged / on a command line).
                with live_apply(
                    "project_template",
                    state_path,
                    endpoint=endpoint,
                    token=token,
                    insecure=True,
                ) as sandbox:
                    apply = sandbox.apply("-var", f"project_slug={project_slug}")

                # (1) The apply MUST fail — a missing privilege is never a success.
                assert apply.returncode != 0, (
                    "apply against an identity lacking SDN.Allocate must FAIL "
                    "(Requirement 7.2), but it exited 0.\n"
                    f"output:\n{apply.output}"
                )

                # (2) The failure must be an authorization/permission error
                # naming the SDN privilege — not an unrelated failure. This now
                # works because the apply reaches Proxmox (against an existing
                # zone) rather than dying at backend init or on a missing zone.
                # The operative marker under the corrected model is
                # `sdn.allocate` (ADR-0001); `sdn.use` is retained for tolerance.
                combined = apply.output.lower()
                assert any(
                    marker in combined
                    for marker in ("permission", "not allowed", "403",
                                   "authorization", "privilege",
                                   "sdn.allocate", "sdn.use")
                ), (
                    "the surfaced failure must be a Proxmox authorization error "
                    f"(Requirement 7.2). Got:\noutput:\n{apply.output}"
                )

                # (3) NO SDN resource may be reported provisioned: the state file
                # (if written at all) must contain zero SDN resource instances.
                provisioned_sdn = 0
                if state_path.is_file():
                    try:
                        state = json.loads(state_path.read_text())
                    except json.JSONDecodeError:
                        state = {}
                    for res in state.get("resources", []):
                        if str(res.get("type", "")).startswith("proxmox_sdn_"):
                            provisioned_sdn += len(res.get("instances", []))
                assert provisioned_sdn == 0, (
                    "no SDN resource may be reported provisioned when "
                    "SDN.Allocate is missing (Requirement 7.2); found "
                    f"{provisioned_sdn} in state"
                )
            finally:
                # Tear down the prerequisite foundation zone (privileged token),
                # leaving the ephemeral cluster as it was found.
                with live_apply(
                    "foundation",
                    foundation_state,
                    endpoint=sdn_endpoint,
                    token=sdn_token,
                    insecure=True,
                ) as foundation:
                    foundation.destroy()
