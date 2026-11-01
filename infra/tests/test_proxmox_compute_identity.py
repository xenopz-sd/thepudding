"""ADR-0021 — `proxmox-compute` first-class non-catalog guest identity.

Spec: consumer-last-mile-hello-workload, task 2.0 (ADR-0021). The shared
`infra/modules/proxmox-compute/` module gains a first-class, validated
NON-CATALOG role-code guest identity (`role_code`, e.g. `workload`) alongside
the catalog `svc_code`. A guest is identified by EXACTLY ONE of the two
(never both, never neither); the hostname is built from whichever is set, and
the computed-hostname well-formedness guard (preconditions.tf Guard 3) accepts
both the catalog `svcNN` and the non-catalog role-code forms.

Two test layers, mirroring the house style in ``test_terraform_shape.py``:

1. **Static HCL/source inspection (primary, always runs).** Parses the module
   `.tf` files as text and asserts the contract shape: both identity inputs
   exist, each is optional (default null), `svc_code` keeps its strict `svcNN`
   validation, `role_code` carries the non-`svc`/not-`svcNN` validation, the
   hostname is built from a single coalesced identity code, Guard 3's regex
   accepts both forms, and Guard 5 (exactly-one-of) is present. No toolchain
   needed.

2. **Plan-harness inspection (secondary, gated on the terraform/tofu binary).**
   Writes a throwaway harness root that calls the module with various identity
   inputs and runs `terraform plan`, asserting:
     * `role_code = "workload"`  -> accepted, hostname `workload-<vlan>-<nn>`;
     * `svc_code  = "svc01"`     -> accepted, hostname UNCHANGED (`svc01-...`);
     * both supplied            -> rejected at plan time (Guard 5);
     * neither supplied         -> rejected at plan time;
     * `role_code = "svc07"`    -> rejected (role_code validation: a `svcNN`
                                   belongs in svc_code).
   When no terraform/tofu binary is present the whole class is skipped (per the
   documentation-testing steering: absent toolchain => skipped, not failed).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_proxmox_compute_identity.py -v
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import requires_terraform, _terraform_binary

# --------------------------------------------------------------------------- #
# Locate the shared compute module relative to this test file (infra/tests/..).
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent
_COMPUTE_MODULE_DIR = _INFRA_DIR / "modules" / "proxmox-compute"


# --------------------------------------------------------------------------- #
# Minimal HCL helpers (local copies — kept independent of sibling test modules
# so this file carries no cross-test import beyond the shared conftest gate).
# --------------------------------------------------------------------------- #
def _strip_hcl_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = []
    for line in text.splitlines():
        out.append(line.split("#", 1)[0].split("//", 1)[0])
    return "\n".join(out)


def _extract_named_block(hcl: str, block_kw: str, label1: str) -> str | None:
    """Brace-balanced body of the first ``<block_kw> "<label1>" {`` block."""
    header = re.search(rf'{block_kw}\s+"{re.escape(label1)}"\s*\{{', hcl)
    if header is None:
        return None
    start = header.end() - 1
    depth = 0
    for i in range(start, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : i + 1]
    return None


# --------------------------------------------------------------------------- #
# 1. Static source inspection (primary — always runs).
# --------------------------------------------------------------------------- #
class TestIdentityContractStatic:
    """ADR-0021 identity contract shape, asserted against the module source."""

    @staticmethod
    @pytest.fixture(scope="class")
    def variables_hcl() -> str:
        return _strip_hcl_comments((_COMPUTE_MODULE_DIR / "variables.tf").read_text())

    @staticmethod
    @pytest.fixture(scope="class")
    def locals_hcl() -> str:
        return _strip_hcl_comments((_COMPUTE_MODULE_DIR / "locals.tf").read_text())

    @staticmethod
    @pytest.fixture(scope="class")
    def preconditions_hcl() -> str:
        return _strip_hcl_comments((_COMPUTE_MODULE_DIR / "preconditions.tf").read_text())

    def test_both_identity_inputs_declared(self, variables_hcl):
        names = re.findall(
            r'^\s*variable\s+"([A-Za-z0-9_]+)"', variables_hcl, re.MULTILINE
        )
        assert "svc_code" in names, "svc_code input must still be declared (ADR-0021)"
        assert "role_code" in names, (
            "role_code input must be declared — the first-class non-catalog "
            "guest identity (ADR-0021)"
        )

    def test_svc_code_optional_but_strict_svcNN_when_set(self, variables_hcl):
        block = _extract_named_block(variables_hcl, "variable", "svc_code")
        assert block is not None, "svc_code variable block not found"
        assert re.search(r"^\s*default\s*=\s*null", block, re.MULTILINE), (
            "svc_code must be OPTIONAL (default null) — a guest supplies exactly "
            "one of svc_code / role_code (ADR-0021)"
        )
        # Null is allowed through; a non-null value is the strict svcNN form.
        assert re.search(r"var\.svc_code\s*==\s*null\s*\|\|", block), (
            "svc_code validation must allow null (the role_code path) via a "
            "`var.svc_code == null || ...` form (ADR-0021)"
        )
        assert re.search(r'regex\(\s*"\^svc\[0-9\]\{2\}\$"', block), (
            "svc_code, when set, must stay the strict catalog slot '^svc[0-9]{2}$'"
        )

    def test_role_code_optional_non_svc_and_not_svcNN(self, variables_hcl):
        block = _extract_named_block(variables_hcl, "variable", "role_code")
        assert block is not None, "role_code variable block not found"
        assert re.search(r"^\s*default\s*=\s*null", block, re.MULTILINE), (
            "role_code must be OPTIONAL (default null) (ADR-0021)"
        )
        # Role-name pattern AND an explicit NOT-svcNN exclusion.
        assert re.search(r'regex\(\s*"\^\[a-z\]\[a-z0-9\]\*\$"', block), (
            "role_code must validate against a lowercase role pattern "
            "'^[a-z][a-z0-9]*$' (ADR-0021)"
        )
        assert re.search(r'!can\(\s*regex\(\s*"\^svc\[0-9\]\{2\}\$"', block), (
            "role_code must EXCLUDE a catalog 'svcNN' form (that belongs in "
            "svc_code) — expected a `!can(regex(\"^svc[0-9]{2}$\", ...))` clause "
            "(ADR-0021)"
        )

    def test_hostname_built_from_single_coalesced_identity(self, locals_hcl):
        # identity_code = coalesce(var.svc_code, var.role_code)
        assert re.search(
            r"identity_code\s*=\s*coalesce\(\s*var\.svc_code\s*,\s*var\.role_code\s*\)",
            locals_hcl,
        ), (
            "locals must compute a single identity_code = "
            "coalesce(var.svc_code, var.role_code) (ADR-0021)"
        )
        # Hostname is built from identity_code, not directly from svc_code.
        assert re.search(r"hostname\s*=.*local\.identity_code", locals_hcl), (
            "hostname must be built from local.identity_code (ADR-0021)"
        )
        assert not re.search(r"hostname\s*=.*var\.svc_code", locals_hcl), (
            "hostname must no longer reference var.svc_code directly — it uses "
            "the coalesced identity_code so the role_code path is honored"
        )

    def test_guard3_regex_accepts_both_identity_forms(self, preconditions_hcl):
        # The well-formedness regex leading segment must accept svcNN OR a role name.
        assert re.search(
            r"\^\(svc\[0-9\]\{2\}\|\[a-z\]\[a-z0-9\]\*\)", preconditions_hcl
        ), (
            "Guard 3's hostname well-formedness regex must accept BOTH the "
            "catalog 'svcNN' and the non-catalog role-code leading forms "
            "(ADR-0021)"
        )

    def test_guard5_exactly_one_of_present(self, preconditions_hcl):
        # The XOR precondition: (svc_code == null) != (role_code == null).
        assert re.search(
            r"\(\s*var\.svc_code\s*==\s*null\s*\)\s*!=\s*\(\s*var\.role_code\s*==\s*null\s*\)",
            preconditions_hcl,
        ), (
            "Guard 5 must enforce EXACTLY ONE of svc_code / role_code via "
            "`(var.svc_code == null) != (var.role_code == null)` (ADR-0021)"
        )


# --------------------------------------------------------------------------- #
# 2. Plan-harness inspection (secondary — gated on the toolchain).
# --------------------------------------------------------------------------- #
@requires_terraform
class TestIdentityPlanBehavior:
    """Plan-time accept/reject behavior via a throwaway module harness root.

    Each case writes a tiny root that calls the committed proxmox-compute module
    with a given identity input set, runs an offline `terraform init
    -backend=false` + `terraform plan`, and inspects the exit code / output.
    The module declares only computed locals + a `terraform_data` guard anchor
    (no real provider resource), so a plan needs no Proxmox credentials and no
    backend — it either succeeds (valid identity) or fails on a
    validation/precondition (invalid identity).
    """

    # Common harness inputs shared by every case (identity lines injected).
    _BASE = (
        '  project_slug = "dronefleet"\n'
        '  service_name = "workload"\n'
        "  vlan_id      = 100\n"
        "  host_index   = 1\n"
        '  instance     = "01"\n'
    )

    @staticmethod
    def _run_harness(identity_lines: str) -> tuple[int, str, str]:
        """Return (plan_returncode, plan_output, planned_hostname|"").

        A non-zero returncode means the identity was rejected (variable
        validation or a plan-time precondition). On success the planned
        `hostname` output value is extracted from the plan text.
        """
        binary = _terraform_binary()
        assert binary is not None  # gated by @requires_terraform
        tmp = Path(tempfile.mkdtemp(prefix="pc_identity_"))
        try:
            (tmp / "main.tf").write_text(
                'module "t" {\n'
                f'  source = "{_COMPUTE_MODULE_DIR}"\n'
                f"{TestIdentityPlanBehavior._BASE}"
                f"{identity_lines}"
                "}\n"
                'output "hostname" {\n'
                "  value = module.t.hostname\n"
                "}\n",
                encoding="utf-8",
            )
            init = subprocess.run(
                [binary, "init", "-backend=false", "-input=false", "-no-color"],
                cwd=tmp,
                capture_output=True,
                text=True,
            )
            if init.returncode != 0:
                # An init that cannot fetch the provider offline is an
                # environmental prerequisite, not a contract failure — skip.
                combined = (init.stdout + init.stderr).lower()
                if any(
                    m in combined
                    for m in (
                        "failed to install provider",
                        "could not retrieve",
                        "failed to query available provider",
                        "network is unreachable",
                        "dial tcp",
                        "timeout",
                    )
                ):
                    pytest.skip(
                        "requires-infra, skipped: terraform init could not "
                        "install the provider offline (environmental, unrelated "
                        "to the ADR-0021 contract under test)."
                    )
                return init.returncode, init.stdout + init.stderr, ""
            plan = subprocess.run(
                [binary, "plan", "-input=false", "-no-color"],
                cwd=tmp,
                capture_output=True,
                text=True,
            )
            out = plan.stdout + plan.stderr
            m = re.search(r'hostname\s*=\s*"([^"]+)"', out)
            return plan.returncode, out, (m.group(1) if m else "")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_role_code_workload_accepted_with_role_hostname(self):
        """role_code='workload' is accepted; hostname is `workload-<vlan>-<nn>`."""
        rc, out, hostname = self._run_harness('  role_code = "workload"\n')
        assert rc == 0, (
            f"role_code='workload' must be accepted at plan time, got rc={rc}:\n{out}"
        )
        assert hostname == "workload-100-01", (
            f"role_code hostname must be 'workload-100-01', got {hostname!r}"
        )

    def test_svc_code_still_accepted_hostname_unchanged(self):
        """svc_code='svc01' is still accepted and the hostname is UNCHANGED from
        before the ADR-0021 contract change (`svc01-<vlan>-<nn>`)."""
        rc, out, hostname = self._run_harness('  svc_code = "svc01"\n')
        assert rc == 0, (
            f"svc_code='svc01' must still be accepted at plan time, got rc={rc}:\n{out}"
        )
        assert hostname == "svc01-100-01", (
            "catalog svc_code hostname must be byte-for-byte unchanged "
            f"('svc01-100-01'), got {hostname!r}"
        )

    def test_both_supplied_rejected(self):
        """Supplying BOTH identities is rejected at plan time (Guard 5)."""
        rc, out, _ = self._run_harness(
            '  svc_code  = "svc01"\n  role_code = "workload"\n'
        )
        assert rc != 0, (
            f"supplying both svc_code and role_code must be rejected:\n{out}"
        )
        assert "exactly-one-of" in out.lower() or "exactly one" in out.lower(), (
            f"the rejection should name the exactly-one-of identity rule:\n{out}"
        )

    def test_neither_supplied_rejected(self):
        """Supplying NEITHER identity is rejected at plan time."""
        rc, out, _ = self._run_harness("")  # no identity line at all
        assert rc != 0, (
            f"supplying neither svc_code nor role_code must be rejected:\n{out}"
        )

    def test_role_code_matching_svcNN_rejected(self):
        """A role_code that is itself a catalog 'svcNN' form is rejected — a
        catalog slot belongs in svc_code, not role_code (ADR-0021)."""
        rc, out, _ = self._run_harness('  role_code = "svc07"\n')
        assert rc != 0, (
            f"role_code='svc07' (a catalog svcNN form) must be rejected:\n{out}"
        )
        assert "role_code" in out.lower(), (
            f"the rejection should reference the role_code validation:\n{out}"
        )
