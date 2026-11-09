#!/usr/bin/env python3
"""ZITADEL Login-V1 interactive-login probe + org MFA login-policy toggle (SVC-06 AC-4).

Feature: svc06 live re-acceptance (WS-A2). Spec: .kiro/specs/svc06-mfa-live-acceptance/.

WHY THIS EXISTS
---------------
AC-4 ("MFA enforceable via org login policy") was only "config verified". The locked
acceptance (PLANNING.md WS-A2) is to prove MFA actually BLOCKS a login live: correct
primary credentials but no second factor must be DENIED while the org policy enforces MFA,
and the same flow must succeed when it does not (the control). This module drives the
built-in Login V1 UI (the platform default, constitution decision-log) with a cookie jar
and reports whether an authorization ``code`` was issued, plus a tiny helper to set / read
back / reset the org login policy.

SAFETY
------
* Stdlib only (mirrors zitadel_provision.py). Secrets only via environment variables, never
  argv: ``ZITADEL_ACCESS_TOKEN`` (policy ops), ``ZPROBE_PASSWORD`` (login).
* The login probe sends ONE credential attempt per call. It is intended for the THROWAWAY
  cluster's throwaway test user only (INSTALL-RUNBOOK A.7.1); it never prints the password,
  token, or authorization code (only the code length).
* ``insecure`` tolerates the dev self-signed mock cert; it is an explicit opt-in flag.

OUTCOMES (``LoginResult.outcome``)
----------------------------------
* ``CODE_ISSUED``  - the flow ended in a redirect to the app callback carrying ``code=``.
* ``MFA_REQUIRED`` - the flow stopped at a mandatory second-factor step (no code issued).
* ``DENIED``       - the flow stopped elsewhere without a code (bad credentials, error page).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

TOKEN_ENV_VAR = "ZITADEL_ACCESS_TOKEN"  # noqa: S105 - env var NAME, not a secret
PASSWORD_ENV_VAR = "ZPROBE_PASSWORD"  # noqa: S105 - env var NAME, not a secret
OTP_ENV_VAR = "ZPROBE_OTP"  # noqa: S105 - literal second-factor code (e.g. a deliberately wrong one)
TOTP_SECRET_ENV_VAR = "ZPROBE_TOTP_SECRET"  # noqa: S105 - base32 secret; a VALID code is computed
_MAX_STEPS = 12
DEFAULT_SECOND_FACTORS = ["SECOND_FACTOR_TYPE_OTP", "SECOND_FACTOR_TYPE_U2F"]
_MFA_PATH_MARKERS = ("/mfa/", "/mfa", "/otp", "/u2f", "/mfa/init", "/mfa/prompt")


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested offline)
# --------------------------------------------------------------------------- #
class _FormParser(HTMLParser):
    """Collect the first <form>: its action/method and every named input/button value."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._cur: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._cur = {"action": a.get("action", ""), "method": a.get("method", "get").lower(),
                         "fields": {}, "buttons": {}}
            self.forms.append(self._cur)
        elif self._cur is not None and a.get("name"):
            is_button = tag == "button" or a.get("type", "").lower() in ("submit", "button")
            if tag == "input" and not is_button:
                self._cur["fields"].setdefault(a["name"], a.get("value", ""))
            elif is_button and tag in ("input", "button"):
                # Named submit buttons (register, resetlogin, skip, next ...) are NOT sent by
                # default: sending the wrong one triggers a different action. Recorded only.
                self._cur["buttons"].setdefault(a["name"], a.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._cur = None


def parse_forms(html: str) -> list[dict[str, Any]]:
    """Return every form on the page as {action, method, fields}."""
    p = _FormParser()
    p.feed(html)
    return p.forms


def totp_now(secret_b32: str, at: float | None = None, *, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (HMAC-SHA1) for a base32 secret; ``at`` is a unix time (default now)."""
    key = base64.b32decode(secret_b32.strip().replace(" ", "").upper() + "=" * (-len(secret_b32.strip()) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def code_from_location(location: str, redirect_uri: str) -> str | None:
    """The auth code if ``location`` is the app callback carrying ``code=``, else None."""
    if not location.startswith(redirect_uri):
        return None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    vals = qs.get("code")
    return vals[0] if vals else None


def is_mfa_path(path: str) -> bool:
    """True when a Login-V1 path is a second-factor step."""
    p = path.lower()
    return any(m in p for m in _MFA_PATH_MARKERS)


def classify(path: str, code: str | None) -> str:
    """Map the terminal state of the flow to an outcome."""
    if code:
        return "CODE_ISSUED"
    return "MFA_REQUIRED" if is_mfa_path(path) else "DENIED"


def page_hint(html: str, limit: int = 140) -> str:
    """Short visible-text excerpt of a rendered page (non-secret; aids diagnosing a denial)."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


@dataclass
class LoginResult:
    outcome: str
    last_path: str
    steps: list[str] = field(default_factory=list)
    code_len: int = 0
    hint: str = ""

    def summary(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "last_path": self.last_path,
                "steps": self.steps, "code_len": self.code_len, "hint": self.hint}


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # noqa: D401
        return None


class Probe:
    """Drives Login V1 for one external OIDC app, one attempt per ``login`` call."""

    def __init__(self, connect_base: str, host: str, *, insecure: bool = False, timeout: int = 20):
        self.connect_base = connect_base.rstrip("/")
        self.host = host
        self.timeout = timeout
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPCookieProcessor(self.jar), urllib.request.HTTPSHandler(context=ctx)
        )

    def _req(self, method: str, path_or_url: str, data: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Return (status, body, location). Absolute URLs on our host are rewritten to the connect base."""
        url = path_or_url
        if url.startswith("http"):
            u = urllib.parse.urlparse(url)
            if u.netloc != self.host:
                return 302, "", url  # leaves our host (the app callback): report, do not follow
            url = u.path + (("?" + u.query) if u.query else "")
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(self.connect_base + url, data=body, method=method)
        req.add_header("Host", self.host)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace"), ""
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "") if e.headers else ""
            return e.code, e.read().decode("utf-8", "replace") if e.fp else "", loc

    def login(self, *, client_id: str, redirect_uri: str, login_name: str, password: str,
              skip_mfa_if_offered: bool = True, force_skip: bool = False, otp_code: str | None = None,
              scope: str = "openid profile") -> LoginResult:
        _verifier, challenge = pkce_pair()
        q = urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": scope, "state": "probe", "code_challenge": challenge, "code_challenge_method": "S256",
        })
        steps: list[str] = []
        submitted: set[str] = set()
        cur_path = "/oauth/v2/authorize"
        status, body, loc = self._req("GET", f"/oauth/v2/authorize?{q}")
        for _ in range(_MAX_STEPS):
            if loc:
                code = code_from_location(loc, redirect_uri)
                path = urllib.parse.urlparse(loc).path
                if code is not None:
                    return LoginResult("CODE_ISSUED", path, steps, len(code))
                steps.append(f"{status} -> {path}")
                if loc.startswith("http") and urllib.parse.urlparse(loc).netloc != self.host:
                    return LoginResult(classify(path, None), path, steps)
                cur_path = path
                status, body, loc = self._req("GET", loc)
                continue
            forms = parse_forms(body)
            if not forms:
                return LoginResult(classify(cur_path, None), cur_path, steps, hint=page_hint(body))
            form = forms[0]
            fields = dict(form["fields"])
            action = form["action"] or cur_path
            action_path = urllib.parse.urlparse(action).path
            if action_path in submitted:
                # The same step re-rendered after our submit (e.g. wrong credentials, error
                # banner): stop. NEVER resubmit credentials in a loop (account lockout).
                return LoginResult("DENIED", action_path, steps, hint=page_hint(body))
            if "password" in fields:  # the password page ALSO carries a hidden loginName
                fields["loginName"] = login_name
                fields["password"] = password
            elif "loginName" in fields:
                fields["loginName"] = login_name
            elif is_mfa_path(action_path) and "code" in fields and otp_code is not None:
                fields["code"] = otp_code  # the second-factor VERIFY step (user has a registered factor)
            elif is_mfa_path(action_path) and (
                    force_skip or (skip_mfa_if_offered and "skip" in form["buttons"])):
                # force_skip submits the skip even when the page does NOT offer it: the
                # bypass attempt AC-4 must see refused while MFA is enforced.
                fields["skip"] = form["buttons"].get("skip") or "true"
            else:
                # A mandatory second-factor (or unknown) step offering nothing we may submit.
                return LoginResult(classify(action_path, None), action_path, steps)
            submitted.add(action_path)
            steps.append(f"form {action_path}")
            cur_path = action_path
            status, body, loc = self._req("POST", action, fields)
        return LoginResult("DENIED", cur_path, steps)


# --------------------------------------------------------------------------- #
# Org login-policy helper
# --------------------------------------------------------------------------- #
class PolicyClient:
    def __init__(self, connect_base: str, host: str, token: str, org_id: str, *, insecure: bool = False):
        if not token:
            raise ValueError(f"a ZITADEL access token is required; set {TOKEN_ENV_VAR} in the environment")
        self.connect_base, self.host, self.token, self.org_id = connect_base.rstrip("/"), host, token, org_id
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(self.connect_base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Host", self.host)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-zitadel-orgid", self.org_id)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=20) as r:
                raw = r.read().decode() or "{}"
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"message": raw[:200]}

    def get(self) -> dict:
        _s, o = self._call("GET", "/management/v1/policies/login")
        return o.get("policy", {})

    def enforce_mfa(self, enforce: bool) -> dict:
        """Create/update the ORG-level custom login policy with forceMfa=enforce; read back."""
        cur = self.get()
        body = {
            "allowUsernamePassword": cur.get("allowUsernamePassword", True),
            "allowRegister": cur.get("allowRegister", True),
            "allowExternalIdp": cur.get("allowExternalIdp", True),
            "forceMfa": enforce,
            "forceMfaLocalOnly": False,
            "passwordlessType": cur.get("passwordlessType", "PASSWORDLESS_TYPE_ALLOWED"),
            "hidePasswordReset": cur.get("hidePasswordReset", False),
            "passwordCheckLifetime": cur.get("passwordCheckLifetime", "864000s"),
            "externalLoginCheckLifetime": cur.get("externalLoginCheckLifetime", "864000s"),
            "mfaInitSkipLifetime": cur.get("mfaInitSkipLifetime", "2592000s"),
            "secondFactorCheckLifetime": cur.get("secondFactorCheckLifetime", "64800s"),
            "multiFactorCheckLifetime": cur.get("multiFactorCheckLifetime", "43200s"),
        }
        # ZITADEL omits false booleans: an ORG custom policy has no `isDefault` key at all.
        method = "POST" if cur.get("isDefault") else "PUT"
        status, out = self._call(method, "/management/v1/policies/login", body)
        if status >= 400 and "NotChanged" not in json.dumps(out):  # NotChanged == already as requested
            raise RuntimeError(f"login-policy {method} failed: HTTP {status} {out.get('message', '')[:160]}")
        if enforce:
            # A freshly created ORG policy starts with NO second factors; carry over the
            # effective ones (instance default) or MFA could never be completed.
            for factor in cur.get("secondFactors") or DEFAULT_SECOND_FACTORS:
                st, o = self._call("POST", "/management/v1/policies/login/second_factors", {"type": factor})
                if st >= 400 and "AlreadyExists" not in json.dumps(o) and st != 409:
                    raise RuntimeError(f"add second factor {factor} failed: HTTP {st} {str(o)[:160]}")
        return self.get()

    def reset(self) -> dict:
        """Drop the org custom policy (revert to the instance default)."""
        self._call("DELETE", "/management/v1/policies/login")
        return self.get()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--connect-base", required=True, help="e.g. https://10.0.20.12 (Traefik guest)")
    ap.add_argument("--host", required=True, help="Host header, e.g. auth.platform.example.test")
    ap.add_argument("--insecure", action="store_true", help="tolerate the dev self-signed cert")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="drive one Login-V1 attempt; password from ZPROBE_PASSWORD")
    lg.add_argument("--client-id", required=True)
    lg.add_argument("--redirect-uri", required=True)
    lg.add_argument("--login-name", required=True)
    lg.add_argument("--no-skip-mfa", action="store_true", help="do NOT take the optional 2FA skip")
    lg.add_argument("--force-skip", action="store_true",
                    help="submit the 2FA skip even when not offered (bypass attempt)")
    po = sub.add_parser("policy", help="set/read/reset the org MFA enforcement; token from ZITADEL_ACCESS_TOKEN")
    po.add_argument("--org-id", required=True)
    po.add_argument("action", choices=["enforce", "relax", "show", "reset"])
    a = ap.parse_args(argv)
    if a.cmd == "login":
        pw = os.environ.get(PASSWORD_ENV_VAR, "")
        if not pw:
            print(f"error: set {PASSWORD_ENV_VAR} in the environment", file=sys.stderr)
            return 2
        otp = os.environ.get(OTP_ENV_VAR) or (
            totp_now(os.environ[TOTP_SECRET_ENV_VAR]) if os.environ.get(TOTP_SECRET_ENV_VAR) else None)
        res = Probe(a.connect_base, a.host, insecure=a.insecure).login(
            client_id=a.client_id, redirect_uri=a.redirect_uri, login_name=a.login_name,
            password=pw, skip_mfa_if_offered=not a.no_skip_mfa, force_skip=a.force_skip, otp_code=otp)
        print(json.dumps(res.summary()))
        return 0
    pc = PolicyClient(a.connect_base, a.host, os.environ.get(TOKEN_ENV_VAR, ""), a.org_id, insecure=a.insecure)
    pol = {"enforce": lambda: pc.enforce_mfa(True), "relax": lambda: pc.enforce_mfa(False),
           "show": pc.get, "reset": pc.reset}[a.action]()
    print(json.dumps({k: pol.get(k) for k in ("forceMfa", "isDefault", "mfaInitSkipLifetime", "secondFactors")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
