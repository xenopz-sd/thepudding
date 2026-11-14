"""Bug-condition + preservation tests: Grafana never starts with the default admin password (WS-C37).

THE BUG (found live on throwaway `shrimp`, WS-C37): the documented one-command install
(`svc-17-18-bootstrap.yml` without `-e obs_openbao_addr=...`) ran with OpenBao UNWIRED and no override,
so the role rendered `GF_SECURITY_ADMIN_PASSWORD=` (empty) and Grafana fell back to its built-in default:
`admin`/`admin` answered 200 on the API. The fail-fast only fired on the WIRED path.
THE FIX: (1) refuse to render the secret env whenever the resolved password is empty, wired or not;
(2) the orchestrator wires OpenBao by default (the SVC-07 primary, like SVC-06), so the documented command
seeds and reads a real password; dev/offline sets `obs_openbao_addr=""` plus an explicit override.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_GRAFANA = _REPO / "ansible/roles/observability_stack/tasks/grafana.yml"
_PLAY = _REPO / "ansible/playbooks/svc-17-18-bootstrap.yml"
_BIND = "Observability (grafana) | Bind the Break_Glass_Admin password fact (no_log)"
_GUARD = "Observability (grafana) | Refuse an empty Break_Glass_Admin password (never the Grafana default)"
_RENDER_FRAGMENT = "grafana.secret.env"


def _tasks():
    return [t for t in yaml.safe_load(_GRAFANA.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _by(name):
    hits = [t for t in _tasks() if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


def test_BUG_an_empty_password_guard_exists_between_bind_and_render():
    tasks = _tasks()
    names = [t.get("name") for t in tasks]
    render = next(i for i, t in enumerate(tasks) if _RENDER_FRAGMENT in str(t.get("ansible.builtin.template", {}).get("src", "")))
    assert names.index(_BIND) < names.index(_GUARD) < render


def test_the_guard_is_unconditional_on_wiring():
    g = _by(_GUARD)
    assert "when" not in g, "it must also fire on the unwired dev/offline path"


def _guard(password):
    a = _by(_GUARD)["ansible.builtin.assert"]
    that = a["that"] if isinstance(a["that"], list) else [a["that"]]
    return all(_tpl("{{ " + c + " }}", {"observability_grafana_admin_password": password}) in (True, "True") for c in that)


def test_guard_truth_table():
    assert _guard("") is False
    assert _guard("   ") is False
    assert _guard("admin") is False, "the Grafana built-in default is refused too"
    assert _guard("a-real-random-password-1234") is True


def test_the_guard_never_prints_the_value_and_names_the_remedy():
    g = _by(_GUARD)
    assert g.get("no_log") is True
    msg = g["ansible.builtin.assert"]["fail_msg"]
    assert "observability_grafana_admin_password }}" not in msg
    assert "obs_openbao_addr" in msg and "observability_grafana_admin_password_override" in msg


def test_BUG_the_orchestrator_wires_openbao_by_default():
    plays = yaml.safe_load(_PLAY.read_text(encoding="utf-8"))
    v = plays[0]["vars"]
    assert v["obs_openbao_addr"] == "10.0.20.10"
    assert v["obs_openbao_cli_host"] == "10.0.20.10"


# --------------------------------------------------------------------------- #
# WS-C37 part 2 (found live): Prometheus could never deliver an alert to Alertmanager
# --------------------------------------------------------------------------- #
_OBS_DEFAULTS = _REPO / "ansible/roles/observability_stack/defaults/main.yml"
_PROM_TASKS = _REPO / "ansible/roles/observability_stack/tasks/prometheus.yml"


def test_BUG_prometheus_targets_alertmanager_by_its_compose_service_name_not_localhost():
    """Prometheus and Alertmanager are separate containers on one Compose network: `localhost` inside the
    Prometheus container is Prometheus itself, so every notification failed with connection refused
    (live: `prometheus_notifications_errors_total` 7/7, Alertmanager empty while InstanceDown was firing)."""
    d = yaml.safe_load(_OBS_DEFAULTS.read_text(encoding="utf-8"))
    host = d["observability_alertmanager_scrape_host"]
    assert host not in ("localhost", "127.0.0.1", "::1")
    services = [s["name"] for s in next(t for t in yaml.safe_load(_PROM_TASKS.read_text(encoding="utf-8"))
                                        if isinstance(t, dict) and "Deploy the Compose stack" in str(t.get("name")))
                ["vars"]["dca_services"]]
    assert host in services, "the target must be the Alertmanager Compose service name"
