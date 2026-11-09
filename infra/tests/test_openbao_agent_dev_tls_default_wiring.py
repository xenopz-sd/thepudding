"""Bug-condition + preservation tests: openbao-agent dev-TLS default wiring (WS-C6).

Feature: fix-openbao-agent-dev-tls-default-wiring.
Spec: .kiro/specs/fix-openbao-agent-dev-tls-default-wiring/.

THE BUG
-------
The openbao-agent role renders `tls_skip_verify` from `openbao_primary_tls_skip_verify`
(default false = production VERIFIES, platform-runtime-patterns P4). The consumer
workload-deploy path never carried the dev self-signed posture, so against the dev
self-signed Primary the sidecar crash-looped on certificate verification unless the
operator remembered `-e openbao_tls_self_signed=true` on every run, with no useful
message.

THE FIX (A + B)
---------------
A. openbao-agent: a fail-CLOSED preflight probes the Primary with verification ON and
   stops with the remedy on a certificate-trust failure (reports only; never skips).
B. hello_workload_deploy: resolve the dev opt-in as explicit `-e` > the
   OPENBAO_TLS_SELF_SIGNED key in the cluster env file > false (verify), and thread it
   into the agent. Default stays verify; a prod env file omits the key.

These tests pin the wiring structurally and EVALUATE the real extracted expressions
with Ansible's own templater. Pure offline.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_AGENT_TASKS = _REPO / "ansible/roles/openbao-agent/tasks/main.yml"
_AGENT_DEFAULTS = _REPO / "ansible/roles/openbao-agent/defaults/main.yml"
_HELLO_TASKS = _REPO / "ansible/roles/hello_workload_deploy/tasks/main.yml"
_PLAYBOOK = _REPO / "ansible/playbooks/hello-workload-deploy.yml"

_PROBE = "4 — Probe the Primary TLS trust with verification ON (read-only, WS-C6)"
_ASSERT = "4 — Fail closed if the Primary cert is untrusted and skip-verify is off (WS-C6)"
_RENDER = "4 — Render agent.hcl onto the workload host (idempotent, Req 4.1/4.2/4.6)"
_COMPOSE = "4 — Deploy the Agent sidecar + workload Compose stack (swap-gate, Req 4.3/7.5)"
_READ = "Hello-workload | Read the cluster env file text (control node, read-only, WS-C6)"
_RESOLVE = "Hello-workload | Resolve the dev TLS posture (explicit -e > env file > verify, WS-C6)"
_INCLUDE = "Hello-workload | Stand up the reference workload via the existing openbao-agent role"


def _load(path: Path) -> list[dict]:
    docs = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [t for t in docs if isinstance(t, dict)]


def _by_name(path: Path, name: str) -> dict:
    for t in _load(path):
        if t.get("name") == name:
            return t
    raise AssertionError(f"task not found in {path.name}: {name!r}")


def _index(path: Path, name: str) -> int:
    for i, t in enumerate(_load(path)):
        if t.get("name") == name:
            return i
    raise AssertionError(f"task not found in {path.name}: {name!r}")


def _tpl(expr: str, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


# --------------------------------------------------------------------------- #
# A. Agent preflight — structure
# --------------------------------------------------------------------------- #
def test_probe_is_read_only_verifying_and_never_fails_the_play_itself():
    probe = _by_name(_AGENT_TASKS, _PROBE)
    uri = probe["ansible.builtin.uri"]
    assert uri["validate_certs"] is True, "the probe MUST verify; it reports, never skips"
    assert probe.get("check_mode") is False
    assert probe.get("changed_when") is False
    assert probe.get("failed_when") is False, "unreachable/sealed Primary is not a TLS failure"
    gate = " ".join(str(w) for w in probe["when"])
    assert "openbao_agent_tls_preflight" in gate and "openbao_primary_tls_skip_verify" in gate


def test_preflight_runs_before_render_and_reconcile():
    p = _index(_AGENT_TASKS, _ASSERT)
    assert p < _index(_AGENT_TASKS, _RENDER) < _index(_AGENT_TASKS, _COMPOSE)


def test_preflight_toggle_defaults_on_and_skip_verify_default_stays_off():
    d = yaml.safe_load(_AGENT_DEFAULTS.read_text(encoding="utf-8"))
    assert d["openbao_agent_tls_preflight"] is True
    # P4: never a committed production skip.
    expr = d["openbao_primary_tls_skip_verify"]
    assert "openbao_tls_self_signed | default(false)" in expr


# --------------------------------------------------------------------------- #
# A. Agent preflight — behaviour (evaluate the real assert expression)
# --------------------------------------------------------------------------- #
_SSL_MSG = ("Status code was -1 and not [200, 429]: Request failed: <urlopen error "
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
            "certificate (_ssl.c:1000)>")


def _assert_passes(probe: dict) -> bool:
    cond = _by_name(_AGENT_TASKS, _ASSERT)["ansible.builtin.assert"]["that"][0]
    # `is skipped` needs a registered-result shape; model it explicitly.
    return bool(_tpl("{{ " + cond.strip() + " }}", {"openbao_agent_tls_probe": probe}))


def test_untrusted_self_signed_cert_fails_closed():
    """BUG CONDITION: dev self-signed Primary + skip-verify off => must stop with the remedy."""
    assert _assert_passes({"msg": _SSL_MSG, "status": -1, "failed": False}) is False


def test_other_ssl_trust_failures_also_fail_closed():
    for msg in ("... unable to get local issuer certificate ...",
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"):
        assert _assert_passes({"msg": msg, "status": -1}) is False


def test_trusted_healthy_primary_passes():
    assert _assert_passes({"msg": "OK (123 bytes)", "status": 200}) is True


def test_unreachable_or_sealed_primary_is_not_a_tls_failure():
    """Fail-closed-by-design: a Primary that is down/sealed is the swap-gate's job."""
    assert _assert_passes({"msg": "Connection refused", "status": -1}) is True
    assert _assert_passes({"msg": "OK", "status": 503}) is True


def test_skipped_probe_passes_when_skip_verify_is_already_on():
    assert _assert_passes({"skipped": True, "skip_reason": "Conditional result was False"}) is True


def test_failure_message_names_the_dev_remedy_and_forbids_prod_skip():
    msg = _by_name(_AGENT_TASKS, _ASSERT)["ansible.builtin.assert"]["fail_msg"]
    assert "OPENBAO_TLS_SELF_SIGNED=true" in msg and "openbao_tls_self_signed=true" in msg
    assert "never skip verification" in msg.lower()


# --------------------------------------------------------------------------- #
# B. Consumer deploy — posture resolution
# --------------------------------------------------------------------------- #
def _resolve(env_text: str, **extra) -> bool:
    expr = _by_name(_HELLO_TASKS, _RESOLVE)["ansible.builtin.set_fact"]["hello_workload_tls_self_signed"]
    d = yaml.safe_load((_REPO / "ansible/roles/hello_workload_deploy/defaults/main.yml").read_text())
    variables = {"hello_workload_envfile_text": env_text,
                 "hello_workload_tls_optin_regex": d["hello_workload_tls_optin_regex"].strip(), **extra}
    return str(_tpl(expr, variables)).strip().lower() == "true"


def test_unset_defaults_to_verify():
    """Preservation (P4): no key, no flag => verify."""
    assert _resolve("") is False
    assert _resolve("PROXMOX_API_TOKEN=x\nOTHER=1\n") is False


def test_env_file_opt_in_enables_dev_posture_by_default():
    """THE WS-C6 FIX: the dev env file alone carries the posture; no per-run flag."""
    assert _resolve("A=1\nOPENBAO_TLS_SELF_SIGNED=true\n") is True
    assert _resolve('OPENBAO_TLS_SELF_SIGNED="true"') is True
    assert _resolve("export OPENBAO_TLS_SELF_SIGNED=1  # dev only") is True


def test_env_file_false_or_commented_key_stays_verify():
    assert _resolve("OPENBAO_TLS_SELF_SIGNED=false\n") is False
    assert _resolve("#OPENBAO_TLS_SELF_SIGNED=true\n") is False
    assert _resolve("XOPENBAO_TLS_SELF_SIGNED=true\n") is False


def test_explicit_extra_var_wins_over_the_env_file_both_ways():
    assert _resolve("", openbao_tls_self_signed=True) is True
    assert _resolve("OPENBAO_TLS_SELF_SIGNED=true", openbao_tls_self_signed=False) is False


# --------------------------------------------------------------------------- #
# B. Consumer deploy — wiring
# --------------------------------------------------------------------------- #
def test_env_read_is_no_log_and_missing_file_is_tolerated():
    t = _by_name(_HELLO_TASKS, _READ)
    assert t.get("no_log") is True, "the env file may hold secrets; never log its text"
    assert "errors='ignore'" in t["ansible.builtin.set_fact"]["hello_workload_envfile_text"]


def test_resolved_posture_is_threaded_into_the_agent_include():
    inc = _by_name(_HELLO_TASKS, _INCLUDE)
    assert inc["vars"]["openbao_primary_tls_skip_verify"] == "{{ hello_workload_tls_self_signed }}"


def test_playbook_names_the_cluster_env_file():
    play = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))[0]
    assert play["vars"]["cluster_env_file"] == "cluster.env"


def test_no_committed_production_skip_default():
    """P4: the skip is only ever derived from an explicit opt-in, never a literal true."""
    assert "openbao_primary_tls_skip_verify: true" not in _AGENT_DEFAULTS.read_text(encoding="utf-8")
    inc = _by_name(_HELLO_TASKS, _INCLUDE)
    assert inc["vars"]["openbao_primary_tls_skip_verify"] != "true"
