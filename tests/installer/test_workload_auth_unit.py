"""Offline unit tests for the openbao-workload-auth pure module.

Feature: openbao-workload-auth (tasks.md Task 1.4, 1.5).

Covers the example/edge-case half of the pure module
(``scripts/installer/workload_auth.py``) — the property-based half lives in
``test_workload_auth_properties.py``. Both are offline, stdlib + Hypothesis only,
no Ansible and no live OpenBao Primary.

* Task 1.4 — unit tests for the TTL-pair validator, the Secret_ID-input
  validator, and ``workload_role_name``. Requirements 2.2, 2.3, 2.4, 3.7.
* Task 1.5 — golden-file test for ``render_agent_hcl`` (one slug, two mappings,
  ``tls_skip_verify`` on) against a committed golden file. Requirements 4.1,
  4.2, 4.4, 4.5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workload_auth import (
    AgentConfig,
    SecretMapping,
    ValidationError,
    render_agent_hcl,
    validate_secret_id_inputs,
    validate_token_ttls,
    workload_role_name,
)

_GOLDEN = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "openbao_workload_auth"
    / "agent.hcl.golden"
)


# ---------------------------------------------------------------------------
# Task 1.4 — TTL-pair validator boundaries (Requirements 2.2, 2.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token_ttl, token_max_ttl",
    [
        (1, 1),            # equal, minimal positive -> accept
        (3600, 86400),     # ttl < max_ttl -> accept
        (86400, 86400),    # equal large -> accept
    ],
)
def test_token_ttls_accept_valid_region(token_ttl, token_max_ttl):
    assert validate_token_ttls(token_ttl, token_max_ttl) == (
        token_ttl,
        token_max_ttl,
    )


@pytest.mark.parametrize(
    "token_ttl, token_max_ttl",
    [
        (0, 3600),      # ttl == 0 -> reject
        (3600, 0),      # max_ttl == 0 -> reject
        (0, 0),         # both zero -> reject
        (-1, 3600),     # negative ttl -> reject
        (3600, -1),     # negative max_ttl -> reject
        (86401, 86400), # ttl > max_ttl -> reject
    ],
)
def test_token_ttls_reject_outside_region(token_ttl, token_max_ttl):
    with pytest.raises(ValidationError):
        validate_token_ttls(token_ttl, token_max_ttl)


def test_token_ttls_reject_bool_input():
    # bool is an int subclass; a True/False TTL is a caller error, not 1/0.
    with pytest.raises(ValidationError):
        validate_token_ttls(True, 3600)


# ---------------------------------------------------------------------------
# Task 1.4 — Secret_ID-input validator boundaries (Requirements 3.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_id_ttl, secret_id_num_uses",
    [
        (86400, 0),   # num_uses == 0 (unlimited) -> ACCEPT
        (1, 0),       # minimal positive ttl, unlimited uses -> accept
        (86400, 5),   # positive ttl, bounded uses -> accept
    ],
)
def test_secret_id_inputs_accept_valid_region(secret_id_ttl, secret_id_num_uses):
    assert validate_secret_id_inputs(secret_id_ttl, secret_id_num_uses) == (
        secret_id_ttl,
        secret_id_num_uses,
    )


@pytest.mark.parametrize(
    "secret_id_ttl, secret_id_num_uses",
    [
        (0, 5),     # ttl == 0 -> reject
        (-1, 5),    # negative ttl -> reject
        (86400, -1),  # negative num_uses -> reject
    ],
)
def test_secret_id_inputs_reject_outside_region(secret_id_ttl, secret_id_num_uses):
    with pytest.raises(ValidationError):
        validate_secret_id_inputs(secret_id_ttl, secret_id_num_uses)


# ---------------------------------------------------------------------------
# Task 1.4 — role-name computation (Requirements 2.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug, expected",
    [
        ("alpha", "alpha-workload"),
        ("drone-ctl", "drone-ctl-workload"),
        ("svc07", "svc07-workload"),
    ],
)
def test_workload_role_name_concrete_slugs(slug, expected):
    result = workload_role_name(slug)
    assert result == expected
    assert result.endswith("-workload")
    # must never collide with the sibling CI JWT role name
    assert result != f"{slug}-ci"


def test_workload_role_name_rejects_empty_slug():
    with pytest.raises(ValidationError):
        workload_role_name("")


# ---------------------------------------------------------------------------
# Task 1.5 — golden-file renderer test (Requirements 4.1, 4.2, 4.4, 4.5)
# ---------------------------------------------------------------------------


def _golden_config() -> AgentConfig:
    return AgentConfig(
        project_slug="demo-slug",
        primary_addr="https://10.0.20.10:8200",
        role_id_file_path="/openbao/agent/role-id",
        secret_id_file_path="/openbao/agent/secret-id",
        token_sink_path="/openbao/agent/token",
        rendered_secret_file="/openbao/agent/rendered/secrets.env",
        secret_mappings=[
            SecretMapping("db", "password", "DB_PASSWORD"),
            SecretMapping("oidc", "client_secret", "OIDC_CLIENT_SECRET"),
        ],
        tls_skip_verify=True,
    )


def test_render_agent_hcl_matches_golden_file():
    rendered = render_agent_hcl(_golden_config())
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert rendered == golden, (
        "rendered agent.hcl drifted from the committed golden file; if the "
        "render change is intentional, regenerate "
        "tests/installer/fixtures/openbao_workload_auth/agent.hcl.golden"
    )


def test_render_agent_hcl_templates_no_secret_value():
    # The renderer emits only KV references and paths, never a secret value.
    rendered = render_agent_hcl(_golden_config())
    # The literal consul-template directive is present (the Agent expands it at
    # runtime); no rendered password/secret value is in the config text.
    assert 'with secret \\"secret/data/demo-slug/db\\"' in rendered
    assert "DB_PASSWORD={{ .Data.data.password }}" in rendered


def test_render_agent_hcl_requires_non_empty_mappings():
    cfg = AgentConfig(
        project_slug="demo-slug",
        primary_addr="https://10.0.20.10:8200",
        role_id_file_path="/openbao/agent/role-id",
        secret_id_file_path="/openbao/agent/secret-id",
        token_sink_path="/openbao/agent/token",
        rendered_secret_file="/openbao/agent/rendered/secrets.env",
        secret_mappings=[],
    )
    with pytest.raises(ValidationError):
        render_agent_hcl(cfg)
