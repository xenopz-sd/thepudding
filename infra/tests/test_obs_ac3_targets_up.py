"""Offline bug-condition exploration + per-target config-contract test for the
SVC-17/18 AC-3 V1_Scrape_Set `up == 1` bugfix.

Spec: .kiro/specs/fix-obs-ac3-targets-up/ (bugfix.md, design.md, tasks.md).

Background: the merged `fix-obs-scrape-targets-multi-inventory` bugfix made
Prometheus *discover* the four V1_Scrape_Set services (SVC-01 postgres_exporter,
SVC-06 ZITADEL, SVC-07 OpenBao, SVC-09 Traefik) via generated `file_sd` — proven
live (`query=up` went 1 -> 5 targets). But discovery is not reachability: of the
four discovered targets only SVC-01 postgres reports `up == 1`; the other three
report `up == 0`, each for a distinct per-target endpoint/TLS defect the
discovery fix merely EXPOSED. AC-3 (the merged svc-17-18-observability acceptance
criterion — all four V1_Scrape_Set at `up == 1` via generated file_sd) is
therefore only 1/4 closed.

The three defects (captured live):

  Defect 1 — OpenBao scrape TLS trust (x509). The `platform-services` scrape job
    in observability_stack/templates/prometheus.yml.j2 has NO `tls_config`.
    OpenBao's generated target carries `__scheme__: https`; Prometheus has no CA
    for its self-signed/mock cert and no insecure_skip_verify -> the TLS
    handshake fails (`x509: certificate signed by unknown authority`) -> up == 0.

  Defect 2 — Traefik :8082 metrics entrypoint not host-published (conn refused).
    traefik_install/tasks/main.yml's Compose `ports:` publishes only :80 (web)
    and :443 (websecure); the :8082 metrics entrypoint listens only inside the
    container, so an off-guest Prometheus gets `connection refused` on
    10.0.20.12:8082 -> up == 0.

  Defect 3 — ZITADEL metrics config key wrong (HTTP 404). zitadel-server/
    tasks/main.yml's Compose env (dca_env) sets `ZITADEL_METRICS_ENABLED`, which
    ZITADEL v4 does not recognize (the key is Metrics.Type -> ZITADEL_METRICS_TYPE).
    So metrics are not served on /debug/metrics -> HTTP 404 -> up == 0.

This module encodes the FIXED per-target config contract (Property 1) so the fix
is validated when it passes after implementation, and the regression cannot
silently return. On the UNFIXED tree all three assertions FAIL — that failure
CONFIRMS the bug (bug-condition exploration test).

Pure PyYAML + text parse; no ansible-playbook binary, no live infra. Mirrors the
task-walking helpers of the sibling tests
infra/tests/test_obs_scrape_multi_inventory.py and
infra/tests/test_obs_bootstrap_token_bridging.py.

Run:  set +H
      ~/venv/devinfra/bin/pytest infra/tests/test_obs_ac3_targets_up.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"

_PROMETHEUS_TEMPLATE = (
    _ROLES / "observability_stack" / "templates" / "prometheus.yml.j2"
)
_TRAEFIK_TASKS = _ROLES / "traefik_install" / "tasks" / "main.yml"
_ZITADEL_TASKS = _ROLES / "zitadel-server" / "tasks" / "main.yml"

# Role var the design names to drive the platform-services tls_config.
_TLS_SKIP_VERIFY_VAR = "observability_platform_services_tls_skip_verify"
# The Traefik metrics-entrypoint port var the ports entry must reference.
_TRAEFIK_METRICS_PORT_VAR = "traefik_entrypoint_metrics_port"
# ZITADEL metrics env keys. In v4.18.0 the NEW Instrumentation exporter-type key
# is the one that actually enables /debug/metrics (the legacy Metrics.Type key is
# deprecated and superseded — on its own it 404'd). The boolean key is invalid.
_ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE = (
    "ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE"
)
_ZITADEL_METRICS_TYPE = "ZITADEL_METRICS_TYPE"
_ZITADEL_METRICS_ENABLED = "ZITADEL_METRICS_ENABLED"


# ---------------------------------------------------------------------------
# Shared task-walking helpers (mirror the sibling obs tests)
# ---------------------------------------------------------------------------

def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _dca_services_vars(tasks: list[dict]) -> list[dict]:
    """Every `dca_services` list found in the `vars` of a docker-compose-app
    include task."""
    out: list[dict] = []
    for t in tasks:
        task_vars = t.get("vars") or {}
        if isinstance(task_vars, dict) and "dca_services" in task_vars:
            svcs = task_vars["dca_services"]
            if isinstance(svcs, list):
                out.extend(s for s in svcs if isinstance(s, dict))
    return out


def _dca_env_dicts(tasks: list[dict]) -> list[dict]:
    """Every `dca_env` dict found in the `vars` of a docker-compose-app include
    task."""
    out: list[dict] = []
    for t in tasks:
        task_vars = t.get("vars") or {}
        if isinstance(task_vars, dict) and isinstance(
            task_vars.get("dca_env"), dict
        ):
            out.append(task_vars["dca_env"])
    return out


def _platform_services_job_block(template_path: Path) -> str:
    """The text of the `platform-services` scrape job block in the Prometheus
    template — from its `- job_name: "platform-services"` line up to (but not
    including) the next `- job_name:` line (or EOF). The template is NOT pure
    YAML (it carries `{{ }}` Jinja), so it is parsed as text/lines."""
    lines = template_path.read_text(encoding="utf-8").splitlines()
    job_re = re.compile(r'^\s*-\s*job_name:\s*"([^"]+)"')
    jobs: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = job_re.match(line)
        if m:
            jobs.append((i, m.group(1)))
    for idx, (start, name) in enumerate(jobs):
        if name == "platform-services":
            end = jobs[idx + 1][0] if idx + 1 < len(jobs) else len(lines)
            return "\n".join(lines[start:end])
    raise AssertionError(
        'no `- job_name: "platform-services"` scrape job found in '
        f"{template_path}"
    )


# ---------------------------------------------------------------------------
# Property 1 — Bug Condition (these FAIL on the unfixed tree)
# ---------------------------------------------------------------------------

def test_platform_services_job_has_gated_tls_config():
    """Defect 1 — the `platform-services` scrape job must carry a `tls_config`
    whose `insecure_skip_verify` is driven by the role var
    `observability_platform_services_tls_skip_verify`, so the OpenBao HTTPS
    V1_Scrape_Set target's TLS handshake succeeds (`up == 1`). Prometheus applies
    `tls_config` only to `https` targets, so the plain-HTTP V1_Scrape_Set targets
    (postgres/traefik/zitadel) are unaffected.

    Unfixed tree: the `platform-services` job has NO `tls_config` (the OpenBao
    HTTPS scrape fails with `x509: certificate signed by unknown authority`,
    up == 0) -> FAILS, confirming the bug.

    Validates: Requirements 2.1
    """
    block = _platform_services_job_block(_PROMETHEUS_TEMPLATE)

    assert "tls_config" in block, (
        "the `platform-services` scrape job in prometheus.yml.j2 must carry a "
        "`tls_config` block (the OpenBao V1_Scrape_Set target is HTTPS; without "
        "it the scrape fails with `x509: certificate signed by unknown "
        "authority` and reports up == 0). None found in the platform-services "
        f"job block:\n{block}"
    )
    assert "insecure_skip_verify" in block, (
        "the `platform-services` job's `tls_config` must set "
        "`insecure_skip_verify` (the homelab mock-cert posture — no platform CA "
        "until SVC-08/EJBCA)."
    )
    assert _TLS_SKIP_VERIFY_VAR in block, (
        "the `platform-services` job's `insecure_skip_verify` must be DRIVEN by "
        f"the role var `{_TLS_SKIP_VERIFY_VAR}` (so it defaults true for the "
        "homelab but is tightenable to false + a real ca_file later), not a "
        f"hard-coded literal. The var name does not appear in the job block:\n"
        f"{block}"
    )


def test_traefik_compose_publishes_metrics_port_on_internal_vlan20_bind():
    """Defect 2 — the Traefik Compose `ports:` list must publish the metrics port
    (`traefik_entrypoint_metrics_port`) so Prometheus on another guest can reach
    10.0.20.12:8082/metrics (`up == 1`), AND that entry must be an INTERNAL
    VLAN-20 bind — NOT bound to `0.0.0.0` (never a second LAN ingress host port;
    SVC-09 §13 / integration-boundaries §1). The :80/:443 web/websecure entries
    are unchanged.

    Unfixed tree: only :80 (web) and :443 (websecure) are published; :8082 is
    never host-published, so an off-guest Prometheus gets `connection refused` on
    10.0.20.12:8082 (up == 0) -> FAILS, confirming the bug.

    Validates: Requirements 2.2
    """
    services = _dca_services_vars(_load_tasks(_TRAEFIK_TASKS))
    assert services, (
        "no docker-compose-app `dca_services` found in "
        f"{_TRAEFIK_TASKS} (expected the Traefik Compose stack)."
    )

    # Collect every ports entry across the Traefik dca_services.
    ports_entries: list[str] = []
    for svc in services:
        ports = svc.get("ports")
        if isinstance(ports, list):
            ports_entries.extend(str(p) for p in ports)

    assert ports_entries, (
        "the Traefik Compose service must declare a `ports:` list "
        f"(found none in {_TRAEFIK_TASKS})."
    )

    metrics_entries = [
        p for p in ports_entries if _TRAEFIK_METRICS_PORT_VAR in p
    ]
    assert metrics_entries, (
        "the Traefik Compose `ports:` list must publish the metrics port "
        f"(an entry referencing `{_TRAEFIK_METRICS_PORT_VAR}`), so Prometheus on "
        "another guest can reach the Traefik :8082 metrics endpoint. On the "
        "unfixed tree only the :80/:443 web/websecure entries are published, so "
        "the scrape fails with `connection refused`. Current ports entries: "
        f"{ports_entries!r}"
    )

    # The metrics publish must be an internal VLAN-20 bind — NOT 0.0.0.0/LAN.
    for entry in metrics_entries:
        assert "0.0.0.0" not in entry, (
            "the Traefik metrics port publish must be an INTERNAL VLAN-20 bind "
            "(bound to the Traefik guest's own VLAN-20 IP), never `0.0.0.0`/LAN "
            "— it is a Prometheus-only internal metrics bind (SVC-09 §13 / "
            f"integration-boundaries §1), not a second ingress. Got: {entry!r}"
        )
        # A host-bound publish has an explicit bind address: "IP:cport:hport".
        # A bare "cport:hport" (two colon-parts) publishes on 0.0.0.0 by default,
        # which is forbidden here — require an explicit (non-0.0.0.0) bind IP.
        assert entry.count(":") >= 2, (
            "the Traefik metrics port publish must carry an explicit VLAN-20 "
            "bind IP (form `<vlan20-ip>:<port>:<port>`); a bare `<port>:<port>` "
            "publishes on 0.0.0.0/LAN, which is forbidden for the internal "
            f"metrics bind. Got: {entry!r}"
        )


def test_zitadel_compose_env_sets_instrumentation_metric_exporter():
    """Defect 3 — the ZITADEL Compose env (`dca_env`) must set
    `ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE` (the NEW v4.18.0 key that
    actually enables /debug/metrics), and must NOT set the invalid
    `ZITADEL_METRICS_ENABLED`, so metrics are served at /debug/metrics
    (`up == 1`).

    v4.18.0 has TWO metrics blocks in defaults.yaml: the LEGACY `Metrics.Type`
    (env `ZITADEL_METRICS_TYPE`, Deprecated, default `otel`) and the NEW
    `Instrumentation.Metric.Exporter.Type`
    (env `ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE`, default `none`). The NEW
    block takes precedence and defaults to `none`, DISABLING /debug/metrics — so
    setting only the deprecated key 404'd. The required assertion is therefore
    the NEW instrumentation key. The legacy `ZITADEL_METRICS_TYPE` is also kept
    set (both present is harmless, documents intent across the deprecation) and
    its presence is asserted too; the MUST-HAVE is the instrumentation key.
    Live-confirmed on the throwaway cluster: the instrumentation exporter type
    set to `prometheus` flipped /debug/metrics 404 -> 200.

    Unfixed tree: `dca_env` set only the deprecated `ZITADEL_METRICS_TYPE` (and
    earlier the invalid `ZITADEL_METRICS_ENABLED`) and did NOT set the NEW
    instrumentation key -> FAILS, confirming the bug.

    Validates: Requirements 2.3
    """
    envs = _dca_env_dicts(_load_tasks(_ZITADEL_TASKS))
    assert envs, (
        "no docker-compose-app `dca_env` dict found in "
        f"{_ZITADEL_TASKS} (expected the ZITADEL Compose stack env)."
    )

    # Aggregate keys across every dca_env dict (there is one for the stack).
    env_keys: set[str] = set()
    for env in envs:
        env_keys.update(env.keys())

    assert _ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE in env_keys, (
        "the ZITADEL Compose env must set the NEW v4.18.0 metrics key "
        f"`{_ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE}` "
        "(Instrumentation.Metric.Exporter.Type = prometheus). This block takes "
        "precedence over the deprecated Metrics.Type and defaults to `none`, so "
        "without it /debug/metrics stays unserved (HTTP 404, up == 0) even when "
        "the deprecated ZITADEL_METRICS_TYPE=otel is set. Live-confirmed 200 on "
        f"the throwaway cluster. dca_env keys seen: {sorted(env_keys)!r}"
    )
    # The legacy (deprecated) key is kept ALSO to document intent across the
    # deprecation — both present is harmless.
    assert _ZITADEL_METRICS_TYPE in env_keys, (
        "the ZITADEL Compose env is expected to also keep the deprecated "
        f"`{_ZITADEL_METRICS_TYPE}` key set (harmless; documents intent across "
        f"the v4.18.0 deprecation). dca_env keys seen: {sorted(env_keys)!r}"
    )
    assert _ZITADEL_METRICS_ENABLED not in env_keys, (
        "the ZITADEL Compose env must NOT set the invalid "
        f"`{_ZITADEL_METRICS_ENABLED}` key (ZITADEL v4 has no `Metrics.Enabled` "
        "config key — an unrecognized key leaves metrics unserved on the "
        "expected path, HTTP 404). On the unfixed tree it is present -> this "
        f"assertion FAILS, confirming the bug. dca_env keys seen: "
        f"{sorted(env_keys)!r}"
    )


# ---------------------------------------------------------------------------
# Property 2 — Preservation (these already hold on the unfixed tree and must
# keep holding after the fix — observation-first methodology)
# ---------------------------------------------------------------------------

_OBS_DEFAULTS = _ROLES / "observability_stack" / "defaults" / "main.yml"
_TRAEFIK_DEFAULTS = _ROLES / "traefik_install" / "defaults" / "main.yml"
_SCRAPE_TARGETS = _ROLES / "observability_stack" / "tasks" / "scrape_targets.yml"

# The web/websecure port vars the Traefik Compose ports: list must keep publishing.
_TRAEFIK_WEB_PORT_VAR = "traefik_entrypoint_web_port"
_TRAEFIK_WEBSECURE_PORT_VAR = "traefik_entrypoint_websecure_port"
# The degrade filter shape (identical to test_obs_scrape_multi_inventory.py).
_DEGRADE_WHEN = "(_obs_row_index | length) > 0"


def _load_single_yaml(path: Path) -> dict:
    """Load a single-document YAML file (defaults/main.yml) as a dict."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_preservation_postgres_v1_scrape_target_is_http_not_https():
    """(a) The SVC-01 postgres V1_Scrape_Set entry in
    `observability_builtin_scrape_targets` must stay plain HTTP — it must carry
    NO `scheme: https` — so the new `platform-services` `tls_config` (which
    Prometheus applies only to `https` targets) does not apply to it and postgres
    stays `up == 1` (unchanged).

    Already holds on the unfixed tree -> PASSES now, and must keep passing after
    the fix.

    Validates: Requirements 3.1, 3.2
    """
    defaults = _load_single_yaml(_OBS_DEFAULTS)
    targets = defaults.get("observability_builtin_scrape_targets")
    assert isinstance(targets, list) and targets, (
        "observability_builtin_scrape_targets must be a non-empty list in "
        f"{_OBS_DEFAULTS}."
    )

    postgres_rows = [
        t for t in targets
        if isinstance(t, dict) and t.get("service") == "postgres"
    ]
    assert postgres_rows, (
        "the SVC-01 postgres V1_Scrape_Set row (service: postgres) must exist in "
        f"observability_builtin_scrape_targets ({_OBS_DEFAULTS})."
    )

    for row in postgres_rows:
        assert row.get("scheme") in (None, "", "http"), (
            "the SVC-01 postgres V1_Scrape_Set target must stay plain HTTP (no "
            "`scheme: https`), so the new platform-services `tls_config` "
            "(applied by Prometheus only to https targets) does not touch it and "
            f"postgres stays up == 1. Got scheme={row.get('scheme')!r} in {row!r}"
        )


def test_preservation_traefik_compose_still_publishes_web_and_websecure():
    """(b) The Traefik Compose `ports:` list in traefik_install/tasks/main.yml
    must STILL publish the web + websecure ports (entries referencing
    `traefik_entrypoint_web_port` and `traefik_entrypoint_websecure_port`) — the
    documented FR-7 LAN ingress exception — unchanged by the metrics-port fix.

    Already holds on the unfixed tree -> PASSES now, and must keep passing after
    the fix.

    Validates: Requirements 3.3
    """
    services = _dca_services_vars(_load_tasks(_TRAEFIK_TASKS))
    assert services, (
        "no docker-compose-app `dca_services` found in "
        f"{_TRAEFIK_TASKS} (expected the Traefik Compose stack)."
    )

    ports_entries: list[str] = []
    for svc in services:
        ports = svc.get("ports")
        if isinstance(ports, list):
            ports_entries.extend(str(p) for p in ports)

    assert any(_TRAEFIK_WEB_PORT_VAR in p for p in ports_entries), (
        "the Traefik Compose `ports:` list must STILL publish the web (:80) "
        f"entry referencing `{_TRAEFIK_WEB_PORT_VAR}` (the FR-7 LAN ingress "
        f"exception). Current ports entries: {ports_entries!r}"
    )
    assert any(_TRAEFIK_WEBSECURE_PORT_VAR in p for p in ports_entries), (
        "the Traefik Compose `ports:` list must STILL publish the websecure "
        f"(:443) entry referencing `{_TRAEFIK_WEBSECURE_PORT_VAR}` (the FR-7 LAN "
        f"ingress exception). Current ports entries: {ports_entries!r}"
    )


def test_preservation_zitadel_dca_env_keeps_non_metrics_keys():
    """(c) The ZITADEL `dca_env` in zitadel-server/tasks/main.yml must keep its
    non-metrics config keys — only the metrics key changes. Assert a stable,
    representative subset that genuinely exists in the dca_env today is present:
    the external-domain/port keys, ZITADEL_TLS_ENABLED, at least one
    ZITADEL_DATABASE_POSTGRES_* key, and at least one ZITADEL_FIRSTINSTANCE_* key.

    Already holds on the unfixed tree -> PASSES now, and must keep passing after
    the fix.

    Validates: Requirements 3.4
    """
    envs = _dca_env_dicts(_load_tasks(_ZITADEL_TASKS))
    assert envs, (
        "no docker-compose-app `dca_env` dict found in "
        f"{_ZITADEL_TASKS} (expected the ZITADEL Compose stack env)."
    )

    env_keys: set[str] = set()
    for env in envs:
        env_keys.update(env.keys())

    # Representative stable non-metrics keys that genuinely exist in dca_env.
    for key in ("ZITADEL_EXTERNALDOMAIN", "ZITADEL_EXTERNALPORT",
                "ZITADEL_TLS_ENABLED"):
        assert key in env_keys, (
            f"the ZITADEL dca_env must keep the non-metrics key `{key}` "
            "(only the metrics key changes in this fix). dca_env keys seen: "
            f"{sorted(env_keys)!r}"
        )
    assert any(k.startswith("ZITADEL_DATABASE_POSTGRES_") for k in env_keys), (
        "the ZITADEL dca_env must keep its backing-DB config (at least one "
        "`ZITADEL_DATABASE_POSTGRES_*` key). dca_env keys seen: "
        f"{sorted(env_keys)!r}"
    )
    assert any(k.startswith("ZITADEL_FIRSTINSTANCE_") for k in env_keys), (
        "the ZITADEL dca_env must keep its FirstInstance provisioning config (at "
        "least one `ZITADEL_FIRSTINSTANCE_*` key). dca_env keys seen: "
        f"{sorted(env_keys)!r}"
    )


def test_preservation_scrape_targets_degrade_filter_unchanged():
    """(d) The generated-file_sd discovery + degrade are unchanged: the
    `observability_stack/tasks/scrape_targets.yml` degrade filter
    `when: (_obs_row_index | length) > 0` ("scrape what exists") is kept, so an
    absent service yields no target file, not a failure. Same assertion shape as
    test_obs_scrape_multi_inventory.py.

    Already holds on the unfixed tree -> PASSES now, and must keep passing after
    the fix.

    Validates: Requirements 3.5
    """
    tasks = _load_tasks(_SCRAPE_TARGETS)
    degrade_tasks = [
        t for t in tasks if _DEGRADE_WHEN in str(t.get("when", ""))
    ]
    assert degrade_tasks, (
        "scrape_targets.yml must keep the degrade filter "
        f"`when: {_DEGRADE_WHEN}` on the 'resolve present built-services' step "
        "so a built service absent from every loaded inventory renders NO target "
        "file (degrade, not fail — 'scrape what exists')."
    )
