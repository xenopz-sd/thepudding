"""Bug-condition + preservation tests: a clean-slate removes the generated inventory of what it tore down (WS-C42).

THE BUG (found live, WS-C37/WS-C38): `ansible/inventory/<x>.generated.yml` files survived every teardown (service
clean-slates, the consumer clean-slate, the platform clean-slate). SVC-17's file_sd discovery reads those files, so
a project torn down days earlier was still scraped (dead `10.0.100.10:9187`) and InstanceDown fired; a stale file
also misleads any later run that trusts it (integration-boundaries.md §4: the inventory is the Terraform-to-Ansible
hand-off and must describe what exists).
THE FIX: each clean-slate, after its empty-baseline gate passes, removes its own generated inventory through the
shared `generated_inventory_cleanup` role. The SVC-01 file is shared by every SVC-01 instance (last writer wins), so
it is removed only when it describes the instance being torn down (its VMID).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROLES = Path(__file__).resolve().parents[2] / "ansible/roles"
_GIC = _ROLES / "generated_inventory_cleanup/tasks/main.yml"

# role -> (expected path expressions, gate-task name fragment)
_CASES = {
    "svc06_clean_slate": (["{{ svc06_repo_root }}/{{ svc06_generated_inventory }}"], "GATE — assert an empty baseline"),
    "svc07_clean_slate": (["{{ svc07_repo_root }}/{{ svc07_generated_inventory }}"], "GATE — assert an empty baseline"),
    "svc09_clean_slate": (["{{ svc09_repo_root }}/{{ svc09_generated_inventory }}"], "GATE — assert an empty baseline"),
    "obs_clean_slate": (["{{ obs_repo_root }}/{{ obs_generated_inventory }}"], "GATE — assert an empty baseline"),
    "mailcatcher_clean_slate": (["{{ mailcatcher_repo_root }}/{{ mailcatcher_generated_inventory }}"],
                                "GATE — assert an empty baseline"),
    "svc08_clean_slate": (["{{ svc08_repo_root }}/{{ svc08_generated_inventory }}"], "Remove the published root"),
    "svc01_clean_slate": (["{{ svc01_repo_root }}/{{ svc01_generated_inventory }}"], "GATE — assert an empty baseline"),
    "consumer_clean_slate": (["{{ consumer_repo_root }}/ansible/inventory/{{ project_slug }}-consumer.generated.yml",
                              "{{ consumer_repo_root }}/ansible/inventory/{{ project_slug }}-workload.generated.yml"],
                             "Re-affirm the Registry entry"),
}


def _flat(tasks):
    for t in tasks or []:
        if isinstance(t, dict):
            yield t
            for k in ("block", "rescue", "always"):
                yield from _flat(t.get(k))


def _top(role):
    return [t for t in yaml.safe_load((_ROLES / role / "tasks/main.yml").read_text(encoding="utf-8"))
            if isinstance(t, dict)]


def _cleanup(role):
    hits = [t for t in _top(role)
            if (t.get("ansible.builtin.include_role") or {}).get("name") == "generated_inventory_cleanup"]
    assert len(hits) == 1, (role, len(hits))
    return hits[0]


@pytest.mark.parametrize("role", sorted(_CASES))
def test_BUG_each_clean_slate_removes_its_generated_inventory_after_the_gate(role):
    paths, gate = _CASES[role]
    names = [t.get("name", "") for t in _top(role)]
    c = _cleanup(role)
    g = next(i for i, n in enumerate(names) if gate in n)
    assert g < names.index(c["name"]), "only after the empty-baseline gate passed"
    assert c["vars"]["gic_paths"] == paths


def test_svc01_removes_the_shared_file_only_for_its_own_instance():
    assert _cleanup("svc01_clean_slate")["vars"]["gic_only_if_vmid"] == "{{ svc01_vmid }}"


def _decide(content, vmid):
    t = next(t for t in _flat(yaml.safe_load(_GIC.read_text(encoding="utf-8")))
             if "Decide which inventories to remove" in t.get("name", ""))
    expr = t["ansible.builtin.set_fact"]["gic_remove"]
    v = {"gic_only_if_vmid": vmid,
         "gic_stats": {"results": [{"item": "/x/a.yml", "stat": {"exists": content is not None}}]},
         "gic_contents": {"/x/a.yml": content or ""}}
    return _tpl(expr, v)


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


_INV = "all:\n  children:\n    postgres:\n      hosts:\n        svc01-20-01:\n          ansible_host: 10.0.20.14\n          vmid: 1102\n"


def test_vmid_filter_keeps_another_instances_file():
    assert _decide(_INV, "1102") == ["/x/a.yml"]
    assert _decide(_INV, "1101") == []


def test_no_filter_removes_any_existing_file_and_ignores_missing_ones():
    assert _decide(_INV, "") == ["/x/a.yml"]
    assert _decide(None, "") == []


def test_removal_is_reported_by_name_only():
    tasks = list(_flat(yaml.safe_load(_GIC.read_text(encoding="utf-8"))))
    rm = next(t for t in tasks if (t.get("ansible.builtin.file") or {}).get("state") == "absent")
    assert rm["loop"] == "{{ gic_remove }}"


# Adoption by glob (teardown parity): a NEW service clean-slate role is covered the moment it lands.
# platform_clean_slate owns no generated inventory (it orchestrates the service clean-slates);
# terraform_clean_slate is a generic helper invoked by them.
_EXEMPT = {"platform_clean_slate", "terraform_clean_slate"}


def test_every_service_clean_slate_role_removes_its_generated_inventory():
    roles = sorted(p.name for p in _ROLES.glob("*_clean_slate") if p.name not in _EXEMPT)
    assert len(roles) >= 7, "non-vacuity"
    missing = [r for r in roles if "generated_inventory_cleanup" not in (_ROLES / r / "tasks/main.yml").read_text()]
    assert not missing, f"clean-slate roles that leave their generated inventory behind: {missing}"
