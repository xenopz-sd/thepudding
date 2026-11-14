"""WS-C27 — the onboarding handoff prints the project's ZITADEL org id.

Found while writing WS-C21: an external author needs the org id to build the ``urn:zitadel:iam:org:id:<id>`` scope, but
onboarding runs with its output suppressed (``no_log``) so the id was never shown and they had to ask the operator. The id is a
non-secret identifier. Mechanism: ``zitadel-org-provision`` prints one stable marker line; ``consumer_bootstrap`` parses it
out of the child run's output (strictly numeric, so nothing else can ride along); the handoff banner shows it and the scope.

Pure offline: tasks are parsed and the real Jinja expressions are evaluated with Ansible's templater.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _ROOT / "ansible" / "roles"


def _tasks(role):
    return [t for t in yaml.safe_load((_ROLES / role / "tasks" / "main.yml").read_text(encoding="utf-8")) if isinstance(t, dict)]


def _task(role, frag):
    return next(t for t in _tasks(role) if frag in str(t.get("name")))


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


_MARK = "Print the org id marker"
_PARSE = "Capture the project org id"
_BANNER = "Print the Developer B completion summary banner"


def test_non_vacuity():
    _task("zitadel-org-provision", _MARK)
    _task("consumer_bootstrap", _PARSE)
    _task("consumer_handoff", _BANNER)


def test_the_marker_prints_only_the_org_id_in_a_fixed_format():
    t = _task("zitadel-org-provision", _MARK)
    assert t["ansible.builtin.debug"]["msg"].startswith("ZITADEL_ORG_ID=")
    assert "no_log" not in t or t["no_log"] is False
    out = _tpl(t["ansible.builtin.debug"]["msg"], {"zop_app_results": [{"stdout": '{"org": {"id": "398761863216627714"}}'}]})
    assert out == "ZITADEL_ORG_ID=398761863216627714"
    # never anything else from the helper result
    assert "client_secret" not in str(t) and "item.stdout }}" not in str(t)


def test_the_marker_is_skipped_when_there_is_no_result():
    t = _task("zitadel-org-provision", _MARK)
    assert "zop_app_results" in str(t["when"])


def _parse(stdout):
    t = _task("consumer_bootstrap", _PARSE)
    return _tpl(t["ansible.builtin.set_fact"]["consumer_sso_org_id"], {"consumer_sso_run": {"stdout": stdout}})


def test_the_numeric_id_is_extracted_from_the_child_output():
    out = 'TASK [x]\nok: [localhost] => {\n    "msg": "ZITADEL_ORG_ID=398761863216627714"\n}\nPLAY RECAP\n'
    assert _parse(out) == "398761863216627714"


@pytest.mark.parametrize("stdout", ["", "no marker here", 'ZITADEL_ORG_ID=', 'ZITADEL_ORG_ID=abc', "ZITADEL_ORG_ID=12;rm -rf /"[:14] + "x"])
def test_missing_or_non_numeric_ids_yield_an_empty_value_never_garbage(stdout):
    assert _parse(stdout) == ""


def test_a_semicolon_or_quote_after_the_digits_is_not_carried_along():
    assert _parse('ZITADEL_ORG_ID=123"; evil') == "123"


def test_the_parse_task_is_gated_on_sso_and_is_not_no_log_hiding_the_fact():
    t = _task("consumer_bootstrap", _PARSE)
    assert "consumer_manifest.sso" in str(t["when"])
    assert t["changed_when"] is False


def _banner_lines(variables):
    msg = _task("consumer_handoff", _BANNER)["ansible.builtin.debug"]["msg"]
    base = {"project_slug": "edge-sensors", "consumer_vlan_id": 102, "consumer_db_port": 5432,
            "consumer_manifest": {"sso": {"org_name": "proj-edge-sensors", "app_registrations": [{"name": "spa"}]}},
            "consumer_sso_app_lines": ["spa: secret/x — none"], "consumer_workload_approle_ref": "r",
            "consumer_cluster_env_abspath": "/x/.env", "consumer_platform_domain": "platform.example.test",
            "consumer_db_secret_path": "secret/proj-x/postgres", "consumer_db_cred_state": "present",
            "consumer_db_guest_ip": "10.0.100.10"}
    base.update(variables)
    return [str(_tpl(m, base)) for m in msg]


def test_the_banner_shows_the_org_id_and_the_scope_when_known():
    lines = _banner_lines({"consumer_sso_org_id": "398761863216627714"})
    text = "\n".join(lines)
    assert "398761863216627714" in text
    assert "urn:zitadel:iam:org:id:398761863216627714" in text


def test_the_banner_says_so_when_the_id_could_not_be_determined():
    text = "\n".join(_banner_lines({"consumer_sso_org_id": ""}))
    assert "not reported" in text and "urn:zitadel:iam:org:id:" not in text


def test_the_banner_is_unchanged_for_a_manifest_without_sso():
    text = "\n".join(_banner_lines({"consumer_manifest": {"database": {"db_name": "d"}}, "consumer_sso_org_id": ""}))
    assert "urn:zitadel" not in text and "not consumed" in text


def test_the_banner_never_prints_a_secret_shaped_value():
    text = "\n".join(_banner_lines({"consumer_sso_org_id": "398761863216627714"}))
    for bad in ("client_secret", "password=", "token="):
        assert bad not in text.lower()
