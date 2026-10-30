"""Composition-root / swap-gate contract test for openbao-workload-auth.

Feature: openbao-workload-auth (tasks.md Task 7.1; design.md "Compose wiring
(swap-gate / fail-closed)" + Testing Strategy "the composition-root / swap-gate
test").

This is the MANDATORY composition-root test (``composition-wiring.md``,
``testing-strategy.md`` §4) applied to the secrets path. It renders the Agent +
workload Compose stack from the SAME ``docker-compose-app`` template the
``openbao-agent`` role uses (so the test exercises the real rendering path, not a
hand-written fixture), and runs ``docker compose config`` to assert the
swap-gate:

* the workload's ``depends_on: { openbao-agent: { condition: service_healthy } }``
  gate on the Agent (Req 5.2 / 6.2);
* the Agent's Rendered_Secret_File-presence healthcheck
  (``test -s <RSF>``) (Req 6.5);
* **creds-present path**: a healthy Agent (RSF rendered) releases the workload to
  start;
* **creds-absent / Primary-unavailable path**: the Agent never becomes healthy
  (no RSF) ⇒ the workload never starts — fail-closed, never a canned/empty
  secret (``composition-wiring.md`` unavailability principle). This is asserted
  at the STRUCTURAL level (the gate is a ``service_healthy`` condition on an
  RSF-presence probe, so "no RSF ⇒ Agent unhealthy ⇒ workload held back" is a
  property of the rendered topology, provable offline);
* the Agent sidecar publishes NO host port (Req 4.3);
* the workload exposes HTTP ONLY via Traefik labels, with NO ``ports:`` entry
  (Req 5.4 / 5.5 / 7.5).

``docker compose config`` is run LIVE when the ``docker`` CLI is available
(``command -v docker`` + ``docker compose version``) — it validates + normalises
the rendered compose and is the offline (Tier-1) confirmation. When the CLI is
absent the SAME assertions are made against the rendered compose parsed directly
with PyYAML, and ``docker compose config`` is noted as the ``requires-infra``
(CLI-present) confirmation. Either way this is OFFLINE: no live OpenBao Primary,
no containers started.

No secret VALUE appears anywhere — the env files referenced are empty/non-secret
fixtures; the real secret only ever lives in the 0600 Rendered_Secret_File the
Agent writes at runtime.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

jinja2 = pytest.importorskip("jinja2")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DCA_TEMPLATE = (
    _REPO_ROOT
    / "ansible"
    / "roles"
    / "docker-compose-app"
    / "templates"
    / "compose.yaml.j2"
)


# --------------------------------------------------------------------------- #
# Render the swap-gate stack via the REAL docker-compose-app template.
# --------------------------------------------------------------------------- #
def _render_compose(services: list, networks: list) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_DCA_TEMPLATE.parent)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # to_json / dictsort shims matching Ansible's filters used by the template.
    env.filters["to_json"] = json.dumps
    env.filters["dictsort"] = lambda d: sorted(d.items())
    return env.get_template(_DCA_TEMPLATE.name).render(
        dca_app_name="demo-slug-workload",
        dca_services=services,
        dca_volumes=[],
        dca_networks=networks,
    )


def _swap_gate_services(rsf_path: str) -> list:
    """The two services the openbao-agent role builds (design 'Compose wiring').

    ``rsf_path`` is the Rendered_Secret_File path used in both the Agent's
    healthcheck probe and the workload's ``extra_env_files`` — the single seam
    the swap-gate turns on.
    """
    return [
        {
            "name": "openbao-agent",
            "image": "openbao/openbao:2.4",
            "command": ["agent", "-config=/openbao/agent/agent.hcl"],
            "restart": "unless-stopped",
            "volumes": [
                "/opt/compose/demo-slug-workload/agent.hcl:/openbao/agent/agent.hcl:ro",
                "/etc/openbao-workload/demo-slug/demo-slug-workload-role-id:/openbao/agent/role-id:ro",
                "/etc/openbao-workload/demo-slug/demo-slug-workload-secret-id:/openbao/agent/secret-id:ro",
                "/opt/compose/demo-slug-workload/rendered:/openbao/agent/rendered",
            ],
            "healthcheck": {
                "test": ["CMD", "test", "-s", rsf_path],
                "interval": "5s",
                "retries": 24,
            },
            # NO env_file, NO ports — the Agent speaks only to the Primary and
            # writes a local file.
            "env_file": False,
        },
        {
            "name": "workload",
            "image": "nginx:1.27",
            "restart": "unless-stopped",
            "extra_env_files": [rsf_path],
            "depends_on": {"openbao-agent": {"condition": "service_healthy"}},
            "labels": [
                "traefik.enable=true",
                "traefik.http.routers.demo-slug-workload.rule=Host(`demo-slug-workload.example.test`)",
            ],
            "networks": ["edge"],
            # NO ports — Traefik-only ingress.
        },
    ]


def _networks() -> list:
    return [{"name": "edge", "external": True}]


# --------------------------------------------------------------------------- #
# docker compose availability.
# --------------------------------------------------------------------------- #
def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=30,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _write_stack(tmp_path: Path, compose_text: str, rsf_path: str) -> Path:
    """Write compose.yaml + the env files it references so `docker compose
    config` can resolve them. The env files are EMPTY/non-secret fixtures — the
    real RSF with secrets is produced by the Agent at runtime, never here."""
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "compose.yaml").write_text(compose_text, encoding="utf-8")
    # the non-secret .env the workload layers under its secrets.
    (stack_dir / ".env").write_text("# non-secret workload config\n", encoding="utf-8")
    # the extra_env_files path (RSF) must exist for `config` to resolve it; in
    # the creds-present scenario we create an EMPTY placeholder (no secret).
    rsf = Path(rsf_path)
    rsf.parent.mkdir(parents=True, exist_ok=True)
    rsf.write_text("", encoding="utf-8")
    return stack_dir


def _run_compose_config(stack_dir: Path) -> dict:
    proc = subprocess.run(
        ["docker", "compose", "-f", str(stack_dir / "compose.yaml"), "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(stack_dir),
    )
    assert proc.returncode == 0, (
        "`docker compose config` rejected the rendered swap-gate stack:\n"
        f"STDERR:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- #
# The assertions — shared by the live-config path and the structural path.
# --------------------------------------------------------------------------- #
def _assert_swap_gate(resolved: dict, rsf_path: str):
    services = resolved["services"]
    assert "openbao-agent" in services and "workload" in services

    agent = services["openbao-agent"]
    workload = services["workload"]

    # 1. the workload is gated on the Agent being service_healthy (Req 5.2/6.2).
    depends = workload.get("depends_on") or {}
    # docker compose config normalises depends_on to a mapping.
    assert "openbao-agent" in depends, (
        "workload must depend_on the openbao-agent sidecar"
    )
    cond = depends["openbao-agent"]
    cond_value = cond.get("condition") if isinstance(cond, dict) else cond
    assert cond_value == "service_healthy", (
        f"workload must gate on condition: service_healthy, got {cond_value!r}"
    )

    # 2. the Agent healthcheck is the RSF-presence probe (Req 6.5).
    hc = agent.get("healthcheck") or {}
    test = hc.get("test")
    test_str = " ".join(test) if isinstance(test, list) else str(test)
    assert "test -s" in test_str and rsf_path in test_str, (
        f"the Agent healthcheck must probe `test -s {rsf_path}`, got {test!r}"
    )

    # 3. creds-present -> healthy Agent releases workload; creds-absent ->
    #    Agent never healthy -> workload never starts. This is a property of the
    #    topology: the gate is service_healthy on an RSF-presence probe, so the
    #    workload start is bound to the RSF existing. Assert the two halves are
    #    wired to the SAME RSF seam.
    # NOTE: `docker compose config` CONSUMES env_file entries — it reads the
    # referenced files and folds their keys into `environment`, dropping the
    # `env_file` list from the normalised output (empty fixture files => nothing
    # folded in). The raw-render (pre-config) output, by contrast, still lists
    # the env_file paths. So only assert the RSF-as-env_file seam when the
    # resolved output still carries env_file entries (the raw-render path);
    # under `docker compose config` the seam is proven by the gate + healthcheck
    # on the SAME RSF plus the dedicated raw-render test
    # (test_rendered_swap_gate_compose_is_well_formed), which inspects the
    # pre-config env_file list directly.
    workload_env_files = workload.get("env_file") or []
    if workload_env_files:
        env_paths = [
            (e.get("path") if isinstance(e, dict) else e) for e in workload_env_files
        ]
        assert any(rsf_path in str(p) for p in env_paths), (
            "the workload must layer the Rendered_Secret_File (the Agent's "
            f"output) as an env_file ({rsf_path}); found {env_paths!r}. The gate "
            "and the secret source must be the SAME seam (fail-closed)."
        )

    # 4. the Agent sidecar publishes NO host port (Req 4.3).
    assert not agent.get("ports"), (
        f"the Agent sidecar must publish no host port; got {agent.get('ports')!r}"
    )

    # 5. the workload exposes HTTP ONLY via Traefik labels, NO ports (Req 5.4/5.5/7.5).
    assert not workload.get("ports"), (
        f"the workload must map no host ports (Traefik-only ingress); got "
        f"{workload.get('ports')!r}"
    )
    labels = workload.get("labels") or {}
    # docker compose config normalises labels to a mapping.
    if isinstance(labels, dict):
        label_items = [f"{k}={v}" for k, v in labels.items()]
    else:
        label_items = [str(x) for x in labels]
    joined = " ".join(label_items)
    assert "traefik.enable=true" in joined, (
        "the workload must register with Traefik via a traefik.enable=true label"
    )
    assert "traefik.http.routers" in joined and ".rule=Host(" in joined, (
        "the workload must declare a Traefik Host() router rule label"
    )


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #
_RSF = "/openbao/agent/rendered/secrets.env"


def test_rendered_swap_gate_compose_is_well_formed():
    """The docker-compose-app template renders both swap-gate services with the
    gate, healthcheck, labels, and no ports — asserted on the raw render so this
    holds even where the docker CLI is absent."""
    compose_text = _render_compose(_swap_gate_services(_RSF), _networks())
    parsed = yaml.safe_load(compose_text)
    _assert_swap_gate(parsed, _RSF)


@pytest.mark.skipif(
    not _docker_compose_available(),
    reason=(
        "docker compose CLI not available — the swap-gate structure is asserted "
        "by test_rendered_swap_gate_compose_is_well_formed (PyYAML parse); "
        "`docker compose config` is the CLI-present confirmation."
    ),
)
def test_docker_compose_config_accepts_and_normalises_the_swap_gate(tmp_path):
    """Run `docker compose config` LIVE on the rendered swap-gate stack and
    assert the NORMALISED topology still carries the gate / healthcheck /
    no-ports / Traefik-labels contract (Req 4.3, 5.2, 5.4, 5.5, 6.2, 6.5, 7.5)."""
    # Use a tmp RSF path so the empty placeholder env file lands under tmp_path
    # (never writing to a real system path during the offline test).
    rsf_path = str(tmp_path / "rendered" / "secrets.env")
    services = _swap_gate_services(rsf_path)
    compose_text = _render_compose(services, _networks())
    stack_dir = _write_stack(tmp_path, compose_text, rsf_path)
    resolved = _run_compose_config(stack_dir)
    _assert_swap_gate(resolved, rsf_path)


def test_agent_sidecar_has_no_ports_in_raw_render():
    """Belt-and-braces (Req 4.3): the rendered compose text contains no `ports:`
    key at all — the template only emits `ports:` when a service explicitly sets
    one, and neither swap-gate service does."""
    compose_text = _render_compose(_swap_gate_services(_RSF), _networks())
    # no service declares ports: — the only 'ports' substring would be a key.
    assert "ports:" not in compose_text, (
        "the swap-gate stack must render no `ports:` entry for either service "
        "(Agent has no host port; workload is Traefik-only)."
    )


# --------------------------------------------------------------------------- #
# Defect F — the workload's Rendered_Secret_File env_file MUST render as the
# long-form `required: false` entry so Compose does NOT abort at its eager
# up-front env_file existence check (the RSF does not exist until the Agent
# renders it). The swap-gate (depends_on: service_healthy) still enforces
# fail-closed. This asserts the long-form `extra_env_files` contract the
# openbao-agent role now uses, AND that the plain-string contract still renders
# byte-identically (backward compatibility for postgres/zitadel/grafana).
# --------------------------------------------------------------------------- #
def _render_workload_env_file(extra_env_files) -> str:
    services = [
        {
            "name": "workload",
            "image": "nginx:1.27",
            "extra_env_files": extra_env_files,
        }
    ]
    return _render_compose(services, _networks())


def test_runtime_rendered_rsf_env_file_is_not_required():
    """The Rendered_Secret_File entry rendered via the mapping long-form
    ({path, required: false}) must emit `required: false`, so Compose's eager
    up-front env_file validation does not abort before the swap-gate runs
    (defect F)."""
    compose_text = _render_workload_env_file(
        [{"path": _RSF, "required": False}]
    )
    parsed = yaml.safe_load(compose_text)
    env_files = parsed["services"]["workload"]["env_file"]
    # The non-secret .env stays a bare string (unchanged); the RSF is the
    # long-form mapping with required: false.
    assert ".env" in env_files, "the non-secret .env short-form line must be preserved"
    rsf_entry = next(
        (e for e in env_files if isinstance(e, dict) and e.get("path") == _RSF),
        None,
    )
    assert rsf_entry is not None, (
        f"the RSF must render as a long-form {{path, required}} entry; got {env_files!r}"
    )
    assert rsf_entry["required"] is False, (
        "the runtime-rendered RSF must be `required: false` so Compose does not "
        "abort at the eager env_file existence check before the swap-gate "
        f"(depends_on: service_healthy) runs; got {rsf_entry!r}"
    )


def test_string_form_extra_env_file_still_renders_short_form():
    """Backward compatibility (shared-symbol contract): a plain-string
    extra_env_files entry — as postgres-server/zitadel-server/observability use —
    must render byte-identically to the pre-change short-form, NOT as a long-form
    mapping."""
    secret_path = "/etc/postgresql/postgres.secret.env"
    compose_text = _render_workload_env_file([secret_path])
    assert f'      - "{secret_path}"' in compose_text, (
        "a plain-string extra_env_files entry must still render as the "
        f"short-form `- \"{secret_path}\"`; got:\n{compose_text}"
    )
    # and it must NOT have been promoted to the long-form mapping.
    parsed = yaml.safe_load(compose_text)
    env_files = parsed["services"]["workload"]["env_file"]
    assert secret_path in env_files, (
        "the string-form secret env must remain a bare string env_file entry "
        f"(backward compat); got {env_files!r}"
    )


def test_mapping_without_required_defaults_to_required_true():
    """A mapping entry that omits `required` defaults to `required: true` — the
    safe default matching the historical implicit-required string behavior."""
    compose_text = _render_workload_env_file([{"path": "/some/secret.env"}])
    parsed = yaml.safe_load(compose_text)
    env_files = parsed["services"]["workload"]["env_file"]
    entry = next(
        (e for e in env_files if isinstance(e, dict) and e.get("path") == "/some/secret.env"),
        None,
    )
    assert entry is not None and entry["required"] is True, (
        f"a mapping without `required` must default to required: true; got {entry!r}"
    )


# --------------------------------------------------------------------------- #
# Defect G — the openbao-agent SIDECAR must stay root so it can read its OWN
# root-owned 0640 agent.hcl and 0600 role-id/secret-id mounts. The fix sets the
# image's documented BAO_SKIP_DROP_ROOT=1 knob via the (new, optional)
# per-service `environment:` map on the docker-compose-app schema. These tests
# pin the new schema key AND its backward compatibility (absent ⇒ no
# `environment:` block, byte-identical to before).
# --------------------------------------------------------------------------- #
def test_environment_map_renders_inline_env_var():
    """A service with an `environment:` map renders it inline — the defect-G fix
    sets BAO_SKIP_DROP_ROOT=1 so the openbao-agent sidecar keeps running as root
    and can read its root-owned mounted config + 0600 cred files."""
    services = [
        {
            "name": "openbao-agent",
            "image": "openbao/openbao:2.4",
            "command": ["agent", "-config=/openbao/agent/agent.hcl"],
            "environment": {"BAO_SKIP_DROP_ROOT": "1"},
            "env_file": False,
        }
    ]
    compose_text = _render_compose(services, _networks())
    parsed = yaml.safe_load(compose_text)
    env = parsed["services"]["openbao-agent"].get("environment")
    assert env is not None, (
        "the sidecar must render an `environment:` block carrying "
        f"BAO_SKIP_DROP_ROOT; got service: {parsed['services']['openbao-agent']!r}"
    )
    # docker-compose-app renders environment as a mapping.
    if isinstance(env, dict):
        assert env.get("BAO_SKIP_DROP_ROOT") == "1"
    else:  # a list of KEY=VALUE strings, if a future renderer changes form
        assert any("BAO_SKIP_DROP_ROOT=1" in str(e) for e in env)


def test_service_without_environment_renders_no_environment_block():
    """Backward compatibility (shared-symbol contract): a service that does NOT
    set `environment:` renders no `environment:` key — byte-identical to before
    the schema was extended. Every existing dca_services caller omits it."""
    services = [
        {
            "name": "workload",
            "image": "nginx:1.27",
            "env_file": False,
        }
    ]
    compose_text = _render_compose(services, _networks())
    assert "environment:" not in compose_text, (
        "a service with no `environment:` map must render no `environment:` "
        f"block (backward compat); got:\n{compose_text}"
    )
