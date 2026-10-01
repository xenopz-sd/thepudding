"""Hypothesis property tests for the PURE core of ``throwaway_guard.py``.

Feature: agent-live-test-authorization (design "Correctness Properties" P1-P4;
Task 4.1). The module under test
(``scripts/installer/throwaway_guard.py``) is the service-agnostic
authorization gate that decides whether a Destructive_Live_Action is AUTHORIZED
against a designated throwaway Proxmox cluster, FAILING CLOSED on any ambiguity.

Only the PURE decision core is exercised here — :func:`throwaway_guard.decide`
and the pure helpers :func:`normalize_host`, :func:`parse_services_scope`,
:func:`is_armed`. There is NO IO: no file access, no environment reads, no
monkeypatching of files/env. The thin ``main()`` CLI wrapper (allowlist YAML
load, ``cluster_env`` resolution, exit codes, non-secrecy) is covered by the
separate example/unit tests in Task 4.2.

The four properties (each docstring names its property number and the
requirement clauses it validates):

* P1 fail-closed invariant  -> Req 1.1, 1.6, 2.1, 4.3
* P2 host normalization      -> Req 2.6
* P3 scope narrowing         -> Req 1.4, 1.5
* P4 never-raises            -> Req 4.1
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import throwaway_guard as g


# --- Shared generators --------------------------------------------------------
#
# Draw hosts and nodes from SMALL pools so a generated identity and a generated
# allowlist SOMETIMES coincide and sometimes do not — this exercises the
# authorization IFF in BOTH directions (member -> AUTHORIZED, non-member ->
# REFUSED) rather than almost-always missing.

# Bare, already-normalized host tokens (lowercase, no scheme/port/path).
_HOST_POOL = ("shrimp.lan", "node1.lan", "192.168.0.69", "pve.example")
# PVE node names (exact-after-trim comparison).
_NODE_POOL = ("shrimp", "node1", "pve")

_hosts = st.sampled_from(_HOST_POOL)
_nodes = st.sampled_from(_NODE_POOL)

# Service codes; a small pool so scope membership hits both ways.
_SERVICE_POOL = ("svc07", "svc01", "svc03")
_services = st.sampled_from(_SERVICE_POOL)

# Interlock raw values: a mix of armed ({1,true,yes,on}, any case/padding) and
# unarmed (None, "0", "false", "off", junk, empty).
_ARMED_RAW = ("1", "true", "TRUE", "Yes", " on ", "On")
_UNARMED_RAW = (None, "", "  ", "0", "false", "off", "no", "maybe", "2", "enabled")
_interlock_raw = st.sampled_from(_ARMED_RAW + _UNARMED_RAW)

# services-scope raw values: None/empty (no narrowing), separators-only (narrow
# to nothing), and comma/space lists drawn from the service pool + a stranger.
_scope_raw = st.one_of(
    st.none(),
    st.sampled_from(("", "   ", ",", " , ", "\t,\t")),
    st.lists(st.sampled_from(_SERVICE_POOL + ("svc99",)), min_size=1, max_size=3).map(
        lambda toks: ", ".join(toks)
    ),
)


@st.composite
def _identities(draw):
    """A ClusterIdentity drawn from the small pools, or None.

    The endpoint host is drawn already-normalized (P1 tests membership, not
    spelling — P2 owns spelling variance). Node is exact-after-trim.
    """
    if draw(st.booleans()):
        return None
    return g.ClusterIdentity(
        endpoint_host=draw(_hosts),
        node_name=draw(_nodes),
    )


@st.composite
def _allowlists(draw):
    """A tuple of AllowEntry drawn from the same small pools (possibly empty).

    Because entries are drawn from the SAME host/node pools as the identities,
    an identity's normalized form lands in the allowlist for a meaningful
    fraction of examples, driving the IFF in both directions.
    """
    entries = draw(
        st.lists(
            st.builds(g.AllowEntry, endpoint_host=_hosts, node_name=_nodes),
            max_size=4,
        )
    )
    return tuple(entries)


def _expected_authorized(
    *,
    service: str,
    interlock_raw,
    services_scope_raw,
    identity,
    allowlist: tuple,
) -> bool:
    """Re-derive the expected AUTHORIZED boolean INDEPENDENTLY of decide().

    Uses the guard's own pure helpers (is_armed / parse_services_scope /
    normalize_host) to compute the five-gate conjunction, mirroring the design
    spec without calling decide(). This is the ground truth P1 asserts equal to
    decide().authorized.
    """
    if not g.is_armed(interlock_raw):
        return False

    scope = g.parse_services_scope(services_scope_raw)
    if scope is not None and service.lower() not in scope:
        return False

    if identity is None:
        return False
    norm_host = g.normalize_host(identity.endpoint_host)
    norm_node = identity.node_name.strip()
    if not norm_host or not norm_node:
        return False

    if not allowlist:
        return False

    resolved = g.ClusterIdentity(endpoint_host=norm_host, node_name=norm_node)
    return resolved in allowlist


# =============================================================================
# P1 — Fail-closed authorization invariant
# Feature: agent-live-test-authorization, Property 1.
# Validates: Requirements 1.1, 1.6, 2.1, 4.3
# =============================================================================


@settings(max_examples=400)
@given(
    service=_services,
    interlock_raw=_interlock_raw,
    services_scope_raw=_scope_raw,
    identity=_identities(),
    allowlist=_allowlists(),
)
def test_p1_fail_closed_authorization_invariant(
    service, interlock_raw, services_scope_raw, identity, allowlist
):
    """P1: decide().authorized is True IFF the full five-gate conjunction holds.

    Feature: agent-live-test-authorization, Property 1: fail-closed
    authorization invariant.

    Validates: Requirements 1.1, 1.6, 2.1, 4.3.

    AUTHORIZED iff ALL of: armed AND (scope None or service in scope) AND
    identity fully resolved (both fields non-empty after normalize/trim) AND
    allowlist non-empty AND the normalized identity is on the allowlist. Every
    other input is REFUSED. The expected boolean is re-derived independently via
    the same pure helpers and asserted equal to decide().authorized. The pools
    are small so membership lands both true and false, exercising the IFF in
    both directions.
    """
    expected = _expected_authorized(
        service=service,
        interlock_raw=interlock_raw,
        services_scope_raw=services_scope_raw,
        identity=identity,
        allowlist=allowlist,
    )

    decision = g.decide(
        service=service,
        interlock_raw=interlock_raw,
        services_scope_raw=services_scope_raw,
        identity=identity,
        allowlist=allowlist,
    )

    assert decision.authorized is expected
    # A non-empty reason always accompanies the decision (for INFO logging).
    assert decision.reason


# =============================================================================
# P2 — Endpoint-host normalization
# Feature: agent-live-test-authorization, Property 2.
# Validates: Requirements 2.6
# =============================================================================

# normalize_host strips a leading scheme via a case-INsensitive regex
# (R2.6: scheme ignored), so the scheme keyword case IS varied here; the
# host-portion case is varied below and normalize_host lowercases it.
_SCHEME_CHOICES = ("", "http://", "https://", "HTTPS://", "Https://", "HTTP://")
_PORT_CHOICES = ("", ":8006")
_PATH_CHOICES = ("", "/", "/api2/json", "/api2/json/")


@st.composite
def _host_spelling(draw, base_host: str):
    """Spell ``base_host`` with a scheme/port/path/host-case variation.

    Every returned string differs from the others only by (lowercase) scheme,
    port, trailing path, and host-portion letter case — all of which
    normalize_host is required to erase — so they must all reduce to the single
    normalized ``base_host`` token.
    """
    scheme = draw(st.sampled_from(_SCHEME_CHOICES))
    port = draw(st.sampled_from(_PORT_CHOICES))
    path = draw(st.sampled_from(_PATH_CHOICES))
    # Randomize case of the host portion; normalize_host lowercases.
    cased = "".join(
        ch.upper() if draw(st.booleans()) else ch.lower() for ch in base_host
    )
    return f"{scheme}{cased}{port}{path}"


@settings(max_examples=300)
@given(base_host=_hosts, node=_nodes, data=st.data())
def test_p2_host_normalization_collapses_equivalent_spellings(base_host, node, data):
    """P2: scheme/port/path/case-only differences normalize to one token.

    Feature: agent-live-test-authorization, Property 2: endpoint-host
    normalization.

    Validates: Requirements 2.6.

    Two equivalent spellings of the same host (differing only by scheme, port,
    trailing path, and case) map to a single normalize_host token; and with an
    armed interlock + an allowlist containing the normalized identity, decide()
    AUTHORIZES regardless of the incoming spelling.
    """
    spelling_a = data.draw(_host_spelling(base_host))
    spelling_b = data.draw(_host_spelling(base_host))

    norm_a = g.normalize_host(spelling_a)
    norm_b = g.normalize_host(spelling_b)

    # All equivalent spellings collapse to one token (and it is the lowercased,
    # scheme/port/path-stripped base host).
    assert norm_a == norm_b == base_host.lower()

    # With the normalized identity on the allowlist and an armed run, any
    # incoming spelling authorizes (the host comparison cannot be bypassed by
    # spelling, nor falsely rejected).
    allowlist = (g.AllowEntry(endpoint_host=base_host.lower(), node_name=node),)
    decision = g.decide(
        service="svc07",
        interlock_raw="1",
        services_scope_raw=None,
        identity=g.ClusterIdentity(endpoint_host=spelling_a, node_name=node),
        allowlist=allowlist,
    )
    assert decision.authorized is True


# =============================================================================
# P3 — Per-service scope only narrows
# Feature: agent-live-test-authorization, Property 3.
# Validates: Requirements 1.4, 1.5
# =============================================================================


@settings(max_examples=300)
@given(service=_services, host=_hosts, node=_nodes, others=st.data())
def test_p3_present_scope_never_authorizes_out_of_scope_service(
    service, host, node, others
):
    """P3: a present scope never authorizes a service outside it.

    Feature: agent-live-test-authorization, Property 3: per-service scope only
    narrows.

    Validates: Requirements 1.4, 1.5.

    Even with the global arm set AND the identity allowlisted, a present
    AGENT_LIVE_AUTHORIZED_SERVICES scope that excludes the requested service
    yields REFUSED; and an absent scope (None/empty) preserves the global arm
    (authorizes). A scope can only narrow, never broaden.
    """
    allowlist = (g.AllowEntry(endpoint_host=host, node_name=node),)
    identity = g.ClusterIdentity(endpoint_host=host, node_name=node)

    # Build a scope that EXCLUDES `service` (drawn from the other pool members).
    excluding = [s for s in _SERVICE_POOL if s != service]
    scope_without = ", ".join(
        others.draw(st.lists(st.sampled_from(excluding), min_size=1, max_size=2))
    )

    refused = g.decide(
        service=service,
        interlock_raw="1",
        services_scope_raw=scope_without,
        identity=identity,
        allowlist=allowlist,
    )
    assert refused.authorized is False
    assert "scope" in refused.reason

    # A scope that INCLUDES `service` authorizes (narrowing that still grants).
    scope_with = f"{service}, {excluding[0]}"
    granted = g.decide(
        service=service,
        interlock_raw="1",
        services_scope_raw=scope_with,
        identity=identity,
        allowlist=allowlist,
    )
    assert granted.authorized is True

    # An ABSENT scope (None) preserves the global arm — authorizes for any
    # service (never narrower than the global arm).
    absent = g.decide(
        service=service,
        interlock_raw="1",
        services_scope_raw=None,
        identity=identity,
        allowlist=allowlist,
    )
    assert absent.authorized is True


# =============================================================================
# P4 — Guard never raises
# Feature: agent-live-test-authorization, Property 4.
# Validates: Requirements 4.1
# =============================================================================

# Deliberately odd / adversarial inputs for the never-raises property: weird
# service strings, arbitrary interlock/scope text (including None and
# separator-only), None or malformed identities, and empty allowlists.
_weird_text = st.text(max_size=12)
_weird_optional_text = st.one_of(st.none(), _weird_text)


@st.composite
def _weird_identity(draw):
    """A ClusterIdentity with arbitrary/odd field values, or None."""
    if draw(st.booleans()):
        return None
    return g.ClusterIdentity(
        endpoint_host=draw(_weird_text),
        node_name=draw(_weird_text),
    )


@st.composite
def _weird_allowlist(draw):
    """An allowlist tuple with arbitrary AllowEntry values (possibly empty)."""
    entries = draw(
        st.lists(
            st.builds(
                g.AllowEntry,
                endpoint_host=_weird_text,
                node_name=_weird_text,
            ),
            max_size=4,
        )
    )
    return tuple(entries)


@settings(max_examples=400)
@given(
    service=_weird_text,
    interlock_raw=_weird_optional_text,
    services_scope_raw=_weird_optional_text,
    identity=_weird_identity(),
    allowlist=_weird_allowlist(),
)
def test_p4_decide_never_raises_and_returns_decision(
    service, interlock_raw, services_scope_raw, identity, allowlist
):
    """P4: decide() returns a Decision for arbitrary inputs and never raises.

    Feature: agent-live-test-authorization, Property 4: guard never raises.

    Validates: Requirements 4.1.

    For arbitrary/odd inputs — including a None identity, an empty allowlist,
    weird strings, and empty/separator-only scope — decide() must return a
    g.Decision (never raise), with a boolean authorized flag and a non-empty
    reason. This pins the fail-closed "map every internal error to REFUSED"
    guarantee at the pure-core boundary.
    """
    decision = g.decide(
        service=service,
        interlock_raw=interlock_raw,
        services_scope_raw=services_scope_raw,
        identity=identity,
        allowlist=allowlist,
    )
    assert isinstance(decision, g.Decision)
    assert isinstance(decision.authorized, bool)
    assert isinstance(decision.reason, str) and decision.reason
