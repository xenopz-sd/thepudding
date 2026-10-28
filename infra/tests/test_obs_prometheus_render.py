"""Offline config-render tests for the SVC-17 Prometheus / Alertmanager / rules.

Task 4.5 (spec: svc-17-18-observability) — requirements.md FR-1, FR-7, FR-8,
FR-12; design.md §3.2 (observability_stack role) and §Testing Strategy
("test_obs_prometheus_render.py — file_sd only, no inline static targets, rules
promtool-clean, deferred rules conditional"). OFFLINE unit / config-validation
tests for the CONFIGURATION layer of SVC-17 — the three artifacts the
``observability_stack`` role templates for the Prometheus guest:

    templates/prometheus.yml.j2
    templates/alertmanager.yml.j2
    templates/rules/baseline.rules.yml.j2

rendered with the role's ``defaults/main.yml`` variable set.

=== HOW THE OFFLINE RENDER WORKS (Ansible-specific jinja filters) ===

Mirrors the sibling ``test_svc09_traefik_config_render.py`` /
``test_svc07_openbao_config_render.py`` render harness: load the role
``defaults/main.yml`` with PyYAML, resolve the shallow jinja self-references
(e.g. ``observability_prometheus_targets_dir: "{{ ... }}/targets"``) with a
fixed-point pass, then render each template with a stock ``jinja2.Environment``.

The templates use a handful of Ansible-specific jinja filters that a stock
jinja2 Environment does not ship, so the harness registers faithful stand-ins:
  * ``to_json``  — alertmanager.yml.j2 uses ``proj.group_by | to_json`` for the
                   per-route ``group_by`` list; registered as ``json.dumps``
                   (what Ansible's ``to_json`` does).
  * ``bool``     — registered for parity with the sibling harnesses (truthy
                   strings → True), though these three templates gate on real
                   booleans from defaults.
  * ``lower`` / ``basename`` — registered for parity with the sibling harness.
Jinja2's own built-ins cover the rest the templates use (``default``, ``trim``,
``string``, ``int``, ``lower`` as a method). Registering these makes a faithful
offline render match Ansible's runtime render.

=== WHAT IS ASSERTED (task 4.5 contract) ===

prometheus.yml (FR-8 / FR-12):
  * ``scrape_configs`` use ONLY ``file_sd_configs`` for the monitored services —
    the sole inline ``static_configs`` is Prometheus's own :9090 self-scrape
    (the sanctioned FR-8 exception, FR-12 self-monitoring).
  * every ``file_sd_configs`` file glob lives under
    ``observability_prometheus_targets_dir``.

baseline.rules.yml (FR-7):
  * structurally valid YAML with the baseline alert groups.
  * if ``promtool`` is available (local binary OR the pinned ``prom/prometheus``
    Docker image), ``promtool check rules`` is clean; otherwise that single
    assertion is skipped with an explicit reason (never a failure).
  * the DEFERRED rules (GPU ``GpuEccErrors`` via ``dcgm_metrics_deferred``, and
    SVC-08 cert-expiry ``CertificateExpiringSoon`` via
    ``observability_cert_expiry_alert_enabled``) are ABSENT at the default
    posture and PRESENT when their flag is flipped (conditional render).

alertmanager.yml (FR-7):
  * a top-level ``route`` with a default receiver, and that default receiver is
    defined in ``receivers``.
  * per-project receivers render from ``observability_project_receivers`` — both
    the routing child routes and the receiver definitions.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_obs_prometheus_render.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

# --------------------------------------------------------------------------- #
# Locate the observability_stack role relative to this test file.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "observability_stack"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_TEMPLATES = _ROLE_DIR / "templates"
_PROM_TMPL = _TEMPLATES / "prometheus.yml.j2"
_ALERTMANAGER_TMPL = _TEMPLATES / "alertmanager.yml.j2"
_RULES_TMPL = _TEMPLATES / "rules" / "baseline.rules.yml.j2"

#: Pinned Prometheus image key in defaults/main.yml — used to run `promtool
#: check rules` via Docker when no local promtool binary exists.
_PROM_IMAGE_VAR = "observability_prometheus_image"


# --------------------------------------------------------------------------- #
# Load + resolve the role defaults (shallow jinja self-references), incl. lists.
# --------------------------------------------------------------------------- #
def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    # Ansible's to_json == json.dumps of the value (alertmanager group_by list).
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


def _render(template_path: Path, variables: dict) -> str:
    assert template_path.is_file(), f"expected template at {template_path}"
    env = _make_env()
    tmpl = env.from_string(template_path.read_text())
    return tmpl.render(**variables)


# --------------------------------------------------------------------------- #
# prometheus.yml — file_sd only (FR-8), self-scrape sole inline static (FR-12).
# --------------------------------------------------------------------------- #
class TestPrometheusConfigRender:
    @pytest.fixture(scope="class")
    def prom_doc(self, role_vars) -> dict:
        doc = yaml.safe_load(_render(_PROM_TMPL, role_vars))
        assert isinstance(doc, dict), "rendered prometheus.yml must parse as a YAML mapping"
        return doc

    def test_has_scrape_configs(self, prom_doc):
        assert prom_doc.get("scrape_configs"), "prometheus.yml must declare scrape_configs"

    def test_only_self_scrape_is_inline_static(self, prom_doc):
        """FR-8: the ONLY job allowed an inline static_configs is `prometheus`.

        Every other scrape job must discover targets via file_sd_configs — no
        inline static target list for a monitored service.
        """
        static_jobs = [
            job["job_name"]
            for job in prom_doc["scrape_configs"]
            if job.get("static_configs")
        ]
        assert static_jobs == ["prometheus"], (
            "the Prometheus self-scrape (:9090) is the ONLY sanctioned inline "
            f"static_configs target (FR-8/FR-12); found inline static jobs: {static_jobs}"
        )

    def test_self_scrape_targets_localhost_9090(self, prom_doc):
        self_job = next(
            j for j in prom_doc["scrape_configs"] if j["job_name"] == "prometheus"
        )
        targets = self_job["static_configs"][0]["targets"]
        assert any(str(t).endswith(":9090") for t in targets), (
            "the self-scrape static target must be localhost:9090 (FR-12)"
        )

    def test_every_other_job_uses_file_sd(self, prom_doc):
        non_self = [j for j in prom_doc["scrape_configs"] if j["job_name"] != "prometheus"]
        assert non_self, "expected file_sd scrape jobs beyond the self-scrape"
        for job in non_self:
            assert job.get("file_sd_configs"), (
                f"job {job['job_name']} must discover targets via file_sd_configs "
                "only — no inline static list (FR-8)"
            )
            assert not job.get("static_configs"), (
                f"job {job['job_name']} must not carry an inline static_configs (FR-8)"
            )

    def test_file_sd_paths_under_targets_dir(self, prom_doc, role_vars):
        """FR-8: every file_sd glob lives under observability_prometheus_targets_dir."""
        targets_dir = role_vars["observability_prometheus_targets_dir"]
        assert targets_dir, "observability_prometheus_targets_dir must be defined"
        file_jobs = [j for j in prom_doc["scrape_configs"] if j.get("file_sd_configs")]
        assert file_jobs, "expected at least one file_sd_configs job"
        for job in file_jobs:
            for fsd in job["file_sd_configs"]:
                for glob in fsd["files"]:
                    assert glob.startswith(targets_dir + "/"), (
                        f"file_sd glob {glob!r} for job {job['job_name']} must live "
                        f"under the targets dir {targets_dir!r} (FR-8)"
                    )


# --------------------------------------------------------------------------- #
# baseline.rules.yml — YAML-valid, promtool-clean (if available), deferred gating.
# --------------------------------------------------------------------------- #
def _promtool_local() -> str | None:
    return shutil.which("promtool")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _promtool_check_rules(rendered_rules: str, prom_image: str) -> tuple[bool, str]:
    """Run ``promtool check rules`` on the rendered file.

    Prefers a local ``promtool`` binary; falls back to the pinned
    ``prom/prometheus`` Docker image (whose entrypoint ships ``promtool``). If
    neither is available, returns ``(None-ish)`` via the ``skipped`` sentinel.

    Returns ``(ok, detail)`` where ``ok`` is True on a clean check. Raises
    ``_PromtoolUnavailable`` when no runner is available so the caller can skip.
    """
    with tempfile.TemporaryDirectory() as td:
        rules_path = Path(td) / "baseline.rules.yml"
        rules_path.write_text(rendered_rules, encoding="utf-8")

        local = _promtool_local()
        if local:
            proc = subprocess.run(
                [local, "check", "rules", str(rules_path)],
                capture_output=True, text=True,
            )
            return proc.returncode == 0, (proc.stdout + proc.stderr)

        if _docker_available():
            # The prom/prometheus image ships promtool; mount the rules file in.
            proc = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--entrypoint", "promtool",
                    "-v", f"{rules_path}:/tmp/baseline.rules.yml:ro",
                    prom_image,
                    "check", "rules", "/tmp/baseline.rules.yml",
                ],
                capture_output=True, text=True,
            )
            out = proc.stdout + proc.stderr
            # A pull/daemon failure is an environmental unavailability, not a
            # rules defect — surface it as unavailable so the caller skips.
            lowered = out.lower()
            env_markers = (
                "cannot connect to the docker daemon",
                "permission denied",
                "error response from daemon",
                "no such host",
                "failed to resolve",
                "unable to find image",
                "pull access denied",
                "network is unreachable",
                "timeout",
            )
            if proc.returncode != 0 and any(m in lowered for m in env_markers):
                raise _PromtoolUnavailable(
                    f"docker/promtool image unavailable offline: {out.strip()[-500:]}"
                )
            return proc.returncode == 0, out

        raise _PromtoolUnavailable("no local promtool binary and no docker available")


class _PromtoolUnavailable(Exception):
    """Raised when neither a local promtool nor a usable Docker runner exists."""


class TestBaselineRulesRender:
    def test_rules_parse_as_yaml(self, role_vars):
        doc = yaml.safe_load(_render(_RULES_TMPL, role_vars))
        assert isinstance(doc, dict) and "groups" in doc, (
            "baseline.rules.yml must parse as YAML with a top-level `groups` list"
        )
        group_names = {g["name"] for g in doc["groups"]}
        assert "platform-baseline" in group_names, "expected the platform-baseline group"
        assert "observability-self" in group_names, "expected the observability-self group"

    def test_rules_promtool_clean_or_skipped(self, role_vars):
        """FR-7: baseline rules are promtool-clean when promtool is available.

        Falls back to the pinned prom/prometheus Docker image; if no runner is
        available the promtool assertion is SKIPPED with an explicit reason and
        the YAML-parse validity (test above) is relied on instead.
        """
        rendered = _render(_RULES_TMPL, role_vars)
        prom_image = role_vars[_PROM_IMAGE_VAR]
        try:
            ok, detail = _promtool_check_rules(rendered, prom_image)
        except _PromtoolUnavailable as exc:
            pytest.skip(
                "promtool unavailable (no local binary and no usable Docker image "
                f"offline) — relying on YAML-parse validity. Reason: {exc}"
            )
            return
        assert ok, f"`promtool check rules` reported errors on the baseline rules:\n{detail}"

    def test_deferred_rules_absent_at_default_posture(self, role_vars):
        """Default: dcgm_metrics_deferred=true + cert flag off → deferred rules absent."""
        assert role_vars.get("dcgm_metrics_deferred") in (True, "true", "True"), (
            "defaults must keep dcgm_metrics_deferred true (GPU deferred)"
        )
        assert _ansible_bool(role_vars.get("observability_cert_expiry_alert_enabled")) is False, (
            "defaults must keep observability_cert_expiry_alert_enabled false"
        )
        doc = yaml.safe_load(_render(_RULES_TMPL, role_vars))
        group_names = {g["name"] for g in doc["groups"]}
        assert "platform-gpu" not in group_names, (
            "the GPU rule group must be ABSENT while dcgm_metrics_deferred is true (FR-4/AC-12)"
        )
        assert "platform-cert-expiry" not in group_names, (
            "the cert-expiry rule group must be ABSENT while the cert flag is off (FR-7 Rec-3)"
        )
        all_alerts = _alert_names(doc)
        assert "GpuEccErrors" not in all_alerts
        assert "CertificateExpiringSoon" not in all_alerts

    def test_gpu_rules_present_when_dcgm_enabled(self, role_vars):
        v = dict(role_vars)
        v["dcgm_metrics_deferred"] = False
        doc = yaml.safe_load(_render(_RULES_TMPL, v))
        group_names = {g["name"] for g in doc["groups"]}
        assert "platform-gpu" in group_names, (
            "flipping dcgm_metrics_deferred=false must RENDER the GPU rule group (conditional render)"
        )
        assert "GpuEccErrors" in _alert_names(doc)

    def test_cert_expiry_rule_present_when_flag_on(self, role_vars):
        v = dict(role_vars)
        v["observability_cert_expiry_alert_enabled"] = True
        doc = yaml.safe_load(_render(_RULES_TMPL, v))
        group_names = {g["name"] for g in doc["groups"]}
        assert "platform-cert-expiry" in group_names, (
            "flipping observability_cert_expiry_alert_enabled=true must RENDER the "
            "cert-expiry rule group (conditional render)"
        )
        assert "CertificateExpiringSoon" in _alert_names(doc)


def _alert_names(rules_doc: dict) -> set[str]:
    names: set[str] = set()
    for group in rules_doc.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" in rule:
                names.add(rule["alert"])
    return names


# --------------------------------------------------------------------------- #
# alertmanager.yml — default receiver + per-project receivers (FR-7).
# --------------------------------------------------------------------------- #
class TestAlertmanagerConfigRender:
    def test_default_receiver_present_and_defined(self, role_vars):
        doc = yaml.safe_load(_render(_ALERTMANAGER_TMPL, role_vars))
        assert isinstance(doc, dict), "alertmanager.yml must parse as a YAML mapping"
        default_recv = doc["route"]["receiver"]
        assert default_recv, "the top-level route must name a default receiver (FR-7)"
        defined = {r["name"] for r in doc["receivers"]}
        assert default_recv in defined, (
            f"the default receiver {default_recv!r} must be defined in `receivers` (FR-7)"
        )

    def test_per_project_receivers_rendered(self, role_vars):
        """FR-7: per-project receivers render from observability_project_receivers."""
        v = dict(role_vars)
        v["observability_project_receivers"] = [
            {
                "name": "mlvideo-webhook",
                "project": "mlvideo",
                "webhook_url": "http://127.0.0.1:9099/mlvideo",
                "group_by": ["alertname", "severity"],
            },
            {
                "name": "dronefleet-webhook",
                "project": "dronefleet",
            },
        ]
        doc = yaml.safe_load(_render(_ALERTMANAGER_TMPL, v))

        # Routing child routes matched on the project label.
        child_routes = doc["route"].get("routes", []) or []
        routed_receivers = {r["receiver"] for r in child_routes}
        assert {"mlvideo-webhook", "dronefleet-webhook"} <= routed_receivers, (
            "each per-project receiver must get a child route (FR-7); "
            f"got {routed_receivers}"
        )

        # Receiver definitions include the defaults + both per-project receivers.
        defined = {r["name"] for r in doc["receivers"]}
        assert {"mlvideo-webhook", "dronefleet-webhook"} <= defined, (
            "each per-project receiver must be defined in `receivers` (FR-7)"
        )

        # The explicit webhook URL is honoured; the omitted one gets a placeholder.
        by_name = {r["name"]: r for r in doc["receivers"] if r["name"] != role_vars["observability_alertmanager_default_receiver"]}
        ml = by_name["mlvideo-webhook"]
        assert ml["webhook_configs"][0]["url"] == "http://127.0.0.1:9099/mlvideo"
        drone = by_name["dronefleet-webhook"]
        assert drone["webhook_configs"][0]["url"], (
            "a per-project receiver without an explicit webhook_url must still render "
            "a placeholder URL (never an empty/invalid receiver)"
        )

    def test_no_project_receivers_by_default(self, role_vars):
        """Default posture: empty observability_project_receivers → only default receiver."""
        assert role_vars.get("observability_project_receivers") == [], (
            "defaults must keep observability_project_receivers empty"
        )
        doc = yaml.safe_load(_render(_ALERTMANAGER_TMPL, role_vars))
        child_routes = doc["route"].get("routes") or []
        assert child_routes == [], "no child routes should render when no project receivers set"
        defined = {r["name"] for r in doc["receivers"]}
        assert defined == {role_vars["observability_alertmanager_default_receiver"]}, (
            "only the default receiver should be defined at the default posture"
        )
