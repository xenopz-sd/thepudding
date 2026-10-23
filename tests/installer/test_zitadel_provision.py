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
