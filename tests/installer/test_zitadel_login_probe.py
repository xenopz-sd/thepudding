"""Offline tests for scripts/installer/zitadel_login_probe.py (SVC-06 AC-4, WS-A2).

Spec: .kiro/specs/svc06-mfa-live-acceptance/. The probe drives ZITADEL's Login V1 UI and
classifies whether an authorization code was issued. These tests pin its pure helpers and,
with ``Probe._req`` replaced by a scripted fake server, the flow decisions that AC-4 relies
on: control (code issued), MFA enforced (stops at the mandatory step), bypass attempt and a
wrong second factor (denied), a correct second factor (code issued), and the guard that
never resubmits credentials in a loop (account-lockout safety). No network, no Docker.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts/installer/zitadel_login_probe.py"
_spec = importlib.util.spec_from_file_location("zitadel_login_probe", _PATH)
z = importlib.util.module_from_spec(_spec)
sys.modules["zitadel_login_probe"] = z
_spec.loader.exec_module(z)  # type: ignore[union-attr]

CB = "https://app.example.test/oauth/callback"
HOST = "auth.example.test"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_parse_forms_separates_inputs_from_named_buttons():
    html = (
        '<form action="/ui/login/mfa/prompt" method="POST">'
        '<input type="hidden" name="gorilla.csrf.Token" value="t">'
        '<input type="radio" name="provider" value="0">'
        '<button type="submit" name="skip" value="true">Skip</button>'
        '<button type="submit">Next</button></form>'
    )
    (f,) = z.parse_forms(html)
    assert f["action"] == "/ui/login/mfa/prompt"
    assert f["fields"] == {"gorilla.csrf.Token": "t", "provider": "0"}
    assert f["buttons"] == {"skip": "true"}, "named buttons are recorded, never sent by default"


def test_register_button_is_never_sent_by_default():
    html = ('<form action="/ui/login/loginname"><input name="loginName" value="">'
            '<button name="register" value="true">Register</button></form>')
    (f,) = z.parse_forms(html)
    assert "register" not in f["fields"] and f["buttons"] == {"register": "true"}


def test_pkce_pair_is_s256():
    import base64, hashlib
    v, c = z.pkce_pair()
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert len(v) >= 43


def test_code_from_location():
    assert z.code_from_location(CB + "?code=abc&state=probe", CB) == "abc"
    assert z.code_from_location(CB + "?error=access_denied", CB) is None
    assert z.code_from_location("https://other.example/x?code=abc", CB) is None


def test_mfa_path_and_classification():
    assert z.is_mfa_path("/ui/login/mfa/prompt") and z.is_mfa_path("/ui/login/mfa/verify")
    assert not z.is_mfa_path("/ui/login/password")
    assert z.classify("/x", "c") == "CODE_ISSUED"
    assert z.classify("/ui/login/mfa/verify", None) == "MFA_REQUIRED"
    assert z.classify("/ui/login/password", None) == "DENIED"


def test_totp_matches_rfc6238_sha1_vectors():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # ASCII "12345678901234567890"
    assert z.totp_now(secret, at=59, digits=8) == "94287082"
    assert z.totp_now(secret, at=1111111109, digits=8) == "07081804"
    assert len(z.totp_now(secret, at=59)) == 6


def test_page_hint_strips_markup_and_is_bounded():
    h = z.page_hint("<html><script>secret()</script><p>Invalid   code</p></html>", limit=20)
    assert h == "Invalid code" and "secret" not in h


# --------------------------------------------------------------------------- #
# Scripted fake Login-V1 server
# --------------------------------------------------------------------------- #
def _page(action, *, fields=(), buttons=(), text=""):
    inputs = "".join(f'<input name="{n}" value="">' for n in fields)
    btns = "".join(f'<button name="{n}" value="true">{n}</button>' for n in buttons)
    return f'<html><body>{text}<form action="{action}" method="POST">{inputs}{btns}</form></body></html>'


class FakeServer:
    """mode: control | enforced_unregistered | enforced_totp."""

    def __init__(self, mode, *, password_ok=True, valid_otp="123456"):
        self.mode, self.password_ok, self.valid_otp = mode, password_ok, valid_otp
        self.posts: list[tuple[str, dict]] = []

    def __call__(self, method, path_or_url, data=None):
        path = path_or_url.split("?")[0]
        if method == "GET" and path.startswith("/oauth/v2/authorize") and "callback" not in path:
            return 302, "", "/ui/login/login?authRequestID=1"
        if method == "GET" and path == "/ui/login/login":
            return 200, _page("/ui/login/loginname", fields=("loginName",), buttons=("register",)), ""
        if method == "POST":
            self.posts.append((path, dict(data or {})))
            if path == "/ui/login/loginname":
                return 200, _page("/ui/login/password", fields=("loginName", "password")), ""
            if path == "/ui/login/password":
                if not self.password_ok:
                    return 200, _page("/ui/login/password", fields=("loginName", "password"),
                                      text="Password invalid"), ""
                if self.mode == "control":
                    return 200, _page("/ui/login/mfa/prompt", fields=("provider",), buttons=("skip",)), ""
                if self.mode == "enforced_unregistered":
                    return 200, _page("/ui/login/mfa/prompt", fields=("provider",), buttons=()), ""
                return 200, _page("/ui/login/mfa/verify", fields=("mfaType", "code")), ""
            if path == "/ui/login/mfa/prompt":
                if self.mode == "control" and data.get("skip") == "true":
                    return 302, "", "/oauth/v2/authorize/callback?id=1"
                return 200, _page("/ui/login/mfa/prompt", fields=("provider",), text="2-Factor Setup"), ""
            if path == "/ui/login/mfa/verify":
                if data.get("code") == self.valid_otp:
                    return 302, "", "/oauth/v2/authorize/callback?id=1"
                return 200, _page("/ui/login/mfa/verify", fields=("mfaType", "code"), text="Invalid code"), ""
        if method == "GET" and path == "/oauth/v2/authorize/callback":
            return 302, "", CB + "?code=" + "x" * 229 + "&state=probe"
        return 404, "", ""


def _probe(server):
    p = z.Probe("https://127.0.0.1", HOST, insecure=True)
    p._req = server  # type: ignore[method-assign]
    return p


def _login(server, **kw):
    return _probe(server).login(client_id="cid", redirect_uri=CB, login_name="u", password="pw", **kw)


def test_control_without_enforcement_issues_a_code_via_the_optional_skip():
    r = _login(FakeServer("control"))
    assert r.outcome == "CODE_ISSUED" and r.code_len == 229


def test_control_without_taking_the_skip_stops_at_the_prompt():
    r = _login(FakeServer("control"), skip_mfa_if_offered=False)
    assert r.outcome == "MFA_REQUIRED" and r.code_len == 0


def test_enforced_mfa_never_offers_skip_so_no_code_is_issued():
    r = _login(FakeServer("enforced_unregistered"))
    assert r.outcome == "MFA_REQUIRED" and r.last_path == "/ui/login/mfa/prompt"


def test_enforced_mfa_forced_skip_bypass_attempt_is_denied():
    srv = FakeServer("enforced_unregistered")
    r = _login(srv, force_skip=True)
    assert r.outcome == "DENIED" and r.code_len == 0
    assert any(p == "/ui/login/mfa/prompt" and d.get("skip") == "true" for p, d in srv.posts), "the bypass WAS attempted"


def test_registered_factor_without_a_code_stops_at_the_verify_step():
    r = _login(FakeServer("enforced_totp"))
    assert r.outcome == "MFA_REQUIRED" and r.last_path == "/ui/login/mfa/verify"


def test_wrong_second_factor_is_denied_and_not_retried():
    srv = FakeServer("enforced_totp")
    r = _login(srv, otp_code="000000")
    assert r.outcome == "DENIED" and "Invalid code" in r.hint
    assert [p for p, _ in srv.posts].count("/ui/login/mfa/verify") == 1, "one attempt only (lockout safety)"


def test_correct_second_factor_issues_a_code():
    r = _login(FakeServer("enforced_totp"), otp_code="123456")
    assert r.outcome == "CODE_ISSUED"


def test_wrong_password_is_denied_with_a_single_submission():
    srv = FakeServer("control", password_ok=False)
    r = _login(srv)
    assert r.outcome == "DENIED"
    assert [p for p, _ in srv.posts].count("/ui/login/password") == 1, "never resubmit credentials in a loop"


def test_cli_refuses_to_run_without_a_password_env(monkeypatch, capsys):
    monkeypatch.delenv("ZPROBE_PASSWORD", raising=False)
    rc = z.main(["--connect-base", "https://127.0.0.1", "--host", HOST, "login",
                 "--client-id", "c", "--redirect-uri", CB, "--login-name", "u"])
    assert rc == 2 and "ZPROBE_PASSWORD" in capsys.readouterr().err


def test_policy_client_requires_a_token():
    with pytest.raises(ValueError):
        z.PolicyClient("https://127.0.0.1", HOST, "", "org")
