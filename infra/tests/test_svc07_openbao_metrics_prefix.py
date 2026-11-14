"""Bug-condition + preservation tests: OpenBao metrics carry the `openbao_` prefix (WS-C36).

THE BUG (found by the first live run of the SVC-07 contract tier on throwaway `shrimp`): OpenBao's telemetry
`metrics_prefix` defaults to "vault", so the live instance exposed `vault_core_unsealed` and no
`openbao_core_unsealed`. Every alert, rule and scrape assertion in this repository is keyed on the
`openbao_` names, so the OpenBaoSealed alerts (SVC-07 fragment and the SVC-17 baseline) could never fire.
Second finding, same run: while OpenBao is unsealed the endpoint still exposes a stale `core_unsealed = 0`
series from before the unseal (label `cluster=""`) for the whole telemetry retention window, so a bare
`openbao_core_unsealed == 0` would fire falsely after every restart. The alert therefore takes the per-instance
MAX: it fires only when no series for that instance reads 1.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO / "ansible/roles/openbao_install/templates/config.hcl.j2"
_INSTALL_DEFAULTS = _REPO / "ansible/roles/openbao_install/defaults/main.yml"
_INIT_DEFAULTS = _REPO / "ansible/roles/openbao_init_unseal/defaults/main.yml"
_BASELINE = _REPO / "ansible/roles/observability_stack/templates/rules/baseline.rules.yml.j2"

_EXPR = "max by (instance) (openbao_core_unsealed) == 0"


def _top_level_telemetry(text: str) -> str:
    """The top-level `telemetry {` block (the one that is not inside the listener)."""
    blocks = re.findall(r"^telemetry \{.*?^\}", text, flags=re.S | re.M)
    assert len(blocks) == 1, "exactly one top-level telemetry stanza"
    return blocks[0]


def test_BUG_top_level_telemetry_sets_the_openbao_metrics_prefix():
    block = _top_level_telemetry(_TEMPLATE.read_text(encoding="utf-8"))
    assert re.search(r'metrics_prefix\s*=\s*"\{\{ openbao_telemetry_metrics_prefix \}\}"', block)


def test_the_prefix_default_is_openbao():
    d = yaml.safe_load(_INSTALL_DEFAULTS.read_text(encoding="utf-8"))
    assert d["openbao_telemetry_metrics_prefix"] == "openbao"


def test_the_prefix_is_not_set_in_the_listener_telemetry_block():
    """metrics_prefix belongs to the top-level stanza (like retention); the listener block only holds the
    unauthenticated-access toggle."""
    text = _TEMPLATE.read_text(encoding="utf-8")
    listener_tel = re.search(r"listener \"tcp\" \{.*?^\}", text, flags=re.S | re.M).group(0)
    assert "metrics_prefix" not in listener_tel


def test_preservation_existing_top_level_telemetry_settings_are_kept():
    block = _top_level_telemetry(_TEMPLATE.read_text(encoding="utf-8"))
    assert "prometheus_retention_time" in block and "disable_hostname" in block


def test_BUG_sealed_alert_uses_the_per_instance_max_so_a_stale_zero_series_cannot_fire_it():
    d = yaml.safe_load(_INIT_DEFAULTS.read_text(encoding="utf-8"))
    assert d["openbao_alert_sealed_expr"] == _EXPR


def test_the_baseline_rule_uses_the_same_expression():
    text = _BASELINE.read_text(encoding="utf-8")
    m = re.search(r"- alert: OpenBaoSealed\s+expr: (.+)", text)
    assert m and m.group(1).strip() == _EXPR
