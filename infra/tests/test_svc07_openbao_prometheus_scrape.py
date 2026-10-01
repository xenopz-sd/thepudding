"""Offline structural-contract tests for the task-10.1 Prometheus scrape wiring.

Task 10.1 (spec: svc-07-secrets-manager) — requirements.md Requirements 10.1,
10.2, 10.3, 10.8, FD.7; design.md "Component topology" + "Testing Strategy §3
(Observability)".

These are OFFLINE, dependency-light assertions over:

  * the ``openbao_install`` role's primary ``config.hcl.j2`` telemetry stanza —
    that it exposes the Prometheus metrics endpoint (``prometheus_retention_time``
    >= 1 min, Req 10.1) and that the metrics endpoint is scrapeable WITHOUT a
    stored bearer token via ``unauthenticated_metrics_access`` (Req 10.2);
  * the ``openbao_init_unseal`` role's task-10.1 additions — the new
    ``tasks/observability.yml`` (the §10.1 render tasks), the §10.1 vars added to
    ``defaults/main.yml``, the new Prometheus scrape-config template
    ``templates/prometheus-openbao.yml.j2``, and the ``tasks/main.yml`` include
    that wires it in.

They are NOT property-based tests and NOT ``requires_infra``: there is no live
OpenBao / Prometheus / Docker here. They RENDER the scrape-config template with
the role defaults (Jinja2, offline) and parse the result as YAML, then pin the
scrape contract the task's brief and the observability requirements demand:

  * a scrape job labelled ``openbao`` (``job_name: openbao``) at a
    ``scrape_interval`` of <= 30 s scraping the PRIMARY's
    ``/v1/sys/metrics`` with ``?format=prometheus`` over TLS (Req 10.2, 10.3);
  * a scrape target/job labelled ``openbao_unsealer`` scraping the UNSEALER so
    its ``up`` series is queryable for the OpenBaoUnsealerUnreachable alert
    (Req 10.2);
  * the primary telemetry stanza exposes metrics (retention >= 1 min, Req 10.1)
    and is unauthenticated-scrapeable (Req 10.2), with the sealed-metric
    guarantee (``openbao_core_unsealed = 0`` while sealed, Req 10.8 / FD.7)
    documented as OpenBao-native and verified by the degraded-mode tests
    (12.4 / 12.5), not implemented here;
  * NO secret value is defaulted or inlined — any authenticated-scrape token is
    referenced by FILE PATH only.

The whole suite is offline and always runs (no gating). It adds only OFFLINE
tests (no ``requires_infra``), so it does not touch the offline-plan-preservation
skip baseline.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_prometheus_scrape.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_INSTALL_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_install"
_INSTALL_DEFAULTS = _INSTALL_DIR / "defaults" / "main.yml"
_PRIMARY_CONFIG_TMPL = _INSTALL_DIR / "templates" / "config.hcl.j2"

_INIT_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_INIT_DEFAULTS = _INIT_DIR / "defaults" / "main.yml"
_OBSERVABILITY = _INIT_DIR / "tasks" / "observability.yml"
_MAIN_TASKS = _INIT_DIR / "tasks" / "main.yml"
_PROM_TMPL = _INIT_DIR / "templates" / "prometheus-openbao.yml.j2"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _duration_to_seconds(dur: str) -> float:
    """Parse a Prometheus-style duration (e.g. "30s", "1m", "500ms") to seconds.

    Supports the subset the scrape config uses: ms, s, m, h. Raises on anything
    unrecognised so a typo in the interval var fails the test loudly rather than
    silently passing.
    """
    m = re.fullmatch(r"(\d+)(ms|s|m|h)", dur.strip())
    assert m, f"unrecognised Prometheus duration {dur!r}"
    value, unit = int(m.group(1)), m.group(2)
    factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return value * factor


@pytest.fixture(scope="module")
def init_defaults() -> dict:
    raw = yaml.safe_load(_INIT_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw
    return raw


@pytest.fixture(scope="module")
def install_defaults() -> dict:
    raw = yaml.safe_load(_INSTALL_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw
    return raw


@pytest.fixture(scope="module")
def obs_tasks() -> list[dict]:
    tasks = yaml.safe_load(_OBSERVABILITY.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and tasks, "observability.yml must be a task list"
    return tasks


@pytest.fixture(scope="module")
def rendered_scrape_config() -> dict:
    """Render prometheus-openbao.yml.j2 with the role defaults and parse as YAML.

    Resolves the (few) chained default references the template needs so the
    render is offline and self-contained — the same approach the config-render
    test uses for config.hcl. The result is the actual scrape_configs document
    Prometheus would load.
    """
    jinja2 = pytest.importorskip("jinja2")
    raw = yaml.safe_load(_INIT_DEFAULTS.read_text(encoding="utf-8"))

    # Resolve the values the template references, mirroring the role's own
    # default chaining (addressing vars -> derived target/server-name vars).
    primary_ip = raw["openbao_primary_ip"]
    unsealer_ip = raw["openbao_unsealer_ip"]
    api_port = raw["openbao_api_port"]
    ctx = {
        "openbao_prometheus_job_name": raw["openbao_prometheus_job_name"],
        "openbao_prometheus_unsealer_job_name": raw["openbao_prometheus_unsealer_job_name"],
        "openbao_prometheus_scrape_interval": raw["openbao_prometheus_scrape_interval"],
        "openbao_prometheus_scrape_timeout": raw["openbao_prometheus_scrape_timeout"],
        "openbao_prometheus_metrics_path": raw["openbao_prometheus_metrics_path"],
        "openbao_prometheus_primary_target": f"{primary_ip}:{api_port}",
        "openbao_prometheus_unsealer_target": f"{unsealer_ip}:{api_port}",
        "openbao_prometheus_tls_ca_file": raw["openbao_prometheus_tls_ca_file"],
        "openbao_prometheus_primary_server_name": raw["openbao_primary_node_id"],
        "openbao_prometheus_unsealer_server_name": raw["openbao_unsealer_node_id"],
        "openbao_prometheus_scrape_bearer_token_file": raw[
            "openbao_prometheus_scrape_bearer_token_file"
        ],
    }
    text = jinja2.Template(_PROM_TMPL.read_text(encoding="utf-8")).render(**ctx)
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict) and "scrape_configs" in doc, (
        "rendered prometheus fragment must be a mapping with scrape_configs"
    )
    return doc


def _job(doc: dict, name: str) -> dict:
    for job in doc["scrape_configs"]:
        if job.get("job_name") == name:
            return job
    raise AssertionError(
        f"no scrape job named {name!r}; "
        f"jobs={[j.get('job_name') for j in doc['scrape_configs']]}"
    )


# --------------------------------------------------------------------------- #
# Req 10.1 / 10.2 — primary telemetry stanza exposes scrapeable metrics
# --------------------------------------------------------------------------- #
class TestPrimaryTelemetryEndpoint:
    """The primary exposes /v1/sys/metrics scrapeable by Prometheus (Req 10.1, 10.2)."""

    def test_telemetry_retention_at_least_one_minute(self, install_defaults):
        # Req 10.1 — prometheus_retention_time >= 1 minute.
        assert _duration_to_seconds(install_defaults["openbao_telemetry_retention"]) >= 60

    def test_config_hcl_has_telemetry_prometheus_retention(self):
        text = _PRIMARY_CONFIG_TMPL.read_text(encoding="utf-8")
        assert "telemetry {" in text
        assert "prometheus_retention_time" in text

    def test_unauthenticated_metrics_access_enabled_by_default(self, install_defaults):
        # Req 10.2 — Prometheus scrapes without a stored token: the platform
        # default exposes metrics unauthenticated.
        assert install_defaults["openbao_telemetry_unauthenticated_metrics"] is True

    def test_config_hcl_renders_unauthenticated_metrics_access(self):
        text = _PRIMARY_CONFIG_TMPL.read_text(encoding="utf-8")
        # rendered into the telemetry stanza from the var (lower-cased bool).
        assert "unauthenticated_metrics_access" in text
        assert "openbao_telemetry_unauthenticated_metrics" in text

    def test_sealed_metric_guarantee_documented(self):
        # Req 10.8 / FD.7 — openbao_core_unsealed = 0 while sealed is
        # OpenBao-native, documented as verified by the degraded tests (12.4/12.5).
        text = _PRIMARY_CONFIG_TMPL.read_text(encoding="utf-8")
        assert "openbao_core_unsealed" in text
        assert "SEALED" in text or "sealed" in text


# --------------------------------------------------------------------------- #
# Req 10.2 / 10.3 — job `openbao` scrapes the primary at <= 30 s
# --------------------------------------------------------------------------- #
class TestOpenbaoScrapeJob:
    """Req 10.2, 10.3 — job openbao: primary /v1/sys/metrics at <= 30 s over TLS."""

    def test_job_openbao_present(self, rendered_scrape_config):
        job = _job(rendered_scrape_config, "openbao")
        assert job

    def test_scrape_interval_at_most_30s(self, rendered_scrape_config):
        job = _job(rendered_scrape_config, "openbao")
        assert "scrape_interval" in job, "job openbao must set an explicit scrape_interval"
        assert _duration_to_seconds(str(job["scrape_interval"])) <= 30  # Req 10.3

    def test_scrapes_primary_metrics_endpoint(self, rendered_scrape_config, init_defaults):
        job = _job(rendered_scrape_config, "openbao")
        assert job["metrics_path"] == "/v1/sys/metrics"
        # ?format=prometheus carried as a scrape param
        assert job.get("params", {}).get("format") == ["prometheus"]
        # target is the PRIMARY (10.0.20.10:8200)
        targets = job["static_configs"][0]["targets"]
        assert f"{init_defaults['openbao_primary_ip']}:{init_defaults['openbao_api_port']}" in targets

    def test_scrapes_over_tls(self, rendered_scrape_config):
        job = _job(rendered_scrape_config, "openbao")
        # the primary terminates TLS (Req 6.1) — scrape must be https with a CA.
        assert job["scheme"] == "https"
        assert "ca_file" in job["tls_config"]

    def test_job_label_is_openbao(self, rendered_scrape_config, init_defaults):
        # Req 10.2 — the job label is `openbao`.
        assert init_defaults["openbao_prometheus_job_name"] == "openbao"
        labels = rendered_scrape_config["scrape_configs"]
        primary = _job({"scrape_configs": labels}, "openbao")
        assert primary["static_configs"][0]["labels"]["service"] == "openbao"


# --------------------------------------------------------------------------- #
# Req 10.2 — job `openbao_unsealer` gives the unsealer an up{} target
# --------------------------------------------------------------------------- #
class TestUnsealerScrapeTarget:
    """Req 10.2 — a target labelled openbao_unsealer scraping the unsealer."""

    def test_job_openbao_unsealer_present(self, rendered_scrape_config):
        job = _job(rendered_scrape_config, "openbao_unsealer")
        assert job

    def test_unsealer_target_is_the_unsealer(self, rendered_scrape_config, init_defaults):
        job = _job(rendered_scrape_config, "openbao_unsealer")
        targets = job["static_configs"][0]["targets"]
        # target is the UNSEALER (10.0.20.11:8200), distinct from the primary
        assert (
            f"{init_defaults['openbao_unsealer_ip']}:{init_defaults['openbao_api_port']}"
            in targets
        )
        assert (
            f"{init_defaults['openbao_primary_ip']}:{init_defaults['openbao_api_port']}"
            not in targets
        )

    def test_unsealer_job_interval_at_most_30s(self, rendered_scrape_config):
        job = _job(rendered_scrape_config, "openbao_unsealer")
        assert _duration_to_seconds(str(job["scrape_interval"])) <= 30

    def test_unsealer_job_name_label(self, init_defaults):
        # the OpenBaoUnsealerUnreachable alert keys off up{job="openbao_unsealer"}.
        assert init_defaults["openbao_prometheus_unsealer_job_name"] == "openbao_unsealer"


# --------------------------------------------------------------------------- #
# Task wiring + idempotency + secrecy
# --------------------------------------------------------------------------- #
class TestTaskWiring:
    """The §10.1 render tasks exist, are gated primary-only, and are wired in."""

    def test_observability_renders_the_scrape_config(self, obs_tasks):
        names = [t.get("name", "") for t in obs_tasks]
        render = next(
            (t for t in obs_tasks if "Render the OpenBao Prometheus scrape config" in t.get("name", "")),
            None,
        )
        assert render is not None, f"no render task; names={names}"
        tmpl = render["ansible.builtin.template"]
        assert tmpl["src"] == "prometheus-openbao.yml.j2"
        assert "openbao_prometheus_scrape_config_file" in tmpl["dest"]

    def test_observability_gated_on_enabled_flag(self, obs_tasks):
        render = next(
            t for t in obs_tasks if "Render the OpenBao Prometheus scrape config" in t.get("name", "")
        )
        assert "openbao_prometheus_enabled" in yaml.safe_dump(render["when"])

    def test_main_includes_observability_primary_only(self):
        # parse the task list and find the include_tasks: observability.yml entry,
        # then assert its `when` gates on the primary role (rendered once).
        tasks = yaml.safe_load(_MAIN_TASKS.read_text(encoding="utf-8"))
        include = next(
            (
                t
                for t in tasks
                if t.get("ansible.builtin.include_tasks") == "observability.yml"
            ),
            None,
        )
        assert include is not None, "tasks/main.yml must include observability.yml"
        assert 'openbao_role == "primary"' in yaml.safe_dump(include["when"])

    def test_prometheus_enabled_default_true(self, init_defaults):
        assert init_defaults["openbao_prometheus_enabled"] is True


class TestSecrecy:
    """No secret defaulted; any scrape token referenced by path only."""

    def test_no_secret_token_value_defaulted(self, init_defaults):
        # only a FILE PATH for the (optional) authenticated-scrape token — never
        # a token value.
        assert init_defaults["openbao_prometheus_scrape_bearer_token_file"]
        # the path var is a filesystem path, not a token literal
        assert init_defaults["openbao_prometheus_scrape_bearer_token_file"].startswith("/")

    def test_template_references_token_by_path_only(self):
        tmpl = _PROM_TMPL.read_text(encoding="utf-8")
        # the authenticated-scrape alternative uses credentials_file (a path),
        # never an inline `credentials:` literal.
        assert "credentials_file" in tmpl
        assert re.search(r"credentials:\s*[\"']?\S", tmpl) is None

    def test_no_secret_literal_in_template(self):
        pat = re.compile(
            r"(hvs\.[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9]{15,}|s\.[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
        )
        assert not pat.search(_PROM_TMPL.read_text(encoding="utf-8"))
