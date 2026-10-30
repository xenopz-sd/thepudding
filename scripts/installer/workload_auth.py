#!/usr/bin/env python3
"""Pure validators + ``agent.hcl`` renderer for the openbao-workload-auth feature.

Feature: openbao-workload-auth (design.md "Correctness Properties"; tasks.md
Task 1).

This module is the ONE genuinely pure, input-varying, side-effect-free surface of
the openbao-workload-auth feature: the TTL / num_uses **input validators**, the
``workload_role_name`` **helper**, and the ``agent.hcl`` **renderer** (a pure
function from config values to config text). It is deliberately extracted as a
standalone, **Ansible-free, OpenBao-free, stdlib-only** module so it can be unit-
and property-tested offline, independent of Ansible and a live Primary.

Both Ansible roles this feature ships (``openbao_workload_approle`` control-plane
and ``openbao-agent`` data-plane) bind to the shapes defined here as a SHARED
SYMBOL:

* the control role's STEP 2 TTL/num_uses guards reuse
  :func:`validate_token_ttls` / :func:`validate_secret_id_inputs`;
* the control + data roles compute the per-slug role name via
  :func:`workload_role_name`;
* the data role's ``templates/agent.hcl.j2`` is kept structurally equivalent to
  :func:`render_agent_hcl`, so the golden-file test and the live render
  cross-check one another (design.md "Open risks and trade-offs").

Per ``testing-strategy.md`` "Verification Scope", any change to the signatures,
return types, or raised exceptions below is a shared-symbol change: it requires a
repo-wide caller search and the FULL offline suite, not just this feature's tests.

Nothing here reads the environment, the filesystem, or the network, and no secret
VALUE is ever templated into the rendered output (only file PATHS and KV
references appear). The renderer is deterministic: two calls on equal input
produce byte-identical text (Property 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "ValidationError",
    "SecretMapping",
    "AgentConfig",
    "WORKLOAD_SUFFIX",
    "validate_token_ttls",
    "validate_secret_id_inputs",
    "workload_role_name",
    "render_agent_hcl",
]

# The suffix that distinguishes the renewable Workload_AppRole from the sibling
# short-lived ``<slug>-ci`` JWT role created by ``openbao_project_onboard``.
WORKLOAD_SUFFIX = "-workload"
_CI_SUFFIX = "-ci"


class ValidationError(ValueError):
    """Raised by the validators when an input pair is outside the valid region.

    A subclass of :class:`ValueError` so callers may catch either. The Ansible
    control role surfaces the message via an ``assert``/``fail`` that runs BEFORE
    any write against the Primary (design.md STEP 2; Requirements 2.3, 3.7).
    """


# ---------------------------------------------------------------------------
# Validators (Properties 1 & 2)
# ---------------------------------------------------------------------------


def validate_token_ttls(token_ttl: int, token_max_ttl: int) -> tuple[int, int]:
    """Validate the Workload_AppRole token TTL pair.

    Accept the pair **iff** ``token_ttl > 0`` AND ``token_max_ttl > 0`` AND
    ``token_ttl <= token_max_ttl``; reject everything else.

    Accept contract: return the normalized ``(token_ttl, token_max_ttl)`` pair.
    Reject contract: raise :class:`ValidationError`.

    Requirements 2.2, 2.3; Correctness Property 1.
    """
    t = _as_int(token_ttl, "token_ttl")
    m = _as_int(token_max_ttl, "token_max_ttl")
    if t <= 0:
        raise ValidationError(
            f"token_ttl must be > 0 seconds, got {t}"
        )
    if m <= 0:
        raise ValidationError(
            f"token_max_ttl must be > 0 seconds, got {m}"
        )
    if t > m:
        raise ValidationError(
            f"token_ttl ({t}) must be <= token_max_ttl ({m})"
        )
    return t, m


def validate_secret_id_inputs(
    secret_id_ttl: int, secret_id_num_uses: int
) -> tuple[int, int]:
    """Validate the Secret_ID TTL / num-uses pair.

    Accept the pair **iff** ``secret_id_ttl > 0`` AND
    ``secret_id_num_uses >= 0``; reject everything else. Note
    ``secret_id_num_uses == 0`` is ACCEPTED (0 means unlimited uses), unlike the
    TTL which must be strictly positive.

    Accept contract: return the normalized ``(secret_id_ttl, secret_id_num_uses)``
    pair. Reject contract: raise :class:`ValidationError`.

    Requirements 3.7; Correctness Property 2.
    """
    ttl = _as_int(secret_id_ttl, "secret_id_ttl")
    num_uses = _as_int(secret_id_num_uses, "secret_id_num_uses")
    if ttl <= 0:
        raise ValidationError(
            f"secret_id_ttl must be > 0 seconds, got {ttl}"
        )
    if num_uses < 0:
        raise ValidationError(
            f"secret_id_num_uses must be >= 0 (0 = unlimited), got {num_uses}"
        )
    return ttl, num_uses


def _as_int(value: object, name: str) -> int:
    """Coerce an input to ``int``, rejecting bools and non-integers.

    ``bool`` is a subclass of ``int`` in Python; a ``True``/``False`` slipping in
    as a TTL is a caller error, not "1"/"0", so it is rejected explicitly. This
    keeps the accept/reject regions in the properties unambiguous.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer, got bool {value!r}")
    if not isinstance(value, int):
        raise ValidationError(
            f"{name} must be an integer, got {type(value).__name__} {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Role-name helper (Property 3)
# ---------------------------------------------------------------------------


def workload_role_name(slug: str) -> str:
    """Return the Workload_AppRole name for ``slug`` — ``"<slug>-workload"``.

    Always ends with :data:`WORKLOAD_SUFFIX` and is never equal to the sibling
    CI role name ``"<slug>-ci"`` for any non-empty slug.

    Requirements 2.4; Correctness Property 3.
    """
    if not isinstance(slug, str):
        raise ValidationError(
            f"slug must be a string, got {type(slug).__name__}"
        )
    if not slug:
        raise ValidationError("slug must be a non-empty string")
    return f"{slug}{WORKLOAD_SUFFIX}"


# ---------------------------------------------------------------------------
# agent.hcl renderer (Properties 4, 5, 6, 7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretMapping:
    """One secret mapping = one ``template`` stanza in the rendered ``agent.hcl``.

    * ``kv_subpath`` — the KV v2 item under ``secret/data/<slug>/<kv_subpath>``.
    * ``kv_field``   — the field within that item's ``.Data.data``.
    * ``env_key``    — the KEY written into the Rendered_Secret_File.
    """

    kv_subpath: str
    kv_field: str
    env_key: str


@dataclass(frozen=True)
class AgentConfig:
    """Pure input to :func:`render_agent_hcl` (no Ansible/OpenBao objects).

    Mirrors the ``openbao-agent`` role defaults consumed by ``agent.hcl.j2`` so
    the Python renderer and the Jinja template stay structurally equivalent.
    """

    project_slug: str
    primary_addr: str
    role_id_file_path: str
    secret_id_file_path: str
    token_sink_path: str
    rendered_secret_file: str
    secret_mappings: Sequence[SecretMapping]
    tls_skip_verify: bool = False
    num_retries: int = 0
    approle_mount_path: str = "auth/approle"
    remove_secret_id_after_read: bool = False
    pid_file: str = "/openbao/agent/agent.pid"


def render_agent_hcl(config: AgentConfig) -> str:
    """Render the OpenBao Agent ``agent.hcl`` as a deterministic text blob.

    The output contains, in a stable order:

    * a ``vault`` block with ``address`` (:attr:`AgentConfig.primary_addr`), the
      ``tls_skip_verify = true`` line emitted **iff**
      :attr:`AgentConfig.tls_skip_verify` is true (Property 6), and a
      ``retry { num_retries = N }`` stanza;
    * exactly one ``auto_auth`` block with a ``method "approle"`` reading the
      given ``role_id_file_path`` / ``secret_id_file_path`` and a token
      ``sink "file"`` (Property 4);
    * exactly one ``template`` stanza per entry in
      :attr:`AgentConfig.secret_mappings`, each referencing
      ``secret/data/<slug>/<kv_subpath>`` and emitting ``env_key`` into the
      single :attr:`AgentConfig.rendered_secret_file` at perms ``0600``
      (Property 5);
    * ``exit_after_auth = false``.

    Determinism (Property 7): the function performs no iteration over unordered
    containers and interpolates no non-deterministic value, so two calls on an
    equal :class:`AgentConfig` return byte-identical strings.

    No secret VALUE is ever rendered — only file paths and the literal OpenBao
    ``template`` directives that the Agent later expands at runtime (Req 4.8).
    """
    if not isinstance(config, AgentConfig):
        raise ValidationError(
            f"config must be an AgentConfig, got {type(config).__name__}"
        )
    if not config.secret_mappings:
        raise ValidationError(
            "config.secret_mappings must be a non-empty sequence"
        )
    slug = config.project_slug
    if not slug:
        raise ValidationError("config.project_slug must be a non-empty string")

    lines: list[str] = []
    lines.append("# MANAGED BY ANSIBLE (openbao-agent) — DO NOT EDIT BY HAND.")
    lines.append(f'pid_file = "{config.pid_file}"')
    lines.append("")

    # vault block
    lines.append("vault {")
    lines.append(f'  address = "{config.primary_addr}"')
    if config.tls_skip_verify:
        lines.append("  tls_skip_verify = true")
    lines.append(f"  retry {{ num_retries = {int(config.num_retries)} }}")
    lines.append("}")
    lines.append("")

    # auto_auth block
    remove_flag = "true" if config.remove_secret_id_after_read else "false"
    lines.append("auto_auth {")
    lines.append('  method "approle" {')
    lines.append(f'    mount_path = "{config.approle_mount_path}"')
    lines.append("    config = {")
    lines.append(f'      role_id_file_path   = "{config.role_id_file_path}"')
    lines.append(f'      secret_id_file_path = "{config.secret_id_file_path}"')
    lines.append(
        f"      remove_secret_id_file_after_reading = {remove_flag}"
    )
    lines.append("    }")
    lines.append("  }")
    lines.append('  sink "file" {')
    lines.append(f'    config = {{ path = "{config.token_sink_path}" }}')
    lines.append("  }")
    lines.append("}")
    lines.append("")

    # one template stanza per mapping, in the given (deterministic) order
    for mapping in config.secret_mappings:
        kv_path = f"secret/data/{slug}/{mapping.kv_subpath}"
        # The OpenBao Agent template directive. We assemble the literal
        # consul-template string the Agent expands at RUNTIME; the braces are
        # emitted verbatim (no secret value is substituted here). The inner
        # double quotes around the KV path are escaped (\") because the directive
        # is itself the value of a double-quoted HCL ``contents`` string — this
        # matches the design's ``agent.hcl.j2`` (``\"secret/data/...\"``).
        contents = (
            '{{ with secret \\"' + kv_path + '\\" }}'
            + mapping.env_key
            + "={{ .Data.data."
            + mapping.kv_field
            + " }}{{ end }}"
        )
        lines.append("template {")
        lines.append(f'  contents    = "{contents}"')
        lines.append(f'  destination = "{config.rendered_secret_file}"')
        lines.append('  perms       = "0600"')
        lines.append("}")
        lines.append("")

    lines.append("exit_after_auth = false")
    lines.append("")

    return "\n".join(lines)
