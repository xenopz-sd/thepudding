"""Hypothesis property-based tests for the openbao-workload-auth pure module.

Feature: openbao-workload-auth (tasks.md Task 1.6–1.12; design.md "Correctness
Properties", Properties 1–7).

One property-based test per correctness property. Each runs a minimum of 100
iterations (``@settings(max_examples=200)``, well above the floor) and is tagged
with its property number and text per the workflow convention:
``# Feature: openbao-workload-auth, Property N: <property text>``.

All offline: the module under test (``scripts/installer/workload_auth.py``) is
pure, stdlib-only, Ansible-free and OpenBao-free, so these need no live Primary
and run under the repo's default offline pytest invocation
(``-m "not requires_infra"``).
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from workload_auth import (
    WORKLOAD_SUFFIX,
    AgentConfig,
    SecretMapping,
    ValidationError,
    render_agent_hcl,
    validate_secret_id_inputs,
    validate_token_ttls,
    workload_role_name,
)

# A broad integer range spanning negatives, zero, and large positives so the
# accept/reject boundary is exercised from both sides.
_ints = st.integers(min_value=-10, max_value=1_000_000)

# Valid slug strings: lowercase alnum + hyphen, non-empty, no leading/trailing
# hyphen (mirrors the platform slug convention; kept simple for the renderer).
_slug_chars = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=20
).filter(lambda s: not s.startswith("-") and not s.endswith("-") and s)

# KV-path / field / env-key tokens: identifier-ish, no quotes or braces so the
# rendered directive stays well-formed and parseable by the assertions below.
_token = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=16,
)

_paths = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789/_-.",
    min_size=1,
    max_size=40,
).map(lambda p: "/" + p.strip("/") if not p.startswith("/") else p)

_addrs = st.builds(
    lambda host, port: f"https://{host}:{port}",
    host=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-", min_size=1, max_size=20),
    port=st.integers(min_value=1, max_value=65535),
)


def _mapping() -> st.SearchStrategy[SecretMapping]:
    return st.builds(SecretMapping, kv_subpath=_token, kv_field=_token, env_key=_token)


def _config(
    *,
    mappings: st.SearchStrategy | None = None,
    tls: st.SearchStrategy | None = None,
    role_id_path: st.SearchStrategy | None = None,
    secret_id_path: st.SearchStrategy | None = None,
    addr: st.SearchStrategy | None = None,
) -> st.SearchStrategy[AgentConfig]:
    return st.builds(
        AgentConfig,
        project_slug=_slug_chars,
        primary_addr=addr or _addrs,
        role_id_file_path=role_id_path or _paths,
        secret_id_file_path=secret_id_path or _paths,
        token_sink_path=_paths,
        rendered_secret_file=_paths,
        secret_mappings=mappings or st.lists(_mapping(), min_size=1, max_size=6),
        tls_skip_verify=tls or st.booleans(),
        num_retries=st.integers(min_value=0, max_value=10),
    )


# =============================================================================
# Task 1.6 — Property 1
# Feature: openbao-workload-auth, Property 1: Token TTL-pair validation accepts
# exactly the valid region
# Validates: Requirements 2.2, 2.3
# =============================================================================
@settings(max_examples=200)
@given(token_ttl=_ints, token_max_ttl=_ints)
def test_property1_token_ttl_pair_valid_region(token_ttl, token_max_ttl):
    expected_valid = token_ttl > 0 and token_max_ttl > 0 and token_ttl <= token_max_ttl
    if expected_valid:
        assert validate_token_ttls(token_ttl, token_max_ttl) == (
            token_ttl,
            token_max_ttl,
        )
    else:
        try:
            validate_token_ttls(token_ttl, token_max_ttl)
        except ValidationError:
            pass
        else:
            raise AssertionError(
                f"expected rejection for ({token_ttl}, {token_max_ttl})"
            )


# =============================================================================
# Task 1.7 — Property 2
# Feature: openbao-workload-auth, Property 2: Secret-ID input validation accepts
# exactly the valid region
# Validates: Requirements 3.7
# =============================================================================
@settings(max_examples=200)
@given(secret_id_ttl=_ints, secret_id_num_uses=_ints)
def test_property2_secret_id_inputs_valid_region(secret_id_ttl, secret_id_num_uses):
    # num_uses == 0 is ACCEPTED (unlimited); ttl must be strictly positive.
    expected_valid = secret_id_ttl > 0 and secret_id_num_uses >= 0
    if expected_valid:
        assert validate_secret_id_inputs(secret_id_ttl, secret_id_num_uses) == (
            secret_id_ttl,
            secret_id_num_uses,
        )
    else:
        try:
            validate_secret_id_inputs(secret_id_ttl, secret_id_num_uses)
        except ValidationError:
            pass
        else:
            raise AssertionError(
                f"expected rejection for ({secret_id_ttl}, {secret_id_num_uses})"
            )


# =============================================================================
# Task 1.8 — Property 3
# Feature: openbao-workload-auth, Property 3: Workload role name is the slug plus
# the `-workload` suffix
# Validates: Requirements 2.4
# =============================================================================
@settings(max_examples=200)
@given(slug=_slug_chars)
def test_property3_workload_role_name_suffix(slug):
    name = workload_role_name(slug)
    assert name == f"{slug}{WORKLOAD_SUFFIX}"
    assert name.endswith("-workload")
    assert name != f"{slug}-ci"


# =============================================================================
# Task 1.9 — Property 4
# Feature: openbao-workload-auth, Property 4: Rendered `agent.hcl` contains the
# AppRole auto_auth stanza with the given file paths
# Validates: Requirements 4.1, 4.4
# =============================================================================
@settings(max_examples=200)
@given(
    cfg=_config(),
)
def test_property4_auto_auth_paths_and_vault_address(cfg):
    out = render_agent_hcl(cfg)
    # exactly one auto_auth block and one method "approle"
    assert out.count("auto_auth {") == 1
    assert out.count('method "approle" {') == 1
    # the given file paths appear exactly as supplied
    assert f'role_id_file_path   = "{cfg.role_id_file_path}"' in out
    assert f'secret_id_file_path = "{cfg.secret_id_file_path}"' in out
    # vault.address equals the given primary address
    assert f'address = "{cfg.primary_addr}"' in out


# =============================================================================
# Task 1.10 — Property 5
# Feature: openbao-workload-auth, Property 5: Rendered `agent.hcl` has exactly one
# template stanza per secret mapping
# Validates: Requirements 4.2
# =============================================================================
@settings(max_examples=200)
@given(cfg=_config())
def test_property5_one_template_stanza_per_mapping(cfg):
    out = render_agent_hcl(cfg)
    # exactly len(mappings) template stanzas
    assert out.count("template {") == len(cfg.secret_mappings)
    for m in cfg.secret_mappings:
        kv_ref = f'secret/data/{cfg.project_slug}/{m.kv_subpath}'
        assert kv_ref in out
        # each stanza emits its env_key into the single rendered file
        assert f"{m.env_key}={{{{ .Data.data.{m.kv_field} }}}}" in out
    # all stanzas target the one Rendered_Secret_File destination
    assert out.count(f'destination = "{cfg.rendered_secret_file}"') == len(
        cfg.secret_mappings
    )


# =============================================================================
# Task 1.11 — Property 6
# Feature: openbao-workload-auth, Property 6: The `tls_skip_verify` toggle is
# rendered iff enabled
# Validates: Requirements 4.5
# =============================================================================
@settings(max_examples=200)
@given(cfg=_config())
def test_property6_tls_skip_verify_iff_enabled(cfg):
    out = render_agent_hcl(cfg)
    if cfg.tls_skip_verify:
        assert "tls_skip_verify = true" in out
    else:
        assert "tls_skip_verify" not in out


# =============================================================================
# Task 1.12 — Property 7
# Feature: openbao-workload-auth, Property 7: The `agent.hcl` renderer is
# deterministic (idempotent render)
# Validates: Requirements 4.6
# =============================================================================
_WS = re.compile(r"\s+")


@settings(max_examples=200)
@given(cfg=_config())
def test_property7_renderer_deterministic(cfg):
    first = render_agent_hcl(cfg)
    second = render_agent_hcl(cfg)
    # byte-identical on repeat
    assert first == second
    # and after whitespace normalisation (the design's explicit phrasing)
    assert _WS.sub(" ", first).strip() == _WS.sub(" ", second).strip()
