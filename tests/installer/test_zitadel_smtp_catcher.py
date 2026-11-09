"""Mock 7 — ZITADEL instance SMTP -> dev mail catcher (spec dev-mail-catcher, R4-R8, ADR-0025).

Helper (``scripts/installer/zitadel_smtp.py``): idempotent reconcile with a read-back gate, never
clobbers a real provider, token via ENV/header only. Role (``zitadel_smtp_catcher``): the swap-gate
(flag OFF => untouched), fail-closed when the catcher is absent, never enabled by merge. The wire
shapes are bound to the committed live-shape fixture (testing-strategy.md "Live-Shape Fixtures").
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
import zitadel_smtp as zs  # noqa: E402

_FIXTURE = _ROOT / "tests" / "fixtures" / "live-shapes" / "zitadel-email-provider-smtp.json"
_ROLE = _ROOT / "ansible" / "roles" / "zitadel_smtp_catcher"
_HOST, _PORT, _SENDER, _NAME = "10.0.20.21", 1025, "noreply@platform.example.test", "ZITADEL (dev)"


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


def _404():
    return urllib.error.HTTPError("http://x", 404, "nf", None, io.BytesIO(b'{"message":"not found"}'))


class _Transport:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, context=None):
        self.calls.append({"method": req.get_method(), "path": req.full_url.split("test", 1)[-1].split(".test", 1)[-1],
                           "url": req.full_url, "headers": dict(req.header_items()),
                           "body": json.loads(req.data) if req.data else None})
        if not self._script:
            raise AssertionError(f"unexpected extra request {req.get_method()} {req.full_url}")
        r = self._script.pop(0)
        if isinstance(r, urllib.error.HTTPError):
            raise r
        return r

    def mutations(self):
        return [c for c in self.calls if c["method"] != "GET"]


def _cfg(*, ours=True, host=f"{_HOST}:{_PORT}", state=zs.STATE_ACTIVE, smtp=True, pid="p1",
         description=None, tls=False):
    c = {"id": pid, "state": state,
         "description": zs.MARKER_DESCRIPTION if ours else (description or "Mailjet prod relay")}
    if smtp:
        c["smtp"] = {"senderAddress": _SENDER, "senderName": _NAME, "tls": tls, "host": host, "none": {}}
    else:
        c["http"] = {"endpoint": "https://hook.example.test"}
    return _Resp(200, {"config": c})


def _client():
    return zs.Client("https://auth.test", "tok-secret-value")


def _run(script, fn):
    t = _Transport(script)
    with mock.patch("urllib.request.urlopen", t):
        return fn(_client()), t


# --------------------------------------------------------------------------- #
# Bound to the live-shape fixture (testing-strategy: the assertion has teeth)
# --------------------------------------------------------------------------- #
def _fx():
    assert _FIXTURE.is_file(), "fixture missing"
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_helper_endpoints_equal_the_recorded_shape():
    fx = _fx()
    base = fx["base_path"]
    assert zs.EP_EMAIL == base + fx["get_active"]["path"]
    assert zs.EP_EMAIL_SMTP == base + fx["add_smtp"]["path"]
    assert zs.EP_EMAIL_SMTP_ID == base + fx["update_smtp"]["path"]
    assert zs.EP_EMAIL_ACTIVATE == base + fx["activate"]["path"]
    assert zs.EP_EMAIL_ID == base + fx["remove"]["path"]
    assert zs.STATE_ACTIVE == fx["get_active"]["active_state"]


def test_emitted_body_has_exactly_the_recorded_fields_and_no_auth_member():
    fx = _fx()
    body = zs.desired_body(_HOST, _PORT, _SENDER, _NAME)
    assert sorted(body) == sorted(fx["smtp_body_fields"])
    assert body["host"] == f"{_HOST}:{_PORT}" and fx["host_includes_port"] is True
    assert body["none"] == {} == fx["no_auth_body_member"]["none"]
    assert body["tls"] is False
    assert not ({"plain", "xoauth2", "user", "password"} & set(body)), "no credential on the dev path"


def test_fixture_records_the_live_confirmation_on_the_pinned_version():
    prov = _fx()["_provenance"]
    assert prov["live_confirmed"] is True and "v4.18.0" in prov["live_confirmed_on"]
    assert _fx()["tls_false_is_omitted_from_responses"] is True
    assert "404" in _fx()["get_active"]["absent"]


def test_login_policy_helper_is_bound_to_the_recorded_shape():
    import zitadel_provision as zp
    lp = _fx()["login_policy"]
    assert zp.EP_LOGIN_POLICY == lp["get"]["path"] == lp["create_custom"]["path"] == lp["update_custom"]["path"]
    assert set(zp._LOGIN_POLICY_FIELDS) == set(lp["request_fields_echoed"])
    assert not (set(zp._LOGIN_POLICY_FIELDS) & set(lp["read_only_response_fields_never_echoed"]))


# --------------------------------------------------------------------------- #
# Reconcile behaviour
# --------------------------------------------------------------------------- #
def test_nothing_set_adds_then_activates_then_reads_back():
    out, t = _run([_404(), _Resp(200, {"id": "p1"}), _Resp(200, {}), _cfg()],
                  lambda c: zs.ensure_catcher(c, _HOST, _PORT, _SENDER, _NAME))
    assert out["action"] == "added" and out["changed"] is True and out["provider_id"] == "p1"
    assert [(c["method"], c["url"].split("auth.test")[1]) for c in t.calls] == [
        ("GET", "/admin/v1/email"), ("POST", "/admin/v1/email/smtp"),
        ("POST", "/admin/v1/email/p1/_activate"), ("GET", "/admin/v1/email")]
    assert t.calls[1]["body"] == zs.desired_body(_HOST, _PORT, _SENDER, _NAME)


def test_already_matching_makes_no_mutation_and_reports_unchanged():
    out, t = _run([_cfg(), _cfg()], lambda c: zs.ensure_catcher(c, _HOST, _PORT, _SENDER, _NAME))
    assert out["changed"] is False and out["action"] == "unchanged"
    assert t.mutations() == []


def test_drifted_catcher_host_is_reconciled_with_a_put():
    out, t = _run([_cfg(host="10.0.20.99:1025"), _Resp(200, {}), _cfg()],
                  lambda c: zs.ensure_catcher(c, _HOST, _PORT, _SENDER, _NAME))
    assert out["action"] == "updated" and out["changed"] is True
    put = t.mutations()[0]
    assert put["method"] == "PUT" and put["url"].endswith("/admin/v1/email/smtp/p1")
    assert put["body"]["host"] == f"{_HOST}:{_PORT}" and put["body"]["id"] == "p1"


def test_an_inactive_catcher_provider_is_not_treated_as_matching():
    out, t = _run([_cfg(state="EMAIL_PROVIDER_INACTIVE"), _Resp(200, {}), _cfg()],
                  lambda c: zs.ensure_catcher(c, _HOST, _PORT, _SENDER, _NAME))
    assert out["action"] == "updated"


def test_a_real_smtp_provider_is_refused_and_nothing_is_mutated():
    t = _Transport([_cfg(ours=False)])
    with mock.patch("urllib.request.urlopen", t), pytest.raises(zs.RefusedError, match="refusing"):
        zs.ensure_catcher(_client(), _HOST, _PORT, _SENDER, _NAME)
    assert t.mutations() == []


def test_a_real_http_provider_is_refused_even_with_replace_existing():
    t = _Transport([_cfg(ours=False, smtp=False)])
    with mock.patch("urllib.request.urlopen", t), pytest.raises(zs.ZitadelError, match="non-SMTP"):
        zs.ensure_catcher(_client(), _HOST, _PORT, _SENDER, _NAME, replace_existing=True)
    assert t.mutations() == []


def test_readback_that_does_not_converge_fails_loud():
    with pytest.raises(zs.ZitadelError, match="read-back"):
        _run([_404(), _Resp(200, {"id": "p1"}), _Resp(200, {}), _cfg(host="10.0.20.99:1025")],
             lambda c: zs.ensure_catcher(c, _HOST, _PORT, _SENDER, _NAME))


def test_remove_deletes_only_the_catcher_provider():
    out, t = _run([_cfg(), _Resp(200, {}), _404()], zs.remove_catcher)  # GET, DELETE, GET(404)
    assert out["action"] == "removed" and t.mutations()[0]["method"] == "DELETE"
    t2 = _Transport([_cfg(ours=False)])
    with mock.patch("urllib.request.urlopen", t2), pytest.raises(zs.RefusedError):
        zs.remove_catcher(_client())
    assert t2.mutations() == []
    out3, _ = _run([_404()], zs.remove_catcher)
    assert out3["action"] == "absent" and out3["changed"] is False


# --------------------------------------------------------------------------- #
# Secret hygiene + CLI contract
# --------------------------------------------------------------------------- #
def test_token_goes_only_in_the_authorization_header():
    _, t = _run([_cfg(), _cfg()], lambda c: zs.ensure_catcher(c, _HOST, _PORT, _SENDER, _NAME))
    for c in t.calls:
        assert c["headers"]["Authorization"] == "Bearer tok-secret-value"
        assert "tok-secret-value" not in c["url"] and "tok-secret-value" not in json.dumps(c["body"])


def test_cli_requires_the_token_in_the_environment(monkeypatch, capsys):
    monkeypatch.delenv(zs.TOKEN_ENV_VAR, raising=False)
    rc = zs.main(["--base-url", "https://auth.test", "--smtp-host", _HOST, "--sender-address", _SENDER])
    assert rc == 2 and zs.TOKEN_ENV_VAR in capsys.readouterr().err


def test_cli_exit_codes_and_the_token_never_appears_in_output(monkeypatch, capsys):
    monkeypatch.setenv(zs.TOKEN_ENV_VAR, "tok-secret-value")
    args = ["--base-url", "https://auth.test", "--smtp-host", _HOST, "--sender-address", _SENDER]
    with mock.patch("urllib.request.urlopen", _Transport([_cfg(ours=False)])):
        assert zs.main(args) == 4
    with mock.patch("urllib.request.urlopen", _Transport([_cfg(), _cfg()])):
        assert zs.main(args) == 0
    err = urllib.error.HTTPError("http://x", 500, "boom", None, io.BytesIO(b'{"message":"server error"}'))
    with mock.patch("urllib.request.urlopen", _Transport([err])):
        assert zs.main(args) == 3
    out = capsys.readouterr()
    assert "tok-secret-value" not in out.out + out.err
    assert json.loads(out.out.strip().splitlines()[0])["changed"] is False


def test_cli_rejects_a_bad_port(monkeypatch):
    monkeypatch.setenv(zs.TOKEN_ENV_VAR, "t")
    assert zs.main(["--base-url", "https://a", "--smtp-host", "h", "--sender-address", "a@b",
                    "--smtp-port", "70000"]) == 2


# --------------------------------------------------------------------------- #
# Role: swap-gate, fail-closed, never enabled by merge
# --------------------------------------------------------------------------- #
def _defaults():
    return yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))


def _tasks():
    return yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))


def test_flags_default_off():
    d = _defaults()
    assert d["zitadel_smtp_dev_catcher"] is False
    assert d["zitadel_smtp_remove"] is False
    assert d["zitadel_smtp_replace_existing"] is False


def test_swap_gate_everything_but_the_info_line_is_inside_the_opt_in_block():
    tasks = _tasks()
    assert "INFO" in tasks[0]["name"] and "ansible.builtin.debug" in tasks[0]
    gated = [t for t in tasks[1:]]
    assert len(gated) == 1 and "block" in gated[0], "exactly one gated block follows the INFO decision"
    when = gated[0]["when"]
    assert "zitadel_smtp_dev_catcher" in when and "zitadel_smtp_remove" in when


def test_fail_closed_when_the_catcher_is_absent_names_the_remedy():
    block = _tasks()[1]["block"]
    a = next(t for t in block if "Fail closed unless the catcher is provisioned" in t["name"])
    assert "mailcatcher-bootstrap.yml" in a["ansible.builtin.assert"]["fail_msg"]
    assert any("wait_for" in "".join(t.keys()) and "SMTP banner" in t["name"] for t in block), \
        "P3: gate on a polled SMTP banner, not a sleep"


def test_helper_run_is_no_log_and_the_token_is_only_in_the_environment():
    block = _tasks()[1]["block"]
    run = next(t for t in block if "Run the helper" in t["name"])
    assert run["no_log"] is True
    assert "ZITADEL_PROVISION_TOKEN" in run["environment"]
    assert "token" not in str(run["ansible.builtin.command"]["argv"]).lower().replace("--", "")
    for frag in ("Bind the token env override", "Read provision_token", "Resolve the access token", "Scrub the token"):
        assert next(t for t in block if frag in t["name"]).get("no_log") is True, frag


def test_playbook_is_a_localhost_orchestrator_with_only_the_role():
    play = yaml.safe_load((_ROOT / "ansible" / "playbooks" / "zitadel-smtp.yml").read_text(encoding="utf-8"))[0]
    assert play["hosts"] == "localhost" and play["roles"] == [{"role": "zitadel_smtp_catcher"}]


def test_no_playbook_role_or_default_ever_turns_the_flag_on():
    """R8: not by merge, not by any default, not by another service's bootstrap."""
    needles = ("zitadel_smtp_dev_catcher: true", "zitadel_smtp_dev_catcher=true", "zitadel_smtp_dev_catcher: True")
    allowed = {"ansible/playbooks/zitadel-smtp.yml",            # comment documenting the explicit command
               "ansible/playbooks/mailcatcher-bootstrap.yml",   # header comment only
               "ansible/roles/zitadel_smtp_catcher/defaults/main.yml",  # comment documenting the opt-in
               "ansible/roles/mailcatcher_handoff/tasks/main.yml"}  # banner that only PRINTS the command
    for p in (_ROOT / "ansible").rglob("*.yml"):
        rel = str(p.relative_to(_ROOT))
        text = p.read_text(encoding="utf-8")
        if any(n in text for n in needles):
            assert rel in allowed, f"{rel} enables the dev-catcher mock"
            for line in text.splitlines():
                if any(n in line for n in needles):
                    s = line.lstrip()
                    assert s.startswith("#") or s.startswith("- \"") or s.startswith('- "'), \
                        f"{rel}: flag set in executable position: {line.strip()}"


# --------------------------------------------------------------------------- #
# Live finding (WS-C16, throwaway ZITADEL v4.18.0)
# --------------------------------------------------------------------------- #
def test_replace_existing_retags_an_otherwise_identical_untagged_provider():
    """BUG found live: an untagged provider with the SAME host/sender was judged 'matching', so
    --replace-existing made no PUT, never applied the ownership marker, and the read-back then
    (correctly) refused. It must PUT (retag) and end active AND tagged as ours."""
    out, t = _run([_cfg(ours=False), _Resp(200, {}), _cfg()],
                  lambda c: zs.ensure_catcher(c, _HOST, _PORT, _SENDER, _NAME, replace_existing=True))
    assert out["action"] == "updated" and out["changed"] is True
    put = t.mutations()[0]
    assert put["method"] == "PUT" and put["body"]["description"] == zs.MARKER_DESCRIPTION


def test_real_get_shape_omits_tls_when_false_and_still_matches():
    """Real v4.18.0 GET /admin/v1/email returns smtp keys host/none/senderAddress/senderName (no `tls`)."""
    cfg = json.loads(_cfg().read())["config"]
    cfg["smtp"].pop("tls", None)
    assert zs._matches(cfg, zs.desired_body(_HOST, _PORT, _SENDER, _NAME)) is True


def test_outcome_message_does_not_tell_you_to_read_codes_after_a_removal():
    """Live finding: after action=removed/absent the INFO line still said 'Read verification codes'."""
    block = _tasks()[1]["block"]
    info = next(t for t in block if "INFO — outcome" in t["name"])
    msg = str(info["ansible.builtin.debug"]["msg"])
    assert "Read verification codes" in msg
    # the codes hint must be conditional on a configure outcome, not unconditional
    assert "action in" in msg or "action not in" in msg or "if " in msg.split("Read verification codes")[0][-200:]


# --------------------------------------------------------------------------- #
# WS-C19 live finding (item 4, playbook path): a refusal must be a CLEAR failure
# --------------------------------------------------------------------------- #
def _templar(variables):
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template
    t = Templar(loader=DataLoader(), variables=variables)
    return lambda expr: t.template(trust_as_template(expr))


def _run_task():
    return next(t for t in _tasks()[1]["block"] if "Run the helper" in t["name"])


@pytest.mark.parametrize("rc,stdout", [(4, ""), (3, ""), (2, "")])
def test_changed_when_survives_a_failing_helper_with_empty_stdout(rc, stdout):
    """Bug condition: on a refusal (exit 4) the helper prints nothing to stdout, and the unguarded
    ``from_json`` in changed_when raised, replacing the real refusal with a censored templating error."""
    render = _templar({"zitadel_smtp_run": {"rc": rc, "stdout": stdout, "stderr": "ERROR: x"}})
    got = render("{{ " + str(_run_task()["changed_when"]).strip() + " }}")
    assert bool(got) is False


def test_changed_when_still_reports_a_real_change():
    expr = "{{ " + str(_run_task()["changed_when"]).strip() + " }}"
    on = _templar({"zitadel_smtp_run": {"rc": 0, "stdout": '{"changed": true, "action": "added"}'}})(expr)
    off = _templar({"zitadel_smtp_run": {"rc": 0, "stdout": '{"changed": false, "action": "unchanged"}'}})(expr)
    assert bool(on) is True and bool(off) is False


def test_a_nonzero_helper_exit_is_surfaced_with_its_own_message_and_never_the_token():
    block = _tasks()[1]["block"]
    run = _run_task()
    assert run["no_log"] is True, "the run task stays censored (it carries the token in its environment)"
    assert run.get("failed_when") is False, "the failure is raised by a dedicated, readable task instead"
    names = [t["name"] for t in block]
    fail = next(t for t in block if "Surface the helper" in t["name"])
    assert names.index(fail["name"]) == names.index(run["name"]) + 1
    assert not fail.get("no_log"), "the refusal message must be visible to the operator"
    assert "rc" in str(fail["when"]) and "!= 0" in str(fail["when"])
    msg = str(fail["ansible.builtin.fail"]["msg"])
    assert "zitadel_smtp_run.stderr" in msg
    for secret_ref in ("zitadel_smtp_token", "zitadel_smtp_env_token", "zitadel_smtp_bao_token", "ZITADEL_PROVISION_TOKEN"):
        assert secret_ref not in msg
    refused = "ERROR: a non-catcher email provider is active (type SMTP); refusing to redirect real mail"
    shown = _templar({"zitadel_smtp_run": {"rc": 4, "stderr": refused}})(msg)
    assert refused in shown


def test_outcome_info_only_runs_after_a_successful_helper_run():
    block = _tasks()[1]["block"]
    info = next(t for t in block if "INFO — outcome" in t["name"])
    names = [t["name"] for t in block]
    assert names.index(info["name"]) > names.index(next(t["name"] for t in block if "Surface the helper" in t["name"]))
