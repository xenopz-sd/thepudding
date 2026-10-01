#!/usr/bin/env python3
"""Throwaway-cluster live-authorization decision helper (pure core).

Feature: agent-live-test-authorization (Req 1.1, 1.4, 1.5, 1.6, 2.1, 2.6, 4.1,
4.3; design "Component 1: scripts/installer/throwaway_guard.py"; Correctness
Properties 1-4).

This is a service-agnostic authorization gate that lets the Kiro agent run
destructive live operations (from-scratch bring-ups, ``clean_slate`` resets,
``terraform apply``/``destroy``) against a **designated throwaway Proxmox
cluster only**, behind an off-by-default per-run interlock
(``AGENT_LIVE_AUTHORIZED``, with an optional ``AGENT_LIVE_AUTHORIZED_SERVICES``
narrowing) and a machine-checked cluster-identity guard that FAILS CLOSED.

It follows the established ``cluster_env.py`` / ``endpoint.py`` mold: a pure,
offline-testable decision core plus a thin CLI wrapper that owns all IO. This
module contains the **pure core only**; the ``main()`` CLI IO wrapper (env
reading, ``cluster_env`` identity resolution, allowlist YAML load, exit-code
mapping) is added by Task 3.2.

Design invariants honoured here:

* **Fail closed, always (Property 1).** :func:`decide` returns ``authorized=True``
  on exactly one fully-specified path — armed AND (scope is None or grants the
  service) AND identity fully resolved AND allowlist non-empty AND the normalized
  identity is on the allowlist. Every other input yields REFUSED.
* **Never raises (Property 4).** :func:`decide` returns a :class:`Decision` for
  any input; it performs no network, subprocess, file, or environment access.
* **Service-agnostic core, per-service refinement (Property 3).** One global arm;
  an optional per-service scope can only NARROW authorization, never broaden it.
* **Host normalization (Property 2).** Endpoint spellings differing only by
  scheme, port, trailing path (``/api2/json``, slashes), or case reduce to a
  single comparable host token via :func:`normalize_host` (reusing
  ``endpoint.canonical_base``), so equivalent spellings match one allowlist entry.
* **Non-secret throughout.** The guard consumes only the non-secret cluster
  identity (endpoint host, node name) and boolean interlock signals; it reads,
  logs, and embeds no credential.

The reason string in a REFUSED :class:`Decision` names the FIRST failing gate,
in the ordered sequence armed -> scope -> identity-resolved -> allowlist-non-empty
-> identity-on-allowlist.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from endpoint import canonical_base

# --- Interlock truthiness -----------------------------------------------------
#
# AGENT_LIVE_AUTHORIZED is armed iff its trimmed, lowercased value is in this set.
# Everything else (including None, "", "0", "false", "off") is unarmed.
_ARMED_VALUES = frozenset({"1", "true", "yes", "on"})

# Strip a leading scheme so a bare host token can be compared. canonical_base
# already removed a trailing /api2/json + slashes; this removes the scheme, and
# the port/path stripping below reduces the remainder to host-only.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


class ClusterIdentity(NamedTuple):
    """The non-secret identity of a Proxmox cluster the guard evaluates."""

    endpoint_host: str  # normalized host: scheme/port/path/case-stripped
    node_name: str  # exact PVE node name after trim


class AllowEntry(NamedTuple):
    """One throwaway-cluster identity from the allowlist.

    ``endpoint_host`` is normalized the same way as :class:`ClusterIdentity`'s so
    the membership comparison is apples-to-apples.
    """

    endpoint_host: str
    node_name: str


class Decision(NamedTuple):
    """The structured, non-secret result of :func:`decide`.

    ``reason`` is human-readable and names the first failing gate on REFUSED (or
    the authorized identity on AUTHORIZED). ``identity`` carries the resolved
    (non-secret) identity for INFO-level audit logging, or None when it could not
    be resolved.
    """

    authorized: bool
    reason: str
    identity: ClusterIdentity | None


def normalize_host(endpoint_or_host: str) -> str:
    """Reduce an endpoint or bare host to a comparable host token.

    Pure function (Req 2.6 / Property 2). Reuses
    :func:`endpoint.canonical_base` to strip a trailing ``/api2/json`` and any
    trailing slashes, then strips a leading ``https?://`` (any) scheme, any
    ``:port``, any leftover ``/path``, and lowercases + trims. So
    ``https://Node1.LAN:8006/api2/json`` and ``node1.lan`` both reduce to
    ``node1.lan``. Empty/whitespace input yields ``''`` (which fails closed
    downstream).
    """
    if endpoint_or_host is None:
        return ""
    # canonical_base trims and drops a trailing /api2/json + slashes.
    value = canonical_base(endpoint_or_host)
    # Drop the scheme (case-insensitively) so only host[:port][/path] remains.
    value = _SCHEME.sub("", value, count=1)
    # Everything from the first '/' onward is a leftover path — drop it.
    value = value.split("/", 1)[0]
    # A ':' introduces a port — drop it and anything after.
    value = value.split(":", 1)[0]
    return value.strip().lower()


def parse_services_scope(raw: str | None) -> frozenset[str] | None:
    """Parse ``AGENT_LIVE_AUTHORIZED_SERVICES`` into a scope set.

    Pure function (Req 1.4, 1.5 / Property 3):

    * None / absent / empty-after-trim -> ``None`` (no per-service narrowing; the
      global arm applies to every service).
    * A comma/space-separated list -> ``frozenset`` of lowered, trimmed tokens.
    * A present-but-only-separators value (e.g. ``",  ,"``) -> an EMPTY frozenset
      (narrows to NOTHING — fail closed for every service; an explicit "scope to
      nothing").
    """
    if raw is None:
        return None
    if not raw.strip():
        return None
    tokens = [tok.strip().lower() for tok in re.split(r"[,\s]+", raw) if tok.strip()]
    return frozenset(tokens)


def is_armed(interlock_raw: str | None) -> bool:
    """Return True iff ``AGENT_LIVE_AUTHORIZED`` is armed.

    Pure function (Req 1.1). Truthy iff the trimmed, lowercased value is one of
    ``{"1", "true", "yes", "on"}``; everything else (including None, ``"0"``,
    ``"false"``, ``"off"``, arbitrary strings) is False.
    """
    if interlock_raw is None:
        return False
    return interlock_raw.strip().lower() in _ARMED_VALUES


def decide(
    *,
    service: str,
    interlock_raw: str | None,
    services_scope_raw: str | None,
    identity: ClusterIdentity | None,
    allowlist: tuple[AllowEntry, ...],
) -> Decision:
    """Decide whether a Destructive_Live_Action is AUTHORIZED. PURE, never raises.

    Five ORDERED checks; the returned REFUSED reason names the FIRST failing gate
    (Req 1.1, 1.5, 1.6, 2.1, 2.6, 4.1 / Properties 1-4):

    1. armed — ``is_armed(interlock_raw)`` else "interlock unset
       (AGENT_LIVE_AUTHORIZED not armed)".
    2. scope — a present ``AGENT_LIVE_AUTHORIZED_SERVICES`` scope must grant the
       service, else "service <service> out of AGENT_LIVE_AUTHORIZED_SERVICES
       scope". An absent scope (None) preserves the global arm.
    3. identity resolved — ``identity`` is not None and both fields are non-empty
       after strip, else "cluster identity unresolved".
    4. allowlist non-empty — else "throwaway allowlist empty or unreadable".
    5. membership — the normalized ``ClusterIdentity`` is on the allowlist, else
       "identity <host>/<node> not on throwaway allowlist".

    AUTHORIZED (reason "authorized: <host>/<node> is a listed throwaway cluster")
    is returned on exactly one path — all five checks pass. The resolved identity
    is included in the :class:`Decision` for logging whenever it is available.
    """
    # Resolve the identity to its normalized/trimmed comparable form up front so
    # every return can carry it for logging. A None identity stays None.
    resolved: ClusterIdentity | None = None
    if identity is not None:
        resolved = ClusterIdentity(
            endpoint_host=normalize_host(identity.endpoint_host),
            node_name=identity.node_name.strip(),
        )

    # 1. Interlock armed?
    if not is_armed(interlock_raw):
        return Decision(
            authorized=False,
            reason="interlock unset (AGENT_LIVE_AUTHORIZED not armed)",
            identity=resolved,
        )

    # 2. Per-service scope grants this service? (absent scope => global arm)
    scope = parse_services_scope(services_scope_raw)
    if scope is not None and service.lower() not in scope:
        return Decision(
            authorized=False,
            reason=f"service {service} out of AGENT_LIVE_AUTHORIZED_SERVICES scope",
            identity=resolved,
        )

    # 3. Cluster identity fully resolved (both fields non-empty)?
    if resolved is None or not resolved.endpoint_host or not resolved.node_name:
        return Decision(
            authorized=False,
            reason="cluster identity unresolved",
            identity=resolved,
        )

    # 4. Allowlist non-empty?
    if not allowlist:
        return Decision(
            authorized=False,
            reason="throwaway allowlist empty or unreadable",
            identity=resolved,
        )

    # 5. Normalized identity on the allowlist?
    if resolved not in allowlist:
        return Decision(
            authorized=False,
            reason=(
                f"identity {resolved.endpoint_host}/{resolved.node_name} "
                f"not on throwaway allowlist"
            ),
            identity=resolved,
        )

    # All five gates passed — the single AUTHORIZED path.
    return Decision(
        authorized=True,
        reason=(
            f"authorized: {resolved.endpoint_host}/{resolved.node_name} "
            f"is a listed throwaway cluster"
        ),
        identity=resolved,
    )


# ---------------------------------------------------------------------------
# Thin IO wrapper (main) — the ONLY IO seam (Task 3.2)
# ---------------------------------------------------------------------------
#
# Everything ABOVE this seam is pure (no network, subprocess, file, or env
# access). Everything BELOW owns all IO: argument parsing, environment reading,
# identity resolution via cluster_env, allowlist YAML loading, JSON emission, and
# exit-code mapping. The pure core must never depend on this section.
#
# CRITICAL non-secrecy posture (design "Component 1 -> Thin IO wrapper"):
#   * The identity is resolved by calling cluster_env.resolve_env_file() and
#     extracting ONLY the two NON-SECRET fields (TF_VAR_proxmox_endpoint,
#     TF_VAR_proxmox_node_name). The API token (TF_VAR_proxmox_api_token) is
#     NEVER read, printed, or logged.
#   * The emitted JSON carries only {authorized, reason, identity} — all
#     non-secret. It can never contain a token.

import argparse
import json
import os
import sys

import cluster_env


def load_allowlist(path: str) -> tuple[AllowEntry, ...]:
    """Load + parse the throwaway-cluster allowlist YAML, FAIL CLOSED.

    IO seam (Req 2.3 / Property 6). Reads ``path`` and expects a top-level
    ``throwaway_clusters`` key holding a list of ``{endpoint_host, node_name}``
    mappings. For each item, builds an :class:`AllowEntry` with the endpoint host
    ``normalize_host``-ed and the node name trimmed, DROPPING any item that is
    missing or has an empty value for either field.

    Any failure — file absent/unreadable, malformed YAML, a non-list or absent
    top-level key, or any other exception — yields an EMPTY tuple ``()`` so the
    guard fails closed (an empty allowlist REFUSES every identity). This function
    never raises.
    """
    # yaml is imported lazily here so the pure core stays free of the dependency;
    # a missing PyYAML (unlikely on the control node) fails closed like any other
    # error rather than crashing the guard.
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except Exception:
        return ()

    if not isinstance(data, dict):
        return ()
    raw_entries = data.get("throwaway_clusters")
    if not isinstance(raw_entries, list):
        return ()

    entries: list[AllowEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        host_raw = item.get("endpoint_host")
        node_raw = item.get("node_name")
        if host_raw is None or node_raw is None:
            continue
        host = normalize_host(str(host_raw))
        node = str(node_raw).strip()
        if not host or not node:
            continue
        entries.append(AllowEntry(endpoint_host=host, node_name=node))

    return tuple(entries)


def _resolve_identity(env_file: str) -> ClusterIdentity | None:
    """Resolve the NON-SECRET cluster identity from a Cluster_Env_File.

    IO seam. Calls :func:`cluster_env.resolve_env_file` and extracts ONLY the two
    non-secret fields — ``TF_VAR_proxmox_endpoint`` (normalized) and
    ``TF_VAR_proxmox_node_name`` (trimmed). NEVER reads or returns the API token.

    Any resolution failure — file absent/unreadable
    (:class:`cluster_env.EnvFileNotFoundError`), validation failure
    (:class:`cluster_env.ValidationError`), or a missing expected key
    (:class:`KeyError`) — maps to ``None`` (fail closed, NOT a crash), so the
    guard's identity-resolved gate refuses rather than the process aborting.
    """
    try:
        resolved = cluster_env.resolve_env_file(env_file)
        endpoint = resolved["TF_VAR_proxmox_endpoint"]
        node = resolved["TF_VAR_proxmox_node_name"]
    except (cluster_env.EnvFileNotFoundError, cluster_env.ValidationError, KeyError):
        return None
    return ClusterIdentity(
        endpoint_host=normalize_host(endpoint),
        node_name=node.strip(),
    )


def main(argv: list[str] | None = None) -> int:
    """Thin CLI wrapper for the throwaway-cluster authorization guard.

    Reads the interlock signals from the process ENVIRONMENT
    (``AGENT_LIVE_AUTHORIZED``, ``AGENT_LIVE_AUTHORIZED_SERVICES``), resolves the
    non-secret cluster identity from ``--env-file`` (fail closed on any error),
    loads the allowlist from ``--allowlist`` (fail closed on any error), calls the
    pure :func:`decide`, and emits the :class:`Decision` as NON-SECRET JSON to
    stdout.

    Exit codes:
    * ``0`` — AUTHORIZED.
    * ``3`` — REFUSED (the decision denied the run).
    * ``2`` — usage error (argparse emits this automatically on bad args).

    The emitted JSON is ``{"authorized": bool, "reason": str, "identity":
    {"endpoint_host": str, "node_name": str} | null}`` — non-secret only; it can
    never contain a token.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Decide whether a destructive live operation is AUTHORIZED against a "
            "designated throwaway Proxmox cluster (fail-closed)."
        )
    )
    parser.add_argument(
        "--service",
        required=True,
        help="Service code requesting authorization (e.g. svc07).",
    )
    parser.add_argument(
        "--env-file",
        required=True,
        help="Path to the throwaway Cluster_Env_File (e.g. .env.throwaway).",
    )
    parser.add_argument(
        "--allowlist",
        required=True,
        help="Path to the throwaway-cluster allowlist YAML "
        "(scripts/installer/throwaway-clusters.yml).",
    )
    args = parser.parse_args(argv)

    interlock_raw = os.environ.get("AGENT_LIVE_AUTHORIZED")
    services_scope_raw = os.environ.get("AGENT_LIVE_AUTHORIZED_SERVICES")

    identity = _resolve_identity(args.env_file)
    allowlist = load_allowlist(args.allowlist)

    decision = decide(
        service=args.service,
        interlock_raw=interlock_raw,
        services_scope_raw=services_scope_raw,
        identity=identity,
        allowlist=allowlist,
    )

    payload = {
        "authorized": decision.authorized,
        "reason": decision.reason,
        "identity": (
            {
                "endpoint_host": decision.identity.endpoint_host,
                "node_name": decision.identity.node_name,
            }
            if decision.identity is not None
            else None
        ),
    }
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")

    return 0 if decision.authorized else 3


if __name__ == "__main__":
    raise SystemExit(main())
