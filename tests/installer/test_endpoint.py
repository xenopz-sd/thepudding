"""Bug condition exploration tests for fix-cluster-env-endpoint-path (Task 1).

Feature: fix-cluster-env-endpoint-path, Property 1: Bug Condition — endpoint
normalized to a canonical base.

These tests encode the EXPECTED (fixed) behaviour of the endpoint-path defect in
the SVC-07 automated installer. The committed ``cluster.env.example`` documents
the Proxmox endpoint WITH a ``/api2/json`` API path, but both consumers expect a
bare base URL:

* Phase-1 Preflight in ``ansible/playbooks/svc-07-bootstrap.yml`` APPENDS
  ``/api2/json...`` (lines 454/471/504), so a ``/api2/json``-suffixed endpoint
  produces a doubled ``.../api2/json/api2/json/version`` URL Proxmox rejects with
  HTTP 501.
* A base-plus-trailing-slash endpoint (``https://h:8006/``) produces a
  double-slash ``.../8006//api2/json/version`` URL Proxmox rejects with HTTP 500
  (``no such file '/json/version'`` — observed live).
* The bpg/proxmox provider wants "No trailing path beyond the host/port."

The fix (design.md Option A) normalizes the resolved endpoint to a canonical base
in the playbook. Because Option A is Jinja-only there is no Python helper to
unit-test directly; this file therefore targets a small Python reference of the
exact normalization rule (``scripts/installer/endpoint.py::canonical_base``) that
the playbook can call via the venv python or mirror in Jinja. That module is
created as part of the FIX (Task 3), NOT here — so on UNFIXED code the import
below fails and this whole module fails to collect. That collection failure IS
the exploration counterexample confirming the bug.

The endpoint tests live in this SEPARATE file (not test_cluster_env.py) so the
ImportError isolates to this module and does not knock out the green
``cluster_env`` / quote-stripping tests.
"""

from __future__ import annotations

# On UNFIXED code this import raises ImportError (endpoint.py / canonical_base do
# not exist yet) — the whole module fails to collect. That is the exploration
# failure; DO NOT create endpoint.py to make it pass (that is Task 3).
from endpoint import canonical_base


def test_canonical_base_strips_api2json():
    """Feature: fix-cluster-env-endpoint-path, Property 1: Bug Condition —
    endpoint normalized to a canonical base.

    An endpoint carrying a trailing ``/api2/json`` normalizes to the bare base,
    so Phase-1 Preflight builds a single-path ``.../api2/json/version`` URL rather
    than the doubled ``.../api2/json/api2/json/version`` that earned HTTP 501.
    """
    assert canonical_base("https://h:8006/api2/json") == "https://h:8006"


def test_canonical_base_strips_api2json_with_trailing_slash():
    """Feature: fix-cluster-env-endpoint-path, Property 1: Bug Condition —
    endpoint normalized to a canonical base.

    ``/api2/json/`` (API path plus a trailing slash) normalizes to the same bare
    base — the slash the strip exposes must also be removed.
    """
    assert canonical_base("https://h:8006/api2/json/") == "https://h:8006"


def test_canonical_base_strips_trailing_slash():
    """Feature: fix-cluster-env-endpoint-path, Property 1: Bug Condition —
    endpoint normalized to a canonical base.

    The live trailing-slash failure case: ``https://h:8006/`` normalizes to
    ``https://h:8006`` so the appended API path is ``/api2/json/version`` and NOT
    the double-slash ``//api2/json/version`` Proxmox rejected with HTTP 500.
    """
    assert canonical_base("https://h:8006/") == "https://h:8006"


def test_canonical_base_already_canonical_is_noop():
    """Feature: fix-cluster-env-endpoint-path, Property 1: Bug Condition —
    endpoint normalized to a canonical base.

    An already-canonical bare base is unchanged (normalization is a no-op), so a
    correctly-supplied endpoint keeps building the request URL exactly as today.
    """
    assert canonical_base("https://h:8006") == "https://h:8006"


# =============================================================================
# Task 2 — Preservation + property tests for canonical_base
#
# Feature: fix-cluster-env-endpoint-path, Property 2: Preservation.
#
# These characterize the non-bug edges and the universal form-collapse /
# idempotence guarantee of the normalizer. They still FAIL on UNFIXED code
# (endpoint.py is missing, so this whole module fails to collect) and PASS after
# Task 3 creates scripts/installer/endpoint.py. They complement the four Task-1
# example cases above.
# =============================================================================

from hypothesis import given
from hypothesis import strategies as st


def test_canonical_base_idempotent():
    """Feature: fix-cluster-env-endpoint-path, Property 2: Preservation.

    ``canonical_base`` is idempotent: normalizing an already-normalized value is
    a no-op, for each accepted input form.
    """
    for form in (
        "https://h:8006",
        "https://h:8006/",
        "https://h:8006/api2/json",
        "https://h:8006/api2/json/",
        "http://host.example:8006/api2/json",
        "https://host.example",
    ):
        once = canonical_base(form)
        assert canonical_base(once) == once


def test_canonical_base_preserves_scheme_host_port():
    """Feature: fix-cluster-env-endpoint-path, Property 2: Preservation.

    A canonical base with no trailing junk (``scheme://host:port``) is returned
    byte-identical — normalization of an already-canonical base is a no-op.
    """
    assert canonical_base("https://host.example:8006") == "https://host.example:8006"
    assert canonical_base("http://host.example:8006") == "http://host.example:8006"
    # No port is equally valid and must be preserved untouched.
    assert canonical_base("https://host.example") == "https://host.example"


# --- Property 2 (form-collapse + idempotence), Hypothesis ---------------------
#
# Keep the host/port alphabet simple (no embedded slashes/quotes) so that the
# only path segments present are the ones the suffix adds.
_scheme = st.sampled_from(("http", "https"))
_host = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-", min_size=1, max_size=20
).filter(lambda h: not h.startswith("-") and not h.endswith("-") and ".." not in h)
_port = st.one_of(st.none(), st.integers(min_value=1, max_value=65535))
_suffix = st.sampled_from(("", "/", "/api2/json", "/api2/json/"))


@given(scheme=_scheme, host=_host, port=_port, suffix=_suffix)
def test_property2_form_collapse_and_idempotence(scheme, host, port, suffix):
    """Feature: fix-cluster-env-endpoint-path, Property 2: Preservation.

    For a generated ``scheme://host[:port]`` base and any suffix drawn from
    {'', '/', '/api2/json', '/api2/json/'}, all four forms collapse to the same
    canonical base, that base equals the bare base itself, and normalization is
    idempotent.
    """
    base = f"{scheme}://{host}" + (f":{port}" if port is not None else "")

    normalized = canonical_base(base + suffix)

    # Form-collapse: every accepted suffix reduces to the same canonical base,
    # which is the bare base itself.
    assert normalized == base
    assert canonical_base(base) == base
    # Idempotence.
    assert canonical_base(normalized) == normalized
