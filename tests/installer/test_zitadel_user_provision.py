"""Offline tests for scripts/installer/zitadel_user_provision.py (WS-C4, ADR-0023).

A FakeZitadel models the parts of the management API the helper uses (machine users, org
members, PATs, human import, search, delete) and the refusals observed live: another org -> 404
"membership not found", org/project create -> 403. These tests pin the contract: least
privilege (exactly ORG_USER_MANAGER), idempotence, PAT minted only when needed, own-org-only user
creation, teardown parity, and that secrets stay out of argv and output.
"""
from __future__ import annotations

import datetime
import itertools
import json

import pytest

import zitadel_user_provision as up

OWN, OTHER = "org-own", "org-other"


class FakeZitadel:
    """Stands in for ZitadelClient: only ``_request`` (and ``_find_org_by_name``) are used."""

    def __init__(self, token_org=OWN):
        self.token_org = token_org
        self._ids = itertools.count(1)
        self.users: dict[str, dict] = {}      # id -> {org, userName, machine, humans...}
        self.members: dict[tuple, list] = {}  # (org, userId) -> roles
        self.pats: dict[str, dict] = {}       # tokenId -> {user, expirationDate}
        self.calls: list[tuple] = []
        self.orgs = {"proj-x": OWN, "proj-y": OTHER}

    def _find_org_by_name(self, name):
        return self.orgs.get(name)

    def _request(self, method, path, body=None, *, org_id=None):
        self.calls.append((method, path, body, org_id))
        org = org_id or self.token_org
        if org != self.token_org and self.token_org != "INSTANCE":
            raise up.ZitadelError(f"ZITADEL API {method} {path} failed: HTTP 404 membership not found")
        if path == "/management/v1/users/_search":
            name = body["queries"][0]["userNameQuery"]["userName"]
            return 200, {"result": [{"id": i, "userName": u["userName"]} for i, u in self.users.items()
                                    if u["org"] == org and u["userName"] == name]}
        if path == "/management/v1/users/machine":
            uid = f"u{next(self._ids)}"
            self.users[uid] = {"org": org, "userName": body["userName"], "machine": True}
            return 200, {"userId": uid}
        if path == "/management/v1/users/human/_import":
            if any(u["org"] == org and u["userName"] == body["userName"] for u in self.users.values()):
                return 409, {"message": "User already exists"}
            uid = f"u{next(self._ids)}"
            self.users[uid] = {"org": org, "userName": body["userName"], "machine": False}
            return 200, {"userId": uid}
        if path == "/management/v1/orgs/me/members" and method == "POST":
            key = (org, body["userId"])
            if key in self.members:
                return 409, {"message": "member exists"}
            self.members[key] = list(body["roles"])
            return 200, {}
        if path == "/management/v1/orgs/me/members/_search":
            uid = body["queries"][0]["userIdQuery"]["userId"]
            roles = self.members.get((org, uid))
            return 200, {"result": [{"userId": uid, "roles": roles}] if roles is not None else []}
        if path.startswith("/management/v1/orgs/me/members/") and method == "PUT":
            uid = path.rsplit("/", 1)[1]
            if self.members.get((org, uid)) == body["roles"]:
                raise up.ZitadelError("HTTP 400 Errors.Org.Member.NotChanged")
            self.members[(org, uid)] = list(body["roles"])
            return 200, {}
        if path.endswith("/pats/_search"):
            uid = path.split("/")[4]
            return 200, {"result": [{"id": t} for t, p in self.pats.items() if p["user"] == uid]}
        if path.endswith("/pats") and method == "POST":
            uid = path.split("/")[4]
            tid = f"t{next(self._ids)}"
            self.pats[tid] = {"user": uid, "expirationDate": body["expirationDate"]}
            return 200, {"tokenId": tid, "token": f"PAT-SECRET-{tid}"}
        if "/pats/" in path and method == "DELETE":
            self.pats.pop(path.rsplit("/", 1)[1], None)
            return 200, {}
        if path.startswith("/management/v1/users/") and method == "DELETE":
            self.users.pop(path.rsplit("/", 1)[1], None)
            return 200, {}
        if path in ("/management/v1/orgs", "/management/v1/projects"):
            raise up.ZitadelError(f"ZITADEL API {method} {path} failed: HTTP 403 No matching permissions found")
        raise AssertionError(f"unmodelled call {method} {path}")


NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _mint(z, **kw):
    return up.ensure_usermgr(z, OWN, now=NOW, **kw)


# --------------------------------------------------------------------------- #
# mint-usermgr
# --------------------------------------------------------------------------- #
def test_mint_creates_machine_user_with_exactly_org_user_manager():
    z = FakeZitadel("INSTANCE")
    r = _mint(z)
    assert r["created"] is True and r["role"] == "ORG_USER_MANAGER" and r["member"] == "added"
    assert z.members[(OWN, r["user_id"])] == ["ORG_USER_MANAGER"], "least privilege: ONLY ORG_USER_MANAGER"
    assert not any(c[1] == "/management/v1/orgs" for c in z.calls), "never creates orgs"


def test_mint_returns_a_bounded_pat_once():
    z = FakeZitadel("INSTANCE")
    r = _mint(z, pat_days=90)
    assert r["pat_minted"] is True and r["pat"].startswith("PAT-SECRET-")
    assert r["pat_expires"] == "2026-04-01T00:00:00Z", "bounded lifetime (90 days from now)"


def test_rerun_with_secret_present_converges_without_minting_a_new_pat():
    z = FakeZitadel("INSTANCE")
    first = _mint(z)
    again = _mint(z, have_secret=True)
    assert again["created"] is False and again["pat_minted"] is False and "pat" not in again
    assert again["user_id"] == first["user_id"] and again["member"] == "ok"
    assert len(z.pats) == 1, "no extra PAT"


def test_rerun_with_secret_missing_replaces_stale_pats_and_remints():
    z = FakeZitadel("INSTANCE")
    first = _mint(z)
    again = _mint(z, have_secret=False)
    assert again["pat_minted"] is True and again["pat"] != first["pat"]
    assert len(z.pats) == 1, "the old PAT was deleted before minting the new one"


def test_escalated_membership_is_converged_back_to_least_privilege():
    z = FakeZitadel("INSTANCE")
    r = _mint(z)
    z.members[(OWN, r["user_id"])] = ["ORG_OWNER", "ORG_USER_MANAGER"]
    again = _mint(z, have_secret=True)
    assert again["member"] == "converged"
    assert z.members[(OWN, r["user_id"])] == ["ORG_USER_MANAGER"]


# --------------------------------------------------------------------------- #
# create-user (paths a and b)
# --------------------------------------------------------------------------- #
def test_create_user_acts_only_on_the_token_owners_org_no_org_header():
    z = FakeZitadel(OWN)
    r = up.create_human_user(z, "alice@x.test", "Alice", "A", "alice@x.test", "S3cret-pw!")
    assert r["created"] is True and r["already_existed"] is False
    assert all(c[3] is None for c in z.calls), "no x-zitadel-orgid: it can only be the PAT's own org"
    assert "S3cret-pw!" not in json.dumps(r), "the password is never returned"


def test_create_user_is_idempotent():
    z = FakeZitadel(OWN)
    first = up.create_human_user(z, "alice@x.test", "Alice", "A", "alice@x.test", "pw-1")
    again = up.create_human_user(z, "alice@x.test", "Alice", "A", "alice@x.test", "pw-1")
    assert again["created"] is False and again["already_existed"] is True and again["user_id"] == first["user_id"]
    assert sum(1 for u in z.users.values() if u["userName"] == "alice@x.test") == 1


def test_create_user_requires_a_password():
    with pytest.raises(ValueError) as e:
        up.create_human_user(FakeZitadel(OWN), "a@x.test", "A", "B", "a@x.test", "")
    assert "ZITADEL_NEW_USER_PASSWORD" in str(e.value)


def test_password_change_required_by_default_and_overridable_for_throwaway():
    z = FakeZitadel(OWN)
    up.create_human_user(z, "a@x.test", "A", "B", "a@x.test", "pw")
    up.create_human_user(z, "b@x.test", "A", "B", "b@x.test", "pw", password_change_required=False)
    bodies = [c[2] for c in z.calls if c[1] == up.EP_USERS_HUMAN_IMPORT]
    assert [b["passwordChangeRequired"] for b in bodies] == [True, False]


def test_a_creator_race_converges_on_409():
    z = FakeZitadel(OWN)
    real = z._request

    def racing(method, path, body=None, *, org_id=None):
        if path == up.EP_USERS_HUMAN_IMPORT:
            z.users["race"] = {"org": OWN, "userName": body["userName"], "machine": False}
            return 409, {"message": "exists"}
        return real(method, path, body, org_id=org_id)

    z._request = racing
    r = up.create_human_user(z, "r@x.test", "R", "R", "r@x.test", "pw")
    assert r["already_existed"] is True and r["user_id"] == "race"


# --------------------------------------------------------------------------- #
# remove-usermgr (teardown parity)
# --------------------------------------------------------------------------- #
def test_remove_deletes_the_machine_user_then_is_idempotent():
    z = FakeZitadel("INSTANCE")
    r = _mint(z)
    out = up.remove_usermgr(z, OWN)
    assert out["removed"] is True and r["user_id"] not in z.users
    again = up.remove_usermgr(z, OWN)
    assert again["removed"] is False and again["already_absent"] is True


# --------------------------------------------------------------------------- #
# probe-refusals (least-privilege proof)
# --------------------------------------------------------------------------- #
def test_probe_reports_all_refused_when_zitadel_refuses():
    z = FakeZitadel(OWN)
    r = up.probe_refusals(z, OTHER)
    assert r["all_refused"] is True
    assert r["create_user_in_other_org"]["http"] == 404
    assert r["create_org"]["http"] == 403 and r["create_project_in_own_org"]["http"] == 403


def test_probe_reports_failure_if_the_credential_is_too_powerful():
    z = FakeZitadel("INSTANCE")  # models an over-privileged token that is NOT refused cross-org
    real = z._request

    def allow_projects(method, path, body=None, *, org_id=None):
        if path == "/management/v1/projects":
            return 200, {}
        return real(method, path, body, org_id=org_id)

    z._request = allow_projects
    r = up.probe_refusals(z, OTHER)
    assert r["all_refused"] is False and r["create_project_in_own_org"]["refused"] is False


# --------------------------------------------------------------------------- #
# CLI: secrets stay in the environment
# --------------------------------------------------------------------------- #
def test_cli_has_no_token_or_password_flags():
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        up.main(["--base-url", "https://x", "mint-usermgr", "--help"])
    helptext = buf.getvalue().lower()
    assert "--token" not in helptext and "--password" not in helptext and "--pat" not in helptext.replace("--pat-days", "")


def test_cli_exits_2_without_the_instance_token(monkeypatch, capsys):
    monkeypatch.delenv("ZITADEL_PROVISION_TOKEN", raising=False)
    with pytest.raises(SystemExit) as e:
        up.main(["--base-url", "https://x", "mint-usermgr", "--org", "proj-x"])
    assert e.value.code == 2
    assert "ZITADEL_PROVISION_TOKEN" in capsys.readouterr().err


def test_cli_create_user_needs_the_usermgr_token_not_the_instance_token(monkeypatch, capsys):
    monkeypatch.setenv("ZITADEL_PROVISION_TOKEN", "instance-admin")
    monkeypatch.delenv("ZITADEL_USERMGR_TOKEN", raising=False)
    with pytest.raises(SystemExit) as e:
        up.main(["--base-url", "https://x", "create-user", "--username", "a@x.test",
                 "--first-name", "A", "--last-name", "B", "--email", "a@x.test"])
    assert e.value.code == 2, "the instance IAM_OWNER token must never be accepted for user-create"
    assert "ZITADEL_USERMGR_TOKEN" in capsys.readouterr().err


def test_cli_mint_prints_the_pat_only_in_stdout_json(monkeypatch, capsys):
    z = FakeZitadel("INSTANCE")
    monkeypatch.setenv("ZITADEL_PROVISION_TOKEN", "instance-admin")
    monkeypatch.setattr(up, "ZitadelClient", lambda *a, **k: z)
    rc = up.main(["--base-url", "https://x", "mint-usermgr", "--org", "proj-x"])
    cap = capsys.readouterr()
    assert rc == 0 and json.loads(cap.out)["pat"].startswith("PAT-SECRET-")
    assert "PAT-SECRET" not in cap.err and "instance-admin" not in cap.out + cap.err


def test_cli_create_user_output_never_contains_the_password(monkeypatch, capsys):
    z = FakeZitadel(OWN)
    monkeypatch.setenv("ZITADEL_USERMGR_TOKEN", "um-pat")
    monkeypatch.setenv("ZITADEL_NEW_USER_PASSWORD", "Sup3r-S3cret!")
    monkeypatch.setattr(up, "ZitadelClient", lambda *a, **k: z)
    rc = up.main(["--base-url", "https://x", "create-user", "--username", "a@x.test",
                  "--first-name", "A", "--last-name", "B", "--email", "a@x.test"])
    cap = capsys.readouterr()
    assert rc == 0 and "Sup3r-S3cret!" not in cap.out + cap.err and "um-pat" not in cap.out + cap.err


def test_cli_remove_for_an_absent_org_is_a_noop(monkeypatch, capsys):
    z = FakeZitadel("INSTANCE")
    monkeypatch.setenv("ZITADEL_PROVISION_TOKEN", "instance-admin")
    monkeypatch.setattr(up, "ZitadelClient", lambda *a, **k: z)
    rc = up.main(["--base-url", "https://x", "remove-usermgr", "--org", "no-such-org"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["org_absent"] is True
