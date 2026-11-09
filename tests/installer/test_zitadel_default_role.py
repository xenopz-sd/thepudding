"""WS-C18 — a default project role for self-registered ZITADEL users (spec fix-zitadel-default-role-on-registration).

Evidence from WS-C17: a user who self-registers through the hosted login lands in the project org (with the org scope) but has
NO project role, because ZITADEL has no built-in default-role grant. The sanctioned mechanism on ZITADEL v4.18 is an Actions v1
action on the Internal Authentication flow, trigger Post Creation, that pushes a user grant. The helper RECONCILES one
platform-owned action plus its trigger membership and verifies by read-back. Absent => untouched (backward compatible).

Pure offline: urllib is replaced by a small stateful fake of the ZITADEL management API.
"""
from __future__ import annotations

import json
import re
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

_FIXTURE = _ROOT / "tests" / "fixtures" / "live-shapes" / "zitadel-actions-default-role.json"


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


class FakeZitadel:
    """The slice of the management API the helper touches, with live-confirmed shapes."""

    def __init__(self, roles=("admin", "member", "viewer"), actions=None, trigger_ids=None):
        self.roles = list(roles)
        self.actions = {a["id"]: dict(a) for a in (actions or [])}
        self.trigger_ids = list(trigger_ids or [])
        self.calls = []
        self._n = 100

    def writes(self):
        return [c for c in self.calls if c["method"] != "GET" and not c["url"].endswith("/_search")]

    def __call__(self, req, timeout=None, context=None):
        m, url = req.get_method(), req.full_url
        body = json.loads(req.data) if req.data else None
        path = re.sub(r"^https?://[^/]+", "", url)
        self.calls.append({"method": m, "url": url, "path": path, "body": body})
        if m == "POST" and re.fullmatch(r"/management/v1/projects/[^/]+/roles/_search", path):
            return _Resp(200, {"result": [{"key": k} for k in self.roles]})
        if m == "POST" and path == "/management/v1/actions/_search":
            return _Resp(200, {"result": [self._view(a) for a in self.actions.values()]} if self.actions else {})
        if m == "POST" and path == "/management/v1/actions":
            self._n += 1
            aid = f"act-{self._n}"
            self.actions[aid] = {"id": aid, **body, "state": "ACTION_STATE_ACTIVE"}
            return _Resp(200, {"id": aid})
        mm = re.fullmatch(r"/management/v1/actions/([^/]+)", path)
        if mm and m == "PUT":
            self.actions[mm[1]].update(body)
            return _Resp(200, {})
        if mm and m == "DELETE":
            self.actions.pop(mm[1])
            return _Resp(200, {})
        if m == "GET" and path == "/management/v1/flows/3":
            ta = [{"triggerType": {"id": "3"}, "actions": [self._view(self.actions[i]) for i in self.trigger_ids
                                                          if i in self.actions]}] if self.trigger_ids else []
            return _Resp(200, {"flow": {"type": {"id": "3"}, "triggerActions": ta} if ta else {"type": {"id": "3"}}})
        if m == "POST" and path == "/management/v1/flows/3/trigger/3":
            self.trigger_ids = list(body["actionIds"])
            return _Resp(200, {})
        raise AssertionError(f"unexpected request {m} {path}")

    @staticmethod
    def _view(a):
        return {"id": a["id"], "name": a["name"], "script": a["script"], "timeout": a.get("timeout", "10s"),
                "allowedToFail": a.get("allowedToFail", False), "state": a.get("state", "ACTION_STATE_ACTIVE")}


def _client(fake):
    with mock.patch("urllib.request.urlopen", fake):
        return zp.ZitadelClient("https://auth.test", "tok", org_id="org-1")


def _ensure(fake, role, project="proj-1"):
    c = _client(fake)
    with mock.patch("urllib.request.urlopen", fake):
        return c.ensure_default_role(project, "org-1", role)


def _ours(fake):
    return [a for a in fake.actions.values() if a["name"] == zp.DEFAULT_ROLE_ACTION_NAME]


# --------------------------------------------------------------------------- #
# Non-vacuity and recorded live shape
# --------------------------------------------------------------------------- #
def test_non_vacuity_the_helper_symbols_exist():
    assert zp.DEFAULT_ROLE_ACTION_NAME == "platformDefaultRole"
    assert hasattr(zp.ZitadelClient, "ensure_default_role")
    assert _FIXTURE.is_file()


def test_the_helper_is_bound_to_the_recorded_live_shape():
    fx = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fx["_provenance"]["live_confirmed"] is True and "v4.18.0" in fx["_provenance"]["live_confirmed_on"]
    assert zp.EP_ACTIONS == fx["actions"]["create"]["path"] == fx["actions"]["search"]["path"].replace("/_search", "")
    assert zp.EP_ACTION == fx["actions"]["update"]["path"] == fx["actions"]["delete"]["path"]
    assert zp.EP_FLOW_INTERNAL_AUTH == fx["flow"]["get"]["path"]
    assert zp.EP_POST_CREATION_TRIGGER == fx["flow"]["set_trigger"]["path"]
    assert (zp.FLOW_INTERNAL_AUTH, zp.TRIGGER_POST_CREATION) == (fx["flow"]["flow_type_id"], fx["flow"]["trigger_type_id"])
    assert fx["action_function_name_must_equal_action_name"] is True


# --------------------------------------------------------------------------- #
# Reconcile behaviour
# --------------------------------------------------------------------------- #
def test_nothing_present_creates_the_action_and_attaches_it_to_post_creation():
    f = FakeZitadel()
    out = _ensure(f, "viewer")
    (a,) = _ours(f)
    assert out["changed"] is True and out["role"] == "viewer" and out["action_id"] == a["id"]
    assert f.trigger_ids == [a["id"]]
    assert a["name"] == a["script"].split("function ")[1].split("(")[0], "ZITADEL requires function name == action name"
    assert "projectID: 'proj-1'" in a["script"] and "roles: ['viewer']" in a["script"]
    assert "api.userGrants.push" in a["script"]


def test_already_converged_makes_no_write_and_reports_unchanged():
    f = FakeZitadel()
    _ensure(f, "viewer")
    before = len(f.writes())
    out = _ensure(f, "viewer")
    assert out["changed"] is False
    assert len(f.writes()) == before, "an idempotent re-run must not mutate"


def test_changing_the_role_updates_the_script_in_place_without_duplicating_the_action():
    f = FakeZitadel()
    _ensure(f, "viewer")
    (first,) = _ours(f)
    out = _ensure(f, "member")
    (a,) = _ours(f)
    assert a["id"] == first["id"] and "roles: ['member']" in a["script"] and "viewer" not in a["script"]
    assert out["changed"] is True and f.trigger_ids == [a["id"]]


def test_other_actions_on_the_trigger_are_preserved_when_attaching_ours():
    other = {"id": "act-other", "name": "someoneElse", "script": "function someoneElse(c,a){}", "timeout": "10s"}
    f = FakeZitadel(actions=[other], trigger_ids=["act-other"])
    _ensure(f, "viewer")
    (a,) = _ours(f)
    assert f.trigger_ids == ["act-other", a["id"]]
    assert "act-other" in f.actions, "an action we do not own is never touched"


def test_an_existing_action_not_on_the_trigger_is_reattached():
    f = FakeZitadel()
    _ensure(f, "viewer")
    f.trigger_ids = []
    out = _ensure(f, "viewer")
    assert out["changed"] is True and f.trigger_ids == [_ours(f)[0]["id"]]


def test_a_role_that_is_not_in_the_project_is_refused_before_any_write():
    f = FakeZitadel(roles=("admin",))
    with pytest.raises(zp.ZitadelError, match="not a role of the project"):
        _ensure(f, "viewer")
    assert f.writes() == []


def test_removal_detaches_and_deletes_only_our_action_and_leaves_others():
    other = {"id": "act-other", "name": "someoneElse", "script": "function someoneElse(c,a){}", "timeout": "10s"}
    f = FakeZitadel(actions=[other], trigger_ids=["act-other"])
    _ensure(f, "viewer")
    out = _ensure(f, None)
    assert out["changed"] is True and out["role"] is None
    assert _ours(f) == [] and f.trigger_ids == ["act-other"] and "act-other" in f.actions


def test_removal_when_nothing_is_there_is_a_no_op():
    f = FakeZitadel()
    out = _ensure(f, None)
    assert out["changed"] is False and f.writes() == []


def test_read_back_gate_fails_loud_when_the_trigger_does_not_keep_the_action():
    class Lossy(FakeZitadel):
        def __call__(self, req, timeout=None, context=None):
            r = super().__call__(req, timeout, context)
            if req.get_method() == "POST" and req.full_url.endswith("/flows/3/trigger/3"):
                self.trigger_ids = []  # server silently drops it
            return r

    with pytest.raises(zp.ZitadelError, match="did not converge"):
        _ensure(Lossy(), "viewer")


# --------------------------------------------------------------------------- #
# CLI / provision() plumbing: absent => untouched
# --------------------------------------------------------------------------- #
def test_cli_exposes_default_role_flags_and_provision_omits_the_block_when_unset():
    src = (_ROOT / "scripts/installer/zitadel_provision.py").read_text(encoding="utf-8")
    assert "--default-role" in src and "--no-default-role" in src
    assert "default_role: str | None = None" in src and "if default_role_result is not None" in src


# --------------------------------------------------------------------------- #
# Manifest + role threading
# --------------------------------------------------------------------------- #
def _manifest(**sso_extra):
    sso = {"org_name": "o", "app_registrations": [{"name": "a", "app_type": "public-spa", "auth_method": "none"}]}
    sso.update(sso_extra)
    return {"slug": "p", "sso": sso}


def test_manifest_accepts_a_role_key_or_false_and_stays_backward_compatible():
    cm.validate_manifest(_manifest())
    cm.validate_manifest(_manifest(default_role="viewer"))
    cm.validate_manifest(_manifest(default_role=False))
    cm.validate_manifest(_manifest(roles=["owner", "observer"], default_role="observer"))


@pytest.mark.parametrize("bad,match", [(True, "default_role"), ("", "default_role"), (7, "default_role"),
                                       ("Bad Key!", "default_role")])
def test_manifest_rejects_a_malformed_default_role(bad, match):
    with pytest.raises(cm.ManifestValidationError, match=match):
        cm.validate_manifest(_manifest(default_role=bad))


def test_manifest_rejects_a_default_role_that_is_not_in_the_declared_vocabulary():
    with pytest.raises(cm.ManifestValidationError, match="not among"):
        cm.validate_manifest(_manifest(roles=["owner"], default_role="viewer"))
    with pytest.raises(cm.ManifestValidationError, match="not among"):
        cm.validate_manifest(_manifest(default_role="owner"))  # default vocabulary is admin/member/viewer


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _zop_vars(frag):
    tasks = yaml.safe_load((_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text())
    return next(t for t in tasks if frag in str(t.get("name")))["vars"]


@pytest.mark.parametrize("frag", ["Provision each app via the helper", "DEV: provision each app via the helper"])
@pytest.mark.parametrize("val,want", [("viewer", ["--default-role=viewer"]), (False, ["--no-default-role"]), (None, [])])
def test_role_renders_the_tristate_arg_in_both_paths(frag, val, want):
    assert _tpl(_zop_vars(frag)["zop_default_role_arg"], {"zop_default_role": val}) == want


def test_role_argv_includes_the_arg_in_both_paths_and_default_is_null():
    text = (_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text()
    assert text.count("zop_default_role_arg }}") + text.count("zop_default_role_arg + ") >= 2
    d = yaml.safe_load((_ROOT / "ansible/roles/zitadel-org-provision/defaults/main.yml").read_text())
    assert d["zop_default_role"] is None


def test_the_changed_when_of_both_paths_counts_a_default_role_change():
    tasks = yaml.safe_load((_ROOT / "ansible/roles/zitadel-org-provision/tasks/main.yml").read_text())
    for reg in ("zop_provision_run_guest", "zop_provision_run_prod"):
        t = next(x for x in tasks if isinstance(x, dict) and x.get("register") == reg)
        assert "default_role.changed" in " ".join(str(t["changed_when"]).split()), reg


@pytest.mark.parametrize("sso,want", [({"org_name": "o"}, None), ({"default_role": "viewer"}, "viewer"),
                                      ({"default_role": False}, False)])
def test_consumer_bootstrap_threads_default_role_and_survives_a_missing_key(sso, want):
    tasks = yaml.safe_load((_ROOT / "ansible/roles/consumer_bootstrap/tasks/main.yml").read_text())
    cmd = next(t for t in tasks if isinstance(t, dict) and t.get("register") == "consumer_sso_run")
    expr = next(a for a in cmd["ansible.builtin.command"]["argv"] if "zop_default_role" in str(a))
    assert json.loads(_tpl(expr, {"consumer_manifest": {"sso": sso}})) == {"zop_default_role": want}


@pytest.mark.parametrize("project,role", [("p'; evil(); '", "viewer"), ("proj-1", "v'] }); x(); //"), ("proj-1", "a b")])
def test_unsafe_identifiers_are_refused_not_escaped_into_the_script(project, role):
    f = FakeZitadel(roles=(role,))
    with pytest.raises(zp.ZitadelError, match="unsafe"):
        _ensure(f, role, project=project)
    assert f.writes() == []
