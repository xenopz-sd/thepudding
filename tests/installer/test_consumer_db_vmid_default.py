"""Bug-condition + preservation tests: a new project's DB VMID is computed, a standing DB keeps its VMID (WS-C48).

THE BUG (found by the WS-C49 docs audit): `consumer_svc01_vmid` defaulted to `1101`, the example project's
grandfathered VMID (ADR-0022), for EVERY project, so a second project's DB would collide with dronefleet's.
THE FIX: the default is `0` (the module computes `vlan_id * 1000 + host_index`). To never force-replace a standing,
stateful DB (ADR-0022 grandfather posture), the orchestrator reuses the VMID recorded in the project's own
generated inventory (`<slug>-consumer.generated.yml`, the file-mediated Terraform hand-off) when it exists. An
explicit `-e consumer_svc01_vmid=<n>` still wins.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROLE = Path(__file__).resolve().parents[2] / "ansible/roles/consumer_provision"
_RESOLVE = "Provisioning | Resolve the DB VMID (explicit, else the standing DB's, else computed)"
_INV = """all:
  children:
    postgres:
      hosts:
        svc01-100-01:
          ansible_host: 10.0.100.10
          vmid: 1101
"""


def _tasks():
    return [t for t in yaml.safe_load((_ROLE / "tasks/main.yml").read_text(encoding="utf-8")) if isinstance(t, dict)]


def _by(name):
    hits = [t for t in _tasks() if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


def test_BUG_the_default_is_the_computed_sentinel_not_the_example_projects_vmid():
    d = yaml.safe_load((_ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    assert d["consumer_svc01_vmid"] == 0


def _resolve(explicit, inventory_text):
    t = _by(_RESOLVE)
    expr = t["ansible.builtin.set_fact"]["consumer_svc01_vmid_effective"]
    tv = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t.get("vars", {}).items()}
    tv["consumer_db_inventory_text"] = inventory_text
    return int(str(_tpl(expr, {"consumer_svc01_vmid": explicit, **tv})).strip())


def test_explicit_vmid_wins():
    assert _resolve(4242, _INV) == 4242


def test_a_standing_db_keeps_its_recorded_vmid():
    assert _resolve(0, _INV) == 1101


def test_a_new_project_without_an_inventory_gets_the_computed_sentinel():
    assert _resolve(0, "") == 0


def test_an_inventory_without_a_db_host_falls_back_to_computed():
    assert _resolve(0, "all:\n  children:\n    workload:\n      hosts:\n        w: {vmid: 100001}\n") == 0


def test_the_child_run_receives_the_resolved_value_and_the_resolve_runs_first():
    names = [t.get("name") for t in _tasks()]
    compose = next(i for i, n in enumerate(names) if "Compose svc-01-bootstrap.yml" in (n or ""))
    assert names.index(_RESOLVE) < compose
    argv = [str(a) for a in _tasks()[compose]["ansible.builtin.command"]["argv"]]
    assert "svc01_vmid={{ consumer_svc01_vmid_effective }}" in argv


def test_the_inventory_is_read_from_the_project_generated_file_not_terraform_state():
    t = _by(_RESOLVE)
    src = str(t.get("vars", {}).get("consumer_db_inventory_text", ""))
    assert "consumer_generated_inventory" in src and "tfstate" not in src


# --------------------------------------------------------------------------- #
# The consumer clean-slate resolves the same way, and never guesses
# --------------------------------------------------------------------------- #
_CS = Path(__file__).resolve().parents[2] / "ansible/roles/consumer_clean_slate"
_CS_RESOLVE = "Clean-Slate | Resolve the DB VMID (explicit, else the standing DB's recorded one)"
_CS_ASSERT = "Clean-Slate | Assert a concrete DB VMID is known before tearing the DB down"


def _cs_tasks():
    return [t for t in yaml.safe_load((_CS / "tasks/main.yml").read_text(encoding="utf-8")) if isinstance(t, dict)]


def _cs_by(name):
    hits = [t for t in _cs_tasks() if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _cs_resolve(explicit, inventory_text):
    t = _cs_by(_CS_RESOLVE)
    expr = t["ansible.builtin.set_fact"]["consumer_svc01_vmid_effective"]
    tv = {k: trust_as_template(x) if isinstance(x, str) else x for k, x in t.get("vars", {}).items()}
    tv["consumer_db_inventory_text"] = inventory_text
    return int(str(_tpl(expr, {"consumer_svc01_vmid": explicit, **tv})).strip())


def test_clean_slate_default_is_zero_and_uses_explicit_then_recorded():
    d = yaml.safe_load((_CS / "defaults/main.yml").read_text(encoding="utf-8"))
    assert d["consumer_svc01_vmid"] == 0
    assert _cs_resolve(4242, _INV) == 4242
    assert _cs_resolve(0, _INV) == 1101
    assert _cs_resolve(0, "") == 0


def test_clean_slate_fails_closed_instead_of_guessing_a_vmid():
    names = [t.get("name") for t in _cs_tasks()]
    compose = next(i for i, n in enumerate(names) if "Compose svc-01-bootstrap.yml clean-slate" in (n or ""))
    assert names.index(_CS_RESOLVE) < names.index(_CS_ASSERT) < compose
    a = _cs_by(_CS_ASSERT)["ansible.builtin.assert"]
    assert "consumer_svc01_vmid_effective" in " ".join(a["that"])
    assert "consumer_svc01_vmid=" in a["fail_msg"] and "Nothing" in a["fail_msg"]
    argv = [str(x) for x in _cs_tasks()[compose]["ansible.builtin.command"]["argv"]]
    assert "svc01_vmid={{ consumer_svc01_vmid_effective }}" in argv
