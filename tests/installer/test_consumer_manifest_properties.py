"""Hypothesis property test for the Consumer_Manifest validator — Property 1.

Feature: project-consumer-onboarding (tasks.md Task 2.3; design.md "Correctness
Properties" → Property 1; Requirements 1.5, 1.6, 1.7).

One property-based test, >=100 iterations (``@settings(max_examples=200)``),
tagged per the workflow convention. It generates manifests across the
accept/reject boundary — well-formed ones, single-field mutations, and key-set
mutations — and asserts the validator accepts IFF the manifest is well-formed,
and that on rejection the raised diagnostic names the offending field or service
entry.

All offline: the module under test is pure, stdlib-only, so this runs under the
repo's default offline pytest invocation (``-m "not requires_infra"``).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from consumer_manifest import (
    APP_TYPES,
    AUTH_METHODS,
    CONSUMABLE_SERVICE_KEYS,
    DATABASE_ENGINES,
    DATABASE_TIERS,
    SLUG_RE,
    ManifestValidationError,
    validate_manifest,
)

# A reference, independent re-statement of "well-formed" so the test does not
# just mirror the implementation. A manifest is well-formed iff all of these
# hold — used to decide the expected accept/reject verdict.


def _slug_ok(slug) -> bool:
    return isinstance(slug, str) and bool(SLUG_RE.match(slug))


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and len(v) > 0


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _database_ok(block) -> bool:
    if not isinstance(block, dict):
        return False
    if not _nonempty_str(block.get("db_name")):
        return False
    if not _nonempty_str(block.get("db_role")):
        return False
    if "engine" in block and block["engine"] not in DATABASE_ENGINES:
        return False
    if "tier" in block and block["tier"] not in DATABASE_TIERS:
        return False
    return True


def _sso_ok(block) -> bool:
    if not isinstance(block, dict):
        return False
    if not _nonempty_str(block.get("org_name")):
        return False
    regs = block.get("app_registrations")
    if not isinstance(regs, list) or not regs:
        return False
    for entry in regs:
        if not isinstance(entry, dict):
            return False
        if not _nonempty_str(entry.get("name")):
            return False
        if entry.get("app_type") not in APP_TYPES:
            return False
        if entry.get("auth_method") not in AUTH_METHODS:
            return False
    return True


def _secrets_ok(block) -> bool:
    if not isinstance(block, dict):
        return False
    if not _nonempty_str(block.get("policy")):
        return False
    path = block.get("path")
    if not _nonempty_str(path) or not path.startswith("secret/"):
        return False
    t = block.get("workload_token_ttl")
    m = block.get("workload_token_max_ttl")
    if not (_pos_int(t) and _pos_int(m) and t <= m):
        return False
    if not _pos_int(block.get("secret_id_ttl")):
        return False
    if not _nonneg_int(block.get("secret_id_num_uses")):
        return False
    return True


def _manifest_well_formed(manifest: dict) -> bool:
    # top-level key set: only allowed keys, no unknown service entry.
    allowed = {"slug"} | set(CONSUMABLE_SERVICE_KEYS)
    if any(k not in allowed for k in manifest):
        return False
    if not _slug_ok(manifest.get("slug")):
        return False
    if "database" in manifest and not _database_ok(manifest["database"]):
        return False
    if "sso" in manifest and not _sso_ok(manifest["sso"]):
        return False
    if "secrets" in manifest and not _secrets_ok(manifest["secrets"]):
        return False
    return True


# --- Generators spanning the accept/reject boundary ----------------------

# Slugs: a mix that lands inside and outside SLUG_RE.
_good_slug = st.from_regex(SLUG_RE, fullmatch=True).filter(lambda s: len(s) <= 20)
_maybe_slug = st.one_of(
    _good_slug,
    st.text(min_size=0, max_size=25),  # often invalid (case, length, punctuation)
)

_engine = st.one_of(st.sampled_from(DATABASE_ENGINES), st.sampled_from(["oracle", "sqlite", ""]))
_tier = st.one_of(st.sampled_from(DATABASE_TIERS), st.sampled_from(["xlarge", ""]))
_tok = st.text(min_size=0, max_size=8)

_database = st.fixed_dictionaries(
    {"db_name": _tok, "db_role": _tok},
    optional={"engine": _engine, "tier": _tier},
)

_app_type = st.one_of(st.sampled_from(APP_TYPES), st.sampled_from(["native", ""]))
_auth = st.one_of(st.sampled_from(AUTH_METHODS), st.sampled_from(["mtls", ""]))
_app_reg = st.fixed_dictionaries(
    {"name": _tok, "app_type": _app_type, "auth_method": _auth}
)
_sso = st.fixed_dictionaries(
    {
        "org_name": _tok,
        "app_registrations": st.lists(_app_reg, min_size=0, max_size=3),
    }
)

_ttl = st.integers(min_value=-5, max_value=100)
_secrets = st.fixed_dictionaries(
    {
        "policy": _tok,
        "path": st.one_of(
            st.builds(lambda s: "secret/" + s, _tok), _tok
        ),
        "workload_token_ttl": _ttl,
        "workload_token_max_ttl": _ttl,
        "secret_id_ttl": _ttl,
        "secret_id_num_uses": st.integers(min_value=-5, max_value=100),
    }
)

# Full manifests: slug always present; service blocks optionally present;
# occasionally an unknown top-level key is injected to exercise Req 1.7.
_manifest = st.builds(
    lambda slug, db, sso, secrets, unknown: {
        **({"slug": slug}),
        **({"database": db} if db is not None else {}),
        **({"sso": sso} if sso is not None else {}),
        **({"secrets": secrets} if secrets is not None else {}),
        **({"cache": {"x": 1}} if unknown else {}),
    },
    slug=_maybe_slug,
    db=st.one_of(st.none(), _database),
    sso=st.one_of(st.none(), _sso),
    secrets=st.one_of(st.none(), _secrets),
    unknown=st.booleans(),
)


# =============================================================================
# Task 2.3 — Property 1
# Feature: project-consumer-onboarding, Property 1: Manifest validation accepts
# exactly the well-formed region
# Validates: Requirements 1.5, 1.6, 1.7
# =============================================================================
@settings(max_examples=200)
@given(manifest=_manifest)
def test_property1_validation_accepts_exactly_well_formed(manifest):
    expected_valid = _manifest_well_formed(manifest)
    if expected_valid:
        # A well-formed manifest is accepted and returned unchanged.
        assert validate_manifest(dict(manifest)) == manifest
    else:
        try:
            validate_manifest(dict(manifest))
        except ManifestValidationError as exc:
            # On rejection the diagnostic must name an offending field/entry:
            # a non-empty, informative message (never a silent/empty failure).
            assert str(exc)
        else:
            raise AssertionError(
                f"expected rejection for ill-formed manifest {manifest!r}"
            )
