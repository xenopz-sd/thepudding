"""SVC-03 Garage alert rules (spec svc-03-alerts, WS-C52): structure + `promtool test rules` unit tests.

The rules live in the SVC-17/18 baseline (``platform-garage`` group). This file
renders the baseline with the role defaults and:

* asserts the six Garage alerts exist with the documented severities and use only
  ``service="garage"`` series (no node_exporter dependency);
* runs ``promtool test rules`` (local binary, else the pinned prom/prometheus
  image, else SKIPPED with a reason) on synthetic series that model a healthy
  cluster, one node down, quorum loss, a nearly full disk, resync errors and 5xx
  errors, so each alert is proven to fire when it should and stay quiet
  otherwise. Offline.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
import yaml

from test_obs_prometheus_render import _RULES_TMPL, _render, role_vars  # noqa: F401 (fixture re-export)

_GARAGE_ALERTS = {
    "GarageStorageNodeDown": "warning",
    "GarageQuorumLost": "critical",
    "GarageUnderReplicated": "warning",
    "GarageDiskAlmostFull": "warning",
    "GarageResyncErrors": "warning",
    "GarageS3ServerErrors": "warning",
}


def _garage_group(role_vars: dict) -> dict:
    doc = yaml.safe_load(_render(_RULES_TMPL, role_vars))
    groups = [g for g in doc["groups"] if g["name"] == "platform-garage"]
    assert len(groups) == 1, "exactly one platform-garage group"
    return groups[0]


def test_garage_alerts_exist_with_severities(role_vars):
    rules = {r["alert"]: r for r in _garage_group(role_vars)["rules"]}
    assert {a: rules[a]["labels"]["severity"] for a in _GARAGE_ALERTS} == _GARAGE_ALERTS
    for r in rules.values():
        assert r["labels"]["service"] == "garage"
        assert 'service="garage"' in r["expr"], f"{r['alert']} must select the garage scrape job's series"
        assert "node_" not in r["expr"], "Garage exports its own disk metrics; no node_exporter on its guests"


def test_s3_error_alert_counts_only_server_errors(role_vars):
    rules = {r["alert"]: r for r in _garage_group(role_vars)["rules"]}
    assert 'status_code=~"5.."' in rules["GarageS3ServerErrors"]["expr"]


# --------------------------------------------------------------------------- #
# promtool test rules
# --------------------------------------------------------------------------- #

_UNIT_TESTS = textwrap.dedent("""
    rule_files:
      - baseline.rules.yml
    evaluation_interval: 1m
    tests:
      # Healthy three-node cluster: no Garage alert.
      - interval: 1m
        input_series:
          - series: 'cluster_storage_nodes{service="garage",instance="n1"}'
            values: '3x60'
          - series: 'cluster_storage_nodes_ok{service="garage",instance="n1"}'
            values: '3x60'
          - series: 'cluster_partitions{service="garage",instance="n1"}'
            values: '256x60'
          - series: 'cluster_partitions_quorum{service="garage",instance="n1"}'
            values: '256x60'
          - series: 'cluster_partitions_all_ok{service="garage",instance="n1"}'
            values: '256x60'
          - series: 'garage_local_disk_avail{service="garage",instance="n1",volume="data"}'
            values: '900x60'
          - series: 'garage_local_disk_total{service="garage",instance="n1",volume="data"}'
            values: '1000x60'
          - series: 'block_resync_errored_blocks{service="garage",instance="n1"}'
            values: '0x60'
          - series: 'api_s3_request_counter{service="garage",instance="n1",api_endpoint="GetObject"}'
            values: '0+100x60'
          - series: 'api_s3_error_counter{service="garage",instance="n1",api_endpoint="GetObject",status_code="403"}'
            values: '0+50x60'
        alert_rule_test:
          - {eval_time: 59m, alertname: GarageStorageNodeDown, exp_alerts: []}
          - {eval_time: 59m, alertname: GarageQuorumLost, exp_alerts: []}
          - {eval_time: 59m, alertname: GarageUnderReplicated, exp_alerts: []}
          - {eval_time: 59m, alertname: GarageDiskAlmostFull, exp_alerts: []}
          - {eval_time: 59m, alertname: GarageResyncErrors, exp_alerts: []}
          - {eval_time: 59m, alertname: GarageS3ServerErrors, exp_alerts: []}

      # One node down: node-down after 5m, under-replicated after 30m, quorum kept.
      - interval: 1m
        input_series:
          - series: 'cluster_storage_nodes{service="garage",instance="n1"}'
            values: '3x60'
          - series: 'cluster_storage_nodes_ok{service="garage",instance="n1"}'
            values: '2x60'
          - series: 'cluster_partitions{service="garage",instance="n1"}'
            values: '256x60'
          - series: 'cluster_partitions_quorum{service="garage",instance="n1"}'
            values: '256x60'
          - series: 'cluster_partitions_all_ok{service="garage",instance="n1"}'
            values: '0x60'
        alert_rule_test:
          - eval_time: 3m
            alertname: GarageStorageNodeDown
            exp_alerts: []
          - eval_time: 10m
            alertname: GarageStorageNodeDown
            exp_alerts:
              - exp_labels: {severity: warning, service: garage}
                exp_annotations:
                  summary: "Garage storage node(s) not connected"
                  description: "Only 2 of the Garage storage nodes are connected. Reads and writes continue while a quorum remains; check the node container and RPC port 3901."
          - eval_time: 10m
            alertname: GarageUnderReplicated
            exp_alerts: []
          - eval_time: 40m
            alertname: GarageUnderReplicated
            exp_alerts:
              - exp_labels: {severity: warning, service: garage}
                exp_annotations:
                  summary: "Garage data under-replicated for 30 minutes"
                  description: "Only 0 Garage partitions have all their copies reachable. Restore the missing node; if it is gone for good, replace it in the layout."
          - eval_time: 40m
            alertname: GarageQuorumLost
            exp_alerts: []

      # Two nodes down: quorum lost after 2m.
      - interval: 1m
        input_series:
          - series: 'cluster_partitions{service="garage",instance="n1"}'
            values: '256x10'
          - series: 'cluster_partitions_quorum{service="garage",instance="n1"}'
            values: '0x10'
        alert_rule_test:
          - eval_time: 5m
            alertname: GarageQuorumLost
            exp_alerts:
              - exp_labels: {severity: critical, service: garage}
                exp_annotations:
                  summary: "Garage partitions without write quorum"
                  description: "0 Garage partitions have a write quorum (fewer than all): writes to some objects fail. At least two storage nodes are unreachable."

      # Disk 85 % full on n2 only; resync errors on n3; 10 % 5xx.
      - interval: 1m
        input_series:
          - series: 'garage_local_disk_avail{service="garage",instance="n2",volume="data"}'
            values: '150x60'
          - series: 'garage_local_disk_total{service="garage",instance="n2",volume="data"}'
            values: '1000x60'
          - series: 'block_resync_errored_blocks{service="garage",instance="n3"}'
            values: '4x60'
          - series: 'api_s3_request_counter{service="garage",instance="n1",api_endpoint="PutObject"}'
            values: '0+100x60'
          - series: 'api_s3_error_counter{service="garage",instance="n1",api_endpoint="PutObject",status_code="500"}'
            values: '0+10x60'
        alert_rule_test:
          - eval_time: 20m
            alertname: GarageDiskAlmostFull
            exp_alerts:
              - exp_labels: {severity: warning, service: garage, instance: n2, volume: data}
                exp_annotations:
                  summary: "Garage data volume over 80 % on n2"
                  description: "The Garage data volume on n2 is more than 80 % full. Grow garage_data_disk_gb and update the node capacity, or add a node."
          - eval_time: 40m
            alertname: GarageResyncErrors
            exp_alerts:
              - exp_labels: {severity: warning, service: garage, instance: n3}
                exp_annotations:
                  summary: "Garage block resync errors on n3"
                  description: "4 blocks on n3 failed their last resync for 30 minutes."
          - eval_time: 30m
            alertname: GarageS3ServerErrors
            exp_alerts:
              - exp_labels: {severity: warning, service: garage}
                exp_annotations:
                  summary: "Garage S3 API returning server errors"
                  description: "More than 5 % of Garage S3 requests returned a 5xx status over 15 minutes."
""")


def _promtool_test(rules: str, tests: str, prom_image: str) -> subprocess.CompletedProcess | None:
    td = Path(tempfile.mkdtemp(prefix="garage_rules_"))
    try:
        (td / "baseline.rules.yml").write_text(rules, encoding="utf-8")
        (td / "tests.yml").write_text(tests, encoding="utf-8")
        # mkdtemp is 0700; the prom/prometheus image runs as `nobody` and must read the mount.
        td.chmod(0o755)
        for f in td.iterdir():
            f.chmod(0o644)
        local = shutil.which("promtool")
        if local:
            return subprocess.run([local, "test", "rules", "tests.yml"], cwd=td, capture_output=True, text=True)
        if shutil.which("docker"):
            proc = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "promtool", "-v", f"{td}:/w:ro", "-w", "/w",
                 prom_image, "test", "rules", "tests.yml"],
                capture_output=True, text=True,
            )
            low = (proc.stdout + proc.stderr).lower()
            if proc.returncode != 0 and any(m in low for m in (
                    "cannot connect to the docker daemon", "permission denied", "pull access denied",
                    "unable to find image", "no such host", "network is unreachable", "timeout")):
                return None
            return proc
        return None
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_promtool_unit_tests_for_the_garage_alerts(role_vars):
    proc = _promtool_test(_render(_RULES_TMPL, role_vars), _UNIT_TESTS, role_vars["observability_prometheus_image"])
    if proc is None:
        pytest.skip("promtool unavailable (no local binary and no usable Docker image offline)")
    assert proc.returncode == 0, f"promtool test rules failed:\n{proc.stdout}\n{proc.stderr}"
