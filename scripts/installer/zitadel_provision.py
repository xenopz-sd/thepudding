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
import re
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
# fix-zitadel-onboarding-role-keys-assertion (§4): role search/delete + the
# project GET/PUT (UpdateProject) endpoints used to reconcile the role-key set to
# the declared vocabulary and to set/read projectRoleAssertion.
EP_PROJECT_ROLES_SEARCH = "/management/v1/projects/{project_id}/roles/_search"
EP_PROJECT_ROLE = "/management/v1/projects/{project_id}/roles/{role_key}"  # DELETE
EP_PROJECT = "/management/v1/projects/{project_id}"  # GET read-back / PUT update
EP_OIDC_APPS = "/management/v1/projects/{project_id}/apps/oidc"
# ZITADEL API (resource-server) application endpoint. An API app is a different
# ZITADEL app shape from an OIDC app (SPA/WEB) — it carries no redirect URIs and
# no interactive grant/response types; it is created here, not under apps/oidc.
EP_API_APPS = "/management/v1/projects/{project_id}/apps/api"
EP_APPS_SEARCH = "/management/v1/projects/{project_id}/apps/_search"
# OIDC app config update (loginVersion reconcile). Full-config PUT — ZITADEL
# requires the complete OIDC config body on update. Live-confirmed on v4.18.0:
# PUT returns 200 and loginVersion {"loginV1": {}} reads back.
EP_OIDC_APP_CONFIG = "/management/v1/projects/{project_id}/apps/{app_id}/oidc_config"
# Org login policy (dev-mail-catcher R7). GET returns the EFFECTIVE policy (`isDefault` true when
# the org inherits the instance default); AddCustomLoginPolicy (POST) and UpdateCustomLoginPolicy
# (PUT) take the same field set and the PUT is a FULL REPLACE, so a read-modify-write is mandatory.
# Field names verified against upstream proto/zitadel/management.proto and LIVE-CONFIRMED on ZITADEL
# v4.18.0 (WS-C16; tests/fixtures/live-shapes/zitadel-email-provider-smtp.json).
EP_LOGIN_POLICY = "/management/v1/policies/login"
# WS-C18 (default role on self-registration): Actions v1. Shapes live-confirmed on v4.18.0
# (tests/fixtures/live-shapes/zitadel-actions-default-role.json).
EP_ACTIONS = "/management/v1/actions"
EP_ACTIONS_SEARCH = "/management/v1/actions/_search"
EP_ACTION = "/management/v1/actions/{action_id}"
EP_FLOW_INTERNAL_AUTH = "/management/v1/flows/3"
EP_POST_CREATION_TRIGGER = "/management/v1/flows/3/trigger/3"
FLOW_INTERNAL_AUTH = "3"
TRIGGER_POST_CREATION = "3"
#: The platform-owned action. ZITADEL requires the script's function name to equal the action name.
DEFAULT_ROLE_ACTION_NAME = "platformDefaultRole"
_LOGIN_POLICY_FIELDS = (
    "allowUsernamePassword", "allowRegister", "allowExternalIdp", "forceMfa", "passwordlessType",
    "hidePasswordReset", "ignoreUnknownUsernames", "defaultRedirectUri", "passwordCheckLifetime",
    "externalLoginCheckLifetime", "mfaInitSkipLifetime", "secondFactorCheckLifetime",
    "multiFactorCheckLifetime", "allowDomainDiscovery", "disableLoginWithEmail", "disableLoginWithPhone",
)
# Instance Login-V2 feature flag. The DECIDING lever for which hosted login UI
# the authorize endpoint uses: with loginV2.required=true (v4 default,
# SOURCE_INSTANCE) authorize 302s to the SEPARATE /ui/v2/login container; set it
# false to fall back to the built-in Login V1 (/ui/login). Live-confirmed:
# PUT /v2/features/instance {"loginV2":{"required":false}} -> authorize 302s to
# /ui/login/login (HTTP 200 real form). Per-app loginVersion does NOT override it.
EP_FEATURES_INSTANCE = "/v2/features/instance"
# ZITADEL loginVersion oneof wire shapes (camelCase JSON). v1 = the built-in
# Go-templated /ui/login UI (platform default); v2 = the separate zitadel-login
# container (deferred svc06-login-v2).
LOGIN_VERSION_BODY = {
    "v1": {"loginV1": {}},
    "v2": {"loginV2": {"baseUri": ""}},
}

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


def _apply_login_version(body: dict[str, Any], login_version: str) -> None:
    """Add the ZITADEL ``loginVersion`` oneof to an OIDC app body in place.

    ``v1`` (platform default) pins the built-in Login-V1 hosted UI on the app;
    ``v2`` selects the separate login container (deferred). An unknown value
    raises ``ValueError`` so a typo fails closed rather than silently omitting
    the field. Only called for the web/spa shapes (API apps carry no login UI).
    """
    if login_version not in LOGIN_VERSION_BODY:
        raise ValueError(
            f"invalid login_version {login_version!r}; expected one of {sorted(LOGIN_VERSION_BODY)}"
        )
    body["loginVersion"] = LOGIN_VERSION_BODY[login_version]


def build_app_request(app_type: str, auth_method: str, app_name: str,
                      redirect_uris: list[str], *,
                      login_version: str = "v1",
                      dev_mode: bool | None = None) -> tuple[str, dict[str, Any]]:
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

    ``dev_mode`` (v0.14.0 feedback #3): ``None`` (default) OMITS ``devMode`` so
    the create body stays byte-identical to the pre-existing golden; a bool adds
    ``devMode`` to the web/spa shapes (ZITADEL needs it for non-https redirect
    URIs). API apps never carry it.

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
        _apply_login_version(body, login_version)
        if dev_mode is not None:
            body["devMode"] = bool(dev_mode)
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
        _apply_login_version(body, login_version)
        if dev_mode is not None:
            body["devMode"] = bool(dev_mode)
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
        """Create each role key if absent. Returns the list of roles created now.

        Create-only (additive). Kept for any caller that wants the historical
        behavior; ``provision()`` now uses ``reconcile_roles`` for exact-set
        semantics (fix-zitadel-onboarding-role-keys-assertion, §4)."""
        created: list[str] = []
        for role in roles:
            status, _ = self._request(
                "POST", EP_PROJECT_ROLES.format(project_id=project_id),
                {"roleKey": role, "displayName": role}, org_id=org_id,
            )
            if status != _ALREADY_EXISTS_STATUS:
                created.append(role)
        return created

    # -- role reconcile + project-role-assertion (§4) ----------------------- #
    def list_project_roles(self, project_id: str, org_id: str) -> list[str]:
        """Return the project's current role keys (via the roles _search endpoint)."""
        _, body = self._request(
            "POST", EP_PROJECT_ROLES_SEARCH.format(project_id=project_id),
            {"queries": []}, org_id=org_id,
        )
        keys: list[str] = []
        for item in (body.get("result") or []):
            key = item.get("key") or item.get("roleKey")
            if key:
                keys.append(str(key))
        return keys

    def reconcile_roles(self, project_id: str, org_id: str,
                        declared: list[str]) -> dict[str, Any]:
        """Make the project's role-key set EXACTLY ``declared`` (§4).

        create each declared key not present (idempotent on 409); DELETE each
        current key NOT in ``declared`` — notably ZITADEL's auto-created
        admin/member/viewer defaults. A role that is both declared and
        pre-existing is left untouched (preserving existing user grants). Returns
        ``{created, removed, final}``. Each removal is logged at INFO to stderr
        (a role key is non-secret)."""
        declared_set = list(dict.fromkeys(declared))  # de-dup, keep order
        current = set(self.list_project_roles(project_id, org_id))
        created: list[str] = []
        removed: list[str] = []
        for key in declared_set:
            if key not in current:
                status, _ = self._request(
                    "POST", EP_PROJECT_ROLES.format(project_id=project_id),
                    {"roleKey": key, "displayName": key}, org_id=org_id,
                )
                if status != _ALREADY_EXISTS_STATUS:
                    created.append(key)
        for key in sorted(current - set(declared_set)):
            # DELETE a non-declared role (already-absent / 404 is idempotent success).
            self._request(
                "DELETE",
                EP_PROJECT_ROLE.format(project_id=project_id, role_key=key),
                org_id=org_id,
            )
            removed.append(key)
            sys.stderr.write(f"INFO: removed non-declared project role {key!r}\n")
        final = sorted(set(self.list_project_roles(project_id, org_id)))
        return {"created": created, "removed": removed, "final": final}

    def get_project_role_assertion(self, project_id: str, org_id: str) -> bool:
        """Return the project's current ``projectRoleAssertion`` (via GET project)."""
        _, body = self._request(
            "GET", EP_PROJECT.format(project_id=project_id), org_id=org_id,
        )
        project = body.get("project") or body
        return bool(project.get("projectRoleAssertion", False))

    def set_project_role_assertion(self, project_id: str, org_id: str,
                                   enabled: bool, project_name: str) -> None:
        """Set ``projectRoleAssertion`` via the UpdateProject PUT (§4).

        UpdateProject replaces project settings, so the body echoes the other
        current toggles (read via GET) back unchanged so only assertion changes.
        Idempotent: setting the value it already holds is a no-op success."""
        _, body = self._request(
            "GET", EP_PROJECT.format(project_id=project_id), org_id=org_id,
        )
        current = body.get("project") or body
        update = {
            "name": project_name or current.get("name"),
            "projectRoleAssertion": bool(enabled),
            # Echo the other toggles unchanged so UpdateProject does not clobber them.
            "projectRoleCheck": bool(current.get("projectRoleCheck", False)),
            "hasProjectCheck": bool(current.get("hasProjectCheck", False)),
            "privateLabelingSetting": current.get(
                "privateLabelingSetting",
                "PRIVATE_LABELING_SETTING_UNSPECIFIED",
            ),
        }
        self._request(
            "PUT", EP_PROJECT.format(project_id=project_id), update, org_id=org_id,
        )

    def ensure_oidc_app(self, project_id: str, org_id: str, app_name: str,
                        redirect_uris: list[str], *, app_type: str = "web",
                        auth_method: str = "basic",
                        login_version: str = "v1",
                        dev_mode: bool | None = None) -> dict[str, Any]:
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
        endpoint, body = build_app_request(app_type, auth_method, app_name, redirect_uris,
                                           login_version=login_version, dev_mode=dev_mode)

        existing = self._find_app_by_name(app_name, project_id, org_id)
        if existing is not None:
            # Idempotent config reconcile (v0.14.0 feedback #3): an app that already
            # exists must CONVERGE to the manifest, not just newly-created apps.
            # Reconciles loginVersion (original), redirectUris and (when declared)
            # devMode via one full-config PUT, then a read-back fail-loud gate.
            # API apps have no redirect/login UI, so skip them.
            changed: list[str] = []
            if app_type in ("web", "spa"):
                changed = self._reconcile_app_config(
                    project_id, org_id, app_name, redirect_uris,
                    app_type, auth_method, login_version, dev_mode)
            return {"app_name": app_name, "client_id": existing, "client_secret": None,
                    "app_type": app_type, "auth_method": auth_method,
                    "login_version": login_version,
                    "login_version_reconciled": "loginVersion" in changed,
                    "redirect_uris_reconciled": "redirectUris" in changed,
                    "dev_mode_reconciled": "devMode" in changed,
                    "config_reconciled": bool(changed),
                    "reconciled_fields": changed,
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
            "login_version": (login_version if app_type in ("web", "spa") else None),
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

    def _find_app_id_and_oidc(self, name: str, project_id: str,
                              org_id: str) -> tuple[str | None, dict[str, Any]]:
        """Return (app_id, oidcConfig) for ``name`` — app_id is the ZITADEL
        resource id (needed for the config-update PUT), distinct from the OIDC
        clientId that ``_find_app_by_name`` returns. ``({}, None)`` when absent."""
        status, body = self._request(
            "POST", EP_APPS_SEARCH.format(project_id=project_id), {"queries": []}, org_id=org_id)
        for item in (body.get("result") or []):
            if item.get("name") == name:
                return str(item.get("id") or ""), (item.get("oidcConfig") or {})
        return None, {}

    def _reconcile_app_config(self, project_id: str, org_id: str, app_name: str,
                              redirect_uris: list[str], app_type: str,
                              auth_method: str, login_version: str,
                              dev_mode: bool | None = None) -> list[str]:
        """Converge an existing web/spa app to the declared config via a
        full-config PUT to ``EP_OIDC_APP_CONFIG``. Returns the list of drifted
        field names that were fixed (``[]`` = already correct, NO PUT, idempotent).

        Compared fields: ``redirectUris`` (set-equal; order is not drift),
        ``loginVersion``, and ``devMode`` ONLY when ``dev_mode`` is declared. An
        undeclared ``dev_mode`` carries the live ``devMode`` over into the PUT so a
        URI-only fix never silently resets it. After the PUT a read-back gate
        raises ``ZitadelError`` unless the live config now matches (ZITADEL
        requires the complete config on update, so the body is rebuilt from the
        same ``build_app_request`` a create uses)."""
        app_id, oidc = self._find_app_id_and_oidc(app_name, project_id, org_id)
        if not app_id:
            return []
        desired_login = LOGIN_VERSION_BODY.get(login_version)
        if desired_login is None:
            raise ValueError(
                f"invalid login_version {login_version!r}; expected one of {sorted(LOGIN_VERSION_BODY)}")
        drift: list[str] = []
        if set(oidc.get("redirectUris") or []) != set(redirect_uris):
            drift.append("redirectUris")
        if (oidc.get("loginVersion") or None) != desired_login:
            drift.append("loginVersion")
        if dev_mode is not None and bool(oidc.get("devMode", False)) != bool(dev_mode):
            drift.append("devMode")
        if not drift:
            return []  # already correct — no PUT (idempotent)
        effective_dev = dev_mode if dev_mode is not None else oidc.get("devMode")
        _endpoint, body = build_app_request(
            app_type, auth_method, app_name, redirect_uris, login_version=login_version,
            dev_mode=(bool(effective_dev) if effective_dev is not None else None))
        body.pop("name", None)  # the update endpoint takes the config fields only
        self._request("PUT", EP_OIDC_APP_CONFIG.format(project_id=project_id, app_id=app_id),
                      body, org_id=org_id)
        # Read-back FAIL-LOUD gate: never report success on an unconverged app.
        _id2, live = self._find_app_id_and_oidc(app_name, project_id, org_id)
        if set(live.get("redirectUris") or []) != set(redirect_uris):
            raise ZitadelError(
                f"OIDC app {app_name!r} redirect URIs did not converge after reconcile: "
                f"live {sorted(live.get('redirectUris') or [])}, declared {sorted(set(redirect_uris))}")
        if (live.get("loginVersion") or None) != desired_login:
            raise ZitadelError(f"OIDC app {app_name!r} loginVersion did not converge after reconcile")
        if dev_mode is not None and bool(live.get("devMode", False)) != bool(dev_mode):
            raise ZitadelError(
                f"OIDC app {app_name!r} devMode did not converge after reconcile: "
                f"live {bool(live.get('devMode', False))}, declared {bool(dev_mode)}")
        return drift

    def ensure_allow_register(self, org_id: str, value: bool) -> dict[str, Any]:
        """Converge the org login policy's ``allowRegister`` to ``value`` (read-modify-write).

        The login-policy PUT replaces EVERY field, so the body is rebuilt from the verified
        field whitelist read back from the current effective policy; only ``allowRegister``
        changes. ``forceMfaLocalOnly`` is echoed only when true (never silently downgraded).
        If the effective value already equals ``value`` nothing is written (an org that
        inherits the instance default is not given a custom policy for nothing). After a write
        a read-back FAIL-LOUD gate verifies ``allowRegister`` AND that password login was not
        switched off (lock-out guard)."""
        _, body = self._request("GET", EP_LOGIN_POLICY, org_id=org_id)
        policy = body.get("policy") or {}
        current = bool(policy.get("allowRegister", False))
        is_default = bool(policy.get("isDefault", False))
        if current == bool(value):
            return {"changed": False, "allow_register": current, "custom_policy": not is_default}
        update = {k: policy[k] for k in _LOGIN_POLICY_FIELDS if k in policy}
        if policy.get("forceMfaLocalOnly"):
            update["forceMfaLocalOnly"] = True
        update["allowRegister"] = bool(value)
        method = "POST" if is_default else "PUT"
        self._request(method, EP_LOGIN_POLICY, update, org_id=org_id)
        _, after_body = self._request("GET", EP_LOGIN_POLICY, org_id=org_id)
        after = after_body.get("policy") or {}
        if bool(after.get("allowRegister", False)) != bool(value):
            raise ZitadelError(
                f"org login policy allowRegister did not converge: have {bool(after.get('allowRegister', False))}, "
                f"declared {bool(value)}")
        if policy.get("allowUsernamePassword") and not after.get("allowUsernamePassword"):
            raise ZitadelError("org login policy update switched password login OFF; refusing (lock-out guard)")
        return {"changed": True, "allow_register": bool(value), "custom_policy": True, "via": method}

    def ensure_default_role(self, project_id: str, org_id: str, role: str | None) -> dict[str, Any]:
        """Reconcile the platform-owned Post Creation action that grants ``role`` on ``project_id``.
        ZITADEL has no built-in default-role grant for self-registered users; the sanctioned
        mechanism is an Actions v1 action on the Internal Authentication flow, trigger Post
        Creation, that pushes a user grant. ``role`` set => create/update ONE action named
        :data:`DEFAULT_ROLE_ACTION_NAME` and make sure it is on the trigger, PRESERVING any other
        action already there (the trigger's list is replaced on write). ``role`` None => detach and
        delete only our action. The role must exist in the project. A read-back FAIL-LOUD gate
        verifies the result; nothing is written when already converged."""
        if role is not None and role not in self.list_project_roles(project_id, org_id):
            raise ZitadelError(f"default role {role!r} is not a role of the project; declare it in sso.roles first")
        ours = self._find_default_role_action(org_id)
        attached = self._post_creation_action_ids(org_id)
        if role is None:
            if ours is None:
                return {"changed": False, "role": None, "action_id": ""}
            keep = [i for i in attached if i != ours["id"]]
            if ours["id"] in attached:
                self._request("POST", EP_POST_CREATION_TRIGGER, {"actionIds": keep}, org_id=org_id)
            self._request("DELETE", EP_ACTION.format(action_id=ours["id"]), org_id=org_id)
            if self._find_default_role_action(org_id) is not None or \
                    (ours["id"] in self._post_creation_action_ids(org_id)):
                raise ZitadelError("default-role action removal did not converge")
            return {"changed": True, "role": None, "action_id": ""}
        script = self._default_role_script(project_id, role)
        body = {"name": DEFAULT_ROLE_ACTION_NAME, "script": script, "timeout": "10s", "allowedToFail": True}
        changed = False
        if ours is None:
            _, created = self._request("POST", EP_ACTIONS, body, org_id=org_id)
            action_id = str(created.get("id") or "")
            if not action_id:
                raise ZitadelError("create action returned no id")
            changed = True
        else:
            action_id = ours["id"]
            if ours.get("script") != script:
                self._request("PUT", EP_ACTION.format(action_id=action_id), body, org_id=org_id)
                changed = True
        if action_id not in attached:
            self._request("POST", EP_POST_CREATION_TRIGGER, {"actionIds": attached + [action_id]}, org_id=org_id)
            changed = True
        final = self._find_default_role_action(org_id)
        if final is None or final.get("script") != script or action_id not in self._post_creation_action_ids(org_id):
            raise ZitadelError(
                "default-role action did not converge (missing, wrong script, or not on the Post Creation trigger)")
        return {"changed": changed, "role": role, "action_id": action_id}
    @staticmethod
    def _default_role_script(project_id: str, role: str) -> str:
        # Only identifier-safe values may be spliced into the JavaScript (single-quoted, as live-verified);
        # anything else is refused rather than escaped.
        for what, value in (("project id", project_id), ("role key", role)):
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value or ""):
                raise ZitadelError(f"refusing to build the default-role script: unsafe {what} {value!r}")
        return (f"function {DEFAULT_ROLE_ACTION_NAME}(ctx, api) "
                f"{{ api.userGrants.push({{ projectID: '{project_id}', roles: ['{role}'] }}); }}")
    def _find_default_role_action(self, org_id: str) -> dict[str, Any] | None:
        _, body = self._request("POST", EP_ACTIONS_SEARCH, {}, org_id=org_id)
        for item in (body.get("result") or []):
            if item.get("name") == DEFAULT_ROLE_ACTION_NAME:
                return {"id": str(item.get("id") or ""), "script": item.get("script")}
        return None
    def _post_creation_action_ids(self, org_id: str) -> list[str]:
        _, body = self._request("GET", EP_FLOW_INTERNAL_AUTH, org_id=org_id)
        ids: list[str] = []
        for trig in ((body.get("flow") or {}).get("triggerActions") or []):
            if str((trig.get("triggerType") or {}).get("id")) == TRIGGER_POST_CREATION:
                ids.extend(str(a.get("id")) for a in (trig.get("actions") or []) if a.get("id"))
        return ids
    def set_instance_login_v2_required(self, required: bool) -> bool:
        """Set the INSTANCE Login-V2 feature flag. This is the deciding lever for
        which hosted login UI the authorize endpoint uses (per-app loginVersion
        does NOT override it — live-confirmed). ``required=False`` falls the
        instance back to the built-in Login V1 (/ui/login), the platform default
        until the svc06-login-v2 container is deployed. Idempotent: reads the
        current value first and returns False (no change) when already set.
        Returns True when a PUT was applied."""
        status, body = self._request("GET", EP_FEATURES_INSTANCE)
        current = ((body.get("loginV2") or {}).get("required"))
        if bool(current) == bool(required):
            return False
        self._request("PUT", EP_FEATURES_INSTANCE, {"loginV2": {"required": bool(required)}})
        return True


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
              project_role_assertion: bool = True,
              login_version: str = "v1", set_instance_login_v2: bool | None = None,
              dev_mode: bool | None = None, allow_register: bool | None = None,
              default_role: str | None = None, no_default_role: bool = False,
              host_header: str | None = None, insecure: bool = False) -> dict[str, Any]:
    """End-to-end idempotent provisioning. Returns a result dict suitable for
    JSON emission: org/project ids, created flags, the reconciled role set, the
    assertion flag, and the OIDC app result (with client_secret only when newly
    created).

    ``app_type``/``auth_method`` select the ZITADEL app shape (design Component
    1-4); they default to the pre-existing confidential ``web``/``basic`` client
    so an existing caller that omits them is byte-for-byte unchanged (R6.1). The
    pair is validated up-front so an invalid combination raises
    ``CombinationError`` before any ZITADEL mutation (R2.4).

    fix-zitadel-onboarding-role-keys-assertion (§4): the project's role-key set
    is RECONCILED to exactly ``roles`` (create missing + remove ZITADEL's
    auto-created admin/member/viewer defaults) and ``projectRoleAssertion`` is set
    to ``project_role_assertion`` (default True). A read-back FAIL-LOUD gate then
    verifies both, raising ``ZitadelError`` (→ exit 3) unless the role-key set
    equals ``roles`` AND assertion matches — so onboarding never reports success
    while RBAC cannot work for the declared roles."""
    validate_combination(app_type, auth_method)
    client = ZitadelClient(base_url, token, host_header=host_header, insecure=insecure)
    org_id, org_created = client.ensure_org(org_name)
    project_id, project_created = client.ensure_project(project_name, org_id)
    reconcile = client.reconcile_roles(project_id, org_id, roles)
    client.set_project_role_assertion(project_id, org_id, project_role_assertion, project_name)
    app = client.ensure_oidc_app(project_id, org_id, app_name, redirect_uris,
                                 app_type=app_type, auth_method=auth_method,
                                 login_version=login_version, dev_mode=dev_mode)

    # Fail-loud read-back gate (§4): the project's role keys must equal the
    # declared set AND assertion must match, else raise before reporting success.
    final_keys = set(client.list_project_roles(project_id, org_id))
    assertion = client.get_project_role_assertion(project_id, org_id)
    if final_keys != set(roles):
        raise ZitadelError(
            "project role keys do not match the declared vocabulary after "
            f"reconcile: have {sorted(final_keys)}, declared {sorted(set(roles))}"
        )
    if assertion != project_role_assertion:
        raise ZitadelError(
            "projectRoleAssertion read-back "
            f"({assertion}) does not match the requested value ({project_role_assertion})"
        )
    # Instance Login-V2 lever (the deciding factor for the hosted login UI).
    # Default behaviour (set_instance_login_v2 is None): DISABLE the instance
    # loginV2 feature whenever the app pins v1 — so a single run makes the
    # built-in V1 login actually route (per-app pin alone does not). An
    # explicit True/False overrides (the svc06-login-v2 path passes True).
    if set_instance_login_v2 is None:
        instance_v2 = (login_version == "v2")
    else:
        instance_v2 = bool(set_instance_login_v2)
    instance_login_v2_changed = client.set_instance_login_v2_required(instance_v2)
    # Optional org login-policy toggle (None = untouched; backward compatible).
    login_policy = client.ensure_allow_register(org_id, allow_register) if allow_register is not None else None
    # Optional default role for self-registered users (WS-C18). default_role None and not
    # no_default_role => untouched; no_default_role => remove our action.
    default_role_result = (client.ensure_default_role(project_id, org_id, None if no_default_role else default_role)
                           if (default_role is not None or no_default_role) else None)
    result = {
        "org": {"name": org_name, "id": org_id, "created": org_created},
        "project": {"name": project_name, "id": project_id, "created": project_created},
        # roles_created retained (now from the reconcile) so downstream JSON
        # consumers are not broken; roles_reconciled + project_role_assertion added.
        "roles_created": reconcile["created"],
        "roles_reconciled": reconcile,
        "project_role_assertion": assertion,
        "oidc_app": app,
        "instance_login_v2_required": instance_v2,
        "instance_login_v2_changed": instance_login_v2_changed,
    }
    if login_policy is not None:
        result["login_policy"] = login_policy
    if default_role_result is not None:
        result["default_role"] = default_role_result
    return result


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
    parser.add_argument("--role", action="append", default=[], dest="roles",
                        help="Project role key (repeatable). When none are given, the "
                             "documented default set (admin/member/viewer) is applied. "
                             "The project's role-key set is RECONCILED to exactly the "
                             "declared keys (non-declared ZITADEL defaults removed).")
    # fix-zitadel-onboarding-role-keys-assertion (§4): enable/disable
    # projectRoleAssertion so the provisioned roles actually reach the token.
    # Default enabled (the safe default; the pre-fix reality left it off = the bug).
    parser.add_argument("--project-role-assertion", dest="project_role_assertion",
                        action="store_true", default=True,
                        help="Enable projectRoleAssertion so roles reach the token (DEFAULT).")
    parser.add_argument("--no-project-role-assertion", dest="project_role_assertion",
                        action="store_false",
                        help="Disable projectRoleAssertion.")
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
    # Hosted-login UI version pinned on the app (design §3.7). Default v1 — the
    # built-in /ui/login UI (platform default). v2 selects the separate
    # zitadel-login container (deferred svc06-login-v2). Not a secret.
    parser.add_argument("--login-version", choices=["v1", "v2"], default="v1",
                        dest="login_version",
                        help="OIDC app hosted-login UI version: v1 (built-in /ui/login, "
                             "DEFAULT) or v2 (separate login container, deferred).")
    # Instance Login-V2 feature lever. Default (flag absent) = DISABLE the
    # instance loginV2 feature when --login-version is v1 (so V1 actually routes;
    # per-app pin alone does not override the instance default). --set-instance-
    # login-v2 forces it on (the svc06-login-v2 path); --no-set-instance-login-v2
    # forces it off regardless of --login-version.
    inst = parser.add_mutually_exclusive_group()
    inst.add_argument("--set-instance-login-v2", dest="set_instance_login_v2",
                      action="store_true", default=None,
                      help="Force the instance loginV2 feature ON (deploy the V2 login container first).")
    inst.add_argument("--no-set-instance-login-v2", dest="set_instance_login_v2",
                      action="store_false",
                      help="Force the instance loginV2 feature OFF (fall back to built-in V1).")
    # OIDC devMode (v0.14.0 feedback #3). Tri-state: absent = leave the live value
    # untouched (and omit it from the create body); --dev-mode / --no-dev-mode
    # declare it and are RECONCILED on re-run. Needed by ZITADEL for non-https
    # redirect URIs (dev only). Not a secret.
    dm = parser.add_mutually_exclusive_group()
    dm.add_argument("--dev-mode", dest="dev_mode", action="store_true", default=None,
                    help="Declare OIDC devMode=true (allows non-https redirect URIs; dev only).")
    dm.add_argument("--no-dev-mode", dest="dev_mode", action="store_false",
                    help="Declare OIDC devMode=false.")
    # Org login policy `allowRegister` (dev-mail-catcher R7). Tri-state: absent = untouched.
    ar = parser.add_mutually_exclusive_group()
    ar.add_argument("--allow-register", dest="allow_register", action="store_true", default=None,
                    help="Declare the org login policy allowRegister=true (self-registration on).")
    ar.add_argument("--no-allow-register", dest="allow_register", action="store_false",
                    help="Declare the org login policy allowRegister=false (self-registration off).")
    # Default project role for self-registered users (WS-C18). Absent = untouched.
    dr = parser.add_mutually_exclusive_group()
    dr.add_argument("--default-role", dest="default_role", default=None, metavar="ROLE_KEY",
                    help="Grant ROLE_KEY on this project to every user created in the org (Actions v1 Post Creation).")
    dr.add_argument("--no-default-role", dest="no_default_role", action="store_true", default=False,
                    help="Remove the platform-owned default-role action.")
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

    # Apply the documented default role vocabulary when none were declared, so a
    # bare CLI call stays backward compatible (§4).
    roles = args.roles or ["admin", "member", "viewer"]

    try:
        result = provision(args.base_url, args.org, args.project, args.app,
                           args.redirect_uris, roles, token,
                           app_type=args.app_type, auth_method=auth_method,
                           project_role_assertion=args.project_role_assertion,
                           login_version=args.login_version,
                           set_instance_login_v2=args.set_instance_login_v2,
                           dev_mode=args.dev_mode, allow_register=args.allow_register,
                           default_role=args.default_role, no_default_role=args.no_default_role,
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
