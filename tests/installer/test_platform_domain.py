"""PLATFORM_DOMAIN resolution (SVC-10 design D5, spec .kiro/specs/svc-10-internal-dns/).

One cluster env key ``PLATFORM_DOMAIN`` becomes the default platform domain of
every installer. ``cluster_env.resolve_platform_domain`` is the pure resolver;
``cluster_env.py --platform-domain`` is the CLI the shared Ansible task file
``platform_domain`` role calls. Every bootstrap that owns a
platform-domain variable must import that role first, and an explicit ``-e`` still
wins (a ``set_fact`` never beats extra vars).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "installer"))

import cluster_env  # noqa: E402
from cluster_env import ValidationError, parse_env, resolve_platform_domain  # noqa: E402

HELPER = REPO / "scripts" / "installer" / "cluster_env.py"
TASK_FILE = REPO / "ansible" / "roles" / "platform_domain" / "tasks" / "main.yml"

# Bootstrap playbook -> the variable the shared task must set in it.
BOOTSTRAPS = {
    "svc-03-bootstrap.yml": "svc03_platform_domain",
    "svc-06-bootstrap.yml": "svc06_platform_domain",
    "svc-07-bootstrap.yml": "platform_domain",
    "svc-09-bootstrap.yml": "traefik_platform_domain",
    "svc-10-bootstrap.yml": "svc10_platform_domain",
    "svc-17-18-bootstrap.yml": "observability_platform_domain",
    "mailcatcher-bootstrap.yml": "mailcatcher_platform_domain",
    "project-consumer-bootstrap.yml": "consumer_platform_domain",
}


def test_absent_returns_none():
    assert resolve_platform_domain(parse_env("PROXMOX_ENDPOINT=x\n")) is None
    assert resolve_platform_domain(parse_env("PLATFORM_DOMAIN=   \n")) is None


@pytest.mark.parametrize("value", ["lab.example.test", "a.b-c.example.org", "Lab.Example.Test"])
def test_valid_domain_lowercased(value):
    assert resolve_platform_domain(parse_env(f"PLATFORM_DOMAIN={value}\n")) == value.lower()


def test_trailing_dot_stripped():
    assert resolve_platform_domain({"PLATFORM_DOMAIN": "lab.example.test."}) == "lab.example.test"


@pytest.mark.parametrize(
    "value",
    ["localhost", "-bad.example.test", "bad-.example.test", "a..b", "a b.test", "x_y.test",
     "https://lab.example.test", "lab.example.123", ("a" * 64) + ".test"],
)
def test_invalid_domain_rejected_by_name_only(value):
    with pytest.raises(ValidationError) as exc:
        resolve_platform_domain({"PLATFORM_DOMAIN": value})
    assert "PLATFORM_DOMAIN" in str(exc.value)


def test_full_resolve_contract_unchanged():
    """PLATFORM_DOMAIN is not a SCHEMA row: the TF_VAR mapping does not grow."""
    assert all(v.proxmox_alias != "PLATFORM_DOMAIN" for v in cluster_env.SCHEMA)


def _cli(tmp_path, text):
    env = tmp_path / "c.env"
    env.write_text(text)
    return subprocess.run(
        [sys.executable, str(HELPER), "--env-file", str(env), "--platform-domain"],
        capture_output=True, text=True, check=False,
    )


def test_cli_prints_domain_without_mandatory_validation(tmp_path):
    out = _cli(tmp_path, "PLATFORM_DOMAIN=lab.example.test\n")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "lab.example.test"


def test_cli_absent_prints_empty(tmp_path):
    out = _cli(tmp_path, "PROXMOX_API_TOKEN=secret-value\n")
    assert out.returncode == 0
    assert out.stdout.strip() == ""
    assert "secret-value" not in out.stdout + out.stderr


def test_cli_invalid_exit_3(tmp_path):
    out = _cli(tmp_path, "PLATFORM_DOMAIN=not a domain\n")
    assert out.returncode == 3
    assert "PLATFORM_DOMAIN" in out.stderr


def test_cli_missing_file_exit_2(tmp_path):
    out = subprocess.run(
        [sys.executable, str(HELPER), "--env-file", str(tmp_path / "nope"), "--platform-domain"],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 2


def test_task_file_sets_named_var_via_set_fact():
    tasks = yaml.safe_load(TASK_FILE.read_text())
    text = TASK_FILE.read_text()
    assert "--platform-domain" in text
    setters = [t for t in tasks if "ansible.builtin.set_fact" in t]
    assert setters, "the task file must set the target variable with set_fact"
    assert any("platform_domain_var" in str(t["ansible.builtin.set_fact"]) for t in setters)
    # A failing helper (invalid value) must stop the run; a missing file must not
    # (the service preflight reports that with its own remedy).
    assert "failed_when: platform_domain_lookup.rc not in [0, 2]" in text


@pytest.mark.parametrize("playbook,var", sorted(BOOTSTRAPS.items()))
def test_every_bootstrap_imports_the_task_first(playbook, var):
    plays = yaml.safe_load((REPO / "ansible" / "playbooks" / playbook).read_text())
    tasks = plays[0]["tasks"]
    first = tasks[0]
    assert first.get("ansible.builtin.import_role") == {"name": "platform_domain"}, playbook
    assert first.get("vars", {}).get("platform_domain_var") == var, playbook


def test_svc06_health_gate_receives_the_platform_domain():
    """Bug condition (found live on a real PLATFORM_DOMAIN): svc06-health.yml sends
    `Host: auth.<zitadel_platform_domain>` but the handoff never passed the domain, so
    the probe used the placeholder and ZITADEL rejected it. The handoff must bridge it."""
    tasks = yaml.safe_load((REPO / "ansible" / "roles" / "svc06_handoff" / "tasks" / "main.yml").read_text())
    run = next(t for t in tasks if "svc06-health.yml" in str(t.get("ansible.builtin.command", {}).get("argv", [])))
    argv = run["ansible.builtin.command"]["argv"]
    assert "zitadel_platform_domain={{ svc06_platform_domain }}" in argv
