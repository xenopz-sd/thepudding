"""State-name uniqueness + `SDN.Allocate` degraded-mode tests for the SVC-07 root.

Task 3.2 (spec: svc-07-secrets-manager) — requirements.md Requirement 9.6 (state
name unique across all roots) and 9.8 (`SDN.Allocate` precondition: an apply by a
Terraform identity lacking `SDN.Allocate` on `/sdn` must fail with an
authorization error and provision NO container). design.md "Testing Strategy §3"
and the "Requirement → evidence map" row 9.x ("... + state-name uniqueness +
`SDN.Allocate` degraded + destroy-isolation").

This file is the SVC-07 analogue of the sibling proxmox-network-foundation suite,
and it mirrors that suite's TWO-file split exactly rather than inventing a new
shape:

  * the OFFLINE state-name-uniqueness assertions follow
    ``test_state_and_registry.py`` (recover the documented state name from each
    root's committed ``-backend-config`` address example, then assert
    uniqueness across roots), and
  * the live ``SDN.Allocate`` degraded-mode apply follows
    ``test_degraded_sdn_use.py`` — a ``@pytest.mark.requires_infra`` class,
    ``skipif``-gated on the SAME Proxmox test-identity env vars, driven through
    the shared ``conftest.py::live_apply`` helper, and SKIPPED cleanly (never
    failed) when the binary or credentials are absent (documentation-testing
    steering: absent infra => skipped, not failed).

No new gate is invented: the offline part needs no binary, and the live part
reuses the established ``requires_infra`` marker (deselected by default via the
root ``pytest.ini``) plus the same ``PROXMOX_SDN_TEST_*`` /
``PROXMOX_TEST_TOKEN_NO_SDN_ALLOCATE`` env-var contract the sibling degraded test
already documents.

The resource-SHAPE assertions (Req 9.1–9.5) live in the companion
``test_svc07_terraform_shape.py`` (task 3.1); the destroy-isolation apply (Req
9.7) is a ``requires_infra`` integration concern covered elsewhere in the plan
(task 12.x) — this file covers exactly 9.6 (offline) and 9.8 (requires_infra).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_state_and_degraded.py -v
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Repo-relative paths. This file lives at <repo>/infra/tests/... so the infra
# dir is one parent up and the repo root is two parents up.
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent

_SVC07_STATE_NAME = "svc-07-secrets-manager"
_FOUNDATION_STATE_NAME = "platform-foundation"

# Terraform's GitLab-managed HTTP backend addresses end in
# ``/terraform/state/<state-name>`` (optionally ``/lock``). We recover the
# documented state name(s) from each root's committed ``-backend-config``
# address example — the SAME idiom as the sibling ``test_state_and_registry.py``
# (both roots use a *partial* ``backend "http" {}`` block whose concrete state
# name is supplied at ``terraform init`` time, so the name lives in the
# documented address example inside ``versions.tf``, not in a literal HCL
# attribute).
_STATE_ADDR_RE = re.compile(
    r"/terraform/state/(?P<name>[A-Za-z0-9._<>-]+?)(?:/lock)?[\"'\s]"
)
_PARTIAL_HTTP_BACKEND_RE = re.compile(r'backend\s+"http"\s*\{')


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


def _documented_state_names(versions_tf_text: str) -> set[str]:
    """State names recovered from the documented ``-backend-config`` examples."""
    return {m.group("name") for m in _STATE_ADDR_RE.finditer(versions_tf_text)}


def _all_root_versions_files() -> list[Path]:
    """Every Terraform root's ``versions.tf`` under ``infra/`` (the files that
    carry a ``backend "http"`` state-name declaration).

    A Terraform *root* is any directory that declares a backend; in this repo
    that is exactly the dirs whose ``versions.tf`` contains a ``backend "http"``
    block (the shared ``proxmox-compute`` MODULE has no backend). We discover
    them by scanning, rather than hardcoding, so a newly-added service root that
    reused ``svc-07-secrets-manager`` would be caught.
    """
    roots: list[Path] = []
    for versions_tf in sorted(_INFRA_DIR.rglob("versions.tf")):
        if _PARTIAL_HTTP_BACKEND_RE.search(_read(versions_tf)):
            roots.append(versions_tf)
    assert roots, f"expected at least one root versions.tf under {_INFRA_DIR}"
    return roots


# --------------------------------------------------------------------------- #
# 1. State-name uniqueness — OFFLINE, ALWAYS RUNS (Requirement 9.6)
# --------------------------------------------------------------------------- #
class TestSvc07StateNameUniqueness:
    """The SVC-07 root's state name is `svc-07-secrets-manager` and no other
    root reuses it (Requirement 9.6)."""

    _SVC07_VERSIONS_TF = _INFRA_DIR / "projects" / "svc-07-secrets-manager" / "versions.tf"

    def test_svc07_root_declares_partial_http_backend(self):
        """The SVC-07 root declares a (partial) `backend "http"` block (Req 9.6)."""
        text = _read(self._SVC07_VERSIONS_TF)
        assert _PARTIAL_HTTP_BACKEND_RE.search(text), (
            "SVC-07 versions.tf must declare a backend \"http\" block "
            "(GitLab-managed HTTP backend, PF FR-2 / Req 9.6)"
        )

    def test_svc07_documents_its_own_state_name(self):
        """The SVC-07 root documents state name `svc-07-secrets-manager` (Req 9.6)."""
        names = _documented_state_names(_read(self._SVC07_VERSIONS_TF))
        assert _SVC07_STATE_NAME in names, (
            "SVC-07 versions.tf must document the "
            f"'{_SVC07_STATE_NAME}' state name in its -backend-config example "
            f"(Req 9.6); found {sorted(names)}"
        )
        # And it must NOT accidentally document the foundation state name or a
        # `<slug>-infra` per-project pattern — this root is a service root.
        assert _FOUNDATION_STATE_NAME not in names, (
            "SVC-07 root must not document the foundation state name "
            f"'{_FOUNDATION_STATE_NAME}'; found {sorted(names)}"
        )
        infra_named = {n for n in names if n.endswith("-infra")}
        assert not infra_named, (
            "SVC-07 root must not document a '<slug>-infra' per-project state "
            f"name; found {sorted(infra_named)}"
        )

    def test_svc07_state_name_appears_in_exactly_one_root(self):
        """`svc-07-secrets-manager` is documented by the SVC-07 root ONLY (Req 9.6).

        Scan every Terraform root's backend config across infra/ and assert the
        SVC-07 state name appears in exactly one root — the SVC-07 root — and no
        other root (foundation, per-project template, or any future service
        root) reuses it.
        """
        roots_declaring_svc07: list[str] = []
        for versions_tf in _all_root_versions_files():
            if _SVC07_STATE_NAME in _documented_state_names(_read(versions_tf)):
                roots_declaring_svc07.append(str(versions_tf.relative_to(_INFRA_DIR)))

        assert roots_declaring_svc07 == [
            str(self._SVC07_VERSIONS_TF.relative_to(_INFRA_DIR))
        ], (
            "state name 'svc-07-secrets-manager' must be documented by EXACTLY "
            "the SVC-07 root and no other root (Req 9.6); found it in: "
            f"{roots_declaring_svc07}"
        )

    def test_svc07_state_name_distinct_from_every_other_root(self):
        """No other root's documented state-name set contains the SVC-07 name,
        and the SVC-07 root does not piggy-back another root's name (Req 9.6)."""
        svc07_names = _documented_state_names(_read(self._SVC07_VERSIONS_TF))
        assert _SVC07_STATE_NAME in svc07_names

        for versions_tf in _all_root_versions_files():
            if versions_tf == self._SVC07_VERSIONS_TF:
                continue
            other_names = _documented_state_names(_read(versions_tf))
            overlap = svc07_names & other_names
            assert not overlap, (
                "SVC-07 state-name set must be disjoint from every other root's "
                f"({versions_tf.relative_to(_INFRA_DIR)}); overlap={sorted(overlap)} "
                "(Req 9.6)"
            )


# --------------------------------------------------------------------------- #
# 2. Live `SDN.Allocate` degraded-mode apply — INFRA-GATED (Requirement 9.8).
#
# Mirrors the sibling ``test_degraded_sdn_use.py`` exactly: withholding
# ``SDN.Allocate`` (the create/manage privilege Proxmox checks on SDN object /
# NIC-attach operations, per ADR-0001) — NOT ``SDN.Use`` — is what causes Proxmox
# to DENY the apply. The SVC-07 root attaches both LXC NICs to the VLAN-20 SDN
# VNet, so the apply exercises that privilege.
#
# SEQUENCING (ADR-0001): the SVC-07 LXCs' NICs reference the VLAN-20 SDN VNet
# (`bridge = "p20"`), which is a Platform-Foundation-owned object that must
# already exist. To get a clean AUTHORIZATION failure rather than an
# "undefined bridge/zone" sequencing error, FIRST provision the foundation SDN
# zone/VNet with a SUFFICIENTLY-privileged token (`PROXMOX_SDN_TEST_*`, WITH
# `SDN.Allocate`), THEN attempt the SVC-07 apply with the under-privileged
# (no-`SDN.Allocate`) token, THEN tear the foundation zone down in `finally`.
#
# The SVC-07 root is NOT one of the two roots the shared ``live_apply`` helper
# knows (`foundation` / `project_template`), so a small local ``live_apply``
# variant is used for the SVC-07 apply — reusing the helper's exact sandbox +
# transient-``backend "local"`` + ``init -reconfigure`` mechanism and its Req-3.6
# credential-in-env-only guarantee, without widening the shared helper's root
# map. The prerequisite foundation zone is still provisioned via the shared
# ``live_apply("foundation", ...)`` (it DOES know that root).
# --------------------------------------------------------------------------- #

# Under-privileged Proxmox test identity that intentionally LACKS SDN.Allocate —
# same env-var contract as the sibling degraded test.
_NO_SDN_ALLOCATE_ENDPOINT_ENV = "PROXMOX_TEST_ENDPOINT"
_NO_SDN_ALLOCATE_TOKEN_ENV = "PROXMOX_TEST_TOKEN_NO_SDN_ALLOCATE"

# Sufficiently-privileged SDN test identity (token WITH SDN.Allocate) used to
# FIRST provision the prerequisite foundation VLAN-20 SDN zone/VNet — same env
# vars as the sibling degraded / coexistence / destroy-isolation tests.
_SDN_TEST_ENDPOINT_ENV = "PROXMOX_SDN_TEST_ENDPOINT"
_SDN_TEST_TOKEN_ENV = "PROXMOX_SDN_TEST_TOKEN"

# Dummy guest inputs so the SVC-07 apply can proceed to the NIC-attach step
# where SDN.Allocate is checked. These are non-secret placeholders (the VMIDs /
# node / template are provisioning inputs, not credentials); the Proxmox
# endpoint + token are threaded ONLY into the subprocess env by the helper.
_SVC07_APPLY_VARS = (
    "-var", "proxmox_node_name=pve-test",
    "-var", "openbao_template_file_id=local:vztmpl/debian-12-docker_amd64.tar.zst",
    "-var", "openbao_primary_vmid=2010",
    "-var", "openbao_unsealer_vmid=2011",
)


def _terraform_binary() -> str | None:
    import shutil

    return shutil.which("terraform") or shutil.which("tofu")


def _live_degraded_gate_reason() -> str | None:
    """Return a skip reason if the live degraded-mode gate is not satisfied,
    else None.

    Needs BOTH credential sets — the SUFFICIENTLY-privileged SDN token (to create
    the prerequisite foundation VLAN-20 zone/VNet) AND the under-privileged
    no-`SDN.Allocate` token (for the negative SVC-07 apply) — plus a
    terraform/tofu binary. If ANY is absent the test SKIPS cleanly (never fails),
    per the documentation-testing steering rule "absent infra => skipped, not
    failed". Same contract as the sibling ``test_degraded_sdn_use.py``.
    """
    if _terraform_binary() is None:
        return (
            "requires-infra, skipped: no terraform/tofu binary available to "
            "drive a live SVC-07 apply against a no-SDN.Allocate Proxmox test "
            "identity"
        )
    if not os.environ.get(_SDN_TEST_ENDPOINT_ENV):
        return (
            f"requires-infra, skipped: {_SDN_TEST_ENDPOINT_ENV} not set "
            "(a privileged Proxmox SDN test-cluster endpoint is required to "
            "provision the prerequisite VLAN-20 zone/VNet)"
        )
    if not os.environ.get(_SDN_TEST_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_SDN_TEST_TOKEN_ENV} not set "
            "(a token WITH SDN.Allocate is required to provision the "
            "prerequisite VLAN-20 zone/VNet)"
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
class TestSvc07MissingSdnAllocateAuthorizationFailure:
    """Live degraded-mode: SVC-07 apply with an identity lacking SDN.Allocate (Req 9.8).

    Per ADR-0001, ``SDN.Allocate`` (not ``SDN.Use``) is the privilege Proxmox
    checks when the SVC-07 LXCs attach their NICs to the VLAN-20 SDN VNet. This
    test reproduces the authorization denial by withholding ``SDN.Allocate``.

    Runs ONLY when a terraform/tofu binary AND BOTH Proxmox test identities are
    present:

      * a SUFFICIENTLY-privileged SDN token (``PROXMOX_SDN_TEST_*``, WITH
        ``SDN.Allocate``) used to FIRST provision the foundation VLAN-20 SDN
        zone/VNet, and
      * a scoped ``no-SDN.Allocate`` token (``PROXMOX_TEST_TOKEN_NO_SDN_ALLOCATE``)
        used for the negative SVC-07 apply.

    Asserts, for the negative SVC-07 apply:

      * it FAILS (non-zero exit) with a Proxmox authorization error, and
      * NO ``proxmox_virtual_environment_container`` resource is reported
        provisioned in state (Requirement 9.8).

    The scoped no-``SDN.Allocate`` identity MUST have every other privilege the
    apply needs (VM/LXC/storage/network) but MUST be missing exactly
    ``SDN.Allocate``, so the only reason for the failure is the deliberately
    withheld create/manage privilege — never an over-privileged or production
    identity. Both tokens are threaded ONLY into the subprocess env by the
    ``live_apply`` helper (never logged, never on a command line, never in an
    assertion message) — Req 3.6 of the fix-live-apply-backend-init spec.
    """

    def test_apply_surfaces_authorization_failure_and_provisions_nothing(self):
        import json
        import shutil
        import subprocess
        import tempfile
        from contextlib import contextmanager

        from conftest import (
            live_apply,
            _copy_roots_into_sandbox,
            _live_override_content,
            _LIVE_OVERRIDE_FILENAME,
        )

        # --- Local live_apply variant for the SVC-07 root -------------------
        # The shared helper's root map only knows `foundation` /
        # `project_template`. Rather than widen that shared contract, we reuse
        # its EXACT mechanism (copy both known roots into a sandbox to preserve
        # the ../../platform-foundation/projects.yaml relative read, then also
        # copy the whole infra/ tree so ../../modules/proxmox-compute resolves,
        # write the transient `backend "local"` override into the sandboxed
        # SVC-07 root, `init -reconfigure`, then apply). Credentials go ONLY into
        # the subprocess env (Req 3.6).
        @contextmanager
        def _svc07_live_apply(state_file: Path, *, endpoint: str, token: str):
            binary = _terraform_binary()
            assert binary is not None, "gated by _live_degraded_gate_reason"
            state_file = Path(state_file).resolve()
            env = {
                **os.environ,
                "TF_VAR_proxmox_endpoint": endpoint,
                "TF_VAR_proxmox_api_token": token,
                "TF_VAR_proxmox_insecure": "true",
            }
            sandbox = Path(tempfile.mkdtemp(prefix="svc07_live_apply_"))
            try:
                # Copy the whole infra/ tree so the SVC-07 root's
                # ../../modules/proxmox-compute source resolves in-sandbox.
                shutil.copytree(_INFRA_DIR, sandbox / "infra")
                target_root = sandbox / "infra" / "projects" / "svc-07-secrets-manager"
                for stale in (target_root / ".terraform",):
                    if stale.is_dir():
                        shutil.rmtree(stale)
                for stale_file in (target_root / "terraform.tfstate",):
                    if stale_file.is_file():
                        stale_file.unlink()
                (target_root / _LIVE_OVERRIDE_FILENAME).write_text(
                    _live_override_content(state_file), encoding="utf-8"
                )
                subprocess.run(
                    [binary, "init", "-reconfigure", "-input=false", "-no-color"],
                    cwd=target_root, env=env, capture_output=True, text=True,
                )

                def _apply(*var_args: str):
                    proc = subprocess.run(
                        [binary, "apply", "-auto-approve", "-input=false",
                         "-no-color", *var_args],
                        cwd=target_root, env=env, capture_output=True, text=True,
                    )
                    return proc.returncode, proc.stdout + proc.stderr

                yield _apply
            finally:
                shutil.rmtree(sandbox, ignore_errors=True)

        # Privileged SDN identity — provisions the prerequisite foundation zone.
        sdn_endpoint = os.environ[_SDN_TEST_ENDPOINT_ENV]
        sdn_token = os.environ[_SDN_TEST_TOKEN_ENV]

        # Under-privileged identity — LACKS SDN.Allocate; used for the negative
        # SVC-07 apply that must be DENIED.
        endpoint = os.environ[_NO_SDN_ALLOCATE_ENDPOINT_ENV]
        token = os.environ[_NO_SDN_ALLOCATE_TOKEN_ENV]

        with tempfile.TemporaryDirectory() as tmp:
            foundation_state = Path(tmp) / "foundation.tfstate"
            svc07_state = Path(tmp) / "svc07.tfstate"

            try:
                # --- Prerequisite: provision the foundation VLAN-20 SDN zone/VNet
                # with a SUFFICIENTLY-privileged token, so the SVC-07 apply's
                # NIC-attach references an EXISTING SDN object. Without this, the
                # under-privileged apply could fail on a missing bridge/zone (a
                # sequencing error) instead of the intended authorization denial
                # (ADR-0001).
                with live_apply(
                    "foundation",
                    foundation_state,
                    endpoint=sdn_endpoint,
                    token=sdn_token,
                    insecure=True,
                ) as foundation:
                    found_apply = foundation.apply()
                assert found_apply.returncode == 0, (
                    "prerequisite foundation SDN apply (privileged token) must "
                    "succeed so the negative SVC-07 apply fails on AUTHORIZATION, "
                    "not on a missing SDN object (ADR-0001).\n"
                    f"output:\n{found_apply.output}"
                )

                # --- Negative apply: attempt the SVC-07 root with the
                # under-privileged (no-SDN.Allocate) token. The local variant
                # writes a transient `backend "local"` override into a SANDBOX
                # COPY and `init -reconfigure`s it, so the apply REACHES Proxmox
                # instead of aborting at backend init.
                with _svc07_live_apply(
                    svc07_state, endpoint=endpoint, token=token
                ) as svc07_apply:
                    rc, output = svc07_apply(*_SVC07_APPLY_VARS)

                # (1) The apply MUST fail — a missing privilege is never a success.
                assert rc != 0, (
                    "SVC-07 apply against an identity lacking SDN.Allocate must "
                    f"FAIL (Requirement 9.8), but it exited 0.\noutput:\n{output}"
                )

                # (2) The failure must be an authorization/permission error — not
                # an unrelated failure. The operative marker under the corrected
                # model is `sdn.allocate` (ADR-0001); `sdn.use` retained for
                # tolerance.
                combined = output.lower()
                assert any(
                    marker in combined
                    for marker in ("permission", "not allowed", "403",
                                   "authorization", "privilege",
                                   "sdn.allocate", "sdn.use")
                ), (
                    "the surfaced failure must be a Proxmox authorization error "
                    f"(Requirement 9.8). Got:\noutput:\n{output}"
                )

                # (3) NO container may be reported provisioned: the state file
                # (if written at all) must contain zero container resource
                # instances (Requirement 9.8).
                provisioned_containers = 0
                if svc07_state.is_file():
                    try:
                        state = json.loads(svc07_state.read_text())
                    except json.JSONDecodeError:
                        state = {}
                    for res in state.get("resources", []):
                        if res.get("type") == "proxmox_virtual_environment_container":
                            provisioned_containers += len(res.get("instances", []))
                assert provisioned_containers == 0, (
                    "no LXC container may be reported provisioned when "
                    "SDN.Allocate is missing (Requirement 9.8); found "
                    f"{provisioned_containers} in state"
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
