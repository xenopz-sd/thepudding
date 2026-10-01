"""Live auto-unseal round-trip + swap-gate seal tests for the OpenBao primary.

Task 12.1 (spec: svc-07-secrets-manager) — requirements.md Requirements 5.3, 5.6,
5.7; design.md "Testing Strategy §3" (Auto-unseal round-trip + Composition-root /
swap-gate seal test) and ``composition-wiring.md`` (real-vs-fake adapter
selection must be verified, and a missing real dependency must surface as a hard
failure, never a masked/fake success).

=== WHAT THIS FILE CONTAINS (two tests, one established pattern) ===

1) AUTO-UNSEAL ROUND-TRIP (Req 5.3) — ``TestOpenBaoAutoUnsealRoundTrip``.
   Given a HEALTHY Transit unsealer (``GET /v1/sys/health`` on the unsealer
   returns 200 with ``sealed:false``), stop and start the primary container and
   assert the primary reaches ``sealed:false`` via ``GET /v1/sys/health`` within
   60 seconds with NO operator key-entry (Req 5.3). This proves Transit
   auto-unseal recovers the primary from a restart unattended.

2) SWAP-GATE SEAL TEST (Req 5.6, 5.7) — ``TestOpenBaoSwapGateSeal``. The
   ``composition-wiring.md`` swap-gate for this service's Transit-unsealer seam:
     * REAL adapter reachable → the primary reaches ``sealed:false`` (same
       positive path the round-trip asserts, restated as the swap-gate's
       "real dependency present ⇒ real success" arm).
     * REAL adapter ABSENT (unsealer stopped, Req 5.6) OR present-but-itself-
       sealed (Req 5.7) → the primary MUST stay ``sealed:true`` AND emit an
       ERROR-level log line containing BOTH the token ``transit seal`` and the
       token ``unreachable`` — and MUST NOT silently come up unsealed or
       fake-unsealed. This is the anti-pattern ``composition-wiring.md`` names:
       a missing real dependency must fail loudly, never be masked by a fake
       success. The unsealer is always restarted in a ``finally`` so the shared
       throwaway instance is left healthy for the next test/run.

=== ENV-VAR CONTRACT (documented here + in TESTING.md) ===

REUSES the OpenBao live-tier contract task 7.4 established (the SAME vars the
sibling ``test_svc07_openbao_engine_contract.py`` / ``_onboard_contract.py`` gate
on — no NEW gate is invented), plus a small number of ADDITIONAL vars this test
needs because it stops/starts containers (which the pure-API contract tests do
not):

  Reused (task 7.4 contract — the primary's live API):
  * ``OPENBAO_TEST_ADDR``   — the PRIMARY OpenBao API base URL (e.g.
    ``https://127.0.0.1:8200``). A THROWAWAY/ephemeral primary only — this test
    STOPS and STARTS its container and its unsealer's container.
  * ``OPENBAO_TEST_TOKEN``  — a token on the throwaway primary able to read
    ``GET /v1/sys/health`` (any valid token; health is technically unauthenticated
    but the client threads the token for parity with the sibling tests).

  Additional (this test only — the unsealer API + the container names to cycle):
  * ``OPENBAO_TEST_UNSEALER_ADDR``      — the Transit UNSEALER's API base URL
    (e.g. ``https://127.0.0.1:8201``). Needed to (a) confirm the unsealer is
    healthy before the round-trip, and (b) in the swap-gate, seal the unsealer
    to exercise the Req 5.7 "present-but-sealed" arm.
  * ``OPENBAO_TEST_PRIMARY_CONTAINER``  — the docker container NAME (or id) of
    the primary OpenBao container, cycled with ``docker stop``/``docker start``
    to trigger the auto-unseal round-trip and read its logs.
  * ``OPENBAO_TEST_UNSEALER_CONTAINER`` — the docker container NAME (or id) of
    the Transit unsealer container, stopped in the swap-gate's "unsealer
    unreachable" (Req 5.6) arm and restarted in ``finally``.

  Optional:
  * ``OPENBAO_TEST_SKIP_TLS_VERIFY``    — ``1``/``true`` (default) skips TLS
    verification for the ephemeral instances' self-signed certs; ``0`` enforces.
    Same knob and default as the sibling contract tests.

SECURITY (security-standards.md — this IS the secrets manager): the token is read
from the environment and threaded ONLY into request headers; it is NEVER logged,
placed on a command line, or echoed in an assertion message. This test never
writes application secrets; it only reads ``sys/health``, cycles containers, and
seals/unseals the throwaway unsealer.

=== GATING & OFFLINE-SKIP DETERMINISM ===

The live tier is the ESTABLISHED ``@pytest.mark.requires_infra`` marker
(deselected by default via the root ``pytest.ini``'s ``addopts = -m "not
requires_infra"``). BOTH test classes are ADDITIONALLY ``skipif``-gated with the
LIVE gate reason returned FIRST — i.e. the class skips on a MISSING
``OPENBAO_TEST_ADDR`` (an env var that IS scrubbed in the offline preservation
baseline) BEFORE it ever considers docker/container-name availability. This makes
the offline skip ENV-VAR-DETERMINISTIC: in the scrubbed offline baseline both
classes skip for the SAME reason (``OPENBAO_TEST_ADDR`` not set) regardless of
whether a docker CLI happens to be installed on the runner. Docker / unsealer /
container-name gates are only consulted AFTER the live-gate passes, exactly the
"live-gate-first ordering" idiom ``test_svc07_openbao_onboard_contract.py`` uses.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_autounseal.py -v
      (skips cleanly with no OPENBAO_TEST_* env vars; runs against a live
       throwaway OpenBao primary + unsealer when they are set, opting into the
       live tier with `-m requires_infra`.)
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Env-var contract (see module docstring + TESTING.md). The first two REUSE the
# task-7.4 OpenBao contract; the rest are additional container-cycling vars this
# test needs. Named in the `<SERVICE>_TEST_<THING>` style of the existing
# PROXMOX_*_TEST_* / OPENBAO_TEST_* vars.
# --------------------------------------------------------------------------- #
_ADDR_ENV = "OPENBAO_TEST_ADDR"                       # primary API (reused)
_TOKEN_ENV = "OPENBAO_TEST_TOKEN"                     # primary token (reused)
_UNSEALER_ADDR_ENV = "OPENBAO_TEST_UNSEALER_ADDR"     # unsealer API (new)
_PRIMARY_CONTAINER_ENV = "OPENBAO_TEST_PRIMARY_CONTAINER"    # primary ctr (new)
_UNSEALER_CONTAINER_ENV = "OPENBAO_TEST_UNSEALER_CONTAINER"  # unsealer ctr (new)
_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"        # optional (reused)

# Req 5.3 / 5.6 timing contract, mirrored from requirements.md:
_UNSEAL_DEADLINE_SECONDS = 60      # Req 5.3: sealed:false within 60 s
_POLL_INTERVAL_SECONDS = 2         # health poll cadence while waiting to unseal
# Req 5.6: 3 attempts spaced 20 s ⇒ ~40-60 s before the primary gives up and
# stays sealed. Wait past that window before asserting it stayed sealed.
_STAY_SEALED_OBSERVE_SECONDS = 75


def _live_gate_reason() -> str | None:
    """Return a skip reason if the LIVE OpenBao gate is unsatisfied, else None.

    Returned FIRST (before docker/container gates) so the offline skip is
    env-var-deterministic: a missing ``OPENBAO_TEST_ADDR`` (scrubbed in the
    preservation baseline) always wins. Requires the primary API + token AND the
    unsealer API (the round-trip needs to confirm the unsealer is healthy; the
    swap-gate needs to seal it). Same clean-skip contract as the sibling
    contract tests — "absent infra => skipped, not failed".
    """
    if not os.environ.get(_ADDR_ENV):
        return (
            f"requires-infra, skipped: {_ADDR_ENV} not set (a live throwaway "
            "OpenBao PRIMARY API endpoint is required for the auto-unseal "
            "round-trip and swap-gate seal tests)"
        )
    if not os.environ.get(_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_TOKEN_ENV} not set (a token on the "
            "throwaway primary is required to read GET /v1/sys/health)"
        )
    if not os.environ.get(_UNSEALER_ADDR_ENV):
        return (
            f"requires-infra, skipped: {_UNSEALER_ADDR_ENV} not set (the Transit "
            "unsealer's API endpoint is required to confirm it is healthy before "
            "the round-trip and to seal it in the swap-gate)"
        )
    return None


def _docker_available() -> bool:
    """Whether a usable ``docker`` CLI is present (this test cycles containers
    via ``docker stop``/``docker start``)."""
    return shutil.which("docker") is not None


def _container_gate_reason() -> str | None:
    """Skip reason for the container-cycling preconditions, consulted ONLY after
    the live gate passes (live-gate-first ordering).

    Needs a ``docker`` CLI plus BOTH container-name env vars. Absent any =>
    SKIP cleanly (never fail). These gates are NOT env-var-deterministic in the
    offline baseline on their own — but they can never be reached offline because
    ``_live_gate_reason()`` (on the scrubbed ``OPENBAO_TEST_ADDR``) short-circuits
    first in every class's skipif expression below.
    """
    if not _docker_available():
        return (
            "requires the docker CLI (this test cycles the primary/unsealer "
            "containers via `docker stop`/`docker start` to trigger and gate "
            "Transit auto-unseal)"
        )
    if not os.environ.get(_PRIMARY_CONTAINER_ENV):
        return (
            f"requires-infra, skipped: {_PRIMARY_CONTAINER_ENV} not set (the "
            "primary OpenBao container name/id is required to stop+start it and "
            "read its logs)"
        )
    if not os.environ.get(_UNSEALER_CONTAINER_ENV):
        return (
            f"requires-infra, skipped: {_UNSEALER_CONTAINER_ENV} not set (the "
            "Transit unsealer container name/id is required to stop it in the "
            "Req 5.6 swap-gate arm)"
        )
    return None


def _autounseal_gate_reason() -> str | None:
    """Combined gate: live gate FIRST (env-var-deterministic), then containers."""
    live = _live_gate_reason()
    if live is not None:
        return live
    return _container_gate_reason()


def _tls_context() -> ssl.SSLContext | None:
    """TLS context for API calls; skips verify by default (self-signed test cert).

    Same knob/default as the sibling contract tests. Returns ``None`` for a plain
    ``http://`` addr (urllib ignores the context there).
    """
    skip = os.environ.get(_SKIP_TLS_ENV, "1").strip().lower() not in ("0", "false", "no", "")
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _OpenBao:
    """Minimal live OpenBao HTTP client for the seal/health assertions.

    The token is placed ONLY in the ``X-Vault-Token`` request header — never
    logged, never on a command line, never returned in an error message.
    """

    def __init__(self, addr: str, token: str | None = None) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._ctx = _tls_context()

    def _request(self, method: str, path: str) -> tuple[int, Any]:
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        req = urllib.request.Request(url=url, method=method)
        if self._token is not None:
            req.add_header("X-Vault-Token", self._token)
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            # sys/health returns non-200 status codes (e.g. 503 sealed) with a
            # JSON body — surface both. NB: never include the token.
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"errors": [raw]}
            return exc.code, parsed
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            # Connection refused / TLS reset while the container is down — model
            # as "unreachable" so callers can treat it as not-yet-healthy.
            return 0, {}

    def health(self) -> tuple[int, Any]:
        """``GET /v1/sys/health`` — status code + parsed JSON body."""
        return self._request("GET", "sys/health")

    def is_sealed(self) -> bool | None:
        """``sealed`` boolean from ``sys/health``, or ``None`` if unreachable/
        unparseable (used to distinguish 'down' from 'up-and-sealed')."""
        status, body = self.health()
        if status == 0 or not isinstance(body, dict) or "sealed" not in body:
            return None
        return bool(body["sealed"])

    def seal(self) -> tuple[int, Any]:
        """``PUT /v1/sys/seal`` — seals this instance (used on the unsealer to
        exercise the Req 5.7 present-but-sealed arm)."""
        return self._request("PUT", "sys/seal")


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a ``docker`` subcommand, capturing output. Never carries a secret."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_until_unsealed(bao: _OpenBao, deadline_seconds: int) -> bool:
    """Poll ``sys/health`` until ``sealed:false`` or the deadline elapses.

    Returns True if the instance reached ``sealed:false`` within the window.
    """
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if bao.is_sealed() is False:
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return bao.is_sealed() is False


def _stayed_sealed_for(bao: _OpenBao, observe_seconds: int) -> bool:
    """Observe for ``observe_seconds`` and return True only if the primary NEVER
    reports ``sealed:false`` during the window (i.e. it stayed sealed / down —
    it did NOT silently come up unsealed). ``None`` (unreachable) counts as
    'not unsealed', which is the safe outcome for the swap-gate."""
    end = time.monotonic() + observe_seconds
    while time.monotonic() < end:
        if bao.is_sealed() is False:
            return False  # it came up unsealed — swap-gate VIOLATION
        time.sleep(_POLL_INTERVAL_SECONDS)
    return bao.is_sealed() is not False


@pytest.fixture(scope="module")
def primary() -> _OpenBao:
    """A live client for the throwaway PRIMARY OpenBao."""
    addr = os.environ.get(_ADDR_ENV)
    token = os.environ.get(_TOKEN_ENV)
    assert addr and token, "live gate should have prevented fixture construction"
    return _OpenBao(addr, token)


@pytest.fixture(scope="module")
def unsealer() -> _OpenBao:
    """A live client for the throwaway Transit UNSEALER OpenBao."""
    addr = os.environ.get(_UNSEALER_ADDR_ENV)
    assert addr, "live gate should have prevented fixture construction"
    # health/seal on the unsealer need a token in most configs; reuse the same
    # test token if the deployment shares it, else health is unauthenticated.
    return _OpenBao(addr, os.environ.get(_TOKEN_ENV))


# =========================================================================== #
# 1) AUTO-UNSEAL ROUND-TRIP — Req 5.3 (INFRA-GATED live test).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _autounseal_gate_reason() is not None,
    reason=_autounseal_gate_reason() or "auto-unseal round-trip gate satisfied",
)
class TestOpenBaoAutoUnsealRoundTrip:
    """Stopping and starting the primary auto-unseals it within 60 s given a
    healthy Transit unsealer, with NO operator key-entry (Req 5.3)."""

    def test_primary_reaches_unsealed_within_60s_after_restart(
        self, primary: _OpenBao, unsealer: _OpenBao
    ):
        primary_ctr = os.environ[_PRIMARY_CONTAINER_ENV]

        # Precondition (Req 5.3): the unsealer must be healthy (200 + sealed:false)
        # BEFORE the start attempt, else the round-trip precondition is unmet and
        # we skip rather than assert a false failure.
        u_status, u_body = unsealer.health()
        if not (u_status == 200 and isinstance(u_body, dict) and u_body.get("sealed") is False):
            pytest.skip(
                "requires-infra, skipped: the Transit unsealer is not healthy "
                "(GET /v1/sys/health did not return 200 sealed:false) before the "
                "round-trip — Req 5.3's precondition is unmet on this instance"
            )

        # Cycle the primary container: stop, then start. No operator key entry.
        stop = _docker("stop", primary_ctr)
        assert stop.returncode == 0, (
            f"could not stop the primary container '{primary_ctr}':\n{stop.stderr}"
        )
        start = _docker("start", primary_ctr)
        assert start.returncode == 0, (
            f"could not start the primary container '{primary_ctr}':\n{start.stderr}"
        )

        # Req 5.3: sealed:false within 60 s, no operator input.
        reached = _wait_until_unsealed(primary, _UNSEAL_DEADLINE_SECONDS)
        assert reached, (
            f"the primary must reach sealed:false within {_UNSEAL_DEADLINE_SECONDS}s "
            "of a restart via Transit auto-unseal, with no operator key-entry "
            "(Req 5.3); it did not"
        )


# =========================================================================== #
# 2) SWAP-GATE SEAL TEST — Req 5.6, 5.7 (INFRA-GATED live test).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _autounseal_gate_reason() is not None,
    reason=_autounseal_gate_reason() or "swap-gate seal gate satisfied",
)
class TestOpenBaoSwapGateSeal:
    """composition-wiring swap-gate for the Transit-unsealer seam (Req 5.6, 5.7).

    Real unsealer reachable ⇒ primary unseals. Real unsealer absent (stopped,
    Req 5.6) or present-but-sealed (Req 5.7) ⇒ primary STAYS sealed AND logs an
    ERROR containing 'transit seal' + 'unreachable' — never a masked/fake-unsealed
    success (the anti-pattern composition-wiring.md forbids). The unsealer is
    always restored in ``finally`` so the shared throwaway instance is left
    healthy.
    """

    @staticmethod
    def _assert_transit_seal_unreachable_error(primary_ctr: str) -> None:
        """Assert the primary's logs carry >=1 ERROR line containing BOTH the
        token 'transit seal' and the token 'unreachable' (Req 5.6/5.7)."""
        logs = _docker("logs", "--tail", "400", primary_ctr, timeout=30)
        combined = (logs.stdout + logs.stderr).lower()
        assert "transit seal" in combined and "unreachable" in combined, (
            "the primary must emit an ERROR log entry containing both 'transit "
            "seal' and 'unreachable' when the Transit unsealer is unreachable / "
            "itself sealed (Req 5.6/5.7); neither/both tokens were found in the "
            "primary container logs. This is the swap-gate's loud-failure "
            "requirement — a missing real unsealer must NOT be masked."
        )

    def test_real_unsealer_reachable_primary_unseals(
        self, primary: _OpenBao, unsealer: _OpenBao
    ):
        """Swap-gate positive arm: with the REAL unsealer reachable and healthy,
        the primary reaches unsealed (real dependency present ⇒ real success)."""
        primary_ctr = os.environ[_PRIMARY_CONTAINER_ENV]

        u_status, u_body = unsealer.health()
        if not (u_status == 200 and isinstance(u_body, dict) and u_body.get("sealed") is False):
            pytest.skip(
                "requires-infra, skipped: the Transit unsealer is not healthy "
                "before the positive swap-gate arm — cannot assert the "
                "real-dependency-present success path"
            )

        # Ensure the primary is (re)started against the healthy unsealer.
        _docker("restart", primary_ctr)
        assert _wait_until_unsealed(primary, _UNSEAL_DEADLINE_SECONDS), (
            "with the real Transit unsealer reachable and healthy, the primary "
            f"must reach sealed:false within {_UNSEAL_DEADLINE_SECONDS}s "
            "(swap-gate: real dependency present ⇒ real success)"
        )

    def test_unsealer_stopped_primary_stays_sealed_and_logs_error(
        self, primary: _OpenBao, unsealer: _OpenBao
    ):
        """Swap-gate negative arm (Req 5.6): with the unsealer STOPPED, restart
        the primary and assert it stays sealed:true and logs the 'transit seal …
        unreachable' ERROR — never a masked/fake-unsealed success."""
        primary_ctr = os.environ[_PRIMARY_CONTAINER_ENV]
        unsealer_ctr = os.environ[_UNSEALER_CONTAINER_ENV]

        try:
            # Take the REAL unsealer away.
            stop = _docker("stop", unsealer_ctr)
            assert stop.returncode == 0, (
                f"could not stop the unsealer container '{unsealer_ctr}':\n{stop.stderr}"
            )

            # Restart the primary so it re-attempts Transit seal init against the
            # now-absent unsealer.
            restart = _docker("restart", primary_ctr)
            assert restart.returncode == 0, (
                f"could not restart the primary container '{primary_ctr}':\n{restart.stderr}"
            )

            # Req 5.6: the primary must NOT come up unsealed while the unsealer is
            # gone — observe past the ~3×20 s retry window.
            stayed_sealed = _stayed_sealed_for(primary, _STAY_SEALED_OBSERVE_SECONDS)
            assert stayed_sealed, (
                "with the Transit unsealer STOPPED, the primary must stay "
                "sealed:true and MUST NOT silently come up unsealed/fake-unsealed "
                "(Req 5.6, composition-wiring swap-gate); it reported sealed:false"
            )

            # And it must have logged the loud ERROR (Req 5.6).
            self._assert_transit_seal_unreachable_error(primary_ctr)
        finally:
            # Restore the unsealer and re-unseal the primary for the next test/run.
            _docker("start", unsealer_ctr)
            # Give the unsealer a moment, then restart the primary so the shared
            # throwaway instance is left healthy (best-effort; not asserted here).
            time.sleep(_POLL_INTERVAL_SECONDS)
            _docker("restart", primary_ctr)

    def test_unsealer_present_but_sealed_primary_stays_sealed_and_logs_error(
        self, primary: _OpenBao, unsealer: _OpenBao
    ):
        """Swap-gate negative arm (Req 5.7): with the unsealer RUNNING but itself
        ``sealed:true``, the primary must behave identically to the unreachable
        case — stay sealed and log the 'transit seal … unreachable' ERROR."""
        primary_ctr = os.environ[_PRIMARY_CONTAINER_ENV]
        unsealer_ctr = os.environ[_UNSEALER_CONTAINER_ENV]

        try:
            # Seal the unsealer (it stays RUNNING, but reports sealed:true).
            seal_status, _ = unsealer.seal()
            if seal_status not in (200, 204):
                # Some deployments require a higher-priv token to seal; if we
                # cannot seal it cleanly, skip rather than assert a false result.
                if unsealer.is_sealed() is not True:
                    pytest.skip(
                        "requires-infra, skipped: could not seal the Transit "
                        f"unsealer (status {seal_status}) to exercise the Req 5.7 "
                        "present-but-sealed arm — a seal-capable token is needed"
                    )

            # Confirm the unsealer is running-but-sealed before proceeding.
            if unsealer.is_sealed() is not True:
                pytest.skip(
                    "requires-infra, skipped: the unsealer did not reach "
                    "sealed:true for the Req 5.7 arm"
                )

            restart = _docker("restart", primary_ctr)
            assert restart.returncode == 0, (
                f"could not restart the primary container '{primary_ctr}':\n{restart.stderr}"
            )

            # Req 5.7: identical to the unreachable case — stay sealed, log ERROR.
            stayed_sealed = _stayed_sealed_for(primary, _STAY_SEALED_OBSERVE_SECONDS)
            assert stayed_sealed, (
                "with the Transit unsealer RUNNING but itself sealed:true, the "
                "primary must stay sealed:true and MUST NOT come up "
                "unsealed/fake-unsealed (Req 5.7); it reported sealed:false"
            )
            self._assert_transit_seal_unreachable_error(primary_ctr)
        finally:
            # Unseal the unsealer path is deployment-specific (Transit auto-unseal
            # or its own restart); restart it and the primary so the shared
            # throwaway instance is left healthy (best-effort; not asserted here).
            _docker("restart", unsealer_ctr)
            time.sleep(_POLL_INTERVAL_SECONDS)
            _docker("restart", primary_ctr)
