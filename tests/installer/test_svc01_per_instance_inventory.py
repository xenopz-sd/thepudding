"""WS-C63 bug condition: every SVC-01 instance wrote ansible/inventory/svc-01-sql-database.generated.yml (last writer
wins), so after a project DB the ZITADEL backing DB vanished from file-mediated consumers (SVC-10 records,
SVC-17/18 targets, SVC-08 trust) — seen in the 2026-11-18 rebuild."""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def _play_vars():
    return yaml.safe_load((REPO / "ansible/playbooks/svc-01-bootstrap.yml").read_text())[0]["vars"]


def test_generated_inventory_is_per_instance():
    inv = _play_vars()["svc01_generated_inventory"]
    assert "{{ project_slug }}" in inv and inv.endswith(".generated.yml"), inv
    a, b = (inv.replace("{{ project_slug }}", s) for s in ("shared-zitadel", "dronefleet"))
    assert a != b


def test_legacy_shared_file_is_removed_only_for_this_instance():
    text = (REPO / "ansible/roles/svc01_provision/tasks/main.yml").read_text()
    assert "svc-01-sql-database.generated.yml" in text and "gic_only_if_vmid" in text


def test_no_role_hardcodes_the_shared_file_for_reading():
    hits = [str(f.relative_to(REPO)) for f in REPO.glob("ansible/roles/*/defaults/main.yml")
            if "svc-01-sql-database.generated.yml" in f.read_text()]
    assert not hits, hits


def test_this_instance_vmid_expression_renders_against_a_real_inventory(tmp_path):
    """The live run hit `regex_search` raising on this expression; render the task's own expression offline."""
    import pytest
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template

    tasks = yaml.safe_load((REPO / "ansible/roles/svc01_provision/tasks/main.yml").read_text())
    t = next(t for t in tasks if isinstance(t, dict) and "Read this instance's VMID" in t.get("name", ""))
    expr = t["ansible.builtin.set_fact"]["svc01_this_vmid"]
    inv = tmp_path / "ansible/inventory/svc-01-x.generated.yml"
    inv.parent.mkdir(parents=True)
    inv.write_text("all:\n  children:\n    postgres:\n      hosts:\n        svc01-20-01:\n"
                   "          ansible_host: 10.0.20.14\n          vmid: 1102\n")
    # A bare Templar has no lookup plugins: substitute the file lookup with its content, keep the filter chain.
    import re as _re
    expr = _re.sub(r"lookup\('ansible\.builtin\.file',[^)]*\)", "_content", expr, flags=_re.S)
    assert "_content" in expr
    v = {"_content": inv.read_text()}
    out = Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))
    assert str(out).strip() == "1102"
