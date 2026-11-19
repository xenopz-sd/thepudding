"""Bug condition (found live 2026-11-17, WS-C60): consumer STEP 4 did not thread the platform domain to the
zitadel-org-provision child, so with a real PLATFORM_DOMAIN the helper targeted auth.platform.example.test,
failed on DNS and printed nothing (from_json on empty stdout). Same class as SVC-10 defect D-3."""
from pathlib import Path

import yaml

TASKS = Path(__file__).resolve().parents[2] / "ansible/roles/consumer_bootstrap/tasks/main.yml"


def _step4():
    for t in yaml.safe_load(TASKS.read_text()):
        if isinstance(t, dict) and "STEP 4 — Compose zitadel-org-provision" in t.get("name", ""):
            return t
    raise AssertionError("STEP 4 task not found (non-vacuity)")


def test_step4_threads_the_platform_domain_to_the_zitadel_child():
    argv = _step4()["ansible.builtin.command"]["argv"]
    assert any("zop_platform_domain={{ consumer_platform_domain" in str(a) for a in argv), argv
