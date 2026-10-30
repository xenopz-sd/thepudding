"""Offline policy/render contract test for the openbao-workload-auth feature.

Feature: openbao-workload-auth (tasks.md Task 6.2).

Renders ``ansible/roles/openbao-agent/templates/agent.hcl.j2`` with a fixture
slug + two mappings and asserts the contract the design pins (design.md
"agent.hcl template structure" / Properties 4, 5, 6):

* exactly ``secret/data/<slug>/<kv_subpath>`` referenced once per mapping;
* the AppRole ``auto_auth`` ``method "approle"`` stanza carrying the given
  ``role_id_file_path`` / ``secret_id_file_path`` and the ``vault.address``;
* ``tls_skip_verify = true`` present IFF the toggle is on, absent otherwise.

It then CROSS-CHECKS the Jinja output against the pure renderer
``scripts/installer/workload_auth.py::render_agent_hcl`` for byte-for-byte
equivalence (after whitespace normalisation) — the design's "keep the template
structurally equivalent to the pure renderer" guarantee (design.md "Open risks
and trade-offs"). The sibling ``test_openbao_agent_template.py`` already pins the
Jinja render to the committed golden file for the ONE golden fixture; this file
does NOT duplicate that — it REFERENCES the golden as a shared fixture and adds
(a) the per-mapping / auto_auth / tls contract assertions on the Jinja output and
(b) the Jinja↔``render_agent_hcl`` equivalence on a SECOND, independent fixture
(a different slug + three mappings + tls off) so the two implementations are
proven equivalent beyond the single golden example.

Offline: stdlib + Jinja2 (ships with Ansible, already in the venv) + the pure
module imported via the installer conftest path. No Ansible runtime, no live
OpenBao Primary. No secret VALUE is ever rendered or asserted — only file paths,
KV references, and the literal consul-template directives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2")

# scripts/installer is on sys.path via tests/installer/conftest.py.
from workload_auth import AgentConfig, SecretMapping, render_agent_hcl  # noqa: E402

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


# --------------------------------------------------------------------------- #
# Jinja render harness (mirrors Ansible's default env; same shims as the
# sibling golden test, copied locally per the cross-tree import ban).
# --------------------------------------------------------------------------- #
def _render_template(context: dict) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["bool"] = lambda v: str(v).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ) or v is True
    env.filters["int"] = lambda v: int(v)
    return env.get_template(_TEMPLATE.name).render(**context)


def _normalise_ws(text: str) -> str:
    """Collapse trailing whitespace + blank-line runs so the two renderers'
    equivalence is judged on structure, not incidental whitespace (Property 7
    is 'byte-identical after whitespace normalisation')."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    out: list[str] = []
    for line in lines:
        if line == "" and out and out[-1] == "":
            continue
        out.append(line)
    return "\n".join(out)


# A SECOND, independent fixture (NOT the golden's demo-slug/two-mappings/tls-on)
# so the Jinja↔pure equivalence is proven beyond the single golden example.
def _second_fixture_jinja_ctx() -> dict:
    return {
        "project_slug": "acme-proj",
        "openbao_primary_addr": "https://10.0.20.10:8200",
        "openbao_primary_tls_skip_verify": False,  # tls OFF this time
        "openbao_agent_num_retries": 3,
        "openbao_agent_approle_mount": "auth/approle",
        "openbao_agent_role_id_path": "/run/creds/role-id",
        "openbao_agent_secret_id_path": "/run/creds/secret-id",
        "openbao_agent_remove_secret_id_after_read": True,
        "openbao_agent_token_sink_path": "/run/creds/token",
        "openbao_agent_rendered_secret_file": "/run/rendered/app.env",
        "openbao_agent_pid_file": "/run/agent.pid",
        "openbao_agent_secret_mappings": [
            {"kv_subpath": "db", "kv_field": "password", "env_key": "DB_PASSWORD"},
            {"kv_subpath": "api", "kv_field": "key", "env_key": "API_KEY"},
            {
                "kv_subpath": "oidc",
                "kv_field": "client_secret",
                "env_key": "OIDC_CLIENT_SECRET",
            },
        ],
    }


def _second_fixture_agent_config() -> AgentConfig:
    ctx = _second_fixture_jinja_ctx()
    return AgentConfig(
        project_slug=ctx["project_slug"],
        primary_addr=ctx["openbao_primary_addr"],
        role_id_file_path=ctx["openbao_agent_role_id_path"],
        secret_id_file_path=ctx["openbao_agent_secret_id_path"],
        token_sink_path=ctx["openbao_agent_token_sink_path"],
        rendered_secret_file=ctx["openbao_agent_rendered_secret_file"],
        secret_mappings=[
            SecretMapping(**m) for m in ctx["openbao_agent_secret_mappings"]
        ],
        tls_skip_verify=ctx["openbao_primary_tls_skip_verify"],
        num_retries=ctx["openbao_agent_num_retries"],
        approle_mount_path=ctx["openbao_agent_approle_mount"],
        remove_secret_id_after_read=ctx["openbao_agent_remove_secret_id_after_read"],
        pid_file=ctx["openbao_agent_pid_file"],
    )


def _golden_jinja_ctx() -> dict:
    """The golden fixture: demo-slug, two mappings, tls on."""
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


def _golden_agent_config() -> AgentConfig:
    ctx = _golden_jinja_ctx()
    return AgentConfig(
        project_slug=ctx["project_slug"],
        primary_addr=ctx["openbao_primary_addr"],
        role_id_file_path=ctx["openbao_agent_role_id_path"],
        secret_id_file_path=ctx["openbao_agent_secret_id_path"],
        token_sink_path=ctx["openbao_agent_token_sink_path"],
        rendered_secret_file=ctx["openbao_agent_rendered_secret_file"],
        secret_mappings=[
            SecretMapping(**m) for m in ctx["openbao_agent_secret_mappings"]
        ],
        tls_skip_verify=ctx["openbao_primary_tls_skip_verify"],
        num_retries=ctx["openbao_agent_num_retries"],
        approle_mount_path=ctx["openbao_agent_approle_mount"],
        remove_secret_id_after_read=ctx["openbao_agent_remove_secret_id_after_read"],
        pid_file=ctx["openbao_agent_pid_file"],
    )


# --------------------------------------------------------------------------- #
# Contract on the Jinja render (fixture slug + two mappings, tls on)
# --------------------------------------------------------------------------- #
def test_jinja_render_references_exactly_one_kv_path_per_mapping():
    """Req 4.2 / Property 5 — secret/data/<slug>/<kv_subpath> once per mapping."""
    ctx = _golden_jinja_ctx()
    rendered = _render_template(ctx)
    slug = ctx["project_slug"]
    for mapping in ctx["openbao_agent_secret_mappings"]:
        ref = f'secret/data/{slug}/{mapping["kv_subpath"]}'
        assert rendered.count(ref) == 1, (
            f"expected exactly one reference to {ref!r}, got "
            f"{rendered.count(ref)}"
        )
    # exactly len(mappings) template stanzas, no more, no fewer.
    assert rendered.count("template {") == len(ctx["openbao_agent_secret_mappings"])
    # no OTHER secret/data/<slug>/ path leaked in.
    all_refs = re.findall(rf"secret/data/{re.escape(slug)}/([A-Za-z0-9_-]+)", rendered)
    assert sorted(all_refs) == sorted(
        m["kv_subpath"] for m in ctx["openbao_agent_secret_mappings"]
    )


def test_jinja_render_carries_the_approle_auto_auth_paths():
    """Req 4.1 / 4.4 / Property 4 — one auto_auth approle stanza with the paths."""
    ctx = _golden_jinja_ctx()
    rendered = _render_template(ctx)
    assert rendered.count("auto_auth {") == 1
    assert rendered.count('method "approle"') == 1
    assert (
        f'role_id_file_path   = "{ctx["openbao_agent_role_id_path"]}"' in rendered
    )
    assert (
        f'secret_id_file_path = "{ctx["openbao_agent_secret_id_path"]}"' in rendered
    )
    assert f'address = "{ctx["openbao_primary_addr"]}"' in rendered
    assert f'mount_path = "{ctx["openbao_agent_approle_mount"]}"' in rendered


def test_jinja_render_tls_skip_verify_is_conditional():
    """Req 4.5 / Property 6 — tls_skip_verify present iff the toggle is on."""
    on = _render_template(_golden_jinja_ctx())  # toggle True
    assert "tls_skip_verify = true" in on

    off_ctx = _golden_jinja_ctx()
    off_ctx["openbao_primary_tls_skip_verify"] = False
    off = _render_template(off_ctx)
    assert "tls_skip_verify" not in off


# --------------------------------------------------------------------------- #
# Jinja ↔ pure renderer equivalence (the shared-symbol cross-check)
# --------------------------------------------------------------------------- #
def test_jinja_and_pure_renderer_agree_on_the_golden_fixture():
    """The Jinja template and render_agent_hcl agree on the golden fixture, and
    both agree with the committed golden file — the design's structural-
    equivalence guarantee (references the golden, does not duplicate the sibling
    byte-for-byte golden assertion)."""
    jinja_out = _normalise_ws(_render_template(_golden_jinja_ctx()))
    pure_out = _normalise_ws(render_agent_hcl(_golden_agent_config()))
    golden = _normalise_ws(_GOLDEN.read_text(encoding="utf-8"))
    assert jinja_out == pure_out, (
        "the Jinja template and scripts/installer/workload_auth.py::"
        "render_agent_hcl diverged on the golden fixture — reconcile them "
        "(they are two implementations of one agent.hcl format)."
    )
    assert pure_out == golden, (
        "render_agent_hcl drifted from the committed golden fixture — "
        "regenerate the golden together with the renderer + the Jinja template."
    )


def test_jinja_and_pure_renderer_agree_on_a_second_independent_fixture():
    """A DIFFERENT slug + three mappings + tls OFF + non-default paths/retries:
    proves Jinja↔pure equivalence beyond the single golden example (so the two
    implementations cannot silently diverge on inputs the golden does not
    exercise)."""
    jinja_out = _normalise_ws(_render_template(_second_fixture_jinja_ctx()))
    pure_out = _normalise_ws(render_agent_hcl(_second_fixture_agent_config()))
    assert jinja_out == pure_out, (
        "the Jinja template and render_agent_hcl diverged on the second fixture "
        "(acme-proj, three mappings, tls off) — they must stay structurally "
        "equivalent across all inputs, not only the golden example."
    )
    # And the tls-off path really omits the line in BOTH implementations.
    assert "tls_skip_verify" not in jinja_out
    assert "tls_skip_verify" not in pure_out
    # three mappings -> three template stanzas in both.
    assert jinja_out.count("template {") == 3
    assert pure_out.count("template {") == 3


def test_neither_renderer_emits_a_secret_value():
    """Req 4.8 — only file paths, KV references, and literal directives appear;
    never a rendered secret value (both implementations)."""
    for text in (
        _render_template(_second_fixture_jinja_ctx()),
        render_agent_hcl(_second_fixture_agent_config()),
    ):
        # the literal consul-template directive is present ...
        assert "DB_PASSWORD={{ .Data.data.password }}" in text
        # ... but no "resolved" secret value marker is (there is none to leak).
        assert "BEGIN" not in text  # e.g. no PEM/key body
