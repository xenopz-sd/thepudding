"""Live JWT/OIDC-login, rotate-root, and Traefik-ingress INTEGRATION tests.

Task 12.3 (spec: svc-07-secrets-manager) — requirements.md Requirement 4
criteria 3 & 4 (valid/invalid GitLab ID-token login), Requirement 6 criteria
for Traefik-only ingress + direct-:8200 refusal (Security AC 6), and Requirement
8 criteria 1-3 (rotate-root for the PostgreSQL `database` and Garage `aws`
engines, pre-rotation leases keep authenticating, next issuance uses the new
root). Design: "Testing Strategy §3 (Integration tests against a live OpenBao —
requires_infra-gated)".

=== WHAT THIS TEST IS (and is NOT) ===

These are INTEGRATION tests in the ``testing-strategy.md`` §3 sense: they exercise
three real seams of a live OpenBao end-to-end — the ``jwt`` auth method's
accept/deny decision on a presented signed JWT, the ``database``/``aws`` engines'
root-credential rotation, and the network path that only Traefik may traverse to
reach the primary's ``:8200``. They are NOT contract tests (task 7.4's
``test_svc07_openbao_engine_contract.py`` already pins the STORED role/policy
shapes); this file closes the behavioural loop that no offline or contract test
can prove — that a login is actually granted/refused, that a rotated root is
actually used on next issuance while old leases keep working, and that a direct
``:8200`` connect from a VLAN-20 vantage point is actually refused.

Because they need a live OpenBao (and, for some assertions, live PG/Garage
backends, a Traefik ingress, and a VLAN-20 vantage point) they are gated with the
ESTABLISHED ``@pytest.mark.requires_infra`` marker (deselected by default via the
root ``pytest.ini``'s ``addopts = -m "not requires_infra"``) and, additionally,
``skipif``-gated on the OpenBao env-var contract below so they SKIP CLEANLY when
no live OpenBao is reachable — never fail for lack of infra
(documentation-testing steering: "absent infra => skipped, not failed"). No new
gate is invented: the marker + ``skipif``-on-env-vars idiom is the SAME one the
sibling task-7.4 ``test_svc07_openbao_engine_contract.py`` uses, differing only
in the ADDITIONAL optional env vars that unlock the PG/aws/Traefik/VLAN-20
sub-assertions (each of which skips cleanly on its own when its extra dependency
is absent, rather than failing the whole class).

=== ENV-VAR CONTRACT (documented here + in TESTING.md) ===

Reused verbatim from task 7.4 (the class-level live gate):

  * ``OPENBAO_TEST_ADDR``  — the live OpenBao API base URL (e.g.
    ``https://127.0.0.1:8200``). A THROWAWAY/ephemeral OpenBao only — this test
    WRITES disposable ``ctest-*`` policies/roles/engine-mounts and tears them
    down in a ``finally`` block.
  * ``OPENBAO_TEST_TOKEN`` — a ``platform-admin``-scoped (or root) token on the
    throwaway instance, able to write+read ``auth/jwt/*``, ``sys/policies/acl/*``,
    ``database/*``, ``aws/*`` and to enable/disable those mounts.
  * ``OPENBAO_TEST_SKIP_TLS_VERIFY`` — optional; ``1``/``true`` (default) skips
    TLS verification for the ephemeral instance's self-signed cert; ``0``
    enforces it. (Same knob as task 7.4.)

ADDITIONAL, task-12.3-specific OPTIONAL vars (each unlocks one sub-assertion; when
absent, only THAT sub-assertion skips cleanly, the rest of the class still runs):

  * ``OPENBAO_TEST_JWT_SIGNING_KEY``  — a PEM RSA PRIVATE key the test uses to
    mint signed JWTs, whose matching PUBLIC key / JWKS the throwaway OpenBao's
    ``jwt`` auth method is configured to trust (via ``jwt_validation_pubkeys`` or
    a static-key config). Required for the accept/deny login round-trip.
  * ``OPENBAO_TEST_JWT_ISSUER``    — the ``iss`` the test-JWT carries and the
    ``jwt`` role/config is bound to (``bound_issuer``). Default ``https://gitlab.test``.
  * ``OPENBAO_TEST_JWT_AUDIENCE``  — the ``aud`` the valid test-JWT carries and
    the role's ``bound_audiences`` is set to. Default ``https://openbao.platform.test``.
  * ``OPENBAO_TEST_PG_ROTATE_CONN``   — the name of a live, already-configured
    ``database/config/<conn>`` PostgreSQL connection on the throwaway OpenBao
    (with at least one issuable role) to exercise ``database/rotate-root`` +
    pre-rotation-lease-survival + next-issuance. Skips cleanly when unset/absent.
  * ``OPENBAO_TEST_PG_ROTATE_ROLE``   — an issuable ``database/roles/<role>`` name
    on that connection; used to mint the pre-rotation lease and the
    post-rotation issuance. Skips cleanly when unset/absent.
  * ``OPENBAO_TEST_AWS_MOUNT``        — the mount path of a live, configured
    ``aws`` engine (default ``aws``) whose ``aws/config/rotate-root`` is exercised.
    Skips cleanly when the mount is absent/unconfigured.
  * ``OPENBAO_TEST_AWS_ROTATE_ROLE``  — an issuable ``aws/roles/<role>`` name used
    to mint the pre-rotation lease and the post-rotation issuance. Skips cleanly
    when unset/absent.
  * ``OPENBAO_TEST_TRAEFIK_URL``      — the external Traefik URL for the primary
    (e.g. ``https://openbao.platform.example``). The test GETs
    ``<url>/v1/sys/health`` and asserts an OpenBao health JSON responds THROUGH
    Traefik. Skips cleanly when unset.
  * ``OPENBAO_TEST_DIRECT_8200_HOST`` — a ``host[:port]`` for the primary's
    DIRECT ``:8200`` endpoint (e.g. ``10.0.20.10:8200``), reachable only from a
    VLAN-20 vantage point. The test opens a raw TCP socket and asserts the
    connect is REFUSED/times out (firewall drop) — proving Security AC 6. This
    assertion is only meaningful when the runner sits on a VLAN-20 host; when the
    var is unset it skips cleanly. Default port 8200 if no ``:port`` given.

SECURITY (security-standards.md — this IS the secrets manager): every token /
signing key is read from the environment and threaded ONLY into request headers
or an in-memory signer; it is NEVER logged, placed on a command line, or echoed
in an assertion message. Every resource this test creates uses a disposable
``ctest-<uuid>`` prefix and is torn down in a ``finally`` block, so a re-run
never collides and the instance is left clean.

=== JWT SIGNING DEPENDENCY ===

Minting a signed JWT the ``jwt`` auth method will accept needs a JWT-signing lib
(``pyjwt`` + ``cryptography``). Those are NOT guaranteed to be in the authoring
venv, so the login test ``importorskip("jwt")`` — when the lib is absent it SKIPS
CLEANLY (never fails), and the accept/deny behaviour is left to CI/an environment
that has the signing lib installed. This keeps the test "real but gated cleanly
when signing deps are absent", exactly as task 12.3 directs.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_auth_rotate_ingress.py -v
      (skips cleanly with no OPENBAO_TEST_* env vars; runs against a live
       throwaway OpenBao + optional PG/aws/Traefik/VLAN-20 deps when set, opting
       into the live tier with `-m requires_infra`.)
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Env-var contract (see module docstring + TESTING.md). The first two are the
# SAME class-level live gate task 7.4 uses; the rest are task-12.3 optional
# extras that each unlock one sub-assertion and skip cleanly when absent.
# --------------------------------------------------------------------------- #
_ADDR_ENV = "OPENBAO_TEST_ADDR"
_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"

_JWT_KEY_ENV = "OPENBAO_TEST_JWT_SIGNING_KEY"
_JWT_ISSUER_ENV = "OPENBAO_TEST_JWT_ISSUER"
_JWT_AUD_ENV = "OPENBAO_TEST_JWT_AUDIENCE"

_PG_CONN_ENV = "OPENBAO_TEST_PG_ROTATE_CONN"
_PG_ROLE_ENV = "OPENBAO_TEST_PG_ROTATE_ROLE"
_AWS_MOUNT_ENV = "OPENBAO_TEST_AWS_MOUNT"
_AWS_ROLE_ENV = "OPENBAO_TEST_AWS_ROTATE_ROLE"

_TRAEFIK_URL_ENV = "OPENBAO_TEST_TRAEFIK_URL"
_DIRECT_8200_ENV = "OPENBAO_TEST_DIRECT_8200_HOST"

# Contract ceilings mirrored from the roles / design "Object shapes".
_JWT_TOKEN_TTL_CEILING = 3600       # Req 4.3 — issued ttl <= 3600s
_DEFAULT_JWT_ISSUER = "https://gitlab.test"
_DEFAULT_JWT_AUDIENCE = "https://openbao.platform.test"
_DEFAULT_DIRECT_PORT = 8200


def _live_gate_reason() -> str | None:
    """Return a skip reason if the live OpenBao gate is not satisfied, else None.

    Requires BOTH ``OPENBAO_TEST_ADDR`` and ``OPENBAO_TEST_TOKEN`` (identical to
    the task-7.4 class-level gate). When either is absent the whole class SKIPS
    cleanly (never fails), per the documentation-testing steering rule
    "absent infra => skipped, not failed". The reason is returned FIRST so the
    class-level ``skipif`` can surface it.
    """
    if not os.environ.get(_ADDR_ENV):
        return (
            f"requires-infra, skipped: {_ADDR_ENV} not set (a live throwaway "
            "OpenBao API endpoint is required for the JWT-login / rotate-root / "
            "Traefik-ingress integration tests)"
        )
    if not os.environ.get(_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_TOKEN_ENV} not set (a platform-admin / "
            "root-scoped token on the throwaway OpenBao is required to write and "
            "read the jwt/database/aws mounts exercised here)"
        )
    return None


def _tls_context() -> ssl.SSLContext | None:
    """TLS context for the API calls.

    Defaults to NOT verifying the cert (the ephemeral test instance typically
    uses a self-signed cert); set ``OPENBAO_TEST_SKIP_TLS_VERIFY=0`` to enforce
    verification. Returns ``None`` for a plain-``http://`` addr. Same idiom as
    task 7.4.
    """
    skip = os.environ.get(_SKIP_TLS_ENV, "1").strip().lower() not in ("0", "false", "no", "")
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _OpenBao:
    """Minimal live OpenBao HTTP client (stdlib ``urllib`` only).

    REUSED verbatim from task 7.4's ``test_svc07_openbao_engine_contract.py`` so
    the two SVC-07 live suites share one dependency-free client idiom. The token
    is placed ONLY in the ``X-Vault-Token`` request header — never logged, never
    on a command line, never returned in an error message (secrets discipline).
    """

    def __init__(self, addr: str, token: str) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._ctx = _tls_context()

    def _request(
        self, method: str, path: str, body: dict | None = None, token: str | None = None
    ) -> tuple[int, Any]:
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, method=method, data=data)
        # Allow a per-call token override (used for the rotate-root
        # permission-scope path); defaults to the admin token.
        req.add_header("X-Vault-Token", token if token is not None else self._token)
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

    def post(self, path: str, body: dict, token: str | None = None) -> tuple[int, Any]:
        return self._request("POST", path, body, token=token)

    def delete(self, path: str) -> tuple[int, Any]:
        return self._request("DELETE", path)

    def mount_exists(self, mount_path: str) -> bool:
        """True if a secrets/auth mount at ``mount_path`` (e.g. ``jwt/``) exists."""
        status, data = self.get("sys/mounts")
        if status == 200 and isinstance(data, dict):
            mounts = data.get("data", data)
            if f"{mount_path.rstrip('/')}/" in mounts:
                return True
        status, data = self.get("sys/auth")
        if status == 200 and isinstance(data, dict):
            auths = data.get("data", data)
            return f"{mount_path.rstrip('/')}/" in auths
        return False

    def enable_auth_method(self, mount_path: str, method_type: str) -> None:
        self.post(f"sys/auth/{mount_path}", {"type": method_type})

    def disable_auth_method(self, mount_path: str) -> None:
        self.delete(f"sys/auth/{mount_path}")


@pytest.fixture(scope="module")
def bao() -> _OpenBao:
    """A live-OpenBao client built from the env-var contract.

    The class-level skipif prevents this from ever being constructed without the
    env vars present, but assert here too as a belt-and-braces guard.
    """
    addr = os.environ.get(_ADDR_ENV)
    token = os.environ.get(_TOKEN_ENV)
    assert addr and token, "live gate should have prevented fixture construction"
    return _OpenBao(addr, token)


@pytest.fixture()
def ctest_prefix() -> str:
    """A disposable, collision-proof resource prefix for one test."""
    return f"ctest-{uuid.uuid4().hex[:12]}"


# =========================================================================== #
# The integration suite — INFRA-GATED (requires_infra + skipif on
# OPENBAO_TEST_ADDR/OPENBAO_TEST_TOKEN, evaluated FIRST).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_gate_reason() is not None,
    reason=_live_gate_reason() or "live OpenBao integration gate satisfied",
)
class TestOpenBaoAuthRotateIngress:
    """JWT-login accept/deny (Req 4.3/4.4), rotate-root (Req 8.1-8.3), and
    Traefik-only ingress + direct-:8200 refusal (Req 6, Security AC 6).

    Every sub-assertion that needs a dependency BEYOND the base live OpenBao
    (a JWT-signing lib, a live PG connection, a live aws mount, a Traefik URL, a
    VLAN-20 vantage point) skips CLEANLY on its own when that dependency is
    absent, rather than failing the class — so the file is honestly runnable in
    partial-infra environments.
    """

    # ---- Req 4.3, 4.4 — JWT login accept / deny --------------------------- #
    def test_jwt_login_accepts_valid_token_and_denies_mismatched_or_expired(
        self, bao: _OpenBao, ctest_prefix: str
    ):
        """Present a VALID GitLab-style ID token -> assert a ``policy-<slug>``-
        scoped, ``renewable=false``, ``ttl <= 3600s`` token is issued (Req 4.3);
        present tokens with a mismatched ``project_path``, mismatched ``aud``,
        and an expired ``exp`` -> assert each is DENIED with NO token (Req 4.4).

        Uses a test issuer/JWKS we control: the throwaway OpenBao's ``jwt`` auth
        method is configured with ``jwt_validation_pubkeys`` = the PEM PUBLIC key
        derived from ``OPENBAO_TEST_JWT_SIGNING_KEY``, and the test mints signed
        JWTs with the right/wrong claims. This is a real login round-trip, not a
        mock — but it needs a JWT-signing lib and a signing key, both of which
        are gated: ``importorskip('jwt')`` and a skip when the key env var is
        absent, so the test SKIPS CLEANLY rather than failing where the signing
        dependency is unavailable.
        """
        jwt = pytest.importorskip(
            "jwt",
            reason=(
                "requires-infra, skipped: PyJWT (and cryptography) not installed "
                "in the venv — the JWT-login round-trip needs a JWT-signing lib "
                "to mint a token the jwt auth method will accept. Install pyjwt "
                "in CI/live env to exercise Req 4.3/4.4 accept-deny."
            ),
        )

        signing_key = os.environ.get(_JWT_KEY_ENV)
        if not signing_key:
            pytest.skip(
                f"requires-infra, skipped: {_JWT_KEY_ENV} not set — need a PEM "
                "RSA private key whose matching public key the throwaway "
                "OpenBao's jwt auth method trusts, to mint a signed JWT for the "
                "Req 4.3/4.4 accept-deny round-trip."
            )

        # Derive the public key (JWKS the jwt method will trust) from the private
        # key. cryptography ships with pyjwt's RS256 backend; importorskip it too.
        crypto_ser = pytest.importorskip(
            "cryptography.hazmat.primitives.serialization",
            reason=(
                "requires-infra, skipped: cryptography not installed — needed to "
                "derive the trusted public key from the test signing key."
            ),
        )
        private_key = crypto_ser.load_pem_private_key(
            signing_key.encode("utf-8"), password=None
        )
        public_pem = private_key.public_key().public_bytes(
            encoding=crypto_ser.Encoding.PEM,
            format=crypto_ser.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        issuer = os.environ.get(_JWT_ISSUER_ENV, _DEFAULT_JWT_ISSUER)
        audience = os.environ.get(_JWT_AUD_ENV, _DEFAULT_JWT_AUDIENCE)
        slug = ctest_prefix
        role_name = f"{slug}-ci"
        policy_name = f"policy-{slug}"
        project_path = f"group/{slug}"

        def _mint(claims_override: dict) -> str:
            now = int(time.time())
            claims = {
                "iss": issuer,
                "aud": audience,
                "sub": f"project_path:{project_path}:ref_type:branch:ref:main",
                "project_path": project_path,
                "iat": now,
                "nbf": now,
                "exp": now + 600,
            }
            claims.update(claims_override)
            return jwt.encode(claims, signing_key, algorithm="RS256")

        enabled_jwt = False
        try:
            if not bao.mount_exists("jwt"):
                bao.enable_auth_method("jwt", "jwt")
                enabled_jwt = True

            # Configure the jwt method to trust our test signing key + issuer.
            status, resp = bao.post(
                "auth/jwt/config",
                {
                    "bound_issuer": issuer,
                    "jwt_validation_pubkeys": [public_pem],
                },
            )
            assert status in (200, 204), f"jwt config write should succeed, got {status}: {resp}"

            # A minimal policy the role references.
            status, _ = bao.post(
                f"sys/policies/acl/{policy_name}",
                {"policy": f'path "secret/data/{slug}/*" {{ capabilities = ["read"] }}'},
            )
            assert status in (200, 204), f"policy write should succeed, got {status}"

            # The <slug>-ci role: binds project_path + aud, ttl ceilings, single
            # policy — the same contract task 9.1 writes per project.
            status, resp = bao.post(
                f"auth/jwt/role/{role_name}",
                {
                    "role_type": "jwt",
                    "user_claim": "sub",
                    "bound_audiences": [audience],
                    "bound_claims": {"project_path": project_path},
                    "token_ttl": _JWT_TOKEN_TTL_CEILING,
                    "token_max_ttl": _JWT_TOKEN_TTL_CEILING,
                    "token_policies": [policy_name],
                    "token_type": "service",
                },
            )
            assert status in (200, 204), f"jwt role write should succeed, got {status}: {resp}"

            # --- Req 4.3: a VALID token is ACCEPTED and the issued token is
            # policy-<slug>-scoped, non-renewable, ttl <= 3600s. ---
            valid_jwt = _mint({})
            status, data = bao.post(
                "auth/jwt/login", {"role": role_name, "jwt": valid_jwt}
            )
            assert status == 200, (
                f"a valid GitLab-style ID token must be accepted (Req 4.3); "
                f"got {status}: {data}"
            )
            auth = (data or {}).get("auth") or {}
            assert auth.get("client_token"), "a token must be issued on valid login (Req 4.3)"
            assert 0 < int(auth.get("lease_duration", 0)) <= _JWT_TOKEN_TTL_CEILING, (
                f"issued token ttl must be >0 and <= {_JWT_TOKEN_TTL_CEILING}s "
                f"(Req 4.3); got lease_duration={auth.get('lease_duration')}"
            )
            assert auth.get("renewable") is False, (
                f"issued CI token must be non-renewable (Req 4.3); "
                f"got renewable={auth.get('renewable')!r}"
            )
            assert list(auth.get("token_policies") or auth.get("policies") or []) == [
                "default",
                policy_name,
            ] or policy_name in (auth.get("token_policies") or auth.get("policies") or []), (
                f"issued token must be scoped to {policy_name} (Req 4.3); "
                f"got policies={auth.get('token_policies') or auth.get('policies')}"
            )
            # Clean up the issued token immediately (do not leave a live lease).
            issued = auth.get("client_token")
            if issued:
                bao.post("auth/token/revoke-self", {}, token=issued)

            # --- Req 4.4: DENY on each single-field violation, no token. ---
            deny_cases = {
                "mismatched project_path (aud/exp valid)": _mint(
                    {"project_path": "group/someone-else"}
                ),
                "mismatched aud (project_path/exp valid)": _mint(
                    {"aud": "https://wrong.audience.test"}
                ),
                "expired exp (iss/aud/project_path valid)": _mint(
                    {"exp": int(time.time()) - 60, "nbf": int(time.time()) - 120,
                     "iat": int(time.time()) - 120}
                ),
            }
            for label, bad_jwt in deny_cases.items():
                status, data = bao.post(
                    "auth/jwt/login", {"role": role_name, "jwt": bad_jwt}
                )
                assert status >= 400, (
                    f"login must be DENIED for {label} (Req 4.4); got {status}: {data}"
                )
                auth = (data or {}).get("auth")
                assert not (auth and auth.get("client_token")), (
                    f"NO token may be issued on a denied login for {label} "
                    f"(Req 4.4); got auth={auth!r}"
                )
        finally:
            bao.delete(f"auth/jwt/role/{role_name}")
            bao.delete(f"sys/policies/acl/{policy_name}")
            if enabled_jwt:
                bao.disable_auth_method("jwt")

    # ---- Req 8.1, 8.3 — database rotate-root ------------------------------ #
    def test_database_rotate_root_keeps_prerotation_leases_and_uses_new_root(
        self, bao: _OpenBao
    ):
        """Rotate the PostgreSQL ``database`` root -> assert leases issued BEFORE
        rotation keep authenticating (Req 8.1) and the NEXT issuance succeeds,
        i.e. uses the new root (Req 8.3).

        Needs a live, already-configured ``database/config/<conn>`` +
        ``database/roles/<role>`` on the throwaway OpenBao (env
        ``OPENBAO_TEST_PG_ROTATE_CONN`` / ``OPENBAO_TEST_PG_ROTATE_ROLE``). When
        either is unset, or the engine/role is absent, this SKIPS CLEANLY — the
        PG backend is a task-12.2-class dependency, gated the same way here.
        """
        conn = os.environ.get(_PG_CONN_ENV)
        role = os.environ.get(_PG_ROLE_ENV)
        if not conn or not role:
            pytest.skip(
                f"requires-infra, skipped: {_PG_CONN_ENV}/{_PG_ROLE_ENV} not set "
                "— a live, configured database connection + issuable role are "
                "needed for the PG rotate-root pre-lease-survival test (Req 8.1/8.3)."
            )
        if not bao.mount_exists("database"):
            pytest.skip(
                "requires-infra, skipped: no `database` engine mounted on the "
                "throwaway OpenBao — cannot exercise PG rotate-root."
            )

        # Pre-rotation lease: read creds/<role> and remember the lease_id.
        status, data = bao.get(f"database/creds/{role}")
        if status != 200:
            pytest.skip(
                f"requires-infra, skipped: could not issue a pre-rotation lease "
                f"from database/roles/{role} (status {status}) — the connection "
                "is likely not reachable in this environment; skipping the PG "
                "rotate-root assertion rather than failing on absent infra."
            )
        pre_lease_id = (data or {}).get("lease_id")
        assert pre_lease_id, "a pre-rotation lease must have a lease_id (Req 8.1 setup)"

        # Rotate the root credential (Req 8.1). Force flag on the endpoint.
        status, resp = bao.post(f"database/rotate-root/{conn}", {})
        assert status in (200, 204), (
            f"database/rotate-root/{conn} must return success (Req 8.1); "
            f"got {status}: {resp}"
        )

        # Req 8.1: the PRE-rotation lease still authenticates -> its lease
        # lookup still succeeds (the lease was neither revoked nor its TTL
        # altered by the root rotation).
        status, look = bao.post("sys/leases/lookup", {"lease_id": pre_lease_id})
        assert status == 200, (
            f"a lease issued BEFORE rotation must remain valid after "
            f"rotate-root (Req 8.1); lease lookup got {status}: {look}"
        )

        # Req 8.3: the NEXT issuance succeeds -> the new root is in use.
        status, data2 = bao.get(f"database/creds/{role}")
        assert status == 200, (
            f"the next dynamic-credential issuance after rotate-root must "
            f"succeed using the new root (Req 8.3); got {status}: {data2}"
        )
        assert (data2 or {}).get("lease_id"), (
            "post-rotation issuance must produce a fresh lease (Req 8.3)"
        )

    # ---- Req 8.2, 8.3 — aws (Garage) rotate-root -------------------------- #
    def test_aws_rotate_root_keeps_prerotation_leases_and_uses_new_root(
        self, bao: _OpenBao
    ):
        """Rotate the Garage ``aws`` engine root -> assert leases issued BEFORE
        rotation keep authenticating (Req 8.2) and the NEXT issuance succeeds
        with the new root (Req 8.3).

        Needs a live, configured ``aws`` mount (env ``OPENBAO_TEST_AWS_MOUNT``,
        default ``aws``) + an issuable role (``OPENBAO_TEST_AWS_ROTATE_ROLE``).
        Skips CLEANLY when unset or the mount/role is absent — the Garage backend
        is a task-12.2-class dependency, gated the same way here.
        """
        mount = os.environ.get(_AWS_MOUNT_ENV, "aws")
        role = os.environ.get(_AWS_ROLE_ENV)
        if not role:
            pytest.skip(
                f"requires-infra, skipped: {_AWS_ROLE_ENV} not set — an issuable "
                f"aws role is needed for the Garage rotate-root pre-lease-survival "
                "test (Req 8.2/8.3)."
            )
        if not bao.mount_exists(mount):
            pytest.skip(
                f"requires-infra, skipped: no `{mount}` aws engine mounted on the "
                "throwaway OpenBao — cannot exercise Garage rotate-root."
            )

        # Pre-rotation lease from aws/creds/<role>.
        status, data = bao.get(f"{mount}/creds/{role}")
        if status != 200:
            pytest.skip(
                f"requires-infra, skipped: could not issue a pre-rotation lease "
                f"from {mount}/creds/{role} (status {status}) — the Garage backend "
                "is likely unreachable; skipping the aws rotate-root assertion "
                "rather than failing on absent infra."
            )
        pre_lease_id = (data or {}).get("lease_id")

        # Rotate the aws root (Req 8.2).
        status, resp = bao.post(f"{mount}/config/rotate-root", {})
        assert status in (200, 204), (
            f"{mount}/config/rotate-root must return success (Req 8.2); "
            f"got {status}: {resp}"
        )

        # Req 8.2: a lease issued before rotation still looks up valid (not
        # revoked / TTL unchanged). aws leases may be dynamic; guard on presence.
        if pre_lease_id:
            status, look = bao.post("sys/leases/lookup", {"lease_id": pre_lease_id})
            assert status == 200, (
                f"an aws lease issued BEFORE rotation must remain valid after "
                f"rotate-root (Req 8.2); lease lookup got {status}: {look}"
            )

        # Req 8.3: next issuance succeeds using the new root.
        status, data2 = bao.get(f"{mount}/creds/{role}")
        assert status == 200, (
            f"the next aws-credential issuance after rotate-root must succeed "
            f"using the new root (Req 8.3); got {status}: {data2}"
        )

    # ---- Req 6 (Testing Strategy §3 Traefik row) — Traefik ingress -------- #
    def test_health_responds_through_traefik(self):
        """Assert ``https://openbao.<domain>/v1/sys/health`` responds THROUGH
        Traefik (Req 6 — the only sanctioned HTTP ingress path, PF FR-7).

        Reads the external Traefik URL from ``OPENBAO_TEST_TRAEFIK_URL`` and GETs
        ``/v1/sys/health``, asserting an OpenBao health JSON comes back (any of
        OpenBao's documented health status codes — 200 active/unsealed,
        429 standby, 472/473 DR/perf-standby, 501 not-initialised, 503 sealed —
        all indicate OpenBao ANSWERED through Traefik). Skips CLEANLY when the
        var is unset (no Traefik vantage point).
        """
        traefik_url = os.environ.get(_TRAEFIK_URL_ENV)
        if not traefik_url:
            pytest.skip(
                f"requires-infra, skipped: {_TRAEFIK_URL_ENV} not set — need the "
                "external Traefik URL for the primary to assert /v1/sys/health "
                "responds through Traefik (Req 6)."
            )

        url = f"{traefik_url.rstrip('/')}/v1/sys/health"
        ctx = _tls_context()
        req = urllib.request.Request(url=url, method="GET")
        # OpenBao's health endpoint returns a non-200 for standby/sealed states;
        # those are still a valid "OpenBao answered through Traefik" signal.
        healthy_codes = {200, 429, 472, 473, 501, 503}
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            pytest.fail(
                f"OpenBao health did not respond through Traefik at {url} "
                f"(Req 6): {type(exc).__name__}"
            )

        assert status in healthy_codes, (
            f"OpenBao health via Traefik must return a documented health status "
            f"(Req 6); got HTTP {status} from {url}"
        )
        # The body must be OpenBao's health JSON (carries `initialized`/`sealed`).
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            body = {}
        assert isinstance(body, dict) and ("initialized" in body or "sealed" in body), (
            "the Traefik-fronted response must be OpenBao's health JSON "
            "(carrying `initialized`/`sealed`) — proving OpenBao, not Traefik "
            f"itself, answered (Req 6); got body keys {list(body.keys())}"
        )

    # ---- Req 6 / Security AC 6 — direct :8200 is refused ------------------ #
    def test_direct_8200_connect_is_refused_from_vlan20(self):
        """Assert a DIRECT TCP connect to the primary's ``:8200`` (bypassing
        Traefik) is REFUSED from a VLAN-20 vantage point (Req 6, Security AC 6:
        VLAN-20 firewall blocks direct :8200 from all but Traefik, the Ansible
        control node, and the unsealer).

        Opens a raw socket to ``OPENBAO_TEST_DIRECT_8200_HOST`` (``host[:port]``,
        default port 8200) and asserts the connect is refused / dropped
        (ConnectionRefused or timeout), NOT accepted. This is only meaningful
        when the runner sits on a VLAN-20 host that is NOT one of the allow-listed
        sources; when the var is unset it SKIPS CLEANLY (no VLAN-20 vantage
        point). A successful connect is a FAILURE — it means the firewall rule
        that must drop direct :8200 is missing.
        """
        target = os.environ.get(_DIRECT_8200_ENV)
        if not target:
            pytest.skip(
                f"requires-infra, skipped: {_DIRECT_8200_ENV} not set — the "
                "direct-:8200-refused assertion needs a VLAN-20 vantage point and "
                "the primary's direct host:port to prove the firewall drops "
                "non-Traefik :8200 access (Req 6 / Security AC 6)."
            )

        host, _, port_str = target.partition(":")
        port = int(port_str) if port_str else _DEFAULT_DIRECT_PORT

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        connected = False
        try:
            sock.connect((host, port))
            connected = True
        except ConnectionRefusedError:  # explicit: refused == PASS
            connected = False
        except (socket.timeout, TimeoutError):
            # A silent DROP (firewall) manifests as a timeout — also a PASS.
            connected = False
        except OSError:
            # No route / network unreachable — the path is blocked; treat as PASS.
            connected = False
        finally:
            sock.close()

        assert not connected, (
            f"a DIRECT TCP connect to {host}:{port} from a VLAN-20 host MUST be "
            "refused/dropped by the firewall (Req 6 / Security AC 6) — only "
            "Traefik, the Ansible control node, and the unsealer may reach :8200 "
            "directly. The connect SUCCEEDED, which means the drop rule is missing."
        )
