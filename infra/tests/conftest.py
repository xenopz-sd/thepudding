"""Shared offline-plan helper for the terraform-gated tests in ``infra/tests/``.

Spec: fix-offline-terraform-plan-tests (bugfix) — Change 1 (design.md
"Fix Implementation / Change 1 — Shared offline-plan helper").

=== WHY THIS EXISTS ===

Both Terraform roots in this repo declare a *partial* GitLab-managed HTTP
backend (``terraform { backend "http" {} }``, PF FR-2). Offline — the ordinary
local/CI-without-live-backend case — none of that backend's address /
lock-endpoint / ``CI_JOB_TOKEN`` credentials exist. The old test helpers ran
``terraform init -backend=false`` (which initialises providers but configures
NO backend) and then ``terraform plan`` (which needs a configured backend), so
``plan`` aborted with ``Error: Backend initialization required ... backend
"http"`` and every terraform-gated test FAILED instead of passing or skipping.

``offline_plan`` fixes that class of failure *without touching the committed
roots* (Req 3.3): it copies BOTH roots into a throwaway sandbox that preserves
their relative layout, drops a transient ``backend "local" {}`` override into
the sandboxed target root only, ``init -reconfigure``s that credential-free
local backend, and runs ``plan`` with dummy (non-contacting)
``proxmox_endpoint`` / ``proxmox_api_token`` values. The committed
``infra/platform-foundation/`` and ``infra/projects/_TEMPLATE/`` trees are never
written to.

=== IMPORTING FROM SIBLING TEST MODULES ===

This module lives at ``infra/tests/conftest.py``. With pytest's default
``prepend`` import mode, the directory containing a ``conftest.py`` is inserted
onto ``sys.path``, so sibling test modules in the same directory can simply::

    from conftest import offline_plan, requires_terraform, _terraform_binary

As a belt-and-braces guarantee (in case the suite is ever invoked in a mode
where that insertion does not happen), the same directory is also inserted onto
``sys.path`` at import time below, so ``from conftest import ...`` resolves
regardless of the invocation style.

=== SECURITY NOTE ===

The dummy ``proxmox_api_token`` is a throwaway, non-secret placeholder in the
valid ``bpg/proxmox`` token *format* (``<user>@<realm>!<token-id>=<uuid>``)
pointed at an unroutable host (``dummy.invalid``). It is NOT a real credential
and never reaches a real Proxmox API (the roots declare only SDN resources with
no read-time data sources). It must never be treated as, or echoed as, a real
secret (security-standards: no real secret in a repo, ever).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pytest

# --------------------------------------------------------------------------- #
# Make ``from conftest import ...`` resolve from sibling test modules even if
# pytest's prepend-mode sys.path insertion is not in effect for some invocation.
# --------------------------------------------------------------------------- #
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# --------------------------------------------------------------------------- #
# Locate the two Terraform roots relative to this file (infra/tests/...).
# --------------------------------------------------------------------------- #
_INFRA_DIR = _THIS_DIR.parent
_FOUNDATION_DIR = _INFRA_DIR / "platform-foundation"
_PROJECT_TEMPLATE_DIR = _INFRA_DIR / "projects" / "_TEMPLATE"

#: Named roots the helper accepts, mapped to their committed source dirs and
#: their relative path WITHIN the preserved sandbox layout. Keeping both roots
#: in the sandbox at their real relative positions is what lets the project
#: root's ``${path.module}/../../platform-foundation/projects.yaml`` read
#: resolve (design.md Change 1, step 1).
_ROOTS: dict[str, Path] = {
    "foundation": _FOUNDATION_DIR,
    "project_template": _PROJECT_TEMPLATE_DIR,
}

#: The sandbox mirrors ``infra/`` closely enough for the roots' relative reads:
#: ``<sandbox>/platform-foundation/`` and ``<sandbox>/projects/_TEMPLATE/``.
_SANDBOX_REL: dict[str, Path] = {
    "foundation": Path("platform-foundation"),
    "project_template": Path("projects") / "_TEMPLATE",
}

#: Dummy, non-contacting provider inputs (design.md Change 1). The endpoint is
#: unroutable; the token is a valid-FORMAT throwaway placeholder, never a real
#: secret. The roots declare only SDN resources with no read-time data sources,
#: so a plan completes without ever contacting the Proxmox API.
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"

#: The transient local-backend override filename written into the sandbox target
#: root only. The ``zz_`` prefix keeps it lexically last; the name is what the
#: preservation test's forbidden-artifact regex looks for, so it must never be
#: written into a committed root.
_OVERRIDE_FILENAME = "zz_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient, test-scoped local backend override written by\n"
    "# infra/tests/conftest.py::offline_plan into a SANDBOX COPY only.\n"
    "# It overrides the roots' partial `backend \"http\" {}` so an offline\n"
    "# `terraform init -reconfigure` configures a credential-free backend and\n"
    "# the subsequent `plan` succeeds without a live GitLab HTTP backend /\n"
    "# CI_JOB_TOKEN. NEVER committed into a real root (Req 3.3, PF FR-2).\n"
    "terraform {\n"
    "  backend \"local\" {}\n"
    "}\n"
)

#: Substrings identifying the partial-``backend "http"`` init abort — used to
#: distinguish "the bug we are fixing" from "an unrelated environmental init
#: failure that should skip cleanly". Matched case-insensitively.
_BACKEND_INIT_MARKERS = (
    "backend initialization required",
    'backend "http"',
    "initial configuration of the requested backend",
)

#: Substrings that indicate init failed for an ENVIRONMENTAL reason unrelated to
#: the bug under test (e.g. no provider mirror reachable offline) — in which
#: case the helper skips cleanly with a requires-infra reason (design.md Change
#: 1, skip-cleanly contract; Req 2.1). Matched case-insensitively.
_PROVIDER_UNAVAILABLE_MARKERS = (
    "failed to install provider",
    "could not retrieve the list of available versions",
    "error while installing",
    "no available releases match",
    "failed to query available provider packages",
    "could not connect",
    "network is unreachable",
    "timeout",
    "dial tcp",
)


def _terraform_binary() -> str | None:
    """Return the ``terraform`` (or ``tofu``) binary path, or ``None``.

    Unchanged idiom shared across the suite: ``shutil.which("terraform") or
    shutil.which("tofu")``. The existing ``requires_terraform`` / ``skipif``
    markers remain the OUTER gate (absent binary => skip, per steering); this
    helper is only ever called once that gate has passed.
    """
    return shutil.which("terraform") or shutil.which("tofu")


#: Re-exported so sibling test modules can gate on the toolchain with the same
#: idiom they already use — ``from conftest import requires_terraform``.
requires_terraform = pytest.mark.skipif(
    _terraform_binary() is None,
    reason="requires terraform toolchain",
)


@dataclass
class OfflinePlanResult:
    """The outcome of an offline ``terraform plan`` run.

    Attributes:
        returncode: the ``terraform plan`` process exit code (0 = success).
        output: combined ``stdout`` + ``stderr`` of the ``plan`` invocation.
        plan_path: path to the ``-out`` plan file inside the sandbox (may or may
            not exist depending on whether the plan wrote it).
        binary: the terraform/tofu binary used (needed for ``terraform show``).
    """

    returncode: int
    output: str
    plan_path: Path
    binary: str
    _resource_changes: list[dict] | None = field(default=None, repr=False)

    @property
    def resource_changes(self) -> list[dict]:
        """Lazily-computed ``resource_changes`` from ``terraform show -json``.

        Returns the plan's ``resource_changes`` array when the ``-out`` plan
        file exists, else ``[]``. The value is computed once and cached.

        NOTE: Terraform 1.16.0 writes the ``-out`` file even for a FAILED plan,
        but ``terraform show -json`` on that partial artifact reports zero
        ``resource_changes`` — which is exactly the version-robust
        "provisions nothing" signal the unregistered-slug test relies on
        (design.md Change 4).
        """
        if self._resource_changes is None:
            self._resource_changes = self._compute_resource_changes()
        return self._resource_changes

    def _compute_resource_changes(self) -> list[dict]:
        if not self.plan_path.is_file():
            return []
        # `terraform show -json <plan>` must run in the root that produced the
        # plan: it needs that root's `.terraform` provider plugins to load the
        # plan's schemas. Running it elsewhere fails with "Failed to load
        # plugin schemas". The plan file lives inside the sandbox target root.
        show = subprocess.run(
            [self.binary, "show", "-json", self.plan_path.name],
            cwd=self.plan_path.parent,
            capture_output=True,
            text=True,
        )
        if show.returncode != 0 or not show.stdout.strip():
            return []
        try:
            data = json.loads(show.stdout)
        except json.JSONDecodeError:
            return []
        return data.get("resource_changes", []) or []


def _copy_roots_into_sandbox(sandbox: Path) -> None:
    """Copy BOTH roots into ``sandbox`` preserving their relative layout.

    Produces ``<sandbox>/platform-foundation/`` and
    ``<sandbox>/projects/_TEMPLATE/`` so the project root's
    ``../../platform-foundation/projects.yaml`` read resolves. Any copied
    ``.terraform/`` provider-cache dir is removed so ``init -reconfigure`` in
    the sandbox starts clean.
    """
    for name, src in _ROOTS.items():
        dst = sandbox / _SANDBOX_REL[name]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        # Drop a stale provider cache / local state that would confuse
        # `init -reconfigure` in the sandbox.
        stale_terraform = dst / ".terraform"
        if stale_terraform.is_dir():
            shutil.rmtree(stale_terraform)
        stale_state = dst / "terraform.tfstate"
        if stale_state.is_file():
            stale_state.unlink()


@contextmanager
def offline_plan(
    root_name: str,
    *var_args: str,
    refresh: bool = False,
) -> Iterator[OfflinePlanResult]:
    """Run an offline ``terraform plan`` for a root against a local backend.

    Copies both committed roots into a throwaway sandbox (preserving the
    ``platform-foundation/`` + ``projects/_TEMPLATE/`` relative layout), writes a
    transient ``backend "local" {}`` override into the sandboxed TARGET root
    only, ``init -reconfigure``s it, and runs ``terraform plan`` supplying dummy
    ``proxmox_endpoint`` / ``proxmox_api_token`` values plus any extra ``-var``
    arguments passed via ``*var_args``. The committed roots are never written to.

    Args:
        root_name: ``"foundation"`` or ``"project_template"``.
        *var_args: extra ``terraform plan`` arguments, typically
            ``"-var", "project_slug=mlvideo"``.
        refresh: when ``False`` (default), passes ``-refresh=false`` to ``plan``.

    Yields:
        An :class:`OfflinePlanResult` with ``returncode``, combined ``output``,
        the ``plan_path``, and a lazily-computed ``resource_changes`` list.

    Raises:
        pytest.skip.Exception: if ``init`` fails for an environmental reason
            unrelated to the bug under test (e.g. providers cannot be installed
            offline) — a documented requires-infra skip, never a failure
            (design.md Change 1, skip-cleanly contract; Req 2.1).

    On exit the entire sandbox (and thus the transient override + plan file) is
    removed.
    """
    if root_name not in _ROOTS:
        raise ValueError(
            f"unknown root_name {root_name!r}; expected one of {sorted(_ROOTS)}"
        )

    binary = _terraform_binary()
    assert binary is not None, (
        "offline_plan called without a terraform/tofu binary present; the "
        "requires_terraform marker should gate this"
    )

    sandbox = Path(tempfile.mkdtemp(prefix="offline_plan_"))
    try:
        _copy_roots_into_sandbox(sandbox)
        target_root = sandbox / _SANDBOX_REL[root_name]

        # Transient local-backend override into the sandbox TARGET root ONLY.
        (target_root / _OVERRIDE_FILENAME).write_text(
            _OVERRIDE_CONTENT, encoding="utf-8"
        )

        # init -reconfigure: configure the credential-free local backend.
        init = subprocess.run(
            [binary, "init", "-reconfigure", "-input=false", "-no-color"],
            cwd=target_root,
            capture_output=True,
            text=True,
        )
        if init.returncode != 0:
            combined = (init.stdout + init.stderr).lower()
            # If init failed on the partial http backend, that IS the bug — let
            # it flow through as a (failed) plan-less result so the caller's
            # assertion catches it rather than masking it as a skip.
            hit_backend_bug = any(
                m in combined for m in _BACKEND_INIT_MARKERS
            )
            hit_env_issue = any(
                m in combined for m in _PROVIDER_UNAVAILABLE_MARKERS
            )
            if hit_env_issue and not hit_backend_bug:
                pytest.skip(
                    "requires-infra, skipped: `terraform init` could not "
                    "install providers / reach a provider source offline, "
                    "which is an environmental prerequisite unrelated to the "
                    "backend bug under test. Offline plan cannot run here.\n"
                    f"init stderr tail:\n{init.stderr[-1500:]}"
                )
            # Otherwise surface the init failure as a zero-plan result so the
            # caller sees a non-zero returncode and the raw output.
            yield OfflinePlanResult(
                returncode=init.returncode,
                output=init.stdout + init.stderr,
                plan_path=target_root / "offline.tfplan",
                binary=binary,
            )
            return

        plan_path = target_root / "offline.tfplan"
        plan_cmd = [
            binary, "plan", "-input=false", "-no-color",
        ]
        if not refresh:
            plan_cmd.append("-refresh=false")
        plan_cmd += [
            "-var", f"proxmox_endpoint={_DUMMY_ENDPOINT}",
            "-var", f"proxmox_api_token={_DUMMY_TOKEN}",
            *var_args,
            f"-out={plan_path}",
        ]
        # NOTE: no check=True — a non-zero exit is inspected, not raised.
        plan = subprocess.run(
            plan_cmd,
            cwd=target_root,
            capture_output=True,
            text=True,
        )
        yield OfflinePlanResult(
            returncode=plan.returncode,
            output=plan.stdout + plan.stderr,
            plan_path=plan_path,
            binary=binary,
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# =========================================================================== #
# Live-apply helper (spec: fix-live-apply-backend-init — Change 1).
#
# Unlike ``offline_plan`` (which sandboxes, plans against DUMMY vars, and
# discards state), ``live_apply`` drives a REAL ``terraform apply``/``destroy``
# against the ephemeral Proxmox SDN test cluster, keeping state in a DISPOSABLE
# temp file the caller can then READ. Each ``live_apply(...)`` invocation builds
# its OWN sandbox with its OWN transient ``backend "local" { path = <caller
# state_file> }`` override, so applying the SAME source root twice (project A
# and project B) writes to two SEPARATE state files — the per-(root,state)
# isolation the coexistence/destroy-isolation tests depend on.
#
# Mechanism reused verbatim from ``offline_plan``: copy BOTH roots into a fresh
# sandbox preserving the ``platform-foundation/`` + ``projects/_TEMPLATE/``
# relative layout (so the project root's
# ``../../platform-foundation/projects.yaml`` read resolves), write the override
# into the sandboxed TARGET root only, and ``init -reconfigure`` the local
# backend. The difference: NOT ``-backend=false`` (the bug), the override's
# ``path`` points at the caller-owned state file (readable after teardown), and
# ``apply``/``destroy`` carry NO deprecated ``-state=`` flag.
#
# SECURITY (Req 3.6): the Proxmox token is placed ONLY into the subprocess
# ``env`` (``TF_VAR_proxmox_api_token``) — NEVER in a command line, a print, or
# an assertion/return message. The ``LiveApplyResult.output`` is Terraform's own
# ``-no-color`` stdout+stderr, which does not echo ``TF_VAR_*`` values.
# =========================================================================== #

#: Transient local-backend override filename for the LIVE-apply helper. Distinct
#: from ``offline_plan``'s ``zz_offline_backend_override.tf`` but sharing the
#: ``zz_``/``*backend_override*`` shape so the preservation forbidden-artifact
#: check (``zz_*backend_override*.tf``) covers it too. Written into a SANDBOX
#: COPY only — NEVER a committed root (Req 3.1, PF FR-2).
_LIVE_OVERRIDE_FILENAME = "zz_live_backend_override.tf"


def _live_override_content(state_file: Path) -> str:
    """Render the transient ``backend "local" { path = <abs state_file> }`` block.

    The ``path`` is the caller's ABSOLUTE state-file path (in the caller's own
    ``TemporaryDirectory``), so the produced state remains readable AFTER the
    sandbox is torn down. This is the direct replacement for the deprecated
    ``-state=<path>`` flag. HCL string escaping: render the absolute path with
    forward slashes / normal POSIX form via ``as_posix()`` and JSON-encode it so
    any unusual characters are safely quoted.
    """
    encoded_path = json.dumps(str(state_file))
    return (
        "# Transient, test-scoped local backend override written by\n"
        "# infra/tests/conftest.py::live_apply into a SANDBOX COPY only.\n"
        "# It overrides the root's partial `backend \"http\" {}` so a live\n"
        "# `terraform init -reconfigure` configures a credential-free local\n"
        "# backend whose state lives at a disposable, caller-owned temp path —\n"
        "# the direct replacement for the deprecated `-state=<path>` flag.\n"
        "# NEVER committed into a real root (Req 3.1, PF FR-2).\n"
        "terraform {\n"
        "  backend \"local\" {\n"
        f"    path = {encoded_path}\n"
        "  }\n"
        "}\n"
    )


@dataclass
class LiveApplyResult:
    """The outcome of a single live ``terraform apply``/``destroy`` run.

    Attributes:
        returncode: the ``apply``/``destroy`` process exit code (0 = success).
        output: combined ``stdout`` + ``stderr`` of the invocation. This is
            Terraform's ``-no-color`` output; it does NOT contain the Proxmox
            token (which is passed only via ``TF_VAR_*`` env, never argv).
        state_path: the disposable local-backend state file — the caller's own
            ``state_file`` path, now populated and readable via
            ``json.loads(state_path.read_text())["resources"]``.
        binary: the terraform/tofu binary used.
    """

    returncode: int
    output: str
    state_path: Path
    binary: str


class LiveApplySandbox:
    """Handle to a per-(root, state) live-apply sandbox.

    Exposes :meth:`apply` and :meth:`destroy`, each running a REAL Terraform
    command in the sandboxed target root against the local backend whose state
    lives at :attr:`state_path`. The subprocess ``env`` carries the Proxmox
    credentials (endpoint/token/insecure) via ``TF_VAR_*`` — the token is never
    placed on a command line or echoed (Req 3.6).

    Instances are created and torn down by the :func:`live_apply` context
    manager; do not construct directly.
    """

    def __init__(
        self,
        *,
        binary: str,
        target_root: Path,
        state_file: Path,
        env: dict[str, str],
    ) -> None:
        self._binary = binary
        self._target_root = target_root
        self._state_file = state_file
        self._env = env

    @property
    def state_path(self) -> Path:
        """The readable, disposable local-backend state file."""
        return self._state_file

    def _run(self, verb: str, var_args: tuple[str, ...]) -> LiveApplyResult:
        """Run ``terraform <verb> -auto-approve ...`` with NO ``-state=`` flag.

        ``capture_output=True`` and no ``check=True``: a non-zero exit is
        returned for the caller to inspect (the negative-auth test needs to
        assert on a FAILING apply), never raised.
        """
        cmd = [
            self._binary, verb, "-auto-approve", "-input=false", "-no-color",
            *var_args,
        ]
        proc = subprocess.run(
            cmd,
            cwd=self._target_root,
            env=self._env,
            capture_output=True,
            text=True,
        )
        return LiveApplyResult(
            returncode=proc.returncode,
            output=proc.stdout + proc.stderr,
            state_path=self._state_file,
            binary=self._binary,
        )

    def apply(self, *var_args: str) -> LiveApplyResult:
        """``terraform apply -auto-approve`` reaching the Proxmox provider."""
        return self._run("apply", var_args)

    def destroy(self, *var_args: str) -> LiveApplyResult:
        """``terraform destroy -auto-approve`` reaching the Proxmox provider."""
        return self._run("destroy", var_args)


@contextmanager
def live_apply(
    root_name: str,
    state_file: Path,
    *var_args: str,
    endpoint: str,
    token: str,
    insecure: bool = True,
) -> Iterator[LiveApplySandbox]:
    """Live ``terraform apply``/``destroy`` for a root against a LOCAL backend.

    Builds a throwaway sandbox for ONE ``(root, state)`` invocation: copies both
    committed roots into a fresh ``tempfile.mkdtemp(prefix="live_apply_")``
    (preserving the ``platform-foundation/`` + ``projects/_TEMPLATE/`` relative
    layout so the project root's ``../../platform-foundation/projects.yaml`` read
    resolves), writes a transient ``backend "local" { path = <state_file> }``
    override into the sandboxed TARGET root only, and ``init -reconfigure``s that
    credential-free local backend. Yields a :class:`LiveApplySandbox` whose
    ``apply``/``destroy`` reach the Proxmox provider with state written to the
    caller-owned ``state_file``. The committed roots are never written to.

    Because each call makes its OWN sandbox + OWN override -> OWN state file,
    applying the SAME ``project_template`` root for project A and project B
    writes to two SEPARATE state files (the per-(root,state) isolation the
    coexistence/destroy-isolation tests rely on).

    Args:
        root_name: ``"foundation"`` or ``"project_template"``.
        state_file: caller-chosen ABSOLUTE path (in the caller's own tempdir)
            where the local backend keeps state; readable after teardown.
        *var_args: kept in the signature per the design sketch for symmetry with
            ``offline_plan``. Extra ``-var`` arguments are passed per-command to
            :meth:`LiveApplySandbox.apply` / :meth:`LiveApplySandbox.destroy`
            (e.g. ``.apply("-var", "project_slug=dronefleet")``), not here.
        endpoint: Proxmox endpoint (from the caller's ``requires-infra`` env
            var), threaded into ``TF_VAR_proxmox_endpoint``.
        token: Proxmox API token (from the caller's env var), threaded ONLY into
            the subprocess ``TF_VAR_proxmox_api_token`` env — NEVER logged, put
            on a command line, or placed in a return/assertion message (Req 3.6).
        insecure: threaded into ``TF_VAR_proxmox_insecure`` (default ``True`` for
            the self-signed cert on the ephemeral test cluster).

    Yields:
        A :class:`LiveApplySandbox` bound to this invocation's sandbox + state.

    Raises:
        ValueError: if ``root_name`` is not a known root.
        AssertionError: if called without a terraform/tofu binary present (the
            caller's ``requires-infra`` gate should prevent this).

    On exit the entire sandbox (and thus the transient override) is removed; the
    caller-owned ``state_file`` remains readable (it lives in the caller's own
    ``TemporaryDirectory``, not in the sandbox).
    """
    if root_name not in _ROOTS:
        raise ValueError(
            f"unknown root_name {root_name!r}; expected one of {sorted(_ROOTS)}"
        )

    binary = _terraform_binary()
    assert binary is not None, (
        "live_apply called without a terraform/tofu binary present; the "
        "caller's requires-infra gate should prevent this"
    )

    state_file = Path(state_file).resolve()

    # Credentials go ONLY into the child-process env (Req 3.6). The token is
    # never placed on a command line, printed, or returned in a message.
    env = {
        **os.environ,
        "TF_VAR_proxmox_endpoint": endpoint,
        "TF_VAR_proxmox_api_token": token,
        "TF_VAR_proxmox_insecure": "true" if insecure else "false",
    }

    sandbox = Path(tempfile.mkdtemp(prefix="live_apply_"))
    try:
        _copy_roots_into_sandbox(sandbox)
        target_root = sandbox / _SANDBOX_REL[root_name]

        # Transient local-backend override -> sandbox TARGET root ONLY, pointing
        # at the caller's chosen absolute state_file (readable after teardown).
        (target_root / _LIVE_OVERRIDE_FILENAME).write_text(
            _live_override_content(state_file), encoding="utf-8"
        )

        # init -reconfigure: configure the credential-free local backend.
        # NOT -backend=false (that is the bug being fixed).
        subprocess.run(
            [binary, "init", "-reconfigure", "-input=false", "-no-color"],
            cwd=target_root,
            env=env,
            capture_output=True,
            text=True,
        )

        yield LiveApplySandbox(
            binary=binary,
            target_root=target_root,
            state_file=state_file,
            env=env,
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
