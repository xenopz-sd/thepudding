"""Offline role-template verification for the openbao-agent data-plane role.

Feature: openbao-workload-auth (tasks.md Task 4.2, 4.4; Task 6.2 contract).

Renders ``ansible/roles/openbao-agent/templates/agent.hcl.j2`` with the SAME
fixture the pure-renderer golden file uses (``demo-slug`` + two mappings +
``tls_skip_verify`` on) and asserts the Jinja output equals the committed
golden ``tests/installer/fixtures/openbao_workload_auth/agent.hcl.golden``
byte-for-byte. This is the design's "keep the template structurally equivalent
to the pure renderer so the golden-file test cross-checks it" guarantee
(design.md "Open risks and trade-offs"): the Jinja template and
``render_agent_hcl`` are two implementations of one format, pinned to a single
golden.

Offline, stdlib + Jinja2 only (Jinja2 ships with Ansible, already in the venv);
no Ansible runtime and no live OpenBao Primary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = (
    _REPO_ROOT
    / "ansible"
    / "roles"
    / "openbao-agent"
    / "templates"
    / "agent.hcl.j2"
)
_GOLDEN = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "openbao_workload_auth"
    / "agent.hcl.golden"
)


def _render_template(context: dict) -> str:
    # trim_blocks + lstrip_blocks mirror Ansible's default Jinja environment, so
    # the `{% if %}` / `{% for %}` control lines do not leave stray blank lines.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # Minimal `| bool` / `| int` filter shims matching Ansible's semantics for
    # the values used in the template.
    env.filters["bool"] = lambda v: str(v).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ) or v is True
    env.filters["int"] = lambda v: int(v)
    template = env.get_template(_TEMPLATE.name)
    return template.render(**context)


def _golden_context() -> dict:
    """The fixture matching the pure-renderer golden (demo-slug, two mappings)."""
    return {
        "project_slug": "demo-slug",
        "openbao_primary_addr": "https://10.0.20.10:8200",
        "openbao_primary_tls_skip_verify": True,
        "openbao_agent_num_retries": 0,
        "openbao_agent_approle_mount": "auth/approle",
        "openbao_agent_role_id_path": "/openbao/agent/role-id",
        "openbao_agent_secret_id_path": "/openbao/agent/secret-id",
        "openbao_agent_remove_secret_id_after_read": False,
        "openbao_agent_token_sink_path": "/openbao/agent/token",
        "openbao_agent_rendered_secret_file": "/openbao/agent/rendered/secrets.env",
        "openbao_agent_pid_file": "/openbao/agent/agent.pid",
        "openbao_agent_secret_mappings": [
            {"kv_subpath": "db", "kv_field": "password", "env_key": "DB_PASSWORD"},
            {
                "kv_subpath": "oidc",
                "kv_field": "client_secret",
                "env_key": "OIDC_CLIENT_SECRET",
            },
        ],
    }


def test_agent_hcl_template_matches_golden_byte_for_byte():
    rendered = _render_template(_golden_context())
    golden = _GOLDEN.read_text(encoding="utf-8")
    assert rendered == golden, (
        "openbao-agent/templates/agent.hcl.j2 drifted from the committed golden "
        "file (and therefore from scripts/installer/workload_auth.py::"
        "render_agent_hcl). The Jinja template and the pure renderer MUST stay "
        "structurally equivalent; reconcile the template to the golden, or if "
        "the render change is intentional regenerate the golden AND the pure "
        "renderer together."
    )


def test_agent_hcl_template_templates_no_secret_value():
    rendered = _render_template(_golden_context())
    # Only the literal consul-template directive + KV reference appear — never a
    # rendered secret value (Req 4.8).
    assert 'with secret \\"secret/data/demo-slug/db\\"' in rendered
    assert "DB_PASSWORD={{ .Data.data.password }}" in rendered
    assert "OIDC_CLIENT_SECRET={{ .Data.data.client_secret }}" in rendered


def test_agent_hcl_template_omits_tls_skip_verify_when_disabled():
    ctx = _golden_context()
    ctx["openbao_primary_tls_skip_verify"] = False
    rendered = _render_template(ctx)
    assert "tls_skip_verify" not in rendered  # Req 4.5 / Property 6


def test_agent_hcl_template_one_stanza_per_mapping():
    rendered = _render_template(_golden_context())
    assert rendered.count("template {") == 2  # Req 4.2 / Property 5
