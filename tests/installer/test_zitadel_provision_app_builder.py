"""Property-based tests for the zitadel_provision pure request-building logic.

Spec: zitadel-spa-pkce-onboarding, Task 1 (1.3-1.6). Covers the pure functions
``validate_combination`` and ``build_app_request`` refactored out of the
previously-hardcoded ``ensure_oidc_app`` body (design Components 2 & 3). These
are pure functions with a large, enumerable input space and clear universal
invariants, so Hypothesis property tests are the sanctioned coverage per the
design's Testing Strategy.

Each property runs a minimum of 100 iterations and is tagged with its design
property number. Run:

    ~/venv/devinfra/bin/pytest tests/installer/test_zitadel_provision_app_builder.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# conftest.py adds scripts/installer to sys.path; keep a defensive insert so the
# module imports regardless of invocation cwd (mirrors the sibling test file).
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "installer"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import zitadel_provision as zp  # noqa: E402


# --------------------------------------------------------------------------- #
# Shared strategies.
# --------------------------------------------------------------------------- #
# App-type / auth-method flag domains (the documented CLI domains) plus a few
# arbitrary strings, so the validator is exercised across in-domain and
# out-of-domain inputs.
_APP_TYPES = ["spa", "web", "api"]
_AUTH_METHODS = ["none", "basic", "jwt"]

_app_type_strategy = st.one_of(
    st.sampled_from(_APP_TYPES),
    st.text(min_size=0, max_size=8),
)
_auth_method_strategy = st.one_of(
    st.sampled_from(_AUTH_METHODS),
    st.text(min_size=0, max_size=8),
)

# App names and redirect-URI lists for body construction.
_app_name_strategy = st.text(min_size=1, max_size=40)
_redirect_uris_strategy = st.lists(
    st.text(min_size=1, max_size=60), min_size=0, max_size=4
)

# The exact legacy confidential-web body the pre-refactor ensure_oidc_app built
# (sans the per-call name/redirectUris, which the test supplies). Used as the
# golden reference for Property 2.
_LEGACY_WEB_STATIC = {
    "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
    "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
    "appType": "OIDC_APP_TYPE_WEB",
    "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",
    "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
}

# Keys that would carry / imply a client secret for a public SPA. A public SPA
# body must not contain any of these.
_SECRET_BEARING_KEYS = {"clientSecret", "client_secret", "secret", "clientSecretString"}


# --------------------------------------------------------------------------- #
# Property 1 — combination validator accepts exactly the documented pairs.
# --------------------------------------------------------------------------- #
# Feature: zitadel-spa-pkce-onboarding, Property 1: Combination validator
# accepts exactly the documented pairs
@settings(max_examples=200)
@given(app_type=_app_type_strategy, auth_method=_auth_method_strategy)
def test_property1_combination_validator(app_type, auth_method):
    """Validates: Requirements 2.3, 2.4

    validate_combination raises CombinationError IFF the (app_type, auth_method)
    pair is not one of the three documented valid combinations; a valid pair
    returns normally and build_app_request produces the documented body shape.
    """
    is_valid = (app_type, auth_method) in zp.VALID_COMBINATIONS

    if is_valid:
        # Returns None (no exception) for a valid pair.
        assert zp.validate_combination(app_type, auth_method) is None
        # ...and a valid pair yields a documented body via the builder.
        endpoint, body = zp.build_app_request(app_type, auth_method, "app", [])
        assert endpoint in (zp.EP_OIDC_APPS, zp.EP_API_APPS)
        assert body["name"] == "app"
        if app_type == "web":
            assert body["appType"] == "OIDC_APP_TYPE_WEB"
            assert body["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_BASIC"
            assert endpoint == zp.EP_OIDC_APPS
        elif app_type == "spa":
            assert body["appType"] == "OIDC_APP_TYPE_USER_AGENT"
            assert body["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_NONE"
            assert endpoint == zp.EP_OIDC_APPS
        else:  # api
            assert body["authMethodType"] == "API_AUTH_METHOD_TYPE_PRIVATE_KEY_JWT"
            assert endpoint == zp.EP_API_APPS
    else:
        # Any pair not in the table raises, and the message names the pair.
        with pytest.raises(zp.CombinationError) as exc:
            zp.validate_combination(app_type, auth_method)
        assert f"{app_type}/{auth_method}" in str(exc.value)


# --------------------------------------------------------------------------- #
# Property 2 — default (no-flag) body is byte-equivalent to the legacy body.
# --------------------------------------------------------------------------- #
# Feature: zitadel-spa-pkce-onboarding, Property 2: Default (no-flag) body is
# byte-equivalent to the legacy confidential-web body
@settings(max_examples=200)
@given(app_name=_app_name_strategy, redirect_uris=_redirect_uris_strategy)
def test_property2_default_body_byte_equivalent(app_name, redirect_uris):
    """Validates: Requirements 2.5, 6.1

    Building the request with the defaulted inputs (web/basic) produces a body
    whose key set and values exactly equal the pre-existing hardcoded legacy
    body, posted to EP_OIDC_APPS.
    """
    endpoint, body = zp.build_app_request("web", "basic", app_name, redirect_uris)

    assert endpoint == zp.EP_OIDC_APPS
    expected = {
        "name": app_name,
        "redirectUris": redirect_uris,
        **_LEGACY_WEB_STATIC,
    }
    # Exact key set and values — byte-equivalent to the legacy dict.
    assert body == expected
    assert set(body.keys()) == set(expected.keys())


# --------------------------------------------------------------------------- #
# Property 3 — SPA body is public and PKCE-shaped.
# --------------------------------------------------------------------------- #
# Feature: zitadel-spa-pkce-onboarding, Property 3: SPA body is public and
# PKCE-shaped
@settings(max_examples=200)
@given(app_name=_app_name_strategy, redirect_uris=_redirect_uris_strategy)
def test_property3_spa_body_public_pkce(app_name, redirect_uris):
    """Validates: Requirements 1.1, 1.2, 8.1

    The spa/none body uses USER_AGENT + NONE, carries the Authorization-Code
    grant (PKCE by construction for a public user-agent client), is posted to
    EP_OIDC_APPS, and carries no client-secret-bearing field.
    """
    endpoint, body = zp.build_app_request("spa", "none", app_name, redirect_uris)

    assert endpoint == zp.EP_OIDC_APPS
    assert body["appType"] == "OIDC_APP_TYPE_USER_AGENT"
    assert body["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_NONE"
    assert "OIDC_GRANT_TYPE_AUTHORIZATION_CODE" in body["grantTypes"]
    # No client-secret-bearing field anywhere in the SPA body.
    assert _SECRET_BEARING_KEYS.isdisjoint(body.keys())


# --------------------------------------------------------------------------- #
# Property 5 — API body targets the resource-server endpoint with JWT-profile.
# --------------------------------------------------------------------------- #
# Feature: zitadel-spa-pkce-onboarding, Property 5: API body targets the
# resource-server endpoint with the JWT-profile method
@settings(max_examples=200)
@given(app_name=_app_name_strategy)
def test_property5_api_body_resource_server(app_name):
    """Validates: Requirements 3.1

    The api/jwt body uses API_AUTH_METHOD_TYPE_PRIVATE_KEY_JWT with no
    interactive-client fields (no redirectUris / grantTypes / responseTypes),
    posted to EP_API_APPS.
    """
    endpoint, body = zp.build_app_request("api", "jwt", app_name, [])

    assert endpoint == zp.EP_API_APPS
    assert body["authMethodType"] == "API_AUTH_METHOD_TYPE_PRIVATE_KEY_JWT"
    assert body["name"] == app_name
    # No interactive-client fields on a resource-server registration.
    assert "redirectUris" not in body
    assert "grantTypes" not in body
    assert "responseTypes" not in body
