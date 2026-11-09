"""Bug-condition + preservation tests: composed-service scope pre-check (WS-C11).

Feature: fix-orchestrator-composed-guard-scope.
Spec: .kiro/specs/fix-orchestrator-composed-guard-scope/.

THE BUG (premise corrected): the per-service scope ``AGENT_LIVE_AUTHORIZED_SERVICES`` IS
inherited by child installers (the guard reads the process environment and the roles
never override it). The real weakness is TIMING. An orchestrator that composes child
installers (consumer -> svc01, svc06 -> svc01, platform clean-slate -> the catalog) was
authorized for itself, started mutating, and only then did a child's own consult REFUSE
("service svc01 out of AGENT_LIVE_AUTHORIZED_SERVICES scope"), leaving a half-done run.

THE FIX: ``throwaway_guard.py --also-service=<csv>`` lets the orchestrator check the whole
composed set in its ONE up-front consult. ``decide()`` is untouched (shared symbol); the
check is the separate pure ``scope_missing()``. It can only add refusals.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
import throwaway_guard as g  # noqa: E402
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_R = _REPO / "ansible/roles"


# --------------------------------------------------------------------------- #
# Pure helper
# --------------------------------------------------------------------------- #
def test_no_scope_means_nothing_is_missing():
    assert g.scope_missing(None, ["svc01", "svc06"]) == []
    assert g.scope_missing("", ["svc01"]) == []
    assert g.scope_missing("   ", ["svc01"]) == []


def test_narrow_scope_reports_the_omitted_services():
    assert g.scope_missing("consumer", ["svc01"]) == ["svc01"]
    assert g.scope_missing("consumer,svc01", ["svc01"]) == []
    assert g.scope_missing("svc01", ["svc01", "svc06", "svc07"]) == ["svc06", "svc07"]


def test_scope_matching_is_case_and_separator_insensitive():
    assert g.scope_missing("Consumer SVC01", ["svc01"]) == []
    assert g.scope_missing("consumer, svc01", ["svc01"]) == []


def test_blank_composed_entries_are_ignored():
    assert g.scope_missing("consumer", ["", "  "]) == []


def test_scope_to_nothing_fails_closed_for_every_composed_service():
    # parse_services_scope: a present-but-only-separators value is an EMPTY set, not None.
    assert g.scope_missing(",  ,", ["svc01"]) == ["svc01"]


# --------------------------------------------------------------------------- #
# CLI behaviour (the real main(), isolated env)
# --------------------------------------------------------------------------- #
_EP = "https://shrimp.lan:8006/api2/json"


def _fixture(tmp_path):
    env = tmp_path / ".env.throwaway"
    env.write_text("\n".join([
        f"PROXMOX_ENDPOINT={_EP}", "PROXMOX_API_TOKEN=SENTINEL-TOKEN-zzz==",
        "PROXMOX_NODE_NAME=shrimp", "PROXMOX_LXC_TEMPLATE=local:vztmpl/debian-12-standard.tar.zst",
    ]), encoding="utf-8")
    allow = tmp_path / "allow.yml"
    allow.write_text(yaml.safe_dump({"throwaway_clusters": [{"endpoint_host": "shrimp.lan", "node_name": "shrimp"}]}),
                     encoding="utf-8")
    return str(env), str(allow)


def _run(tmp_path, monkeypatch, capsys, *, scope, also, armed="1", service="consumer"):
    env, allow = _fixture(tmp_path)
    if armed is None:
        monkeypatch.delenv("AGENT_LIVE_AUTHORIZED", raising=False)
    else:
        monkeypatch.setenv("AGENT_LIVE_AUTHORIZED", armed)
    if scope is None:
        monkeypatch.delenv("AGENT_LIVE_AUTHORIZED_SERVICES", raising=False)
    else:
        monkeypatch.setenv("AGENT_LIVE_AUTHORIZED_SERVICES", scope)
    argv = ["--service", service, "--env-file", env, "--allowlist", allow]
    if also is not None:
        argv.append(f"--also-service={also}")
    rc = g.main(argv)
    return rc, json.loads(capsys.readouterr().out)


def test_BUG_narrow_scope_missing_a_composed_child_is_refused_up_front(tmp_path, monkeypatch, capsys):
    """Scope grants only the orchestrator; its composed svc01 child would REFUSE mid-run."""
    rc, out = _run(tmp_path, monkeypatch, capsys, scope="consumer", also="svc01")
    assert rc == 3 and out["authorized"] is False
    assert "svc01" in out["reason"] and "composed" in out["reason"]
    assert out["identity"] is not None, "the refusal still carries the non-secret identity for logging"


def test_scope_granting_orchestrator_and_children_is_authorized(tmp_path, monkeypatch, capsys):
    rc, out = _run(tmp_path, monkeypatch, capsys, scope="consumer,svc01", also="svc01")
    assert rc == 0 and out["authorized"] is True


def test_unset_scope_preserves_the_global_arm(tmp_path, monkeypatch, capsys):
    rc, out = _run(tmp_path, monkeypatch, capsys, scope=None, also="svc01,svc06")
    assert rc == 0 and out["authorized"] is True


def test_empty_also_service_is_the_unchanged_legacy_behaviour(tmp_path, monkeypatch, capsys):
    rc, out = _run(tmp_path, monkeypatch, capsys, scope="consumer", also="")
    assert rc == 0, "an orchestrator composing nothing is unaffected"
    rc2, out2 = _run(tmp_path, monkeypatch, capsys, scope="consumer", also=None)
    assert rc2 == 0


def test_the_first_failing_gate_still_wins(tmp_path, monkeypatch, capsys):
    rc, out = _run(tmp_path, monkeypatch, capsys, scope="consumer", also="svc01", armed=None)
    assert rc == 3 and "interlock unset" in out["reason"]
    rc, out = _run(tmp_path, monkeypatch, capsys, scope="svc01", also="svc01", service="consumer")
    assert rc == 3 and "service consumer out of" in out["reason"]


def test_refusal_output_never_contains_the_token(tmp_path, monkeypatch, capsys):
    rc, out = _run(tmp_path, monkeypatch, capsys, scope="consumer", also="svc01")
    assert "SENTINEL-TOKEN" not in json.dumps(out)


# --------------------------------------------------------------------------- #
# Wiring: each composing orchestrator's consult carries the pre-check
# --------------------------------------------------------------------------- #
_CONSULTS = {
    # role: (consult task name fragment, composed-services var, expected default)
    "consumer_preflight": ("Consult the throwaway-cluster authorization guard", "consumer_guard_composed_services", ["svc01"]),
    "consumer_clean_slate": ("Consult the throwaway-cluster authorization guard", "consumer_guard_composed_services", ["svc01"]),
    "svc06_preflight": ("Consult the throwaway-cluster authorization guard", "svc06_guard_composed_services", ["svc01"]),
    "svc06_clean_slate": (None, "svc06_guard_composed_services", ["svc01"]),
}


def _tasks(role):
    return [t for t in yaml.safe_load((_R / role / "tasks/main.yml").read_text(encoding="utf-8")) if isinstance(t, dict)]


def _guard_consult(role):
    hits = [t for t in _tasks(role)
            if isinstance(t.get("ansible.builtin.command"), dict)
            and any("throwaway_guard" in str(a) for a in t["ansible.builtin.command"].get("argv", []))]
    assert len(hits) == 1, (role, len(hits))
    return hits[0]


@pytest.mark.parametrize("role", sorted(_CONSULTS) + ["platform_clean_slate"])
def test_consult_passes_also_service(role):
    argv = [str(a).strip() for a in _guard_consult(role)["ansible.builtin.command"]["argv"]]
    assert any(a.startswith("--also-service=") for a in argv), f"{role} does not pre-check its composed children"


@pytest.mark.parametrize("role", sorted(_CONSULTS))
def test_composed_service_defaults(role):
    _, var, expected = _CONSULTS[role]
    d = yaml.safe_load((_R / role / "defaults/main.yml").read_text(encoding="utf-8"))
    assert d[var] == expected


def test_platform_clean_slate_precheck_covers_the_catalog_plus_the_svc01_child():
    d = yaml.safe_load((_R / "platform_clean_slate/defaults/main.yml").read_text(encoding="utf-8"))
    arg = next(str(a) for a in _guard_consult("platform_clean_slate")["ansible.builtin.command"]["argv"]
               if str(a).strip().startswith("--also-service="))
    rendered = Templar(loader=DataLoader(), variables={
        "platform_clean_slate_services": d["platform_clean_slate_services"],
        "platform_guard_extra_composed_services": d["platform_guard_extra_composed_services"],
    }).template(trust_as_template(arg)).strip()
    services = set(rendered.split("=", 1)[1].split(","))
    assert {s["service"] for s in d["platform_clean_slate_services"]} <= services
    assert "svc01" in services, "svc06's composed backing-DB clean-slate consults under svc01"


def test_composed_lists_cover_every_bootstrap_playbook_the_orchestrator_composes():
    """Drift guard: if an orchestrator starts composing another svc-NN-bootstrap child, its
    pre-check list must grow with it, or the mid-run refusal is back."""
    pairs = {
        "consumer_preflight": ["consumer_provision", "consumer_clean_slate"],
        "consumer_clean_slate": ["consumer_clean_slate"],
        "svc06_preflight": ["svc06_provision"],
        "svc06_clean_slate": ["svc06_clean_slate"],
    }
    for guard_role, composing in pairs.items():
        d = yaml.safe_load((_R / guard_role / "defaults/main.yml").read_text(encoding="utf-8"))
        covered = set(d[_CONSULTS[guard_role][1]])
        found: set[str] = set()
        for r in composing:
            for f in (_R / r).rglob("*.yml"):
                found |= {f"svc{m}" for m in re.findall(r"svc-(\d\d)-bootstrap\.yml", f.read_text(encoding="utf-8"))}
                found |= {f"svc{m}" for m in re.findall(r"consumer_svc(\d\d)_bootstrap_playbook", f.read_text(encoding="utf-8"))}
        found.discard("svc06") if guard_role.startswith("svc06") else None
        assert found <= covered, f"{guard_role} composes {sorted(found)} but pre-checks only {sorted(covered)}"
