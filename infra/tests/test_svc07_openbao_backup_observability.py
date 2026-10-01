"""Live BACKUP/RESTORE + OBSERVABILITY integration tests for SVC-07 (OpenBao).

Task 12.4 (spec: svc-07-secrets-manager) — requirements.md Requirements 10.3,
10.8, 11.1, 11.4; design.md "Testing Strategy §3" (the "Backup/restore" and
"Observability" bullets).

=== WHAT THIS TEST IS (and is NOT) ===

These are INTEGRATION tests in the ``testing-strategy.md`` §3 sense: they drive
the REAL backup/DR and observability seams against a live throwaway OpenBao —
never a mock, never a rendered-artifact stand-in. The offline structural suites
already pin the *source* shape of both seams (``test_svc07_openbao_backup.py``
renders the task-11.1 snapshot script and asserts its rc==0/non-empty gate +
PBS hand-off; ``test_svc07_openbao_prometheus_scrape.py`` renders the task-10.1
scrape config and asserts the ``job=openbao`` shape + the documented sealed-metric
guarantee). THIS file closes the loop by asserting what a live OpenBao actually
PRODUCES and EXPOSES:

  * Req 11.1 — running the snapshot flow produces a **non-empty** snapshot file
    (and, when a PBS test env is present, lands it in PBS).
  * Req 11.4 — a ``bao operator raft snapshot restore`` into a fresh/test
    instance leaves it **unsealed** with a KV secret **readable** (asserts
    success within the 60-min RTO — the test asserts success, it does not wait
    the literal 60 minutes).
  * Req 10.3 — Prometheus (when a test Prometheus is reachable) ingests the
    ``job=openbao`` series **within two scrape intervals**.
  * Req 10.8 — ``openbao_core_unsealed`` is exposed on the metrics endpoint,
    reading ``0`` while the instance is **sealed** (the sealed-metric guarantee,
    FD.7).

Because they need live infra they are gated with the ESTABLISHED
``@pytest.mark.requires_infra`` marker (deselected by default via the root
``pytest.ini``'s ``addopts = -m "not requires_infra"``) and, additionally,
``skipif``-gated on the SAME OpenBao env-var contract task 7.4's
``test_svc07_openbao_engine_contract.py`` uses, so they SKIP CLEANLY when no
live OpenBao is reachable — never fail for lack of infra (documentation-testing
steering: "absent infra => skipped, not failed"). No new gate is invented: the
marker + ``skipif``-on-``OPENBAO_TEST_ADDR``/``OPENBAO_TEST_TOKEN`` idiom is the
SAME one the sibling engine-contract test (task 7.4) uses; the live-gate reason
is evaluated FIRST and is an env-var-deterministic offline skip.

Individual sub-capabilities that need MORE than the base OpenBao gate (a Docker
CLI + container name for the docker-exec snapshot/restore drive; a reachable
test Prometheus; the ability to seal an instance) gate CLEANLY and INDIVIDUALLY
at method scope with ``pytest.skip(...)`` so the base-gated methods still run.

=== ENV-VAR CONTRACT (documented here + in TESTING.md) ===

Base gate (class-level skipif — SAME as task 7.4):

  * ``OPENBAO_TEST_ADDR``  — the live OpenBao API base URL (e.g.
    ``https://127.0.0.1:8200``). A THROWAWAY/ephemeral OpenBao only — this test
    WRITES a disposable ``btest-*`` KV secret and (optionally) drives a snapshot.
  * ``OPENBAO_TEST_TOKEN`` — a token with enough privilege to write+read a KV
    secret under ``secret/`` and call ``sys/storage/raft/snapshot`` (i.e. a
    ``platform-admin``-scoped or root test token on the throwaway instance).

Optional (each unlocks one more assertion; absent => that sub-assertion skips
cleanly, the base-gated ones still run):

  * ``OPENBAO_TEST_SKIP_TLS_VERIFY`` — ``1``/``true`` (default) skips TLS
    verification for the ephemeral instance's self-signed cert; ``0`` enforces.
  * ``OPENBAO_TEST_CONTAINER`` — the Docker container NAME (or id) the live
    primary OpenBao runs in, used to drive ``bao operator raft snapshot save``
    via ``docker exec`` (mirrors the task-11.1 script's docker-exec convention).
    Requires a ``docker`` CLI on PATH. Absent (or no docker) => the snapshot
    save/restore drive skips cleanly.
  * ``OPENBAO_TEST_RESTORE_CONTAINER`` — the Docker container NAME of a
    FRESH/second throwaway OpenBao to restore INTO (Req 11.4). When unset, the
    restore is performed back into ``OPENBAO_TEST_CONTAINER`` only if
    ``OPENBAO_TEST_ALLOW_SELF_RESTORE`` is truthy (a restore mutates the
    instance — never do it to a shared/production instance implicitly); else
    the restore-half of the round-trip skips cleanly.
  * ``OPENBAO_TEST_ALLOW_SELF_RESTORE`` — opt-in (``1``/``true``) to restore
    back into the same ``OPENBAO_TEST_CONTAINER`` when no dedicated restore
    container is given. Off by default (a self-restore is destructive).
  * ``OPENBAO_TEST_PROM_URL`` — a reachable test Prometheus base URL (e.g.
    ``http://127.0.0.1:9090``). When set, the Req 10.3 ingestion assertion runs
    against its ``/api/v1/query`` API; absent => that assertion skips cleanly.
  * ``OPENBAO_TEST_PROM_SCRAPE_INTERVAL`` — the Prometheus scrape interval in
    seconds (default ``30``, matching the task-10.1 ``<= 30 s`` job). "Within
    two scrape intervals" is derived from this.
  * ``OPENBAO_TEST_SEALED_ADDR`` — the API base URL of an ALREADY-SEALED
    throwaway OpenBao whose ``/v1/sys/metrics`` still answers, used to assert
    ``openbao_core_unsealed = 0`` while sealed (Req 10.8) WITHOUT sealing a
    shared instance. When unset, the test attempts to seal via
    ``OPENBAO_TEST_ALLOW_SEAL`` (below); if neither is available it skips
    cleanly rather than sealing a shared instance.
  * ``OPENBAO_TEST_ALLOW_SEAL`` — opt-in (``1``/``true``) to ``sys/seal`` the
    base ``OPENBAO_TEST_ADDR`` instance to read the sealed metric, then leave it
    sealed (unsealing needs the Transit unsealer / operator). Off by default.

SECURITY (security-standards.md — this IS the secrets manager): the token is
read from the environment and threaded ONLY into request headers / the
``docker exec`` child-process env (``BAO_TOKEN``); it is NEVER logged, placed on
a command line, or echoed in an assertion message. Every KV secret this test
writes uses a disposable ``btest-<uuid>`` prefix and is torn down in a
``finally`` block, so a re-run never collides and the instance is left clean.

=== WHY HTTP + docker exec, NOT the local ``bao`` CLI ===

The metric/KV assertions read the live HTTP API directly with ``urllib``
(stdlib) — dependency-free, matching the rest of ``infra/tests/`` and the task
7.4 client. The snapshot save/restore is driven via ``docker exec`` against the
server's OWN ``bao`` binary/version (exactly the task-11.1 script's convention),
so it uses the same code path production will, and needs no ``bao`` binary on
the test runner.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_backup_observability.py -v
      (skips cleanly with no OPENBAO_TEST_* env vars; runs against a live
       throwaway OpenBao when they are set, opting into the live tier with
       `-m requires_infra`.)
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Env-var contract (see module docstring + TESTING.md). Base gate is IDENTICAL
# to task 7.4's engine-contract test; the rest are optional per-assertion knobs.
# --------------------------------------------------------------------------- #
_ADDR_ENV = "OPENBAO_TEST_ADDR"
_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"
_CONTAINER_ENV = "OPENBAO_TEST_CONTAINER"
_RESTORE_CONTAINER_ENV = "OPENBAO_TEST_RESTORE_CONTAINER"
_ALLOW_SELF_RESTORE_ENV = "OPENBAO_TEST_ALLOW_SELF_RESTORE"
_PROM_URL_ENV = "OPENBAO_TEST_PROM_URL"
_PROM_INTERVAL_ENV = "OPENBAO_TEST_PROM_SCRAPE_INTERVAL"
_SEALED_ADDR_ENV = "OPENBAO_TEST_SEALED_ADDR"
_ALLOW_SEAL_ENV = "OPENBAO_TEST_ALLOW_SEAL"

# Contract values mirrored from the role defaults / the task brief.
_KV_MOUNT = "secret"                 # openbao_kv_mount
_DEFAULT_SCRAPE_INTERVAL_S = 30      # openbao_prometheus_scrape_interval "30s"
_SEALED_METRIC = "openbao_core_unsealed"   # Req 10.8 / FD.7
# The container path the snapshot save writes to (mirrors
# openbao_backup_container_tmp; kept independent so a re-run is clean).
_CONTAINER_SNAPSHOT_PATH = "/openbao/data/.btest-snapshot.snap"


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _live_gate_reason() -> str | None:
    """Return a skip reason if the base live-OpenBao gate is not satisfied.

    Requires BOTH ``OPENBAO_TEST_ADDR`` and ``OPENBAO_TEST_TOKEN`` — the SAME
    base gate task 7.4's engine-contract test uses. Env-var-deterministic:
    offline (neither set), the whole class SKIPS cleanly, never fails
    (documentation-testing steering: "absent infra => skipped, not failed").
    Evaluated FIRST (module import time) so the offline skip is deterministic.
    """
    if not os.environ.get(_ADDR_ENV):
        return (
            f"requires-infra, skipped: {_ADDR_ENV} not set (a live throwaway "
            "OpenBao API endpoint is required for the SVC-07 backup/restore + "
            "observability integration tests)"
        )
    if not os.environ.get(_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_TOKEN_ENV} not set (a platform-admin / "
            "root-scoped token on the throwaway OpenBao is required to write a "
            "KV secret and drive a Raft snapshot)"
        )
    return None


def _tls_context() -> ssl.SSLContext | None:
    """TLS context for the API calls.

    Defaults to NOT verifying the cert (the ephemeral test instance typically
    uses a self-signed cert); set ``OPENBAO_TEST_SKIP_TLS_VERIFY=0`` to enforce.
    Returns ``None`` for a plain-``http://`` addr (urllib ignores the context).
    """
    skip = os.environ.get(_SKIP_TLS_ENV, "1").strip().lower() not in ("0", "false", "no", "")
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _OpenBao:
    """Minimal live OpenBao HTTP client for the backup/observability assertions.

    Wraps ``urllib`` GET/POST against the OpenBao API. The token is placed ONLY
    in the ``X-Vault-Token`` request header — never logged, never on a command
    line, never returned in an error message (Req: secrets discipline). The
    metrics endpoint is fetched separately (it may be unauthenticated per task
    10.1 ``unauthenticated_metrics_access = true``).
    """

    def __init__(self, addr: str, token: str | None = None) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._ctx = _tls_context()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, method=method, data=data)
        if self._token:
            req.add_header("X-Vault-Token", self._token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"errors": [raw]}
            # NB: never include the token in the surfaced tuple.
            return exc.code, parsed

    def get(self, path: str) -> tuple[int, Any]:
        return self._request("GET", path)

    def post(self, path: str, body: dict) -> tuple[int, Any]:
        return self._request("POST", path, body)

    def health(self) -> tuple[int, Any]:
        """``GET sys/health`` — 200 = initialized+unsealed+active, 503 = sealed."""
        return self.get("sys/health")

    def is_unsealed(self) -> bool:
        """True iff ``sys/seal-status`` reports ``sealed: false``."""
        status, data = self.get("sys/seal-status")
        if status == 200 and isinstance(data, dict):
            payload = data.get("data", data)
            return payload.get("sealed") is False
        return False

    def fetch_metrics_text(self) -> tuple[int, str]:
        """Fetch ``/v1/sys/metrics?format=prometheus`` as raw exposition text.

        Uses the token if present (harmless when unauthenticated access is on);
        returns (status, text). Never raises for an HTTP error status.
        """
        url = f"{self._addr}/v1/sys/metrics?format=prometheus"
        req = urllib.request.Request(url=url, method="GET")
        if self._token:
            req.add_header("X-Vault-Token", self._token)
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")


def _parse_prometheus_metric(text: str, metric_name: str) -> float | None:
    """Return the first sample value for ``metric_name`` in exposition text.

    Skips ``# HELP``/``# TYPE`` comment lines; matches a line whose metric name
    (before any ``{labels}`` and the trailing value) equals ``metric_name``.
    Returns ``None`` when the series is absent.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # A sample line is: `name{labels} value [timestamp]` or `name value`.
        head = line.split("{", 1)[0].split(None, 1)[0]
        if head != metric_name:
            continue
        # Value is the last whitespace-separated token (drop optional timestamp).
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            return float(parts[-1])
        except ValueError:
            try:
                return float(parts[-2])
            except (ValueError, IndexError):
                continue
    return None


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture(scope="module")
def bao() -> _OpenBao:
    """A live-OpenBao client built from the base env-var contract.

    The class-level skipif prevents this from ever being constructed without the
    env vars present, but assert here too as a belt-and-braces guard.
    """
    addr = os.environ.get(_ADDR_ENV)
    token = os.environ.get(_TOKEN_ENV)
    assert addr and token, "live gate should have prevented fixture construction"
    return _OpenBao(addr, token)


@pytest.fixture()
def btest_prefix() -> str:
    """A disposable, collision-proof resource prefix for one test."""
    return f"btest-{uuid.uuid4().hex[:12]}"


# =========================================================================== #
# The integration suite — INFRA-GATED (requires_infra + skipif on the SAME
# OPENBAO_TEST_ADDR/OPENBAO_TEST_TOKEN base gate as task 7.4).
#
# The class-level skipif's reason is evaluated FIRST (module import time), so the
# offline deterministic skip is on OPENBAO_TEST_ADDR/OPENBAO_TEST_TOKEN. Methods
# that need MORE (docker+container, a test Prometheus, the ability to seal)
# gate CLEANLY and INDIVIDUALLY inside the method so the base-gated ones run.
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_gate_reason() is not None,
    reason=_live_gate_reason() or "live OpenBao backup/observability gate satisfied",
)
class TestOpenBaoBackupAndObservability:
    """Backup/restore + observability integration assertions against a live
    throwaway OpenBao (Req 10.3, 10.8, 11.1, 11.4).

    Each test writes/reads via the SAME HTTP API + docker-exec conventions the
    task-11.1 snapshot script and task-10.1 scrape config use, and tears down
    any disposable ``btest-*`` KV secret in a ``finally`` block so the throwaway
    instance is left clean and a re-run never collides.
    """

    # ------------------------------------------------------------------ #
    # Req 11.1 — the snapshot flow produces a NON-EMPTY file (docker exec).
    # ------------------------------------------------------------------ #
    def test_raft_snapshot_save_produces_nonempty_file(
        self, bao: _OpenBao, btest_prefix: str
    ):
        """``bao operator raft snapshot save`` via docker exec produces a
        non-empty snapshot file (Req 11.1).

        Drives the SAME docker-exec ``operator raft snapshot save`` the task-11.1
        script runs, then asserts the in-container file exists and has size > 0
        (the exact rc==0 AND non-empty gate the script enforces). When the PBS
        test env is present, asserts a copy lands in PBS too. Gates cleanly if
        docker or the container name is absent.
        """
        container = os.environ.get(_CONTAINER_ENV)
        if not container:
            pytest.skip(
                f"requires-infra, skipped: {_CONTAINER_ENV} not set — the "
                "snapshot save is driven via `docker exec` against the live "
                "OpenBao container (mirrors the task-11.1 script), which needs "
                "the container name."
            )
        if not _docker_available():
            pytest.skip(
                "requires-infra, skipped: no `docker` CLI on PATH — the snapshot "
                "save is driven via `docker exec` against the OpenBao container."
            )

        token = os.environ.get(_TOKEN_ENV)
        assert token, "base gate guarantees the token is present"
        snap_path = _CONTAINER_SNAPSHOT_PATH

        try:
            # Take the snapshot online (Req 11.6 — no stop/seal), exactly as the
            # task-11.1 script does. The token is passed ONLY via the child-proc
            # env (BAO_TOKEN), never on the command line / never logged.
            rc = self._docker_snapshot_save(container, token, snap_path)
            assert rc == 0, (
                "`bao operator raft snapshot save` must exit 0 (Req 11.1); "
                f"got rc={rc}"
            )

            # Assert the snapshot file is NON-EMPTY inside the container (the
            # script's `[ -s <file> ]` gate). `stat -c %s` prints the byte size.
            size = self._docker_file_size(container, snap_path)
            assert size is not None and size > 0, (
                "the produced snapshot file must be non-empty (Req 11.1); "
                f"stat reported size={size!r}"
            )

            # Optional: if a PBS test env is present, assert a copy lands in PBS.
            self._assert_pbs_landing_if_configured(container, snap_path)
        finally:
            # Remove the in-container snapshot temp so a re-run is clean.
            self._docker_rm(container, snap_path)

    # ------------------------------------------------------------------ #
    # Req 11.4 — restore round-trip: restored instance unsealed + KV readable.
    # ------------------------------------------------------------------ #
    def test_raft_snapshot_restore_leaves_instance_unsealed_and_kv_readable(
        self, bao: _OpenBao, btest_prefix: str
    ):
        """A ``raft snapshot restore`` leaves the restored instance unsealed with
        a KV secret readable within the 60-min RTO (Req 11.4).

        Writes a disposable KV secret, snapshots the source, restores into a
        fresh/test instance, and asserts (a) the restored instance is unsealed
        and (b) the KV secret is readable there. The test asserts SUCCESS — it
        does not wait the literal 60 minutes; reaching operational state at all
        is the RTO evidence. Gates cleanly when docker / a restore target is
        absent, or when a self-restore is not explicitly opted into.
        """
        container = os.environ.get(_CONTAINER_ENV)
        if not container or not _docker_available():
            pytest.skip(
                f"requires-infra, skipped: {_CONTAINER_ENV} + a `docker` CLI are "
                "required to drive `raft snapshot save`/`restore` via docker exec."
            )

        restore_container = os.environ.get(_RESTORE_CONTAINER_ENV)
        restore_addr = os.environ.get(_ADDR_ENV)  # default: same-instance addr
        self_restore_ok = _is_truthy(os.environ.get(_ALLOW_SELF_RESTORE_ENV))
        if not restore_container:
            if not self_restore_ok:
                pytest.skip(
                    "requires-infra, skipped: no "
                    f"{_RESTORE_CONTAINER_ENV} (a FRESH throwaway OpenBao to "
                    "restore INTO) and self-restore not opted into. A restore "
                    "MUTATES the target instance, so it is never done to the "
                    f"shared base instance implicitly — set {_ALLOW_SELF_RESTORE_ENV}"
                    "=1 to restore back into the base container, or point "
                    f"{_RESTORE_CONTAINER_ENV} at a dedicated fresh instance."
                )
            restore_container = container  # opted-in destructive self-restore
        else:
            # A dedicated restore target should expose its own API for the
            # unseal/read-back check; fall back to the base addr if not given.
            restore_addr = os.environ.get("OPENBAO_TEST_RESTORE_ADDR", restore_addr)

        token = os.environ.get(_TOKEN_ENV)
        assert token
        secret_path = f"{btest_prefix}/dr-canary"
        canary_key, canary_val = "canary", btest_prefix
        snap_path = _CONTAINER_SNAPSHOT_PATH

        try:
            # 1) Write a disposable KV v2 secret we expect to survive the restore.
            status, resp = bao.post(
                f"{_KV_MOUNT}/data/{secret_path}",
                {"data": {canary_key: canary_val}},
            )
            assert status in (200, 204), (
                f"KV write should succeed before snapshot (Req 11.4); got {status}: {resp}"
            )

            # 2) Snapshot the source instance online.
            rc = self._docker_snapshot_save(container, token, snap_path)
            assert rc == 0, f"snapshot save must exit 0 before restore; rc={rc}"
            size = self._docker_file_size(container, snap_path)
            assert size and size > 0, "snapshot must be non-empty before restore"

            # 3) If restoring into a SEPARATE container, copy the snapshot over.
            if restore_container != container:
                self._docker_copy_between(container, snap_path, restore_container, snap_path)

            # 4) Restore into the target instance (docker exec, same bao binary).
            rc = self._docker_snapshot_restore(restore_container, token, snap_path)
            assert rc == 0, (
                "`bao operator raft snapshot restore` must exit 0 (Req 11.4); "
                f"got rc={rc}"
            )

            # 5) Assert the restored instance reaches operational state: unsealed
            #    and the KV canary readable. Transit auto-unseal (Req 5.3) brings
            #    it back with no operator input; poll briefly (NOT 60 min — we
            #    assert it reaches success quickly; the RTO is the ceiling).
            restored = _OpenBao(restore_addr, token)
            deadline = time.monotonic() + 120  # generous ceiling for a test box
            unsealed = False
            while time.monotonic() < deadline:
                if restored.is_unsealed():
                    unsealed = True
                    break
                time.sleep(2)
            assert unsealed, (
                "the restored instance must reach `sealed: false` within the RTO "
                "(Req 11.4) — Transit auto-unseal brings it back with no operator "
                "input (Req 5.3)"
            )

            status, data = restored.get(f"{_KV_MOUNT}/data/{secret_path}")
            assert status == 200, (
                f"the KV canary must be readable on the restored instance "
                f"(Req 11.4); got {status}: {data}"
            )
            read_back = (((data or {}).get("data") or {}).get("data") or {}).get(canary_key)
            assert read_back == canary_val, (
                "the restored KV secret value must match what was written "
                f"pre-snapshot (Req 11.4); got {read_back!r}"
            )
        finally:
            # Best-effort cleanup: delete the canary metadata + the snapshot temp.
            bao.post(f"{_KV_MOUNT}/delete/{secret_path}", {"versions": [1, 2, 3]})
            self._docker_rm(container, snap_path)
            if restore_container and restore_container != container:
                self._docker_rm(restore_container, snap_path)

    # ------------------------------------------------------------------ #
    # Req 10.8 / FD.7 — openbao_core_unsealed is exposed and reads 0 sealed.
    # ------------------------------------------------------------------ #
    def test_core_unsealed_metric_exposed_and_reads_one_while_unsealed(
        self, bao: _OpenBao
    ):
        """The metrics endpoint exposes ``openbao_core_unsealed`` and it reads
        ``1`` on the running (unsealed) instance (Req 10.8, part 1).

        The task-10.1 telemetry stanza makes ``/v1/sys/metrics`` scrapeable
        WITHOUT a token (``unauthenticated_metrics_access = true``); assert the
        sealed-metric series is present and reads 1 while unsealed. The sealed=0
        half is the next test (needs a sealed instance).
        """
        status, text = bao.fetch_metrics_text()
        assert status == 200, (
            f"/v1/sys/metrics?format=prometheus must be scrapeable (Req 10.2); "
            f"got status {status}"
        )
        value = _parse_prometheus_metric(text, _SEALED_METRIC)
        assert value is not None, (
            f"the {_SEALED_METRIC} series must be exposed on the metrics "
            "endpoint (Req 10.8)"
        )
        # On the running instance (assumed unsealed) the metric is 1.
        if bao.is_unsealed():
            assert value == 1.0, (
                f"{_SEALED_METRIC} must read 1 while the instance is unsealed "
                f"(Req 10.8); got {value}"
            )

    def test_core_unsealed_metric_reads_zero_while_sealed(self, bao: _OpenBao):
        """``openbao_core_unsealed`` reads ``0`` while the instance is SEALED and
        the metrics endpoint stays scrapeable (Req 10.8 / FD.7).

        Prefers a dedicated already-sealed throwaway instance
        (``OPENBAO_TEST_SEALED_ADDR``) so no shared instance is disturbed. Only
        if that is absent AND ``OPENBAO_TEST_ALLOW_SEAL`` is opted into does it
        seal the base instance (and leave it sealed — unsealing needs the Transit
        unsealer/operator). Skips cleanly otherwise rather than sealing a shared
        instance.
        """
        sealed_addr = os.environ.get(_SEALED_ADDR_ENV)
        allow_seal = _is_truthy(os.environ.get(_ALLOW_SEAL_ENV))

        if sealed_addr:
            sealed = _OpenBao(sealed_addr, os.environ.get(_TOKEN_ENV))
            status, text = sealed.fetch_metrics_text()
            assert status == 200, (
                "an already-sealed instance must STILL expose scrapeable metrics "
                f"(Req 10.8 / FD.7); {_SEALED_ADDR_ENV} returned status {status}"
            )
            value = _parse_prometheus_metric(text, _SEALED_METRIC)
            assert value == 0.0, (
                f"{_SEALED_METRIC} must read 0 while sealed (Req 10.8 / FD.7); "
                f"got {value} from {_SEALED_ADDR_ENV}"
            )
            return

        if not allow_seal:
            pytest.skip(
                "requires-infra, skipped: reading the sealed metric needs either "
                f"a dedicated already-sealed instance ({_SEALED_ADDR_ENV}) or "
                f"explicit opt-in ({_ALLOW_SEAL_ENV}=1) to seal the base "
                "instance. Sealing the shared base instance is destructive "
                "(unsealing needs the Transit unsealer/operator), so it is never "
                "done implicitly."
            )

        # Opted-in: seal the base instance, read the metric, leave it sealed.
        token = os.environ.get(_TOKEN_ENV)
        assert token
        status, resp = bao.post("sys/seal", {})
        assert status in (200, 204), (
            f"sys/seal should succeed when opted in ({_ALLOW_SEAL_ENV}=1); "
            f"got {status}: {resp}"
        )
        # Poll briefly for the sealed state to reflect on the metrics endpoint
        # (within one scrape interval; we assert it appears, not the timing).
        interval = self._scrape_interval_seconds()
        deadline = time.monotonic() + max(interval, 10)
        value = None
        while time.monotonic() < deadline:
            m_status, text = bao.fetch_metrics_text()
            if m_status == 200:
                value = _parse_prometheus_metric(text, _SEALED_METRIC)
                if value == 0.0:
                    break
            time.sleep(1)
        assert value == 0.0, (
            f"{_SEALED_METRIC} must read 0 while sealed and the metrics endpoint "
            f"must stay scrapeable (Req 10.8 / FD.7); got {value}"
        )

    # ------------------------------------------------------------------ #
    # Req 10.3 — Prometheus ingests job=openbao within two scrape intervals.
    # ------------------------------------------------------------------ #
    def test_prometheus_ingests_job_openbao_within_two_scrape_intervals(
        self, bao: _OpenBao
    ):
        """A reachable test Prometheus has ingested the ``job=openbao`` series
        within two scrape intervals (Req 10.3).

        Queries the test Prometheus ``/api/v1/query`` for ``up{job="openbao"}``
        and polls until a sample appears, bounded by two scrape intervals plus a
        small margin. Skips cleanly when no test Prometheus URL is provided.
        """
        prom_url = os.environ.get(_PROM_URL_ENV)
        if not prom_url:
            pytest.skip(
                f"requires-infra, skipped: {_PROM_URL_ENV} not set — the Req 10.3 "
                "ingestion assertion needs a reachable test Prometheus "
                "(e.g. http://127.0.0.1:9090). The metric-exposition half is "
                "covered by the sealed-metric tests above."
            )

        interval = self._scrape_interval_seconds()
        # "within two scrape intervals" + a small margin for scrape jitter.
        deadline = time.monotonic() + (2 * interval) + 5
        found = False
        last_seen: Any = None
        while time.monotonic() < deadline:
            samples = self._prom_query(prom_url, 'up{job="openbao"}')
            last_seen = samples
            if samples:
                found = True
                break
            time.sleep(min(interval, 5))
        assert found, (
            "Prometheus must ingest the job=openbao series within two scrape "
            f"intervals (Req 10.3); no `up{{job=openbao}}` sample after "
            f"~{2 * interval + 5:.0f}s. Last query result: {last_seen!r}"
        )

    # ------------------------------------------------------------------ #
    # Helpers — docker-exec snapshot drive + Prometheus query.
    # The token is threaded ONLY via the child-process env (BAO_TOKEN); it is
    # NEVER placed on a command line, logged, or returned in a message.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scrape_interval_seconds() -> int:
        raw = os.environ.get(_PROM_INTERVAL_ENV)
        if not raw:
            return _DEFAULT_SCRAPE_INTERVAL_S
        try:
            return max(int(raw), 1)
        except ValueError:
            return _DEFAULT_SCRAPE_INTERVAL_S

    @staticmethod
    def _docker_env(token: str) -> dict[str, str]:
        # Credentials go ONLY into the child-process env, never argv (Req: secrets
        # discipline, mirrors conftest.live_apply's TF_VAR_* handling).
        return {**os.environ, "BAO_TOKEN": token}

    def _docker_snapshot_save(self, container: str, token: str, path: str) -> int:
        """Run `bao operator raft snapshot save <path>` in the container."""
        proc = subprocess.run(
            [
                "docker", "exec",
                "-e", "BAO_ADDR=https://127.0.0.1:8200",
                "-e", "BAO_TOKEN",  # value comes from env, not argv
                "-e", "BAO_SKIP_VERIFY=true",
                container,
                "bao", "operator", "raft", "snapshot", "save", path,
            ],
            env=self._docker_env(token),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode

    def _docker_snapshot_restore(self, container: str, token: str, path: str) -> int:
        """Run `bao operator raft snapshot restore -force <path>` in the container."""
        proc = subprocess.run(
            [
                "docker", "exec",
                "-e", "BAO_ADDR=https://127.0.0.1:8200",
                "-e", "BAO_TOKEN",
                "-e", "BAO_SKIP_VERIFY=true",
                container,
                "bao", "operator", "raft", "snapshot", "restore", "-force", path,
            ],
            env=self._docker_env(token),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode

    @staticmethod
    def _docker_file_size(container: str, path: str) -> int | None:
        """`stat -c %s <path>` inside the container; None if it does not exist."""
        proc = subprocess.run(
            ["docker", "exec", container, "stat", "-c", "%s", path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return None

    @staticmethod
    def _docker_rm(container: str, path: str) -> None:
        subprocess.run(
            ["docker", "exec", container, "rm", "-f", path],
            capture_output=True, text=True, timeout=30,
        )

    @staticmethod
    def _docker_copy_between(
        src_container: str, src_path: str, dst_container: str, dst_path: str
    ) -> None:
        """Copy a file from one container to another via the host (docker cp)."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="btest-snap-") as tmp:
            host_path = os.path.join(tmp, "snap.snap")
            subprocess.run(
                ["docker", "cp", f"{src_container}:{src_path}", host_path],
                capture_output=True, text=True, timeout=60, check=True,
            )
            subprocess.run(
                ["docker", "cp", host_path, f"{dst_container}:{dst_path}"],
                capture_output=True, text=True, timeout=60, check=True,
            )

    def _assert_pbs_landing_if_configured(self, container: str, snap_path: str) -> None:
        """When a PBS test env is present, assert a snapshot copy lands in PBS.

        Uses ``proxmox-backup-client`` (inside the container or on the host,
        whichever has it) against ``OPENBAO_TEST_PBS_REPOSITORY`` /
        ``OPENBAO_TEST_PBS_PASSWORD``. When the PBS env is absent this is a no-op
        (the non-empty-file assertion already satisfies the core of Req 11.1);
        the PBS-landing sub-assertion is best-effort and only runs when the
        operator supplied a throwaway PBS target.
        """
        pbs_repo = os.environ.get("OPENBAO_TEST_PBS_REPOSITORY")
        pbs_password = os.environ.get("OPENBAO_TEST_PBS_PASSWORD")
        if not (pbs_repo and pbs_password):
            return  # PBS sub-assertion not configured; core Req 11.1 already asserted
        if not shutil.which("proxmox-backup-client"):
            return  # no PBS client on the runner; skip the landing sub-assertion
        # Copy the snapshot out of the container, then back it up + verify a
        # snapshot appears in the repository listing.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="btest-pbs-") as tmp:
            host_path = os.path.join(tmp, "openbao-raft.snap")
            subprocess.run(
                ["docker", "cp", f"{container}:{snap_path}", host_path],
                capture_output=True, text=True, timeout=60, check=True,
            )
            env = {**os.environ, "PBS_PASSWORD": pbs_password}
            backup = subprocess.run(
                [
                    "proxmox-backup-client", "backup",
                    f"openbao-raft.img:{host_path}",
                    "--repository", pbs_repo,
                    "--backup-id", "svc07-openbao-btest",
                ],
                env=env, capture_output=True, text=True, timeout=120,
            )
            assert backup.returncode == 0, (
                "the snapshot must land in PBS when a PBS test target is "
                "configured (Req 11.1 hand-off); proxmox-backup-client backup "
                f"exited {backup.returncode}"
            )
            listing = subprocess.run(
                ["proxmox-backup-client", "snapshots", "--repository", pbs_repo],
                env=env, capture_output=True, text=True, timeout=60,
            )
            assert "svc07-openbao-btest" in listing.stdout, (
                "a non-empty snapshot must be listed in PBS after hand-off "
                "(Req 11.1)"
            )

    @staticmethod
    def _prom_query(prom_url: str, promql: str) -> list:
        """Run an instant ``/api/v1/query`` and return the result vector.

        Returns the ``data.result`` list (empty when the series is absent).
        Never raises for an HTTP error / unreachable Prometheus — returns [].
        """
        query = urllib.parse.urlencode({"query": promql})
        url = f"{prom_url.rstrip('/')}/api/v1/query?{query}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return []
        if data.get("status") != "success":
            return []
        return (data.get("data") or {}).get("result") or []
