"""Offline structural guard for SVC-01 local-backend per-instance state.

Bugfix: fix-svc01-local-backend-per-instance-state. The SVC-01 provision role
(ansible/roles/svc01_provision/tasks/main.yml) runs ONE reusable Terraform root
(infra/projects/svc-01-sql-database) for every SVC-01 instance. On the GitLab
backend each instance's state is isolated via a per-slug state ADDRESS
(<slug>-svc01-postgres). On the LOCAL backend it previously wrote
`backend "local" {}` with NO path, so every slug shared the default
terraform.tfstate — a second instance (e.g. the dronefleet consumer DB) collided
with the first (the shared-zitadel backing DB) and the module's prevent_destroy
guard aborted the apply.

The fix writes a per-slug `path = "{{ svc01_state_name }}.tfstate"` into the local
override so each instance gets its own isolated local state (ADR-0019,
integration-boundaries §5). This test pins that contract OFFLINE (PyYAML/text,
no infra), plus the preservation of the GitLab path / swap-gate / no_log /
gitignore the fix must NOT touch.

Run: ~/venv/devinfra/bin/pytest tests/installer/test_svc01_local_backend_path.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION = _REPO_ROOT / "ansible" / "roles" / "svc01_provision" / "tasks" / "main.yml"
_GITIGNORE = _REPO_ROOT / ".gitignore"


def _tasks() -> list[dict]:
    docs = yaml.safe_load(_PROVISION.read_text(encoding="utf-8"))
    assert isinstance(docs, list) and docs, f"{_PROVISION} must be a task list"
    return docs


def _find_task(name_fragment: str) -> dict:
    for t in _tasks():
        if isinstance(t, dict) and name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"task containing {name_fragment!r} not found in {_PROVISION}")


def _local_override_content() -> str:
    t = _find_task("Ensure local_backend_override.tf is present")
    content = (t.get("ansible.builtin.copy") or t.get("copy") or {}).get("content", "")
    assert content, "the local-backend override task must write inline content:"
    return content


# --------------------------------------------------------------------------- #
# Property 1 — Bug Condition / Expected Behavior: per-slug local state path.
# FAILS on the unfixed role (backend "local" {} with no path); PASSES after fix.
# --------------------------------------------------------------------------- #
def test_local_override_has_per_slug_state_path():
    """The local backend block MUST carry a per-slug `path` derived from
    svc01_state_name — else every slug shares the default terraform.tfstate
    (Req 2.1, 2.2)."""
    content = _local_override_content()
    assert 'backend "local"' in content, "local override must declare backend \"local\""
    assert 'path = "{{ svc01_state_name }}.tfstate"' in content, (
        "the local backend MUST set a per-slug path = \"{{ svc01_state_name }}.tfstate\" "
        "(Req 2.1) — an empty `backend \"local\" {}` shares the default terraform.tfstate "
        "across all slugs, which is the bug"
    )
    # It must NOT be the bare, path-less block (the pre-fix defect).
    assert not re.search(r'backend\s+"local"\s*\{\s*\}', content), (
        "the local backend must not be the path-less `backend \"local\" {}` (the bug)"
    )


def test_two_slugs_yield_two_distinct_local_state_paths():
    """Interpolating svc01_state_name = '<slug>-svc01-postgres' for two distinct
    slugs yields two distinct, correctly-named local state files (Req 2.2)."""
    def rendered_path(slug: str) -> str:
        state_name = f"{slug}-svc01-postgres"
        # mirror the template substitution of {{ svc01_state_name }}.tfstate
        return f"{state_name}.tfstate"

    a = rendered_path("shared-zitadel")
    b = rendered_path("dronefleet")
    assert a != b, "two different slugs must resolve to two different local state files"
    for p in (a, b):
        assert re.match(r"^[a-z0-9-]+-svc01-postgres\.tfstate$", p), (
            f"per-slug local state file {p!r} must match <slug>-svc01-postgres.tfstate"
        )
    assert a == "shared-zitadel-svc01-postgres.tfstate"
    assert b == "dronefleet-svc01-postgres.tfstate"


# --------------------------------------------------------------------------- #
# Property 2 — Preservation: GitLab path / swap-gate / no_log / gitignore / init.
# PASS on the unfixed role too (regions the fix must not touch).
# --------------------------------------------------------------------------- #
def test_gitlab_backend_path_unchanged():
    """The GitLab init still uses the per-slug state address; the fix is local-only
    (Req 3.1)."""
    text = _PROVISION.read_text(encoding="utf-8")
    assert "terraform/state/{{ svc01_state_name }}" in text, (
        "the GitLab per-slug state address must be unchanged (Req 3.1)"
    )
    gitlab_init = _find_task("terraform init (GitLab HTTP backend")
    argv = (gitlab_init.get("ansible.builtin.command") or gitlab_init.get("command") or {}).get("argv", [])
    joined = " ".join(str(a) for a in argv)
    assert "-backend-config=address=" in joined, "GitLab init -backend-config address must be intact (Req 3.1)"


def test_swap_gate_three_ci_keys_unchanged():
    """The swap-gate still selects GitLab iff all three CI_* keys are present (Req 3.2)."""
    t = _find_task("Decide the Terraform backend")
    body = (t.get("ansible.builtin.set_fact") or t.get("set_fact") or {}).get("svc01_use_gitlab_backend", "")
    for key in ("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"):
        assert key in body, f"swap-gate must still test {key} (Req 3.2)"


def test_local_init_keeps_reconfigure():
    """The local init keeps -reconfigure so a slug switch re-points cleanly (Req 3.6)."""
    t = _find_task("terraform init (local backend)")
    argv = (t.get("ansible.builtin.command") or t.get("command") or {}).get("argv", [])
    assert "-reconfigure" in [str(a) for a in argv], "local init must keep -reconfigure (Req 3.6)"


def test_token_handling_tasks_are_no_log():
    """Backend-selection / token tasks retain no_log (Req 3.5)."""
    for frag in (
        "Read the cluster env file to detect the state-backend signal",
        "Extract GitLab CI state-backend values",
        "Derive the per-slug GitLab state address + job token",
    ):
        t = _find_task(frag)
        assert t.get("no_log") is True, f"task {frag!r} must keep no_log: true (Req 3.5)"


def test_gitignore_covers_per_slug_state_and_override():
    """The per-slug state file + override stay gitignored (Req 3.7)."""
    gi = _GITIGNORE.read_text(encoding="utf-8")
    assert "*.tfstate" in gi, ".gitignore must cover *.tfstate (Req 3.7)"
    assert "*_override.tf" in gi, ".gitignore must cover *_override.tf (Req 3.7)"
