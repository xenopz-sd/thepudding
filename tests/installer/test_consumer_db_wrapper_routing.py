"""Bug-condition + preservation tests: consumer DB routed through the db/ wrapper (WS-C9).

Feature: fix-consumer-db-wrapper-routing.
Spec: .kiro/specs/fix-consumer-db-wrapper-routing/ ; ADR-0019 (+ amendment).

THE BUG (ADR-vs-implementation drift)
-------------------------------------
ADR-0019 specifies that a consumer project's DB is provisioned by the per-project
SVC01_Consumer_Root ``infra/projects/<slug>/db/`` (state ``<slug>-db``), which resolves
``vlan_id`` from the registry and ``host_index`` from the ledger. The live flow
(consumer_provision -> svc-01-bootstrap) instead drove ``svc-01-sql-database`` directly
(state ``<slug>-svc01-postgres``), bypassing the wrapper; ``consumer_db_tf_root`` was
defined but never used.

THE FIX
-------
An explicit opt-in ``svc01_consumer_wrapper`` (default false) on the svc-01 installer.
The consumer orchestrator (provision + clean-slate) passes it true. The default path
(SVC-06's shared backing DB, standalone SVC-01 runs) is unchanged.

Pure offline: parses the role/playbook YAML and the wrapper's variables.tf, and evaluates
the real expressions with Ansible's templater.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_R = _REPO / "ansible/roles"
_PROVISION = _R / "svc01_provision/tasks/main.yml"
_PROV_DEFAULTS = _R / "svc01_provision/defaults/main.yml"
_CLEAN = _R / "svc01_clean_slate/tasks/main.yml"
_CONSUMER_PROV = _R / "consumer_provision/tasks/main.yml"
_CONSUMER_CLEAN = _R / "consumer_clean_slate/tasks/main.yml"
_SVC01_PLAY = _REPO / "ansible/playbooks/svc-01-bootstrap.yml"
_SVC06_PROV = _R / "svc06_provision"
_TEMPLATE_DB = _REPO / "infra/projects/_TEMPLATE/db"
_DRONE_DB = _REPO / "infra/projects/dronefleet/db"


def _tasks(path: Path) -> list[dict]:
    return [t for t in yaml.safe_load(path.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _task(path: Path, fragment: str) -> dict:
    hits = [t for t in _tasks(path) if fragment in str(t.get("name", ""))]
    assert len(hits) == 1, f"expected exactly one task containing {fragment!r} in {path.name}, got {len(hits)}"
    return hits[0]


def _argv(task: dict) -> list[str]:
    return [str(a) for a in task["ansible.builtin.command"]["argv"]]


def _tpl(expr: str, variables: dict):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


# --------------------------------------------------------------------------- #
# The bug condition: the consumer flow must route through the wrapper
# --------------------------------------------------------------------------- #
def test_consumer_provision_opts_into_wrapper_mode():
    argv = _argv(_task(_CONSUMER_PROV, "Compose svc-01-bootstrap.yml to provision"))
    assert "svc01_consumer_wrapper=true" in argv


def test_consumer_provision_regenerates_inventory_from_the_wrapper_root():
    argv = _argv(_task(_CONSUMER_PROV, "Regenerate the consumer-scoped dynamic inventory"))
    root = argv[argv.index("--root") + 1]
    assert root == "infra/projects/{{ project_slug }}/db"


def test_consumer_clean_slate_targets_the_same_wrapper_root():
    argv = _argv(_task(_CONSUMER_CLEAN, "Compose svc-01-bootstrap.yml clean-slate"))
    assert "svc01_consumer_wrapper=true" in argv, "the DB was provisioned via the wrapper; tear it down via the same root"


def test_consumer_db_tf_root_is_no_longer_dead():
    d = yaml.safe_load((_R / "consumer_provision/defaults/main.yml").read_text(encoding="utf-8"))
    assert d["consumer_db_tf_root"] == "infra/projects/{{ project_slug }}/db"
    root = _R / "consumer_provision"
    blob = "".join(p.read_text(encoding="utf-8") for p in root.rglob("*.yml"))
    assert "svc01_consumer_wrapper=true" in blob  # the flag that consumes the wrapper path


# --------------------------------------------------------------------------- #
# Preservation: the default path (SVC-06 backing DB, standalone SVC-01) is unchanged
# --------------------------------------------------------------------------- #
def test_wrapper_mode_defaults_off_everywhere():
    play = yaml.safe_load(_SVC01_PLAY.read_text(encoding="utf-8"))[0]
    assert play["vars"]["svc01_consumer_wrapper"] is False
    d = yaml.safe_load(_PROV_DEFAULTS.read_text(encoding="utf-8"))
    assert d["svc01_consumer_wrapper"] is False


def test_svc06_backing_db_never_opts_in():
    for p in _SVC06_PROV.rglob("*.yml"):
        assert "svc01_consumer_wrapper" not in p.read_text(encoding="utf-8"), p


def test_legacy_state_name_and_apply_are_gated_off_in_wrapper_mode():
    legacy = _task(_PROVISION, "Derive the per-instance GitLab state name")
    assert legacy["ansible.builtin.set_fact"]["svc01_state_name"] == "{{ project_slug }}-svc01-postgres"
    assert "not (svc01_consumer_wrapper | bool)" in legacy["when"]
    legacy_apply = _task(_PROVISION, "terraform apply (non-interactive, auto-approve)")
    assert "not (svc01_consumer_wrapper | bool)" in legacy_apply["when"]


# --------------------------------------------------------------------------- #
# Behaviour of the real expressions
# --------------------------------------------------------------------------- #
def _defaults() -> dict:
    return yaml.safe_load(_PROV_DEFAULTS.read_text(encoding="utf-8"))


def test_tf_root_resolution_in_both_modes():
    d = _defaults()
    base = {"project_slug": "dronefleet", "svc01_repo_root": "/repo"}
    off = _tpl(d["svc01_tf_root_rel"], {**base, "svc01_consumer_wrapper": False}).strip()
    on = _tpl(d["svc01_tf_root_rel"], {**base, "svc01_consumer_wrapper": True}).strip()
    assert off == "infra/projects/svc-01-sql-database"
    assert on == "infra/projects/dronefleet/db"


def test_wrapper_state_name_is_slug_db():
    t = _task(_PROVISION, "Derive the consumer-wrapper state name")
    assert _tpl(t["ansible.builtin.set_fact"]["svc01_state_name"], {"project_slug": "dronefleet"}) == "dronefleet-db"
    assert t["when"] == "svc01_consumer_wrapper | bool"


# --------------------------------------------------------------------------- #
# The wrapper apply must match what the wrapper actually declares (drift guard)
# --------------------------------------------------------------------------- #
def _declared_vars(variables_tf: Path) -> dict[str, bool]:
    """name -> has_default, parsed from variable blocks."""
    text = variables_tf.read_text(encoding="utf-8")
    out: dict[str, bool] = {}
    for m in re.finditer(r'variable\s+"([a-z0-9_]+)"\s*\{(.*?)\n\}', text, re.S):
        out[m.group(1)] = re.search(r"^\s*default\s*=", m.group(2), re.M) is not None
    return out


def _wrapper_apply_vars() -> set[str]:
    argv = _argv(_task(_PROVISION, "terraform apply the per-project consumer wrapper root"))
    return {a[len("-var="):].split("=", 1)[0] for a in argv if a.startswith("-var=")}


def test_wrapper_apply_passes_only_declared_variables():
    declared = set(_declared_vars(_TEMPLATE_DB / "variables.tf"))
    passed = _wrapper_apply_vars()
    assert passed <= declared, f"undeclared -var would fail terraform: {sorted(passed - declared)}"


def test_wrapper_apply_passes_every_required_variable():
    required = {n for n, has_default in _declared_vars(_TEMPLATE_DB / "variables.tf").items() if not has_default}
    # Provider plumbing is supplied via TF_VAR_* env (svc01_tf_env), not -var.
    env_supplied = {"proxmox_endpoint", "proxmox_api_token", "proxmox_insecure", "operator_ssh_public_keys"}
    missing = required - _wrapper_apply_vars() - env_supplied
    assert not missing, f"required wrapper variables not passed: {sorted(missing)}"


def test_wrapper_apply_never_passes_registry_or_ledger_resolved_inputs():
    """The wrapper READS vlan_id (registry) and host_index (ledger); passing them is the
    'free operator input' the wrapper exists to prevent (ADR-0019, Req 2.3 / 3.8)."""
    passed = _wrapper_apply_vars()
    assert not passed & {"vlan_id", "host_index", "ledger_component"}


def test_wrapper_host_index_is_adopted_from_the_wrapper_output():
    read = _task(_PROVISION, "Read the wrapper's ledger-resolved host_index")
    assert read["ansible.builtin.command"]["argv"][:3] == ["terraform", "output", "-raw"]
    adopt = _task(_PROVISION, "Adopt the ledger-resolved host_index")
    assert "host_index" in adopt["ansible.builtin.set_fact"]
    names = [str(t.get("name")) for t in _tasks(_PROVISION)]
    assert names.index(read["name"]) < names.index(adopt["name"])
    keyscan = _task(_PROVISION, "ssh-keyscan the guest IP")
    assert names.index(adopt["name"]) < names.index(keyscan["name"]), "known_hosts must use the TRUE ledger IP"


def test_missing_wrapper_root_fails_closed_before_any_terraform_with_the_copy_command():
    names = [str(t.get("name")) for t in _tasks(_PROVISION)]
    stat_i = next(i for i, n in enumerate(names) if "Stat the per-project consumer wrapper root" in n)
    init_i = next(i for i, n in enumerate(names) if "terraform init (local backend)" in n)
    assert stat_i < init_i
    fail = _task(_PROVISION, "Fail closed if the per-project db/ wrapper root is missing")
    msg = fail["ansible.builtin.assert"]["fail_msg"]
    assert "cp -r infra/projects/_TEMPLATE/db infra/projects/{{ project_slug }}/db" in msg


# --------------------------------------------------------------------------- #
# Clean-slate addressing in wrapper mode
# --------------------------------------------------------------------------- #
def test_clean_slate_target_follows_the_mode():
    t = _task(_CLEAN, "Assemble the single SVC-01 clean_slate_target")
    target = t["ansible.builtin.set_fact"]["svc01_clean_slate_targets"][0]
    d = _defaults()
    base = {"project_slug": "dronefleet", "svc01_repo_root": "/repo", "vlan_id": 100,
            "proxmox_node_name": "n", "svc01_vmid": 1101}
    for mode, root, addr in (
        (False, "infra/projects/svc-01-sql-database", "proxmox_virtual_environment_container.db"),
        (True, "infra/projects/dronefleet/db", "module.db.proxmox_virtual_environment_container.db"),
    ):
        v = {**base, "svc01_consumer_wrapper": mode}
        v["svc01_tf_root_rel"] = _tpl(d["svc01_tf_root_rel"], v).strip()
        assert _tpl(target["terraform_root"], v).strip() == root
        assert _tpl(target["resource_address"], v).strip() == addr


# --------------------------------------------------------------------------- #
# Template / instance coherence
# --------------------------------------------------------------------------- #
def test_template_db_is_the_copy_source_for_the_dronefleet_instance():
    assert set(_declared_vars(_TEMPLATE_DB / "variables.tf")) == set(_declared_vars(_DRONE_DB / "variables.tf"))
    for f in ("main.tf", "outputs.tf", "providers.tf", "versions.tf"):
        assert (_TEMPLATE_DB / f).exists(), f
    assert 'source = "../../svc-01-sql-database"' in (_TEMPLATE_DB / "main.tf").read_text(encoding="utf-8")
    # The template must not default to a real registered slug.
    assert 'default     = "dronefleet"' not in (_TEMPLATE_DB / "variables.tf").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Shared clean-slate tolerance: a root with no state file is already-absent
# --------------------------------------------------------------------------- #
def test_terraform_clean_slate_tolerates_a_root_with_no_state_file():
    """Live-caught: clean-slate of the freshly-routed wrapper root (never applied) died on
    `No state file was found!` instead of treating it as already-absent."""
    t = _task(_R / "terraform_clean_slate/tasks/reset_target.yml", "terraform state rm")
    guards = " ".join(str(c) for c in t["failed_when"])
    for stream in ("stdout", "stderr", "msg"):
        assert f"'No state file was found' not in (clean_slate_state_rm.{stream} | default(''))" in guards
    # preservation: the original already-absent tolerance and the rc gate remain.
    assert "'No matching objects' not in (clean_slate_state_rm.stdout" in guards
    assert "(clean_slate_state_rm.rc | default(1)) != 0" in guards
