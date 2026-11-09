"""Bug-condition + preservation tests: consumer_preflight ZITADEL probe is bounded-polled (WS-C8).

Feature: fix-consumer-preflight-zitadel-probe-retry.
Spec: .kiro/specs/fix-consumer-preflight-zitadel-probe-retry/.

THE BUG: the probe was a single-shot GET (status_code [200], failed_when false) followed by a
fail-closed assert. One transient connect/TLS reset through Traefik failed the entire
onboarding while ZITADEL was healthy (standalone probe 20/20 x 200).

THE FIX: poll with a BOUNDED retry/until (platform-runtime-patterns P3), keeping the probe
read-only and the fail-closed halt once the attempts are exhausted. Pure offline.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_TASKS = _REPO / "ansible/roles/consumer_preflight/tasks/main.yml"
_DEFAULTS = _REPO / "ansible/roles/consumer_preflight/defaults/main.yml"
_PROBE = "Preflight | Probe ZITADEL (bounded, read-only health GET) — when sso declared"
_HALT = "Preflight | Halt fail-closed if ZITADEL is unreachable (Req 9.3)"


def _task(name: str) -> dict:
    for t in yaml.safe_load(_TASKS.read_text(encoding="utf-8")):
        if isinstance(t, dict) and t.get("name") == name:
            return t
    raise AssertionError(name)


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def test_probe_polls_with_until_retries_delay():
    t = _task(_PROBE)
    assert "until" in t, "single-shot probe: one transient reset fails the whole onboarding (WS-C8)"
    assert "retries" in t and "delay" in t


def test_until_is_true_only_on_200():
    until = _task(_PROBE)["until"]
    ok = _tpl("{{ " + until + " }}", {"consumer_zitadel_probe": {"status": 200}})
    assert ok is True
    for bad in ({"status": -1}, {"status": 503}, {"status": 502}, {}):
        assert _tpl("{{ " + until + " }}", {"consumer_zitadel_probe": bad}) is False


def test_retry_is_bounded_and_defaults_are_sane():
    d = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert 2 <= d["consumer_probe_retries"] <= 10, "bounded: never an unbounded wait"
    assert 1 <= d["consumer_probe_retry_delay"] <= 15
    t = _task(_PROBE)
    assert int(_tpl(t["retries"], {"consumer_probe_retries": d["consumer_probe_retries"]})) == d["consumer_probe_retries"]
    assert int(_tpl(t["delay"], {"consumer_probe_retry_delay": d["consumer_probe_retry_delay"]})) == d["consumer_probe_retry_delay"]


def test_probe_stays_read_only_and_never_fails_the_play_itself():
    t = _task(_PROBE)
    assert "ansible.builtin.uri" in t and t["ansible.builtin.uri"]["method"] == "GET"
    assert t["changed_when"] is False and t["failed_when"] is False
    assert t["ansible.builtin.uri"]["status_code"] == [200]


def test_fail_closed_halt_preserved_and_reports_the_last_result():
    halt = _task(_HALT)
    assert halt["ansible.builtin.assert"]["that"] == ["(consumer_zitadel_probe.status | default(0)) == 200"]
    msg = halt["ansible.builtin.assert"]["fail_msg"]
    assert "svc-06-bootstrap.yml" in msg and "never raw" in msg
    assert "consumer_zitadel_probe.status" in msg, "operator must see WHY the last attempt failed"
    assert halt["when"] == "consumer_manifest.sso is defined"


def test_no_fixed_sleep_added():
    text = _TASKS.read_text(encoding="utf-8")
    assert "ansible.builtin.pause" not in text and "sleep " not in text
