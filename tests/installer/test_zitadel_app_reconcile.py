"""Reconcile existing OIDC apps on re-run (v0.14.0 operator feedback #3).

Bug condition: the helper reconciled ONLY ``loginVersion`` for an app that already
exists. A changed manifest ``redirect_uris`` (or a dev-mode flag) was silently
ignored on re-run, so the stale URI persisted and the login callback was rejected
with no hint the manifest and the live app diverged.

Fix: ``ensure_oidc_app`` converges ``redirectUris`` (set-equal), ``devMode`` (when
declared) and ``loginVersion`` via the full-config PUT, then a read-back
FAIL-LOUD gate (``ZitadelError``) verifies the live config. Unchanged apps make
NO PUT and report ``config_reconciled`` False (changed=0).

Pure offline: urllib is replaced by a scripted transport.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts" / "installer"))
import zitadel_provision as zp  # noqa: E402

_V1 = {"loginV1": {}}


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Transport:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append({"method": req.get_method(), "url": req.full_url,
                           "body": req.data.decode() if req.data else None})
        if not self._script:
            raise AssertionError(f"unexpected extra request: {req.get_method()} {req.full_url}")
        r = self._script.pop(0)
        if isinstance(r, urllib.error.HTTPError):
            raise r
        return r

    def puts(self):
        return [c for c in self.calls if c["method"] == "PUT" and "oidc_config" in c["url"]]


def _app(redirects, *, dev_mode=None, login=_V1):
    oidc = {"clientId": "cid-1", "redirectUris": list(redirects),
            "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
            "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
            "appType": "OIDC_APP_TYPE_USER_AGENT", "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",
            "loginVersion": login}
    if dev_mode is not None:
        oidc["devMode"] = dev_mode
    return _Resp(200, {"result": [{"name": "webapp", "id": "app-1", "oidcConfig": oidc}]})


def _run(script, redirects, **kw):
    t = _Transport(script)
    with mock.patch("urllib.request.urlopen", t):
        c = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        out = c.ensure_oidc_app("proj-1", "org-1", "webapp", redirects,
                                app_type="spa", auth_method="none", **kw)
    return out, t


# --------------------------------------------------------------------------- #
# Bug-condition (FAIL on unfixed code)
# --------------------------------------------------------------------------- #
def test_BUG_changed_redirect_uri_is_put_on_rerun():
    live = ["https://old.test/cb"]
    want = ["https://new.test/cb"]
    out, t = _run([_app(live), _app(live), _Resp(200, {}), _app(want)], want)
    assert len(t.puts()) == 1, "a drifted redirect URI must be PUT on re-run"
    body = json.loads(t.puts()[0]["body"])
    assert body["redirectUris"] == want
    assert out["redirect_uris_reconciled"] is True
    assert out["config_reconciled"] is True


def test_BUG_declared_dev_mode_is_reconciled():
    uris = ["http://10.0.0.5/cb"]
    out, t = _run([_app(uris, dev_mode=False), _app(uris, dev_mode=False), _Resp(200, {}),
                   _app(uris, dev_mode=True)], uris, dev_mode=True)
    assert json.loads(t.puts()[0]["body"])["devMode"] is True
    assert out["dev_mode_reconciled"] is True


def test_BUG_readback_gate_fails_loud_when_live_config_does_not_converge():
    live, want = ["https://old.test/cb"], ["https://new.test/cb"]
    with pytest.raises(zp.ZitadelError, match="redirect"):
        # PUT "succeeds" but the read-back still shows the stale URI.
        _run([_app(live), _app(live), _Resp(200, {}), _app(live)], want)


# --------------------------------------------------------------------------- #
# Preservation (pass before AND after)
# --------------------------------------------------------------------------- #
def test_unchanged_app_makes_no_put_and_reports_no_change():
    uris = ["https://app.test/cb"]
    out, t = _run([_app(uris), _app(uris)], uris)
    assert t.puts() == []
    assert out["already_existed"] is True
    assert out["login_version_reconciled"] is False
    assert out.get("config_reconciled", False) is False


def test_redirect_order_difference_is_not_drift():
    out, t = _run([_app(["https://b/cb", "https://a/cb"]), _app(["https://b/cb", "https://a/cb"])],
                  ["https://a/cb", "https://b/cb"])
    assert t.puts() == []


def test_login_version_drift_still_reconciled():
    uris = ["https://app.test/cb"]
    out, t = _run([_app(uris, login={"loginV2": {"baseUri": ""}}),
                   _app(uris, login={"loginV2": {"baseUri": ""}}), _Resp(200, {}), _app(uris)], uris)
    assert out["login_version_reconciled"] is True
    assert len(t.puts()) == 1


def test_undeclared_dev_mode_is_carried_over_not_clobbered():
    """dev_mode=None (not declared) must NOT reset a live devMode=true on a URI-only PUT."""
    live, want = ["http://x/old"], ["http://x/new"]
    out, t = _run([_app(live, dev_mode=True), _app(live, dev_mode=True), _Resp(200, {}),
                   _app(want, dev_mode=True)], want)
    assert json.loads(t.puts()[0]["body"])["devMode"] is True
    assert out["dev_mode_reconciled"] is False


def test_create_body_is_byte_identical_when_dev_mode_not_declared():
    _ep, body = zp.build_app_request("spa", "none", "n", ["https://a/cb"])
    assert "devMode" not in body
    _ep, body2 = zp.build_app_request("spa", "none", "n", ["https://a/cb"], dev_mode=True)
    assert body2["devMode"] is True


def test_api_apps_are_never_reconciled_for_redirects():
    t = _Transport([_Resp(200, {"result": [{"name": "api", "id": "a", "oidcConfig": {"clientId": "c"}}]})])
    with mock.patch("urllib.request.urlopen", t):
        c = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        out = c.ensure_oidc_app("proj-1", "org-1", "api", [], app_type="api", auth_method="jwt")
    assert t.puts() == [] and out["already_existed"] is True


# --------------------------------------------------------------------------- #
# Threading: CLI, manifest, roles
# --------------------------------------------------------------------------- #
def test_cli_accepts_dev_mode_tristate():
    import argparse  # noqa: F401
    src = (_ROOT / "scripts/installer/zitadel_provision.py").read_text()
    assert "--dev-mode" in src and "--no-dev-mode" in src


def test_roles_pass_all_redirect_uris_and_dev_mode_and_count_reconcile_as_changed():
    zop = (_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text()
    assert "redirect_uris | first" not in zop, "all redirect URIs must be passed, not just the first"
    assert zop.count("--dev-mode") >= 2 or zop.count("zop_dev_mode_arg") >= 2
    assert zop.count("config_reconciled") >= 2, "both paths must report reconcile as changed"
    cb = (_ROOT / "ansible/roles/consumer_bootstrap/tasks/main.yml").read_text()
    assert "'dev_mode'" in cb, "STEP 4 mapping must preserve dev_mode"


def test_manifest_accepts_bool_dev_mode_and_rejects_other():
    sys.path.insert(0, str(_ROOT / "scripts" / "installer"))
    from consumer_manifest import ManifestValidationError, validate_manifest
    base = {"slug": "p", "sso": {"org_name": "o", "app_registrations": [
        {"name": "a", "app_type": "public-spa", "auth_method": "none", "dev_mode": True}]}}
    validate_manifest(base)
    base["sso"]["app_registrations"][0]["dev_mode"] = "yes"
    with pytest.raises(ManifestValidationError, match="dev_mode"):
        validate_manifest(base)


def test_role_yaml_still_parses():
    for p in ("ansible/roles/zitadel-org-provision/tasks/main.yml",
              "ansible/roles/consumer_bootstrap/tasks/main.yml"):
        assert yaml.safe_load((_ROOT / p).read_text())


# --------------------------------------------------------------------------- #
# Rendered argv (real Ansible templater): guards the backslash-escape pitfall
# --------------------------------------------------------------------------- #
def _render_vars(task_name_fragment, item):
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template
    tasks = yaml.safe_load((_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text())
    task = next(t for t in tasks if task_name_fragment in str(t.get("name")))
    out = {}
    for k in ("zop_redirect_arg", "zop_dev_mode_arg"):
        out[k] = Templar(loader=DataLoader(), variables={"item": item}).template(
            trust_as_template(task["vars"][k]))
    return out


@pytest.mark.parametrize("frag", ["Provision each app via the helper", "DEV: provision each app via the helper"])
def test_rendered_argv_carries_every_redirect_uri_and_tristate_dev_mode(frag):
    r = _render_vars(frag, {"redirect_uris": ["https://a/cb", "http://b/cb"], "dev_mode": True})
    assert r["zop_redirect_arg"] == ["--redirect-uri=https://a/cb", "--redirect-uri=http://b/cb"]
    assert r["zop_dev_mode_arg"] == ["--dev-mode"]
    r = _render_vars(frag, {"redirect_uris": [], "dev_mode": False})
    assert r["zop_redirect_arg"] == [] and r["zop_dev_mode_arg"] == ["--no-dev-mode"]
    r = _render_vars(frag, {"redirect_uris": ["https://a/cb"]})
    assert r["zop_dev_mode_arg"] == []


# --------------------------------------------------------------------------- #
# WS-C19 live finding: a login-policy-only change must be reported as changed
# --------------------------------------------------------------------------- #
def _helper_tasks():
    import yaml
    tasks = yaml.safe_load((_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text())
    return {t["register"]: t for t in tasks if isinstance(t, dict) and t.get("register") in
            ("zop_provision_run_guest", "zop_provision_run_prod")}


def _render_changed(task, reg, stdout):
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template
    expr = "{{ " + " ".join(str(task["changed_when"]).split()) + " }}"
    return bool(Templar(loader=DataLoader(), variables={reg: {"stdout": stdout}}).template(trust_as_template(expr)))


_QUIET_APP = {"already_existed": True, "config_reconciled": False}


@pytest.mark.parametrize("reg", ["zop_provision_run_guest", "zop_provision_run_prod"])
def test_a_login_policy_only_change_is_reported_as_changed(reg):
    """Bug condition: ``ensure_allow_register`` PUT the org policy (live-verified: the org flipped to a custom
    policy) yet the recap said ``changed=0`` because changed_when read only the OIDC-app keys."""
    import json
    task = _helper_tasks()[reg]
    changed = json.dumps({"oidc_app": _QUIET_APP, "login_policy": {"changed": True, "allow_register": False}})
    quiet = json.dumps({"oidc_app": _QUIET_APP, "login_policy": {"changed": False, "allow_register": False}})
    absent = json.dumps({"oidc_app": _QUIET_APP})
    assert _render_changed(task, reg, changed) is True
    assert _render_changed(task, reg, quiet) is False
    assert _render_changed(task, reg, absent) is False, "no allow_register requested => nothing to report"


@pytest.mark.parametrize("reg", ["zop_provision_run_guest", "zop_provision_run_prod"])
def test_app_level_changes_are_still_reported(reg):
    import json
    task = _helper_tasks()[reg]
    assert _render_changed(task, reg, json.dumps({"oidc_app": {"already_existed": False}})) is True
    assert _render_changed(task, reg, json.dumps({"oidc_app": {"already_existed": True, "config_reconciled": True}})) is True
