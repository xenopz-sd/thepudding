#!/usr/bin/env python3
"""ZITADEL org / project / OIDC-app provisioning helper (SVC-06).

Feature: svc-06-identity-provider (requirements FR-3/FR-6; design.md §3.4). An
idempotent, scriptable client for the ZITADEL management API that onboards a
platform project to SSO — create its organization, default roles, and an OIDC
application registration — WITHOUT manual console clicking, so it can be driven
from the `zitadel-org-provision` Ansible role / CI.

Design constraints (grounded in the platform's secret-hygiene + no-new-dep
rules):

* **Stdlib only.** HTTP is done with ``urllib.request`` — no ``requests`` (the
  control-node venv does not ship it, and this introduces no new runtime, per
  tech.md). This mirrors the other ``scripts/installer`` helpers.
* **No secret on argv (security-standards).** The service-account access token
  authenticating to the ZITADEL management API is read ONLY from the environment
  variable ``ZITADEL_PROVISION_TOKEN`` — NEVER a CLI flag (which would land in
  the process table / shell history). The caller (the role) supplies it in the
  child-process environment.
* **Idempotent (FR-3).** Every ``ensure_*`` is check-then-create: a name that
  already exists is treated as a no-op SUCCESS (ZITADEL returns HTTP 409 /
  gRPC code 6 ``ALREADY_EXISTS``, or a list lookup finds it), so a re-run makes
  no change and never errors.
* **Returns created secrets as JSON on stdout.** A newly-created OIDC app's
  ``client_id`` / ``client_secret`` are emitted as a JSON object on stdout for
  the role to capture into OpenBao. Nothing secret is logged to stderr. On a
  re-run where the app already exists, no secret is emitted (ZITADEL only returns
  a client secret at creation time) and the ``already_existed`` flag is set.

The management-API endpoint paths are kept as module constants with ZITADEL v1
management-API defaults; they are overridable so a version bump does not require
editing the logic. The unit tests mock the HTTP transport, so they validate the
idempotency + no-secret-on-argv + JSON-return CONTRACT, not a live instance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# Environment variable the service-account access token is read from. NEVER a
# CLI flag (no secret on argv — security-standards).
TOKEN_ENV_VAR = "ZITADEL_PROVISION_TOKEN"

# ZITADEL management-API endpoint paths (v1 management service defaults).
# Overridable constants so a version/path change does not touch the logic.
# Organizations moved to the resource-based v2 API in ZITADEL v4
# (verified against v4.18.0: /management/v1/orgs/_search is 404, while
# /v2/organizations and /v2/organizations/_search are the live endpoints).
# Projects / roles / OIDC apps remain under the management/v1 API in v4.
EP_ORGS = "/v2/organizations"
EP_ORGS_SEARCH = "/v2/organizations/_search"
EP_PROJECTS = "/management/v1/projects"
EP_PROJECTS_SEARCH = "/management/v1/projects/_search"
EP_PROJECT_ROLES = "/management/v1/projects/{project_id}/roles"
EP_OIDC_APPS = "/management/v1/projects/{project_id}/apps/oidc"
# ZITADEL API (resource-server) application endpoint. An API app is a different
# ZITADEL app shape from an OIDC app (SPA/WEB) — it carries no redirect URIs and
# no interactive grant/response types; it is created here, not under apps/oidc.
EP_API_APPS = "/management/v1/projects/{project_id}/apps/api"
EP_APPS_SEARCH = "/management/v1/projects/{project_id}/apps/_search"

# Valid (app_type, auth_method) combinations (design Component 2 — R2.3). Any
# pair not present here is rejected by validate_combination. The mapped value is
# a human-readable description used only for documentation; the keys are the
# authoritative accepted set.
VALID_COMBINATIONS = {
    ("spa", "none"): "public SPA / PKCE, no client secret",
    ("web", "basic"): "confidential web, client secret (DEFAULT)",
    ("api", "jwt"): "API resource server, JWT-profile key material",
}

# HTTP status ZITADEL returns when a resource already exists (gRPC code 6 maps
# to HTTP 409 Conflict). Treated as idempotent success.
_ALREADY_EXISTS_STATUS = 409
_DEFAULT_TIMEOUT = 30


class ZitadelError(Exception):
    """A non-recoverable ZITADEL API error (not an already-exists conflict)."""


class CombinationError(Exception):
    """An invalid (app_type, auth_method) combination was requested (R2.4).

    Raised by ``validate_combination`` for any pair not in ``VALID_COMBINATIONS``.
    The message names the offending pair so ``main`` can surface it on stderr.
    """


def validate_combination(app_type: str, auth_method: str) -> None:
    """Validate an (app_type, auth_method) pair against VALID_COMBINATIONS.

    Pure function (R2.3/R2.4). Returns ``None`` for a valid pair; raises
    ``CombinationError`` naming the pair for any combination not in the table —
    e.g. a public SPA cannot hold a secret (``spa``/``basic``), a confidential
    web app requires an auth method (``web``/``none``). Only the three documented
    combinations ``spa/none``, ``web/basic``, ``api/jwt`` are accepted.
    """
    if (app_type, auth_method) not in VALID_COMBINATIONS:
        raise CombinationError(
            f"invalid app-type/auth-method combination: {app_type}/{auth_method}"
        )
    return None


def build_app_request(app_type: str, auth_method: str, app_name: str,
                      redirect_uris: list[str]) -> tuple[str, dict[str, Any]]:
    """Build the (endpoint_template, request_body) for a validated combination.

    Pure function (design Component 3). This is the core of the PBT surface: it
    centralizes the previously-hardcoded WEB/BASIC body and adds the SPA and API
    shapes, keyed on the validated ``(app_type, auth_method)`` pair.

    * ``web``/``basic`` (DEFAULT) — MUST be byte-equivalent to the legacy
      hardcoded body: appType WEB + authMethodType BASIC, Authorization-Code
      grant + Code response, BEARER access token, posted to ``EP_OIDC_APPS``.
    * ``spa``/``none`` — public SPA/USER_AGENT + authMethodType NONE,
      Authorization-Code grant (PKCE by construction for a public user-agent
      client), NO client-secret-bearing field, posted to ``EP_OIDC_APPS``.
    * ``api``/``jwt`` — resource-server registration: just the name +
      API_AUTH_METHOD_TYPE_PRIVATE_KEY_JWT, NO redirect/grant/response types,
      posted to ``EP_API_APPS``.

    Raises ``CombinationError`` (via ``validate_combination``) for any pair not
    in ``VALID_COMBINATIONS``.
    """
    validate_combination(app_type, auth_method)

    if app_type == "web":
        # Byte-equivalent legacy confidential-web body (R2.5/R6.1 — golden).
        body: dict[str, Any] = {
            "name": app_name,
            "redirectUris": redirect_uris,
            "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
            "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
            "appType": "OIDC_APP_TYPE_WEB",
            "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",
            "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
        }
        return EP_OIDC_APPS, body

    if app_type == "spa":
        # Public SPA/PKCE (R1.1/R1.2): USER_AGENT + NONE. The Authorization-Code
        # grant for a public user-agent client is PKCE by construction; no
        # separate request field and NO client-secret-bearing field.
        body = {
            "name": app_name,
            "redirectUris": redirect_uris,
            "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
            "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
            "appType": "OIDC_APP_TYPE_USER_AGENT",
            "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",
            "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
        }
        return EP_OIDC_APPS, body

    # app_type == "api" (the only remaining valid type): resource server (R3.1).
    # No redirect URIs, no grant/response types — not an interactive client.
    body = {
        "name": app_name,
        "authMethodType": "API_AUTH_METHOD_TYPE_PRIVATE_KEY_JWT",
    }
    return EP_API_APPS, body


class ZitadelClient:
    """Thin idempotent ZITADEL management-API client (stdlib urllib)."""

    def __init__(self, base_url: str, token: str, *, org_id: str | None = None,
                 timeout: int = _DEFAULT_TIMEOUT, host_header: str | None = None,
                 insecure: bool = False):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            # Fail closed: no token means we cannot authenticate. The token is
            # read from the environment by the CLI, never passed on argv.
            raise ValueError(
                f"a ZITADEL access token is required; set {TOKEN_ENV_VAR} in the environment "
                "(never pass it on the command line)"
            )
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.org_id = org_id
        self.timeout = timeout
        # Dev/mock transport knobs (NOT secrets — safe on argv). In the
        # Traefik-less dev path the helper is pointed at the ZITADEL container's
        # own IP:8080 (plaintext) and must present the Host ZITADEL expects for
        # its configured external domain, exactly as svc06-health.yml does.
        # `insecure` tolerates the self-signed mock cert when base_url is https.
        self.host_header = host_header or None
        self._ssl_ctx = None
        if insecure:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

    # -- transport ---------------------------------------------------------- #
    def _request(self, method: str, path: str, body: dict | None = None,
                 *, org_id: str | None = None) -> tuple[int, dict[str, Any]]:
        """Perform one HTTP request. Returns (status, parsed-json-body).

        A 409 (already-exists) is returned to the caller as a normal (status,
        body) tuple, NOT raised — the ensure_* methods branch on it. Other 4xx/5xx
        raise ZitadelError. The access token goes in the Authorization header,
        never in the URL or a logged string.
        """
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # Dev/mock Host override: ZITADEL validates the request Host against its
        # configured external domain, so when we connect by container IP the Host
        # header must still name the external domain (same as svc06-health.yml).
        if self.host_header:
            req.add_header("Host", self.host_header)
        # Org context header when operating inside a specific organization.
        ctx_org = org_id if org_id is not None else self.org_id
        if ctx_org:
            req.add_header("x-zitadel-orgid", ctx_org)
        # Only pass an SSL context when one was built (dev/mock --insecure), so
        # the default path keeps urlopen's original signature (and test mocks
        # that stub urlopen without a `context` kwarg remain compatible).
        open_kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self._ssl_ctx is not None:
            open_kwargs["context"] = self._ssl_ctx
        try:
            with urllib.request.urlopen(req, **open_kwargs) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            parsed: dict[str, Any] = {}
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"message": raw}
            if exc.code == _ALREADY_EXISTS_STATUS or _is_already_exists(parsed):
                # Idempotent: surface as a 409 for the caller to treat as success.
                return _ALREADY_EXISTS_STATUS, parsed
            raise ZitadelError(
                f"ZITADEL API {method} {path} failed: HTTP {exc.code} {parsed.get('message', raw[:200])}"
            ) from None
        except urllib.error.URLError as exc:
            raise ZitadelError(f"ZITADEL API {method} {path} unreachable: {exc.reason}") from None

    # -- idempotent ensure_* ------------------------------------------------ #
    def ensure_org(self, name: str) -> tuple[str, bool]:
        """Create the organization ``name`` if absent. Returns (org_id, created).

        Idempotent: if an org with this name already exists, its id is looked up
        and returned with created=False (ALREADY_EXISTS is success, FR-3)."""
        existing = self._find_org_by_name(name)
        if existing is not None:
            return existing, False
        status, body = self._request("POST", EP_ORGS, {"name": name})
        if status == _ALREADY_EXISTS_STATUS:
            found = self._find_org_by_name(name)
            if found is None:
                raise ZitadelError(f"org {name!r} reported already-exists but could not be found")
            return found, False
        # v2 create returns `organizationId`; keep the v1 fallbacks for safety.
        org_id = str(body.get("organizationId") or body.get("id") or body.get("orgId") or "")
        if not org_id:
            raise ZitadelError(f"create org {name!r} returned no id: {body}")
        return org_id, True

    def ensure_project(self, name: str, org_id: str) -> tuple[str, bool]:
        """Create project ``name`` in ``org_id`` if absent. Returns (project_id, created)."""
        existing = self._find_project_by_name(name, org_id)
        if existing is not None:
            return existing, False
        status, body = self._request("POST", EP_PROJECTS, {"name": name}, org_id=org_id)
        if status == _ALREADY_EXISTS_STATUS:
            found = self._find_project_by_name(name, org_id)
            if found is None:
                raise ZitadelError(f"project {name!r} already-exists but not found")
            return found, False
        project_id = str(body.get("id") or body.get("projectId") or "")
        if not project_id:
            raise ZitadelError(f"create project {name!r} returned no id: {body}")
        return project_id, True

    def ensure_roles(self, project_id: str, org_id: str, roles: list[str]) -> list[str]:
        """Create each role key if absent. Returns the list of roles created now."""
        created: list[str] = []
        for role in roles:
            status, _ = self._request(
                "POST", EP_PROJECT_ROLES.format(project_id=project_id),
                {"roleKey": role, "displayName": role}, org_id=org_id,
            )
            if status != _ALREADY_EXISTS_STATUS:
                created.append(role)
        return created

    def ensure_oidc_app(self, project_id: str, org_id: str, app_name: str,
                        redirect_uris: list[str], *, app_type: str = "web",
                        auth_method: str = "basic") -> dict[str, Any]:
        """Create an OIDC/API application if absent. Returns an ADDITIVE dict:
        app_name, client_id, client_secret (only when newly created), app_type,
        auth_method, already_existed (bool).

        The request body is built by the pure ``build_app_request`` helper keyed
        on the validated ``(app_type, auth_method)`` pair (design Component 3/4),
        so the previously-hardcoded WEB/BASIC body now also covers the public SPA
        (no secret) and API (resource-server) shapes:

        * ``web``/``basic`` (DEFAULT) — byte-equivalent legacy confidential-web
          body posted to ``EP_OIDC_APPS``; emits the created ``client_secret``.
        * ``spa``/``none`` — public SPA/PKCE posted to ``EP_OIDC_APPS``; ZITADEL
          returns no secret for ``authMethodType=NONE``, so ``client_secret`` is
          always ``None`` (R1.3).
        * ``api``/``jwt`` — resource-server registration posted to ``EP_API_APPS``
          (see ``ensure_api_app`` / the ``apps/api`` branch below).

        ZITADEL returns the client secret ONLY at creation time, so a re-run
        (already-exists) emits no secret — the role keeps the secret it captured
        on first creation (idempotent, FR-3, R1.4/R3.4)."""
        # Validates the pair + selects endpoint/body (raises CombinationError on
        # an invalid pair — surfaced by provision()/main as exit 2).
        endpoint, body = build_app_request(app_type, auth_method, app_name, redirect_uris)

        existing = self._find_app_by_name(app_name, project_id, org_id)
        if existing is not None:
            return {"app_name": app_name, "client_id": existing, "client_secret": None,
                    "app_type": app_type, "auth_method": auth_method,
                    "already_existed": True}

        status, resp = self._request(
            "POST", endpoint.format(project_id=project_id), body, org_id=org_id,
        )
        if status == _ALREADY_EXISTS_STATUS:
            found = self._find_app_by_name(app_name, project_id, org_id)
            return {"app_name": app_name, "client_id": found, "client_secret": None,
                    "app_type": app_type, "auth_method": auth_method,
                    "already_existed": True}

        if app_type == "spa":
            # A public SPA never carries a client secret (authMethodType=NONE):
            # emit client_secret: null regardless of what the response carries.
            client_secret: str | None = None
        else:
            # web (BASIC) and api (JWT-profile) both emit created-once credential
            # material captured into the generic `client_secret` result key so
            # the role's per-app OpenBao capture path is unchanged. ZITADEL's API
            # app returns its key material under one of a few field names
            # depending on version; try the common ones (live-confirm, design
            # Open Risk 2) before falling back to the OIDC `clientSecret`.
            client_secret = (
                resp.get("clientSecret")
                or resp.get("clientKey")
                or resp.get("keyDetails")
                or None
            )
        return {
            "app_name": app_name,
            "client_id": resp.get("clientId"),
            "client_secret": client_secret,
            "app_type": app_type,
            "auth_method": auth_method,
            "already_existed": False,
        }

    def ensure_api_app(self, project_id: str, org_id: str, app_name: str,
                       *, auth_method: str = "jwt") -> dict[str, Any]:
        """Create an API (resource-server) application if absent (R3.1/R3.2).

        Thin convenience wrapper over ``ensure_oidc_app`` with ``app_type="api"``
        — the shared ensure path already routes an ``api`` app to ``EP_API_APPS``
        via ``build_app_request`` and captures the created-once credential
        material into the ``client_secret`` result key. A redirect-URI list is
        irrelevant for a resource server and is passed empty."""
        return self.ensure_oidc_app(project_id, org_id, app_name, [],
                                    app_type="api", auth_method=auth_method)

    # -- lookups ------------------------------------------------------------ #
    def _find_org_by_name(self, name: str) -> str | None:
        status, body = self._request("POST", EP_ORGS_SEARCH, {"queries": [{"nameQuery": {"name": name, "method": "TEXT_QUERY_METHOD_EQUALS"}}]})
        for item in (body.get("result") or []):
            if item.get("name") == name:
                return str(item.get("id") or "")
        return None

    def _find_project_by_name(self, name: str, org_id: str) -> str | None:
        status, body = self._request("POST", EP_PROJECTS_SEARCH, {"queries": [{"nameQuery": {"name": name, "method": "TEXT_QUERY_METHOD_EQUALS"}}]}, org_id=org_id)
        for item in (body.get("result") or []):
            if item.get("name") == name:
                return str(item.get("id") or "")
        return None

    def _find_app_by_name(self, name: str, project_id: str, org_id: str) -> str | None:
        status, body = self._request("POST", EP_APPS_SEARCH.format(project_id=project_id), {"queries": []}, org_id=org_id)
        for item in (body.get("result") or []):
            if item.get("name") == name:
                # The client id lives on the oidcConfig sub-object for OIDC apps.
                oidc = item.get("oidcConfig") or {}
                return str(oidc.get("clientId") or item.get("id") or "")
        return None


def _is_already_exists(body: dict[str, Any]) -> bool:
    """Detect ZITADEL's already-exists signal in a parsed error body (gRPC code 6
    or a message mentioning it)."""
    if not isinstance(body, dict):
        return False
    if body.get("code") == 6:
        return True
    msg = str(body.get("message", "")).lower()
    return "already exists" in msg or "already existing" in msg


def provision(base_url: str, org_name: str, project_name: str, app_name: str,
              redirect_uris: list[str], roles: list[str], token: str,
              *, app_type: str = "web", auth_method: str = "basic",
              host_header: str | None = None, insecure: bool = False) -> dict[str, Any]:
    """End-to-end idempotent provisioning. Returns a result dict suitable for
    JSON emission: org/project ids, created flags, roles created, and the OIDC
    app result (with client_secret only when newly created).

    ``app_type``/``auth_method`` select the ZITADEL app shape (design Component
    1-4); they default to the pre-existing confidential ``web``/``basic`` client
    so an existing caller that omits them is byte-for-byte unchanged (R6.1). The
    pair is validated up-front so an invalid combination raises
    ``CombinationError`` before any ZITADEL mutation (R2.4)."""
    validate_combination(app_type, auth_method)
    client = ZitadelClient(base_url, token, host_header=host_header, insecure=insecure)
    org_id, org_created = client.ensure_org(org_name)
    project_id, project_created = client.ensure_project(project_name, org_id)
    roles_created = client.ensure_roles(project_id, org_id, roles)
    app = client.ensure_oidc_app(project_id, org_id, app_name, redirect_uris,
                                 app_type=app_type, auth_method=auth_method)
    return {
        "org": {"name": org_name, "id": org_id, "created": org_created},
        "project": {"name": project_name, "id": project_id, "created": project_created},
        "roles_created": roles_created,
        "oidc_app": app,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI. Reads the access token from ZITADEL_PROVISION_TOKEN (never argv).
    Emits the provisioning result as JSON on stdout. Exit codes:
    * 0 — provisioned (idempotent) successfully.
    * 2 — missing token / bad arguments.
    * 3 — a ZITADEL API error (not an already-exists conflict).
    """
    parser = argparse.ArgumentParser(
        description="Idempotently provision a ZITADEL org/project/OIDC-app (SVC-06). "
                    f"The access token is read from the {TOKEN_ENV_VAR} env var, never a flag."
    )
    parser.add_argument("--base-url", required=True, help="ZITADEL base URL, e.g. https://auth.platform.example.test")
    parser.add_argument("--org", required=True, help="Organization name, e.g. proj-dronefleet")
    parser.add_argument("--project", required=True, help="Project name within the org")
    parser.add_argument("--app", required=True, help="OIDC application name")
    parser.add_argument("--redirect-uri", action="append", default=[], dest="redirect_uris",
                        help="OIDC redirect URI (repeatable)")
    parser.add_argument("--role", action="append", default=["admin", "member", "viewer"], dest="roles",
                        help="Project role key (repeatable; defaults admin/member/viewer)")
    # App-shape selection (design Component 1). --app-type defaults to `web`
    # (the pre-existing confidential-web client, backward compatible). When
    # --auth-method is omitted it derives from the app type (spa->none,
    # web->basic, api->jwt). argparse rejects out-of-domain values (R2.1/R2.2).
    parser.add_argument("--app-type", choices=["spa", "web", "api"], default="web",
                        dest="app_type",
                        help="OIDC application shape: spa (public/PKCE, no secret), "
                             "web (confidential, DEFAULT), api (resource server).")
    parser.add_argument("--auth-method", choices=["none", "basic", "jwt"], default=None,
                        dest="auth_method",
                        help="Auth method; derived from --app-type when omitted "
                             "(spa->none, web->basic, api->jwt).")
    # Dev/mock transport knobs (NOT secrets). In the Traefik-less dev path the
    # helper connects to the ZITADEL container IP:8080 and must present the
    # external-domain Host header; --insecure tolerates the self-signed mock cert.
    parser.add_argument("--host-header", default=None,
                        help="Override the HTTP Host header (dev/mock: name the external domain "
                             "while connecting by container IP). Not a secret.")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS verification (dev/mock self-signed cert only).")
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        print(f"ERROR: {TOKEN_ENV_VAR} is not set in the environment. The ZITADEL access token "
              "must be supplied via that env var, never on the command line.", file=sys.stderr)
        return 2

    # Derive the auth method from the app type when --auth-method is omitted
    # (design Component 1): spa->none, web->basic, api->jwt.
    _DEFAULT_AUTH_FOR = {"spa": "none", "web": "basic", "api": "jwt"}
    auth_method = args.auth_method or _DEFAULT_AUTH_FOR[args.app_type]

    # Validate the combination up-front so an invalid pair exits 2 naming the
    # pair before any ZITADEL mutation (R2.4).
    try:
        validate_combination(args.app_type, auth_method)
    except CombinationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        result = provision(args.base_url, args.org, args.project, args.app,
                           args.redirect_uris, args.roles, token,
                           app_type=args.app_type, auth_method=auth_method,
                           host_header=args.host_header, insecure=args.insecure)
    except CombinationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ZitadelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
