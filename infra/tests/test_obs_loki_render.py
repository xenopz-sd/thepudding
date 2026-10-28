"""Offline config-render test for the SVC-18 Loki monolithic configuration.

Task 4.5 (spec: svc-17-18-observability) — requirements.md FR-10 (per-tenant /
per-label limits) and §6 (30-day retention). design.md §3.2 (observability_stack
role, Loki guest) and §Testing Strategy ("test_obs_loki_render.py — monolithic
mode, 30-day retention, per-tenant limits present"). OFFLINE unit /
config-validation test for the artifact the ``observability_stack`` role
templates for the Loki guest:

    templates/loki-config.yaml.j2

rendered with the role's ``defaults/main.yml`` variable set.

=== HOW THE OFFLINE RENDER WORKS (Ansible-specific jinja filters) ===

Same harness as the sibling render tests (``test_svc09_traefik_config_render.py``
etc.): load ``defaults/main.yml`` with PyYAML, resolve the shallow jinja
self-references with a fixed-point pass, then render with a stock
``jinja2.Environment``.

The Loki template's only non-trivial jinja is the retention-unit normaliser,
which converts the human-facing ``<N>d`` retention var to Loki's ``h``/``m``/``s``
grammar (``30d`` → ``720h``) using jinja2 BUILT-IN filters only (``string``,
``trim``, ``lower``, ``int``) plus slicing — so a stock Environment renders it
faithfully. For parity with the other SVC-17/18 render tests the harness still
registers the ``bool`` / ``lower`` / ``basename`` / ``to_json`` stand-ins, but
this template does not require ``to_json``.

=== WHAT IS ASSERTED (task 4.5 contract — FR-10 / §6) ===

  * monolithic / single-store-tsdb schema: ``schema: v13``, ``store: tsdb``,
    ``object_store: filesystem`` (3.x single-node idiom).
  * 30-day retention expressed as ``720h`` on BOTH the compactor path
    (``compactor.retention_enabled: true``) and
    ``limits_config.retention_period`` (the value that actually drives deletion).
  * the per-label limits are present with the values from defaults:
    ``ingestion_rate_mb``, ``ingestion_burst_size_mb``, ``max_streams_per_user``,
    ``max_query_series``.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_obs_loki_render.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "observability_stack"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_LOKI_TMPL = _ROLE_DIR / "templates" / "loki-config.yaml.j2"


def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    env.filters["to_json"] = lambda v, **kw: json.dumps(v)
    return env


def _resolve_self_references(raw: dict) -> dict:
    env = _make_env()
    resolved = dict(raw)

    def _render_scalars(d: dict) -> tuple[dict, bool]:
        changed = False
        out = {}
        for key, val in d.items():
            if isinstance(val, str) and "{{" in val:
                try:
                    rendered = env.from_string(val).render(**resolved)
                except Exception:
                    rendered = val
                if rendered != val:
                    changed = True
                out[key] = rendered
            elif isinstance(val, list):
                new_list = []
                for item in val:
                    if isinstance(item, str) and "{{" in item:
                        try:
                            r = env.from_string(item).render(**resolved)
                        except Exception:
                            r = item
                        if r != item:
                            changed = True
                        new_list.append(r)
                    else:
                        new_list.append(item)
                out[key] = new_list
            else:
                out[key] = val
        return out, changed

    for _ in range(10):
        resolved, changed = _render_scalars(resolved)
        if not changed:
            break
    return resolved


@pytest.fixture(scope="module")
def role_vars() -> dict:
    assert _DEFAULTS.is_file(), f"expected role defaults at {_DEFAULTS}"
    raw = yaml.safe_load(_DEFAULTS.read_text())
    assert isinstance(raw, dict) and raw, "defaults/main.yml must be a non-empty mapping"
    return _resolve_self_references(raw)


@pytest.fixture(scope="module")
def loki_doc(role_vars) -> dict:
    env = _make_env()
    rendered = env.from_string(_LOKI_TMPL.read_text()).render(**role_vars)
    doc = yaml.safe_load(rendered)
    assert isinstance(doc, dict), "rendered loki-config.yaml must parse as a YAML mapping"
    return doc


class TestLokiConfigRender:
    def test_monolithic_single_store_tsdb_schema_v13(self, loki_doc):
        """§3.2 / FR: monolithic single-store-tsdb, schema v13, filesystem store."""
        configs = loki_doc["schema_config"]["configs"]
        assert configs, "schema_config.configs must be non-empty"
        latest = configs[-1]
        assert latest["schema"] == "v13", "Loki 3.x single-store schema must be v13"
        assert latest["store"] == "tsdb", "store must be tsdb (single-store-tsdb, not boltdb-shipper)"
        assert latest["object_store"] == "filesystem", (
            "object_store must be filesystem (single-node monolithic deployment)"
        )
        # Monolithic single-node: inmemory ring + replication_factor 1.
        assert loki_doc["common"]["replication_factor"] == 1
        assert loki_doc["common"]["ring"]["kvstore"]["store"] == "inmemory"

    def test_30_day_retention_720h_on_compactor_and_limits(self, loki_doc, role_vars):
        """§6 / FR-10: 30-day retention (720h) via compactor + limits_config.retention_period."""
        assert role_vars["observability_logs_retention"] == "30d", (
            "defaults must declare 30-day log retention"
        )
        # Compactor must be the deletion mechanism and enabled.
        compactor = loki_doc["compactor"]
        assert compactor["retention_enabled"] is True, (
            "compactor.retention_enabled must be true so limits_config.retention_period deletes"
        )
        # retention_period must be the 30d→720h normalised value.
        retention = str(loki_doc["limits_config"]["retention_period"])
        assert retention == "720h", (
            f"limits_config.retention_period must normalise 30d to 720h, got {retention!r}"
        )

    def test_per_label_limits_present_with_default_values(self, loki_doc, role_vars):
        """FR-10: per-label ingestion + query limits present with the default values."""
        limits = loki_doc["limits_config"]
        assert limits["ingestion_rate_mb"] == role_vars["observability_loki_ingestion_rate_mb"], (
            "ingestion_rate_mb must carry the default value (FR-10)"
        )
        assert limits["ingestion_burst_size_mb"] == role_vars["observability_loki_ingestion_burst_mb"], (
            "ingestion_burst_size_mb must carry the default value (FR-10)"
        )
        assert limits["max_streams_per_user"] == role_vars["observability_loki_max_streams_per_user"], (
            "max_streams_per_user must carry the default value (FR-10)"
        )
        assert limits["max_query_series"] == role_vars["observability_loki_max_query_series"], (
            "max_query_series must carry the default value (FR-10)"
        )

    def test_internal_only_push_api_port(self, loki_doc, role_vars):
        """FR-13 posture: the push/query API binds the internal VLAN-20 port only."""
        assert loki_doc["server"]["http_listen_port"] == role_vars["observability_loki_port"], (
            "Loki must bind its internal push/query API port from defaults (3100)"
        )

    def test_retention_normaliser_handles_hms_passthrough(self, role_vars):
        """A retention var already in h/m/s grammar passes through unchanged."""
        v = dict(role_vars)
        v["observability_logs_retention"] = "168h"
        env = _make_env()
        doc = yaml.safe_load(env.from_string(_LOKI_TMPL.read_text()).render(**v))
        assert str(doc["limits_config"]["retention_period"]) == "168h", (
            "an h/m/s retention value must pass through the normaliser unchanged"
        )
