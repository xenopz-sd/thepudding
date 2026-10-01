"""Offline structural-contract tests for the task-10.2 alerting wiring.

Task 10.2 (spec: svc-07-secrets-manager) — requirements.md Requirements 10.4,
10.5, 10.6, 10.7, 10.9; design.md "Error Handling (audit fail-closed → alert)".

These are OFFLINE, dependency-light assertions over the ``openbao_init_unseal``
role's task-10.2 additions — the new ``tasks/alerts.yml`` (the §10.2 render
tasks), the §10.2 vars added to ``defaults/main.yml``, the three new templates
(``templates/prometheus-openbao-alerts.yml.j2``, ``templates/loki-openbao-audit
-rule.yml.j2``, ``templates/alertmanager-openbao-route.yml.j2``), and the
``tasks/main.yml`` include that wires them in.

Like the task-10.1 scrape test, SVC-07 CONTRIBUTES these rule/route files to the
separate SVC-17/18 (Prometheus/Alertmanager/Loki) services — it does not own the
collectors. So these tests RENDER the templates with the role defaults (Jinja2,
offline), parse the result as YAML, and pin the alerting contract the task's
brief + the observability requirements demand:

  * ``OpenBaoSealed``            — critical; ``openbao_core_unsealed == 0`` for
    > 2m; auto-resolve inherent to Prometheus alerting (Req 10.4);
  * ``OpenBaoUnsealerUnreachable`` — critical; ``up{job=openbao_unsealer} == 0``
    for > 2m; auto-resolve inherent (Req 10.5);
  * ``OpenBaoLeaseCountHigh``    — warning; ``openbao_expire_num_leases`` >
    threshold (default 1000, OVERRIDABLE via a var) for > 5m; auto-resolve
    inherent (Req 10.6);
  * ``OpenBaoAuditFailure``      — critical; a Loki ruler LogQL rule firing
    within 1 min of an audit-write-failure log line, DISTINCT from the
    task-11.1 snapshot-failure marker (Req 10.7);
  * a 30-min unacknowledged escalation route to a SECONDARY on-call receiver
    (Req 10.9).

They are NOT property-based tests and NOT ``requires_infra``: there is no live
OpenBao / Prometheus / Alertmanager / Loki here. They add only OFFLINE tests, so
they do not touch the offline-plan-preservation skip baseline.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_alerting.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_INIT_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_INIT_DEFAULTS = _INIT_DIR / "defaults" / "main.yml"
_ALERTS_TASKS = _INIT_DIR / "tasks" / "alerts.yml"
_MAIN_TASKS = _INIT_DIR / "tasks" / "main.yml"
_PROM_ALERTS_TMPL = _INIT_DIR / "templates" / "prometheus-openbao-alerts.yml.j2"
_LOKI_RULE_TMPL = _INIT_DIR / "templates" / "loki-openbao-audit-rule.yml.j2"
_AM_ROUTE_TMPL = _INIT_DIR / "templates" / "alertmanager-openbao-route.yml.j2"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _duration_to_seconds(dur: str) -> float:
    """Parse a Prometheus/Alertmanager-style duration (e.g. "2m", "30s", "0s")."""
    m = re.fullmatch(r"(\d+)(ms|s|m|h|d)", str(dur).strip())
    assert m, f"unrecognised duration {dur!r}"
    value, unit = int(m.group(1)), m.group(2)
    factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return value * factor


@pytest.fixture(scope="module")
def init_defaults() -> dict:
    raw = yaml.safe_load(_INIT_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw
    return raw


def _render(tmpl_path: Path, raw: dict) -> str:
    """Render a template resolving the (few) chained default refs it needs.

    Mirrors the task-10.1 test's offline-render approach: resolve the nested
    ``{{ }}`` references embedded in the default *values* so the render is
    self-contained without a full Ansible var pass.
    """
    jinja2 = pytest.importorskip("jinja2")
    thr = raw["openbao_alert_lease_count_threshold"]
    backup_marker = "OPENBAO_SNAPSHOT_FAILURE"

    ctx = dict(raw)
    # lease-count expr embeds the threshold var
    ctx["openbao_alert_lease_count_expr"] = raw[
        "openbao_alert_lease_count_expr"
    ].replace("{{ openbao_alert_lease_count_threshold }}", str(thr))
    ctx["openbao_backup_failure_marker"] = backup_marker
    # the loki expr embeds the audit matchers + the snapshot marker exclusion
    ctx["openbao_loki_audit_failure_expr"] = jinja2.Template(
        raw["openbao_loki_audit_failure_expr"]
    ).render(
        openbao_audit_failure_log_matcher=raw["openbao_audit_failure_log_matcher"],
        openbao_audit_failure_log_error_matcher=raw[
            "openbao_audit_failure_log_error_matcher"
        ],
        openbao_backup_failure_marker=backup_marker,
    )
    return jinja2.Template(tmpl_path.read_text(encoding="utf-8")).render(**ctx)


@pytest.fixture(scope="module")
def prom_alerts(init_defaults) -> dict:
    doc = yaml.safe_load(_render(_PROM_ALERTS_TMPL, init_defaults))
    assert isinstance(doc, dict) and "groups" in doc
    return doc


@pytest.fixture(scope="module")
def loki_rule(init_defaults) -> dict:
    doc = yaml.safe_load(_render(_LOKI_RULE_TMPL, init_defaults))
    assert isinstance(doc, dict) and "groups" in doc
    return doc


@pytest.fixture(scope="module")
def am_route(init_defaults) -> dict:
    doc = yaml.safe_load(_render(_AM_ROUTE_TMPL, init_defaults))
    assert isinstance(doc, dict)
    return doc


@pytest.fixture(scope="module")
def alerts_tasks() -> list[dict]:
    tasks = yaml.safe_load(_ALERTS_TASKS.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and tasks
    return tasks


def _prom_rules(doc: dict) -> list[dict]:
    rules: list[dict] = []
    for group in doc["groups"]:
        rules.extend(group.get("rules", []))
    return rules


def _alert(doc: dict, name: str) -> dict:
    for rule in _prom_rules(doc):
        if rule.get("alert") == name:
            return rule
    raise AssertionError(
        f"no alert named {name!r}; alerts={[r.get('alert') for r in _prom_rules(doc)]}"
    )


# --------------------------------------------------------------------------- #
# Req 10.4 — OpenBaoSealed (critical; openbao_core_unsealed == 0 > 2m)
# --------------------------------------------------------------------------- #
class TestOpenBaoSealed:
    def test_present_and_critical(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoSealed")
        assert rule["labels"]["severity"] == "critical"

    def test_expr_on_core_unsealed_zero(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoSealed")
        assert "openbao_core_unsealed" in rule["expr"]
        assert "== 0" in rule["expr"].replace(" ", " ")

    def test_for_is_two_minutes(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoSealed")
        assert _duration_to_seconds(rule["for"]) == 120  # > 2 min window


# --------------------------------------------------------------------------- #
# Req 10.5 — OpenBaoUnsealerUnreachable (critical; up{job=openbao_unsealer}==0 >2m)
# --------------------------------------------------------------------------- #
class TestOpenBaoUnsealerUnreachable:
    def test_present_and_critical(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoUnsealerUnreachable")
        assert rule["labels"]["severity"] == "critical"

    def test_expr_keys_off_unsealer_up_series(self, prom_alerts, init_defaults):
        rule = _alert(prom_alerts, "OpenBaoUnsealerUnreachable")
        # keys off up{job="openbao_unsealer"} — the job task 10.1 created.
        assert 'up{job="openbao_unsealer"}' in rule["expr"]
        assert init_defaults["openbao_prometheus_unsealer_job_name"] == "openbao_unsealer"
        assert "== 0" in rule["expr"]

    def test_for_is_two_minutes(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoUnsealerUnreachable")
        assert _duration_to_seconds(rule["for"]) == 120


# --------------------------------------------------------------------------- #
# Req 10.6 — OpenBaoLeaseCountHigh (warning; > threshold default 1000 > 5m)
# --------------------------------------------------------------------------- #
class TestOpenBaoLeaseCountHigh:
    def test_present_and_warning(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoLeaseCountHigh")
        assert rule["labels"]["severity"] == "warning"

    def test_expr_on_lease_metric_over_threshold(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoLeaseCountHigh")
        assert "openbao_expire_num_leases" in rule["expr"]
        assert "> 1000" in rule["expr"]  # default threshold rendered

    def test_for_is_five_minutes(self, prom_alerts):
        rule = _alert(prom_alerts, "OpenBaoLeaseCountHigh")
        assert _duration_to_seconds(rule["for"]) == 300

    def test_threshold_default_1000(self, init_defaults):
        assert init_defaults["openbao_alert_lease_count_threshold"] == 1000

    def test_threshold_is_overridable_via_var(self, init_defaults):
        # Req 10.6 — overridable via alert rule config without a code change:
        # the expr references the threshold VAR (not a hardcoded literal), so
        # overriding the var re-renders the rule with a new threshold.
        assert (
            "{{ openbao_alert_lease_count_threshold }}"
            in init_defaults["openbao_alert_lease_count_expr"]
        )

    def test_override_reflows_into_the_rule(self, init_defaults):
        # Prove overriding the threshold var changes the rendered expr.
        raw = dict(init_defaults)
        raw["openbao_alert_lease_count_threshold"] = 5000
        doc = yaml.safe_load(_render(_PROM_ALERTS_TMPL, raw))
        rule = _alert(doc, "OpenBaoLeaseCountHigh")
        assert "> 5000" in rule["expr"]
        assert "> 1000" not in rule["expr"]


# --------------------------------------------------------------------------- #
# Req 10.7 — OpenBaoAuditFailure (critical; Loki rule within 1 min)
# --------------------------------------------------------------------------- #
class TestOpenBaoAuditFailure:
    def test_present_and_critical(self, loki_rule):
        rule = _alert(loki_rule, "OpenBaoAuditFailure")
        assert rule["labels"]["severity"] == "critical"

    def test_is_a_logql_rule(self, loki_rule):
        # LogQL, not PromQL: a log-stream selector + a range window.
        rule = _alert(loki_rule, "OpenBaoAuditFailure")
        assert '{service="openbao"}' in rule["expr"]
        assert "count_over_time" in rule["expr"]

    def test_fires_within_one_minute(self, loki_rule):
        rule = _alert(loki_rule, "OpenBaoAuditFailure")
        # for: 0s so the first ingested line fires at the next eval; the eval
        # interval and the [range] are both <= 1 min, keeping the fire within
        # the 1-minute SLA (Req 10.7).
        assert _duration_to_seconds(rule["for"]) == 0
        # [1m] window inside the LogQL expr
        assert "[1m]" in rule["expr"]

    def test_eval_interval_within_one_minute(self, init_defaults):
        assert _duration_to_seconds(init_defaults["openbao_loki_audit_rule_interval"]) <= 60

    def test_matches_audit_signal_not_snapshot_marker(self, loki_rule):
        # Req 10.7 — the audit-write-failure signal is its OWN alert, DISTINCT
        # from the task-11.1 snapshot-failure marker (OPENBAO_SNAPSHOT_FAILURE).
        rule = _alert(loki_rule, "OpenBaoAuditFailure")
        expr = rule["expr"]
        assert "audit" in expr.lower()
        # explicitly excludes the snapshot marker so the two never conflate.
        assert "OPENBAO_SNAPSHOT_FAILURE" in expr
        assert "!=" in expr  # the marker appears as a line-exclusion filter

    def test_audit_alert_not_in_prometheus_rules(self, prom_alerts):
        # the audit alert is Loki-derived and must NOT be a Prometheus rule.
        names = [r.get("alert") for r in _prom_rules(prom_alerts)]
        assert "OpenBaoAuditFailure" not in names


# --------------------------------------------------------------------------- #
# All four alerts present across the two rule fragments
# --------------------------------------------------------------------------- #
class TestAllFourAlertsPresent:
    def test_four_named_alerts_exist(self, prom_alerts, loki_rule):
        prom = {r.get("alert") for r in _prom_rules(prom_alerts)}
        loki = {r.get("alert") for r in _prom_rules(loki_rule)}
        all_alerts = prom | loki
        for name in (
            "OpenBaoSealed",
            "OpenBaoUnsealerUnreachable",
            "OpenBaoLeaseCountHigh",
            "OpenBaoAuditFailure",
        ):
            assert name in all_alerts, f"{name} missing; found {sorted(all_alerts)}"


# --------------------------------------------------------------------------- #
# Req 10.9 — 30-min unacknowledged escalation to the secondary on-call receiver
# --------------------------------------------------------------------------- #
class TestEscalationRoute:
    def test_primary_route_to_ops_receiver(self, am_route, init_defaults):
        # Req 10.4 — primary route to the platform operations receiver.
        assert am_route["route"]["receiver"] == init_defaults[
            "openbao_alertmanager_primary_receiver"
        ]
        # matches SVC-07's alerts.
        assert any(
            'service="openbao"' in m for m in am_route["route"]["matchers"]
        )

    def test_escalation_child_route_present(self, am_route, init_defaults):
        child_routes = am_route["route"].get("routes", [])
        assert child_routes, "escalation child route missing"
        secondary = init_defaults["openbao_alertmanager_secondary_receiver"]
        esc = next(
            (r for r in child_routes if r.get("receiver") == secondary), None
        )
        assert esc is not None, f"no route to secondary receiver {secondary!r}"

    def test_escalation_delayed_by_30_minutes(self, am_route, init_defaults):
        secondary = init_defaults["openbao_alertmanager_secondary_receiver"]
        esc = next(
            r for r in am_route["route"]["routes"] if r.get("receiver") == secondary
        )
        # ~30 min hold before the secondary is first notified (Req 10.9).
        assert _duration_to_seconds(esc["group_wait"]) == 30 * 60

    def test_escalation_targets_a_distinct_secondary_receiver(self, init_defaults):
        assert (
            init_defaults["openbao_alertmanager_secondary_receiver"]
            != init_defaults["openbao_alertmanager_primary_receiver"]
        )

    def test_both_receivers_defined(self, am_route, init_defaults):
        names = {r["name"] for r in am_route["receivers"]}
        assert init_defaults["openbao_alertmanager_primary_receiver"] in names
        assert init_defaults["openbao_alertmanager_secondary_receiver"] in names


# --------------------------------------------------------------------------- #
# Task wiring + idempotency + secrecy
# --------------------------------------------------------------------------- #
class TestTaskWiring:
    def test_renders_all_three_fragments(self, alerts_tasks):
        srcs = {
            t["ansible.builtin.template"]["src"]
            for t in alerts_tasks
            if "ansible.builtin.template" in t
        }
        assert "prometheus-openbao-alerts.yml.j2" in srcs
        assert "loki-openbao-audit-rule.yml.j2" in srcs
        assert "alertmanager-openbao-route.yml.j2" in srcs

    def test_render_tasks_gated_on_enabled_flag(self, alerts_tasks):
        for t in alerts_tasks:
            if "ansible.builtin.template" in t:
                assert "openbao_alerts_enabled" in yaml.safe_dump(t["when"])

    def test_main_includes_alerts_primary_only(self):
        tasks = yaml.safe_load(_MAIN_TASKS.read_text(encoding="utf-8"))
        include = next(
            (
                t
                for t in tasks
                if t.get("ansible.builtin.include_tasks") == "alerts.yml"
            ),
            None,
        )
        assert include is not None, "tasks/main.yml must include alerts.yml"
        assert 'openbao_role == "primary"' in yaml.safe_dump(include["when"])

    def test_alerts_enabled_default_true(self, init_defaults):
        assert init_defaults["openbao_alerts_enabled"] is True


class TestSecrecy:
    """No secret defaulted; receiver credentials are never inlined."""

    def test_no_secret_literal_in_templates(self):
        pat = re.compile(
            r"(hvs\.[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9]{15,}|s\.[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
        )
        for tmpl in (_PROM_ALERTS_TMPL, _LOKI_RULE_TMPL, _AM_ROUTE_TMPL):
            assert not pat.search(tmpl.read_text(encoding="utf-8")), tmpl

    def test_receiver_urls_are_placeholders(self, init_defaults):
        for key in (
            "openbao_alertmanager_primary_receiver_url",
            "openbao_alertmanager_secondary_receiver_url",
        ):
            url = init_defaults[key]
            assert url.startswith("http")
            # no embedded credential in the placeholder URL.
            assert "@" not in url.split("//", 1)[1].split("/", 1)[0]
