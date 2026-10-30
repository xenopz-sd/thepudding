"""Local Test_Slices for the ``infra/tests`` pytest root.

Feature: ci-offline-gate-hardening (design §Components 4.3, §Data Models 5.2).

WHY THIS EXISTS. ``infra/tests`` is large enough that a whole-tree
``pytest infra/tests`` run exceeds a routine 120s local command budget, so an
agent/developer running it by hand had to hand-pick files. This module is the
SINGLE SOURCE OF TRUTH for a stable set of named slices that PARTITION the whole
tree — each small enough to run within budget, and together covering EXACTLY the
same files as one ``pytest infra/tests`` run.

LOCAL ONLY. CI is unchanged — the ``pytest:infra`` job still runs the whole tree in
one invocation (ci-offline-gate-hardening Req 5.1). Slices appear in NO
``.gitlab-ci.yml`` job. The documented per-slice commands live in
``docs/TESTING.md`` → "Local slices for infra/tests".

INVARIANT (enforced by ``test_slice_completeness.py``):
  * ``union(SLICES.values())`` == every ``test_*.py`` on disk in ``infra/tests``
  * every slice is non-empty
  * no file appears in two slices
So adding a test file later without slotting it into exactly one slice FAILS that
guard offline (slice-drift catch).

Run a slice (single-root invocation — preserves the Four_Root_Rule):
    ~/venv/devinfra/bin/python -m pytest $(python -c \
      "import infra.tests.slices as s; print(' '.join('infra/tests/'+f for f in s.SLICES['svc07a']))")
or, more simply, the explicit command documented per slice in docs/TESTING.md.

Two slices (``core``) carry the branch-context meta-tests
(``test_accessible_root_readme_links.py``, ``test_offline_plan_preservation.py``);
on a FEATURE branch add the same ``--deselect`` node-ids CI uses (see
docs/TESTING.md). On ``main`` they run with no deselection.
"""

from __future__ import annotations

#: slice name -> the ``test_*.py`` basenames (under ``infra/tests/``) in that slice.
#: A partition of the whole tree; the authoritative definition rendered as a table
#: in docs/TESTING.md. Each slice runs within the ~120s Routine_Budget EXCEPT the
#: single-file ``plan-preservation`` slice: ``test_offline_plan_preservation.py``
#: spawns a full child pytest + terraform-plan baseline and legitimately takes a few
#: minutes. It is isolated in its own slice so the other eight slices stay fast; run
#: it with a longer timeout (it is fine in CI, which has no per-command budget).
SLICES: dict[str, list[str]] = {
    "svc07a": [
        "test_openbao_unsealer_bootstrap.py",
        "test_sdn_gateway_openbao_unsealer_scoping.py",
        "test_svc07_clean_slate_preservation.py",
        "test_svc07_clean_slate_state_residue.py",
        "test_svc07_openbao_admin_token_mint.py",
        "test_svc07_openbao_alerting.py",
        "test_svc07_openbao_auth_audit.py",
        "test_svc07_openbao_auth_rotate_ingress.py",
        "test_svc07_openbao_autounseal.py",
        "test_svc07_openbao_backup.py",
        "test_svc07_openbao_backup_observability.py",
        "test_svc07_openbao_bootstrap_flow.py",
        "test_svc07_openbao_config_render.py",
    ],
    "svc07b": [
        "test_svc07_openbao_degraded.py",
        "test_svc07_openbao_dynamic_creds.py",
        "test_svc07_openbao_engine_contract.py",
        "test_svc07_openbao_engines.py",
        "test_svc07_openbao_health_defaults_removed.py",
        "test_svc07_openbao_init_unseal_idempotency.py",
        "test_svc07_openbao_install_idempotency.py",
        "test_svc07_openbao_metrics_port_publish.py",
        "test_svc07_openbao_onboard_contract.py",
        "test_svc07_openbao_prometheus_scrape.py",
        "test_svc07_openbao_rotate_root.py",
        "test_svc07_state_and_degraded.py",
        "test_svc07_terraform_shape.py",
    ],
    "svc06": [
        "test_svc06_clean_slate_backing_db_residue.py",
        "test_svc06_clean_slate_provision_token_residue.py",
        "test_svc06_org_provision_delegate_to.py",
        "test_svc06_provision_token_wiring.py",
        "test_svc06_terraform_shape.py",
        "test_svc06_zitadel_contract.py",
        "test_svc06_zitadel_db_password_bug_condition.py",
        "test_svc06_zitadel_db_password_preservation.py",
    ],
    "obs": [
        "test_host_indices_obs.py",
        "test_obs_ac3_targets_up.py",
        "test_obs_bootstrap_token_bridging.py",
        "test_obs_loki_render.py",
        "test_obs_offline_plan.py",
        "test_obs_prometheus_render.py",
        "test_obs_scrape_multi_inventory.py",
        "test_obs_scrape_targets.py",
        "test_obs_terraform_shape.py",
    ],
    "svc01-09": [
        "test_svc01_destroy_isolation.py",
        "test_svc01_openbao_token_failfast.py",
        "test_svc01_postgres_contract.py",
        "test_svc01_terraform_shape.py",
        "test_svc09_terraform_shape.py",
        "test_svc09_traefik_config_render.py",
    ],
    "core": [
        "test_accessible_root_readme_links.py",
        "test_discover_tf_roots.py",
        "test_slice_completeness.py",
    ],
    "tfplan-a": [
        "test_destroy_isolation.py",
        "test_integration_apply.py",
        "test_live_apply_bug_condition.py",
        "test_plan_idempotency.py",
        "test_terraform_clean_slate.py",
    ],
    "tfplan-b": [
        "test_consumer_db_offline_plan.py",
        "test_degraded_modes.py",
        "test_degraded_sdn_use.py",
        "test_offline_plan_bug_condition.py",
        "test_state_and_registry.py",
        "test_terraform_shape.py",
    ],
    "plan-preservation": [
        "test_offline_plan_preservation.py",
    ],
}

#: Slices that contain the two branch-context meta-tests, which must be deselected
#: on a feature branch (they FAIL by construction off `main`). docs/TESTING.md shows
#: the exact --deselect node-ids for these.
META_TEST_FILES = (
    "test_accessible_root_readme_links.py",
    "test_offline_plan_preservation.py",
)
