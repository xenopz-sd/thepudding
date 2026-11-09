"""Structural + drift tests for the WS-C4 user-provisioning wiring (ADR-0023).

Covers: the live-shape fixture vs the helper's emitted endpoints/bodies; the onboarding mint step
(zitadel-org-provision); the operator/teardown role (zitadel-user-provision) and playbook; the
consumer clean-slate composition; and the secret/least-privilege discipline (no_log, env-only
tokens, the instance IAM_OWNER token never used for user-create). Pure offline.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import zitadel_user_provision as up

_REPO = Path(__file__).resolve().parents[2]
_R = _REPO / "ansible/roles"
_FX = json.loads((_REPO / "tests/fixtures/live-shapes/zitadel-user-management.json").read_text(encoding="utf-8"))


def _y(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _tasks(*parts: str) -> list[dict]:
    return [t for t in _y(_R.joinpath(*parts)) if isinstance(t, dict)]


def _by(tasks, fragment):
    hits = [t for t in tasks if fragment in str(t.get("name", ""))]
    assert len(hits) == 1, (fragment, len(hits))
    return hits[0]


# --------------------------------------------------------------------------- #
# Live-shape fixture <-> emitting code (WS-M4)
# --------------------------------------------------------------------------- #
def test_fixture_matches_the_helper_endpoints():
    assert up.EP_USERS_MACHINE == _FX["machine_user_create"]["endpoint"]
    assert up.EP_ORG_MEMBERS == _FX["org_member"]["add"]["endpoint"]
    assert up.EP_ORG_MEMBERS_SEARCH == _FX["org_member"]["search"]["endpoint"]
    assert up.EP_USERS_HUMAN_IMPORT == _FX["human_user_import"]["endpoint"]
    assert up.EP_USERS_SEARCH == _FX["user_search"]["endpoint"]
    assert up._user_ep("{userId}", "/pats") == _FX["pat"]["mint"]["endpoint"]
    assert up._user_ep("{userId}", "/pats/_search") == _FX["pat"]["search"]["endpoint"]
    assert up._user_ep("{userId}") == _FX["user_delete"]["endpoint"]


def test_fixture_matches_the_helper_least_privilege_role_and_default_name():
    assert up.USERMGR_ROLE == _FX["org_member"]["least_privilege_role"] == "ORG_USER_MANAGER"
    assert up.DEFAULT_USERMGR_NAME == "usermgr-sa"


def test_fixture_matches_the_bodies_the_helper_sends():
    import datetime
    import test_zitadel_user_provision as t  # the FakeZitadel recorder (same test root)

    z = t.FakeZitadel("INSTANCE")
    up.ensure_usermgr(z, t.OWN, now=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    up.create_human_user(t.FakeZitadel(t.OWN), "a@x.test", "A", "B", "a@x.test", "pw")
    machine = next(c for c in z.calls if c[1] == up.EP_USERS_MACHINE)[2]
    assert sorted(machine) == sorted(_FX["machine_user_create"]["body_keys"])
    assert machine["accessTokenType"] == _FX["machine_user_create"]["access_token_type"]
    member = next(c for c in z.calls if c[1] == up.EP_ORG_MEMBERS)[2]
    assert sorted(member) == sorted(_FX["org_member"]["add"]["body_keys"])
    pat = next(c for c in z.calls if c[1].endswith("/pats") and c[0] == "POST")[2]
    assert sorted(pat) == sorted(_FX["pat"]["mint"]["body_keys"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", pat["expirationDate"])
    zh = t.FakeZitadel(t.OWN)
    up.create_human_user(zh, "h@x.test", "H", "U", "h@x.test", "pw")
    human = next(c for c in zh.calls if c[1] == up.EP_USERS_HUMAN_IMPORT)[2]
    assert sorted(human) == sorted(_FX["human_user_import"]["body_keys"])
    assert sorted(human["profile"]) == sorted(_FX["human_user_import"]["profile_keys"])
    assert sorted(human["email"]) == sorted(_FX["human_user_import"]["email_keys"])


def test_fixture_has_no_secret_or_real_host():
    blob = json.dumps(_FX)
    assert not re.search(r"(eyJ|Bearer |token\":\s*\"[A-Za-z0-9_-]{20,})", blob)
    assert "platform.example" not in blob and "10.0." not in blob


# --------------------------------------------------------------------------- #
# Onboarding mint (zitadel-org-provision)
# --------------------------------------------------------------------------- #
def test_main_includes_the_mint_only_when_enabled_wired_and_on_the_production_path():
    main = _tasks("zitadel-org-provision", "tasks", "main.yml")
    inc = _by(main, "Mint the per-org ORG_USER_MANAGER identity (ADR-0023)")
    assert inc["ansible.builtin.include_tasks"] == "usermgr_mint.yml"
    when = " ".join(inc["when"])
    assert "zop_usermgr_enabled" in when and "zop_openbao_addr" in when and "zop_dev_guest_run" in when
    # it comes after the provisioning token resolution and after the per-app OpenBao writes
    names = [str(t.get("name", "")) for t in main]
    assert names.index(inc["name"]) > names.index("ZITADEL-org | Resolve the provisioning token (env override else OpenBao, no_log)")
    assert names.index(inc["name"]) == len(names) - 2, "the mint is the last step (followed only by its WARN)"


def test_mint_tasks_never_leak_the_pat_or_tokens():
    tasks = _tasks("zitadel-org-provision", "tasks", "usermgr_mint.yml")
    for t in tasks:
        text = yaml.safe_dump(t)
        if any(k in text for k in ("zop_provision_token", "zop_bao_token", "zop_usermgr_run.stdout", "zop_usermgr_readback")):
            if "ansible.builtin.debug" in t:
                continue
            assert t.get("no_log") is True, f"task carries a secret but is not no_log: {t.get('name')}"


def test_mint_is_gated_so_a_converged_rerun_does_not_remint():
    tasks = _tasks("zitadel-org-provision", "tasks", "usermgr_mint.yml")
    mint = _by(tasks, "Mint/converge the per-org ORG_USER_MANAGER machine user")
    assert "zop_usermgr_have_secret_arg" in mint["ansible.builtin.command"]["argv"]
    assert "'--have-secret'" in mint["vars"]["zop_usermgr_have_secret_arg"], "the stored-PAT check must feed --have-secret"
    assert "zop_usermgr_stored.rc" in mint["vars"]["zop_usermgr_have_secret_arg"]
    for frag in ("Persist the PAT", "Read back the stored PAT", "Verify the read-back"):
        assert _by(tasks, frag)["when"] == "zop_usermgr_minted | bool"


def test_mint_uses_the_instance_token_only_for_the_mint_and_passes_it_by_env():
    tasks = _tasks("zitadel-org-provision", "tasks", "usermgr_mint.yml")
    mint = _by(tasks, "Mint/converge the per-org ORG_USER_MANAGER machine user")
    assert mint["environment"] == {"ZITADEL_PROVISION_TOKEN": "{{ zop_provision_token }}"}
    argv_text = json.dumps(mint["ansible.builtin.command"]["argv"])
    assert "zop_provision_token" not in argv_text, "never on argv"


def test_mint_defaults():
    d = _y(_R / "zitadel-org-provision/defaults/main.yml")
    assert d["zop_usermgr_enabled"] is True and d["zop_usermgr_name"] == "usermgr-sa"
    assert 30 <= d["zop_usermgr_pat_days"] <= 365, "bounded lifetime"
    assert d["zop_openbao_usermgr_secret_path"].endswith("/proj-{{ zop_project_slug }}/zitadel/usermgr")


# --------------------------------------------------------------------------- #
# Operator path + teardown (zitadel-user-provision)
# --------------------------------------------------------------------------- #
def test_create_never_touches_the_instance_iam_owner_token():
    text = (_R / "zitadel-user-provision/tasks/create.yml").read_text(encoding="utf-8")
    assert "ZITADEL_PROVISION_TOKEN" not in text and "provision_token" not in text and "zup_iam" not in text
    assert "ZITADEL_USERMGR_TOKEN" in text


def test_create_reads_the_pat_from_openbao_and_the_password_from_the_environment_only():
    tasks = _tasks("zitadel-user-provision", "tasks", "create.yml")
    read = _by(tasks, "Read the org's user-manager PAT from OpenBao")
    assert read["no_log"] is True and read["failed_when"] is False
    run = _by(tasks, "Create the human user in org")
    assert run["no_log"] is True
    assert set(run["environment"]) == {"ZITADEL_USERMGR_TOKEN", "ZITADEL_NEW_USER_PASSWORD"}
    argv = run["ansible.builtin.command"]["argv"]
    assert "zup_new_user_password" not in argv and "ZITADEL_NEW_USER_PASSWORD" not in argv, "the password is env-only"
    assert "zup_pat_read" not in argv and "ZITADEL_USERMGR_TOKEN" not in argv, "the PAT is env-only"
    pw = _by(tasks, "Bind the new user's initial password from the environment")
    assert "lookup('ansible.builtin.env', 'ZITADEL_NEW_USER_PASSWORD')" in pw["ansible.builtin.set_fact"]["zup_new_user_password"]


def test_create_fails_fast_without_a_pat_and_names_the_path():
    tasks = _tasks("zitadel-user-provision", "tasks", "create.yml")
    a = _by(tasks, "Fail fast if the user-manager PAT is absent")
    assert "Onboard the project first" in a["ansible.builtin.assert"]["fail_msg"]


def test_teardown_removes_machine_user_and_secret_and_gates_on_no_residue():
    tasks = _tasks("zitadel-user-provision", "tasks", "teardown.yml")
    rm = _by(tasks, "Delete the org's user-manager machine user")
    assert "remove-usermgr" in rm["ansible.builtin.command"]["argv"]
    assert rm["environment"] == {"ZITADEL_PROVISION_TOKEN": "{{ zup_iam_token }}"}
    sec = _by(tasks, "Delete the user-manager secret from OpenBao")
    assert "metadata" in sec["ansible.builtin.command"]["argv"] and "delete" in sec["ansible.builtin.command"]["argv"]
    gate = _by(tasks, "GATE")
    assert gate["ansible.builtin.assert"]["that"] == ["(zup_secret_verify.rc | default(0)) != 0"]


def test_every_secret_bearing_task_in_the_new_role_is_no_log():
    for f in ("create.yml", "teardown.yml", "main.yml"):
        for t in _tasks("zitadel-user-provision", "tasks", f):
            text = yaml.safe_dump(t)
            if ("zup_bao_token" in text or "zup_pat_read" in text or "zup_iam" in text or "zup_new_user_password" in text) \
                    and "ansible.builtin.debug" not in t:
                assert t.get("no_log") is True, (f, t.get("name"))


def test_role_defaults_carry_no_secret_and_action_defaults_to_create():
    d = _y(_R / "zitadel-user-provision/defaults/main.yml")
    assert d["zup_action"] == "create"
    for k, v in d.items():
        assert not re.search(r"(token|password|secret)$", k) or v in ("", None) or "path" in k or "field" in k, k


def test_playbook_runs_the_role_on_localhost():
    play = _y(_REPO / "ansible/playbooks/zitadel-user-provision.yml")[0]
    assert play["hosts"] == "localhost" and play["roles"] == ["zitadel-user-provision"]


# --------------------------------------------------------------------------- #
# Consumer clean-slate composes the teardown (WS-M2 parity)
# --------------------------------------------------------------------------- #
def test_consumer_clean_slate_composes_the_teardown_for_sso_projects_with_openbao():
    tasks = _tasks("consumer_clean_slate", "tasks", "main.yml")
    t = _by(tasks, "Compose the user-manager identity teardown")
    argv = [str(a) for a in t["ansible.builtin.command"]["argv"]]
    assert "ansible/playbooks/zitadel-user-provision.yml" in argv and "zup_action=teardown" in argv
    assert any(a.startswith("zup_project_slug=") for a in argv)
    assert "consumer_manifest.sso is defined" in t["when"]
    assert any("openbao_addr" in w for w in t["when"])
    d = _y(_R / "consumer_clean_slate/defaults/main.yml")
    assert d["consumer_cs_usermgr_teardown_strict"] is True, "strict by default: no silent residue"
    assert "consumer_cs_usermgr_teardown_strict | bool" in " ".join(t["failed_when"])


def test_consumer_clean_slate_never_calls_zitadel_or_bao_directly_for_this():
    t = _by(_tasks("consumer_clean_slate", "tasks", "main.yml"), "Compose the user-manager identity teardown")
    assert "docker" not in json.dumps(t["ansible.builtin.command"]["argv"])
