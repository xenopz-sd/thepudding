"""Offline guards for the consumer-manifest app_type/auth_method mapping bugfix.

Feature: fix-consumer-manifest-app-type-mapping.

The Consumer_Manifest schema (``scripts/installer/consumer_manifest.py``) and the
``zitadel-org-provision`` role use two DIFFERENT vocabularies for an
App_Registration's ``app_type``/``auth_method``:

* manifest (descriptive long forms): ``APP_TYPES = (public-spa, confidential-web,
  api)``, ``AUTH_METHODS = (none, basic, jwt-profile)``.
* role (short forms): ``zop_valid_app_types = [spa, web, api]`` with per-type
  default auth methods ``{spa: none, web: basic, api: jwt}``.

``consumer_bootstrap`` STEP 4 (the composition boundary) passed the manifest list
to the ``zitadel-org-provision`` child run UNTRANSLATED, so the role's app_type
assert rejected the descriptive values — live failure on the dronefleet manifest:
``unrecognized app_type 'public-spa'. Valid app types are ['spa', 'web', 'api']``.

The fix adds two mapping dicts to ``consumer_bootstrap`` defaults
(``consumer_app_type_map`` + ``consumer_auth_method_map``, the single source of
truth) and, in STEP 4, a fail-closed per-entry assert + a ``set_fact`` building
``consumer_mapped_app_registrations`` that maps ``app_type``/``auth_method`` and
preserves ``name``/``redirect_uris``, then swaps the compose ``-e`` to the mapped
list. The manifest schema and the role accepted-set are UNCHANGED; a false
``consumer_manifest.py`` comment is corrected.

These tests are pure-logic (PyYAML parse of the committed role files + import of
the manifest module) — no infrastructure.

Two layers (bugfix.md / design.md):

* **Exploration / Fix-Checking (Property 1):** the mapping dicts exist with the
  exact total mapping, and STEP 4 composes the MAPPED list
  (``consumer_mapped_app_registrations``) instead of the raw manifest list. On
  UNFIXED code these FAIL (no mapping dicts; STEP 4 still composes
  ``consumer_manifest.sso.app_registrations``) — the structural counterexample.

* **Preservation (Property 2):** the manifest ``APP_TYPES``/``AUTH_METHODS``
  tuples, the role's ``zop_valid_app_types`` + per-type default auth methods, and
  STEP 4's ``no_log``/token-in-ENV/rescue-propagating posture are unchanged;
  already-role-vocabulary values map to themselves (identity); ``name`` and
  ``redirect_uris`` are preserved. These PASS on UNFIXED code (baseline to keep).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import consumer_manifest  # noqa: E402  (sys.path set in conftest.py)

# tests/installer/test_consumer_app_type_mapping.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONSUMER_BOOTSTRAP_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "defaults" / "main.yml"
)
_CONSUMER_BOOTSTRAP_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "tasks" / "main.yml"
)
_ZOP_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "zitadel-org-provision" / "defaults" / "main.yml"
)

# The total boundary mapping (design "Change 1" / "Correctness Properties").
_EXPECTED_APP_TYPE_MAP = {
    "public-spa": "spa",
    "confidential-web": "web",
    "api": "api",
}
_EXPECTED_AUTH_METHOD_MAP = {
    "none": "none",
    "basic": "basic",
    "jwt-profile": "jwt",
}

# The role's accepted sets (unchanged — preservation anchors).
_ROLE_APP_TYPES = {"spa", "web", "api"}
_ROLE_AUTH_METHODS = {"none", "basic", "jwt"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path):
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _command_argv_text(task: dict) -> str | None:
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if spec is None:
            continue
        if isinstance(spec, str):
            return spec
        if isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                return " ".join(str(a) for a in argv)
            if isinstance(argv, str):
                return argv
    return None


def _find_sso_compose_task(tasks) -> dict | None:
    """The STEP 4 task that composes zitadel-org-provision via a child playbook.

    The playbook path is a Jinja var (``{{ consumer_zitadel_org_provision_playbook
    }}``), so the literal ``zitadel-org-provision`` string is NOT in the argv;
    match instead on the ``ansible-playbook`` child invocation that carries the
    ``zop_app_registrations`` -e (the unique STEP 4 signature, present both before
    and after the fix).
    """
    for t in tasks:
        argv = _command_argv_text(t)
        if argv and "ansible-playbook" in argv and "zop_app_registrations" in argv:
            return t
    return None


def _set_fact_text(tasks) -> str:
    """Concatenated text of every set_fact spec (for mapped-list detection)."""
    chunks = []
    for t in tasks:
        spec = t.get("ansible.builtin.set_fact") or t.get("set_fact")
        if spec is not None:
            chunks.append(str(t.get("name", "")) + " " + str(spec))
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed code.
# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
# --------------------------------------------------------------------------- #
def test_defaults_define_app_type_map_total_and_exact():
    """Property 1 (Validates: Req 2.1, 2.2, 2.4).

    consumer_bootstrap defaults define ``consumer_app_type_map`` with the exact
    total mapping public-spa->spa, confidential-web->web, api->api. Keys must be
    exactly the manifest APP_TYPES; values must land in the role set. FAILS on
    unfixed (no such default).
    """
    data = _load_defaults(_CONSUMER_BOOTSTRAP_DEFAULTS)
    assert "consumer_app_type_map" in data, (
        "consumer_bootstrap defaults must define consumer_app_type_map (the "
        "single-source-of-truth boundary mapping)."
    )
    amap = data["consumer_app_type_map"]
    assert amap == _EXPECTED_APP_TYPE_MAP, (
        f"consumer_app_type_map must be exactly {_EXPECTED_APP_TYPE_MAP}, got {amap!r}."
    )
    # Totality over the manifest vocabulary + lands in the role's accepted set.
    assert set(amap.keys()) == set(consumer_manifest.APP_TYPES), (
        "consumer_app_type_map keys must be exactly the manifest APP_TYPES (total)."
    )
    assert set(amap.values()) <= _ROLE_APP_TYPES, (
        "every consumer_app_type_map value must be in the role's {spa, web, api}."
    )


def test_defaults_define_auth_method_map_total_and_exact():
    """Property 1 (Validates: Req 2.3, 2.4).

    consumer_bootstrap defaults define ``consumer_auth_method_map`` with the exact
    total mapping none->none, basic->basic, jwt-profile->jwt. FAILS on unfixed.
    """
    data = _load_defaults(_CONSUMER_BOOTSTRAP_DEFAULTS)
    assert "consumer_auth_method_map" in data, (
        "consumer_bootstrap defaults must define consumer_auth_method_map."
    )
    mmap = data["consumer_auth_method_map"]
    assert mmap == _EXPECTED_AUTH_METHOD_MAP, (
        f"consumer_auth_method_map must be exactly {_EXPECTED_AUTH_METHOD_MAP}, got {mmap!r}."
    )
    assert set(mmap.keys()) == set(consumer_manifest.AUTH_METHODS), (
        "consumer_auth_method_map keys must be exactly the manifest AUTH_METHODS (total)."
    )
    assert set(mmap.values()) <= _ROLE_AUTH_METHODS, (
        "every consumer_auth_method_map value must be in the role's {none, basic, jwt}."
    )


def test_mapping_lands_every_manifest_value_in_role_vocabulary():
    """Property 1 (Validates: Req 2.1, 2.2, 2.3, 2.4, 2.6).

    Applying the two maps to EVERY manifest-enum value yields a role-vocabulary
    value — the mapping is total and closes the vocabulary gap that broke STEP 4.
    FAILS on unfixed (maps absent -> KeyError-equivalent assert on presence).
    """
    data = _load_defaults(_CONSUMER_BOOTSTRAP_DEFAULTS)
    amap = data.get("consumer_app_type_map") or {}
    mmap = data.get("consumer_auth_method_map") or {}
    assert amap and mmap, "mapping dicts absent on unfixed code (expected failure)."
    for at in consumer_manifest.APP_TYPES:
        assert amap.get(at) in _ROLE_APP_TYPES, (
            f"manifest app_type {at!r} does not map into the role's accepted set."
        )
    for am in consumer_manifest.AUTH_METHODS:
        assert mmap.get(am) in _ROLE_AUTH_METHODS, (
            f"manifest auth_method {am!r} does not map into the role's accepted set."
        )


def test_step4_composes_the_mapped_list_not_the_raw_manifest_list():
    """Property 1 (Validates: Req 2.1, 2.6).

    STEP 4's compose task passes ``consumer_mapped_app_registrations`` (the mapped
    list) to the child run, NOT the raw ``consumer_manifest.sso.app_registrations``.
    FAILS on unfixed (compose still carries the raw manifest list).
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    compose = _find_sso_compose_task(tasks)
    assert compose is not None, "STEP 4 zitadel-org-provision compose task not found."
    argv = _command_argv_text(compose) or ""
    assert "consumer_mapped_app_registrations" in argv, (
        "STEP 4 must compose the MAPPED list consumer_mapped_app_registrations "
        "(not the untranslated manifest list)."
    )
    assert "consumer_manifest.sso.app_registrations" not in argv, (
        "STEP 4 must NOT pass the raw manifest list consumer_manifest.sso."
        "app_registrations to the child run (that is the untranslated-boundary bug)."
    )


def test_step4_builds_mapped_list_via_set_fact_using_the_maps():
    """Property 1 (Validates: Req 2.1, 2.2, 2.3, 2.4, Req 3.3 preserve).

    STEP 4 builds consumer_mapped_app_registrations in a set_fact that maps
    app_type via consumer_app_type_map, auth_method via consumer_auth_method_map,
    and preserves name + redirect_uris. FAILS on unfixed (no such set_fact).
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    sf_text = _set_fact_text(tasks)
    assert "consumer_mapped_app_registrations" in sf_text, (
        "STEP 4 must build consumer_mapped_app_registrations in a set_fact."
    )
    assert "consumer_app_type_map[" in sf_text, (
        "the mapped-list set_fact must map app_type through consumer_app_type_map."
    )
    assert "consumer_auth_method_map[" in sf_text, (
        "the mapped-list set_fact must map auth_method through consumer_auth_method_map."
    )
    assert "redirect_uris" in sf_text and "name" in sf_text, (
        "the mapped-list set_fact must preserve name + redirect_uris."
    )


def test_step4_fail_closed_assert_on_unmappable_value():
    """Property 4 / Property 1 (Validates: Req 2.5).

    STEP 4 carries a per-entry assert that each entry's app_type is a key in
    consumer_app_type_map AND auth_method a key in consumer_auth_method_map, so an
    out-of-vocabulary value fails closed naming the entry. FAILS on unfixed.
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    assert_tasks = [
        t for t in tasks
        if (t.get("ansible.builtin.assert") or t.get("assert"))
        and "consumer_app_type_map" in str(t.get("ansible.builtin.assert") or t.get("assert"))
    ]
    assert assert_tasks, (
        "STEP 4 must carry a fail-closed per-entry assert that app_type/auth_method "
        "are keys in the mapping dicts (Req 2.5) — never silent passthrough."
    )
    spec = str(assert_tasks[0].get("ansible.builtin.assert") or assert_tasks[0].get("assert"))
    assert "consumer_app_type_map" in spec and "consumer_auth_method_map" in spec, (
        "the fail-closed assert must check BOTH app_type and auth_method mappability."
    )


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2/3) — PASS on unfixed AND after fix.
# Validates: Requirements 2.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
# --------------------------------------------------------------------------- #
def test_manifest_enum_values_unchanged():
    """Preservation (Validates: Req 3.1).

    The manifest's own validated contract is untouched — the fix maps at the
    boundary, it does not alter APP_TYPES/AUTH_METHODS.
    """
    assert consumer_manifest.APP_TYPES == ("public-spa", "confidential-web", "api")
    assert consumer_manifest.AUTH_METHODS == ("none", "basic", "jwt-profile")


def test_role_accepted_set_unchanged():
    """Preservation (Validates: Req 3.2).

    The zitadel-org-provision role's accepted set + per-type default auth methods
    are unchanged (the fix does NOT touch the sibling role's contract).
    """
    data = _load_defaults(_ZOP_DEFAULTS)
    assert data.get("zop_valid_app_types") == ["spa", "web", "api"]
    assert data.get("zop_app_type_default_auth_method") == {
        "spa": "none",
        "web": "basic",
        "api": "jwt",
    }


def test_identity_on_already_role_vocabulary_values():
    """Preservation (Validates: Req 3.4).

    Values already in the role vocabulary (api, none, basic) map to themselves —
    the boundary mapping is a no-op on already-accepted values. This holds for the
    EXPECTED mapping regardless of whether the defaults file has been fixed yet.
    """
    assert _EXPECTED_APP_TYPE_MAP["api"] == "api"
    assert _EXPECTED_AUTH_METHOD_MAP["none"] == "none"
    assert _EXPECTED_AUTH_METHOD_MAP["basic"] == "basic"


def test_step4_secret_and_error_posture_preserved():
    """Preservation (Validates: Req 3.5).

    STEP 4's composing task keeps no_log: true, bridges the provisioning token in
    the child ENV (never argv), and propagates a non-zero child exit via its
    changed_when to the Phase-3 rescue — only the app_type/auth_method values
    change. PASSES on unfixed (and must keep passing).
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    compose = _find_sso_compose_task(tasks)
    assert compose is not None, "STEP 4 compose task not found."
    assert compose.get("no_log") is True, "STEP 4 compose task must keep no_log: true (Req 3.5)."
    argv = _command_argv_text(compose) or ""
    # The ZITADEL provisioning token must NOT appear as a positional argv; the
    # token travels in the child ENV (the child playbook reads it from env/OpenBao).
    assert "ZITADEL_PROVISION_TOKEN" not in argv, (
        "STEP 4 must not pass the provisioning token on argv (Req 3.5)."
    )
    assert "changed_when" in compose, "STEP 4 compose task must keep its changed_when (Req 3.5)."


def test_step4_feature_gated_on_sso():
    """Preservation (Validates: Req 3.6).

    The STEP 4 compose task stays feature-gated on a declared sso block
    (``when: consumer_manifest.sso is defined``). PASSES on unfixed.
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    compose = _find_sso_compose_task(tasks)
    assert compose is not None, "STEP 4 compose task not found."
    assert "consumer_manifest.sso is defined" in str(compose.get("when", "")), (
        "STEP 4 must stay gated on consumer_manifest.sso is defined (Req 3.6)."
    )


def test_role_empty_list_legacy_default_unchanged():
    """Preservation (Validates: Req 3.7).

    The role's standalone empty-list legacy default (single confidential-web
    <slug>-webapp, app_type web, auth_method basic) is unchanged — the mapping
    lives in consumer_bootstrap, not the role, so the role's default path is
    untouched.
    """
    data = _load_defaults(_ZOP_DEFAULTS)
    assert data.get("zop_app_registrations") == []
    assert data.get("zop_default_app_type") == "web"
    assert data.get("zop_default_auth_method") == "basic"
    assert "{{ zop_project_slug }}-webapp" in str(data.get("zop_default_app_name"))


def test_manifest_comment_no_longer_claims_verbatim_reuse():
    """Preservation of intent (Validates: Req 2.7).

    The APP_TYPES comment in consumer_manifest.py no longer claims the manifest
    "reuses" the sibling role values verbatim; it states they are MAPPED at the
    consumer_bootstrap STEP 4 boundary. This FAILS on unfixed code (the false
    'reusing the sibling' comment is still present) and PASSES after Change 3.

    NOTE: this is the one preservation-labelled check that is also fix-gated — it
    pins Req 2.7's comment correction. It is expected to flip from FAIL to PASS.
    """
    src = (_REPO_ROOT / "scripts" / "installer" / "consumer_manifest.py").read_text(
        encoding="utf-8"
    )
    assert "reusing the sibling" not in src, (
        "consumer_manifest.py must not claim the manifest 'reuses' the sibling "
        "role's app_type values verbatim — they are MAPPED at the boundary (Req 2.7)."
    )
    assert "MAPPED" in src or "mapped" in src, (
        "consumer_manifest.py should state the descriptive forms are mapped at the "
        "consumer_bootstrap STEP 4 boundary (Req 2.7)."
    )
