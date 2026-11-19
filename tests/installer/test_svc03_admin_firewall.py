"""SVC-03 admin-API firewall (spec svc-03-admin-api-firewall, WS-C53): offline structure.

Renders the firewall script and unit and checks the rule shape (D1), idempotency (R5), persistence ordering (R3),
the installer's verify step (R4), the opt-out path (R6) and that the health stage runs on the guests (R7).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _ROOT / "ansible" / "roles" / "garage_install"


def _render(name: str, **v) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=False)
    return env.from_string((_ROLE / "templates" / name).read_text()).render(**v)


def _script(sources=("10.0.20.18",)) -> str:
    return _render("garage-admin-firewall.sh.j2", garage_admin_port=3903, garage_admin_allowed_sources=list(sources))


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks" / name).read_text())


def test_defaults_allow_only_the_prometheus_guest():
    d = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert d["garage_admin_firewall_enabled"] is True
    assert d["garage_admin_allowed_sources"] == ["10.0.20.18"]
    ledger = yaml.safe_load((_ROOT / "infra" / "platform-foundation" / "host-indices.yaml").read_text())
    prom = next(e for e in ledger[20] if e.get("svc_code") == "svc17" and e.get("component") == "prometheus")
    assert f"10.0.20.{10 + prom['host_index']}" == "10.0.20.18", "allow list matches the ledger"


def test_rule_shape_matches_original_destination_port_and_direction():
    s = _script(("10.0.20.18", "10.0.20.99"))
    assert 'JUMP="-p tcp -m conntrack --ctorigdstport 3903 --ctdir ORIGINAL -j $CHAIN"' in s
    assert s.index("-s 10.0.20.18 -j RETURN") < s.index('-A "$CHAIN" -j DROP')
    assert "-s 10.0.20.99 -j RETURN" in s
    assert "iptables -C DOCKER-USER $JUMP 2>/dev/null || iptables -I DOCKER-USER 1 $JUMP" in s, "R5: no duplicates"
    assert 'iptables -F "$CHAIN"' in s, "the allow list is rebuilt, not appended to"
    assert "INPUT" not in s, "DNAT traffic never reaches INPUT"


def test_script_is_valid_posix_shell():
    p = _ROOT / ".pytest_garage_fw.sh"
    try:
        p.write_text(_script())
        assert subprocess.run(["sh", "-n", str(p)], capture_output=True).returncode == 0
        if shutil.which("shellcheck"):
            r = subprocess.run(["shellcheck", "-s", "sh", str(p)], capture_output=True, text=True)
            assert r.returncode == 0, r.stdout
    finally:
        p.unlink(missing_ok=True)


def test_unit_runs_after_docker_and_with_its_restarts_and_never_opens_the_port_on_stop():
    u = _render("garage-admin-firewall.service.j2", garage_admin_port=3903)
    for line in ("After=docker.service", "Requires=docker.service", "PartOf=docker.service",
                 "Type=oneshot", "RemainAfterExit=yes", "ExecStart=/usr/local/sbin/garage-admin-firewall apply"):
        assert line in u
    assert "ExecStop=" not in u.replace("# No ExecStop", ""), "a Docker restart must not remove the rules"
    assert "WantedBy=multi-user.target docker.service" in u


def test_installer_verifies_the_rule_and_has_an_opt_out():
    tasks = _tasks("admin_firewall.yml")
    verify = next(t for t in tasks if t["name"].startswith("Firewall | VERIFY"))
    argv = verify["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["iptables", "-C", "DOCKER-USER"] and argv[-1] == "GARAGE-ADMIN"
    assert any(t["name"].startswith("Firewall | Fail closed") for t in tasks)
    opt_out = [t for t in tasks if "opt-out" in t["name"]]
    assert len(opt_out) == 4
    assert all("not (garage_admin_firewall_enabled | bool)" in str(t["when"]) for t in opt_out)
    main = _tasks("main.yml")
    names = [t["name"] for t in main]
    assert names.index("Garage | Restrict the admin API to the allowed sources (WS-C53)") > names.index(
        "Garage | Deploy the Compose stack (docker-compose-app)")


def test_health_stage_one_runs_on_the_guest():
    play = yaml.safe_load((_ROOT / "ansible" / "playbooks" / "garage-health.yml").read_text())[0]
    stage1 = play["tasks"][0]
    assert stage1["name"].startswith("Health | Stage 1")
    assert "delegate_to" not in stage1, "the control node is no longer allowed to reach 3903"


@pytest.mark.parametrize("arg", ["apply", "remove"])
def test_script_offers_apply_and_remove(arg):
    assert f"  {arg}) {arg} ;;" in _script()
