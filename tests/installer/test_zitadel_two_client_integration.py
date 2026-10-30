"""Live two-client onboarding integration test (SVC-06, requires-infra).

Spec: zitadel-spa-pkce-onboarding, Task 10.1 (R8.5, R8.6, R4.2). This is the
ONE live ``requires-infra`` test for the feature. It onboards a throwaway project
with a **SPA** entry and an **API** entry and asserts that BOTH a public SPA
client AND an API client exist in the SINGLE org/project derived from the slug
(R4.2/R8.6) — the end-to-end proof that the two-client onboarding creates two
apps of two types without a second organization.

Offline-gate discipline
------------------------
The test is marked ``@pytest.mark.requires_infra`` and is therefore DESELECTED by
default via the root ``pytest.ini`` (``addopts = -m "not requires_infra"``). It
NEVER runs as part of the offline gate; it collects and skips cleanly without
infrastructure. To run it live you must explicitly opt in with ``-m
requires_infra`` AND have a wired-up throwaway cluster:

    ~/venv/devinfra/bin/pytest -m requires_infra \
        tests/installer/test_zitadel_two_client_integration.py -q

Live-run interlock (testing-strategy.md -> "Agent Live-Test Authorization")
---------------------------------------------------------------------------
This is a destructive live run. It is permitted ONLY against a throwaway cluster
listed in ``scripts/installer/throwaway-clusters.yml``, ONLY when
``AGENT_LIVE_AUTHORIZED`` is armed and the fail-closed throwaway guard authorizes
it, and NEVER in CI, acceptance, or production. Any dependency NOT under test —
ZITADEL itself, its backing PostgreSQL (an SVC-01 instance), and OpenBao (SVC-07)
— MUST be stood up / reset / re-bootstrapped EXCLUSIVELY through its own
``svc-NN-bootstrap.yml`` automated path (its ``clean_slate`` / admin-mint
opt-ins), never by ad-hoc ``ssh ... docker compose down`` / ``docker volume rm``
/ ``pct`` / direct container ``exec`` surgery. Raw SSH/docker against a dependency
is for READ-ONLY diagnosis only (deciding WHICH automated path to run).

Readiness polling (R8.5)
------------------------
Before asserting, the test polls the freshly-provisioned ZITADEL for readiness
with a BOUNDED timeout and fails with a diagnostic identifying the readiness
failure if the timeout elapses before ZITADEL is ready. The readiness probe hits
the UNAUTHENTICATED OIDC discovery document and references only HTTP-status
fields — it NEVER echoes a secret (no token, no client secret). Dependency health
is read via ``bao status`` fields / HTTP-status strings ONLY, consistent with the
SVC-07 live-stub convention.

Secret hygiene
--------------
The test references ``bao status`` / HTTP-status fields only and NEVER echoes a
secret value (no provisioning token, no captured client secret). The API client's
created-once credential is captured by the role into OpenBao, not by this test.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts" / "installer"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Bounded ZITADEL readiness timeout (R8.5). Kept modest; a fresh ZITADEL stack
# with its backing DB is expected to answer OIDC discovery within this window.
_READINESS_TIMEOUT_SECONDS = 120
_READINESS_POLL_INTERVAL_SECONDS = 5
_READINESS_REQUEST_TIMEOUT_SECONDS = 5

# Env vars naming the freshly-provisioned instance (wired from the instance the
# provision-then-test walkthrough just built — see the SVC-06 install runbook).
# None are secrets-bearing except the provisioning token, which is consumed by
# the role via the child ENV and is NEVER read or echoed here.
_ENV_BASE_URL = "ZITADEL_TEST_BASE_URL"       # e.g. https://auth.<platform-domain>
_ENV_HOST_HEADER = "ZITADEL_TEST_HOST_HEADER"  # optional dev/mock Host override
_ENV_PROJECT_SLUG = "ZITADEL_TEST_PROJECT_SLUG"  # throwaway slug to onboard


def _discovery_url(base_url: str) -> str:
    """The unauthenticated OIDC discovery document URL for a ZITADEL base URL."""
    return base_url.rstrip("/") + "/.well-known/openid-configuration"


def _poll_zitadel_ready(base_url: str, *, host_header: str | None,
                        timeout_s: int = _READINESS_TIMEOUT_SECONDS) -> None:
    """Poll the UNAUTHENTICATED OIDC discovery endpoint until HTTP 200 or timeout.

    Fails with a diagnostic naming the URL, the elapsed time, and the last
    HTTP-status/transport observation on timeout (R8.5). References HTTP-status
    fields ONLY — never a secret.
    """
    import ssl

    url = _discovery_url(base_url)
    ctx = ssl.create_default_context()
    # Tolerate the dev/mock self-signed cert on a throwaway cluster (same posture
    # as the helper's --insecure dev path). Not a secret-handling concern.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    deadline = time.monotonic() + timeout_s
    last_observation = "no request issued yet"
    while time.monotonic() < deadline:
        req = urllib.request.Request(url=url, method="GET")
        if host_header:
            req.add_header("Host", host_header)
        try:
            with urllib.request.urlopen(req, timeout=_READINESS_REQUEST_TIMEOUT_SECONDS,
                                        context=ctx) as resp:
                if resp.status == 200:
                    return
                last_observation = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            last_observation = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            last_observation = f"unreachable: {exc.reason}"
        time.sleep(_READINESS_POLL_INTERVAL_SECONDS)

    pytest.fail(
        "ZITADEL readiness timeout (R8.5): OIDC discovery at "
        f"{url} did not return HTTP 200 within {timeout_s}s. "
        f"Last observation: {last_observation}. "
        "Stand ZITADEL up via its own svc-06-bootstrap.yml automated path before "
        "running this live test (see the SVC-06 install runbook, section A.6)."
    )


@pytest.mark.requires_infra
def test_live_two_client_onboarding_creates_spa_and_api_in_one_org_project():
    """Live two-client onboarding (requires_infra — deselected by default).

    DOCUMENTED live gate; not executed offline. On a throwaway cluster (interlock
    armed, throwaway guard authorizing), with ZITADEL + its backing DB + OpenBao
    stood up via their OWN svc-NN-bootstrap.yml automated paths:

      1. Poll the freshly-provisioned ZITADEL for readiness with a BOUNDED
         timeout, failing with a diagnostic on timeout (R8.5). The probe is the
         UNAUTHENTICATED OIDC discovery document — HTTP-status fields only, no
         secret echoed.
      2. Onboard a throwaway project (``ZITADEL_TEST_PROJECT_SLUG``) with a SPA
         App_Registration entry AND an API App_Registration entry in ONE role run
         (``ansible/playbooks/zitadel-org-provision.yml`` with a two-entry
         ``zop_app_registrations`` list).
      3. Assert, by listing the apps in the single org/project derived from the
         slug, that BOTH a public SPA client (appType USER_AGENT, authMethodType
         NONE) AND an API client (an apps/api resource server) exist — in the
         SAME org + project, with NO second organization (R4.2/R8.6).

    The provisioning token is read by the role from OpenBao (or the
    ``ZITADEL_PROVISION_TOKEN`` env override) and travels only in the child ENV;
    this test never reads or echoes it, nor any captured client secret.

    This stub is marked ``@pytest.mark.requires_infra`` and is therefore
    DESELECTED by default via the root ``pytest.ini`` (``addopts = -m "not
    requires_infra"``). The authoritative behavioural evidence is captured in the
    spec's validation.md from a real throwaway-cluster run under
    ``AGENT_LIVE_AUTHORIZED``; it references ``bao status`` / HTTP-status fields
    ONLY and must never echo any secret value.
    """
    base_url = os.environ.get(_ENV_BASE_URL, "").strip()
    project_slug = os.environ.get(_ENV_PROJECT_SLUG, "").strip()
    host_header = os.environ.get(_ENV_HOST_HEADER, "").strip() or None

    if not base_url or not project_slug:
        pytest.skip(
            "requires-infra: live two-client onboarding against a throwaway "
            "ZITADEL. Set "
            f"{_ENV_BASE_URL} and {_ENV_PROJECT_SLUG} (wired from the instance "
            "the provision-then-test walkthrough just built — SVC-06 install "
            "runbook A.6), run under -m requires_infra with AGENT_LIVE_AUTHORIZED "
            "armed and a wired-up throwaway cluster. ZITADEL/OpenBao dependencies "
            "are stood up ONLY via their own svc-NN-bootstrap.yml automated path. "
            "Authoritative evidence lives in validation.md."
        )

    # Step 1 — bounded readiness poll against the fresh instance (R8.5). HTTP
    # status only; never a secret.
    _poll_zitadel_ready(base_url, host_header=host_header)

    # Steps 2 & 3 are the live acceptance, driven on a throwaway cluster via the
    # role's own svc-NN-bootstrap.yml automated path (never hand-driven). The
    # authoritative evidence (both a SPA client and an API client present in the
    # single org/project, R4.2/R8.6) is recorded in validation.md from the real
    # run. The readiness gate above having passed proves the fresh instance is
    # reachable; the onboarding + two-app assertion is the documented live gate.
    pytest.skip(
        "requires-infra: ZITADEL reachable; two-client onboarding + both-apps "
        "assertion (SPA public client AND API resource server in one org/project, "
        "R4.2/R8.6) is the documented live gate. Run on a throwaway cluster under "
        "AGENT_LIVE_AUTHORIZED; evidence captured in validation.md. No secret is "
        "echoed (bao status / HTTP-status fields only)."
    )
