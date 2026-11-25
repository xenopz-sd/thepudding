"""Offline tests for fix-consumer-expose-route-and-gateway-docs.

Spec: .kiro/specs/fix-consumer-expose-route-and-gateway-docs/ (bugfix.md 2.1-2.6, 3.1-3.6).

Three defects found by the 2026-11-24 live acceptance:

1. The manifest's ``expose`` block never reached the Traefik route: onboarding set the
   route facts in its own process, but the route is rendered by ``openbao-agent`` inside
   the separate ``hello-workload-deploy.yml`` run, which did not read the manifest.
2. The end-to-end route check ran in onboarding Phase 4 (before the workload exists) and
   probed ``/`` (which the reference workload does not serve).
3. The documented manual gateway command fed ``registry_status.py --json`` keys the
   ``sdn_gateway`` role does not read, so the role saw an empty active set.

Structural tests over the role YAML plus a Jinja render of the route-host expression;
no cluster, no secret. Run:
  ~/venv/devinfra/bin/pytest tests/installer/test_consumer_expose_route_wiring.py -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "installer"))

import consumer_manifest as cm  # noqa: E402

_DEPLOY_TASKS = _REPO_ROOT / "ansible" / "roles" / "hello_workload_deploy" / "tasks" / "main.yml"
_DEPLOY_DEFAULTS = _REPO_ROOT / "ansible" / "roles" / "hello_workload_deploy" / "defaults" / "main.yml"
_PROVISION_TASKS = _REPO_ROOT / "ansible" / "roles" / "consumer_provision" / "tasks" / "main.yml"
_HANDOFF_TASKS = _REPO_ROOT / "ansible" / "roles" / "consumer_handoff" / "tasks" / "main.yml"
_SDN_DEFAULTS = _REPO_ROOT / "ansible" / "roles" / "sdn_gateway" / "defaults" / "main.yml"
_REGISTRY_SCRIPTS = _REPO_ROOT / "infra" / "platform-foundation" / "scripts"

_ROUTE_KEYS = (
    "openbao_agent_workload_host_rule",
    "openbao_agent_workload_http_port",
    "openbao_agent_traefik_route_enabled",
)


def _iter_tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _iter_tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always"):
            if key in node:
                yield from _iter_tasks(node[key])


def _tasks(path: Path) -> list[dict]:
    return list(_iter_tasks(yaml.safe_load(path.read_text(encoding="utf-8"))))


def _index(tasks: list[dict], pred) -> int:
    for i, t in enumerate(tasks):
        if pred(t):
            return i
    return -1


def _is_agent_include(t: dict) -> bool:
    inc = t.get("ansible.builtin.include_role") or t.get("include_role")
    return isinstance(inc, dict) and inc.get("name") == "openbao-agent"


def _route_fact_task(tasks: list[dict]) -> dict | None:
    for t in tasks:
        sf = t.get("ansible.builtin.set_fact")
        if isinstance(sf, dict) and "openbao_agent_workload_host_rule" in sf:
            return t
    return None


# --------------------------------------------------------------------------- #
# Defect 1 — the deploy step derives the route from the manifest's expose block.
# --------------------------------------------------------------------------- #
def test_deploy_step_sets_route_inputs_from_expose_before_the_agent_include():
    tasks = _tasks(_DEPLOY_TASKS)
    task = _route_fact_task(tasks)
    assert task is not None, "hello_workload_deploy must set openbao_agent_workload_host_rule from expose"
    sf = task["ansible.builtin.set_fact"]
    for key in _ROUTE_KEYS:
        assert key in sf, f"the expose route fact task must set {key}"
    assert "expose" in str(task.get("when", "")), "the route facts must be gated on expose.enabled"
    assert tasks.index(task) < _index(tasks, _is_agent_include), (
        "the route facts must be set BEFORE openbao-agent renders the route"
    )


def test_deploy_step_route_host_renders_expose_host_dot_platform_domain():
    task = _route_fact_task(_tasks(_DEPLOY_TASKS))
    assert task is not None
    sf = task["ansible.builtin.set_fact"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx = {
        "hello_workload_manifest": {"expose": {"enabled": True, "host": "moto", "port": 9000}},
        "hello_workload_platform_domain": "lab.example.test",
    }
    host = env.from_string(str(sf["openbao_agent_workload_host_rule"])).render(ctx).strip()
    port = env.from_string(str(sf["openbao_agent_workload_http_port"])).render(ctx).strip()
    assert host == "moto.lab.example.test"
    assert port == "9000"
    ctx["hello_workload_manifest"] = {"expose": {"enabled": True, "host": "moto"}}
    assert env.from_string(str(sf["openbao_agent_workload_http_port"])).render(ctx).strip() == "8080"


def test_deploy_step_without_expose_passes_no_route_overrides_to_the_agent():
    """Preservation 3.1: without expose the agent keeps its own route defaults."""
    tasks = _tasks(_DEPLOY_TASKS)
    include = tasks[_index(tasks, _is_agent_include)]
    inc_vars = include.get("vars", {}) or {}
    for key in _ROUTE_KEYS + ("openbao_agent_mock_edge_network",):
        assert key not in inc_vars, f"the agent include must not hard-code {key}"


def test_deploy_step_validates_the_manifest_on_the_control_node_fail_closed():
    tasks = _tasks(_DEPLOY_TASKS)
    checks = [
        t for t in tasks
        if "consumer_manifest.py" in str(t.get("ansible.builtin.command", ""))
        and "--check" in str(t.get("ansible.builtin.command", ""))
    ]
    assert checks, "the deploy step must validate the manifest with consumer_manifest.py --check"
    assert checks[0].get("delegate_to") == "localhost", "the manifest is validated on the control node"
    assert tasks.index(checks[0]) < tasks.index(_route_fact_task(tasks))


def test_deploy_step_resolves_platform_domain_from_the_cluster_env_file():
    tasks = _tasks(_DEPLOY_TASKS)
    lookups = [
        t for t in tasks
        if "cluster_env.py" in str(t.get("ansible.builtin.command", ""))
        and "--platform-domain" in str(t.get("ansible.builtin.command", ""))
    ]
    assert lookups, "the deploy step must read PLATFORM_DOMAIN via cluster_env.py --platform-domain"
    assert lookups[0].get("delegate_to") == "localhost"
    defaults = yaml.safe_load(_DEPLOY_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults["hello_workload_platform_domain"] == "platform.example.test"


# --------------------------------------------------------------------------- #
# Defect 2 — the route check lives in the deploy step and probes expose.health_path.
# --------------------------------------------------------------------------- #
def test_deploy_step_checks_the_route_after_deploy_against_the_health_path():
    tasks = _tasks(_DEPLOY_TASKS)
    probes = [t for t in tasks if "ansible.builtin.uri" in t]
    assert probes, "the deploy step must probe the exposed route through Traefik"
    probe = probes[0]
    uri = probe["ansible.builtin.uri"]
    assert "hello_workload_traefik_ingress_host" in str(uri["url"])
    assert "health_path" in str(uri["url"]), "the probe must request expose.health_path"
    assert "openbao_agent_workload_host_rule" in str(uri.get("headers", {}).get("Host", ""))
    assert "expose" in str(probe.get("when", ""))
    assert tasks.index(probe) > _index(tasks, _is_agent_include), "the probe must run after deploy"
    asserts = [
        t for t in tasks
        if "ansible.builtin.assert" in t and "hello_workload_route_probe" in str(t["ansible.builtin.assert"])
    ]
    assert asserts, "a fail-closed assert on the probe result is required"
    that = " ".join(map(str, asserts[0]["ansible.builtin.assert"]["that"]))
    for code in ("-1", "404", "502", "503", "504"):
        assert code in that, f"the route assert must treat {code} as failure"
    msg = str(asserts[0]["ansible.builtin.assert"].get("fail_msg", ""))
    for needle in ("openbao_agent_workload_host_rule", "health_path", "status"):
        assert needle in msg, f"the failure message must name the {needle}"


def test_onboarding_handoff_no_longer_probes_the_route():
    """Operator decision Q1: the deploy step is the only owner of the route check."""
    tasks = _tasks(_HANDOFF_TASKS)
    assert not [t for t in tasks if "ansible.builtin.uri" in t and "expose" in str(t)], (
        "consumer_handoff must not probe the exposed route"
    )
    assert "hello-workload-deploy.yml" in _HANDOFF_TASKS.read_text(encoding="utf-8"), (
        "the handoff must say the deploy step verifies the route"
    )


def test_onboarding_provision_carries_no_dead_route_facts():
    text = _PROVISION_TASKS.read_text(encoding="utf-8")
    for key in _ROUTE_KEYS:
        assert key not in text, f"consumer_provision must not set the unused {key}"


@pytest.mark.parametrize("path", ["/", "/cgi-bin/healthz", "/health?x=1"])
def test_expose_health_path_valid(path):
    m = {"slug": "moto", "expose": {"enabled": True, "host": "moto", "health_path": path}}
    assert cm.validate_manifest(m) is m


@pytest.mark.parametrize("path", ["healthz", "/a b", "", 7, "/" + "a" * 255])
def test_expose_health_path_invalid(path):
    m = {"slug": "moto", "expose": {"enabled": True, "host": "moto", "health_path": path}}
    with pytest.raises(cm.ManifestValidationError, match="expose.health_path"):
        cm.validate_manifest(m)


def test_expose_without_health_path_still_valid():
    """Preservation 3.4 + operator decision Q2 (optional, default /)."""
    m = {"slug": "moto", "expose": {"enabled": True, "host": "moto", "port": 8080}}
    assert cm.validate_manifest(m) is m


# --------------------------------------------------------------------------- #
# Defect 3 — documented gateway commands feed the role's own variable names.
# --------------------------------------------------------------------------- #
_GATEWAY_DOCS = (
    _REPO_ROOT / "docs" / "TESTING.md",
    _REPO_ROOT / "ansible" / "playbooks" / "sdn-gateway.yml",
    _REGISTRY_SCRIPTS / "registry_status.py",
)


def _emitter_commands(text: str) -> list[str]:
    """Each registry_status.py invocation, joined across backslash continuations."""
    lines = [re.sub(r"^\s*#\s?", "", ln) for ln in text.splitlines()]
    cmds = []
    for i, ln in enumerate(lines):
        if "registry_status.py" in ln and "--" in "".join(lines[i:i + 3]):
            cmd = ln
            j = i
            while cmd.rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                cmd = cmd.rstrip()[:-1] + " " + lines[j].strip()
            if "--out" in cmd:
                cmds.append(cmd)
    return cmds


def test_documented_gateway_emitter_commands_use_ansible_vars():
    found = 0
    for doc in _GATEWAY_DOCS:
        for cmd in _emitter_commands(doc.read_text(encoding="utf-8")):
            found += 1
            assert "--ansible-vars" in cmd, (
                f"{doc.relative_to(_REPO_ROOT)}: a documented registry_status.py command that "
                f"writes a vars file for sdn-gateway.yml must pass --ansible-vars: {cmd.strip()}"
            )
    assert found >= 5, f"expected the documented emitter commands (non-vacuous), found {found}"


def test_ansible_vars_keys_are_sdn_gateway_inputs(tmp_path):
    out = tmp_path / "vlan-status.json"
    proc = subprocess.run(
        [sys.executable, str(_REGISTRY_SCRIPTS / "registry_status.py"), "--json", "--ansible-vars",
         "--out", str(out)],
        cwd=_REPO_ROOT, env={**os.environ, "PYTHONPATH": str(_REGISTRY_SCRIPTS)},
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    keys = set(json.loads(out.read_text(encoding="utf-8")))
    role_inputs = set(yaml.safe_load(_SDN_DEFAULTS.read_text(encoding="utf-8")))
    assert keys, "the --ansible-vars payload must not be empty"
    assert keys <= role_inputs, f"keys not read by sdn_gateway: {sorted(keys - role_inputs)}"
