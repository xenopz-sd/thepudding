"""Offline drift-guards for the SVC-17/18 (observability) mock stand-ins.

Task 7.4 (spec: svc-17-18-observability) — requirements.md FR-9, FR-14, FR-4;
design.md §4 (Mocks A/B/C) + .kiro/steering/mock-stand-ins.md. Pins the three
PERMANENT, default-off/deferred observability mocks:

  * Mock A — ``grafana_oidc_enabled`` (stand-in for the SVC-06 Grafana OIDC
    client): default FALSE; the ``[auth.generic_oauth]`` block in grafana.ini.j2
    renders ONLY when the flag is on; tasks/grafana.yml fails closed when the
    flag is on but the OIDC issuer/client-id vars are empty — never silently
    anonymous (FR-9).
  * Mock B — ``obs_tls_self_signed`` (stand-in for SVC-08/EJBCA + Let's Encrypt):
    default FALSE; tasks/grafana.yml fails closed when the mock is off AND no
    real CA is available (``when: not (obs_tls_self_signed | bool)``); the
    mock_tls_self_signed.yml generate/copy block is gated
    ``when: obs_tls_self_signed | bool`` and is idempotent (stat-the-target skip
    via a ``*_generate`` fact) — never serve no-TLS (FR-14).
  * Mock C — ``dcgm_metrics_deferred`` (stand-in for the SVC-23 GPU host):
    default TRUE (deferred — the one inverse-polarity mock); the dcgm_exporter
    role deploys only when ``not dcgm_metrics_deferred`` AND gpu-group
    membership; the ``GpuEccErrors`` rule renders only when not deferred (FR-4).

Also asserts the three catalogue rows (``### Mock A``/``### Mock B``/``### Mock
C``) are present in mock-stand-ins.md with their flag names.

Pure-structure, offline: PyYAML parse of the role defaults + the role task
files + a text scan / Jinja render — no infra, no terraform binary. Structural
gates (``when:`` conditions) are asserted by parsing the task YAML; the
grafana.ini.j2 and baseline.rules.yml.j2 CONDITIONAL render is asserted by
rendering both ways with a faithful offline Jinja harness (copied locally from
infra/tests/test_obs_prometheus_render.py — a SEPARATE pytest root, so no
cross-tree import; the conftest-collision rule forbids one).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_obs_mocks.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OBS_ROLE = _REPO_ROOT / "ansible" / "roles" / "observability_stack"
_OBS_DEFAULTS = _OBS_ROLE / "defaults" / "main.yml"
_GRAFANA_TASKS = _OBS_ROLE / "tasks" / "grafana.yml"
_MOCK_TLS_TASKS = _OBS_ROLE / "tasks" / "mock_tls_self_signed.yml"
_GRAFANA_INI_TMPL = _OBS_ROLE / "templates" / "grafana" / "grafana.ini.j2"
_RULES_TMPL = _OBS_ROLE / "templates" / "rules" / "baseline.rules.yml.j2"

_DCGM_ROLE = _REPO_ROOT / "ansible" / "roles" / "dcgm_exporter"
_DCGM_TASKS = _DCGM_ROLE / "tasks" / "main.yml"

_CATALOGUE = _REPO_ROOT / ".kiro" / "steering" / "mock-stand-ins.md"


# --------------------------------------------------------------------------- #
# Task-dict helpers (local copies; conftest-collision rule — no cross-tree import).
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_task_file(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _defaults() -> dict:
    raw = yaml.safe_load(_OBS_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw, "observability_stack defaults must be a non-empty mapping"
    return raw


# --------------------------------------------------------------------------- #
# Faithful offline Jinja harness (local copy of the infra/tests render harness;
# the two pytest roots run as SEPARATE invocations and the shared same-named
# conftest.py forbids a cross-tree import — mirrors every *_mocks.py sibling).
# --------------------------------------------------------------------------- #
def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    env.filters["to_json"] = lambda v, **kw: json.dumps(v)
    return env


def _resolve_self_references(raw: dict) -> dict:
    """Resolve the shallow ``{{ ... }}`` self-references in the role defaults."""
    env = _make_env()
    resolved = dict(raw)

    def _render_scalars(d: dict) -> tuple[dict, bool]:
        changed = False
        out = {}
        for key, val in d.items():
            if isinstance(val, str) and "{{" in val:
                try:
                    rendered = env.from_string(val).render(**resolved)
                except Exception:
                    rendered = val
                if rendered != val:
                    changed = True
                out[key] = rendered
            elif isinstance(val, list):
                new_list = []
                for item in val:
                    if isinstance(item, str) and "{{" in item:
                        try:
                            r = env.from_string(item).render(**resolved)
                        except Exception:
                            r = item
                        if r != item:
                            changed = True
                        new_list.append(r)
                    else:
                        new_list.append(item)
                out[key] = new_list
            else:
                out[key] = val
        return out, changed

    for _ in range(10):
        resolved, changed = _render_scalars(resolved)
        if not changed:
            break
    return resolved


def _role_vars() -> dict:
    resolved = _resolve_self_references(_defaults())
    # ``ansible_managed`` is an Ansible magic var injected at template time (not a
    # role default); the grafana.ini.j2 header references it. Supply a stand-in so
    # the offline render matches Ansible's runtime render.
    resolved.setdefault("ansible_managed", "Ansible managed")
    return resolved


def _render(template_path: Path, variables: dict) -> str:
    assert template_path.is_file(), f"expected template at {template_path}"
    env = _make_env()
    tmpl = env.from_string(template_path.read_text(encoding="utf-8"))
    return tmpl.render(**variables)


# =========================================================================== #
# 1. All three mock flags default to the safe/inert posture.
# =========================================================================== #
def test_mock_flags_default_safe_inert_posture():
    """Mock A/B default FALSE (real posture); Mock C defaults TRUE (deferred is
    the inert/safe posture when zero GPU hosts exist — mock-stand-ins.md
    'Default-sense note')."""
    d = _defaults()

    assert "grafana_oidc_enabled" in d, "grafana_oidc_enabled must be declared in defaults"
    assert d["grafana_oidc_enabled"] is False, (
        "grafana_oidc_enabled must default FALSE — Mock A is opt-in; default is the "
        "Break_Glass_Admin posture, never real OIDC without a registered client (FR-9)"
    )

    assert "obs_tls_self_signed" in d, "obs_tls_self_signed must be declared in defaults"
    assert d["obs_tls_self_signed"] is False, (
        "obs_tls_self_signed must default FALSE — Mock B is an opt-in dev stand-in, "
        "never the production default (FR-14)"
    )

    assert "dcgm_metrics_deferred" in d, "dcgm_metrics_deferred must be declared in defaults"
    assert d["dcgm_metrics_deferred"] is True, (
        "dcgm_metrics_deferred must default TRUE — the inert/safe posture when no GPU "
        "host exists is deferral (Mock C; GPU metrics are not a v1 gate, FR-4/AC-12)"
    )


# =========================================================================== #
# 2a. Mock A — grafana_oidc_enabled: conditional generic_oauth + fail-closed.
# =========================================================================== #
def test_mock_a_generic_oauth_block_conditional_on_flag():
    """grafana.ini renders the [auth.generic_oauth] block ONLY when
    grafana_oidc_enabled=true — rendered both ways and asserted."""
    base = _role_vars()

    # The literal "[auth.generic_oauth]" also appears in the template's header
    # COMMENT (lines prefixed with "; "), so match the ACTIVE INI section header
    # only — a non-comment line that is exactly the section header.
    def _has_generic_oauth_section(rendered: str) -> bool:
        return any(
            line.strip() == "[auth.generic_oauth]" and not line.lstrip().startswith(";")
            for line in rendered.splitlines()
        )

    # Default posture (flag off): the OIDC section is absent.
    off = dict(base)
    off["grafana_oidc_enabled"] = False
    rendered_off = _render(_GRAFANA_INI_TMPL, off)
    assert not _has_generic_oauth_section(rendered_off), (
        "the [auth.generic_oauth] section must NOT render while grafana_oidc_enabled=false "
        "(Mock A default — Break_Glass_Admin only, FR-9)"
    )
    assert "enabled = false" in rendered_off, (
        "anonymous access must stay explicitly disabled in both postures (never anonymous)"
    )

    # Flag on (with the OIDC vars wired): the OIDC section renders.
    on = dict(base)
    on["grafana_oidc_enabled"] = True
    on["observability_grafana_oidc_issuer_url"] = "https://zitadel.platform.example.test"
    on["observability_grafana_oidc_client_id"] = "grafana-client-id"
    rendered_on = _render(_GRAFANA_INI_TMPL, on)
    assert _has_generic_oauth_section(rendered_on), (
        "flipping grafana_oidc_enabled=true must RENDER the [auth.generic_oauth] section "
        "(conditional render, Mock A flip-to-real, FR-9)"
    )
    assert "grafana-client-id" in rendered_on, "the OIDC block must carry the configured client_id"


def test_mock_a_fails_closed_when_oidc_on_without_client_vars():
    """tasks/grafana.yml must assert (fail closed) when grafana_oidc_enabled=true
    but the OIDC issuer/client-id vars are empty — never a broken/anonymous
    render."""
    tasks = _load_task_file(_GRAFANA_TASKS)
    asserts = [t for t in tasks if ("ansible.builtin.assert" in t or "assert" in t)]

    fail_closed = []
    for t in asserts:
        when = str(t.get("when", ""))
        if "grafana_oidc_enabled" not in when:
            continue
        spec = t.get("ansible.builtin.assert", t.get("assert", {})) or {}
        clauses = " ".join(str(c) for c in (spec.get("that") or []))
        if "oidc_issuer_url" in clauses and "oidc_client_id" in clauses:
            fail_closed.append(t)

    assert fail_closed, (
        "tasks/grafana.yml must fail closed (assert, when: grafana_oidc_enabled | bool) "
        "requiring the OIDC issuer URL AND client id be non-empty — never render a broken "
        "or silently anonymous OIDC block (FR-9 / Mock A)"
    )


# =========================================================================== #
# 2b. Mock B — obs_tls_self_signed: fail-closed-when-off, gated+idempotent gen.
# =========================================================================== #
def test_mock_b_fails_closed_when_off_and_no_ca():
    """tasks/grafana.yml must fail closed when obs_tls_self_signed=false AND no
    real CA is available — gated `when: not (obs_tls_self_signed | bool)` and
    asserting obs_tls_real_ca_available."""
    tasks = _load_task_file(_GRAFANA_TASKS)
    asserts = [t for t in tasks if ("ansible.builtin.assert" in t or "assert" in t)]

    fail_closed = []
    for t in asserts:
        when = str(t.get("when", "")).replace(" ", "")
        spec = t.get("ansible.builtin.assert", t.get("assert", {})) or {}
        clauses = " ".join(str(c) for c in (spec.get("that") or []))
        if "obs_tls_self_signed" in when and "obs_tls_real_ca_available" in clauses:
            # gated on the mock being OFF: `when: not (obs_tls_self_signed | bool)`
            assert "not(obs_tls_self_signed|bool)" in when, (
                "the no-TLS fail-closed assert must be gated `when: not "
                f"(obs_tls_self_signed | bool)`; got when={when!r}"
            )
            fail_closed.append(t)

    assert fail_closed, (
        "tasks/grafana.yml must fail closed (assert obs_tls_real_ca_available) when the "
        "self-signed TLS mock is OFF and no real CA exists — never serve Grafana without "
        "TLS (FR-14 / Mock B)"
    )


def test_mock_b_generate_block_gated_on_flag():
    """The mock_tls_self_signed.yml generator must be included ONLY when
    obs_tls_self_signed is on, and the generate/copy tasks inside it must each be
    gated on the *_generate idempotency fact."""
    # The include in grafana.yml is gated on the flag.
    grafana_tasks = _load_task_file(_GRAFANA_TASKS)
    includes = [
        t for t in grafana_tasks
        if str(t.get("ansible.builtin.include_tasks", t.get("include_tasks", "")))
        .endswith("mock_tls_self_signed.yml")
    ]
    assert includes, "tasks/grafana.yml must include mock_tls_self_signed.yml (the Mock B generator)"
    for inc in includes:
        when = str(inc.get("when", "")).replace(" ", "")
        assert "obs_tls_self_signed|bool" in when and "not" not in when, (
            "the mock_tls_self_signed.yml include must be gated `when: obs_tls_self_signed "
            f"| bool` (opt-in, default off); got when={when!r}"
        )

    assert _MOCK_TLS_TASKS.is_file(), "tasks/mock_tls_self_signed.yml must exist (the Mock B generator)"


def test_mock_b_generation_is_idempotent_stat_skip():
    """Idempotency: the generator stats the target cert, sets an
    `obs_mock_tls_generate` fact, and gates generate/copy on it — a present cert
    fires no copy / no restart churn (mock-stand-ins.md 'Idempotent' rule)."""
    tasks = _load_task_file(_MOCK_TLS_TASKS)

    has_stat = any(("ansible.builtin.stat" in t or "stat" in t) for t in tasks)
    assert has_stat, "Mock B must stat the target cert to decide whether to (re)generate"

    text = _MOCK_TLS_TASKS.read_text(encoding="utf-8")
    assert "obs_mock_tls_generate" in text, (
        "Mock B must gate generate/copy on an `obs_mock_tls_generate` fact derived from the "
        "stat of the target cert (idempotent — a present cert is kept untouched)"
    )

    # The community.crypto generation tasks must be gated on the *_generate fact.
    gen = [t for t in tasks if any("community.crypto" in k for k in t.keys())]
    assert gen, "Mock B must generate the cert/key via community.crypto (control-node generation)"
    for t in gen:
        assert "obs_mock_tls_generate" in str(t.get("when", "")), (
            "each community.crypto generate task must be gated `when: obs_mock_tls_generate` "
            "(idempotency — skipped when the cert already exists)"
        )

    # Control-node generation (the minimal guest lacks python-cryptography).
    assert any(t.get("delegate_to") == "localhost" for t in gen), (
        "Mock B cert generation must delegate_to localhost (control-node generation)"
    )
    # The private-key generation task must be no_log (secret hygiene).
    key_tasks = [t for t in tasks if "community.crypto.openssl_privatekey" in t]
    assert key_tasks and all(t.get("no_log") is True for t in key_tasks), (
        "the Mock B private-key generation task must set no_log: true (secret hygiene)"
    )


# =========================================================================== #
# 2c. Mock C — dcgm_metrics_deferred: role gate + conditional GPU rule.
# =========================================================================== #
def test_mock_c_role_deploys_only_when_not_deferred_and_gpu_group():
    """dcgm_exporter resolves a deploy gate requiring BOTH
    `not dcgm_metrics_deferred` AND gpu-group membership, and the deploy task is
    gated on that resolved fact."""
    tasks = _load_task_file(_DCGM_TASKS)

    # A set_fact resolving the deploy gate from BOTH conditions.
    gate_facts = [
        t for t in tasks
        if ("ansible.builtin.set_fact" in t or "set_fact" in t)
    ]
    resolved = []
    for t in gate_facts:
        spec = t.get("ansible.builtin.set_fact", t.get("set_fact", {})) or {}
        body = str(spec)
        if "dcgm_exporter_should_deploy" in body:
            assert "dcgm_metrics_deferred" in body and "group_names" in body, (
                "the deploy gate must require BOTH `not (dcgm_metrics_deferred | bool)` AND "
                f"gpu-group membership (group_names); got: {body}"
            )
            resolved.append(t)
    assert resolved, (
        "dcgm_exporter must resolve a `dcgm_exporter_should_deploy` gate from the deferral "
        "flag AND gpu-group membership (Mock C / FR-4)"
    )

    # The docker-compose-app deploy is gated on the resolved fact.
    deploys = [
        t for t in tasks
        if str(t.get("ansible.builtin.include_role", t.get("include_role", {})))
        .find("docker-compose-app") != -1
    ]
    assert deploys, "dcgm_exporter must deploy via the docker-compose-app role"
    for t in deploys:
        when = str(t.get("when", ""))
        assert "dcgm_exporter_should_deploy" in when, (
            "the dcgm_exporter deploy must be gated `when: dcgm_exporter_should_deploy` so a "
            "deferred/non-GPU host is a logged no-op (Mock C / FR-4 / AC-12)"
        )


def test_mock_c_gpu_rule_conditional_on_deferral():
    """The GpuEccErrors rule renders ONLY when NOT deferred — baseline.rules.yml
    rendered both ways and asserted (absent when deferred, present when not)."""
    base = _role_vars()

    deferred = dict(base)
    deferred["dcgm_metrics_deferred"] = True
    doc_off = yaml.safe_load(_render(_RULES_TMPL, deferred))
    names_off = _alert_names(doc_off)
    groups_off = {g["name"] for g in doc_off.get("groups", [])}
    assert "GpuEccErrors" not in names_off, (
        "the GpuEccErrors rule must be ABSENT while dcgm_metrics_deferred=true (Mock C default, "
        "FR-4/AC-12)"
    )
    assert "platform-gpu" not in groups_off, "the platform-gpu rule group must be absent while deferred"

    not_deferred = dict(base)
    not_deferred["dcgm_metrics_deferred"] = False
    doc_on = yaml.safe_load(_render(_RULES_TMPL, not_deferred))
    names_on = _alert_names(doc_on)
    groups_on = {g["name"] for g in doc_on.get("groups", [])}
    assert "GpuEccErrors" in names_on, (
        "flipping dcgm_metrics_deferred=false must RENDER the GpuEccErrors rule (conditional "
        "render, Mock C flip-to-real)"
    )
    assert "platform-gpu" in groups_on, "the platform-gpu rule group must render when not deferred"


def _alert_names(rules_doc: dict) -> set[str]:
    names: set[str] = set()
    for group in rules_doc.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" in rule:
                names.add(rule["alert"])
    return names


# =========================================================================== #
# 3. Catalogue rows present in mock-stand-ins.md.
# =========================================================================== #
def test_catalogue_has_all_three_obs_mock_rows():
    text = _CATALOGUE.read_text(encoding="utf-8")

    assert "### Mock A" in text and "grafana_oidc_enabled" in text, (
        "mock-stand-ins.md must carry the Mock A (grafana_oidc_enabled) catalogue row"
    )
    assert "### Mock B" in text and "obs_tls_self_signed" in text, (
        "mock-stand-ins.md must carry the Mock B (obs_tls_self_signed) catalogue row"
    )
    assert "### Mock C" in text and "dcgm_metrics_deferred" in text, (
        "mock-stand-ins.md must carry the Mock C (dcgm_metrics_deferred) catalogue row"
    )


def test_catalogue_preserves_sibling_service_mocks():
    """mocks are PERMANENT: the SVC-06/07/09 rows must remain catalogued."""
    text = _CATALOGUE.read_text(encoding="utf-8")
    for row in ("### Mock 1", "### Mock 2", "### Mock 3", "### Mock 4", "### Mock 5", "### Mock 6"):
        assert row in text, f"sibling-service catalogue row {row} must remain (mocks are permanent)"
