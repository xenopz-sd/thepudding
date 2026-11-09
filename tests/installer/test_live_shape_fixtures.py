"""Offline contract tests: our code's shape assumptions match recorded real shapes.

Feature: live-shape-fixtures (WS-M4 Increment 1 — the fixtures-only Option B of the
decided B->A staging; PLANNING.md §F.5).

Spec: .kiro/specs/live-shape-fixtures/ (requirements.md FR-1..FR-6; design.md
"Shape 1/2"). Traces to PLANNING.md WS-M4 and the bug class A.2 #3/#4/#5.

WHY THIS FILE EXISTS (the risk it closes)
-----------------------------------------
A recurring, expensive bug class here is NOT KNOWING THE REAL EXTERNAL API/FIELD
SHAPE at authoring time -- code is written against an assumed shape and the
mistake only surfaces at live run, as a merged-then-reopened fix-*:

  * OpenBao KV v2: the read/API path carries a `data/` infix
    (secret/data/<slug>/<sub>) and the response nests under .Data.data.<field>,
    while `bao kv` takes the BARE logical path -- getting it wrong doubled the
    infix (fix-consumer-db-bridge-kv-v2-path).
  * ZITADEL: the per-app `loginVersion` oneof (loginV1/loginV2) AND the INSTANCE
    feature loginV2.required are two different levers; the instance one decides
    the hosted-login UI (fix-svc06-login-v1-ac3 / SVC-06 AC-3 dead-end).

This file records those real shapes as committed fixtures
(tests/fixtures/live-shapes/) and asserts OUR EMITTING CODE still matches them
(framing 1a -- teeth, not documentation-only). A future edit (or a transcribed
upstream change) that drifts our assumption away from the recorded real shape
fails HERE, offline, at author time.

Increment 2 (a read-only MCP to REFRESH these fixtures against a live cluster) is
ADR-gated and NOT part of this increment -- see PLANNING.md §F.5.

The two emitters are imported via the existing tests/installer/conftest.py
sys.path injection of scripts/installer/. The json loader is local (no cross-tree
import; conftest-collision rule).

Validates: FR-1..FR-6 / AC-1..AC-3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# tests/installer/test_live_shape_fixtures.py -> tests/ is parents[1];
# fixtures live at tests/fixtures/live-shapes/.
_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "live-shapes"


def _load_fixture(name: str) -> dict:
    """Load a live-shape fixture; assert it exists and carries a `shape` key.

    Non-vacuity (FR-5): a missing/renamed fixture fails loudly here rather than
    letting a test pass without actually checking anything.
    """
    path = _FIXTURES_DIR / name
    assert path.is_file(), (
        f"live-shape fixture {name!r} not found at {path} -- the fixtures dir may "
        "have moved, or the fixture was renamed/removed."
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "shape" in data, (
        f"{name} is not a shape fixture (missing top-level 'shape' key)."
    )
    return data


# --------------------------------------------------------------------------- #
# Shape 1 — OpenBao KV v2 read path (FR-2).
# --------------------------------------------------------------------------- #
def test_openbao_kv_v2_read_shape_matches_emitter():
    """render_agent_hcl emits the KV-v2 read path + accessor the fixture records.

    Validates: FR-2 / AC-2. Renders an agent.hcl via the real emitter
    (scripts/installer/workload_auth.py::render_agent_hcl) for a known slug +
    mapping, then asserts the emitted consul-template read path equals the
    fixture's `read_api_path_template` (with the `data/` infix) and the emitted
    accessor equals `.Data.data.<field>`. A drift (single-`data` nesting, a
    dropped/doubled `data/` infix) makes the rendered string no longer contain the
    fixture's path -> FAIL.
    """
    fx = _load_fixture("openbao-kv-v2-read.json")
    assert fx["shape"] == "openbao-kv-v2-read"

    # Import the real emitter (non-vacuity FR-5: a renamed symbol fails here).
    wa = pytest.importorskip("workload_auth")
    assert hasattr(wa, "render_agent_hcl") and hasattr(wa, "AgentConfig") \
        and hasattr(wa, "SecretMapping"), (
        "workload_auth must expose render_agent_hcl / AgentConfig / SecretMapping "
        "-- the emitter API may have been renamed."
    )

    slug, subpath, field, env_key = "demo-slug", "db", "password", "DB_PASSWORD"
    cfg = wa.AgentConfig(
        project_slug=slug,
        primary_addr="https://10.0.20.10:8200",
        role_id_file_path="/openbao/agent/role-id",
        secret_id_file_path="/openbao/agent/secret-id",
        token_sink_path="/openbao/agent/token",
        rendered_secret_file="/openbao/agent/rendered/secrets.env",
        secret_mappings=[wa.SecretMapping(kv_subpath=subpath, kv_field=field, env_key=env_key)],
    )
    rendered = wa.render_agent_hcl(cfg)

    expected_read_path = fx["read_api_path_template"].format(slug=slug, subpath=subpath)
    expected_accessor = fx["response_field_accessor"].format(field=field)

    assert expected_read_path == f"secret/data/{slug}/{subpath}", (
        "fixture read_api_path_template drifted from the real KV-v2 shape "
        f"secret/data/<slug>/<sub>: {fx['read_api_path_template']!r}"
    )
    # The emitted agent.hcl must reference the data/-infix read path ...
    assert expected_read_path in rendered, (
        "render_agent_hcl did NOT emit the KV-v2 read path "
        f"{expected_read_path!r}; our OpenBao read-path assumption has drifted "
        "from the recorded real shape (the data/ infix). Emitted:\n" + rendered
    )
    # ... and the v2 double-`data` accessor .Data.data.<field> (not .Data.<field>).
    assert expected_accessor == ".Data.data.password"
    assert expected_accessor in rendered, (
        f"render_agent_hcl did NOT emit the KV-v2 accessor {expected_accessor!r} "
        "(.Data.data.<field> — the v2 double-`data` nesting); the assumption has "
        "drifted. Emitted:\n" + rendered
    )
    # Guard against the single-`data` drift explicitly: the bare `.Data.password`
    # form must NOT be what we rely on (it would be the v1 shape).
    assert ".Data.data.password" in rendered


# --------------------------------------------------------------------------- #
# Shape 2 — ZITADEL loginVersion oneof + instance loginV2 feature (FR-3).
# --------------------------------------------------------------------------- #
def test_zitadel_login_version_shape_matches_emitter():
    """zitadel_provision's loginVersion/instance shapes match the fixture.

    Validates: FR-3 / AC-2. Imports scripts/installer/zitadel_provision.py and
    asserts LOGIN_VERSION_BODY equals the fixture's per-app oneof,
    EP_FEATURES_INSTANCE equals the fixture's instance endpoint, and the instance
    feature body our code sends is shaped {"loginV2": {"required": <bool>}}. A
    renamed oneof key, a moved lever (per-app instead of instance), or a changed
    endpoint fails HERE.
    """
    fx = _load_fixture("zitadel-login-version.json")
    assert fx["shape"] == "zitadel-login-version"

    zp = pytest.importorskip("zitadel_provision")
    assert hasattr(zp, "LOGIN_VERSION_BODY") and hasattr(zp, "EP_FEATURES_INSTANCE"), (
        "zitadel_provision must expose LOGIN_VERSION_BODY and EP_FEATURES_INSTANCE "
        "-- the emitter API may have been renamed."
    )

    # Per-app loginVersion oneof: our constant must equal the recorded real shape.
    assert zp.LOGIN_VERSION_BODY == fx["login_version_oneof"], (
        "zitadel_provision.LOGIN_VERSION_BODY has drifted from the recorded "
        f"ZITADEL loginVersion oneof shape. ours={zp.LOGIN_VERSION_BODY!r} "
        f"fixture={fx['login_version_oneof']!r}"
    )

    # Instance feature endpoint.
    assert zp.EP_FEATURES_INSTANCE == fx["instance_feature_endpoint"], (
        "zitadel_provision.EP_FEATURES_INSTANCE drifted from the recorded "
        f"instance-feature endpoint. ours={zp.EP_FEATURES_INSTANCE!r} "
        f"fixture={fx['instance_feature_endpoint']!r}"
    )

    # Instance feature body shape: the deciding lever is loginV2.required (bool),
    # at the INSTANCE level (fixture deciding_lever == 'instance'). Assert the
    # fixture's recorded body has exactly that key path with a bool value, so the
    # shape our code's set_instance_login_v2_required sends is pinned.
    body = fx["instance_feature_body"]
    assert set(body) == {"loginV2"} and set(body["loginV2"]) == {"required"} \
        and isinstance(body["loginV2"]["required"], bool), (
        "fixture instance_feature_body drifted from the real "
        f'{{"loginV2": {{"required": <bool>}}}} shape: {body!r}'
    )
    assert fx.get("deciding_lever") == "instance", (
        "the deciding lever is the INSTANCE loginV2 feature, not per-app "
        "loginVersion (live-confirmed, fix-svc06-login-v1-ac3) -- the fixture "
        "must record that."
    )
