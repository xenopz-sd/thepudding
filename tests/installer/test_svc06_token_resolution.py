"""Bug condition (found live 2026-11-18, full rebuild): svc06_bootstrap handed its child runs ONLY the
process-env OPENBAO_ADMIN_TOKEN, so without an exported token ZITADEL's OpenBao reads failed inside a no_log child
(the WS-C59 "transient"). Every other installer falls back to the cluster env file (WS-C45)."""
from pathlib import Path

import yaml

TASKS = Path(__file__).resolve().parents[2] / "ansible/roles/svc06_bootstrap/tasks/main.yml"


def test_children_get_a_token_resolved_with_the_cluster_env_file_fallback():
    tasks = [t for t in yaml.safe_load(TASKS.read_text()) if isinstance(t, dict)]
    resolve = [t for t in tasks if "svc06_openbao_admin_token" in str(t.get("ansible.builtin.set_fact", {}))]
    assert resolve and "cluster_env_file" in str(resolve[0]) and resolve[0].get("no_log") is True
    children = [t for t in tasks if "OPENBAO_ADMIN_TOKEN" in str(t.get("environment", {}))]
    assert len(children) >= 2
    for t in children:
        assert "svc06_openbao_admin_token" in str(t["environment"]["OPENBAO_ADMIN_TOKEN"]), t["name"]
