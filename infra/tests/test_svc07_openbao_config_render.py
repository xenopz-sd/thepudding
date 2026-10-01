"""Offline config-validation tests for the rendered OpenBao ``config.hcl`` files.

Task 5.4 (spec: svc-07-secrets-manager) — requirements.md Requirements 5.1, 5.2,
6.1, 10.1 (plus the storage-path assertion of Req 11); design.md "Testing
Strategy §1 (Unit / config-validation tests — Rendered-artifact validation)"
and the "Requirement → evidence map" rows 5.1, 5.2-5.3, 6.1, 10.1.

These are OFFLINE unit / config-validation tests for the CONFIGURATION layer of
SVC-07 — the two ``config.hcl`` artifacts the ``openbao_install`` role templates
(``templates/config.hcl.j2`` primary + ``templates/config.unsealer.hcl.j2``).
They are NOT property-based tests: per the design's (intentionally empty)
Correctness Properties section, a rendered declarative artifact's correctness
does not vary with generated input; one representative render pins the contract.

=== WHY THESE LIVE IN infra/tests/ ===

``pytest.ini`` at the repo root is the single pytest rootdir; ``infra/tests/`` is
the established test tree and the only place with the shared ``conftest.py`` on
``sys.path``. The repo has no separate ``ansible/tests/`` convention (the only
prior SVC-07 tests are the terraform-shape / state ones in this same
directory), so these artifact tests are placed here alongside them for a single
``~/venv/devinfra/bin/pytest infra/tests/`` entry point. They test an
ANSIBLE-rendered artifact rather than terraform, so they are self-contained and
do NOT import the terraform gate from ``conftest`` — they always run offline.

=== HOW THE OFFLINE RENDER WORKS ===

The two templates use ONLY plain ``{{ var }}`` substitution — no Ansible-specific
filters (no ``to_json``, ``to_nice_yaml``, etc.) and no control flow. So a stock
``jinja2.Environment`` renders them faithfully once the role's
``defaults/main.yml`` variable set is loaded and its jinja self-references
(e.g. ``openbao_storage_path: "{{ openbao_data_dir }}"``) are resolved. We load
those defaults with PyYAML, resolve the self-references with a small
fixed-point pass (a handful of iterations; the references are shallow), and
render each template selecting the appropriate ``openbao_role`` and node id.

``openbao_transit_token`` is intentionally undefined in ``defaults`` (it is a
never-committed, runtime-injected bootstrap value). For a faithful OFFLINE
render we inject a NON-SECRET, obviously-fake placeholder token — it is not a
real credential and is never asserted upon except to prove the seal stanza is
present and points at the right address.

=== WHAT IS ASSERTED (task 5.4 contract) ===

Primary ``config.hcl``:
  * ``listener "tcp"`` has ``tls_disable = false`` and there is NO plaintext
    listener anywhere in the file (Req 6.1).
  * ``seal "transit"`` ``address`` is exactly the unsealer at ``10.0.20.11``
    (Req 5.2).
  * ``storage "raft"`` ``path`` is ``/openbao/data`` (Req 11).
  * ``telemetry`` ``prometheus_retention_time`` parses to >= 1 minute (Req 10.1).

Unsealer ``config.hcl``:
  * NO ``seal`` stanza at all (it IS the seal provider, not a consumer) (Req 5.1).
  * NO KV / database / aws / PKI secrets-engine mount declared in the config
    (Req 5.1) — the Transit engine is the only engine and is enabled at runtime
    by the init role, never declared in this file.
  * (belt-and-braces) still TLS-only, no plaintext listener.

An offline, dependency-free HCL parse is used (brace-balanced block extraction),
mirroring the sibling ``test_svc07_terraform_shape.py`` approach — no full HCL
parser is pinned in the venv, and one is not warranted for this shape checking.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_config_render.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

# --------------------------------------------------------------------------- #
# Locate the openbao_install role relative to this test file (infra/tests/...).
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_install"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_TEMPLATES = _ROLE_DIR / "templates"
_PRIMARY_TMPL = _TEMPLATES / "config.hcl.j2"
_UNSEALER_TMPL = _TEMPLATES / "config.unsealer.hcl.j2"

#: Non-secret, obviously-fake placeholder for the runtime-injected seal token.
#: The token is intentionally NOT in defaults/main.yml (never-committed); this
#: value only lets the primary template render so the seal STANZA can be
#: asserted structurally. It is never treated as a real credential.
_FAKE_TRANSIT_TOKEN = "s.OFFLINE-RENDER-PLACEHOLDER-not-a-real-token"  # noqa: S105

#: The unsealer's VLAN-20 address the primary's seal stanza must point at
#: (Req 5.2; NET-00 §3 cidrhost("10.0.20.0/24", 10 + 1)).
_UNSEALER_ADDR = "https://10.0.20.11:8200"

#: Req 11 — Raft storage path.
_STORAGE_PATH = "/openbao/data"

#: Secrets-engine mount keywords that MUST NOT appear in the unsealer config
#: (Req 5.1). ``transit`` is deliberately NOT here: the unsealer's mount path
#: `transit/` appears only at runtime via the init role, not in this file, and
#: we assert its ABSENCE from the config too (there is no `secrets`/mount stanza
#: at all in the templated config).
_FORBIDDEN_UNSEALER_ENGINES = ("kv", "database", "aws", "pki")


# --------------------------------------------------------------------------- #
# Load + resolve the role defaults (jinja self-references) once per session.
# --------------------------------------------------------------------------- #
def _resolve_self_references(raw: dict) -> dict:
    """Resolve ``{{ other_var }}`` self-references within the defaults dict.

    ``defaults/main.yml`` uses shallow jinja self-references between its own
    string scalars (e.g. ``openbao_storage_path: "{{ openbao_data_dir }}"``,
    ``openbao_api_addr: "https://{{ openbao_primary_ip }}:{{ openbao_api_port }}"``).
    Ansible would resolve these at runtime; offline we run a small fixed-point:
    repeatedly render every string scalar against the current dict until nothing
    changes (or a safe iteration cap is hit). Non-string values (ints, dicts,
    lists) are passed through unchanged — the nested ``openbao_env_*`` mapping
    values are shell-``${VAR:-default}`` strings that contain no jinja and so
    render to themselves.
    """
    env = Environment(undefined=StrictUndefined, autoescape=False)
    resolved = dict(raw)

    def _render_scalars(d: dict) -> tuple[dict, bool]:
        changed = False
        out = {}
        for key, val in d.items():
            if isinstance(val, str) and "{{" in val:
                try:
                    rendered = env.from_string(val).render(**resolved)
                except Exception:
                    # A scalar that references a not-yet-resolved var — leave it
                    # for a later iteration.
                    rendered = val
                if rendered != val:
                    changed = True
                out[key] = rendered
            else:
                out[key] = val
        return out, changed

    for _ in range(10):  # shallow references; converges in 2-3 passes
        resolved, changed = _render_scalars(resolved)
        if not changed:
            break
    return resolved


@pytest.fixture(scope="module")
def role_vars() -> dict:
    """The openbao_install ``defaults/main.yml`` set, self-references resolved."""
    assert _DEFAULTS.is_file(), f"expected role defaults at {_DEFAULTS}"
    raw = yaml.safe_load(_DEFAULTS.read_text())
    assert isinstance(raw, dict) and raw, "defaults/main.yml must be a non-empty mapping"
    return _resolve_self_references(raw)


def _ansible_bool(value) -> bool:
    """Minimal stand-in for Ansible's ``bool`` filter for offline rendering.

    The primary config.hcl gained a mock-cert-gated conditional
    (`{% if openbao_tls_self_signed | default(false) | bool %}`) for the seal
    stanza's `tls_skip_verify`. Ansible renders the template WITH its own `bool`
    filter at runtime; the offline harness registers this equivalent so it
    renders the template faithfully (truthy strings "true"/"1"/"yes"/"on" and
    real booleans → True; everything else → False), matching Ansible's `bool`.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _render_template(template_path: Path, variables: dict) -> str:
    """Render a role template offline with a jinja2 Environment.

    The templates are mostly plain ``{{ var }}`` substitution, plus a small
    mock-cert-gated conditional in the primary config.hcl that uses Ansible's
    ``bool`` filter — registered here via ``_ansible_bool`` so a faithful render
    matches Ansible's runtime behaviour. ``StrictUndefined`` makes a missing
    variable a hard error rather than a silent empty string, so the test fails
    loudly if the template grows a var the fixture doesn't supply.
    """
    assert template_path.is_file(), f"expected template at {template_path}"
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    tmpl = env.from_string(template_path.read_text())
    return tmpl.render(**variables)


@pytest.fixture(scope="module")
def primary_hcl(role_vars) -> str:
    """Rendered PRIMARY config.hcl (openbao_role = 'primary')."""
    variables = {
        **role_vars,
        "openbao_role": "primary",
        "openbao_transit_token": _FAKE_TRANSIT_TOKEN,
    }
    return _render_template(_PRIMARY_TMPL, variables)


@pytest.fixture(scope="module")
def unsealer_hcl(role_vars) -> str:
    """Rendered UNSEALER config.hcl (openbao_role = 'unsealer')."""
    variables = {
        **role_vars,
        "openbao_role": "unsealer",
        # No transit token needed — the unsealer template has no seal stanza.
    }
    return _render_template(_UNSEALER_TMPL, variables)


# --------------------------------------------------------------------------- #
# Minimal offline HCL helpers — brace-balanced block extraction + comment strip.
# Same idiom as the sibling test_svc07_terraform_shape.py (no full HCL parser).
# --------------------------------------------------------------------------- #
def _strip_hcl_comments(text: str) -> str:
    """Remove ``#`` / ``//`` line comments and ``/* ... */`` block comments.

    Prevents the templates' explanatory comment prose (which mentions
    ``tls_disable``, ``seal``, ``kv``, ``database``, ``aws``, ``pki`` by name)
    from being miscounted as real config when asserting presence/absence.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = []
    for line in text.splitlines():
        # Strip `#` line comments (HCL native, used by these templates).
        line = line.split("#", 1)[0]
        # Strip `//` line comments, but NOT the `//` inside a URL scheme
        # (e.g. https://10.0.20.11:8200 as a seal address value). A `://`
        # is never a comment start; only a `//` not preceded by `:` is.
        line = re.sub(r"(?<!:)//.*$", "", line)
        out.append(line)
    return "\n".join(out)


def _extract_labeled_block(hcl: str, block_kw: str, label: str) -> str | None:
    """Return the brace-balanced body of the first ``<block_kw> "<label>" {``.

    e.g. ``listener "tcp"``, ``storage "raft"``, ``seal "transit"``. Expects
    comment-stripped HCL. Returns ``None`` if the block is absent.
    """
    pattern = rf'{block_kw}\s+"{re.escape(label)}"\s*\{{'
    header = re.search(pattern, hcl)
    if header is None:
        return None
    start = header.end() - 1
    depth = 0
    for i in range(start, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : i + 1]
    return None


def _retention_to_seconds(value: str) -> float:
    """Parse an OpenBao/Go-style duration (e.g. ``24h``, ``30s``, ``1m``) to seconds.

    Supports h/m/s (and bare seconds). Sufficient for the retention values this
    config uses; raises on an unrecognised unit so a typo fails loudly.
    """
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hms]?)\s*", value)
    assert m, f"unrecognised duration literal: {value!r}"
    qty = float(m.group(1))
    unit = m.group(2) or "s"
    return qty * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]


# =========================================================================== #
# PRIMARY config.hcl assertions (Req 6.1, 5.2, 11, 10.1)
# =========================================================================== #
class TestPrimaryConfig:
    """The primary OpenBao config.hcl matches the task-5.4 contract."""

    def test_renders_nonempty(self, primary_hcl):
        assert primary_hcl.strip(), "primary config.hcl rendered empty"

    def test_tls_disable_false(self, primary_hcl):
        """listener "tcp" sets tls_disable = false (Req 6.1)."""
        hcl = _strip_hcl_comments(primary_hcl)
        listener = _extract_labeled_block(hcl, "listener", "tcp")
        assert listener is not None, 'primary must declare a listener "tcp" (Req 6.1)'
        assert re.search(r"^\s*tls_disable\s*=\s*false\b", listener, re.MULTILINE), (
            "primary listener must set tls_disable = false (Req 6.1); "
            f"listener block was:\n{listener}"
        )

    def test_no_plaintext_listener(self, primary_hcl):
        """No listener anywhere sets tls_disable = true (Req 6.1)."""
        hcl = _strip_hcl_comments(primary_hcl)
        assert not re.search(r"tls_disable\s*=\s*true\b", hcl), (
            "primary config must NOT declare any plaintext listener "
            "(tls_disable = true) — Req 6.1"
        )
        # And every declared listener carries TLS material.
        listener = _extract_labeled_block(hcl, "listener", "tcp")
        assert listener and re.search(r"^\s*tls_cert_file\s*=", listener, re.MULTILINE), (
            "primary listener must reference a TLS cert file (Req 6.1)"
        )
        assert re.search(r"^\s*tls_key_file\s*=", listener, re.MULTILINE), (
            "primary listener must reference a TLS key file (Req 6.1)"
        )

    def test_seal_transit_address_is_unsealer(self, primary_hcl):
        """seal "transit" address is exactly the unsealer at 10.0.20.11 (Req 5.2)."""
        hcl = _strip_hcl_comments(primary_hcl)
        seal = _extract_labeled_block(hcl, "seal", "transit")
        assert seal is not None, 'primary must declare a seal "transit" stanza (Req 5.2)'
        addr = re.search(r'^\s*address\s*=\s*"([^"]+)"', seal, re.MULTILINE)
        assert addr is not None, 'seal "transit" must set an address (Req 5.2)'
        assert addr.group(1) == _UNSEALER_ADDR, (
            f'seal "transit" address must be exactly {_UNSEALER_ADDR} (Req 5.2); '
            f"got {addr.group(1)!r}"
        )
        # The 10.0.20.11 host must be present in that address (defence-in-depth).
        assert "10.0.20.11" in addr.group(1), (
            "seal address must point at the unsealer host 10.0.20.11 (Req 5.2)"
        )

    def test_seal_tls_skip_verify_gated_on_mock_cert(self, role_vars):
        """seal "transit" gets tls_skip_verify=true ONLY under the mock cert.

        The primary's seal CLIENT connects to the unsealer's API over TLS. While
        the unsealer presents the self-signed MOCK cert (openbao_tls_self_signed),
        the seal client must skip verification or the primary crash-loops with
        `x509: certificate signed by unknown authority`. With a real EJBCA cert
        (flag false, the production default) tls_skip_verify MUST be absent so
        verification is enforced. Locks in the 21st correction / mock-stand-ins.
        """
        base = {**role_vars, "openbao_role": "primary",
                "openbao_transit_token": _FAKE_TRANSIT_TOKEN}
        on = _strip_hcl_comments(
            _render_template(_PRIMARY_TMPL, {**base, "openbao_tls_self_signed": True}))
        off = _strip_hcl_comments(
            _render_template(_PRIMARY_TMPL, {**base, "openbao_tls_self_signed": False}))
        seal_on = _extract_labeled_block(on, "seal", "transit")
        seal_off = _extract_labeled_block(off, "seal", "transit")
        assert seal_on is not None and seal_off is not None
        assert re.search(r'tls_skip_verify\s*=\s*"?true"?', seal_on), (
            "seal stanza must set tls_skip_verify=true when the mock cert is used "
            "(openbao_tls_self_signed=true) — else the primary crash-loops on the "
            "unsealer's self-signed cert"
        )
        assert not re.search(r"tls_skip_verify", seal_off), (
            "seal stanza must NOT set tls_skip_verify when a real EJBCA cert is "
            "used (openbao_tls_self_signed=false, production default) — TLS "
            "verification must be enforced"
        )

    def test_declarative_audit_file_stanza(self, primary_hcl):
        """Req 7.1 — primary config.hcl declares the file audit device (OpenBao 2.4).

        OpenBao 2.4 removed API-driven audit enablement (CVE-2025-54997 hardening),
        so the file audit device MUST be declared in config.hcl and created at
        startup. Assert the audit "file" stanza renders with the audit log
        file_path, and that fail-closed is not relaxed (no log_raw). 31st correction.
        """
        hcl = _strip_hcl_comments(primary_hcl)
        # OpenBao's declarative audit stanza is HCL TWO-label: `audit "<type>"
        # "<path>" { ... }` (type then path). A single-label / nested-block form
        # makes OpenBao report `audit type must be specified` (32nd correction),
        # so assert the two-label header explicitly (not via the single-label
        # _extract_labeled_block helper).
        m = re.search(r'audit\s+"file"\s+"file"\s*\{.*?\n\}', hcl, re.DOTALL)
        assert m is not None, (
            'primary config.hcl must declare a TWO-label audit "file" "<path>" '
            "stanza — OpenBao 2.4 cannot enable audit devices via the API (Req 7.1)"
        )
        audit = m.group(0)
        assert "file_path" in audit and "/openbao/audit/" in audit, (
            "the audit stanza must set file_path under the mounted audit volume"
        )
        assert "log_raw" not in audit, (
            "fail-closed must not be relaxed — log_raw must be absent (Req 7.3)"
        )

    def test_storage_raft_path(self, primary_hcl):
        """storage "raft" path is /openbao/data (Req 11)."""
        hcl = _strip_hcl_comments(primary_hcl)
        storage = _extract_labeled_block(hcl, "storage", "raft")
        assert storage is not None, 'primary must declare storage "raft" (Req 11)'
        path = re.search(r'^\s*path\s*=\s*"([^"]+)"', storage, re.MULTILINE)
        assert path is not None, 'storage "raft" must set a path (Req 11)'
        assert path.group(1) == _STORAGE_PATH, (
            f'storage "raft" path must be {_STORAGE_PATH} (Req 11); '
            f"got {path.group(1)!r}"
        )
        # node_id identifies the primary node.
        node = re.search(r'^\s*node_id\s*=\s*"([^"]+)"', storage, re.MULTILINE)
        assert node and node.group(1) == "svc07-20-01", (
            "primary storage node_id must be svc07-20-01 (design.md config shape); "
            f"got {node.group(1) if node else None!r}"
        )

    def test_telemetry_retention_at_least_one_minute(self, primary_hcl):
        """top-level telemetry prometheus_retention_time parses to >= 60s (Req 10.1)."""
        hcl = _strip_hcl_comments(primary_hcl)
        # There are TWO telemetry blocks now: the listener "tcp" telemetry SUB-block
        # (metrics access) and the TOP-LEVEL telemetry stanza (retention). Extract
        # every `telemetry { ... }` and pick the one carrying prometheus_retention_time.
        telemetry = None
        for m in re.finditer(r"telemetry\s*\{", hcl):
            start = m.end() - 1
            depth = 0
            for i in range(start, len(hcl)):
                if hcl[i] == "{":
                    depth += 1
                elif hcl[i] == "}":
                    depth -= 1
                    if depth == 0:
                        block = hcl[start : i + 1]
                        if "prometheus_retention_time" in block:
                            telemetry = block
                        break
            if telemetry is not None:
                break
        assert telemetry is not None, (
            "primary must declare a top-level telemetry stanza with "
            "prometheus_retention_time (Req 10.1)"
        )
        retention = re.search(
            r'^\s*prometheus_retention_time\s*=\s*"([^"]+)"', telemetry, re.MULTILINE
        )
        assert retention is not None, (
            "telemetry must set prometheus_retention_time (Req 10.1)"
        )
        seconds = _retention_to_seconds(retention.group(1))
        assert seconds >= 60.0, (
            f"prometheus_retention_time must be >= 1 minute (Req 10.1); "
            f"got {retention.group(1)!r} = {seconds}s"
        )

    def test_unauth_metrics_access_in_listener_not_toplevel_telemetry(self, primary_hcl):
        """unauthenticated_metrics_access is a LISTENER telemetry param (Req 10.2).

        OpenBao accepts unauthenticated_metrics_access ONLY inside the
        `listener "tcp"` `telemetry {}` sub-block, not the top-level telemetry
        stanza — placing it top-level makes OpenBao log `unknown or unsupported
        field` and silently ignore it, leaving /v1/sys/metrics authenticated
        (fix-openbao-metrics-listener-telemetry). Assert correct placement.
        """
        hcl = _strip_hcl_comments(primary_hcl)
        listener = _extract_labeled_block(hcl, "listener", "tcp")
        assert listener is not None, 'primary must declare listener "tcp"'
        assert "unauthenticated_metrics_access" in listener, (
            "unauthenticated_metrics_access must live in the listener \"tcp\" "
            "telemetry sub-block (Req 10.2)"
        )
        # and it must be inside a telemetry {} sub-block of the listener
        assert re.search(r"telemetry\s*\{[^}]*unauthenticated_metrics_access", listener, re.DOTALL), (
            "unauthenticated_metrics_access must be inside the listener's "
            "telemetry {} sub-block"
        )
        # regression guard: the TOP-LEVEL telemetry stanza must NOT carry it
        # (that is the misplacement OpenBao rejects). Find the top-level telemetry
        # block (the one with prometheus_retention_time) and assert absence.
        for m in re.finditer(r"telemetry\s*\{", hcl):
            start = m.end() - 1
            depth = 0
            for i in range(start, len(hcl)):
                if hcl[i] == "{":
                    depth += 1
                elif hcl[i] == "}":
                    depth -= 1
                    if depth == 0:
                        block = hcl[start : i + 1]
                        if "prometheus_retention_time" in block:
                            assert "unauthenticated_metrics_access" not in block, (
                                "top-level telemetry must NOT carry "
                                "unauthenticated_metrics_access (OpenBao rejects it "
                                "there as an unknown field)"
                            )
                        break

    def test_no_seal_token_literal_in_defaults(self, role_vars):
        """Defence: the bootstrap transit token is never a committed LITERAL value.

        Req 5.4 / Security AC 7: the seal token is never committed. The token MAY
        be wired in defaults as a runtime ``env`` LOOKUP (so the plain
        openbao_install pass can render config.hcl on the primary without an
        undefined-var crash — ADR-driven fix), but it must NEVER be a committed
        literal secret. This test therefore asserts the precise invariant: IF
        ``openbao_transit_token`` is present, it must be an ``ansible.builtin.env``
        lookup keyed on ``openbao_bootstrap_token_env_var`` with an EMPTY default
        (resolves to '' when the env var is unset) — not a hardcoded value. (The
        test's own render injects an obviously-fake placeholder that lives only in
        this test file.)
        """
        token = role_vars.get("openbao_transit_token")
        if token is None:
            # Absent is also acceptable (older shape) — nothing committed.
            return
        assert not isinstance(token, str) or "lookup(" in token, (
            "openbao_transit_token, if defaulted, must be an env LOOKUP, never a "
            "committed literal secret value — Req 5.4, Security AC 7. Got a "
            "non-lookup value."
        )
        # It must be an env lookup on the never-committed bootstrap env var, with
        # an empty default so an unset env yields '' (fail-closed downstream),
        # never a real token literal.
        assert "ansible.builtin.env" in token or "env'" in token or 'env"' in token, (
            "openbao_transit_token must resolve via an `env` lookup (never a "
            "literal) — Req 5.4, Security AC 7"
        )
        assert "openbao_bootstrap_token_env_var" in token, (
            "the token env lookup must key on openbao_bootstrap_token_env_var "
            "(OPENBAO_BOOTSTRAP_TRANSIT_TOKEN), the shared never-committed source"
        )
        assert "default=''" in token or 'default=""' in token, (
            "the token env lookup must default to EMPTY (unset env -> '' -> "
            "fail-closed), never a fallback secret — Req 5.4, Security AC 7"
        )


# =========================================================================== #
# UNSEALER config.hcl assertions (Req 5.1)
# =========================================================================== #
class TestUnsealerConfig:
    """The unsealer config.hcl is Transit-only: no seal stanza, no app engine."""

    def test_renders_nonempty(self, unsealer_hcl):
        assert unsealer_hcl.strip(), "unsealer config.hcl rendered empty"

    def test_no_seal_stanza(self, unsealer_hcl):
        """The unsealer has NO seal stanza — it is the seal PROVIDER (Req 5.1)."""
        hcl = _strip_hcl_comments(unsealer_hcl)
        assert _extract_labeled_block(hcl, "seal", "transit") is None, (
            'unsealer config must NOT declare a seal "transit" stanza (Req 5.1)'
        )
        # No seal block of ANY seal type either.
        assert not re.search(r'^\s*seal\s+"', hcl, re.MULTILINE), (
            "unsealer config must NOT declare any seal stanza (Req 5.1)"
        )

    @pytest.mark.parametrize("engine", _FORBIDDEN_UNSEALER_ENGINES)
    def test_no_app_secrets_engine_mount(self, unsealer_hcl, engine):
        """No KV / database / aws / PKI mount is declared in the config (Req 5.1).

        Secrets engines in OpenBao are enabled at runtime (``bao secrets
        enable``), not in ``config.hcl`` — so the strongest offline assertion is
        that the unsealer's config declares NO ``secrets`` mount block and none
        of the forbidden engine keywords appear as a config directive. We assert
        both: no ``secrets``/``mount`` block, and no bare engine keyword outside
        comments.
        """
        hcl = _strip_hcl_comments(unsealer_hcl)
        # No `secrets "<engine>" { ... }` or `mount "<engine>" { ... }` block.
        assert _extract_labeled_block(hcl, "secrets", engine) is None, (
            f'unsealer config must NOT declare a secrets "{engine}" mount (Req 5.1)'
        )
        assert _extract_labeled_block(hcl, "mount", engine) is None, (
            f'unsealer config must NOT declare a mount "{engine}" block (Req 5.1)'
        )
        # The engine keyword must not appear at all in the (comment-stripped) config.
        assert not re.search(rf"\b{re.escape(engine)}\b", hcl, re.IGNORECASE), (
            f"unsealer config must not reference the {engine!r} secrets engine "
            f"(Req 5.1); found it in the rendered config"
        )

    def test_no_transit_mount_in_config(self, unsealer_hcl):
        """Even the Transit engine is enabled at runtime, not in config (Req 5.1).

        The unsealer IS the Transit provider, but its Transit engine is mounted
        by the init role at runtime — the templated config.hcl declares no
        secrets mount at all (it is a bare TLS-listener + Raft-storage config).
        """
        hcl = _strip_hcl_comments(unsealer_hcl)
        assert not re.search(r'^\s*secrets\s+"', hcl, re.MULTILINE), (
            "unsealer config.hcl must declare no secrets mount at all — the "
            "Transit engine is enabled at runtime by the init role (Req 5.1)"
        )
        assert _extract_labeled_block(hcl, "secrets", "transit") is None
        assert _extract_labeled_block(hcl, "mount", "transit") is None

    def test_still_tls_only_no_plaintext(self, unsealer_hcl):
        """Belt-and-braces: the unsealer is also TLS-only, no plaintext listener."""
        hcl = _strip_hcl_comments(unsealer_hcl)
        listener = _extract_labeled_block(hcl, "listener", "tcp")
        assert listener is not None, 'unsealer must declare a listener "tcp"'
        assert re.search(r"^\s*tls_disable\s*=\s*false\b", listener, re.MULTILINE), (
            "unsealer listener must set tls_disable = false"
        )
        assert not re.search(r"tls_disable\s*=\s*true\b", hcl), (
            "unsealer config must NOT declare a plaintext listener"
        )

    def test_has_raft_storage_for_own_state(self, unsealer_hcl):
        """The unsealer keeps minimal Raft storage for its own state (design shape)."""
        hcl = _strip_hcl_comments(unsealer_hcl)
        storage = _extract_labeled_block(hcl, "storage", "raft")
        assert storage is not None, 'unsealer must declare storage "raft" for its own state'
        node = re.search(r'^\s*node_id\s*=\s*"([^"]+)"', storage, re.MULTILINE)
        assert node and node.group(1) == "svc07-unsealer-20-01", (
            "unsealer storage node_id must be svc07-unsealer-20-01; "
            f"got {node.group(1) if node else None!r}"
        )


# =========================================================================== #
# ADR-0006 swap-hardening regression guard (spec: fix-openbao-swap-hardening,
# Task 1 / Design C4). Fix-Checking property assertions over the ANSIBLE layer:
#
#   (a) the openbao_init_unseal role contains NO vm.swappiness sysctl task
#       (host-global tunable written from inside an unprivileged guest — the
#       defect; RED until Task 2 removes it);
#   (b) disable_mlock = true is present in BOTH rendered OpenBao config shapes
#       (primary + unsealer; RED until Task 3 adds it — currently unset in
#       neither template so mlock defaults ON);
#   (c) the unsealer Docker service carries NO cap_add: ["IPC_LOCK"] (the
#       mlock-enabling capability for a feature OpenBao disables — RED until
#       Task 3 removes it);
#   (d) the per-container swap cap (#2 in ADR-0006) is RETAINED on the unsealer
#       service: mem_swappiness: 0 AND --memory-swappiness=0 (GREEN today — the
#       cap already exists; a preservation guard). The Terraform `swap = 0`
#       half of (d) lives in test_svc07_terraform_shape.py alongside the other
#       SVC-07 Terraform-shape assertions.
#
# These extend the existing SVC-07 offline config-render module (no bespoke new
# framework) and directly realise the design's Fix-Checking property assertions.
# They are RED-first by construction: (a)(b)(c) fail against the current unfixed
# code and turn GREEN as Tasks 2-3 apply the fix; (d) passes throughout.
# =========================================================================== #

import yaml as _yaml  # noqa: E402  (module-level import kept near the guard)

#: The openbao_init_unseal role whose tasks must contain NO vm.swappiness write.
_INIT_UNSEAL_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal" / "tasks" / "main.yml"
)
#: The openbao_install role tasks that declare the unsealer Docker service.
_INSTALL_TASKS = _ROLE_DIR / "tasks" / "main.yml"


def _iter_task_dicts(tasks_doc):
    """Yield every task mapping in a parsed Ansible tasks file, recursively.

    Handles the top-level task list plus any nested ``block:``/``rescue:``/
    ``always:`` task lists, so a sysctl write hidden inside a block is still
    found. Non-mapping / None entries are skipped.
    """
    if not isinstance(tasks_doc, list):
        return
    for task in tasks_doc:
        if not isinstance(task, dict):
            continue
        yield task
        for nested_key in ("block", "rescue", "always"):
            if nested_key in task:
                yield from _iter_task_dicts(task[nested_key])


class TestSwapHardeningRegressionGuard:
    """ADR-0006 Fix-Checking guard over the Ansible layer (Task 1 / C4)."""

    # ---- (a) no vm.swappiness sysctl task in openbao_init_unseal ----------- #
    def test_no_vm_swappiness_sysctl_task_in_init_unseal_role(self):
        """(a) The openbao_init_unseal role declares NO sysctl task targeting a
        host-global kernel tunable (vm.swappiness). Parsing the task list, any
        ``ansible.posix.sysctl`` / ``ansible.builtin.sysctl`` / bare ``sysctl``
        module invocation that sets ``name: vm.swappiness`` is the defect
        (ADR-0006 bug #1). RED until Task 2 deletes the swap-lockdown block."""
        assert _INIT_UNSEAL_TASKS.is_file(), (
            f"expected openbao_init_unseal tasks at {_INIT_UNSEAL_TASKS}"
        )
        doc = _yaml.safe_load(_INIT_UNSEAL_TASKS.read_text())
        offending = []
        for task in _iter_task_dicts(doc):
            for key, val in task.items():
                # Match the sysctl module by any of its module keys.
                if key in ("ansible.posix.sysctl", "ansible.builtin.sysctl", "sysctl"):
                    name = (val or {}).get("name") if isinstance(val, dict) else None
                    if name is not None and "swappiness" in str(name):
                        offending.append(task.get("name", "<unnamed task>"))
        assert not offending, (
            "openbao_init_unseal must contain NO vm.swappiness sysctl task "
            "(host-global kernel tunable written from inside an unprivileged "
            "guest — ADR-0006 bug #1). Found offending task(s): "
            f"{offending}. RED until Task 2 removes the swap-lockdown block."
        )

    def test_no_vm_swappiness_string_in_init_unseal_role(self):
        """(a, belt-and-braces) The literal ``vm.swappiness`` does not appear as
        a live (non-comment) directive anywhere in the role tasks. Complements
        the structured parse above so a reintroduction in any form is caught."""
        assert _INIT_UNSEAL_TASKS.is_file()
        # Strip YAML `#` comments so the file-header scope prose does not count.
        live_lines = []
        for line in _INIT_UNSEAL_TASKS.read_text().splitlines():
            live_lines.append(line.split("#", 1)[0])
        live = "\n".join(live_lines)
        assert "vm.swappiness" not in live, (
            "the literal `vm.swappiness` must not appear as a live directive in "
            "openbao_init_unseal/tasks/main.yml (ADR-0006 bug #1). RED until "
            "Task 2 removes the swap-lockdown block."
        )

    # ---- (b) disable_mlock = true in BOTH rendered config shapes ----------- #
    def test_disable_mlock_true_in_primary_config(self, primary_hcl):
        """(b) The rendered PRIMARY config.hcl sets ``disable_mlock = true``
        (ADR-0006: Raft-only OpenBao must not mlock; upstream removing mlock).
        RED until Task 3 adds the directive (currently set in neither template
        so mlock defaults ON)."""
        hcl = _strip_hcl_comments(primary_hcl)
        assert re.search(r"^\s*disable_mlock\s*=\s*true\b", hcl, re.MULTILINE), (
            "primary config.hcl must set `disable_mlock = true` (ADR-0006 — "
            "Raft-only OpenBao, mlock disabled). RED until Task 3 adds it."
        )

    def test_disable_mlock_true_in_unsealer_config(self, unsealer_hcl):
        """(b) The rendered UNSEALER config.hcl sets ``disable_mlock = true``
        (ADR-0006). RED until Task 3 adds the directive."""
        hcl = _strip_hcl_comments(unsealer_hcl)
        assert re.search(r"^\s*disable_mlock\s*=\s*true\b", hcl, re.MULTILINE), (
            "unsealer config.hcl must set `disable_mlock = true` (ADR-0006 — "
            "Raft-only OpenBao, mlock disabled). RED until Task 3 adds it."
        )

    # ---- (c) no cap_add: ["IPC_LOCK"] on the unsealer service -------------- #
    def test_no_ipc_lock_cap_on_unsealer_service(self):
        """(c) The unsealer Docker service in openbao_install/tasks/main.yml
        declares NO ``cap_add: ["IPC_LOCK"]`` — the mlock-enabling capability
        for a feature OpenBao disables (ADR-0006 bug #3). Scanned on the live
        (comment-stripped) task file so a commented mention does not count. RED
        until Task 3 removes the cap from the unsealer service entry."""
        assert _INSTALL_TASKS.is_file(), (
            f"expected openbao_install tasks at {_INSTALL_TASKS}"
        )
        live_lines = []
        for line in _INSTALL_TASKS.read_text().splitlines():
            live_lines.append(line.split("#", 1)[0])
        live = "\n".join(live_lines)
        # Any live cap_add entry granting IPC_LOCK is the defect. The unsealer
        # is the only service that carries it today; asserting it is absent from
        # the whole file is the strongest form of the guard (the primary must
        # not gain it either).
        assert not re.search(r"cap_add\s*:.*IPC_LOCK", live), (
            "the unsealer Docker service must NOT declare cap_add: [\"IPC_LOCK\"] "
            "(mlock-enabling capability for a disabled feature — ADR-0006 bug "
            "#3). RED until Task 3 removes it."
        )
        assert "IPC_LOCK" not in live, (
            "IPC_LOCK must not appear as a live directive anywhere in "
            "openbao_install/tasks/main.yml (ADR-0006 bug #3). RED until Task 3."
        )

    # ---- (d) per-container swap cap RETAINED on the unsealer service ------- #
    def test_per_container_swap_cap_retained_on_unsealer_service(self):
        """(d) PRESERVATION: the per-container swap cap (#2 in ADR-0006) stays
        on the unsealer service — BOTH ``mem_swappiness: 0`` and
        ``--memory-swappiness=0`` (runtime_options). This is a real per-guest
        cgroup limit that is explicitly KEPT; it passes today and must keep
        passing after the fix. (The Terraform ``swap = 0`` half of (d) is
        asserted in test_svc07_terraform_shape.py.)"""
        assert _INSTALL_TASKS.is_file()
        live_lines = []
        for line in _INSTALL_TASKS.read_text().splitlines():
            live_lines.append(line.split("#", 1)[0])
        live = "\n".join(live_lines)
        assert re.search(r"^\s*mem_swappiness\s*:\s*0\b", live, re.MULTILINE), (
            "the unsealer service must RETAIN `mem_swappiness: 0` — the "
            "per-container swap cap (#2 in ADR-0006) is kept as per-guest "
            "defence-in-depth (Req 3.3)."
        )
        assert "--memory-swappiness=0" in live, (
            "the unsealer service must RETAIN "
            "`runtime_options: [\"--memory-swappiness=0\"]` — the per-container "
            "swap cap (#2 in ADR-0006) is kept (Req 3.3)."
        )
