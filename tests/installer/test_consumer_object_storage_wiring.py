"""Consumer orchestrator wiring for `object_storage` (spec consumer-object-storage R2, R3, R4, R6, R7).

Offline structural checks: every new step is gated on the manifest block, composes the SVC-03 onboarder
(never raw Garage/bao calls), keeps the OpenBao token off argv, and the teardown is strict by default.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _ROOT / "ansible" / "roles"
_GATE = "consumer_manifest.object_storage is defined"


def _tasks(role: str) -> list[dict]:
    out = []

    def walk(ts):
        for t in ts or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    walk(yaml.safe_load((_ROLES / role / "tasks" / "main.yml").read_text(encoding="utf-8")))
    return out


def _named(role: str, prefix: str) -> dict:
    matches = [t for t in _tasks(role) if t.get("name", "").startswith(prefix)]
    assert len(matches) == 1, f"{role}: expected one task starting {prefix!r}, got {len(matches)}"
    return matches[0]


def _when(t: dict) -> list[str]:
    w = t.get("when", [])
    return w if isinstance(w, list) else [w]


def test_preflight_fails_closed_without_svc03():
    t = _named("consumer_preflight", "Preflight | Fail closed when object_storage is declared")
    assert _GATE in _when(t)
    assert "svc-03-bootstrap.yml" in t["ansible.builtin.assert"]["fail_msg"]
    d = yaml.safe_load((_ROLES / "consumer_preflight" / "defaults" / "main.yml").read_text())
    assert d["consumer_svc03_inventory"] == "ansible/inventory/svc-03-object-storage.generated.yml"


def test_bootstrap_composes_the_svc03_onboarder_gated_with_token_in_env():
    t = _named("consumer_bootstrap", "Bootstrap | STEP 5 — Compose garage-bucket-provision")
    argv = t["ansible.builtin.command"]["argv"]
    assert argv[1:4] == ["-i", "{{ consumer_svc03_inventory }}", "{{ consumer_garage_bucket_provision_playbook }}"]
    assert "garage_bp_action" not in " ".join(argv), "provision is the default action"
    assert "TOKEN" not in " ".join(argv)
    assert t["environment"] == {"OPENBAO_ADMIN_TOKEN": "{{ consumer_openbao_admin_token }}"}
    assert t["no_log"] is True and _GATE in _when(t)
    assert t["changed_when"] == "(consumer_object_storage_run.stdout | default('')) is search('changed=[1-9]')", "R7"
    tail = _named("consumer_bootstrap", "Bootstrap | STEP 5 — Fail with the child run's output tail")
    assert _GATE in _when(tail)


def test_handoff_reads_back_the_path_without_the_token_on_argv():
    t = _named("consumer_handoff", "Handoff | Probe the object-storage credential path")
    argv = t["ansible.builtin.command"]["argv"]
    assert "'-e', 'BAO_TOKEN'," in argv and "BAO_TOKEN=" not in argv
    assert "'metadata', 'get'" in argv, "metadata only, never the value"
    assert t["vars"]["ansible_connection"] == "ssh", "P2: the orchestrator play is connection: local"
    assert t["no_log"] is True and _GATE in _when(t)
    banner = _named("consumer_handoff", "Handoff | Print the Developer B completion summary banner")
    assert any("Object storage:" in str(line) for line in banner["ansible.builtin.debug"]["msg"])


def test_clean_slate_composes_the_onboarder_teardown_strict_by_default():
    t = _named("consumer_clean_slate", "Clean-Slate | Compose the object-storage teardown")
    argv = t["ansible.builtin.command"]["argv"]
    assert "garage_bp_action=teardown" in argv and _GATE in _when(t)
    assert t["no_log"] is True and "TOKEN" not in " ".join(argv)
    strict = _named("consumer_clean_slate", "Clean-Slate | Fail (strict)")
    assert "consumer_cs_object_storage_teardown_strict | bool" in _when(strict)
    d = yaml.safe_load((_ROLES / "consumer_clean_slate" / "defaults" / "main.yml").read_text())
    assert d["consumer_cs_object_storage_teardown_strict"] is True


def test_object_storage_teardown_runs_before_the_database_teardown():
    names = [t.get("name", "") for t in _tasks("consumer_clean_slate")]
    garage = names.index(_named("consumer_clean_slate", "Clean-Slate | Compose the object-storage teardown")["name"])
    db = next(i for i, n in enumerate(names) if "svc-01" in n.lower() or "database" in n.lower() and "teardown" in n.lower())
    assert garage < db
