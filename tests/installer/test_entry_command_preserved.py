"""Offline drift-guard for the preserved Developer-B entry command (Task 10.2).

Feature: svc07-installer-simplification.

Spec: .kiro/specs/svc07-installer-simplification/ (requirements.md Requirements
1.4, 6.1-6.4, 7.1; design.md "Property 2: Entry unchanged" and "Property 5:
Swap-gate preserved"; tasks.md Task 10.2). This pins, as an offline
text/structure drift-guard, that the ~1900-line -> ~150-line thinning of
``ansible/playbooks/svc-07-bootstrap.yml`` did NOT change what the Developer-B
entry command selects.

WHAT IS ASSERTED (offline, no infra, PyYAML parse of the committed play/role)
-----------------------------------------------------------------------------
Property 2 (Entry unchanged) — against ``ansible/playbooks/svc-07-bootstrap.yml``:
  * the orchestrator is still a SINGLE play running ``hosts: localhost`` /
    ``connection: local`` (the Day-0 orchestrator posture the entry command
    relies on — integration-boundaries.md §4), with ``gather_facts: true`` and
    ``any_errors_fatal: true`` (Req 1.4, 7.1);
  * the play still accepts the SAME three ``-e`` variables the documented entry
    invocation passes — ``cluster_env_file`` (default ``cluster.env``),
    ``clean_slate`` (default false), and ``clean_slate_confirm`` — as play-level
    ``vars:`` so ``-e cluster_env_file=cluster.dev.env`` /
    ``-e clean_slate=true`` / ``-e clean_slate_confirm=force`` bind exactly as
    the baseline (Req 1.4, 7.1);
  * the documented entry invocation itself
    (``ansible-playbook -i ansible/inventory/localhost.yml
    ansible/playbooks/svc-07-bootstrap.yml -e cluster_env_file=...``) still
    resolves to real committed paths (the localhost inventory + the play).

Property 5 (Swap-gate preserved, at play/role level) — this NEW file focuses on
the ENTRY COMMAND + play-level invariants and DEFERS the detailed swap-gate
DECISION EXPRESSION to the repointed ``test_swap_gate.py`` (which owns the
CI-key AND-semantics + the harness cross-check). To stay consistent with — and
not contradict — that test, this file asserts only the coarse, complementary
fact that the swap-gate + its INFO decision log now live in the
``svc07_provision`` ROLE (``ansible/roles/svc07_provision/tasks/main.yml``), NOT
inline in the thinned play (Req 6.1-6.4). ``test_swap_gate.py`` asserts the exact
decision expression there; this file asserts only "it moved into the role and
left the play".

HOW IT STAYS OFFLINE
--------------------
Pure PyYAML + text parse of the committed files — no ``ansible-playbook`` binary,
no live Proxmox. Always runs; no ``requires_infra`` gate. Mirrors the drift-guard
style of ``test_thin_orchestrator_play.py`` / ``test_swap_gate.py``.

Validates: Requirements 1.4, 6.1, 6.2, 6.3, 6.4, 7.1
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_entry_command_preserved.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _REPO_ROOT / "ansible" / "playbooks" / "svc-07-bootstrap.yml"
_LOCALHOST_INVENTORY = _REPO_ROOT / "ansible" / "inventory" / "localhost.yml"
# The swap-gate + its INFO decision log moved into the svc07_provision role
# (Task 4.1). This file only checks it LEFT the play and LANDED in the role; the
# exact decision expression is asserted by test_swap_gate.py.
_PROVISION_ROLE_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc07_provision" / "tasks" / "main.yml"
)

# The three -e variables the documented Developer-B entry invocation passes; the
# play must declare each as a play-level var so -e binds it (Req 1.4, 7.1).
_ENTRY_E_VARS = ("cluster_env_file", "clean_slate", "clean_slate_confirm")


# --------------------------------------------------------------------------- #
# Helpers — parse the play.
# --------------------------------------------------------------------------- #
def _load_plays(path: Path) -> list[dict]:
    """Return the list of play dicts from an Ansible playbook document."""
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


def _orchestrator_play() -> dict:
    plays = _load_plays(_ORCHESTRATOR)
    assert len(plays) == 1, (
        f"svc-07-bootstrap.yml must be a single-play orchestrator; found {len(plays)}."
    )
    return plays[0]


def _play_vars() -> dict:
    play = _orchestrator_play()
    play_vars = play.get("vars") or {}
    assert isinstance(play_vars, dict), (
        "svc-07-bootstrap.yml's top-level `vars:` must be a mapping."
    )
    return play_vars


# =============================================================================
# Property 2 — the play still runs on localhost/local with the entry posture.
# Validates: Requirements 1.4, 7.1
# =============================================================================
def test_play_runs_localhost_connection_local_orchestrator_posture():
    """Validates: Requirements 1.4, 7.1 / Property 2.

    The thinned orchestrator must keep the Day-0 orchestrator posture the entry
    command relies on: a single play on ``hosts: localhost`` /
    ``connection: local`` with ``gather_facts: true`` and
    ``any_errors_fatal: true``. If the thinning changed any of these, the entry
    command would select different behavior.
    """
    play = _orchestrator_play()

    assert str(play.get("hosts")) == "localhost", (
        "The orchestrator must run `hosts: localhost` (Day-0 orchestrator "
        f"posture); found hosts={play.get('hosts')!r}."
    )
    assert str(play.get("connection")) == "local", (
        "The orchestrator must run `connection: local`; found "
        f"connection={play.get('connection')!r}."
    )
    # gather_facts: true — preflight's Python/Ansible version asserts read the
    # gathered ansible_python_version / ansible_version facts.
    assert play.get("gather_facts") in (True, "true", "yes"), (
        "The orchestrator must keep `gather_facts: true` (preflight reads gathered "
        f"facts); found gather_facts={play.get('gather_facts')!r}."
    )
    # any_errors_fatal: true — a failed phase / non-zero child exit aborts the run.
    assert play.get("any_errors_fatal") in (True, "true", "yes"), (
        "The orchestrator must keep `any_errors_fatal: true`; found "
        f"any_errors_fatal={play.get('any_errors_fatal')!r}."
    )


def test_play_accepts_the_same_entry_e_vars_as_play_vars():
    """Validates: Requirements 1.4, 7.1 / Property 2.

    The play must declare ``cluster_env_file``, ``clean_slate`` and
    ``clean_slate_confirm`` as play-level ``vars:`` so the documented entry
    invocation's ``-e`` flags bind exactly as the baseline. A ``-e`` override of
    a play var is honored, so declaring them here (with the baseline defaults)
    preserves the entry contract.
    """
    play_vars = _play_vars()

    for name in _ENTRY_E_VARS:
        assert name in play_vars, (
            f"The play must declare {name!r} as a play-level var so the entry "
            f"command's `-e {name}=...` binds it. Play vars: {sorted(play_vars)}."
        )

    # Baseline defaults preserved: cluster_env_file defaults to cluster.env, and
    # clean_slate defaults to a falsy value (off by default).
    assert str(play_vars.get("cluster_env_file")) == "cluster.env", (
        "cluster_env_file must default to 'cluster.env' (overridable with "
        f"-e cluster_env_file=cluster.dev.env); found "
        f"{play_vars.get('cluster_env_file')!r}."
    )
    clean_slate_default = play_vars.get("clean_slate")
    assert clean_slate_default in (False, "false", "no", 0), (
        "clean_slate must default to false (the install path runs unless "
        f"-e clean_slate=true); found {clean_slate_default!r}."
    )


def test_documented_entry_invocation_paths_resolve():
    """Validates: Requirements 1.4, 7.1 / Property 2.

    The documented Developer-B entry invocation
    ``ansible-playbook -i ansible/inventory/localhost.yml
    ansible/playbooks/svc-07-bootstrap.yml -e cluster_env_file=...`` must still
    resolve to real committed paths — the inventory and the play both exist at
    the documented locations, so the unchanged entry command still works.
    """
    assert _ORCHESTRATOR.is_file(), (
        "The entry playbook ansible/playbooks/svc-07-bootstrap.yml must exist at "
        "the documented path."
    )
    assert _LOCALHOST_INVENTORY.is_file(), (
        "The entry inventory ansible/inventory/localhost.yml must exist at the "
        "documented path so `-i ansible/inventory/localhost.yml` resolves."
    )


# =============================================================================
# Property 5 (play/role level) — the swap-gate moved INTO the provision role and
# LEFT the thinned play. The exact decision expression is owned by
# test_swap_gate.py; this only asserts the relocation, to stay consistent.
# Validates: Requirements 6.1, 6.2, 6.3, 6.4
# =============================================================================
def test_swap_gate_and_info_log_live_in_provision_role_not_the_play():
    """Validates: Requirements 6.1, 6.2, 6.3, 6.4 / Property 5.

    After the thinning, the backend swap-gate decision fact
    ``svc07_use_gitlab_backend`` and its INFO decision-log marker
    (``INFO: Terraform backend:``) must live in the ``svc07_provision`` ROLE, not
    inline in ``svc-07-bootstrap.yml``. This is the coarse relocation invariant;
    the detailed CI-key AND-semantics decision expression is asserted by
    ``test_swap_gate.py`` (repointed to the same role file), so this file does
    not duplicate — and cannot contradict — that assertion.
    """
    play_text = _ORCHESTRATOR.read_text(encoding="utf-8")
    provision_text = _PROVISION_ROLE_TASKS.read_text(encoding="utf-8")

    # The gate + its INFO log now live in the provision role.
    assert "svc07_use_gitlab_backend" in provision_text, (
        "The backend swap-gate fact svc07_use_gitlab_backend must live in "
        "svc07_provision/tasks/main.yml after the thinning (Req 6.1, 6.2)."
    )
    assert "INFO: Terraform backend:" in provision_text, (
        "The swap-gate INFO decision-log marker must live in "
        "svc07_provision/tasks/main.yml (Req 6.3)."
    )

    # ...and they no longer live inline in the thinned orchestrator play. The
    # thin play is only import_role calls + per-phase rescue fails, so the gate
    # text must be absent from it.
    assert "svc07_use_gitlab_backend" not in play_text, (
        "The swap-gate decision must NOT remain inline in the thinned "
        "svc-07-bootstrap.yml — it moved into the svc07_provision role (Req 1.1, "
        "6.1)."
    )
    assert "INFO: Terraform backend:" not in play_text, (
        "The swap-gate INFO decision log must NOT remain inline in the thinned "
        "svc-07-bootstrap.yml — it moved into the svc07_provision role (Req 6.3)."
    )


def test_provision_role_names_all_three_ci_keys():
    """Validates: Requirements 6.1, 6.2 / Property 5.

    Coarse corroboration that the relocated swap-gate still keys off the SAME
    three GitLab CI state-backend variables (the entry command's behavior selects
    the backend from these). The exact AND-semantics expression is owned by
    ``test_swap_gate.py``; here we only confirm all three key names are present in
    the provision role, so the relocation did not drop one.
    """
    provision_text = _PROVISION_ROLE_TASKS.read_text(encoding="utf-8")
    for key in ("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"):
        assert key in provision_text, (
            f"The relocated swap-gate must still reference {key} in "
            "svc07_provision/tasks/main.yml (Req 6.1, 6.2)."
        )
