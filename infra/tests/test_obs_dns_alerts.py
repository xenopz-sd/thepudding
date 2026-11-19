"""SVC-10 internal DNS alert rules + scrape rows (spec svc-10-internal-dns, §13).

Renders the SVC-17/18 baseline with the role defaults and:
* asserts the ``platform-dns`` group holds DnsRecursorDown (critical) and
  DnsRecursorServfailRate (warning), both on ``service="pdns-recursor"`` series;
* asserts the scrape table carries the two PowerDNS webserver rows (``dns`` role,
  ports 8081/8082), so the DOWN alert has an ``up`` series to watch;
* runs ``promtool test rules`` on synthetic series (healthy, recursor down, 20 %
  SERVFAIL) so each alert fires when it should and stays quiet otherwise. Offline;
  SKIPPED with a reason when neither a local promtool nor the pinned image is usable.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from test_obs_garage_alerts import _promtool_test
from test_obs_prometheus_render import _RULES_TMPL, _render, role_vars  # noqa: F401 (fixture re-export)

_DEFAULTS = Path(__file__).resolve().parents[2] / "ansible" / "roles" / "observability_stack" / "defaults" / "main.yml"
_DNS_ALERTS = {"DnsRecursorDown": "critical", "DnsRecursorServfailRate": "warning"}


def _dns_group(role_vars: dict) -> dict:
    doc = yaml.safe_load(_render(_RULES_TMPL, role_vars))
    groups = [g for g in doc["groups"] if g["name"] == "platform-dns"]
    assert len(groups) == 1, "exactly one platform-dns group"
    return groups[0]


def test_dns_alerts_exist_with_severities(role_vars):
    rules = {r["alert"]: r for r in _dns_group(role_vars)["rules"]}
    assert {a: rules[a]["labels"]["severity"] for a in _DNS_ALERTS} == _DNS_ALERTS
    for r in rules.values():
        assert r["labels"]["service"] == "dns"
        assert 'service="pdns-recursor"' in r["expr"]


def test_scrape_rows_for_both_powerdns_webservers():
    rows = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))["observability_builtin_scrape_targets"]
    dns = {r["service"]: r for r in rows if r["service_role"] == "dns"}
    assert set(dns) == {"pdns-auth", "pdns-recursor"}
    assert dns["pdns-auth"]["port"] == 8081 and dns["pdns-recursor"]["port"] == 8082
    for r in dns.values():
        assert r["project"] == "shared" and not r.get("metrics_path") and not r.get("scheme")


_UNIT_TESTS = textwrap.dedent("""
    rule_files:
      - baseline.rules.yml
    evaluation_interval: 1m
    tests:
      # Healthy, 1 % SERVFAIL: no DNS alert.
      - interval: 1m
        input_series:
          - series: 'up{service="pdns-recursor",instance="10.0.20.25:8082",job="platform-services"}'
            values: '1x30'
          - series: 'pdns_recursor_questions{service="pdns-recursor",instance="10.0.20.25:8082"}'
            values: '0+100x30'
          - series: 'pdns_recursor_servfail_answers{service="pdns-recursor",instance="10.0.20.25:8082"}'
            values: '0+1x30'
        alert_rule_test:
          - {eval_time: 29m, alertname: DnsRecursorDown, exp_alerts: []}
          - {eval_time: 29m, alertname: DnsRecursorServfailRate, exp_alerts: []}
      # Recursor down: alert after 2m.
      - interval: 1m
        input_series:
          - series: 'up{service="pdns-recursor",instance="10.0.20.25:8082",job="platform-services"}'
            values: '0x10'
        alert_rule_test:
          - {eval_time: 1m, alertname: DnsRecursorDown, exp_alerts: []}
          - eval_time: 5m
            alertname: DnsRecursorDown
            exp_alerts:
              - exp_labels:
                  severity: critical
                  service: dns
                  instance: "10.0.20.25:8082"
                  job: platform-services
                exp_annotations:
                  summary: "SVC-10 DNS recursor is down"
                  description: "The PowerDNS recursor metrics target 10.0.20.25:8082 has been down for 2 minutes: platform names do not resolve for clients that use the platform resolver. Re-run svc-10-bootstrap.yml."
      # 20 % SERVFAIL: warning after 10m.
      - interval: 1m
        input_series:
          - series: 'pdns_recursor_questions{service="pdns-recursor",instance="10.0.20.25:8082"}'
            values: '0+100x40'
          - series: 'pdns_recursor_servfail_answers{service="pdns-recursor",instance="10.0.20.25:8082"}'
            values: '0+20x40'
        alert_rule_test:
          - {eval_time: 8m, alertname: DnsRecursorServfailRate, exp_alerts: []}
          - eval_time: 25m
            alertname: DnsRecursorServfailRate
            exp_alerts:
              - exp_labels: {severity: warning, service: dns}
                exp_annotations:
                  summary: "SVC-10 DNS recursor SERVFAIL rate above 10 %"
                  description: "More than 10 % of the recursor's answers were SERVFAIL over 10 minutes. Check the authoritative container (pdns-auth-1) and the guest's upstream connectivity."
""")


def test_promtool_unit_tests_for_the_dns_alerts(role_vars):
    proc = _promtool_test(_render(_RULES_TMPL, role_vars), _UNIT_TESTS, role_vars["observability_prometheus_image"])
    if proc is None:
        pytest.skip("promtool unavailable (no local binary and no usable Docker image offline)")
    assert proc.returncode == 0, f"promtool test rules failed:\n{proc.stdout}\n{proc.stderr}"
