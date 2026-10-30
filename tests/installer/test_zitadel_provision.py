"""Mocked-HTTP unit tests for scripts/installer/zitadel_provision.py (SVC-06).

Slice-3 Task 16 (spec: svc-06-identity-provider) — design.md §3.4 / FR-3/FR-6.
The ZITADEL management-API helper is unit-tested with the HTTP transport MOCKED
(no live ZITADEL) — exactly the testkit discipline testing-strategy.md requires
for an external-system client. These tests pin the CONTRACT:

  * idempotent ensure_org/ensure_project/ensure_oidc_app (an already-existing
    resource is a no-op SUCCESS, created=False, no error);
  * the access token is read ONLY from the environment (never a CLI flag / argv),
    and never appears on stdout;
  * a newly-created OIDC app returns its client_id + client_secret as JSON; a
    re-run (already-exists) returns already_existed=True and NO client_secret;
  * the end-to-end provision() returns a JSON-serializable result dict.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_zitadel_provision.py -v
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts" / "installer"
sys.path.insert(0, str(_SCRIPTS))

import zitadel_provision as zp  # noqa: E402


# --------------------------------------------------------------------------- #
# A tiny fake urllib transport driven by a scripted sequence of responses.
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://x", code=status, msg="err", hdrs=None, fp=io.BytesIO(raw)
    )


class _Transport:
    """Scripted urlopen: maps (method, path-substring) -> response or HTTPError.

    Records every (method, url, headers, body) call so tests can assert the
    token travels in the Authorization header and never in the URL/body argv.
    """

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):
        self.calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "headers": dict(req.header_items()),
            "body": req.data.decode("utf-8") if req.data else None,
        })
        if not self._script:
            raise AssertionError(f"unexpected extra request: {req.get_method()} {req.full_url}")
        resp = self._script.pop(0)
        if isinstance(resp, urllib.error.HTTPError):
            raise resp
        return resp


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv(zp.TOKEN_ENV_VAR, "pat-secret-xyz")
    return "pat-secret-xyz"


# --------------------------------------------------------------------------- #
# ensure_org idempotency.
# --------------------------------------------------------------------------- #
def test_ensure_org_creates_when_absent():
    # search -> empty result; POST -> {id}.
    transport = _Transport([
        _FakeResp(200, {"result": []}),              # _find_org_by_name
        _FakeResp(200, {"id": "org-123"}),            # POST create
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok")
        org_id, created = client.ensure_org("proj-dronefleet")
    assert org_id == "org-123" and created is True


def test_ensure_org_is_noop_when_present():
    # search finds it -> no POST at all (created=False).
    transport = _Transport([
        _FakeResp(200, {"result": [{"name": "proj-dronefleet", "id": "org-existing"}]}),
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok")
        org_id, created = client.ensure_org("proj-dronefleet")
    assert org_id == "org-existing" and created is False
    assert len(transport.calls) == 1, "a present org must NOT trigger a create POST (idempotent)"


def test_ensure_org_treats_409_as_success():
    # search misses (race), POST 409 already-exists, re-search finds it.
    transport = _Transport([
        _FakeResp(200, {"result": []}),
        _http_error(409, {"code": 6, "message": "Org already exists"}),
        _FakeResp(200, {"result": [{"name": "proj-x", "id": "org-raced"}]}),
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok")
        org_id, created = client.ensure_org("proj-x")
    assert org_id == "org-raced" and created is False


# --------------------------------------------------------------------------- #
# ensure_oidc_app returns secret on create, none on already-exists.
# --------------------------------------------------------------------------- #
def test_ensure_oidc_app_returns_client_secret_on_create():
    transport = _Transport([
        _FakeResp(200, {"result": []}),  # _find_app_by_name
        _FakeResp(200, {"clientId": "cid-1", "clientSecret": "csecret-1"}),  # create
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        app = client.ensure_oidc_app("proj-1", "org-1", "dronefleet-webapp", ["https://app/cb"])
    assert app["client_id"] == "cid-1"
    assert app["client_secret"] == "csecret-1"
    assert app["already_existed"] is False


def test_ensure_oidc_app_no_secret_when_already_exists():
    transport = _Transport([
        _FakeResp(200, {"result": [{"name": "dronefleet-webapp", "oidcConfig": {"clientId": "cid-old"}}]}),
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        app = client.ensure_oidc_app("proj-1", "org-1", "dronefleet-webapp", ["https://app/cb"])
    assert app["already_existed"] is True
    assert app["client_secret"] is None, (
        "ZITADEL only returns a client secret at creation; a re-run must NOT fabricate one"
    )
    assert app["client_id"] == "cid-old"


# --------------------------------------------------------------------------- #
# Token travels in the Authorization header, never the URL/body.
# --------------------------------------------------------------------------- #
def test_token_in_auth_header_never_in_url_or_body():
    transport = _Transport([
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"id": "org-123"}),
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "pat-secret-xyz")
        client.ensure_org("proj-x")
    for call in transport.calls:
        # Authorization header carries the bearer token...
        auth = {k.lower(): v for k, v in call["headers"].items()}.get("authorization", "")
        assert auth == "Bearer pat-secret-xyz"
        # ...and the token never leaks into the URL or request body.
        assert "pat-secret-xyz" not in call["url"]
        assert "pat-secret-xyz" not in (call["body"] or "")


def test_client_fails_closed_without_token():
    with pytest.raises(ValueError):
        zp.ZitadelClient("https://auth.test", "")


# --------------------------------------------------------------------------- #
# end-to-end provision() + CLI contract.
# --------------------------------------------------------------------------- #
def test_provision_end_to_end_json_serializable():
    transport = _Transport([
        _FakeResp(200, {"result": []}),                       # find org
        _FakeResp(200, {"id": "org-1"}),                      # create org
        _FakeResp(200, {"result": []}),                       # find project
        _FakeResp(200, {"id": "proj-1"}),                     # create project
        _FakeResp(200, {}),                                   # role admin
        _FakeResp(200, {}),                                   # role member
        _FakeResp(200, {}),                                   # role viewer
        _FakeResp(200, {"result": []}),                       # find app
        _FakeResp(200, {"clientId": "cid-1", "clientSecret": "csecret-1"}),  # create app
    ])
    with mock.patch("urllib.request.urlopen", transport):
        result = zp.provision("https://auth.test", "proj-x", "proj-x-project",
                              "proj-x-webapp", ["https://app/cb"],
                              ["admin", "member", "viewer"], "tok")
    # Fully JSON-serializable + carries the created ids + the client secret.
    dumped = json.dumps(result)
    assert '"org-1"' in dumped and '"proj-1"' in dumped
    assert result["oidc_app"]["client_secret"] == "csecret-1"
    assert result["roles_created"] == ["admin", "member", "viewer"]


def test_cli_requires_token_env(monkeypatch, capsys):
    monkeypatch.delenv(zp.TOKEN_ENV_VAR, raising=False)
    rc = zp.main(["--base-url", "https://auth.test", "--org", "o", "--project", "p", "--app", "a"])
    assert rc == 2, "CLI must fail closed (rc 2) when the token env var is absent"
    err = capsys.readouterr().err
    assert zp.TOKEN_ENV_VAR in err


def test_cli_has_no_token_flag():
    """The CLI must NOT expose a --token flag (no secret on argv)."""
    import argparse
    with mock.patch.object(sys, "argv", ["zitadel_provision.py", "--help"]):
        with pytest.raises(SystemExit):
            # argparse prints help and exits 0; we only care the parser has no token flag.
            parser = argparse.ArgumentParser()
            raise SystemExit(0)
    # Inspect the source: no add_argument for a token flag.
    src = (_SCRIPTS / "zitadel_provision.py").read_text(encoding="utf-8")
    assert '"--token"' not in src and "'--token'" not in src, (
        "zitadel_provision.py must NOT expose a --token CLI flag (token comes from the env, "
        "never argv — security-standards)"
    )


def test_cli_emits_json_on_stdout(token_env):
    transport = _Transport([
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"id": "org-1"}),
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"id": "proj-1"}),
        _FakeResp(200, {}), _FakeResp(200, {}), _FakeResp(200, {}),
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"clientId": "cid-1", "clientSecret": "csecret-1"}),
    ])
    buf = io.StringIO()
    with mock.patch("urllib.request.urlopen", transport), mock.patch.object(sys, "stdout", buf):
        rc = zp.main(["--base-url", "https://auth.test", "--org", "proj-x",
                      "--project", "proj-x-project", "--app", "proj-x-webapp",
                      "--redirect-uri", "https://app/cb"])
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["org"]["id"] == "org-1"
    assert out["oidc_app"]["client_secret"] == "csecret-1"


# =========================================================================== #
# zitadel-spa-pkce-onboarding — Task 3 (3.3-3.5) + Task 4 (4.1).
#
# Property 4 (SPA result null secret), Property 6 (token only in Authorization
# header), the CLI-domain / invalid-combo / create-failure / API once+idempotent
# unit tests, and the R8.1-R8.4 contract scenarios. All use the existing
# _Transport / _FakeResp mocked-HTTP style above.
# =========================================================================== #

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


def _spa_create_script():
    """A scripted transport for a successful SPA create: find-app empty, then a
    create response that carries a clientId (ZITADEL returns no secret for a
    public USER_AGENT/NONE app)."""
    return _Transport([
        _FakeResp(200, {"result": []}),                 # _find_app_by_name
        _FakeResp(200, {"clientId": "spa-cid-1"}),      # create (no secret)
    ])


# --------------------------------------------------------------------------- #
# Property 4 — newly-created SPA result carries client_id and a null secret.
# --------------------------------------------------------------------------- #
# Feature: zitadel-spa-pkce-onboarding, Property 4: Newly-created SPA result
# carries a client_id and a null client_secret
@settings(max_examples=150, deadline=None)
@given(
    app_name=st.text(min_size=1, max_size=40),
    client_id=st.text(min_size=1, max_size=24).filter(lambda s: s.strip() != ""),
    # ZITADEL may or may not echo a clientSecret for a SPA; the helper must emit
    # null either way, so draw the mocked response's secret field freely.
    echoed_secret=st.one_of(st.none(), st.text(min_size=0, max_size=24)),
)
def test_property4_spa_result_null_secret(app_name, client_id, echoed_secret):
    """Validates: Requirements 1.3, 8.2

    For any successful SPA creation (mocked create response), the emitted result
    has a non-null client_id, a client_secret that is None, and
    already_existed=False — regardless of whether the mocked response echoed a
    secret field.
    """
    create_body = {"clientId": client_id}
    if echoed_secret is not None:
        create_body["clientSecret"] = echoed_secret
    transport = _Transport([
        _FakeResp(200, {"result": []}),     # _find_app_by_name -> absent
        _FakeResp(200, create_body),        # create
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        app = client.ensure_oidc_app("proj-1", "org-1", app_name, ["https://app/cb"],
                                     app_type="spa", auth_method="none")
    assert app["client_id"] == client_id
    assert app["client_secret"] is None
    assert app["already_existed"] is False
    assert app["app_type"] == "spa"
    assert app["auth_method"] == "none"


# --------------------------------------------------------------------------- #
# Property 6 — the provisioning token appears only in the Authorization header.
# --------------------------------------------------------------------------- #
# Feature: zitadel-spa-pkce-onboarding, Property 6: The provisioning token
# appears only in the Authorization header
@settings(max_examples=150, deadline=None)
@given(
    # A realistic PAT-shaped token: a fixed, non-colliding sentinel prefix plus a
    # random alnum suffix. The prefix guarantees the drawn value is not a
    # coincidental substring of unrelated JSON (e.g. a short word like "nameQuer"
    # that appears inside the `nameQuery` search field) — the property under test
    # is that the TOKEN leaks, not that an arbitrary short string never appears.
    token_suffix=st.text(min_size=4, max_size=40, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_")),
    app_type_auth=st.sampled_from([("spa", "none"), ("web", "basic"), ("api", "jwt")]),
)
def test_property6_token_only_in_auth_header(token_suffix, app_type_auth):
    """Validates: Requirements 2.6, 8.4

    Across every request the helper issues for any app type, the configured
    token value appears ONLY in the Authorization: Bearer header and never in a
    request URL or body.
    """
    token = f"pat-SENTINEL-{token_suffix}"
    app_type, auth_method = app_type_auth
    # A full end-to-end provision() so org/project/role/app requests are all
    # recorded: find-org, create-org, find-proj, create-proj, 3 roles, find-app,
    # create-app.
    transport = _Transport([
        _FakeResp(200, {"result": []}),              # find org
        _FakeResp(200, {"organizationId": "org-1"}),  # create org
        _FakeResp(200, {"result": []}),              # find project
        _FakeResp(200, {"id": "proj-1"}),            # create project
        _FakeResp(200, {}), _FakeResp(200, {}), _FakeResp(200, {}),  # 3 roles
        _FakeResp(200, {"result": []}),              # find app
        _FakeResp(200, {"clientId": "cid-1", "clientSecret": "sek"}),  # create app
    ])
    with mock.patch("urllib.request.urlopen", transport):
        zp.provision("https://auth.test", "proj-x", "proj-x-project",
                     "proj-x-app", ["https://app/cb"],
                     ["admin", "member", "viewer"], token,
                     app_type=app_type, auth_method=auth_method)
    assert transport.calls, "expected at least one recorded request"
    for call in transport.calls:
        headers = {k.lower(): v for k, v in call["headers"].items()}
        assert headers.get("authorization") == f"Bearer {token}"
        assert token not in call["url"]
        assert token not in (call["body"] or "")


# --------------------------------------------------------------------------- #
# Task 3.5 — CLI domain / invalid combo / create failure / API once+idempotent.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flag,bad_value", [
    ("--app-type", "webapp"),
    ("--app-type", "SPA"),
    ("--auth-method", "mtls"),
    ("--auth-method", "pkce"),
])
def test_cli_rejects_out_of_domain_app_type_and_auth_method(flag, bad_value, token_env):
    """R2.1/R2.2 — argparse rejects out-of-domain --app-type / --auth-method with
    a non-zero exit (argparse uses exit code 2)."""
    with pytest.raises(SystemExit) as exc:
        zp.main(["--base-url", "https://auth.test", "--org", "o", "--project", "p",
                 "--app", "a", flag, bad_value])
    assert exc.value.code != 0


@pytest.mark.parametrize("app_type,auth_method", [
    ("spa", "basic"),
    ("spa", "jwt"),
    ("web", "none"),
    ("web", "jwt"),
    ("api", "none"),
    ("api", "basic"),
])
def test_cli_invalid_combination_exits_2_naming_pair(app_type, auth_method, token_env, capsys):
    """R2.4 — an in-domain but invalid (app_type, auth_method) pair exits 2 and
    names the offending pair on stderr."""
    rc = zp.main(["--base-url", "https://auth.test", "--org", "o", "--project", "p",
                  "--app", "a", "--app-type", app_type, "--auth-method", auth_method])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"{app_type}/{auth_method}" in err


def test_create_failure_exits_3(token_env, capsys):
    """R1.5/R3.5 — a non-409 ZITADEL error on the app create surfaces as exit 3."""
    transport = _Transport([
        _FakeResp(200, {"result": []}),               # find org
        _FakeResp(200, {"organizationId": "org-1"}),  # create org
        _FakeResp(200, {"result": []}),               # find project
        _FakeResp(200, {"id": "proj-1"}),             # create project
        _FakeResp(200, {}), _FakeResp(200, {}), _FakeResp(200, {}),  # roles
        _FakeResp(200, {"result": []}),               # find app
        _http_error(500, {"message": "boom"}),        # create app -> hard error
    ])
    with mock.patch("urllib.request.urlopen", transport):
        rc = zp.main(["--base-url", "https://auth.test", "--org", "proj-x",
                      "--project", "proj-x-project", "--app", "proj-x-spa",
                      "--app-type", "spa"])
    assert rc == 3
    assert "ERROR" in capsys.readouterr().err


def test_api_newly_created_emits_material_once():
    """R3.2 — a newly-created API client emits its created-once credential
    material in the client_secret result key exactly once."""
    transport = _Transport([
        _FakeResp(200, {"result": []}),  # find app
        _FakeResp(200, {"clientId": "api-cid-1", "clientKey": "jwt-key-material"}),  # create
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        app = client.ensure_api_app("proj-1", "org-1", "proj-x-api")
    assert app["app_type"] == "api"
    assert app["auth_method"] == "jwt"
    assert app["client_id"] == "api-cid-1"
    assert app["client_secret"] == "jwt-key-material"
    assert app["already_existed"] is False


def test_api_already_exists_no_reemit_exit_0():
    """R3.4 — an API client that already exists re-emits no credential material
    and reports already_existed=True (idempotent, no create)."""
    transport = _Transport([
        _FakeResp(200, {"result": [{"name": "proj-x-api", "id": "api-old"}]}),  # find app
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        app = client.ensure_api_app("proj-1", "org-1", "proj-x-api")
    assert app["already_existed"] is True
    assert app["client_secret"] is None
    assert app["client_id"] == "api-old"
    assert len(transport.calls) == 1, "an existing API app must NOT trigger a create POST"


def test_result_keys_additive_no_retype():
    """R6.2 — the result dict preserves the existing keys additively: app_name,
    client_id, client_secret, already_existed keep their names/types; app_type and
    auth_method are ADDED (no existing key removed/renamed/retyped)."""
    transport = _Transport([
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"clientId": "cid-1", "clientSecret": "csecret-1"}),
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        app = client.ensure_oidc_app("proj-1", "org-1", "webapp", ["https://app/cb"])
    # Existing keys unchanged in name + type (default web/basic).
    assert app["app_name"] == "webapp" and isinstance(app["app_name"], str)
    assert app["client_id"] == "cid-1" and isinstance(app["client_id"], str)
    assert app["client_secret"] == "csecret-1" and isinstance(app["client_secret"], str)
    assert app["already_existed"] is False and isinstance(app["already_existed"], bool)
    # Additive keys present.
    assert app["app_type"] == "web"
    assert app["auth_method"] == "basic"
    # Superset of the legacy key set (nothing removed).
    assert {"app_name", "client_id", "client_secret", "already_existed"} <= set(app.keys())


def test_stdlib_only_import_guard():
    """R6.3 — the helper is implemented with the standard library only; no
    third-party dependency such as `requests` is imported."""
    src = (_SCRIPTS / "zitadel_provision.py").read_text(encoding="utf-8")
    assert "import requests" not in src
    assert "from requests" not in src
    # Positive: the stdlib HTTP transport is used.
    assert "import urllib.request" in src


# --------------------------------------------------------------------------- #
# Task 4.1 — R8.1-R8.4 contract scenarios (mocked HTTP), example-based.
# --------------------------------------------------------------------------- #
def test_r8_1_spa_request_body_sets_user_agent_none_pkce():
    """R8.1 — a public SPA registration request body sets USER_AGENT, NONE, and
    the Authorization-Code grant (PKCE by construction)."""
    transport = _spa_create_script()
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        client.ensure_oidc_app("proj-1", "org-1", "spa-app", ["https://app/cb"],
                               app_type="spa", auth_method="none")
    # The create POST is the last recorded request; its body is the SPA body.
    create_call = next(c for c in transport.calls if c["method"] == "POST" and "apps/oidc" in c["url"])
    body = json.loads(create_call["body"])
    assert body["appType"] == "OIDC_APP_TYPE_USER_AGENT"
    assert body["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_NONE"
    assert "OIDC_GRANT_TYPE_AUTHORIZATION_CODE" in body["grantTypes"]
    assert "clientSecret" not in body


def test_r8_2_spa_result_emits_no_client_secret():
    """R8.2 — a public SPA registration result emits no client_secret."""
    transport = _spa_create_script()
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        app = client.ensure_oidc_app("proj-1", "org-1", "spa-app", ["https://app/cb"],
                                     app_type="spa", auth_method="none")
    assert app["client_secret"] is None


def test_r8_3_default_registration_is_confidential_web_golden_body():
    """R8.3 — the default registration (no new flags) remains the confidential
    WEB client with the exact golden body."""
    transport = _Transport([
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"clientId": "cid-1", "clientSecret": "csecret-1"}),
    ])
    with mock.patch("urllib.request.urlopen", transport):
        client = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        client.ensure_oidc_app("proj-1", "org-1", "webapp", ["https://app/cb"])
    create_call = next(c for c in transport.calls if c["method"] == "POST" and "apps/oidc" in c["url"])
    body = json.loads(create_call["body"])
    assert body == {
        "name": "webapp",
        "redirectUris": ["https://app/cb"],
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
        "appType": "OIDC_APP_TYPE_WEB",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",
        "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
    }


def test_r8_4_token_never_in_url_or_body(token_env):
    """R8.4 — the provisioning token is never present in any request URL or body
    (end-to-end, default web registration via the CLI)."""
    transport = _Transport([
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"organizationId": "org-1"}),
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"id": "proj-1"}),
        _FakeResp(200, {}), _FakeResp(200, {}), _FakeResp(200, {}),
        _FakeResp(200, {"result": []}),
        _FakeResp(200, {"clientId": "cid-1", "clientSecret": "csecret-1"}),
    ])
    with mock.patch("urllib.request.urlopen", transport):
        rc = zp.main(["--base-url", "https://auth.test", "--org", "proj-x",
                      "--project", "proj-x-project", "--app", "proj-x-webapp",
                      "--redirect-uri", "https://app/cb"])
    assert rc == 0
    for call in transport.calls:
        assert token_env not in call["url"]
        assert token_env not in (call["body"] or "")
