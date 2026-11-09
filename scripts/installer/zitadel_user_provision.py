#!/usr/bin/env python3
"""SVC-06 org-scoped human-user provisioning helper (WS-C4, ADR-0023).

Feature: svc06-user-provisioning. Spec: .kiro/specs/svc06-user-provisioning/.

WHAT THIS IS
------------
ADR-0023 gives every project org a dedicated NON-HUMAN "user-manager" machine user that
holds ``ORG_USER_MANAGER`` on its single org (never ``IAM_OWNER``, never ``ORG_OWNER``)
and authenticates with a bounded-lifetime PAT stored only in the project's OpenBao path.
This helper implements the four operations around that identity, reusing the stdlib
``ZitadelClient`` transport from ``zitadel_provision.py``:

* ``mint-usermgr``   (instance ``IAM_OWNER`` token) create the machine user, grant it
  ``ORG_USER_MANAGER`` (and ONLY that), and mint a bounded PAT. Idempotent: an existing user
  is converged; a PAT is minted only when the user is new or the caller says the stored
  secret is missing (ZITADEL returns a PAT value once, at creation).
* ``create-user``    (per-org PAT) create a human login user in the PAT's OWN org. Idempotent:
  an existing username converges. Because no org header is sent, the call can only ever act on
  the token owner's own org, which is the least-privilege shape (path a and path b).
* ``remove-usermgr`` (instance token) teardown parity: delete the machine user (idempotent).
* ``probe-refusals`` (per-org PAT) acceptance helper: prove the credential CANNOT act on
  another org, create an org, or create a project.

SECRET HYGIENE
--------------
Tokens and the new user's password are read ONLY from environment variables, never argv:
``ZITADEL_PROVISION_TOKEN`` (instance), ``ZITADEL_USERMGR_TOKEN`` (per-org PAT) and
``ZITADEL_NEW_USER_PASSWORD``. The minted PAT is emitted ONLY by ``mint-usermgr`` (once, as JSON
on stdout, for the role to capture into OpenBao under ``no_log``). No secret is logged to stderr.
Stdlib only.

Exit codes: 0 ok; 2 usage / missing env; 3 ZITADEL API error.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from typing import Any

import zitadel_provision as zp

ZitadelClient = zp.ZitadelClient
ZitadelError = zp.ZitadelError

INSTANCE_TOKEN_ENV = zp.TOKEN_ENV_VAR  # ZITADEL_PROVISION_TOKEN
USERMGR_TOKEN_ENV = "ZITADEL_USERMGR_TOKEN"  # noqa: S105 - env var NAME, not a secret
NEW_USER_PASSWORD_ENV = "ZITADEL_NEW_USER_PASSWORD"  # noqa: S105 - env var NAME, not a secret

USERMGR_ROLE = "ORG_USER_MANAGER"
DEFAULT_USERMGR_NAME = "usermgr-sa"
DEFAULT_PAT_DAYS = 90

EP_USERS_SEARCH = "/management/v1/users/_search"
EP_USERS_MACHINE = "/management/v1/users/machine"
EP_USERS_HUMAN_IMPORT = "/management/v1/users/human/_import"
EP_ORG_MEMBERS = "/management/v1/orgs/me/members"
EP_ORG_MEMBERS_SEARCH = "/management/v1/orgs/me/members/_search"
_ALREADY_EXISTS = 409


def _user_ep(user_id: str, tail: str = "") -> str:
    return f"/management/v1/users/{user_id}{tail}"


def _http_status(exc: Exception) -> int | None:
    m = re.search(r"HTTP (\d{3})", str(exc))
    return int(m.group(1)) if m else None


def _find_user(client: Any, user_name: str, *, org_id: str | None = None) -> dict[str, Any] | None:
    status, body = client._request(
        "POST", EP_USERS_SEARCH,
        {"queries": [{"userNameQuery": {"userName": user_name, "method": "TEXT_QUERY_METHOD_EQUALS"}}]},
        org_id=org_id)
    for u in (body or {}).get("result", []) or []:
        if u.get("userName") == user_name:
            return u
    return None


def _pat_expiry(days: int, now: datetime.datetime | None = None) -> str:
    base = now or datetime.datetime.now(datetime.timezone.utc)
    return (base + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# mint-usermgr
# --------------------------------------------------------------------------- #
def _ensure_member_role(client: Any, org_id: str, user_id: str) -> str:
    """Make the user an org member with EXACTLY [ORG_USER_MANAGER]. Returns 'added'|'converged'|'ok'."""
    status, _ = client._request("POST", EP_ORG_MEMBERS,
                                {"userId": user_id, "roles": [USERMGR_ROLE]}, org_id=org_id)
    if status != _ALREADY_EXISTS:
        return "added"
    _s, body = client._request("POST", EP_ORG_MEMBERS_SEARCH,
                               {"queries": [{"userIdQuery": {"userId": user_id}}]}, org_id=org_id)
    members = (body or {}).get("result", []) or []
    roles = sorted(members[0].get("roles", [])) if members else []
    if roles == [USERMGR_ROLE]:
        return "ok"
    # Never leave an escalated grant behind: force exactly the least-privilege role.
    try:
        client._request("PUT", f"{EP_ORG_MEMBERS}/{user_id}", {"roles": [USERMGR_ROLE]}, org_id=org_id)
    except ZitadelError as exc:  # NotChanged is a benign 400
        if "NotChanged" not in str(exc) and "not changed" not in str(exc).lower():
            raise
    return "converged"


def ensure_usermgr(client: Any, org_id: str, user_name: str = DEFAULT_USERMGR_NAME, *,
                   have_secret: bool = False, pat_days: int = DEFAULT_PAT_DAYS,
                   now: datetime.datetime | None = None) -> dict[str, Any]:
    """Create/converge the per-org user-manager identity (ADR-0023). See module docstring."""
    existing = _find_user(client, user_name, org_id=org_id)
    created = existing is None
    if created:
        _s, body = client._request(
            "POST", EP_USERS_MACHINE,
            {"userName": user_name, "name": "User manager (platform)",
             "description": f"{USERMGR_ROLE} service account for this org (ADR-0023)",
             "accessTokenType": "ACCESS_TOKEN_TYPE_BEARER"}, org_id=org_id)
        user_id = body.get("userId", "")
        if not user_id:
            raise ZitadelError("machine-user create returned no userId")
    else:
        user_id = existing["id"]
    member = _ensure_member_role(client, org_id, user_id)
    result: dict[str, Any] = {
        "org_id": org_id, "user_id": user_id, "user_name": user_name, "created": created,
        "role": USERMGR_ROLE, "member": member, "pat_minted": False,
    }
    if created or not have_secret:
        # The PAT value is returned once; to (re)mint, drop stale PATs so none outlive their record.
        _s, body = client._request("POST", _user_ep(user_id, "/pats/_search"), {}, org_id=org_id)
        for p in (body or {}).get("result", []) or []:
            client._request("DELETE", _user_ep(user_id, f"/pats/{p['id']}"), None, org_id=org_id)
        expires = _pat_expiry(pat_days, now)
        _s, body = client._request("POST", _user_ep(user_id, "/pats"), {"expirationDate": expires}, org_id=org_id)
        token = body.get("token", "")
        if not token:
            raise ZitadelError("PAT mint returned no token")
        result.update({"pat_minted": True, "pat": token, "pat_expires": expires})
    return result


# --------------------------------------------------------------------------- #
# create-user (operator path a, external-app path b)
# --------------------------------------------------------------------------- #
def create_human_user(client: Any, user_name: str, first_name: str, last_name: str, email: str,
                      password: str, *, password_change_required: bool = True) -> dict[str, Any]:
    """Create a human login user in the TOKEN OWNER's own org. Idempotent. Never returns the password."""
    if not password:
        raise ValueError(f"a password is required; set {NEW_USER_PASSWORD_ENV} in the environment")
    existing = _find_user(client, user_name)
    if existing is not None:
        return {"user_id": existing["id"], "user_name": user_name, "created": False, "already_existed": True}
    status, body = client._request("POST", EP_USERS_HUMAN_IMPORT, {
        "userName": user_name,
        "profile": {"firstName": first_name, "lastName": last_name},
        "email": {"email": email, "isEmailVerified": True},
        "password": password,
        "passwordChangeRequired": password_change_required,
    })
    if status == _ALREADY_EXISTS:  # raced with another creator
        again = _find_user(client, user_name)
        return {"user_id": (again or {}).get("id", ""), "user_name": user_name, "created": False,
                "already_existed": True}
    return {"user_id": body.get("userId", ""), "user_name": user_name, "created": True, "already_existed": False}


# --------------------------------------------------------------------------- #
# remove-usermgr (teardown parity)
# --------------------------------------------------------------------------- #
def remove_usermgr(client: Any, org_id: str, user_name: str = DEFAULT_USERMGR_NAME) -> dict[str, Any]:
    existing = _find_user(client, user_name, org_id=org_id)
    if existing is None:
        return {"user_name": user_name, "removed": False, "already_absent": True}
    client._request("DELETE", _user_ep(existing["id"]), None, org_id=org_id)
    return {"user_name": user_name, "removed": True, "already_absent": False}


# --------------------------------------------------------------------------- #
# probe-refusals (acceptance: least-privilege proof)
# --------------------------------------------------------------------------- #
_REFUSED = (401, 403, 404)


def _attempt(client: Any, method: str, path: str, body: dict | None, org_id: str | None) -> dict[str, Any]:
    try:
        status, _ = client._request(method, path, body, org_id=org_id)
    except ZitadelError as exc:
        status = _http_status(exc) or 0
    return {"http": status, "refused": status in _REFUSED}


def probe_refusals(client: Any, other_org_id: str, probe_user: str = "usermgr-probe@invalid.example") -> dict[str, Any]:
    """With a per-org PAT, attempt actions it MUST NOT be able to perform. Creates nothing when refused."""
    human = {"userName": probe_user, "profile": {"firstName": "Probe", "lastName": "User"},
             "email": {"email": probe_user, "isEmailVerified": True}, "password": "Probe-Passw0rd!x1",
             "passwordChangeRequired": True}
    results = {
        "create_user_in_other_org": _attempt(client, "POST", EP_USERS_HUMAN_IMPORT, human, other_org_id),
        "create_org": _attempt(client, "POST", "/management/v1/orgs", {"name": "usermgr-probe-should-fail"}, None),
        "create_project_in_own_org": _attempt(client, "POST", zp.EP_PROJECTS, {"name": "usermgr-probe-project"}, None),
    }
    results["all_refused"] = all(v["refused"] for v in results.values())
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _token(env_name: str) -> str:
    tok = os.environ.get(env_name, "").strip()
    if not tok:
        print(f"ERROR: {env_name} is not set in the environment (tokens are never passed on argv).", file=sys.stderr)
        raise SystemExit(2)
    return tok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--host-header", default=None)
    ap.add_argument("--insecure", action="store_true", help="tolerate the dev self-signed cert")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint-usermgr", help=f"instance token ({INSTANCE_TOKEN_ENV})")
    m.add_argument("--org", required=True, help="organization NAME, e.g. proj-dronefleet")
    m.add_argument("--user", default=DEFAULT_USERMGR_NAME)
    m.add_argument("--pat-days", type=int, default=DEFAULT_PAT_DAYS)
    m.add_argument("--have-secret", action="store_true", help="the PAT is already stored; do not re-mint")
    c = sub.add_parser("create-user", help=f"per-org PAT ({USERMGR_TOKEN_ENV}); password from {NEW_USER_PASSWORD_ENV}")
    c.add_argument("--username", required=True)
    c.add_argument("--first-name", required=True)
    c.add_argument("--last-name", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--no-password-change", action="store_true", help="THROWAWAY test users only")
    r = sub.add_parser("remove-usermgr", help=f"instance token ({INSTANCE_TOKEN_ENV})")
    r.add_argument("--org", required=True)
    r.add_argument("--user", default=DEFAULT_USERMGR_NAME)
    p = sub.add_parser("probe-refusals", help=f"per-org PAT ({USERMGR_TOKEN_ENV}); acceptance only")
    p.add_argument("--other-org-id", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd in ("mint-usermgr", "remove-usermgr"):
            client = ZitadelClient(a.base_url, _token(INSTANCE_TOKEN_ENV), host_header=a.host_header, insecure=a.insecure)
            org_id = client._find_org_by_name(a.org)
            if not org_id:
                if a.cmd == "remove-usermgr":
                    out = {"user_name": a.user, "removed": False, "already_absent": True, "org_absent": True}
                    json.dump(out, sys.stdout, sort_keys=True)
                    sys.stdout.write("\n")
                    return 0
                print(f"ERROR: organization {a.org!r} not found; onboard the project first.", file=sys.stderr)
                return 3
            if a.cmd == "mint-usermgr":
                out = ensure_usermgr(client, org_id, a.user, have_secret=a.have_secret, pat_days=a.pat_days)
            else:
                out = remove_usermgr(client, org_id, a.user)
        else:
            client = ZitadelClient(a.base_url, _token(USERMGR_TOKEN_ENV), host_header=a.host_header, insecure=a.insecure)
            if a.cmd == "create-user":
                out = create_human_user(client, a.username, a.first_name, a.last_name, a.email,
                                        os.environ.get(NEW_USER_PASSWORD_ENV, ""),
                                        password_change_required=not a.no_password_change)
            else:
                out = probe_refusals(client, a.other_org_id)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ZitadelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    json.dump(out, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
