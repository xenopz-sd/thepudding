"""Offline guard: no NEW `BAO_TOKEN=<value>` on a `docker exec` command line (WS-C62, operator decision 2026-11-17).

Passing `-e BAO_TOKEN=<value>` puts the token on that process's argv on the guest for the call's duration
(`ps`, /proc/<pid>/cmdline). On today's single-tenant, root-only guests that is accepted, so the existing call
sites are grandfathered below. New code MUST use the name-only form, which the Docker CLI resolves from its own
environment:

    argv: ['docker', 'exec', '-e', 'BAO_TOKEN', <container>, 'bao', ...]
    environment:
      BAO_TOKEN: "{{ token }}"
    no_log: true

The allowlist holds EXACT per-file counts: a new occurrence fails, and so does a removed one (lower the count or
delete the row) so the list can never go stale. Converting the grandfathered sites is backlog WS-C62.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_PATTERN = re.compile(r"""BAO_TOKEN=['"]?\s*~|BAO_TOKEN=\{\{""")

# Grandfathered call sites (WS-C62). Only ever lower these numbers.
_GRANDFATHERED = {
    "ansible/roles/consumer_bootstrap/tasks/main.yml": 4,
    "ansible/roles/consumer_handoff/tasks/main.yml": 3,
    "ansible/roles/obs_bootstrap/tasks/seed_grafana_admin.yml": 2,
    "ansible/roles/observability_stack/tasks/grafana.yml": 4,
    "ansible/roles/openbao_init_unseal/tasks/engines_auth_audit.yml": 18,
    "ansible/roles/openbao_init_unseal/tasks/primary_bootstrap.yml": 7,
    "ansible/roles/openbao_init_unseal/tasks/unsealer_bootstrap.yml": 6,
    "ansible/roles/openbao_project_onboard/tasks/main.yml": 8,
    "ansible/roles/openbao_workload_approle/tasks/main.yml": 9,
    "ansible/roles/postgres-server/tasks/main.yml": 2,
    "ansible/roles/svc01_handoff/tasks/main.yml": 1,
    "ansible/roles/svc06_clean_slate/tasks/main.yml": 1,
    "ansible/roles/zitadel-org-provision/tasks/main.yml": 3,
    "ansible/roles/zitadel-org-provision/tasks/usermgr_mint.yml": 3,
    "ansible/roles/zitadel-server/tasks/main.yml": 5,
    "ansible/roles/zitadel-user-provision/tasks/create.yml": 1,
    "ansible/roles/zitadel-user-provision/tasks/teardown.yml": 3,
    "ansible/roles/zitadel_smtp_catcher/tasks/main.yml": 1,
}


def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    files = sorted(set(REPO.glob("ansible/**/*.yml")) | set(REPO.glob("ansible/**/*.j2")))
    for f in files:
        n = 0
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            n += len(_PATTERN.findall(line))
        if n:
            out[str(f.relative_to(REPO))] = n
    return out


def test_scan_is_not_vacuous():
    files = list(REPO.glob("ansible/roles/*/tasks/*.yml"))
    assert len(files) > 50, "ansible task glob found too few files"
    assert _PATTERN.search("'-e', 'BAO_TOKEN=' ~ token") and _PATTERN.search("BAO_TOKEN={{ t }}")
    assert not _PATTERN.search("'-e', 'BAO_TOKEN', container"), "the name-only form must not match"


def test_no_new_token_value_on_docker_exec_argv():
    current = _counts()
    new = {f: n for f, n in current.items() if n > _GRANDFATHERED.get(f, 0)}
    assert not new, (
        "New `BAO_TOKEN=<value>` on a command line (puts the token on the guest's process argv). Use the name-only "
        f"form `-e BAO_TOKEN` with the value in the task `environment:` (WS-C62): {new}"
    )


def test_grandfather_list_does_not_rot():
    current = _counts()
    stale = {f: (n, current.get(f, 0)) for f, n in _GRANDFATHERED.items() if current.get(f, 0) != n}
    assert not stale, (
        "Grandfathered counts no longer match (allowed, recorded, now). Lower or remove the row so the allowlist "
        f"stays exact: {stale}"
    )
