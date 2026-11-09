"""Org login-policy ``allowRegister`` reconcile (dev-mail-catcher R7, spec dev-mail-catcher).

The login-policy PUT is a FULL REPLACE, so the helper does a read-modify-write from a verified
field whitelist, never echoes read-only/unknown fields, never downgrades ``forceMfaLocalOnly``,
guards against switching password login off, and verifies by read-back. Absent => untouched.
Pure offline: urllib is replaced by a scripted transport.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts" / "installer"))
import consumer_manifest as cm  # noqa: E402
import zitadel_provision as zp  # noqa: E402


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


class _T:
    def __init__(self, script):
        self._s = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, context=None):
        self.calls.append({"method": req.get_method(), "url": req.full_url,
                           "org": dict(req.header_items()).get("X-zitadel-orgid"),
                           "body": json.loads(req.data) if req.data else None})
        if not self._s:
            raise AssertionError(f"unexpected extra request {req.get_method()} {req.full_url}")
        return self._s.pop(0)

    def writes(self):
        return [c for c in self.calls if c["method"] != "GET"]


def _policy(**kw):
    base = {"allowUsernamePassword": True, "allowRegister": False, "allowExternalIdp": False, "forceMfa": False,
            "passwordlessType": "PASSWORDLESS_TYPE_NOT_ALLOWED", "hidePasswordReset": False,
            "ignoreUnknownUsernames": False, "defaultRedirectUri": "", "passwordCheckLifetime": "864000s",
            "externalLoginCheckLifetime": "864000s", "mfaInitSkipLifetime": "2592000s",
            "secondFactorCheckLifetime": "64800s", "multiFactorCheckLifetime": "43200s",
            "allowDomainDiscovery": False, "disableLoginWithEmail": False, "disableLoginWithPhone": False,
            "isDefault": True, "details": {"sequence": "1"}, "idps": [], "secondFactors": ["SECOND_FACTOR_TYPE_OTP"]}
    base.update(kw)
    return _Resp(200, {"policy": base})


def _run(script, value):
    t = _T(script)
    with mock.patch("urllib.request.urlopen", t):
        c = zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")
        return c.ensure_allow_register("org-1", value), t


def test_already_equal_writes_nothing_and_creates_no_custom_policy():
    out, t = _run([_policy(allowRegister=False)], False)
    assert out["changed"] is False and out["custom_policy"] is False and t.writes() == []


def test_inherited_default_gets_a_custom_policy_via_post_with_only_whitelisted_fields():
    out, t = _run([_policy(isDefault=True), _Resp(200, {}), _policy(allowRegister=True, isDefault=False)], True)
    assert out["changed"] is True and out["via"] == "POST"
    body = t.writes()[0]["body"]
    assert body["allowRegister"] is True and t.writes()[0]["org"] == "org-1"
    assert set(body) <= set(zp._LOGIN_POLICY_FIELDS), "only verified request fields"
    for ro in ("isDefault", "details", "idps", "secondFactors"):
        assert ro not in body, f"read-only field {ro} must not be echoed"
    assert body["allowUsernamePassword"] is True and body["passwordCheckLifetime"] == "864000s", \
        "full-replace PUT must carry every other current value unchanged"


def test_existing_custom_policy_is_updated_with_put_preserving_other_settings():
    cur = _policy(isDefault=False, allowRegister=True, forceMfa=True, hidePasswordReset=True)
    out, t = _run([cur, _Resp(200, {}), _policy(isDefault=False, allowRegister=False, forceMfa=True,
                                                  hidePasswordReset=True)], False)
    assert out["via"] == "PUT"
    b = t.writes()[0]["body"]
    assert b["allowRegister"] is False and b["forceMfa"] is True and b["hidePasswordReset"] is True


def test_force_mfa_local_only_is_echoed_only_when_true_never_downgraded():
    t_true = _run([_policy(isDefault=False, forceMfaLocalOnly=True), _Resp(200, {}),
                   _policy(isDefault=False, allowRegister=True)], True)[1]
    assert t_true.writes()[0]["body"].get("forceMfaLocalOnly") is True
    t_false = _run([_policy(isDefault=False, forceMfaLocalOnly=False), _Resp(200, {}),
                    _policy(isDefault=False, allowRegister=True)], True)[1]
    assert "forceMfaLocalOnly" not in t_false.writes()[0]["body"]


def test_readback_that_did_not_converge_fails_loud():
    with pytest.raises(zp.ZitadelError, match="allowRegister did not converge"):
        _run([_policy(), _Resp(200, {}), _policy(allowRegister=False)], True)


def test_lockout_guard_refuses_when_password_login_got_switched_off():
    with pytest.raises(zp.ZitadelError, match="lock-out"):
        _run([_policy(), _Resp(200, {}), _policy(allowRegister=True, allowUsernamePassword=False)], True)


def test_cli_exposes_tristate_flags_and_provision_omits_the_block_when_unset():
    src = (_ROOT / "scripts/installer/zitadel_provision.py").read_text(encoding="utf-8")
    assert "--allow-register" in src and "--no-allow-register" in src
    assert "if login_policy is not None" in src and "allow_register: bool | None = None" in src


# --------------------------------------------------------------------------- #
# Manifest + role threading
# --------------------------------------------------------------------------- #
def _manifest(**sso_extra):
    sso = {"org_name": "o", "app_registrations": [{"name": "a", "app_type": "public-spa", "auth_method": "none"}]}
    sso.update(sso_extra)
    return {"slug": "p", "sso": sso}


def test_manifest_accepts_login_policy_allow_register_bool_and_stays_backward_compatible():
    cm.validate_manifest(_manifest())
    cm.validate_manifest(_manifest(login_policy={"allow_register": True}))
    cm.validate_manifest(_manifest(login_policy={}))


@pytest.mark.parametrize("bad,match", [({"allow_register": "yes"}, "allow_register"), ({"nope": 1}, "unknown"),
                                       ("on", "mapping")])
def test_manifest_rejects_a_malformed_login_policy(bad, match):
    with pytest.raises(cm.ManifestValidationError, match=match):
        cm.validate_manifest(_manifest(login_policy=bad))


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _zop_vars(frag):
    tasks = yaml.safe_load((_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text())
    return next(t for t in tasks if frag in str(t.get("name")))["vars"]


@pytest.mark.parametrize("frag", ["Provision each app via the helper", "DEV: provision each app via the helper"])
@pytest.mark.parametrize("val,want", [(True, ["--allow-register"]), (False, ["--no-allow-register"]), (None, [])])
def test_role_renders_the_tristate_arg_in_both_paths(frag, val, want):
    assert _tpl(_zop_vars(frag)["zop_allow_register_arg"], {"zop_allow_register": val}) == want


def test_role_argv_includes_the_arg_in_both_paths_and_default_is_null():
    text = (_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text()
    assert text.count("zop_allow_register_arg }}") + text.count("zop_allow_register_arg + ") >= 2
    d = yaml.safe_load((_ROOT / "ansible/roles/zitadel-org-provision/defaults/main.yml").read_text())
    assert d["zop_allow_register"] is None


@pytest.mark.parametrize("sso,want", [({"org_name": "o"}, None),
                                      ({"login_policy": {}}, None),
                                      ({"login_policy": {"allow_register": False}}, False),
                                      ({"login_policy": {"allow_register": True}}, True)])
def test_consumer_bootstrap_threads_allow_register_and_survives_a_missing_block(sso, want):
    tasks = yaml.safe_load((_ROOT / "ansible/roles/consumer_bootstrap/tasks/main.yml").read_text())
    cmd = next(t for t in tasks if isinstance(t, dict) and t.get("register") == "consumer_sso_run")
    expr = next(a for a in cmd["ansible.builtin.command"]["argv"] if "zop_allow_register" in str(a))
    got = json.loads(_tpl(expr, {"consumer_manifest": {"sso": sso}}))
    assert got == {"zop_allow_register": want}
