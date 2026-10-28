"""Offline contract tests for the SVC-06 zitadel-server role.

Slice-3 Task 15 (spec: svc-06-identity-provider) — design.md §3.2 /
requirements.md FR-1/FR-6, §8, §12, §13. OFFLINE config-validation of the
Ansible CONFIGURATION layer: the `zitadel-server` role (defaults + tasks +
secret-env template), parsed with PyYAML / inspected as text. No infra — always
runs.

What is asserted (the SVC-06 ZITADEL contract):
  * HTTP ingress is CROSS-GUEST (post svc-06-cross-guest-ingress conversion,
    ADR-0018): Traefik (SVC-09) runs on a SEPARATE guest, so the REAL path
    publishes the VLAN20_Port bound to ZITADEL's OWN VLAN-20 IP
    (`{{ zitadel_bind_ip }}:8080:8080` — never 0.0.0.0/LAN, the ADR-0018
    private-VLAN-bound-port exception, consumed only by Traefik) and delegates a
    Traefik file-provider Route_Fragment onto the Traefik_Guest; the `edge`
    network + Traefik Docker labels are gated behind the default-OFF
    `zitadel_mock_edge_network` mock path (co-located-dev only). Traefik remains
    the sole LAN-facing ingress (integration-boundaries §1 as clarified by
    ADR-0018). ZITADEL no longer attaches `edge` unconditionally nor forbids all
    host ports.
  * The ZITADEL_MASTERKEY + backing-DB password come from OpenBao (`bao` CLI) and
    live in a 0600 secret env layered via `extra_env_files` — NEVER the non-secret
    docker-compose-app `.env` (secret hygiene, §8). Secret-handling tasks no_log.
  * The masterkey is generated-ONCE (read-from-OpenBao-first; persisted only when
    absent) — never regenerated on a converged run (§6, non-rotatable).
  * EXTERNAL* / TLS settings present (match the Traefik endpoint — §6/§12).
  * Prometheus metrics enabled (§13).
  * Image pinned (never `latest`), backing a PostgreSQL 14-18 engine (§12).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc06_zitadel_contract.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "zitadel-server"
_TASKS = _ROLE / "tasks" / "main.yml"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_SECRET_ENV = _ROLE / "templates" / "zitadel.secret.env.j2"


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


def _task_text(t: dict) -> str:
    return str(t).lower()


def _defaults() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- #
# Image pin (tech.md: never latest).
# --------------------------------------------------------------------------- #
def test_image_pinned_not_latest():
    img = str(_defaults().get("zitadel_image", ""))
    assert img.startswith("ghcr.io/zitadel/zitadel:"), "ZITADEL image must be the official ghcr image"
    tag = img.rsplit(":", 1)[-1]
    assert tag and tag != "latest", f"ZITADEL image must be pinned to a specific tag, not 'latest' (got {tag!r})"


# --------------------------------------------------------------------------- #
# Cross-guest Traefik ingress — VLAN20_Port on the real path, edge/labels gated
# behind the default-off mock (post svc-06-cross-guest-ingress conversion;
# ADR-0018 clarifies integration-boundaries §1). SUPERSEDES the pre-conversion
# "no host port at all + unconditional edge" invariant, reconciled against the
# already-green tests/installer/test_svc06_cross_guest.py.
# --------------------------------------------------------------------------- #
def test_http_ingress_via_traefik_labels_no_host_port():
    """Post-conversion (svc-06-cross-guest-ingress, ADR-0018) ZITADEL ingress.

    ZITADEL's Traefik registration (traefik.enable=true + the Host router rule)
    still lives in the role text — it now sits in the MOCK-EDGE
    ``zitadel_traefik_labels`` set_fact — so the text asserts are kept.

    The zitadel docker-compose-app service's ``ports``/``networks``/``labels``
    and the stack ``dca_networks`` are now PATH-SELECTED Jinja CONDITIONAL
    expressions keyed on ``zitadel_mock_edge_network``:
      * REAL path (default-off mock): publish the VLAN20_Port bound to ZITADEL's
        OWN VLAN-20 IP (``zitadel_bind_ip:zitadel_http_port`` — the ADR-0018
        private-VLAN-bound-port exception, NEVER 0.0.0.0/LAN, consumed only by
        Traefik), NO ``edge`` network, NO Docker labels (the file-provider route
        supersedes them cross-guest).
      * MOCK-EDGE path: ``ports: []``, attach to the local ``edge`` net + emit
        the Traefik labels (co-located-dev discovery only).
    Traefik stays the sole LAN-facing ingress (integration-boundaries §1 as
    clarified by ADR-0018); the published port is VLAN-20-internal plumbing, NOT
    a second ingress. Mirrors tests/installer/test_svc06_cross_guest.py.
    """
    text = _TASKS.read_text(encoding="utf-8")
    # Traefik registration still present in the role text (now in the mock-edge
    # zitadel_traefik_labels set_fact, but still text-present).
    assert "traefik.enable=true" in text, "ZITADEL must register with Traefik via Docker labels (mock-edge path; FR-1/ADR-0018)"
    assert "traefik.http.routers.zitadel.rule=host(" in text.lower() or "routers.{{ zitadel_traefik_router }}.rule=host(" in text.lower(), (
        "ZITADEL must declare a Traefik Host router rule (mock-edge labels; ADR-0018)"
    )
    # The zitadel docker-compose-app service's ingress wiring is now path-selected
    # Jinja conditionals keyed on zitadel_mock_edge_network.
    tasks = _load_tasks(_TASKS)
    for t in tasks:
        if "ansible.builtin.include_role" in t:
            spec = t["ansible.builtin.include_role"]
            if isinstance(spec, dict) and spec.get("name") == "docker-compose-app":
                tvars = t.get("vars", {})
                services = tvars.get("dca_services", [])
                for svc in services:
                    if isinstance(svc, dict) and svc.get("name") == "zitadel":
                        # --- ports: VLAN20_Port on real, [] on mock, never 0.0.0.0 ---
                        ports_expr = svc.get("ports")
                        assert isinstance(ports_expr, str) and "{{" in ports_expr, (
                            "the ZITADEL compose `ports` must now be a path-selected Jinja "
                            f"conditional expression (ADR-0018); got {ports_expr!r}"
                        )
                        ports_c = ports_expr.replace(" ", "")
                        assert "zitadel_mock_edge_network" in ports_c, (
                            "`ports` must be keyed on zitadel_mock_edge_network (ADR-0018 path select)"
                        )
                        assert "zitadel_bind_ip" in ports_c and "zitadel_http_port" in ports_c, (
                            "the REAL-path port mapping must bind the guest's OWN VLAN-20 IP "
                            "(zitadel_bind_ip) + zitadel_http_port — the ADR-0018 VLAN20_Port, "
                            "consumed only by Traefik"
                        )
                        assert "0.0.0.0" not in ports_c, (
                            "the ZITADEL VLAN20_Port must NEVER bind 0.0.0.0/LAN — it is bound to "
                            "ZITADEL's own VLAN-20 IP, the ADR-0018 private-VLAN-bound-port exception, "
                            "NOT a LAN host port (integration-boundaries §1 as clarified by ADR-0018)"
                        )
                        assert "[]" in ports_c and "else" in ports_c, (
                            "`ports` must carry BOTH branches — [] on the mock-edge path and the "
                            "IP:port mapping on the real path (ADR-0018)"
                        )
                        # --- networks: edge on mock, [] on real ---
                        nets_expr = svc.get("networks")
                        assert isinstance(nets_expr, str) and "{{" in nets_expr, (
                            "the ZITADEL compose `networks` must now be a path-selected Jinja "
                            f"conditional (edge gated behind the mock, ADR-0018); got {nets_expr!r}"
                        )
                        nets_c = nets_expr.replace(" ", "")
                        assert "zitadel_mock_edge_network" in nets_c, (
                            "`networks` must be keyed on zitadel_mock_edge_network"
                        )
                        assert "zitadel_traefik_network" in nets_c, (
                            "`networks` must reference zitadel_traefik_network (the `edge` net) on the MOCK branch"
                        )
                        assert "[]" in nets_c, (
                            "`networks` must resolve to [] on the REAL path — ZITADEL no longer attaches "
                            "edge unconditionally; edge is gated behind the default-off mock (ADR-0018)"
                        )
                        # --- dca_networks: external edge on mock, [] on real ---
                        dca_nets_expr = tvars.get("dca_networks")
                        assert isinstance(dca_nets_expr, str) and "{{" in dca_nets_expr, (
                            "the stack `dca_networks` must now be a path-selected Jinja conditional "
                            f"(ADR-0018); got {dca_nets_expr!r}"
                        )
                        dca_c = dca_nets_expr.replace(" ", "")
                        assert "zitadel_mock_edge_network" in dca_c, (
                            "`dca_networks` must be keyed on zitadel_mock_edge_network"
                        )
                        assert "zitadel_traefik_network" in dca_c, (
                            "`dca_networks` must declare the external `edge` dependency on the MOCK branch"
                        )
                        assert "[]" in dca_c, (
                            "`dca_networks` must resolve to [] on the REAL path (no external edge dependency)"
                        )
                        # --- labels: mock-only set, [] on real ---
                        labels_expr = svc.get("labels")
                        assert isinstance(labels_expr, str) and "{{" in labels_expr, (
                            "the ZITADEL compose `labels` must now be a path-selected Jinja conditional "
                            f"(Traefik labels gated behind the mock, ADR-0018); got {labels_expr!r}"
                        )
                        labels_c = labels_expr.replace(" ", "")
                        assert "zitadel_mock_edge_network" in labels_c, (
                            "`labels` must be keyed on zitadel_mock_edge_network"
                        )
                        assert "zitadel_traefik_labels" in labels_c, (
                            "`labels` must reference the mock-only zitadel_traefik_labels set on the MOCK branch"
                        )
                        assert "[]" in labels_c, (
                            "`labels` must resolve to [] on the REAL path — Traefik on another guest cannot "
                            "read Docker labels; the file-provider route supersedes them (ADR-0018)"
                        )


# --------------------------------------------------------------------------- #
# Secrets via OpenBao + 0600 extra_env_files, never the non-secret .env (§8).
# --------------------------------------------------------------------------- #
def test_masterkey_and_db_password_from_openbao_not_env():
    text = _TASKS.read_text(encoding="utf-8")
    # The masterkey + DB password are read/written via the `bao` CLI docker-exec.
    assert "bao" in text and "docker" in text and "exec" in text, (
        "ZITADEL secrets must be read/written via the `bao` CLI docker-exec pattern (OpenBao, SVC-07)"
    )
    assert "zitadel_openbao_secret_path" in text, "masterkey must be stored under the OpenBao secret path"
    # The non-secret dca_env must NOT carry the masterkey or a DB password.
    tasks = _load_tasks(_TASKS)
    for t in tasks:
        if "ansible.builtin.include_role" in t:
            spec = t["ansible.builtin.include_role"]
            if isinstance(spec, dict) and spec.get("name") == "docker-compose-app":
                dca_env = str(t.get("vars", {}).get("dca_env", {})).lower()
                assert "masterkey" not in dca_env, "dca_env (non-secret .env) must NOT carry the masterkey (§8)"
                assert "password" not in dca_env, "dca_env (non-secret .env) must NOT carry a DB password (§8)"
                # The secret env is layered via extra_env_files on the zitadel service.
                services = t.get("vars", {}).get("dca_services", [])
                zsvc = next((s for s in services if isinstance(s, dict) and s.get("name") == "zitadel"), {})
                assert "extra_env_files" in zsvc, (
                    "the ZITADEL service must layer the 0600 secret env via extra_env_files"
                )


def test_secret_env_is_0600_and_carries_masterkey_and_db_password():
    text = _TASKS.read_text(encoding="utf-8")
    assert "zitadel.secret.env" in text and '"0600"' in text, (
        "the masterkey + DB password must be written to a 0600 runtime secret env on the guest (§8)"
    )
    tmpl = _SECRET_ENV.read_text(encoding="utf-8")
    assert "ZITADEL_MASTERKEY=" in tmpl, "the secret env template must set ZITADEL_MASTERKEY"
    assert "ZITADEL_DATABASE_POSTGRES_USER_PASSWORD=" in tmpl, (
        "the secret env template must set the backing-DB user password"
    )


def test_secret_handling_tasks_are_no_log():
    tasks = _load_tasks(_TASKS)
    for t in tasks:
        body = _task_text(t)
        if any(m in body for m in ("masterkey", "password", "bao_token")):
            # A pure DIAGNOSTIC task (ansible.builtin.fail / assert) that only
            # NAMES a secret-related concept (a KV path/field or var name) in its
            # message but carries NO secret VALUE is exempt — forcing no_log on a
            # `fail`/`assert` would suppress the very diagnostic it exists to emit
            # (e.g. the fail-fast "OpenBao read empty while wired" guard, whose
            # message names only the non-secret KV path+field). Spec:
            # fix-svc06-zitadel-db-app-password-mismatch.
            is_pure_diagnostic = (
                any(k in t for k in ("ansible.builtin.fail", "fail",
                                     "ansible.builtin.assert", "assert"))
                and "set_fact" not in t
                and "ansible.builtin.command" not in t
                and "ansible.builtin.shell" not in t
                and "ansible.builtin.template" not in t
                and "ansible.builtin.copy" not in t
            )
            if is_pure_diagnostic:
                continue
            # read/write/generate/bind/template tasks that touch secret material.
            if ("set_fact" in body or "bao" in body or "secret.env" in body or "kv" in body):
                assert t.get("no_log") is True, (
                    f"secret-handling task must be no_log: true: {t.get('name')!r}"
                )


# --------------------------------------------------------------------------- #
# Masterkey generated ONCE (read-first; persist only when absent) — §6.
# --------------------------------------------------------------------------- #
def test_masterkey_is_generated_once_not_regenerated():
    text = _TASKS.read_text(encoding="utf-8")
    # There must be a READ of the existing masterkey before generation, and the
    # persist step must be gated on the masterkey being absent (check-then-write).
    assert "read any existing masterkey" in text.lower(), (
        "the role must READ any existing masterkey from OpenBao before generating (never blind-regenerate — §6)"
    )
    assert "when newly generated" in text.lower() or "rc | default(1)) != 0" in text, (
        "the masterkey persist step must be gated on absence (check-then-write), so a converged run does not "
        "regenerate it (§6 non-rotatable)"
    )


# --------------------------------------------------------------------------- #
# External endpoint / TLS + metrics (§6/§12/§13).
# --------------------------------------------------------------------------- #
def test_external_endpoint_and_tls_settings_present():
    text = _TASKS.read_text(encoding="utf-8")
    for key in ("ZITADEL_EXTERNALDOMAIN", "ZITADEL_EXTERNALPORT", "ZITADEL_EXTERNALSECURE"):
        assert key in text, f"ZITADEL config must set {key} to match the Traefik endpoint (§6/§12)"
    d = _defaults()
    assert d.get("zitadel_external_secure") is True, "zitadel_external_secure must default true (HTTPS endpoint)"


def test_metrics_enabled():
    text = _TASKS.read_text(encoding="utf-8")
    # ZITADEL v4.18.0 has TWO metrics blocks; the NEW
    # Instrumentation.Metric.Exporter.Type (env
    # ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE) takes precedence and defaults
    # to `none`, so it is the key that actually enables /debug/metrics — the
    # deprecated Metrics.Type (env ZITADEL_METRICS_TYPE) on its own 404'd. See
    # .kiro/specs/fix-obs-ac3-targets-up/ (Defect 3). The invalid
    # Metrics.Enabled boolean must not be set.
    assert "ZITADEL_INSTRUMENTATION_METRIC_EXPORTER_TYPE" in text, (
        "ZITADEL v4.18.0 must expose Prometheus metrics via the NEW "
        "Instrumentation.Metric.Exporter.Type key (§13); live-confirmed the one "
        "that flips /debug/metrics to 200"
    )
    # The deprecated key is kept ALSO (harmless; documents intent).
    assert "ZITADEL_METRICS_TYPE" in text, (
        "the deprecated Metrics.Type (ZITADEL_METRICS_TYPE) is expected to remain "
        "set alongside the instrumentation key (harmless; documents intent)"
    )
    assert "ZITADEL_METRICS_ENABLED" not in text, (
        "ZITADEL v4 has no Metrics.Enabled key; ZITADEL_METRICS_ENABLED must not be set (left /debug/metrics 404)"
    )
    assert _defaults().get("zitadel_metrics_exporter_type") == "prometheus", (
        "zitadel_metrics_exporter_type must default to 'prometheus' (ZITADEL "
        "v4.18.0 Instrumentation.Metric.Exporter.Type — the key that serves "
        "/debug/metrics for Prometheus to scrape, §13)"
    )


# --------------------------------------------------------------------------- #
# Backing DB wiring points at the SVC-01 shared-zitadel instance (§3.3 / §12).
# --------------------------------------------------------------------------- #
def test_backing_db_env_points_at_svc01_instance():
    text = _TASKS.read_text(encoding="utf-8")
    for key in ("ZITADEL_DATABASE_POSTGRES_HOST", "ZITADEL_DATABASE_POSTGRES_DATABASE",
                "ZITADEL_DATABASE_POSTGRES_USER_USERNAME"):
        assert key in text, f"ZITADEL must configure {key} to reach its backing SVC-01 Postgres (§12)"
    d = _defaults()
    assert d.get("zitadel_db_slug") == "shared-zitadel", (
        "the backing DB slug must be shared-zitadel (the SVC-01 instance SVC-06 provisions)"
    )
