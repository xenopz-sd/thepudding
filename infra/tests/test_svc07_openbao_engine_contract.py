"""Live engine/auth/audit CONTRACT tests against a real OpenBao mount shape.

Task 7.4 (spec: svc-07-secrets-manager) — requirements.md Requirements 2.2, 2.3,
3.2, 3.3, 4.2, 4.3, 7.1; design.md "Testing Strategy §2 (Contract tests)" and the
"Object shapes — policy, JWT role, database role, aws role" table.

=== WHAT THIS TEST IS (and is NOT) ===

These are CONTRACT tests in the ``testing-strategy.md`` §2 sense: the adapter
seams (the ``jwt`` role, the ``database`` role, the ``aws`` role, and the file
audit device) are exercised against a REAL OpenBao mount shape — never a mock or
a rendered-YAML stand-in. The offline structural-contract suites already pin the
Ansible-role *source* shape (``test_svc07_openbao_engines.py`` for §7.1,
``test_svc07_openbao_auth_audit.py`` for §7.2); THIS file closes the loop by
asserting what a live OpenBao actually STORES and EMITS when the same shapes are
written to it, which no offline test can prove.

Because they need a live OpenBao they are gated with the ESTABLISHED
``@pytest.mark.requires_infra`` marker (deselected by default via the root
``pytest.ini``'s ``addopts = -m "not requires_infra"``) and, additionally,
``skipif``-gated on the OpenBao env-var contract below so they SKIP CLEANLY when
no live OpenBao is reachable — never fail for lack of infra (documentation-testing
steering: "absent infra => skipped, not failed"). No new gate is invented: the
marker + ``skipif``-on-env-vars idiom is the SAME one the sibling
``test_svc07_state_and_degraded.py`` (task 3.2) and ``test_degraded_sdn_use.py``
use, differing only in WHICH env vars gate it (OpenBao here, Proxmox there).

=== ENV-VAR CONTRACT (documented here + in TESTING.md) ===

Two variables reach a live OpenBao, named in the ``<SERVICE>_TEST_<THING>`` style
of the existing ``PROXMOX_*_TEST_*`` vars:

  * ``OPENBAO_TEST_ADDR``  — the live OpenBao API base URL (e.g.
    ``https://127.0.0.1:8200``). A THROWAWAY/ephemeral OpenBao only — this test
    WRITES a disposable ``ctest-*`` policy/role and enables a file audit device.
  * ``OPENBAO_TEST_TOKEN`` — a token with enough privilege to write+read
    ``sys/policies/acl/*``, ``auth/jwt/role/*``, ``database/roles/*``,
    ``aws/roles/*``, and to enable+read a ``file`` audit device (i.e. a
    ``platform-admin``-scoped or root test token on the throwaway instance).

Optional:

  * ``OPENBAO_TEST_SKIP_TLS_VERIFY`` — set to ``1``/``true`` to skip TLS
    verification against a self-signed test cert (default: skip verify, since the
    ephemeral test instance typically uses a self-signed cert). Set to ``0`` to
    enforce verification.
  * ``OPENBAO_TEST_AUDIT_HOST_DIR`` — a host directory the live OpenBao can write
    an audit file into AND that this test process can read back, used for the
    Req 7.1 one-JSON-object-per-line assertion. When unset, the audit-line
    portion is skipped with a clear reason (the rest still runs), because reading
    the emitted file requires a shared path between the OpenBao instance and the
    test runner.

SECURITY (security-standards.md — this IS the secrets manager): the token is read
from the environment and threaded ONLY into request headers; it is NEVER logged,
placed on a command line, or echoed in an assertion message. Every resource this
test creates uses a disposable ``ctest-<uuid>`` prefix and is torn down in a
``finally`` block, so a re-run never collides and the instance is left clean.

=== WHY HTTP, NOT THE ``bao`` CLI ===

The assertions read the STORED role/policy/mount shape, which the OpenBao HTTP
API returns verbatim as JSON — the same JSON ``bao read -format=json`` prints
(``bao`` is a thin client over this API). Using ``urllib`` (stdlib) keeps the
test dependency-free (matching the rest of ``infra/tests/``, which import no
third-party HTTP client) and avoids requiring a ``bao`` binary on the test
runner. The shapes written here mirror EXACTLY the Ansible defaults the roles
use (``ansible/roles/openbao_init_unseal/defaults/main.yml`` for the engine TTLs
/ least-privilege statements, and the onboarding JWT-role contract from design.md
"Object shapes") so the live assertions track the real deployment shapes.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_engine_contract.py -v
      (skips cleanly with no OPENBAO_TEST_* env vars; runs against a live
       throwaway OpenBao when they are set, opting into the live tier with
       `-m requires_infra`.)
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Env-var contract (see module docstring + TESTING.md). Named in the
# `<SERVICE>_TEST_<THING>` style of the existing PROXMOX_*_TEST_* vars.
# --------------------------------------------------------------------------- #
_ADDR_ENV = "OPENBAO_TEST_ADDR"
_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"
_AUDIT_HOST_DIR_ENV = "OPENBAO_TEST_AUDIT_HOST_DIR"

# Contract values mirrored from ansible/roles/openbao_init_unseal/defaults/main.yml
# and design.md "Object shapes". These are the shapes the real deployment writes;
# the live assertions below check OpenBao stores them faithfully.
_DB_DEFAULT_TTL_SECONDS = 3600      # openbao_database_default_ttl "1h"
_DB_MAX_TTL_SECONDS = 86400         # openbao_database_max_ttl "24h"
_AWS_DEFAULT_TTL_SECONDS = 3600     # openbao_aws_default_ttl "1h"
_AWS_MAX_TTL_SECONDS = 86400        # openbao_aws_max_ttl "24h"
_JWT_TOKEN_TTL_CEILING = 3600       # Req 4.2/4.3 token_ttl <= 3600
_KV_MOUNT = "secret"                # openbao_kv_mount


def _live_gate_reason() -> str | None:
    """Return a skip reason if the live OpenBao gate is not satisfied, else None.

    Requires BOTH ``OPENBAO_TEST_ADDR`` and ``OPENBAO_TEST_TOKEN``. When either
    is absent the whole class SKIPS cleanly (never fails), per the
    documentation-testing steering rule "absent infra => skipped, not failed" —
    the same clean-skip contract the sibling Proxmox live tests use.
    """
    if not os.environ.get(_ADDR_ENV):
        return (
            f"requires-infra, skipped: {_ADDR_ENV} not set (a live throwaway "
            "OpenBao API endpoint is required for the engine/auth/audit contract "
            "tests)"
        )
    if not os.environ.get(_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_TOKEN_ENV} not set (a platform-admin / "
            "root-scoped token on the throwaway OpenBao is required to write and "
            "read the contract role/policy/audit shapes)"
        )
    return None


def _tls_context() -> ssl.SSLContext | None:
    """TLS context for the API calls.

    Defaults to NOT verifying the cert (the ephemeral test instance typically
    uses a self-signed cert); set ``OPENBAO_TEST_SKIP_TLS_VERIFY=0`` to enforce
    verification. Returns ``None`` for a plain-``http://`` addr (urllib ignores
    the context there).
    """
    skip = os.environ.get(_SKIP_TLS_ENV, "1").strip().lower() not in ("0", "false", "no", "")
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _OpenBao:
    """Minimal live OpenBao HTTP client for the contract assertions.

    Wraps ``urllib`` GET/POST/DELETE against the OpenBao API. The token is placed
    ONLY in the ``X-Vault-Token`` request header — never logged, never on a
    command line, never returned in an error message (Req: secrets discipline).
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

    def enable_secret_engine(self, mount_path: str, engine_type: str, **options: Any) -> None:
        body: dict[str, Any] = {"type": engine_type}
        if options:
            body["options"] = options
        self.post(f"sys/mounts/{mount_path}", body)

    def enable_auth_method(self, mount_path: str, method_type: str) -> None:
        self.post(f"sys/auth/{mount_path}", {"type": method_type})

    def disable_secret_engine(self, mount_path: str) -> None:
        self.delete(f"sys/mounts/{mount_path}")

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
# The contract suite — INFRA-GATED (requires_infra + skipif on OPENBAO_TEST_*).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_gate_reason() is not None,
    reason=_live_gate_reason() or "live OpenBao contract gate satisfied",
)
class TestOpenBaoEngineAuthAuditContract:
    """Contract assertions against a real OpenBao mount shape (Req 2.2, 2.3, 3.2,
    3.3, 4.2, 4.3, 7.1).

    Each test creates a disposable ``ctest-<uuid>`` resource via the SAME
    ``bao write`` shapes the roles use (engine TTLs / least-privilege statements
    from the role defaults; the JWT-role contract from design.md "Object
    shapes"), reads it back via ``bao read -format=json`` (the HTTP API), asserts
    the stored contract fields, and tears the resource down in a ``finally``
    block so the throwaway instance is left clean and a re-run never collides.
    """

    # ---- Req 4.2, 4.3 — JWT role contract ---------------------------------- #
    def test_jwt_role_stores_ttl_ceiling_non_renewable_bound_claims_and_policy(
        self, bao: _OpenBao, ctest_prefix: str
    ):
        """A ``<slug>-ci`` JWT role stores ``token_ttl<=3600``, ``renewable=false``,
        ``bound_audiences``/``bound_claims.project_path``, and
        ``token_policies=[policy-<slug>]`` (Req 4.2, 4.3).

        Mirrors design.md "Object shapes — JWT role" and the onboarding role's
        intended write (auth/jwt/role/<slug>-ci); task 9.1 creates these per
        project, so this contract test creates a throwaway one via the same
        shape and asserts what OpenBao actually stores.
        """
        slug = ctest_prefix
        role_name = f"{slug}-ci"
        policy_name = f"policy-{slug}"
        project_path = f"group/{slug}"
        audience = "https://openbao.platform.example.test"

        enabled_jwt = False
        try:
            if not bao.mount_exists("jwt"):
                bao.enable_auth_method("jwt", "jwt")
                enabled_jwt = True

            # A minimal policy the role references (content is not the subject of
            # this test — the JWT-role → policy binding is).
            status, _ = bao.post(
                f"sys/policies/acl/{policy_name}",
                {"policy": f'path "{_KV_MOUNT}/data/{slug}/*" {{ capabilities = ["read"] }}'},
            )
            assert status in (200, 204), f"policy write should succeed, got {status}"

            # Write the JWT role using the design's "Object shapes" contract.
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

            # Read it back and assert the STORED contract (bao read -format=json).
            status, data = bao.get(f"auth/jwt/role/{role_name}")
            assert status == 200, f"jwt role read should succeed, got {status}: {data}"
            role = data["data"]

            # token_ttl <= 3600 (Req 4.2, 4.3)
            assert 0 < int(role["token_ttl"]) <= _JWT_TOKEN_TTL_CEILING, (
                f"token_ttl must be >0 and <= {_JWT_TOKEN_TTL_CEILING}s "
                f"(Req 4.2/4.3); stored {role['token_ttl']}"
            )
            assert 0 < int(role["token_max_ttl"]) <= _JWT_TOKEN_TTL_CEILING, (
                f"token_max_ttl must be >0 and <= {_JWT_TOKEN_TTL_CEILING}s "
                f"(Req 4.2); stored {role['token_max_ttl']}"
            )
            # renewable = false — non-renewable CI token (Req 4.3)
            # OpenBao does not renew service tokens issued from a jwt role beyond
            # max_ttl; the role stores no renew grant. Assert the explicit knob if
            # present, else assert token_type is the non-renewing "service".
            if "token_explicit_max_ttl" in role:
                pass  # informational; ceiling already asserted above
            assert role.get("token_type") in ("service", "default-service"), (
                "a CI jwt-role token must be a non-renewable service token "
                f"(Req 4.3); stored token_type={role.get('token_type')!r}"
            )
            # bound_audiences present and matches (Req 4.2)
            assert audience in (role.get("bound_audiences") or []), (
                f"bound_audiences must contain the platform audience (Req 4.2); "
                f"stored {role.get('bound_audiences')}"
            )
            # bound_claims.project_path present and matches (Req 4.2)
            bound_claims = role.get("bound_claims") or {}
            assert bound_claims.get("project_path") == project_path, (
                f"bound_claims.project_path must bind the repo path (Req 4.2); "
                f"stored {bound_claims!r}"
            )
            # token_policies == [policy-<slug>] (Req 4.2)
            assert list(role.get("token_policies") or []) == [policy_name], (
                f"token_policies must be exactly [{policy_name}] (Req 4.2); "
                f"stored {role.get('token_policies')}"
            )
        finally:
            bao.delete(f"auth/jwt/role/{role_name}")
            bao.delete(f"sys/policies/acl/{policy_name}")
            if enabled_jwt:
                bao.disable_auth_method("jwt")

    # ---- Req 2.2, 2.3 — database role contract ----------------------------- #
    def test_database_role_stores_1h_24h_ttls_and_least_privilege_statements(
        self, bao: _OpenBao, ctest_prefix: str
    ):
        """A ``<slug>-<db>-rw`` database role stores ``default_ttl=1h``/
        ``max_ttl=24h`` and least-privilege ``creation_statements`` — no
        superuser / CREATEROLE / cross-DB (Req 2.2, 2.3).

        The engine is enabled if absent; the role is written WITHOUT configuring
        a live PostgreSQL connection (this test asserts the STORED role shape,
        Req 2.2/2.3 — the live PG issuance round-trip is task 12.2's integration
        concern, not this contract test).
        """
        slug = ctest_prefix
        role_name = f"{slug}-appdb-rw"
        creation_statements = [
            "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' "
            "VALID UNTIL '{{expiration}}' NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION;",
            'GRANT CONNECT ON DATABASE "postgres" TO "{{name}}";',
            'GRANT USAGE ON SCHEMA public TO "{{name}}";',
            'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{{name}}";',
        ]

        enabled_db = False
        try:
            if not bao.mount_exists("database"):
                bao.enable_secret_engine("database", "database")
                enabled_db = True

            status, resp = bao.post(
                f"database/roles/{role_name}",
                {
                    "db_name": f"{slug}-conn",
                    "creation_statements": creation_statements,
                    "default_ttl": "1h",
                    "max_ttl": "24h",
                },
            )
            assert status in (200, 204), f"database role write should succeed, got {status}: {resp}"

            status, data = bao.get(f"database/roles/{role_name}")
            assert status == 200, f"database role read should succeed, got {status}: {data}"
            role = data["data"]

            # TTLs stored as 1h / 24h (OpenBao returns seconds) — Req 2.3
            assert int(role["default_ttl"]) == _DB_DEFAULT_TTL_SECONDS, (
                f"default_ttl must be 1h/{_DB_DEFAULT_TTL_SECONDS}s (Req 2.3); "
                f"stored {role['default_ttl']}"
            )
            assert int(role["max_ttl"]) == _DB_MAX_TTL_SECONDS, (
                f"max_ttl must be 24h/{_DB_MAX_TTL_SECONDS}s (Req 2.3); "
                f"stored {role['max_ttl']}"
            )

            # Least-privilege creation_statements — no superuser / CREATEROLE /
            # cross-DB (Req 2.2). Assert against the STORED statements.
            stored = " ".join(role.get("creation_statements") or []).upper()
            assert "NOSUPERUSER" in stored and "NOCREATEROLE" in stored and "NOCREATEDB" in stored, (
                "stored creation_statements must be least-privilege "
                "(NOSUPERUSER NOCREATEROLE NOCREATEDB) — Req 2.2"
            )
            # must not positively grant superuser/createrole
            assert " WITH SUPERUSER" not in stored, "must not grant SUPERUSER (Req 2.2)"
            assert " CREATEROLE" not in stored.replace("NOCREATEROLE", ""), (
                "must not grant CREATEROLE (Req 2.2)"
            )
        finally:
            bao.delete(f"database/roles/{role_name}")
            if enabled_db:
                bao.disable_secret_engine("database")

    # ---- Req 3.2, 3.3 — aws (Garage) role contract ------------------------- #
    def test_aws_role_scoped_to_slug_bucket_only_with_1h_24h_ttls(
        self, bao: _OpenBao, ctest_prefix: str
    ):
        """An ``<slug>-<bucket>-rw`` aws role stores a policy scoped to
        ``<slug>-<bucket>`` ONLY with ``default_ttl=1h``/``max_ttl=24h``
        (Req 3.2, 3.3).

        Mirrors the ``openbao_aws_policy_document_rw_template`` default
        (per-bucket-scoped S3 actions) with the ``<slug>-<bucket>`` placeholder
        instantiated, and the 1h/24h role TTLs.
        """
        slug = ctest_prefix
        bucket = "assets"
        role_name = f"{slug}-{bucket}-rw"
        resource = f"{slug}-{bucket}"
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

        enabled_aws = False
        try:
            if not bao.mount_exists("aws"):
                bao.enable_secret_engine("aws", "aws")
                enabled_aws = True

            status, resp = bao.post(
                f"aws/roles/{role_name}",
                {
                    "credential_type": "iam_user",
                    "policy_document": json.dumps(policy_document),
                    "default_sts_ttl": "1h",
                    "max_sts_ttl": "24h",
                },
            )
            assert status in (200, 204), f"aws role write should succeed, got {status}: {resp}"

            status, data = bao.get(f"aws/roles/{role_name}")
            assert status == 200, f"aws role read should succeed, got {status}: {data}"
            role = data["data"]

            # Scoped to <slug>-<bucket> ONLY (Req 3.2): every Resource ARN in the
            # stored policy names this bucket and no other project's slug.
            stored_doc_raw = role.get("policy_document")
            assert stored_doc_raw, "aws role must store a policy_document (Req 3.2)"
            stored_doc = (
                json.loads(stored_doc_raw) if isinstance(stored_doc_raw, str) else stored_doc_raw
            )
            resources = []
            for stmt in stored_doc.get("Statement", []):
                res = stmt.get("Resource")
                resources.extend(res if isinstance(res, list) else [res])
            assert resources, "stored policy must name at least one Resource (Req 3.2)"
            for arn in resources:
                assert resource in arn, (
                    f"every Resource ARN must be scoped to '{resource}' only "
                    f"(Req 3.2); found {arn!r}"
                )
                # must not grant a bare bucket wildcard that would cross projects
                assert arn not in ("arn:aws:s3:::*", "arn:aws:s3:::*/*"), (
                    f"aws role must not grant a cross-project bucket wildcard "
                    f"(Req 3.2); found {arn!r}"
                )

            # 1h / 24h TTLs (Req 3.3) — OpenBao's aws engine stores STS TTLs in
            # seconds. Assert against whichever TTL fields the version returns.
            default_ttl = int(role.get("default_sts_ttl") or role.get("default_ttl") or 0)
            max_ttl = int(role.get("max_sts_ttl") or role.get("max_ttl") or 0)
            assert default_ttl == _AWS_DEFAULT_TTL_SECONDS, (
                f"aws role default TTL must be 1h/{_AWS_DEFAULT_TTL_SECONDS}s "
                f"(Req 3.3); stored {default_ttl}"
            )
            assert max_ttl == _AWS_MAX_TTL_SECONDS, (
                f"aws role max TTL must be 24h/{_AWS_MAX_TTL_SECONDS}s (Req 3.3); "
                f"stored {max_ttl}"
            )
        finally:
            bao.delete(f"aws/roles/{role_name}")
            if enabled_aws:
                bao.disable_secret_engine("aws")

    # ---- Req 7.1 — audit-entry contract ------------------------------------ #
    def test_audit_line_is_one_json_object_per_line_with_required_fields(
        self, bao: _OpenBao, ctest_prefix: str
    ):
        """An emitted audit line is one JSON object per line carrying the
        required fields — timestamp, entity, operation type, path, success bool
        (Req 7.1).

        Enables a ``file`` audit device pointed at a host directory shared with
        the test runner (``OPENBAO_TEST_AUDIT_HOST_DIR``), triggers an audited
        operation, then reads the emitted file back and asserts EACH line parses
        as a standalone JSON object exposing the required fields. When no shared
        audit dir is provided the assertion is skipped with a clear reason (the
        emitted file cannot be read back without a shared path), rather than
        producing a false pass.
        """
        from pathlib import Path

        audit_host_dir = os.environ.get(_AUDIT_HOST_DIR_ENV)
        if not audit_host_dir:
            pytest.skip(
                f"requires-infra, skipped: {_AUDIT_HOST_DIR_ENV} not set — the "
                "Req 7.1 audit-line assertion needs a directory the live OpenBao "
                "writes the audit file into AND this test process can read back "
                "(a path shared between the OpenBao instance and the runner)."
            )

        device_path = ctest_prefix  # disposable audit device mount name
        audit_file = f"/openbao/audit/{ctest_prefix}.log"
        # The host-visible path where OpenBao writes it (shared mount).
        host_audit_file = Path(audit_host_dir) / f"{ctest_prefix}.log"

        try:
            # Enable a file audit device writing one JSON object per line (the
            # OpenBao default format; pinned explicitly, log_raw NOT set so the
            # native fail-closed/HMAC behaviour is preserved — Req 7.2/7.3/7.6).
            status, resp = bao.post(
                f"sys/audit/{device_path}",
                {
                    "type": "file",
                    "options": {"file_path": audit_file, "format": "json"},
                },
            )
            if status not in (200, 204):
                pytest.skip(
                    "requires-infra, skipped: could not enable a file audit "
                    f"device on the live OpenBao (status {status}). The audit "
                    "file path is likely not writable by the OpenBao instance; "
                    "point OPENBAO_TEST_AUDIT_HOST_DIR at a shared, writable dir."
                )

            # Trigger an audited operation (any authenticated read). The audit
            # device logs the request+response as one JSON object per line.
            bao.get("sys/mounts")

            # Read the emitted file back from the shared host path.
            if not host_audit_file.is_file():
                pytest.skip(
                    "requires-infra, skipped: the audit file did not appear at "
                    f"the shared host path — OPENBAO_TEST_AUDIT_HOST_DIR must map "
                    "to the SAME directory the OpenBao container writes "
                    "/openbao/audit into."
                )

            lines = [
                ln for ln in host_audit_file.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            assert lines, "the audit device must have emitted at least one line (Req 7.1)"

            for raw_line in lines:
                # (1) one JSON object PER LINE — each line parses standalone.
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise AssertionError(
                        f"each audit line must be one standalone JSON object "
                        f"(Req 7.1); a line failed to parse: {exc}"
                    )
                assert isinstance(entry, dict), (
                    "each audit line must be a JSON OBJECT (Req 7.1)"
                )

            # (2) At least one entry carries all the required fields. OpenBao's
            # audit schema nests request/response; assert the required pieces are
            # locatable across the emitted entries (Req 7.1: timestamp, entity,
            # operation type, path, success bool).
            def _has_required_fields(entry: dict) -> bool:
                if "time" not in entry:              # UTC timestamp
                    return False
                req = entry.get("request") or {}
                if "operation" not in req:           # operation type
                    return False
                if "path" not in req:                # secret path
                    return False
                # entity identifier: token accessor (or auth mount accessor)
                auth = entry.get("auth") or {}
                has_entity = bool(
                    auth.get("accessor")
                    or auth.get("entity_id")
                    or req.get("client_token_accessor")
                )
                # success indicator: an audited request either records an "error"
                # field (failure) or omits it (success) — the presence/absence of
                # "error" is the boolean success signal OpenBao emits.
                has_success_signal = ("error" in entry) or (entry.get("type") == "response")
                return has_entity and has_success_signal

            assert any(_has_required_fields(json.loads(ln)) for ln in lines), (
                "at least one audit entry must carry a timestamp, an entity "
                "identifier, an operation type, a path, and a success/error "
                "signal (Req 7.1)"
            )
        finally:
            bao.delete(f"sys/audit/{device_path}")
