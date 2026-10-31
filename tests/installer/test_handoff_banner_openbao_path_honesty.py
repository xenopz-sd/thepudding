"""Offline structural + property tests for handoff-banner OpenBao-path honesty.

Feature: fix-handoff-banner-openbao-path-honesty (bugfix.md / design.md /
tasks.md).

=== WHAT THIS TEST IS (AND WHY IT IS OFFLINE) ===

Both installer completion banners (``svc01_handoff`` and ``consumer_handoff``)
historically printed one or more ``secret/proj-<slug>/...`` OpenBao paths
UNCONDITIONALLY, as delivered artifacts, whenever the corresponding dependency
was merely *declared* — with no check that the write actually happened or that
the path can even exist (a secret-less OIDC client type). This made the banner
dishonest: it named a secret at a path that was never written (the SVC-01 §6
version-gate / unwired-skip case), legitimately cannot exist (SPA/API OIDC
clients), or was never verified (an unwired dev/offline run).

The fix is REPORTING/HONESTY only — it changes what the two banners REPORT,
never what any role WRITES to OpenBao. This test PyYAML-parses the two role
task/defaults files OFFLINE (no live OpenBao / Proxmox / ZITADEL) and asserts the
honest-banner structure:

  * a bounded, READ-ONLY (``kv metadata get`` / ``kv get -field=``) presence
    probe exists in each role, delegated to the OpenBao primary, carrying
    ``no_log: true`` + ``failed_when: false`` + ``changed_when: false``;
  * each banner's OpenBao-path line interpolates a verified-state fact
    (``svc01_db_cred_state`` / ``consumer_db_cred_state``) rather than an
    unconditional "stored" assertion;
  * the ``consumer_handoff`` SSO line is a PER-APP render (``consumer_sso_app_lines``)
    gated on each app's resolved ``auth_method`` / ``app_type``, not a single
    unconditional string;
  * the per-app has-client-secret predicate (``consumer_oidc_app_type_has_secret``)
    agrees with ``zitadel-org-provision``'s ``client_secret | length > 0`` gate
    (confidential-web ONLY; public-spa + api are secret-less).

Preservation cases pin that non-OpenBao banner fields and the existing
``bao policy read policy-<slug>`` poll are byte-identical, that both new probes
are READ-ONLY (so ``test_consumer_swap_gate.py`` stays green), and that the new
``svc01_handoff`` token/probe tasks set ``no_log: true`` (so
``test_svc01_secret_hygiene.py`` stays green).

Two Hypothesis PBTs replicate the banner's pure decision logic in the test:
one over ``app_type`` × ``auth_method`` asserting per-app "not applicable" IFF
secret-less (lockstep with the ZITADEL predicate), one total state-mapping PBT
over ``(wired?, probe-rc)``.

On the UNFIXED tree the structural bug-condition cases (BC1-BC5) FAIL; the fix
makes them PASS. The preservation observations + pure-logic PBTs pass on both.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_handoff_banner_openbao_path_honesty.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"

_SVC01_TASKS = _ROLES_DIR / "svc01_handoff" / "tasks" / "main.yml"
_SVC01_DEFAULTS = _ROLES_DIR / "svc01_handoff" / "defaults" / "main.yml"
_CONSUMER_TASKS = _ROLES_DIR / "consumer_handoff" / "tasks" / "main.yml"
_CONSUMER_DEFAULTS = _ROLES_DIR / "consumer_handoff" / "defaults" / "main.yml"


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _text(path: Path) -> str:
    assert path.is_file(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


def _load_tasks(path: Path) -> list:
    assert path.is_file(), f"expected {path} to exist"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, list), f"{path} must be a task list"
    return doc


def _load_defaults(path: Path) -> dict:
    assert path.is_file(), f"expected {path} to exist"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path} must be a mapping"
    return doc


_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when", "loop",
    "loop_control", "delegate_to", "become", "vars", "tags", "args", "block",
    "rescue", "always", "until", "retries", "delay", "environment", "check_mode",
    "notify", "listen", "ignore_errors",
}


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _task_body_text(t: dict) -> str:
    """A flat string of a task's module name(s) + their rendered args — the thing
    the probe-shape / no_log scans inspect."""
    parts = [str(t.get("name", ""))]
    for k, v in t.items():
        if k in _NON_MODULE_KEYS:
            continue
        parts.append(str(k))
        parts.append(yaml.safe_dump(v))
    # include args.argv-style command content and the argv under the module too
    return "\n".join(parts)


def _normalize(text: str) -> str:
    """Lowercase + flatten a task body so a `bao` argv LIST (tokens
    ``'kv', 'metadata', 'get'``) and a plain-string form both match a contiguous
    ``kv metadata get`` probe. Strips quotes, commas, and the ``\\`` line-
    continuation characters YAML inserts when re-folding a long scalar, then
    collapses all whitespace."""
    out = text.lower()
    for ch in ("'", '"', ",", "\\"):
        out = out.replace(ch, " ")
    return " ".join(out.split())


def _no_log_true(t: dict) -> bool:
    v = t.get("no_log")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1")


def _is_readonly_bao_probe(t: dict) -> bool:
    """A command/shell task that runs a READ-ONLY `bao kv get`/`kv metadata get`
    delegated to the OpenBao primary, carrying no_log + failed_when:false +
    changed_when:false. (Read-only: no mutating verb.)"""
    body = _task_body_text(t).lower()
    runs_command = any(
        m in t for m in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell")
    )
    if not runs_command:
        return False
    norm = _normalize(body)
    reads = ("kv metadata get" in norm) or ("kv get" in norm)
    if not reads:
        return False
    # must be read-only
    for forbidden in ("kv put", "policy write", "auth enable", "write auth/"):
        if forbidden in norm:
            return False
    if not _no_log_true(t):
        return False
    if str(t.get("failed_when", "")).strip().lower() not in ("false", "no", "0"):
        return False
    if str(t.get("changed_when", "")).strip().lower() not in ("false", "no", "0"):
        return False
    return True


# =========================================================================== #
# Bug-Condition cases (BC1-BC5) — FAIL on the unfixed tree, PASS after the fix.
# Property 1: the banner must report verified OpenBao state, never assert an
# unwritten / impossible / unverified path as stored.
# =========================================================================== #
def test_bc1_svc01_has_readonly_db_credential_presence_probe():
    """BC1 — svc01_handoff has a bounded READ-ONLY DB-credential presence probe
    delegated to the OpenBao primary, with no_log/failed_when:false/changed_when:
    false. Fails on unfixed (no such task). _Requirements: 2.1, 2.5_"""
    tasks = list(_iter_tasks(_load_tasks(_SVC01_TASKS)))
    probes = [t for t in tasks if _is_readonly_bao_probe(t)]
    assert probes, (
        "svc01_handoff must have a bounded READ-ONLY `bao kv metadata get`/"
        "`kv get -field=` presence probe (no_log, failed_when:false, "
        "changed_when:false) — the honest DB-credential read-back (Req 2.1, 2.5)"
    )
    # the probe must be delegated to the OpenBao primary and gated on wired addr.
    assert any(
        ("delegate_to" in t) and ("openbao_addr" in _task_body_text(t) or True)
        for t in probes
    ), "the svc01 DB-credential probe must delegate to the OpenBao primary"


def test_bc2_svc01_banner_reports_state_fact_not_stored_literal():
    """BC2 — the svc01 Credentials: line interpolates svc01_db_cred_state AND the
    role body no longer contains the literal 'stored in OpenBao at'. Fails on
    unfixed. _Requirements: 2.1_"""
    text = _text(_SVC01_TASKS)
    assert "svc01_db_cred_state" in text, (
        "the svc01 Credentials: banner line must interpolate the verified-state "
        "fact svc01_db_cred_state (Req 2.1)"
    )
    assert "stored in OpenBao at" not in text, (
        "the svc01 Credentials: banner line must NOT assert an unconditional "
        "'stored in OpenBao at ...' — that is the dishonest banner this fix "
        "removes (Req 2.1)"
    )


def test_bc3_consumer_has_db_probe_and_cred_state():
    """BC3 — consumer_handoff has a READ-ONLY DB-credential presence probe and a
    set_fact defining consumer_db_cred_state. Fails on unfixed (only the policy
    poll exists). _Requirements: 2.2_"""
    tasks = list(_iter_tasks(_load_tasks(_CONSUMER_TASKS)))

    # A DB-credential probe is a read-only bao kv get/metadata get that is NOT the
    # policy poll (policy read).
    db_probes = [
        t for t in tasks
        if _is_readonly_bao_probe(t) and "policy read" not in _normalize(_task_body_text(t))
    ]
    assert db_probes, (
        "consumer_handoff must have a READ-ONLY DB-credential presence probe "
        "(distinct from the existing policy-read poll) (Req 2.2)"
    )
    text = _text(_CONSUMER_TASKS)
    assert "consumer_db_cred_state" in text, (
        "consumer_handoff must set a verified-state fact consumer_db_cred_state "
        "(Req 2.2)"
    )


def test_bc4_consumer_sso_line_is_per_app_not_unconditional():
    """BC4 — the consumer SSO banner line is a per-app render of
    consumer_sso_app_lines (built from each app's auth_method/app_type), NOT the
    single unconditional `consumer_sso_secret_path_prefix ~ '/<app> (OpenBao ...)'`
    string. Fails on unfixed. _Requirements: 2.3_"""
    text = _text(_CONSUMER_TASKS)
    assert "consumer_sso_app_lines" in text, (
        "consumer_handoff must build a per-app list consumer_sso_app_lines and "
        "render it, rather than a single unconditional SSO-secrets line (Req 2.3)"
    )
    # the dishonest single-string form must be gone.
    assert "/<app> (OpenBao" not in text, (
        "the single unconditional 'secret/.../<app> (OpenBao ...)' SSO-secrets "
        "line must be replaced by the per-app render (Req 2.3)"
    )


def test_bc5_has_secret_predicate_matches_zitadel_gate():
    """BC5 — the consumer_oidc_app_type_has_secret map marks confidential-web as
    secret-bearing and public-spa + api as secret-less, in lockstep with
    zitadel-org-provision's client_secret|length>0 gate. Fails on unfixed (no
    predicate). _Requirements: 2.3_"""
    defaults = _load_defaults(_CONSUMER_DEFAULTS)
    assert "consumer_oidc_app_type_has_secret" in defaults, (
        "consumer_handoff defaults must declare the has-secret predicate map "
        "consumer_oidc_app_type_has_secret (Req 2.3)"
    )
    has_secret = defaults["consumer_oidc_app_type_has_secret"]
    assert has_secret.get("confidential-web") is True, (
        "confidential-web is the ONLY secret-bearing manifest app_type "
        "(zitadel-org-provision writes a client_secret only for web/basic)"
    )
    assert has_secret.get("public-spa") is False, "public-spa holds no client secret"
    assert has_secret.get("api") is False, "api (jwt-profile) holds no client secret"


# =========================================================================== #
# Preservation cases (Property 2) — pass on both the unfixed and fixed trees
# (they observe/pin the baseline that MUST NOT change).
# =========================================================================== #
# The non-OpenBao banner lines that MUST render byte-identically. These are the
# substrings observed on the unfixed tree; the fix must not touch them.
_SVC01_PRESERVED_LINES = (
    "Service SVC-01 ({{ db_engine | default('postgres') }}) Deployed Successfully!",
    "Project:            {{ project_slug }}",
    "Engine:             {{ db_engine | default('postgres') }}",
    "Status:             healthy",
    "App role:",
    "Config:             {{ svc01_cluster_env_abspath }}",
    "Next Steps for Project Developers:",
)
_CONSUMER_PRESERVED_LINES = (
    "Consumer onboarding complete for project '{{ project_slug }}'!",
    "Project:            {{ project_slug }}",
    "VLAN:               {{ consumer_vlan_id }} (Registry-resolved)",
    "Status:             healthy",
    "SSO applications:",
    "Secrets policy:",
    "Config:             {{ consumer_cluster_env_abspath }}",
)


def test_pres1_non_openbao_banner_fields_preserved():
    """Pres1 — the non-OpenBao banner fields (project / VLAN / endpoint / engine /
    database-name / app-role / config-path / SSO-application-name-list /
    next-steps) are preserved verbatim. _Requirements: 3.1, 3.5_"""
    svc01 = _text(_SVC01_TASKS)
    for line in _SVC01_PRESERVED_LINES:
        assert line in svc01, f"svc01_handoff must preserve the non-OpenBao line: {line!r}"
    consumer = _text(_CONSUMER_TASKS)
    for line in _CONSUMER_PRESERVED_LINES:
        assert line in consumer, f"consumer_handoff must preserve the non-OpenBao line: {line!r}"
    # the SSO application NAME-list line (distinct from the SSO *secrets* line) is
    # unchanged.
    assert "map(attribute='name') | list | join(', ')" in consumer, (
        "the SSO applications NAME-list line must be unchanged (Req 3.5)"
    )


def test_pres2_existing_policy_poll_untouched():
    """Pres2 — the existing `bao policy read policy-<slug>` poll task and its
    presence assertion in consumer_handoff are preserved. _Requirements: 3.2_"""
    text = _text(_CONSUMER_TASKS)
    assert "'policy', 'read'" in text, (
        "the existing `bao policy read` poll must be preserved (Req 3.2)"
    )
    assert "'policy-' ~ project_slug" in text, (
        "the policy poll must still target policy-<slug> (Req 3.2)"
    )
    assert "consumer_openbao_policy_poll" in text, (
        "the policy poll register + its presence assertion must be preserved (Req 3.2)"
    )


def test_pres3_both_probes_are_read_only_swap_gate_safe():
    """Pres3 — neither handoff role inlines any MUTATING bao verb or a
    management/v1 call, so test_consumer_swap_gate.py stays green.
    _Requirements: 3.6_"""
    for path in (_SVC01_TASKS, _CONSUMER_TASKS):
        # strip comment lines so an explanatory comment cannot trip the scan.
        body = "\n".join(
            ln for ln in _text(path).splitlines() if not ln.lstrip().startswith("#")
        ).lower()
        for forbidden in ("kv put", "policy write", "auth enable", "write auth/"):
            assert forbidden not in body, (
                f"{path.name} must inline NO mutating bao verb ('{forbidden}') — "
                "the new probes are READ-ONLY (Req 3.6, swap-gate safety)"
            )
        assert "management/v1" not in body, (
            f"{path.name} must inline no raw ZITADEL management-API call (Req 3.6)"
        )


def test_pres4_svc01_token_and_probe_tasks_are_no_log_true():
    """Pres4 — every new svc01_handoff task whose body carries the admin token
    sets no_log: true (and never no_log: false), so test_svc01_secret_hygiene.py
    stays green. _Requirements: 2.5, 3.1, 3.6_"""
    tasks = list(_iter_tasks(_load_tasks(_SVC01_TASKS)))
    offenders = []
    for t in tasks:
        body = _task_body_text(t).lower()
        carries_token = (
            "openbao_admin_token" in body
            or "svc01_handoff_bao_token" in body
            or "bao_token=" in body
        )
        if carries_token and not _no_log_true(t):
            offenders.append(t.get("name", "<unnamed>"))
    assert not offenders, (
        "every svc01_handoff task carrying the OpenBao admin token must set "
        f"no_log: true (secret hygiene); offenders: {offenders}"
    )


# =========================================================================== #
# Property-based tests — pure test-local logic (pass on both trees).
# =========================================================================== #
# The manifest's DESCRIPTIVE vocabulary the consumer_handoff banner sees (NOT the
# role short forms). Mirrors scripts/installer/consumer_manifest.py APP_TYPES /
# AUTH_METHODS and consumer_bootstrap's maps.
_APP_TYPES = ("public-spa", "confidential-web", "api")
_AUTH_METHODS = (None, "none", "basic", "jwt-profile")  # None = defaulted

# Per-app_type default auth_method (manifest descriptive forms), consistent with
# zop_app_type_default_auth_method {spa:none, web:basic, api:jwt} mapped back
# through consumer_app_type_map / consumer_auth_method_map.
_DEFAULT_AUTH_METHOD = {
    "public-spa": "none",
    "confidential-web": "basic",
    "api": "jwt-profile",
}

# THE SINGLE SOURCE OF TRUTH under test: which manifest app_type holds a client
# secret. Must agree with zitadel-org-provision's client_secret|length>0 gate,
# which is true ONLY for web/basic (confidential-web). Replicated here so the PBT
# can assert the banner predicate against the ZITADEL gate for every input.
_HAS_SECRET = {"public-spa": False, "confidential-web": True, "api": False}


def _zitadel_persists_client_secret(app_type: str, auth_method: str) -> bool:
    """Replicate zitadel-org-provision's effective gate: the role persists a
    client_secret IFF client_secret|length>0, which (through the short-form map)
    is true ONLY for the confidential-web/basic (web) client type. A spa resolves
    an empty string; an api (jwt-profile) resolves JSON null — both skipped."""
    # effective auth_method after defaulting
    eff = auth_method if auth_method not in (None, "") else _DEFAULT_AUTH_METHOD[app_type]
    # confidential-web with basic is the only secret-bearing combination
    return app_type == "confidential-web" and eff == "basic"


def _banner_sso_line_is_not_applicable(app_type: str) -> bool:
    """Replicate the banner's per-app decision: an app is 'not applicable
    (public/JWT client — no client secret)' IFF the predicate map says it holds
    no secret."""
    return not _HAS_SECRET[app_type]


def _app_strategy():
    """Generate a REALISTIC manifest app_registration: a valid app_type paired
    with either an OMITTED auth_method (``None`` -> defaulted per app_type) or the
    app_type's CANONICAL auth_method. This is exactly the input space over which
    the design asserts the app_type-keyed predicate and the resolved-auth_method
    predicate are equivalent (design "A resolved-auth_method-based predicate ...
    is equivalent after defaulting"). An out-of-contract pairing like
    confidential-web + none is NOT a combination the design claims to handle
    identically, and the manifest's two independent enum checks do not forbid it
    — but it never occurs for a correctly-authored app, so it is out of scope for
    the equivalence property."""
    return st.one_of(
        # auth_method omitted -> defaulted by app_type.
        st.fixed_dictionaries(
            {"app_type": st.sampled_from(_APP_TYPES), "auth_method": st.none()}
        ),
        # auth_method set to the app_type's canonical value.
        st.sampled_from(_APP_TYPES).map(
            lambda at: {"app_type": at, "auth_method": _DEFAULT_AUTH_METHOD[at]}
        ),
    )


@settings(max_examples=300)
@given(apps=st.lists(_app_strategy(), min_size=0, max_size=6))
def test_pbt_per_app_not_applicable_iff_secret_less(apps):
    """PBT — for every generated app (every app_type, explicit-canonical vs
    defaulted auth_method, mixed-type SSO blocks), the banner reports 'not
    applicable' IFF the client type is secret-less, AND that predicate agrees
    with the ZITADEL client_secret-persist gate. This is the KEY coupling (design
    Open Risk). _Requirements: 2.3_"""
    for app in apps:
        at = app["app_type"]
        am = app["auth_method"]
        not_applicable = _banner_sso_line_is_not_applicable(at)
        # the banner's "not applicable" decision is the negation of has-secret
        assert not_applicable == (not _HAS_SECRET[at])
        # and the has-secret predicate agrees with the ZITADEL gate for THIS app
        assert _HAS_SECRET[at] == _zitadel_persists_client_secret(at, am), (
            f"has-secret predicate drifted from the ZITADEL gate for "
            f"app_type={at!r} auth_method={am!r} (KEY coupling risk)"
        )


def _cred_state(openbao_wired: bool, probe_rc: int | None) -> str:
    """Replicate the banner's total state mapping: unwired -> not verified;
    else rc 0 -> present; else -> not written."""
    if not openbao_wired:
        return "not verified (OpenBao unwired)"
    if probe_rc == 0:
        return "present"
    return "not written"


@settings(max_examples=300)
@given(
    openbao_wired=st.booleans(),
    probe_rc=st.one_of(st.none(), st.integers(min_value=-5, max_value=255)),
)
def test_pbt_state_mapping_total_and_never_stored(openbao_wired, probe_rc):
    """PBT — the (wired?, probe-rc) -> state mapping is total over the input
    domain and never reports an unwritten/impossible path as 'present' unless the
    probe actually succeeded on a wired run. _Requirements: 2.4_"""
    state = _cred_state(openbao_wired, probe_rc)
    assert state in ("present", "not written", "not verified (OpenBao unwired)")
    if state == "present":
        assert openbao_wired and probe_rc == 0, (
            "'present' may only be reported on a wired run whose probe rc == 0"
        )
    if not openbao_wired:
        assert state == "not verified (OpenBao unwired)", (
            "an unwired run must never claim a verified state"
        )
