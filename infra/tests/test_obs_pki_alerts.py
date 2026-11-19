"""SVC-08 certificate alerts (spec svc-08-internal-pki D7 revised, WS-C59).

Renders the SVC-17/18 baseline and checks the ``platform-pki`` group: PlatformCertExpiringSoon (warning, < 14 days)
and PlatformCertExpired (critical), both on Traefik's ``traefik_tls_certs_not_after`` series (live-confirmed on the
throwaway cluster, 2026-11-17). promtool runs synthetic series; SKIPPED when promtool is unavailable offline.
"""
from __future__ import annotations

import textwrap

import pytest
import yaml
from test_obs_garage_alerts import _promtool_test
from test_obs_prometheus_render import _RULES_TMPL, _render, role_vars  # noqa: F401 (fixture re-export)

_ALERTS = {"PlatformCertExpiringSoon": "warning", "PlatformCertExpired": "critical"}


def test_pki_group_is_always_rendered_on_the_traefik_series(role_vars):
    doc = yaml.safe_load(_render(_RULES_TMPL, role_vars))
    groups = [g for g in doc["groups"] if g["name"] == "platform-pki"]
    assert len(groups) == 1
    rules = {r["alert"]: r for r in groups[0]["rules"]}
    assert {a: rules[a]["labels"]["severity"] for a in _ALERTS} == _ALERTS
    for r in rules.values():
        assert 'traefik_tls_certs_not_after{service="traefik"}' in r["expr"]


# promtool starts at t=0: a not_after of 20 days is healthy, 10 days fires the warning, -1 fires critical.
_UNIT_TESTS = textwrap.dedent("""
    rule_files:
      - baseline.rules.yml
    evaluation_interval: 1m
    tests:
      - interval: 1m
        input_series:
          - series: 'traefik_tls_certs_not_after{service="traefik",cn="d.example.test",instance="10.0.20.12:8082"}'
            values: '1728000x120'
        alert_rule_test:
          - {eval_time: 90m, alertname: PlatformCertExpiringSoon, exp_alerts: []}
          - {eval_time: 90m, alertname: PlatformCertExpired, exp_alerts: []}
      - interval: 1m
        input_series:
          - series: 'traefik_tls_certs_not_after{service="traefik",cn="d.example.test",instance="10.0.20.12:8082"}'
            values: '864000x120'
        alert_rule_test:
          - {eval_time: 30m, alertname: PlatformCertExpiringSoon, exp_alerts: []}
          - eval_time: 90m
            alertname: PlatformCertExpiringSoon
            exp_alerts:
              - exp_labels: {severity: warning, service: pki, cn: d.example.test, instance: "10.0.20.12:8082"}
                exp_annotations:
                  summary: "Traefik certificate d.example.test expires in under 14 days"
                  description: "Traefik serves a certificate for d.example.test that expires in 9.938 days. Renewal should have happened at 30 days: check the Traefik logs for ACME errors, OpenBao (SVC-07) and the PowerDNS API (SVC-10)."
      - interval: 1m
        input_series:
          - series: 'traefik_tls_certs_not_after{service="traefik",cn="d.example.test",instance="10.0.20.12:8082"}'
            values: '0x20'
        alert_rule_test:
          - eval_time: 10m
            alertname: PlatformCertExpired
            exp_alerts:
              - exp_labels: {severity: critical, service: pki, cn: d.example.test, instance: "10.0.20.12:8082"}
                exp_annotations:
                  summary: "Traefik serves an expired certificate for d.example.test"
                  description: "Clients that verify TLS now fail for every route covered by d.example.test."
""")


def test_promtool_unit_tests_for_the_pki_alerts(role_vars):
    proc = _promtool_test(_render(_RULES_TMPL, role_vars), _UNIT_TESTS, role_vars["observability_prometheus_image"])
    if proc is None:
        pytest.skip("promtool unavailable (no local binary and no usable Docker image offline)")
    assert proc.returncode == 0, f"promtool test rules failed:\n{proc.stdout}\n{proc.stderr}"
