"""Composition-root / swap-gate phase-gating test for project-consumer-onboarding.

Feature: project-consumer-onboarding (tasks.md Task 9.1; design.md "Testing
Strategy / Composition-root / swap-gate test (orchestrator phase gating)";
Requirements 2.3, 4.4, 4.5, 4.6, 5.1, 5.2).

=== WHAT THIS TEST IS (AND WHY IT IS OFFLINE) ===

This is the MANDATORY composition-root test (``composition-wiring.md``,
``testing-strategy.md`` §4) applied to the Consumer_Onboarding_Orchestrator's
PHASE GATING — the seam where a failure fails CLOSED rather than letting a later
phase run against an absent/half-wired dependency. It is the structural analogue
of the sibling ``test_workload_auth_swap_gate.py`` / ``test_svc01_swap_gate.py``:
rather than a live run (which would need a real Proxmox cluster, a live OpenBao,
and a live ZITADEL — all ``requires-infra``), it PARSES the orchestrator play and
the five ``consumer_*`` roles OFFLINE with PyYAML and asserts the fail-closed
phase-gating shape is a PROPERTY of the rendered topology:

  * the four phases are present and sequenced in the fixed order
    preflight -> provision -> bootstrap -> handoff (Req 5.1);
  * each phase is a ``block``/``rescue`` whose rescue ``fail``s naming the phase,
    AND the play sets ``any_errors_fatal: true`` — so a failure at any phase
    aborts the run before the next phase's ``import_role`` executes (Req 5.2);
  * therefore a Phase-1 (preflight) failure leaves Phase-2 Terraform UNTOUCHED
    (the ``consumer_provision`` role — the only phase that composes
    ``svc-01-bootstrap.yml`` / ``terraform`` — sits in a LATER play task than the
    preflight gate), and a Phase-2 failure never reaches the Phase-3 onboarders
    (asserted by task ordering + any_errors_fatal);
  * an operator-supplied ``vlan_id`` is IGNORED in favour of the
    Registry-resolved value: the play declares no ``vlan_id`` input it feeds to a
    mutating phase, preflight WARNs that an operator ``vlan_id`` is ignored, and
    the authoritative ``consumer_vlan_id`` fact is bound ONLY from the
    ``consumer_registry.py`` resolver output (Req 2.3);
  * the orchestrator COMPOSES the onboarders (``include_role`` /
    ``import_role`` / child ``ansible-playbook`` run) rather than duplicating
    their logic — it imports ``openbao_project_onboard`` /
    ``openbao_workload_approle`` / ``zitadel-org-provision`` and child-runs
    ``svc-01-bootstrap.yml``, and inlines NO raw ``bao``/ZITADEL-API call of its
    own (Req 4.4, 4.5, 4.6).

Everything here reads YAML files on disk; nothing starts a container, contacts a
cluster, or runs ansible. It is collected under the default offline gate.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_consumer_swap_gate.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "project-consumer-bootstrap.yml"
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"

# The five consumer_* roles the orchestrator composes.
_PREFLIGHT = _ROLES_DIR / "consumer_preflight" / "tasks" / "main.yml"
_PROVISION = _ROLES_DIR / "consumer_provision" / "tasks" / "main.yml"
_BOOTSTRAP = _ROLES_DIR / "consumer_bootstrap" / "tasks" / "main.yml"
_HANDOFF = _ROLES_DIR / "consumer_handoff" / "tasks" / "main.yml"
_BOOTSTRAP_DEFAULTS = _ROLES_DIR / "consumer_bootstrap" / "defaults" / "main.yml"


# --------------------------------------------------------------------------- #
# Helpers — load the play / role task lists.
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path):
    assert path.is_file(), f"expected {path} to exist"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _text(path: Path) -> str:
    assert path.is_file(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


def _play() -> dict:
    doc = _load_yaml(_PLAYBOOK)
    assert isinstance(doc, list) and len(doc) == 1, (
        "project-consumer-bootstrap.yml must be a single-play playbook"
    )
    return doc[0]


def _play_tasks() -> list:
    tasks = _play().get("tasks")
    assert isinstance(tasks, list) and tasks, "the orchestrator play must have tasks"
    return tasks


def _task_name(task: dict) -> str:
    return str(task.get("name", ""))


def _imported_role_in_block(task: dict) -> str | None:
    """Return the role name a phase block imports, if the task is a phase block
    wrapping exactly one ``import_role`` (FQCN ``ansible.builtin.import_role`` or
    the short ``import_role`` key)."""
    block = task.get("block")
    if not isinstance(block, list):
        return None
    for inner in block:
        imp = inner.get("import_role") or inner.get("ansible.builtin.import_role")
        if isinstance(imp, dict) and "name" in imp:
            return imp["name"]
    return None


def _non_comment_text(path: Path) -> str:
    """Return a role's task text with YAML comment lines stripped, so a forbidden
    pattern mentioned only in an explanatory ``# ...`` comment does not trip the
    'no raw inlined call' assertions — those target real invocations, not the
    docstring-style comments that explain WHY the role avoids them."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        # drop trailing inline comments too (best-effort; safe for these files).
        lines.append(raw)
    return "\n".join(lines)


# =========================================================================== #
# Play-level fail-closed guarantees.
# =========================================================================== #
def test_play_sets_any_errors_fatal():
    """any_errors_fatal:true is what guarantees a failed phase aborts the whole
    run before the next phase begins (Req 5.2)."""
    assert _play().get("any_errors_fatal") is True, (
        "the orchestrator play MUST set any_errors_fatal: true so a phase "
        "failure halts before any later phase runs (Req 5.2)"
    )


def test_play_runs_on_localhost_connection_local():
    """Day-0 orchestrator shape (service-installer.md §2.1): hosts localhost,
    connection local."""
    play = _play()
    assert play.get("hosts") == "localhost"
    assert play.get("connection") == "local"


def test_four_phases_present_in_fixed_order():
    """The four phase blocks run in the fixed order preflight -> provision ->
    bootstrap -> handoff, each importing its own role (Req 5.1)."""
    # The expected (role, when-not-clean-slate) phase sequence.
    expected_roles = [
        "consumer_preflight",
        "consumer_provision",
        "consumer_bootstrap",
        "consumer_handoff",
    ]
    # Walk the play tasks in order, collecting the imported role of each phase
    # block (skipping the clean-slate branch, which imports consumer_clean_slate).
    phase_role_sequence = []
    for task in _play_tasks():
        role = _imported_role_in_block(task)
        if role in expected_roles:
            phase_role_sequence.append(role)
    assert phase_role_sequence == expected_roles, (
        "the four onboard phases must appear in the fixed order preflight -> "
        f"provision -> bootstrap -> handoff (Req 5.1); got {phase_role_sequence}"
    )


def test_each_phase_block_has_a_phase_naming_rescue():
    """Every phase block has a rescue that fails naming its phase, so a failure
    is a fail-closed halt with a diagnostic (Req 5.2, design Error Handling)."""
    phase_roles = {
        "consumer_preflight": "Phase 1",
        "consumer_provision": "Phase 2",
        "consumer_bootstrap": "Phase 3",
        "consumer_handoff": "Phase 4",
    }
    seen = {}
    for task in _play_tasks():
        role = _imported_role_in_block(task)
        if role not in phase_roles:
            continue
        rescue = task.get("rescue")
        assert isinstance(rescue, list) and rescue, (
            f"phase importing {role} must have a rescue block (fail-closed)"
        )
        # the rescue must contain a fail: naming the phase.
        rescue_text = yaml.safe_dump(rescue)
        assert "ansible.builtin.fail" in rescue_text or "fail:" in rescue_text, (
            f"the {role} phase rescue must `fail` (abort non-zero)"
        )
        assert phase_roles[role] in rescue_text, (
            f"the {role} phase rescue diagnostic must name '{phase_roles[role]}'"
        )
        seen[role] = True
    assert set(seen) == set(phase_roles), (
        f"every phase must have a naming rescue; saw {sorted(seen)}"
    )


# =========================================================================== #
# Phase-gating: a failure at each phase means no later phase executes.
# This is proven structurally: the only phase that touches Terraform / child
# svc-01-bootstrap is consumer_provision, and it is a strictly LATER play task
# than the preflight gate; combined with any_errors_fatal a preflight failure
# can never reach it. Likewise provision precedes the bootstrap onboarders.
# =========================================================================== #
def _phase_task_index(role: str) -> int:
    for i, task in enumerate(_play_tasks()):
        if _imported_role_in_block(task) == role:
            return i
    raise AssertionError(f"phase importing {role} not found in the play")


def test_preflight_failure_leaves_provision_untouched():
    """Phase-1 (preflight) runs strictly BEFORE Phase-2 (provision); with
    any_errors_fatal a preflight failure aborts before the Terraform-touching
    provision phase's import_role executes (Req 5.2)."""
    assert _phase_task_index("consumer_preflight") < _phase_task_index(
        "consumer_provision"
    ), "preflight must precede provision so a preflight failure skips provision"
    # And the Terraform / child-run composition lives ONLY in consumer_provision,
    # never in consumer_preflight (preflight is a read-only gate). Check the
    # non-comment task text so an explanatory comment can't trip the assert.
    pre = _non_comment_text(_PREFLIGHT)
    assert "terraform" not in pre.lower(), (
        "consumer_preflight must perform NO terraform action (read-only gate); a "
        "preflight failure must leave Phase-2 Terraform untouched (Req 5.3)"
    )
    assert "svc-01-bootstrap.yml" not in pre, (
        "consumer_preflight must not compose svc-01-bootstrap.yml (provision does)"
    )


def test_provision_failure_never_reaches_bootstrap_onboarders():
    """Phase-2 (provision) runs strictly BEFORE Phase-3 (bootstrap); a provision
    failure aborts before the OpenBao/ZITADEL onboarders run (Req 5.2)."""
    assert _phase_task_index("consumer_provision") < _phase_task_index(
        "consumer_bootstrap"
    ), "provision must precede bootstrap so a provision failure skips the onboarders"
    # And the OpenBao/ZITADEL compose points live ONLY in consumer_bootstrap.
    prov = _non_comment_text(_PROVISION)
    assert "openbao_project_onboard" not in prov and "zitadel-org-provision" not in prov, (
        "consumer_provision must not run the OpenBao/ZITADEL onboarders — those "
        "are Phase-3; a Phase-2 failure must never reach them (Req 5.2)"
    )


def test_bootstrap_precedes_handoff():
    """Phase-3 (bootstrap) runs strictly before Phase-4 (handoff) (Req 5.1)."""
    assert _phase_task_index("consumer_bootstrap") < _phase_task_index(
        "consumer_handoff"
    )


# =========================================================================== #
# Req 2.3 — operator-supplied vlan_id is IGNORED; Registry is authoritative.
# =========================================================================== #
def test_play_declares_no_vlan_id_fed_to_a_mutating_phase():
    """The play's vars default vlan_id is NOT present as an authoritative input:
    the authoritative fact is consumer_vlan_id, bound only in preflight from the
    Registry resolver. The play must not pass an operator vlan_id to provision
    (Req 2.3)."""
    play = _play()
    play_vars = play.get("vars") or {}
    # The play must not establish vlan_id as an operator-overridable play var that
    # feeds the mutating phases; the only VLAN fact is Registry-derived.
    assert "vlan_id" not in play_vars, (
        "the orchestrator play must NOT declare a vlan_id var — vlan_id is "
        "resolved from the Registry only (Req 2.3)"
    )


def test_preflight_warns_operator_vlan_id_is_ignored():
    """consumer_preflight WARNs that an operator-supplied vlan_id is ignored
    (Req 2.3)."""
    pre = _text(_PREFLIGHT)
    assert "IGNORED" in pre and "Req 2.3" in pre, (
        "consumer_preflight must WARN that an operator-supplied vlan_id is "
        "IGNORED (Req 2.3)"
    )
    # the warning is conditioned on an operator having supplied one.
    assert "consumer_operator_supplied_vlan_id" in pre or "vlan_id | default" in pre, (
        "the preflight must detect an operator-supplied vlan_id to warn about it"
    )


def test_vlan_id_fact_is_bound_only_from_the_registry_resolver():
    """The authoritative consumer_vlan_id fact is derived SOLELY from the
    consumer_registry.py resolver stdout — never from an operator input
    (Req 2.1, 2.3)."""
    pre = _text(_PREFLIGHT)
    assert "consumer_registry.py" in pre or "consumer_registry_helper" in pre, (
        "preflight must resolve vlan_id by composing consumer_registry.py"
    )
    assert "consumer_vlan_id" in pre, (
        "preflight must bind the authoritative consumer_vlan_id fact"
    )
    # the binding must come from the resolver's registered stdout, not from a
    # vlan_id operator variable.
    assert "consumer_registry_resolve.stdout" in pre, (
        "consumer_vlan_id must be extracted from the Registry resolver's stdout "
        "(Registry-authoritative, Req 2.3)"
    )


# =========================================================================== #
# Req 4.4-4.6 — COMPOSITION, not reimplementation.
# =========================================================================== #
def test_provision_composes_svc01_bootstrap_child_run():
    """Phase 2 composes svc-01-bootstrap.yml as a child ansible-playbook run
    rather than reimplementing the SVC-01 provisioning (Req 4.4)."""
    prov = _text(_PROVISION)
    assert "svc-01-bootstrap.yml" in prov or "consumer_svc01_bootstrap_playbook" in prov, (
        "consumer_provision must COMPOSE the svc-01-bootstrap.yml child run (Req 4.4)"
    )
    assert "ansible-playbook" in prov, (
        "the SVC-01 composition must be a child ansible-playbook run (Req 4.4)"
    )


def test_bootstrap_includes_the_three_onboarders():
    """Phase 3 composes the three onboarders by include/child-run (Req 4.5, 4.6):
    openbao_project_onboard + openbao_workload_approle (include_role) and
    zitadel-org-provision (child playbook run)."""
    boot = _text(_BOOTSTRAP)
    defaults = _text(_BOOTSTRAP_DEFAULTS)
    # OpenBao onboarders composed via include_role.
    assert "include_role" in boot, (
        "consumer_bootstrap must compose the OpenBao onboarders via include_role "
        "(Req 4.6)"
    )
    assert "openbao_project_onboard" in defaults or "openbao_project_onboard" in boot, (
        "consumer_bootstrap must compose openbao_project_onboard (Req 4.6)"
    )
    assert "openbao_workload_approle" in defaults or "openbao_workload_approle" in boot, (
        "consumer_bootstrap must compose openbao_workload_approle (Req 4.6)"
    )
    # ZITADEL onboarder composed via its own playbook child run.
    assert "zitadel-org-provision" in defaults or "zitadel-org-provision" in boot, (
        "consumer_bootstrap must compose zitadel-org-provision (Req 4.5)"
    )


def test_bootstrap_does_not_inline_raw_bao_or_zitadel_api_calls():
    """The orchestrator must NOT duplicate onboarder logic — it inlines no raw
    OpenBao write (`bao kv put` / `bao policy write` / `bao auth enable`) and no
    raw ZITADEL management-API mutation of its own (Req 4.4-4.6).

    The only `bao` invocations allowed in the bootstrap/handoff roles are
    READ-ONLY verifications (`bao kv get`, `bao policy read`) used for the
    resumable check-then-verify + the health poll — never a mutating write."""
    for path in (_BOOTSTRAP, _HANDOFF):
        text = _non_comment_text(path)
        # Forbidden mutating OpenBao verbs inlined directly in the orchestrator.
        for forbidden in ("kv put", "policy write", "auth enable", "write auth/"):
            assert forbidden not in text, (
                f"{path.name} must not inline a raw mutating OpenBao call "
                f"('{forbidden}') — that logic belongs to the composed onboarder "
                "(Req 4.6, composition not reimplementation)"
            )
        # No raw ZITADEL management API mutation (POST to the mgmt API) inlined.
        assert "management/v1" not in text, (
            f"{path.name} must not inline a raw ZITADEL management API call — the "
            "zitadel-org-provision onboarder owns that (Req 4.5)"
        )


def test_clean_slate_orchestrates_onboarders_own_reset_not_cross_boundary():
    """The clean-slate role composes each onboarder's OWN reset (ADR-0011) and
    never reaches across a boundary with raw pct/docker/terraform-state surgery
    (Req 9.5, 9.6)."""
    cs_full = _text(_ROLES_DIR / "consumer_clean_slate" / "tasks" / "main.yml")
    cs = _non_comment_text(_ROLES_DIR / "consumer_clean_slate" / "tasks" / "main.yml")
    assert "svc-01-bootstrap.yml" in cs and "clean_slate=true" in cs, (
        "consumer_clean_slate must compose the DB's own svc-01-bootstrap.yml "
        "clean-slate (ADR-0011, Req 9.5)"
    )
    # No raw cross-boundary destruction inlined (comments explaining the ban are
    # fine — only a real inlined invocation is forbidden).
    for forbidden in ("pct destroy", "terraform state rm", "docker volume rm"):
        assert forbidden not in cs, (
            f"consumer_clean_slate must not inline a raw '{forbidden}' across a "
            "boundary it does not own (Req 9.6)"
        )
    # Req 7.7 — never removes the Registry entry or the <slug>-infra slice.
    assert "Req 7.7" in cs_full or "infra network slice" in cs_full or "<slug>-infra" in cs_full, (
        "consumer_clean_slate must preserve the Registry entry + <slug>-infra "
        "slice (Req 7.7)"
    )
