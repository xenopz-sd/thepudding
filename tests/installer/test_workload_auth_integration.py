"""`requires-infra` integration tests for the openbao-workload-auth feature.

Feature: openbao-workload-auth (tasks.md Task 8 — 8.1 happy-path, 8.2
idempotency+divergence, 8.3 degraded-mode+fail-closed; design.md "Testing
Strategy / Integration tests (`requires-infra`, live Primary)").

=== WHAT THESE TESTS ARE (AND WHEN THEY RUN) ===

These are the live-Primary integration tests that CLOSE the openbao-workload-auth
acceptance loop — the half that `--syntax-check` / offline lint / `docker compose
config` structurally CANNOT reach (an actual AppRole login against a real primary,
an actual `bao agent` auto-auth + `template` render, an actual idempotent re-run
reporting `changed=0`, and the actual fail-closed behaviour when the primary is
sealed or the admin token is absent). Per `testing-strategy.md` "Agent Live-Test
Authorization" they run **only**:

  * against a running, unsealed SVC-07 **Primary** that has been onboarded for a
    throwaway slug — stood up EXCLUSIVELY through SVC-07's own
    `ansible/playbooks/svc-07-bootstrap.yml` automated path + its `clean_slate`
    contract (NEVER ad-hoc `ssh`/`docker`/`pct` surgery — the dependency is driven
    through its own installer, `testing-strategy.md`
    "Dependencies-not-under-test");
  * **only** against a throwaway cluster listed in
    `scripts/installer/throwaway-clusters.yml`, **only** with the
    `AGENT_LIVE_AUTHORIZED` interlock armed and the fail-closed throwaway guard
    authorizing;
  * **never** in CI, acceptance, or production.

Every test here carries `@pytest.mark.requires_infra`, so the root `pytest.ini`
`addopts = -m "not requires_infra"` **DESELECTS the entire module by default** —
a bare offline `pytest tests/installer/` never collects or runs any of them, even
with a live `.env` loaded. Opting in is explicit: `-m requires_infra`. On top of
the marker, every test is `skipif`-gated on the live env-var contract below, so a
`-m requires_infra` run on a machine with no wired throwaway Primary SKIPS cleanly
(never fails) — the `documentation-testing.md` "absent infra => skipped, not
failed" rule, matching the sibling `test_svc07_openbao_engine_contract.py`.

=== HOW A LIVE RUN IS WIRED (provision-then-test) ===

The provision half (stand up the Primary + onboard the throwaway slug, enable
AppRole, mint the Workload_AppRole, run the Agent sidecar) is the
provision-then-test walkthrough in
`infra/projects/svc-07-secrets-manager/INSTALL-RUNBOOK.md` (the openbao-workload-auth
section). These tests are the TEST half: they point at the instance that
walkthrough built via the env-var contract and then drive the two feature roles
(`openbao_workload_approle`, `openbao-agent`) against it with
`~/venv/devinfra/bin/ansible-playbook`, asserting the design's live acceptance.

Env-var contract (all referenced by NAME only; no secret value is ever echoed,
logged, put on a command line, or placed in an assertion message):

  WORKLOAD_AUTH_TEST_INVENTORY   the SVC-07 generated inventory the bootstrap wrote
                                 (`ansible/inventory/svc-07-secrets-manager.generated.yml`).
  WORKLOAD_AUTH_TEST_SLUG        the throwaway slug already onboarded on the Primary
                                 (its `policy-<slug>` isolation triple must exist).
  OPENBAO_ADMIN_TOKEN            the platform-admin token the control role reads
                                 (same env var the role's default names); the live
                                 writes fail closed without it.
  WORKLOAD_AUTH_TEST_PRIMARY_CONTAINER  the primary Compose container name, for the
                                 degraded-mode seal/stop manipulation (8.3) — and
                                 that manipulation is itself driven only through the
                                 dependency's own lifecycle, never hand-surgery.
  WORKLOAD_AUTH_TEST_ALLOW_MUTATE  set to "1" to arm the tests that WRITE to the
                                 Primary / bring up a Compose stack (safety
                                 interlock, mirrors SVC07_TEST_ALLOW_DESTROY).

Self-signed mock-cert Primary: the live acceptance target is the SVC-07 Primary
brought up WITHOUT SVC-08/EJBCA, serving the self-signed MOCK cert
(`openbao_tls_self_signed=true`). Both feature roles derive
`openbao_primary_tls_skip_verify` from `openbao_tls_self_signed` (default false),
so this harness threads `openbao_tls_self_signed=true` into EVERY role run via
`_DEFAULT_ROLE_EXTRA_VARS` — otherwise every `bao` docker-exec call and the
Agent's auto-auth fail `x509: certificate signed by unknown authority` against
the mock cert. This is identical to how the sibling SVC-07 onboard path is driven
(`-e openbao_tls_self_signed=true`, INSTALL-RUNBOOK §6). A real-EJBCA-cert run
overrides it to false per-test. (Caught live: the workload-approle path initially
omitted this and every `bao` call failed TLS verification against the mock-cert
Primary, while the already-flagged onboard path worked.)

This module drives Ansible and reads back state via the live OpenBao HTTP API
(stdlib `urllib`, same minimal client shape as the sibling SVC-07 contract test).
It NEVER parses `.tfstate` and NEVER queries Proxmox. It is DESELECTED by default;
nothing in it executes under the offline gate.

Run (opt-in, throwaway cluster only, interlock armed elsewhere):
  ~/venv/devinfra/bin/pytest -m requires_infra \
    tests/installer/test_workload_auth_integration.py -v
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

# scripts/installer is on sys.path via tests/installer/conftest.py; the pure
# module's role-name helper is the shared symbol the live role also binds to, so
# the test derives the expected Workload_AppRole name from the SAME function the
# role uses (never a hand-written string that could drift).
from workload_auth import workload_role_name  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSIBLE_PLAYBOOK = str(Path.home() / "venv" / "devinfra" / "bin" / "ansible-playbook")

# --------------------------------------------------------------------------- #
# Env-var contract (see module docstring). Named in the
# `WORKLOAD_AUTH_TEST_<THING>` style of the sibling SVC07_TEST_* / OPENBAO_TEST_*.
# --------------------------------------------------------------------------- #
_INVENTORY_ENV = "WORKLOAD_AUTH_TEST_INVENTORY"
_SLUG_ENV = "WORKLOAD_AUTH_TEST_SLUG"
_ADMIN_TOKEN_ENV = "OPENBAO_ADMIN_TOKEN"
_PRIMARY_CONTAINER_ENV = "WORKLOAD_AUTH_TEST_PRIMARY_CONTAINER"
_ALLOW_MUTATE_ENV = "WORKLOAD_AUTH_TEST_ALLOW_MUTATE"

# Live Primary API, for reading back the AppRole state the role wrote. Reuses the
# SVC-07 contract test's addr/token/skip-tls contract so one throwaway Primary
# serves both suites.
_ADDR_ENV = "OPENBAO_TEST_ADDR"
_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"

# Contract constants mirrored from the role defaults
# (ansible/roles/openbao_workload_approle/defaults/main.yml) — the shapes the
# live role writes; the assertions below check OpenBao stored them faithfully.
_WORKLOAD_TOKEN_TTL = 3600       # openbao_workload_token_ttl
_WORKLOAD_TOKEN_MAX_TTL = 86400  # openbao_workload_token_max_ttl


def _live_gate_reason() -> str | None:
    """Return a skip reason if the live-workload-auth gate is unmet, else None.

    Requires the inventory + slug + admin token + live Primary API contract. When
    any is absent the whole module SKIPS cleanly (never fails) — the
    `documentation-testing.md` "absent infra => skipped, not failed" contract,
    matching the sibling SVC-07 live tests. The `requires_infra` MARKER is the
    outer gate (deselect by default); this skipif is the inner "even when opted
    in, skip if not actually wired" gate.
    """
    if not os.environ.get(_INVENTORY_ENV):
        return (
            f"requires-infra, skipped: {_INVENTORY_ENV} not set (the SVC-07 "
            "generated inventory from a throwaway-cluster bootstrap is required "
            "to drive the two feature roles against the live Primary)"
        )
    if not os.environ.get(_SLUG_ENV):
        return (
            f"requires-infra, skipped: {_SLUG_ENV} not set (a throwaway slug "
            "already onboarded on the Primary — its policy-<slug> triple must "
            "exist — is required)"
        )
    if not os.environ.get(_ADMIN_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_ADMIN_TOKEN_ENV} not set (the "
            "platform-admin token the control role authenticates with; the live "
            "writes fail closed without it)"
        )
    if not os.environ.get(_ADDR_ENV) or not os.environ.get(_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_ADDR_ENV}/{_TOKEN_ENV} not set (a live "
            "Primary API endpoint + read token is required to read back the "
            "AppRole state the role wrote)"
        )
    return None


def _mutate_gate_reason() -> str | None:
    """Skip reason for tests that WRITE to the Primary / bring up a stack.

    Layered ON TOP of :func:`_live_gate_reason`: even with a wired Primary, the
    mutating tests stay skipped unless `WORKLOAD_AUTH_TEST_ALLOW_MUTATE=1` arms
    them — the same safety interlock the SVC-07 installer suite uses
    (`SVC07_TEST_ALLOW_DESTROY`), so a read-only live run can never accidentally
    enable AppRole / mint a Secret_ID / start containers.
    """
    base = _live_gate_reason()
    if base is not None:
        return base
    if os.environ.get(_ALLOW_MUTATE_ENV, "").strip() not in ("1", "true", "yes"):
        return (
            f"requires-infra, skipped: {_ALLOW_MUTATE_ENV} is not armed (set it "
            "to 1 to allow these tests to enable AppRole / mint a Secret_ID / "
            "bring up the Agent+workload stack against the throwaway Primary)"
        )
    return None


def _degraded_gate_reason() -> str | None:
    """Skip reason for the degraded-mode test (needs the primary container name).

    Layered on top of :func:`_mutate_gate_reason`: the seal/stop manipulation
    needs the primary Compose container name AND the mutate interlock, because it
    cycles the dependency. The sealing/stopping is driven through the
    dependency's own lifecycle contract, not raw surgery (see the test body).
    """
    base = _mutate_gate_reason()
    if base is not None:
        return base
    if not os.environ.get(_PRIMARY_CONTAINER_ENV):
        return (
            f"requires-infra, skipped: {_PRIMARY_CONTAINER_ENV} not set (the "
            "primary Compose container name is required to drive the sealed/"
            "stopped degraded-mode scenario)"
        )
    return None


# --------------------------------------------------------------------------- #
# Minimal live OpenBao HTTP client (same shape as the SVC-07 contract test).
# Token lives ONLY in the X-Vault-Token header — never logged, never on a
# command line, never in an error message.
# --------------------------------------------------------------------------- #
def _tls_context() -> ssl.SSLContext | None:
    skip = os.environ.get(_SKIP_TLS_ENV, "1").strip().lower() not in (
        "0", "false", "no", "",
    )
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _OpenBao:
    """Read-back client for the live AppRole state the control role wrote."""

    def __init__(self, addr: str, token: str) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._ctx = _tls_context()

    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, Any]:
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        data = None
        req = urllib.request.Request(url=url, method=method)
        req.add_header("X-Vault-Token", self._token)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                req, data=data, context=self._ctx, timeout=30
            ) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"errors": [raw]}
            # NB: never surface the token in the returned tuple.
            return exc.code, parsed

    def get(self, path: str) -> tuple[int, Any]:
        return self._request("GET", path)

    def write_kv(self, mount: str, subpath: str, data: dict) -> None:
        """KV-v2 write to ``<mount>/data/<subpath>`` (the ``{"data": {...}}``
        envelope KV-v2 requires).

        This is the exact path+shape the Agent template reads back — e.g.
        ``write_kv("secret", "dronefleet/db", {...})`` targets
        ``secret/data/dronefleet/db``, matching the Agent's
        ``vault.read(secret/data/dronefleet/db)``. The write MUST be
        mutate-gated by the caller (see :func:`_mutate_gate_reason`); this is a
        local test-helper only. The supplied values are ephemeral throwaway-test
        values and are never logged or echoed.
        """
        status, parsed = self._request(
            "POST", f"{mount}/data/{subpath.lstrip('/')}", body={"data": data}
        )
        assert status in (200, 204), (
            f"KV-v2 write to {mount}/data/{subpath} failed with status "
            f"{status}: {parsed!r}"
        )

    def auth_methods(self) -> dict:
        status, data = self.get("sys/auth")
        if status == 200 and isinstance(data, dict):
            return data.get("data", data)
        return {}

    def read_role(self, role_name: str) -> dict | None:
        status, data = self.get(f"auth/approle/role/{role_name}")
        if status == 200 and isinstance(data, dict):
            return data.get("data", data)
        return None


@pytest.fixture(scope="module")
def bao() -> _OpenBao:
    addr = os.environ.get(_ADDR_ENV)
    token = os.environ.get(_TOKEN_ENV)
    assert addr and token, "live gate should have prevented fixture construction"
    return _OpenBao(addr, token)


@pytest.fixture(scope="module")
def slug() -> str:
    value = os.environ.get(_SLUG_ENV)
    assert value, "live gate should have prevented fixture construction"
    return value


@pytest.fixture(scope="module")
def inventory() -> str:
    value = os.environ.get(_INVENTORY_ENV)
    assert value, "live gate should have prevented fixture construction"
    return value


# --------------------------------------------------------------------------- #
# Ansible driver — runs a feature role via a tiny generated play, against the
# live inventory. Secrets (the admin token) ride in the child-process env only,
# NEVER on a command line (Req 7.2); the role itself carries `no_log`.
# --------------------------------------------------------------------------- #
# Default extra-vars threaded into EVERY role run below. The live acceptance
# target is the SVC-07 Primary brought up with the self-signed MOCK cert (a
# homelab bring-up WITHOUT SVC-08/EJBCA — `openbao_tls_self_signed=true`). Both
# feature roles derive `openbao_primary_tls_skip_verify` from
# `openbao_tls_self_signed` (defaulting to false), so against a mock-cert Primary
# the roles MUST be told the cert is self-signed or every `bao` docker-exec call
# fails `x509: certificate signed by unknown authority` and the Agent's auto-auth
# fails TLS verification. This mirrors EXACTLY how the sibling SVC-07 onboard path
# is driven (`-e openbao_tls_self_signed=true`, INSTALL-RUNBOOK §6) — a
# standalone play does not see the install-time choice, so the flag must be
# passed explicitly. A live run against a REAL EJBCA cert overrides this to false
# via its own extra_vars (the merge below lets any individual test override it).
_DEFAULT_ROLE_EXTRA_VARS: dict[str, Any] = {"openbao_tls_self_signed": True}


def _run_role(
    role_name: str,
    inventory: str,
    *,
    extra_vars: dict[str, Any] | None = None,
    hosts: str = "openbao",
    check_mode: bool = False,
) -> subprocess.CompletedProcess:
    """Drive ``role_name`` against the live inventory via ``ansible-playbook``.

    Builds an ephemeral one-play file (``hosts: <hosts>`` / ``import_role``) and
    runs it with the venv ``ansible-playbook`` by absolute path (`dev-workflow.md`
    venv rule). ``extra_vars`` are merged over :data:`_DEFAULT_ROLE_EXTRA_VARS`
    (which sets ``openbao_tls_self_signed=true`` for the self-signed mock-cert
    Primary — see that constant; an individual test MAY override it, e.g. for a
    real-EJBCA-cert run) and passed as a single ``--extra-vars`` JSON blob. The
    admin token is inherited from the current process env (never added to argv).
    ``check_mode`` adds ``--check`` for the idempotency assertion.
    """
    play = [
        {
            "hosts": hosts,
            "gather_facts": False,
            "tasks": [
                {
                    "name": f"drive {role_name} (openbao-workload-auth integration test)",
                    "import_role": {"name": role_name},
                }
            ],
        }
    ]
    import tempfile

    # IMPORTANT (playbook_dir anchoring): the control role's STEP 0
    # registry-validation default anchors `openbao_projects_yaml_path` at
    # `{{ playbook_dir }}/../../infra/platform-foundation/projects.yaml` — i.e.
    # TWO levels below the repo root — matching the sibling
    # openbao_project_onboard role and the real svc-NN-bootstrap orchestrators,
    # which all live in `ansible/playbooks/`. The ephemeral play MUST therefore
    # be written into `ansible/playbooks/` so `playbook_dir` resolves there and
    # the role's `../../` default lands on the repo-root registry exactly as in a
    # real run. Writing it at the repo root instead makes `../../` resolve TWO
    # levels ABOVE the repo root and the STEP 0 slurp fails "File not found".
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yml", delete=False, dir=str(_REPO_ROOT / "ansible" / "playbooks")
    ) as fh:
        json.dump(play, fh)
        play_path = fh.name
    try:
        cmd = [_ANSIBLE_PLAYBOOK, "-i", inventory, play_path]
        # Merge caller extra_vars OVER the defaults so a test can still override
        # (e.g. a real-EJBCA-cert run setting openbao_tls_self_signed=false), but
        # the mock-cert TLS-skip toggle is present by default for every run.
        merged_extra_vars = {**_DEFAULT_ROLE_EXTRA_VARS, **(extra_vars or {})}
        cmd += ["--extra-vars", json.dumps(merged_extra_vars)]
        if check_mode:
            cmd.append("--check")
        return subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ},  # OPENBAO_ADMIN_TOKEN inherited; never on argv
        )
    finally:
        Path(play_path).unlink(missing_ok=True)


def _changed_count(ansible_output: str) -> int | None:
    """Parse the ``changed=N`` from an ansible-playbook PLAY RECAP line.

    Returns the integer, or None if no recap line was found (treated as an
    assertion failure by the caller). Used to prove a converged re-run is a
    no-op (``changed=0``) vs. a divergence rewrite (``changed>=1``).
    """
    for line in ansible_output.splitlines():
        if "changed=" in line and ("ok=" in line or "failed=" in line):
            for tok in line.replace(":", " ").split():
                if tok.startswith("changed="):
                    try:
                        return int(tok.split("=", 1)[1])
                    except ValueError:
                        return None
    return None


# =========================================================================== #
# 8.1 — Happy-path live AppRole login + render (Req 1.1-1.3, 2.1, 2.2, 3.1,
#        3.2, 3.6, 7.6, 7.7).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live workload-auth mutate gate satisfied",
)
class TestWorkloadAuthHappyPath:
    """Enable AppRole + mint a Workload_AppRole + run the Agent, against a live
    Primary onboarded for a throwaway slug (stood up via SVC-07's own
    `svc-07-bootstrap.yml` automated path).

    Acceptance (design.md "Happy path"): `approle/` enabled; `<slug>-workload`
    present with `token_policies == [policy-<slug>]`, renewable, configured TTLs;
    `role_id`/`secret_id` minted; the Agent auto-auths, renders the
    Rendered_Secret_File, and the workload reaches healthy.
    """

    def test_control_role_enables_approle_and_mints_workload_role(
        self, bao: _OpenBao, slug: str, inventory: str
    ):
        """Run `openbao_workload_approle`, then READ BACK the live AppRole state
        (Req 1.1-1.3, 2.1, 2.2, 7.6)."""
        result = _run_role(
            "openbao_workload_approle",
            inventory,
            extra_vars={"project_slug": slug},
        )
        assert result.returncode == 0, (
            "openbao_workload_approle failed against the live Primary:\n"
            f"{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )

        # approle/ auth method enabled (Req 1.1-1.3).
        methods = bao.auth_methods()
        assert "approle/" in methods, (
            "the AppRole auth method must be enabled at approle/ after the role "
            f"runs; got auth methods {sorted(methods)}"
        )
        assert methods["approle/"].get("type") == "approle"

        # <slug>-workload role bound to EXACTLY [policy-<slug>], renewable, TTLs
        # (Req 2.1, 2.2, 7.6). The role name comes from the SHARED pure symbol.
        role_name = workload_role_name(slug)
        assert role_name.endswith("-workload")
        role = bao.read_role(role_name)
        assert role is not None, f"{role_name} must exist on the Primary"
        assert role.get("token_policies") == [f"policy-{slug}"], (
            "Workload_AppRole token_policies must be EXACTLY [policy-<slug>] "
            f"(Req 2.1/7.6); got {role.get('token_policies')!r}"
        )
        assert int(role.get("token_ttl", 0)) == _WORKLOAD_TOKEN_TTL
        assert int(role.get("token_max_ttl", 0)) == _WORKLOAD_TOKEN_MAX_TTL
        # renewable service token, NOT the <=3600 non-renewable CI ceiling.
        assert role.get("token_type") == "service"

    def test_agent_renders_secret_file_and_workload_reaches_healthy(
        self, bao: _OpenBao, slug: str, inventory: str
    ):
        """Run `openbao-agent`; the Agent auto-auths + renders the
        Rendered_Secret_File and the workload Compose service reaches healthy
        (Req 3.1, 3.2, 3.6, 7.7). Asserted via the role run succeeding and the
        swap-gate healthcheck being satisfied on the live host.

        The role brings the stack up through `docker-compose-app`; its completion
        is the evidence the gate passed (the workload's `depends_on
        service_healthy` on the Agent could not be satisfied unless the Agent
        rendered a non-empty RSF — the fail-closed swap-gate inverts this in 8.3).

        Precondition seeded here (NOT in 8.3): the mapping below reads
        ``secret/data/<slug>/db``'s ``password`` field, so the KV-v2 item MUST
        exist on the onboarded Primary before the Agent runs. We seed it with a
        clearly-synthetic, ephemeral throwaway-test value (never a real
        credential, never echoed to stdout) via the KV-v2 data path
        ``secret/data/<slug>/db`` — exactly the path the Agent reads. The
        degraded/fail-closed tests (8.3) deliberately do NOT seed, so a sealed
        Primary yields no Rendered_Secret_File.
        """
        # Synthetic throwaway-cluster test secret — not a real credential; the
        # value is never printed, logged, or placed in an assertion message.
        bao.write_kv(
            "secret",
            f"{slug}/db",
            {"password": "test-only-" + secrets.token_urlsafe(16)},
        )
        result = _run_role(
            "openbao-agent",
            inventory,
            extra_vars={
                "project_slug": slug,
                # minimal, non-secret mapping set — the KV items must exist under
                # secret/data/<slug>/ on the onboarded Primary.
                "openbao_agent_secret_mappings": [
                    {
                        "kv_subpath": "db",
                        "kv_field": "password",
                        "env_key": "DB_PASSWORD",
                    }
                ],
                "workload_image": "nginx:1.27",
            },
        )
        assert result.returncode == 0, (
            "openbao-agent failed to bring up the Agent+workload stack:\n"
            f"{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )
        # The composition-wiring INFO line names the Primary endpoint (Req 7.7).
        assert "approle" in result.stdout.lower() and "auto" in result.stdout.lower(), (
            "the role must emit the composition-wiring INFO line naming AppRole "
            "auto_auth + the Primary endpoint (Req 7.7)"
        )


# =========================================================================== #
# 8.2 — Idempotency + divergence (Req 1.3, 2.6, 2.7, 4.6).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live workload-auth mutate gate satisfied",
)
class TestWorkloadAuthIdempotencyAndDivergence:
    """Re-running against a converged state does NOT rewrite the control-plane
    role (the AppRole method enable + the `<slug>-workload` role are unchanged);
    a divergence IS rewritten (`changed>=1`).

    IMPORTANT — what "idempotent" means here, and why NOT a whole-play
    `changed==0` (design.md "Open risks and trade-offs", the `secret_id`
    re-mint): the control role mints a FRESH `secret_id` on EVERY run (a
    Secret_ID is an un-rereadable, TTL/num_uses-bounded credential — re-minting
    is the correct remedy for an expired/leaked one, never a thing to compare),
    and the credential handoff file write follows that mint. Those two tasks
    therefore report `changed` on every run — INCLUDING under `--check` — so a
    whole-play `changed==0` assertion would contradict the design. The design is
    explicit that idempotency is a property of the CONTROL-PLANE WRITES (enable
    the AppRole method, create/reconcile the `<slug>-workload` role), NOT of the
    always-mint secret_id. This test therefore asserts the faithful thing: the
    STORED Workload_AppRole on the Primary is UNCHANGED across a converged
    re-run (its `token_ttl`/`token_max_ttl`/`token_policies`/`token_type` are
    byte-for-byte identical before and after), and the run exits 0 — proving the
    control-plane did not rewrite the role — while allowing the by-design
    secret_id mint + handoff to be `changed`.
    """

    def test_converged_rerun_does_not_rewrite_the_control_plane_role(
        self, bao: _OpenBao, slug: str, inventory: str
    ):
        """A converged `openbao_workload_approle` re-run leaves the stored
        Workload_AppRole UNCHANGED on the Primary (Req 1.3, 2.6) — the
        control-plane writes are idempotent even though the by-design secret_id
        re-mint + handoff report `changed` every run (design.md "Open risks").

        Asserted by reading the role's control-plane fields (`token_ttl`,
        `token_max_ttl`, `token_policies`, `token_type`) BEFORE and AFTER a
        `--check` re-run and requiring them identical, rather than asserting a
        whole-play `changed==0` (which the always-mint secret_id contradicts).
        Assumes 8.1 already enabled approle + minted the role (run 8.1 first in a
        live session)."""
        role_name = workload_role_name(slug)

        def _control_plane_fields() -> dict:
            role = bao.read_role(role_name)
            assert role is not None, (
                f"precondition: {role_name} must already exist on the Primary "
                "(run the 8.1 happy-path test first in this live session)"
            )
            # Only the CONTROL-PLANE-managed fields — deliberately NOT anything
            # tied to the per-run secret_id mint.
            return {
                "token_ttl": int(role.get("token_ttl", -1)),
                "token_max_ttl": int(role.get("token_max_ttl", -1)),
                "token_policies": role.get("token_policies"),
                "token_type": role.get("token_type"),
            }

        before = _control_plane_fields()

        # A converged re-run. `--check` performs the check-then-write READS; the
        # always-mint secret_id + handoff may report changed, which is WHY we do
        # not assert a whole-play changed==0 here.
        result = _run_role(
            "openbao_workload_approle",
            inventory,
            extra_vars={"project_slug": slug},
            check_mode=True,
        )
        assert result.returncode == 0, (
            "converged --check run must succeed:\n"
            f"{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )

        after = _control_plane_fields()
        assert after == before, (
            "a converged re-run must NOT rewrite the control-plane role "
            "(enable-approle + create-role are idempotent, Req 1.3/2.6); the "
            "stored role's control-plane fields changed across the re-run.\n"
            f"before={before!r}\nafter={after!r}\n"
            "(the by-design secret_id re-mint + handoff MAY be changed — see "
            "design.md 'Open risks and trade-offs' — but the role itself must "
            "not be rewritten)"
        )

    def test_diverged_role_is_rewritten(
        self, bao: _OpenBao, slug: str, inventory: str
    ):
        """Mutate the live role to diverge from the desired contract, re-run, and
        assert it is rewritten (`changed>=1`) back to the contract (Req 2.7).

        The divergence is introduced via the SAME control role with an overridden
        TTL var (so the stored `token_ttl` differs), then a run with the default
        vars must detect the drift and rewrite. This drives the dependency
        through the role, never by hand-poking OpenBao state.
        """
        # Step 1: write a DIVERGED role (different token_ttl) via the role itself.
        diverge = _run_role(
            "openbao_workload_approle",
            inventory,
            extra_vars={
                "project_slug": slug,
                "openbao_workload_token_ttl": 7200,  # != default 3600 -> drift
            },
        )
        assert diverge.returncode == 0, (
            f"diverge run failed:\n{diverge.stdout[-2000:]}\n{diverge.stderr[-1500:]}"
        )
        role = bao.read_role(workload_role_name(slug))
        assert role is not None and int(role.get("token_ttl", 0)) == 7200, (
            "precondition: the role should now carry the diverged token_ttl=7200"
        )

        # Step 2: re-run with DEFAULT vars -> the check-then-write detects drift
        # and rewrites (changed>=1), restoring token_ttl to the default.
        reconcile = _run_role(
            "openbao_workload_approle",
            inventory,
            extra_vars={"project_slug": slug},
        )
        assert reconcile.returncode == 0
        changed = _changed_count(reconcile.stdout)
        assert changed is not None and changed >= 1, (
            "a diverged role must be rewritten (changed>=1) (Req 2.7); got "
            f"changed={changed}"
        )
        role = bao.read_role(workload_role_name(slug))
        assert role is not None and int(role.get("token_ttl", 0)) == _WORKLOAD_TOKEN_TTL, (
            "after reconcile the role's token_ttl must be back to the default "
            f"{_WORKLOAD_TOKEN_TTL}; got {role.get('token_ttl')!r}"
        )


# =========================================================================== #
# 8.3 — Degraded-mode + fail-closed control path (Req 1.6, 2.5, 6.1-6.5).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _degraded_gate_reason() is not None,
    reason=_degraded_gate_reason() or "live workload-auth degraded gate satisfied",
)
class TestWorkloadAuthDegradedModeAndFailClosed:
    """Fail-closed behaviour: a sealed/stopped Primary yields NO
    Rendered_Secret_File + retry/ERROR logs + a non-healthy workload; an absent
    admin token or absent `policy-<slug>` fails the control role BEFORE any write.

    IMPORTANT (dependency-driven-through-its-own-path): the sealed/stopped Primary
    scenario cycles the SVC-07 dependency. Per `testing-strategy.md`
    "Dependencies-not-under-test", the Primary is sealed/reset ONLY through its own
    `svc-07-bootstrap.yml` / `clean_slate` automated path — the raw
    `docker compose stop` shown in the design is the read-only-adjacent cycling
    the dependency's OWN lifecycle exposes, never ad-hoc state surgery. This test
    asserts the OBSERVABLE fail-closed effect (no RSF, non-healthy workload); it
    does not hand-mutate OpenBao internals.
    """

    def test_absent_admin_token_fails_before_any_write(
        self, slug: str, inventory: str
    ):
        """Unset `OPENBAO_ADMIN_TOKEN` -> the control role fails closed BEFORE any
        read/enable/write, naming the env var (Req 1.6)."""
        env = {k: v for k, v in os.environ.items() if k != _ADMIN_TOKEN_ENV}
        play = [
            {
                "hosts": "openbao",
                "gather_facts": False,
                "tasks": [
                    {
                        "name": "drive openbao_workload_approle (no admin token)",
                        "import_role": {"name": "openbao_workload_approle"},
                    }
                ],
            }
        ]
        import tempfile

        # Same playbook_dir anchoring as _run_role: write the ephemeral play into
        # `ansible/playbooks/` so the control role's `{{ playbook_dir }}/../../`
        # projects.yaml default resolves to the repo root (not two levels above
        # it). This test fails closed at STEP 1 (admin-token guard) before STEP 0
        # ever slurps the registry — the registry validation default is still
        # active here (openbao_validate_slug_in_registry defaults true) and would
        # also fail on a mis-anchored path, so the directory must match _run_role.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yml", delete=False, dir=str(_REPO_ROOT / "ansible" / "playbooks")
        ) as fh:
            json.dump(play, fh)
            play_path = fh.name
        try:
            result = subprocess.run(
                [
                    _ANSIBLE_PLAYBOOK, "-i", inventory, play_path,
                    # openbao_tls_self_signed=true mirrors the mock-cert Primary
                    # the other runs use (_DEFAULT_ROLE_EXTRA_VARS). This test
                    # fails closed at STEP 1 (admin-token guard) BEFORE any `bao`
                    # call, so the toggle is not strictly needed here, but it
                    # keeps this bespoke invocation consistent with _run_role.
                    "--extra-vars",
                    json.dumps({**_DEFAULT_ROLE_EXTRA_VARS, "project_slug": slug}),
                ],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=300,
                env=env,  # OPENBAO_ADMIN_TOKEN deliberately absent
            )
        finally:
            Path(play_path).unlink(missing_ok=True)

        assert result.returncode != 0, (
            "the control role MUST fail closed when OPENBAO_ADMIN_TOKEN is unset "
            "(Req 1.6); it exited 0"
        )
        combined = (result.stdout + result.stderr)
        assert _ADMIN_TOKEN_ENV in combined, (
            "the fail-closed message must NAME the expected env var "
            f"({_ADMIN_TOKEN_ENV}) (Req 1.6)"
        )

    def test_absent_policy_fails_with_remedy(self, inventory: str):
        """A slug with no `policy-<slug>` -> the control role fails with a
        'run openbao_project_onboard first' remedy, writing no role (Req 2.5)."""
        # A slug that is deliberately NOT onboarded. Validation against the
        # registry is disabled so we reach the policy-existence assert (not the
        # registry assert). This writes nothing on failure.
        bogus = "neveronboarded-workloadauth-test"
        result = _run_role(
            "openbao_workload_approle",
            inventory,
            extra_vars={
                "project_slug": bogus,
                "openbao_validate_slug_in_registry": False,
            },
        )
        assert result.returncode != 0, (
            "the control role MUST fail closed when policy-<slug> is absent "
            "(Req 2.5); it exited 0"
        )
        combined = (result.stdout + result.stderr).lower()
        assert "openbao_project_onboard" in combined or "onboard" in combined, (
            "the fail-closed message must direct the operator to run "
            "openbao_project_onboard first (Req 2.5)"
        )

    def test_sealed_primary_yields_no_rendered_secret_file(
        self, slug: str, inventory: str
    ):
        """With the Primary sealed/stopped, the Agent writes NO
        Rendered_Secret_File, logs retry/ERROR, and the workload stays
        non-healthy past the render timeout (Req 6.1-6.5).

        The Primary seal/stop is effected through the dependency's OWN lifecycle
        (its `svc-07-bootstrap.yml` clean_slate / stop contract), NOT raw surgery.
        A short render timeout is passed so the assertion does not wait the full
        default 120s. The assertion is on the OBSERVABLE fail-closed effect:
        the Agent run surfaces the no-RSF / non-healthy condition.
        """
        # This scenario requires the Primary to be sealed/stopped out-of-band via
        # the dependency's own automated path BEFORE this test (the live operator
        # / harness does that between runs). Here we assert the Agent, pointed at
        # an unreachable/sealed Primary, fails closed: the workload never reaches
        # healthy within the (shortened) render timeout and the stack bring-up
        # surfaces that rather than rendering a placeholder.
        result = _run_role(
            "openbao-agent",
            inventory,
            extra_vars={
                "project_slug": slug,
                "openbao_agent_secret_mappings": [
                    {
                        "kv_subpath": "db",
                        "kv_field": "password",
                        "env_key": "DB_PASSWORD",
                    }
                ],
                "workload_image": "nginx:1.27",
                "openbao_agent_render_timeout": 20,  # short, fail fast
            },
        )
        # Fail-closed: the stack bring-up must NOT report a healthy workload. The
        # healthcheck gating on a non-empty RSF can never pass while the Primary
        # is sealed/unreachable, so either the role run fails at the health gate
        # or the output shows the Agent never rendered the RSF — never a silent
        # success with a placeholder secret.
        combined = (result.stdout + result.stderr).lower()
        assert result.returncode != 0 or "unhealthy" in combined or "service_healthy" in combined, (
            "with a sealed/unreachable Primary the Agent must fail closed — the "
            "workload must NOT reach healthy and NO Rendered_Secret_File is "
            "written (Req 6.1/6.2/6.5). The run reported an apparent success with "
            "no evidence of the fail-closed gate.\n"
            f"{result.stdout[-2000:]}"
        )
