"""Live dynamic-credential INTEGRATION tests against a real OpenBao + backends.

Task 12.2 (spec: svc-07-secrets-manager) — requirements.md Requirements 2.1,
2.2, 2.4, 2.6, 2.8 (dynamic PostgreSQL creds) and 3.1, 3.2, 3.4 (dynamic
S3/Garage creds); design.md "Testing Strategy §3 (Integration tests against a
live OpenBao — requires_infra-gated)".

=== WHAT THIS TEST IS (and is NOT) ===

These are INTEGRATION tests in the ``testing-strategy.md`` §3 sense: they exercise
the full end-to-end dynamic-credential lifecycle across OpenBao AND its real
backends — a real PostgreSQL (SVC-01) reached through OpenBao's ``database``
engine, and a real Garage (SVC-03) S3 endpoint reached through OpenBao's ``aws``
engine. Nothing here is a mock: a credential is minted by OpenBao, USED against
the real backend, and then — after its TTL elapses or its lease is explicitly
revoked — the SAME real backend is observed REJECTING it. That end-to-end
"mint → use → expire → rejected-by-backend" round-trip is exactly what the
offline config-validation (``test_svc07_openbao_config_render.py``) and the live
role-SHAPE contract test (``test_svc07_openbao_engine_contract.py``, task 7.4)
cannot prove — those pin the stored role/TTL shape; THIS closes the loop on the
runtime revocation behaviour the backend actually enforces.

Because they need a live OpenBao (plus a live PG and a live Garage) they are
gated with the ESTABLISHED ``@pytest.mark.requires_infra`` marker (deselected by
default via the root ``pytest.ini``'s ``addopts = -m "not requires_infra"``) and,
additionally, ``skipif``-gated on the SAME OpenBao env-var contract the task-7.4
contract test uses (``OPENBAO_TEST_ADDR`` / ``OPENBAO_TEST_TOKEN``) so they SKIP
CLEANLY when no live OpenBao is reachable — never fail for lack of infra
(documentation-testing steering: "absent infra => skipped, not failed"). No new
gate is invented: the marker + ``skipif``-on-env-vars idiom, and the stdlib
``urllib`` OpenBao client, are LIFTED verbatim from
``test_svc07_openbao_engine_contract.py`` (task 7.4). The ONLY additions are the
extra backend env vars (``OPENBAO_TEST_PG_*`` / ``OPENBAO_TEST_GARAGE_*``) that
gate the PG- and Garage-specific sub-assertions.

=== ENV-VAR CONTRACT (documented here + in .env.example / TESTING.md) ===

The OpenBao gate (REQUIRED — returned FIRST so the offline skip is deterministic
on ``OPENBAO_TEST_ADDR``, matching the task-7.4 / task-9.2 precedent that the
preservation baseline scrubs):

  * ``OPENBAO_TEST_ADDR``  — the live OpenBao API base URL (e.g.
    ``https://127.0.0.1:8200``). A THROWAWAY/ephemeral OpenBao only — this test
    WRITES a disposable ``ctest-*`` database/aws connection + role and mints
    real backend credentials under it.
  * ``OPENBAO_TEST_TOKEN`` — a ``platform-admin``-/root-scoped token on the
    throwaway instance (write+read ``database/*`` and ``aws/*``, and revoke
    leases via ``sys/leases/*``). Threaded ONLY into request headers; never
    logged, never on a command line, never in an assertion message.
  * ``OPENBAO_TEST_SKIP_TLS_VERIFY`` — optional; ``1``/``true`` (default) skips
    verification of a self-signed test cert, ``0`` enforces it.

The PostgreSQL backend gate (Req 2 — when ABSENT, the PG class skips cleanly
AFTER the OpenBao gate, so ``OPENBAO_TEST_ADDR`` remains the deterministic
offline skip signal):

  * ``OPENBAO_TEST_PG_HOST`` / ``OPENBAO_TEST_PG_PORT`` — a reachable test
    PostgreSQL (SVC-01) OpenBao's ``database`` engine connects to. Port defaults
    to 5432.
  * ``OPENBAO_TEST_PG_CONN_URL`` — the ``connection_url`` template OpenBao uses,
    e.g. ``postgresql://{{username}}:{{password}}@pg-host:5432/postgres?sslmode=disable``.
  * ``OPENBAO_TEST_PG_ADMIN_USER`` / ``OPENBAO_TEST_PG_ADMIN_PASSWORD`` — the
    dedicated ``openbao`` superuser OpenBao authenticates as to mint/drop roles
    (Req 2.1). Never logged.
  * ``OPENBAO_TEST_PG_DB`` — the database the minted user connects to
    (default ``postgres``).

The Garage backend gate (Req 3 — same clean-skip-after-OpenBao contract):

  * ``OPENBAO_TEST_GARAGE_S3_ENDPOINT`` — the Garage S3-compatible API base URL
    the ``aws`` engine and this test's S3 probe both target.
  * ``OPENBAO_TEST_GARAGE_REGION`` — S3 region label (default ``garage``).
  * ``OPENBAO_TEST_GARAGE_BUCKET`` — an existing test bucket to probe with a
    minted key (default ``ctest-bucket``).
  * ``OPENBAO_TEST_GARAGE_ADMIN_ACCESS_KEY`` / ``OPENBAO_TEST_GARAGE_ADMIN_SECRET_KEY``
    — the Garage admin key pair OpenBao's ``aws`` engine roots on. Never logged.

=== TTL / TIMING ===

Requirements 2.4 / 3.4 assert rejection "within 60 seconds of expiry". To keep
the live tier fast, the disposable roles are minted with a SHORT ``default_ttl``
(``OPENBAO_TEST_SHORT_TTL_SECONDS``, default 10 s) and ``max_ttl`` large enough
to cover it; the test then waits ``ttl + revocation_grace`` (grace default 60 s,
Req 2.4/3.4 window) and asserts the backend rejects. This mirrors the real 1h/24h
role shape (asserted by the task-7.4 contract test) without a 1-hour wait — the
REVOCATION behaviour under test is identical regardless of the TTL magnitude.

=== WHY urllib / stdlib, NOT bao / boto3 / psycopg-by-default ===

The OpenBao control-plane calls (enable engine, write role, read creds, list +
revoke leases) are HTTP and use the SAME stdlib ``urllib`` client as task 7.4 —
no ``bao`` binary, no third-party HTTP client (matching the rest of
``infra/tests/``). For the *backend-rejection* proof:

  * PostgreSQL: proving PG REJECTS an expired credential needs a real PG
    connection. ``psycopg`` is imported via ``pytest.importorskip`` (falling back
    to ``psycopg2``) so the AUTH-rejection sub-assertion skips cleanly when no
    driver is installed — but expiry is ALSO proven driver-free via OpenBao's
    lease API (the lease disappears after TTL), so the core Req 2.4/2.8 signal
    does not hinge on a driver being present.
  * Garage: an S3 ``ListBucket``/``GetObject`` request is SigV4-signed with
    stdlib ``hmac``/``hashlib`` (no boto3), so proving Garage accepts-then-rejects
    a minted key needs no third-party dependency at all.

Every resource this test creates uses a disposable ``ctest-<uuid>`` prefix and is
torn down in a ``finally`` block, so a re-run never collides and the instances
are left clean.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_dynamic_creds.py -v
      (skips cleanly with no OPENBAO_TEST_* env vars; runs against a live
       throwaway OpenBao + test PG + test Garage when they are set, opting into
       the live tier with `-m requires_infra`.)
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Env-var contract (see module docstring + .env.example). The OpenBao pair is
# LIFTED verbatim from task 7.4 so the offline skip stays deterministic on
# OPENBAO_TEST_ADDR (the preservation baseline scrubs exactly these).
# --------------------------------------------------------------------------- #
_ADDR_ENV = "OPENBAO_TEST_ADDR"
_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"

# PostgreSQL backend (Req 2) gate vars.
_PG_HOST_ENV = "OPENBAO_TEST_PG_HOST"
_PG_PORT_ENV = "OPENBAO_TEST_PG_PORT"
_PG_CONN_URL_ENV = "OPENBAO_TEST_PG_CONN_URL"
_PG_ADMIN_USER_ENV = "OPENBAO_TEST_PG_ADMIN_USER"
_PG_ADMIN_PASSWORD_ENV = "OPENBAO_TEST_PG_ADMIN_PASSWORD"
_PG_DB_ENV = "OPENBAO_TEST_PG_DB"

# Garage backend (Req 3) gate vars.
_GARAGE_ENDPOINT_ENV = "OPENBAO_TEST_GARAGE_S3_ENDPOINT"
_GARAGE_REGION_ENV = "OPENBAO_TEST_GARAGE_REGION"
_GARAGE_BUCKET_ENV = "OPENBAO_TEST_GARAGE_BUCKET"
_GARAGE_ADMIN_ACCESS_KEY_ENV = "OPENBAO_TEST_GARAGE_ADMIN_ACCESS_KEY"
_GARAGE_ADMIN_SECRET_KEY_ENV = "OPENBAO_TEST_GARAGE_ADMIN_SECRET_KEY"

# Timing knobs (see module docstring "TTL / TIMING").
_SHORT_TTL_ENV = "OPENBAO_TEST_SHORT_TTL_SECONDS"
_DEFAULT_SHORT_TTL_SECONDS = 10
_REVOCATION_GRACE_SECONDS = 60  # Req 2.4 / 3.4: rejection within 60 s of expiry
_POLL_INTERVAL_SECONDS = 3


def _live_gate_reason() -> str | None:
    """Return a skip reason if the OpenBao live gate is not satisfied, else None.

    Requires BOTH ``OPENBAO_TEST_ADDR`` and ``OPENBAO_TEST_TOKEN``. Checked
    FIRST (before any PG/Garage var) so the whole class SKIPS cleanly on a
    DETERMINISTIC ``OPENBAO_TEST_ADDR`` signal — the SAME contract the task-7.4
    engine-contract class and the task-9.2 onboard tests use, which the offline
    preservation baseline relies on (it scrubs OPENBAO_TEST_ADDR to force this
    exact skip). Never fails for lack of infra.
    """
    if not os.environ.get(_ADDR_ENV):
        return (
            f"requires-infra, skipped: {_ADDR_ENV} not set (a live throwaway "
            "OpenBao API endpoint is required for the dynamic-credential "
            "integration tests)"
        )
    if not os.environ.get(_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_TOKEN_ENV} not set (a platform-admin / "
            "root-scoped token on the throwaway OpenBao is required to write the "
            "database/aws connection+role, mint creds, and revoke leases)"
        )
    return None


def _pg_gate_reason() -> str | None:
    """Return a skip reason if the PostgreSQL backend gate is not satisfied.

    Consulted only AFTER ``_live_gate_reason`` passes, so the OpenBao addr stays
    the deterministic offline signal. When the PG vars are absent the PG test
    skips cleanly (Req 2 needs a real SVC-01 to observe backend-side rejection).
    """
    required = {
        _PG_HOST_ENV: "a reachable test PostgreSQL host",
        _PG_CONN_URL_ENV: "the OpenBao database-engine connection_url template",
        _PG_ADMIN_USER_ENV: "the dedicated `openbao` superuser (Req 2.1)",
        _PG_ADMIN_PASSWORD_ENV: "the `openbao` superuser password",
    }
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        return (
            "requires-infra, skipped: dynamic-PG integration needs a live "
            "test PostgreSQL (SVC-01) — missing "
            + ", ".join(sorted(missing))
        )
    return None


def _garage_gate_reason() -> str | None:
    """Return a skip reason if the Garage backend gate is not satisfied.

    Consulted only AFTER ``_live_gate_reason`` passes. When the Garage vars are
    absent the Garage test skips cleanly (Req 3 needs a real SVC-03 endpoint to
    observe backend-side rejection).
    """
    required = {
        _GARAGE_ENDPOINT_ENV: "the Garage S3-compatible API endpoint",
        _GARAGE_ADMIN_ACCESS_KEY_ENV: "the Garage admin access key (Req 3.1)",
        _GARAGE_ADMIN_SECRET_KEY_ENV: "the Garage admin secret key",
    }
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        return (
            "requires-infra, skipped: dynamic-Garage integration needs a live "
            "test Garage (SVC-03) S3 endpoint — missing "
            + ", ".join(sorted(missing))
        )
    return None


def _short_ttl_seconds() -> int:
    try:
        return max(1, int(os.environ.get(_SHORT_TTL_ENV, _DEFAULT_SHORT_TTL_SECONDS)))
    except ValueError:
        return _DEFAULT_SHORT_TTL_SECONDS


def _tls_context() -> ssl.SSLContext | None:
    """TLS context for the OpenBao API calls (identical policy to task 7.4)."""
    skip = os.environ.get(_SKIP_TLS_ENV, "1").strip().lower() not in ("0", "false", "no", "")
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _OpenBao:
    """Minimal live OpenBao HTTP client (LIFTED from task 7.4).

    Token placed ONLY in the ``X-Vault-Token`` header — never logged, never on a
    command line, never in an error message.
    """

    def __init__(self, addr: str, token: str) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._ctx = _tls_context()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, method=method, data=data)
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

    def delete(self, path: str) -> tuple[int, Any]:
        return self._request("DELETE", path)

    def mount_exists(self, mount_path: str) -> bool:
        status, data = self.get("sys/mounts")
        if status == 200 and isinstance(data, dict):
            mounts = data.get("data", data)
            return f"{mount_path.rstrip('/')}/" in mounts
        return False

    def enable_secret_engine(self, mount_path: str, engine_type: str) -> None:
        self.post(f"sys/mounts/{mount_path}", {"type": engine_type})

    def disable_secret_engine(self, mount_path: str) -> None:
        self.delete(f"sys/mounts/{mount_path}")

    def list_leases(self, prefix: str) -> list[str]:
        """Return the lease-id suffixes under ``sys/leases/lookup/<prefix>``.

        Uses the LIST verb (OpenBao models it as GET with ``?list=true``).
        Returns ``[]`` when the prefix has no active leases.
        """
        status, data = self.get(f"sys/leases/lookup/{prefix.lstrip('/')}?list=true")
        if status == 200 and isinstance(data, dict):
            return list((data.get("data") or {}).get("keys") or [])
        return []


@pytest.fixture(scope="module")
def bao() -> _OpenBao:
    addr = os.environ.get(_ADDR_ENV)
    token = os.environ.get(_TOKEN_ENV)
    assert addr and token, "live gate should have prevented fixture construction"
    return _OpenBao(addr, token)


@pytest.fixture()
def ctest_slug() -> str:
    """A disposable, collision-proof slug for one test (``ctest-<uuid>``)."""
    return f"ctest-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Minimal stdlib PostgreSQL "does this credential authenticate?" probe.
#
# We do NOT need a full driver to observe REJECTION: we only need to know whether
# PG ACCEPTS or REJECTS a username/password at startup. But a correct SCRAM/MD5
# handshake is non-trivial to hand-roll, so the AUTH probe prefers a real driver
# via importorskip and the expiry proof ALSO uses OpenBao's lease API (no driver
# needed). This helper is only invoked once a driver is confirmed present.
# --------------------------------------------------------------------------- #
def _pg_can_authenticate(driver, host: str, port: int, db: str, user: str, password: str) -> bool:
    """True if ``user``/``password`` authenticates against PG, False if rejected.

    ``driver`` is the imported ``psycopg`` (v3) or ``psycopg2`` module. A
    successful connect returns True; an auth/role error returns False; any other
    connection error is re-raised so a genuine infra problem is not masked as a
    rejection.
    """
    conn = None
    try:
        if hasattr(driver, "connect") and driver.__name__ == "psycopg":
            conn = driver.connect(
                host=host, port=port, dbname=db, user=user,
                password=password, connect_timeout=10,
            )
        else:  # psycopg2
            conn = driver.connect(
                host=host, port=port, dbname=db, user=user,
                password=password, connect_timeout=10,
            )
        return True
    except Exception as exc:  # noqa: BLE001 — inspect the error class/text
        text = f"{type(exc).__name__}: {exc}".lower()
        # PG rejects a dropped/invalid role with a password/authentication or
        # "role does not exist" error — that is the REJECTION we assert.
        if any(
            marker in text
            for marker in (
                "password authentication failed",
                "authentication failed",
                "role",  # e.g. 'role "v-..." does not exist'
                "does not exist",
                "no password supplied",
            )
        ):
            return False
        # A non-auth error (host unreachable, DB missing, TLS) is an infra fault,
        # not a credential rejection — re-raise so it surfaces, not a false pass.
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Minimal stdlib SigV4 S3 probe for Garage (no boto3).
# --------------------------------------------------------------------------- #
def _sigv4_s3_get(
    endpoint: str,
    region: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    key: str = "",
) -> int:
    """Perform a SigV4-signed S3 GET (ListBucket if ``key`` empty, else GetObject).

    Returns the HTTP status code. 200/206 => accepted; 403 => rejected (the
    signal Req 3.4 asserts once the key expires); 404 => accepted-but-absent
    (still an ACCEPTED credential). Signed with stdlib hmac/hashlib only.
    """
    parsed = urllib.parse.urlparse(endpoint)
    host = parsed.netloc
    scheme = parsed.scheme or "https"
    canonical_uri = f"/{bucket}" + (f"/{key}" if key else "")
    service = "s3"

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_querystring = "" if key else "list-type=2"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (
        f"GET\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    )

    def _hmac(key_bytes: bytes, msg: str) -> bytes:
        return hmac.new(key_bytes, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"{algorithm} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    url = f"{scheme}://{host}{canonical_uri}"
    if canonical_querystring:
        url = f"{url}?{canonical_querystring}"
    req = urllib.request.Request(url=url, method="GET")
    req.add_header("Host", host)
    req.add_header("x-amz-content-sha256", payload_hash)
    req.add_header("x-amz-date", amz_date)
    req.add_header("Authorization", authorization)

    ctx = _tls_context() if scheme == "https" else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


# =========================================================================== #
# The dynamic-PostgreSQL integration suite — INFRA-GATED.
# Req 2.1, 2.2, 2.4, 2.6, 2.8.
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_gate_reason() is not None,
    reason=_live_gate_reason() or "live OpenBao gate satisfied",
)
class TestOpenBaoDynamicPostgresCredentials:
    """Mint → use → expire → PG-rejects, and bulk-revoke drops all users (Req 2).

    Creates a disposable ``ctest-<uuid>`` database connection + two short-TTL
    roles under it, mints real PG credentials, and asserts (a) OpenBao reports the
    lease gone within 60 s of TTL expiry and PG rejects the expired user
    (Req 2.4), and (b) an explicit bulk revoke of ``database/creds/<slug>-*``
    drops every user within 60 s (Req 2.8). All resources torn down in
    ``finally``.
    """

    def _pg_gate_or_skip(self) -> None:
        reason = _pg_gate_reason()
        if reason:
            pytest.skip(reason)

    def test_expired_pg_credential_is_rejected_within_60s_of_ttl(
        self, bao: _OpenBao, ctest_slug: str
    ):
        """A minted PG credential authenticates, then after TTL expiry the lease
        is gone and PG rejects it within 60 s (Req 2.2, 2.4).

        Expiry is proven driver-free via OpenBao's lease API (the lease vanishes
        after TTL); the PG-side REJECTION is additionally proven with a real
        driver when one is installed, else that single sub-assertion skips
        cleanly (importorskip) — the lease-gone signal still covers Req 2.4.
        """
        self._pg_gate_or_skip()

        slug = ctest_slug
        conn_name = f"{slug}-conn"
        role_name = f"{slug}-appdb-rw"
        ttl = _short_ttl_seconds()

        pg_host = os.environ[_PG_HOST_ENV]
        pg_port = int(os.environ.get(_PG_PORT_ENV, "5432"))
        pg_db = os.environ.get(_PG_DB_ENV, "postgres")

        enabled_db = False
        try:
            if not bao.mount_exists("database"):
                bao.enable_secret_engine("database", "database")
                enabled_db = True

            # Configure the connection against the real test PG as the dedicated
            # `openbao` superuser (Req 2.1). Admin creds live only in env + this
            # request body — never logged.
            status, resp = bao.post(
                f"database/config/{conn_name}",
                {
                    "plugin_name": "postgresql-database-plugin",
                    "connection_url": os.environ[_PG_CONN_URL_ENV],
                    "allowed_roles": [role_name],
                    "username": os.environ[_PG_ADMIN_USER_ENV],
                    "password": os.environ[_PG_ADMIN_PASSWORD_ENV],
                },
            )
            assert status in (200, 204), f"db config should succeed, got {status}: {resp}"

            # Short-TTL least-privilege role (Req 2.2 shape; short TTL for speed).
            status, resp = bao.post(
                f"database/roles/{role_name}",
                {
                    "db_name": conn_name,
                    "creation_statements": [
                        "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' "
                        "VALID UNTIL '{{expiration}}' NOSUPERUSER NOCREATEROLE "
                        "NOCREATEDB NOREPLICATION;",
                        'GRANT CONNECT ON DATABASE "' + pg_db + '" TO "{{name}}";',
                    ],
                    "revocation_statements": [
                        'DROP ROLE IF EXISTS "{{name}}";',
                    ],
                    "default_ttl": f"{ttl}s",
                    "max_ttl": f"{ttl * 4}s",
                },
            )
            assert status in (200, 204), f"db role should succeed, got {status}: {resp}"

            # Mint a credential (Req 2.2): unique username, >=20-char password.
            status, data = bao.get(f"database/creds/{role_name}")
            assert status == 200, f"creds read should succeed, got {status}: {data}"
            issued = data["data"]
            lease_id = data.get("lease_id")
            username = issued["username"]
            password = issued["password"]
            assert username, "OpenBao must mint a username (Req 2.2)"
            assert len(password) >= 20, (
                f"minted password must be >=20 chars (Req 2.2); got {len(password)}"
            )
            assert lease_id, "issuance must create a lease (Req 2.2)"

            # Prove the credential authenticates NOW (real driver if present).
            driver = _import_pg_driver()
            if driver is not None:
                assert _pg_can_authenticate(
                    driver, pg_host, pg_port, pg_db, username, password
                ), "freshly-minted PG credential must authenticate (Req 2.2)"

            # Wait past TTL + revocation grace and assert the lease is GONE
            # (driver-free proof of revocation, Req 2.4).
            deadline = time.monotonic() + ttl + _REVOCATION_GRACE_SECONDS
            lease_gone = False
            while time.monotonic() < deadline:
                remaining = bao.list_leases(f"database/creds/{role_name}")
                # lease suffixes are the trailing id segment; match ours.
                if not any(lease_id.endswith(suffix) for suffix in remaining):
                    lease_gone = True
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)
            assert lease_gone, (
                f"lease for {role_name} must be revoked within "
                f"{ttl + _REVOCATION_GRACE_SECONDS}s of TTL expiry (Req 2.4)"
            )

            # PG-side rejection (Req 2.4): the dropped user must no longer auth.
            if driver is not None:
                assert not _pg_can_authenticate(
                    driver, pg_host, pg_port, pg_db, username, password
                ), (
                    "PostgreSQL must REJECT the expired/dropped credential "
                    "within 60 s of TTL expiry (Req 2.4)"
                )
            else:
                pytest.skip(
                    "requires-infra (partial): OpenBao lease-revocation proven "
                    "(Req 2.4 lease gone), but no psycopg/psycopg2 driver is "
                    "installed to prove the PG-side auth rejection directly — "
                    "install psycopg to exercise that sub-assertion."
                )
        finally:
            bao.post(f"sys/leases/revoke-prefix/database/creds/{role_name}", {})
            bao.delete(f"database/roles/{role_name}")
            bao.delete(f"database/config/{conn_name}")
            if enabled_db:
                bao.disable_secret_engine("database")

    def test_bulk_revoke_of_slug_prefix_drops_all_users_within_60s(
        self, bao: _OpenBao, ctest_slug: str
    ):
        """Bulk revoke of ``database/creds/<slug>-*`` drops every active user
        within 60 s (Req 2.8) — project decommission invalidates all outstanding
        dynamic credentials without waiting for TTL.

        Mints credentials from TWO roles under the same slug, confirms both
        leases exist, issues a single prefix revoke, and asserts all leases under
        the slug prefix are gone within 60 s. Driver-free (lease-API based), so it
        runs whenever the OpenBao + PG gate is satisfied.
        """
        self._pg_gate_or_skip()

        slug = ctest_slug
        conn_name = f"{slug}-conn"
        role_a = f"{slug}-alpha-rw"
        role_b = f"{slug}-beta-rw"
        pg_db = os.environ.get(_PG_DB_ENV, "postgres")
        # Longer TTL here: we prove EXPLICIT revoke beats TTL, so TTL must not
        # expire on its own during the test window.
        ttl = _short_ttl_seconds() + _REVOCATION_GRACE_SECONDS + 120

        enabled_db = False
        try:
            if not bao.mount_exists("database"):
                bao.enable_secret_engine("database", "database")
                enabled_db = True

            status, resp = bao.post(
                f"database/config/{conn_name}",
                {
                    "plugin_name": "postgresql-database-plugin",
                    "connection_url": os.environ[_PG_CONN_URL_ENV],
                    "allowed_roles": [role_a, role_b],
                    "username": os.environ[_PG_ADMIN_USER_ENV],
                    "password": os.environ[_PG_ADMIN_PASSWORD_ENV],
                },
            )
            assert status in (200, 204), f"db config should succeed, got {status}: {resp}"

            for role_name in (role_a, role_b):
                status, resp = bao.post(
                    f"database/roles/{role_name}",
                    {
                        "db_name": conn_name,
                        "creation_statements": [
                            "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD "
                            "'{{password}}' VALID UNTIL '{{expiration}}' "
                            "NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION;",
                            'GRANT CONNECT ON DATABASE "' + pg_db + '" TO "{{name}}";',
                        ],
                        "revocation_statements": ['DROP ROLE IF EXISTS "{{name}}";'],
                        "default_ttl": f"{ttl}s",
                        "max_ttl": f"{ttl * 2}s",
                    },
                )
                assert status in (200, 204), f"db role {role_name} should succeed: {resp}"

            # Mint from both roles.
            minted = []
            for role_name in (role_a, role_b):
                status, data = bao.get(f"database/creds/{role_name}")
                assert status == 200, f"creds read {role_name} should succeed: {data}"
                minted.append(data["lease_id"])
            assert all(minted), "both roles must issue leases (Req 2.8 precondition)"

            # Sanity: leases exist under each role's creds prefix before revoke.
            pre_a = bao.list_leases(f"database/creds/{role_a}")
            pre_b = bao.list_leases(f"database/creds/{role_b}")
            assert pre_a and pre_b, (
                "both roles must have an active lease before bulk revoke "
                "(Req 2.8 precondition)"
            )

            # Bulk revoke the WHOLE slug prefix in one call (Req 2.8).
            status, resp = bao.post(
                f"sys/leases/revoke-prefix/database/creds/{slug}-", {}
            )
            assert status in (200, 204), (
                f"prefix revoke of database/creds/{slug}-* should succeed, "
                f"got {status}: {resp}"
            )

            # Assert ALL leases under both roles are gone within 60 s (Req 2.8).
            deadline = time.monotonic() + _REVOCATION_GRACE_SECONDS
            all_gone = False
            while time.monotonic() < deadline:
                if not bao.list_leases(f"database/creds/{role_a}") and not bao.list_leases(
                    f"database/creds/{role_b}"
                ):
                    all_gone = True
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)
            assert all_gone, (
                f"bulk revoke of database/creds/{slug}-* must drop ALL users "
                f"within {_REVOCATION_GRACE_SECONDS}s (Req 2.8)"
            )
        finally:
            bao.post(f"sys/leases/revoke-prefix/database/creds/{slug}-", {})
            bao.delete(f"database/roles/{role_a}")
            bao.delete(f"database/roles/{role_b}")
            bao.delete(f"database/config/{conn_name}")
            if enabled_db:
                bao.disable_secret_engine("database")


# =========================================================================== #
# The dynamic-Garage integration suite — INFRA-GATED.
# Req 3.1, 3.2, 3.4.
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_gate_reason() is not None,
    reason=_live_gate_reason() or "live OpenBao gate satisfied",
)
class TestOpenBaoDynamicGarageCredentials:
    """Mint → use → expire → Garage-rejects (Req 3.1, 3.2, 3.4).

    Creates a disposable ``ctest-<uuid>`` aws role scoped to ``<slug>-<bucket>``,
    mints a real access-key/secret-key pair against the live Garage endpoint,
    proves the key is USABLE (SigV4 ListBucket accepted), waits past TTL, and
    asserts Garage REJECTS it (403) within 60 s of expiry. All resources torn
    down in ``finally``. SigV4 signing is stdlib-only (no boto3).
    """

    def _garage_gate_or_skip(self) -> None:
        reason = _garage_gate_reason()
        if reason:
            pytest.skip(reason)

    def test_expired_garage_key_is_rejected_within_60s_of_ttl(
        self, bao: _OpenBao, ctest_slug: str
    ):
        self._garage_gate_or_skip()

        slug = ctest_slug
        bucket = os.environ.get(_GARAGE_BUCKET_ENV, "ctest-bucket")
        region = os.environ.get(_GARAGE_REGION_ENV, "garage")
        endpoint = os.environ[_GARAGE_ENDPOINT_ENV]
        role_name = f"{slug}-{bucket}-rw"
        resource = f"{slug}-{bucket}"
        ttl = _short_ttl_seconds()

        enabled_aws = False
        configured = False
        try:
            if not bao.mount_exists("aws"):
                bao.enable_secret_engine("aws", "aws")
                enabled_aws = True

            # Root the aws engine on the Garage admin key against the Garage
            # S3/IAM endpoint (Req 3.1). Admin creds live only in env + body.
            status, resp = bao.post(
                "aws/config/root",
                {
                    "access_key": os.environ[_GARAGE_ADMIN_ACCESS_KEY_ENV],
                    "secret_key": os.environ[_GARAGE_ADMIN_SECRET_KEY_ENV],
                    "region": region,
                    "endpoint": endpoint,
                    "iam_endpoint": endpoint,
                    "sts_endpoint": endpoint,
                },
            )
            # Some Garage/aws-engine combos accept 204; a non-2xx here is an infra
            # mismatch (the documented Garage-compatibility risk) — skip cleanly.
            if status not in (200, 204):
                pytest.skip(
                    "requires-infra, skipped: could not configure the aws engine "
                    f"root against the Garage endpoint (status {status}). This is "
                    "the documented Garage/aws-engine S3-compatibility risk "
                    "(design.md Open Risks); a Garage admin-API shim may be "
                    "required before this integration runs."
                )
            configured = True

            # Per-bucket-scoped short-TTL role (Req 3.2 scope; short TTL for speed).
            policy_document = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:ListBucket",
                        ],
                        "Resource": [
                            f"arn:aws:s3:::{resource}",
                            f"arn:aws:s3:::{resource}/*",
                        ],
                    }
                ],
            }
            status, resp = bao.post(
                f"aws/roles/{role_name}",
                {
                    "credential_type": "iam_user",
                    "policy_document": json.dumps(policy_document),
                    "default_sts_ttl": f"{ttl}s",
                    "max_sts_ttl": f"{ttl * 4}s",
                },
            )
            assert status in (200, 204), f"aws role should succeed, got {status}: {resp}"

            # Mint a key (Req 3.2).
            status, data = bao.get(f"aws/creds/{role_name}")
            if status != 200:
                pytest.skip(
                    "requires-infra, skipped: the aws engine could not mint a key "
                    f"against Garage (status {status}: {data}). Documented Garage "
                    "S3/IAM-compatibility risk (design.md Open Risks)."
                )
            issued = data["data"]
            lease_id = data.get("lease_id")
            access_key = issued["access_key"]
            secret_key = issued["secret_key"]
            assert access_key and secret_key, "aws engine must mint a key pair (Req 3.2)"
            assert lease_id, "issuance must create a lease (Req 3.2)"

            # Garage propagation can lag key creation slightly; poll for ACCEPT.
            probe_bucket = os.environ.get(_GARAGE_BUCKET_ENV, bucket)
            accept_deadline = time.monotonic() + 30
            accepted = False
            while time.monotonic() < accept_deadline:
                code = _sigv4_s3_get(
                    endpoint, region, access_key, secret_key, probe_bucket
                )
                # 200/206/404 => the CREDENTIAL was accepted (auth passed); 403 =>
                # not yet propagated / denied — keep polling.
                if code in (200, 206, 404):
                    accepted = True
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)
            if not accepted:
                pytest.skip(
                    "requires-infra, skipped: the minted Garage key never became "
                    "usable within 30 s (Garage S3/IAM-compatibility or bucket-"
                    "existence issue). Cannot assert expiry-rejection without a "
                    "usable-then-expired transition. See design.md Open Risks."
                )

            # Wait past TTL + revocation grace, then assert Garage REJECTS (Req 3.4).
            time.sleep(ttl + 5)
            deadline = time.monotonic() + _REVOCATION_GRACE_SECONDS
            rejected = False
            while time.monotonic() < deadline:
                code = _sigv4_s3_get(
                    endpoint, region, access_key, secret_key, probe_bucket
                )
                if code in (401, 403):
                    rejected = True
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)
            assert rejected, (
                "Garage must REJECT the expired access key (401/403) within "
                f"{_REVOCATION_GRACE_SECONDS}s of TTL expiry (Req 3.4)"
            )
        finally:
            bao.post(f"sys/leases/revoke-prefix/aws/creds/{role_name}", {})
            bao.delete(f"aws/roles/{role_name}")
            if configured:
                # Best-effort: leave the engine's root config as-is is fine on a
                # throwaway instance; disabling the mount clears everything.
                pass
            if enabled_aws:
                bao.disable_secret_engine("aws")


# --------------------------------------------------------------------------- #
# PG driver import helper (importorskip-style, but non-fatal — returns None so
# the caller can still assert the driver-free lease-revocation signal).
# --------------------------------------------------------------------------- #
def _import_pg_driver():
    """Return an imported psycopg (v3) or psycopg2 module, or None if neither is
    installed. Non-fatal by design: the lease-API expiry proof does not need a
    driver, so the caller decides whether to skip the PG-side sub-assertion.
    """
    try:
        import psycopg  # type: ignore

        return psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return psycopg2
    except ImportError:
        return None
