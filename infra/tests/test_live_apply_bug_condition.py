"""Bug-condition exploration test for the fix-live-apply-backend-init bugfix.

Spec: fix-live-apply-backend-init (bugfix) — Task 1 (design.md "Testing
Strategy / Exploratory Bug Condition Checking").

=== WHY THIS TEST EXISTS (AND WHY IT FAILS BEFORE THE FIX) ===

The three LIVE (`requires-infra`) apply/destroy tests in this suite
(`test_integration_apply.py::TestFullEphemeralApplyCoexistence`,
`test_destroy_isolation.py::TestLiveDestroyIsolation`,
`test_degraded_sdn_use.py::TestMissingSdnUseAuthorizationFailure`) all drive a
real ``terraform`` against ``infra/platform-foundation/`` and/or
``infra/projects/_TEMPLATE/``, both of which declare a *partial* GitLab-managed
HTTP backend (``terraform { backend "http" {} }``, PF FR-2). Each test runs
``terraform init -backend=false`` (which initialises providers but configures
NO backend) and then ``apply``/``destroy`` with the deprecated ``-state=<tmp>``
flag. The apply/destroy needs a configured backend, so Terraform aborts with
``Error: Backend initialization required ... backend "http"`` **before** ever
contacting Proxmox — and additionally warns ``Warning: Deprecated flag:
-state``. That abort is the bug: the live tests can never reach the cluster and
exercise the coexistence / destroy-isolation / missing-``SDN.Use`` behaviour
they exist to prove.

This exploration test asserts the offline-safe EXPECTED (fixed) behaviour: the
combined output of a live apply against a SANDBOX COPY of each root does NOT
contain the backend-init abort markers and does NOT contain ``Deprecated flag:
-state``.

=== THE EXPLORATION -> VALIDATION FLIP (design.md "Exploratory Bug Condition
Checking") ===

BEFORE the fix, this test drove the CURRENT (buggy) helper sequence
(``init -backend=false`` then ``apply -state=<tmp>``) inlined directly against a
sandbox copy, and the offline-safe assertions FAILED — the backend-init abort
markers (``Backend initialization required ... backend "http"``) and the
``Deprecated flag: -state`` warning were both present. That failure confirmed
the bug.

AFTER the fix (Tasks 3.1-3.5), the SAME offline-safe assertions are driven
through the FIXED ``live_apply`` helper (``conftest.py``) — which writes a
transient ``backend "local" { path = ... }`` override and runs
``init -reconfigure`` (NOT ``-backend=false``), then ``apply`` with NO
``-state=`` flag. The fixed path therefore never emits the backend-init abort
markers and never emits ``Deprecated flag: -state``, so the same file that
FAILED on the unfixed code now PASSES. That is the exploration -> validation
flip: the assertions and the file are unchanged in intent; only the code path
they exercise (the shared ``live_apply`` helper instead of an inlined broken
sequence) reflects the fix.

Because the backend-init abort was an ``init``/backend-config-time failure — and
the fixed ``init -reconfigure`` likewise runs before any Proxmox contact — this
test needs only a ``terraform``/``tofu`` binary and DUMMY credentials pointed at
an unroutable host: NO live cluster and NO ``PROXMOX_*`` creds. The ``apply``
against the dummy endpoint is expected to fail (it cannot reach Proxmox), but it
fails WITHOUT the backend-init abort markers and WITHOUT the deprecated-flag
warning — which is exactly the offline-safe fixed behaviour under test.

=== SECURITY NOTE ===

The dummy ``proxmox_api_token`` reuses conftest's throwaway, valid-FORMAT
placeholder pointed at an unroutable host — it is NOT a real credential and
never reaches a real Proxmox API. It must never be treated as, or echoed as, a
real secret.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from conftest import (
    _DUMMY_ENDPOINT,
    _DUMMY_TOKEN,
    live_apply,
    requires_terraform,
)

# A registered fixture slug so the project template can resolve its vlan_id via
# the projects.yaml registry lookup (dronefleet -> vlan 100 is registered).
_DUMMY_PROJECT_SLUG = "dronefleet"

# Markers identifying the partial-`backend "http"` init abort — the bug.
_BACKEND_INIT_MARKERS = (
    "Backend initialization required",
    'backend "http"',
    "Initial configuration of the requested backend",
)

# The deprecated-flag warning emitted by the current `-state=<path>` sequence.
_DEPRECATED_STATE_MARKER = "Deprecated flag: -state"


def _run_fixed_sequence(root_name: str, *var_args: str) -> str:
    """Drive the FIXED ``live_apply`` helper against a sandbox copy of a root.

    This is the exploration -> validation flip (design.md "Exploratory Bug
    Condition Checking"): where this helper previously inlined the CURRENT
    (buggy) ``init -backend=false`` + ``apply -state=<tmp>`` sequence, it now
    drives the SAME sandbox/root through the shared, FIXED ``live_apply`` context
    manager from ``conftest.py``, which:

      1. writes a transient ``backend "local" { path = <disposable state> }``
         override into a SANDBOX COPY of the target root (committed roots are
         never written to);
      2. runs ``terraform init -reconfigure`` (NOT ``-backend=false``) to
         configure the credential-free local backend;
      3. runs ``terraform apply -auto-approve`` with NO deprecated ``-state=``
         flag.

    DUMMY provider creds pointed at an unroutable host are threaded in via the
    helper's ``endpoint``/``token`` params, so NO live cluster and NO
    ``PROXMOX_*`` creds are required. The apply is *attempted* and expected to
    FAIL (it cannot reach the dummy endpoint) — but crucially it fails WITHOUT
    the backend-init abort markers and WITHOUT the ``Deprecated flag: -state``
    warning, which is exactly the offline-safe fixed behaviour under test.

    Returns the combined stdout+stderr of the fixed ``apply``.
    """
    # The caller-owned state file lives in its own tempdir so it survives the
    # helper's sandbox teardown (matching the real live-apply tests' pattern);
    # for this offline exploration we only inspect the apply OUTPUT, not state.
    caller_tmp = Path(tempfile.mkdtemp(prefix="live_apply_bug_state_"))
    state_file = caller_tmp / f"{root_name}.tfstate"
    with live_apply(
        root_name,
        state_file,
        endpoint=_DUMMY_ENDPOINT,
        token=_DUMMY_TOKEN,
        insecure=True,
    ) as sandbox:
        result = sandbox.apply(*var_args)
    return result.output


@requires_terraform
class TestLiveApplyBackendInitBugCondition:
    """Property 1 (Bug Condition): the current live-apply sequence aborts at
    backend init before reaching Proxmox.

    Each test asserts the offline-safe EXPECTED (fixed) behaviour — no
    backend-init abort markers, no ``Deprecated flag: -state`` warning. On the
    UNFIXED helpers these assertions FAIL (the abort + deprecated-flag warning
    are present), which is the intended exploration outcome that confirms the
    bug. After the fix the same assertions PASS.

    Gated behind ``requires_terraform`` only — NO cluster, NO ``PROXMOX_*``
    creds needed, because the backend-init abort fires at init/backend-config
    time before any Proxmox contact.
    """

    def test_foundation_apply_does_not_abort_at_backend_init(self):
        """Foundation root: the live-apply path must NOT abort at backend init.

        Drives the FIXED ``live_apply`` helper (``init -reconfigure`` against a
        transient ``backend "local"`` override, then ``apply`` with no
        ``-state=`` flag) on a sandbox copy of ``infra/platform-foundation/``.
        On the UNFIXED code this FAILED because the inlined
        ``init -backend=false`` + ``apply -state=<tmp>`` sequence aborted with
        ``Backend initialization required ... backend "http"`` (Req 1.1, 1.2)
        and emitted ``Deprecated flag: -state`` (Req 1.5); on the fixed helper
        the same assertions PASS (Req 2.1, 2.5).
        """
        output = _run_fixed_sequence("foundation")

        present = [m for m in _BACKEND_INIT_MARKERS if m in output]
        assert not present, (
            "the foundation live-apply sequence must NOT abort at backend init "
            "(Req 2.1); found backend-init abort marker(s) "
            f"{present!r} in the combined output:\n{output}"
        )
        assert _DEPRECATED_STATE_MARKER not in output, (
            "the foundation live-apply sequence must NOT use the deprecated "
            f"-state flag (Req 2.5); found {_DEPRECATED_STATE_MARKER!r} in the "
            f"combined output:\n{output}"
        )

    def test_project_apply_does_not_abort_at_backend_init(self):
        """Project template root: the live-apply path must NOT abort at backend
        init.

        Drives the FIXED ``live_apply`` helper on a sandbox copy of
        ``infra/projects/_TEMPLATE/`` with a dummy (registered) ``project_slug``.
        On the UNFIXED code this FAILED because the inlined
        ``init -backend=false`` + ``apply -state=<tmp>`` sequence aborted with
        ``Backend initialization required ... backend "http"`` (Req 1.3, 1.4)
        and emitted ``Deprecated flag: -state`` (Req 1.5); on the fixed helper
        the same assertions PASS (Req 2.1, 2.5).
        """
        output = _run_fixed_sequence(
            "project_template", "-var", f"project_slug={_DUMMY_PROJECT_SLUG}"
        )

        present = [m for m in _BACKEND_INIT_MARKERS if m in output]
        assert not present, (
            "the project live-apply sequence must NOT abort at backend init "
            "(Req 2.1); found backend-init abort marker(s) "
            f"{present!r} in the combined output:\n{output}"
        )
        assert _DEPRECATED_STATE_MARKER not in output, (
            "the project live-apply sequence must NOT use the deprecated "
            f"-state flag (Req 2.5); found {_DEPRECATED_STATE_MARKER!r} in the "
            f"combined output:\n{output}"
        )
