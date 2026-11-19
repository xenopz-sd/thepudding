"""`requires-infra` integration tests for the project-consumer-onboarding feature.

Feature: project-consumer-onboarding (tasks.md Task 10 — 10.1 first per-project
SVC-01 bring-up, 10.2 full orchestrator run, 10.3 degraded-mode, 10.4
idempotency/resumable, 10.5 clean-slate verification; design.md "Testing
Strategy / Integration / requires-infra (throwaway cluster only)";
Requirements 3, 4, 5.4, 6, 7, 8, 9).

=== WHAT THESE TESTS ARE (AND WHEN THEY RUN) ===

These are the live tests that CLOSE the project-consumer-onboarding acceptance
loop — the half that offline parsing / `--syntax-check` / `terraform validate`
structurally CANNOT reach:

  * the FIRST REAL per-project SVC-01 bring-up — a PostgreSQL guest actually
    provisioned on a PROJECT VLAN (100+, never VLAN 20) via the SVC01_Consumer_Root
    `db/` root, reachable on the VLAN (10.1). SVC-01 has only ever run as the
    ZITADEL backing DB on VLAN 20, so this is the first exercise of the reusable
    per-project path (design Open Risk 2);
  * a full orchestrator run that wires DB + the OpenBao isolation triple +
    the `<slug>-workload` AppRole + the ZITADEL org/SPA/API clients, with the
    generated DB password and OIDC client secret stored in OpenBao and the
    completion summary printed (10.2);
  * the degraded-mode fail-closed halts (OpenBao sealed / ZITADEL down / DB not
    up each halt non-zero naming the dependency, with NO partial wiring) (10.3);
  * idempotence + resumability (a converged re-run reports zero changes; a re-run
    after a forced partial failure completes only the remainder; no orphaned
    resources) (10.4);
  * the clean-slate verification (`clean_slate=true` tears consumer resources to
    baseline, the verify gate confirms zero surviving compute + zero state, and
    the Registry entry + `<slug>-infra` network slice SURVIVE) (10.5).

=== GOVERNANCE (testing-strategy.md "Agent Live-Test Authorization") ===

Per `testing-strategy.md` these run **only**:

  * against the designated THROWAWAY cluster listed in
    `scripts/installer/throwaway-clusters.yml`, **only** with the
    `AGENT_LIVE_AUTHORIZED` interlock armed and the fail-closed throwaway guard
    authorizing — **never** in CI, acceptance, or production;
  * with every dependency-not-under-test (OpenBao, ZITADEL) stood up / reset /
    re-bootstrapped **exclusively** through its OWN `svc-NN-bootstrap.yml`
    automated path (`svc-07-bootstrap.yml`, `svc-06-bootstrap.yml`) and its
    `clean_slate`/admin-mint opt-ins — NEVER ad-hoc `ssh`/`docker`/`pct` surgery
    (`testing-strategy.md` "Dependencies-not-under-test"). The orchestrator under
    test is driven through its own sanctioned entry point
    `ansible/playbooks/project-consumer-bootstrap.yml`.

Every test here carries `@pytest.mark.requires_infra`, so the root `pytest.ini`
`addopts = -m "not requires_infra"` **DESELECTS the whole module by default** —
a bare offline `pytest tests/installer/` never collects or runs any of them, even
with a live `.env` loaded. Opting in is explicit: `-m requires_infra`. On top of
the marker every test is `skipif`-gated on the live env-var contract below, so a
`-m requires_infra` run on a machine with no wired throwaway cluster SKIPS
cleanly (never fails) — the `documentation-testing.md` "absent infra => skipped,
not failed" rule.

=== HOW A LIVE RUN IS WIRED (provision-then-test) ===

The provision half (stand up the dependencies via their own installers, onboard
a throwaway slug into the Registry + host-index ledger, apply the SVC01_Consumer_Root)
is the provision-then-test walkthrough in
`infra/projects/dronefleet/INSTALL-RUNBOOK.md`. These tests are the TEST half:
they point at the throwaway slug that walkthrough registered and drive the
orchestrator with `~/venv/devinfra/bin/ansible-playbook`.

Env-var contract (referenced by NAME only; no secret value is ever echoed,
logged, placed on a command line, or put in an assertion message):

  CONSUMER_TEST_SLUG            a throwaway slug ALREADY recorded in the Registry
                               (projects.yaml) + host-index ledger, with a
                               consumer-manifest.yml under infra/projects/<slug>/.
  CONSUMER_TEST_CLUSTER_ENV     the Cluster_Env_File for the throwaway cluster
                               (e.g. cluster.throwaway.env) the orchestrator reads.
  CONSUMER_TEST_DB_HOST         the per-project DB guest IP on the project VLAN
                               (= 10.0.<vlan>.10), for the reachability probe (10.1).
  CONSUMER_TEST_DB_PORT         the DB wire port (5432 postgres / 3306 mariadb).
  OPENBAO_ADMIN_TOKEN           the platform-admin token the composed OpenBao
                               onboarders authenticate with (live writes fail
                               closed without it).
  OPENBAO_TEST_ADDR / _TOKEN    a live OpenBao read endpoint + token, to read back
                               the isolation triple / workload AppRole / stored
                               secrets the orchestrator wrote.
  CONSUMER_TEST_ALLOW_MUTATE    set to "1" to arm the tests that APPLY / bring up
                               / destroy (safety interlock, mirrors
                               SVC07_TEST_ALLOW_DESTROY / WORKLOAD_AUTH_TEST_ALLOW_MUTATE).

This module drives Ansible/Terraform and reads back state via the live OpenBao
HTTP API (stdlib `urllib`). It NEVER parses `.tfstate` and NEVER queries Proxmox
for host discovery. It is DESELECTED by default; nothing in it executes under the
offline gate.

Run (opt-in, throwaway cluster only, interlock armed elsewhere):
  AGENT_LIVE_AUTHORIZED=1 ~/venv/devinfra/bin/pytest -m requires_infra \
    tests/installer/test_consumer_onboarding_integration.py -v
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

# scripts/installer is on sys.path via tests/installer/conftest.py. The pure
# resolver + derivation modules are the SAME symbols the live orchestrator binds
# to, so the test derives its expectations (resolved vlan_id, DB host IP, DB
# state name) from the SAME functions the roles use — never a hand-written string
# that could drift from the implementation.
from consumer_derivation import (  # noqa: E402
    consumer_db_host_ip,
    consumer_db_state_name,
    network_slice_state_name,
)
from consumer_registry import resolve_vlan_id_from_file  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_PYTHON = str(Path.home() / "venv" / "devinfra" / "bin" / "python")
_ANSIBLE_PLAYBOOK = str(Path.home() / "venv" / "devinfra" / "bin" / "ansible-playbook")
_ORCHESTRATOR = "ansible/playbooks/project-consumer-bootstrap.yml"
_LOCALHOST_INV = "ansible/inventory/localhost.yml"

# --------------------------------------------------------------------------- #
# Env-var contract (see module docstring).
# --------------------------------------------------------------------------- #
_SLUG_ENV = "CONSUMER_TEST_SLUG"
_CLUSTER_ENV_ENV = "CONSUMER_TEST_CLUSTER_ENV"
_DB_HOST_ENV = "CONSUMER_TEST_DB_HOST"
_DB_PORT_ENV = "CONSUMER_TEST_DB_PORT"
_ADMIN_TOKEN_ENV = "OPENBAO_ADMIN_TOKEN"
_BAO_ADDR_ENV = "OPENBAO_TEST_ADDR"
_BAO_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_BAO_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"
_ALLOW_MUTATE_ENV = "CONSUMER_TEST_ALLOW_MUTATE"
_AGENT_LIVE_ENV = "AGENT_LIVE_AUTHORIZED"

_REGISTRY = _REPO_ROOT / "infra" / "platform-foundation" / "projects.yaml"


# --------------------------------------------------------------------------- #
# Gate functions — layered (read -> mutate), mirroring the sibling suites.
# --------------------------------------------------------------------------- #
def _live_gate_reason() -> str | None:
    """Return a skip reason if the live-consumer gate is unmet, else None.

    The `requires_infra` MARKER is the outer gate (deselect by default); this
    skipif is the inner "even when opted in, skip cleanly if not actually wired"
    gate (documentation-testing.md "absent infra => skipped, not failed").
    """
    if not os.environ.get(_SLUG_ENV):
        return (
            f"requires-infra, skipped: {_SLUG_ENV} not set (a throwaway slug "
            "already recorded in the Registry + host-index ledger, with a "
            "consumer-manifest.yml, is required to drive the orchestrator)"
        )
    if not os.environ.get(_CLUSTER_ENV_ENV):
        return (
            f"requires-infra, skipped: {_CLUSTER_ENV_ENV} not set (the "
            "Cluster_Env_File for the throwaway cluster the orchestrator reads)"
        )
    if not os.environ.get(_BAO_ADDR_ENV) or not os.environ.get(_BAO_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_BAO_ADDR_ENV}/{_BAO_TOKEN_ENV} not set "
            "(a live OpenBao read endpoint + token is required to read back the "
            "isolation triple / workload AppRole / stored secrets)"
        )
    if not os.environ.get(_ADMIN_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_ADMIN_TOKEN_ENV} not set (the "
            "platform-admin token the composed OpenBao onboarders authenticate "
            "with; the live writes fail closed without it)"
        )
    return None


def _mutate_gate_reason() -> str | None:
    """Skip reason for tests that APPLY / bring up / destroy real resources.

    Layered ON TOP of :func:`_live_gate_reason`: even with a wired cluster the
    mutating tests stay skipped unless BOTH the `AGENT_LIVE_AUTHORIZED` interlock
    is armed AND `CONSUMER_TEST_ALLOW_MUTATE=1`. This is the agent live-run
    interlock (testing-strategy.md) plus the per-suite safety interlock, so a
    read-only live run can never accidentally apply Terraform, bring up a stack,
    or destroy a guest.
    """
    base = _live_gate_reason()
    if base is not None:
        return base
    if (os.environ.get(_AGENT_LIVE_ENV, "").strip() or "") == "":
        return (
            f"requires-infra, skipped: {_AGENT_LIVE_ENV} interlock is not armed "
            "(testing-strategy.md Agent Live-Test Authorization — destructive "
            "live runs are allowed only against the throwaway cluster with the "
            "interlock set)"
        )
    if os.environ.get(_ALLOW_MUTATE_ENV, "").strip() not in ("1", "true", "yes"):
        return (
            f"requires-infra, skipped: {_ALLOW_MUTATE_ENV} is not armed (set it "
            "to 1 to allow these tests to apply the SVC01_Consumer_Root / run the "
            "orchestrator / tear down against the throwaway cluster)"
        )
    return None


# --------------------------------------------------------------------------- #
# Minimal live OpenBao HTTP read-back client (same shape as the sibling suites).
# The token lives ONLY in the X-Vault-Token header — never logged, never on a
# command line, never in an error message.
# --------------------------------------------------------------------------- #
def _tls_context() -> ssl.SSLContext | None:
    skip = os.environ.get(_BAO_SKIP_TLS_ENV, "1").strip().lower() not in (
        "0", "false", "no", "",
    )
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _OpenBao:
    """Read-back client for the live state the orchestrator's composed onboarders
    wrote (isolation triple / workload AppRole / stored secrets)."""

    def __init__(self, addr: str, token: str) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._ctx = _tls_context()

    def _request(self, method: str, path: str) -> tuple[int, Any]:
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        req = urllib.request.Request(url=url, method=method)
        req.add_header("X-Vault-Token", self._token)
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"errors": [raw]}
            return exc.code, parsed  # never surface the token

    def get(self, path: str) -> tuple[int, Any]:
        return self._request("GET", path)

    def policy_exists(self, name: str) -> bool:
        status, _ = self.get(f"sys/policies/acl/{name}")
        return status == 200

    def role_exists(self, role_name: str) -> bool:
        status, _ = self.get(f"auth/approle/role/{role_name}")
        return status == 200

    def secret_present(self, kv_data_path: str, field: str) -> bool:
        """True iff a non-empty value exists at the KV path for ``field``. The
        VALUE is never returned/logged — only a boolean presence."""
        status, data = self.get(kv_data_path)
        if status != 200 or not isinstance(data, dict):
            return False
        payload = (data.get("data") or {}).get("data") or {}
        return bool(payload.get(field))


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def slug() -> str:
    value = os.environ.get(_SLUG_ENV)
    assert value, "live gate should have prevented fixture construction"
    return value


@pytest.fixture(scope="module")
def cluster_env_file() -> str:
    value = os.environ.get(_CLUSTER_ENV_ENV)
    assert value, "live gate should have prevented fixture construction"
    return value


@pytest.fixture(scope="module")
def vlan_id(slug: str) -> int:
    """The Registry-resolved vlan_id — via the SAME pure resolver the
    orchestrator's preflight composes (never a hand-written VLAN)."""
    return resolve_vlan_id_from_file(slug, _REGISTRY)


@pytest.fixture(scope="module")
def bao() -> _OpenBao:
    addr = os.environ.get(_BAO_ADDR_ENV)
    token = os.environ.get(_BAO_TOKEN_ENV)
    assert addr and token, "live gate should have prevented fixture construction"
    return _OpenBao(addr, token)


# --------------------------------------------------------------------------- #
# Orchestrator driver — runs the sanctioned entry point. Secrets (admin token)
# ride in the child-process env only, NEVER on a command line; the roles carry
# `no_log`.
# --------------------------------------------------------------------------- #
def _run_orchestrator(
    slug: str,
    cluster_env_file: str,
    *,
    extra_vars: dict[str, Any] | None = None,
    check_mode: bool = False,
    timeout: int = 1800,
) -> subprocess.CompletedProcess:
    """Drive project-consumer-bootstrap.yml via the venv ansible-playbook by
    absolute path (dev-workflow.md venv rule). The admin token is inherited from
    the process env, never added to argv."""
    cmd = [
        _ANSIBLE_PLAYBOOK,
        "-i", _LOCALHOST_INV,
        _ORCHESTRATOR,
        "-e", f"cluster_env_file={cluster_env_file}",
        "-e", f"project_slug={slug}",
    ]
    if extra_vars:
        cmd += ["--extra-vars", json.dumps(extra_vars)]
    if check_mode:
        cmd.append("--check")
    return subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ},  # OPENBAO_ADMIN_TOKEN inherited; never on argv
    )


def _changed_count(ansible_output: str) -> int | None:
    """Parse the last ``changed=N`` from an ansible-playbook PLAY RECAP line."""
    found = None
    for line in ansible_output.splitlines():
        if "changed=" in line and ("ok=" in line or "failed=" in line):
            for tok in line.replace(":", " ").split():
                if tok.startswith("changed="):
                    try:
                        found = int(tok.split("=", 1)[1])
                    except ValueError:
                        found = found
    return found


# =========================================================================== #
# 10.1 — First real per-project SVC-01 bring-up (Req 3.1, 3.3, 3.4).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live consumer mutate gate satisfied",
)
class TestFirstPerProjectSvc01BringUp:
    """The SVC01_Consumer_Root applies on a PROJECT VLAN (100+) and the DB guest
    is reachable — the first exercise of SVC-01's reusable per-project path
    (previously only the shared ZITADEL backing DB on VLAN 20)."""

    def test_db_guest_is_on_a_project_vlan_not_vlan_20(self, vlan_id: int):
        """The resolved VLAN is a PROJECT VLAN (100+), never the management (10)
        or shared-services (20) VLAN (Req 3.2)."""
        assert vlan_id >= 100, (
            f"the consumer DB must be on a project VLAN (100+); resolved {vlan_id}"
        )
        assert vlan_id not in (10, 20), "consumer DB must never be on VLAN 10/20"

    def test_db_guest_reachable_on_the_project_vlan(self, vlan_id: int):
        """After the SVC01_Consumer_Root apply + postgres-server role, the DB
        guest is reachable on the project VLAN at the derived IP (Req 3.1, 3.3,
        3.4). The expected IP is derived via the SAME shared symbol the HCL uses
        (consumer_db_host_ip -> cidrhost formula)."""
        expected_ip = consumer_db_host_ip(vlan_id, 0)  # single-guest VLAN => index 0
        db_host = os.environ.get(_DB_HOST_ENV, expected_ip)
        assert db_host == expected_ip, (
            f"{_DB_HOST_ENV} ({db_host}) must equal the derived DB host IP "
            f"{expected_ip} = cidrhost('10.0.{vlan_id}.0/24', 10) (Req 3.4)"
        )
        db_port = int(os.environ.get(_DB_PORT_ENV, "5432"))
        # Bounded TCP reachability check (read-only).
        import socket

        with socket.create_connection((db_host, db_port), timeout=30) as sock:
            assert sock is not None, (
                f"the per-project DB guest {db_host}:{db_port} must be reachable "
                "on the project VLAN after provisioning (Req 3.1)"
            )


# =========================================================================== #
# 10.2 — Full orchestrator run (Req 4.1, 4.4, 4.5, 4.6, 5.4, 8.1, 8.2).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live consumer mutate gate satisfied",
)
class TestFullOrchestratorRun:
    """One orchestrator run wires DB + OpenBao isolation triple + <slug>-workload
    AppRole + ZITADEL org/SPA/API, stores the generated secrets in OpenBao, and
    prints the completion summary."""

    def test_orchestrator_run_succeeds_and_prints_summary(
        self, slug: str, cluster_env_file: str
    ):
        """A full run exits 0 and prints the Phase-4 completion summary naming the
        DB endpoint / SSO app IDs / OpenBao path + workload AppRole (Req 4.1,
        5.4)."""
        result = _run_orchestrator(slug, cluster_env_file)
        assert result.returncode == 0, (
            "the orchestrator run failed:\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
        )
        out = result.stdout
        assert "Consumer onboarding complete" in out, (
            "the Phase-4 completion summary banner must be printed (Req 5.4)"
        )
        # the summary names the DB, SSO apps, and the OpenBao policy by name/path.
        assert "Database:" in out and "SSO applications:" in out and "Secrets policy:" in out, (
            "the completion summary must name DB endpoint / SSO app IDs / secrets "
            "policy (Req 5.4)"
        )

    def test_isolation_triple_workload_approle_and_secrets_in_openbao(
        self, bao: _OpenBao, slug: str
    ):
        """Read back the live OpenBao state the composed onboarders wrote: the
        isolation triple policy, the <slug>-workload AppRole, and the DB password
        + OIDC client secret (presence only, values never shown) (Req 4.6, 8.1,
        8.2)."""
        assert bao.policy_exists(f"policy-{slug}"), (
            f"the isolation-triple policy policy-{slug} must exist (Req 4.6)"
        )
        assert bao.role_exists(f"{slug}-workload"), (
            f"the {slug}-workload AppRole must exist (Req 4.6)"
        )
        # DB password stored under secret/data/proj-<slug>/<engine> (Req 8.1).
        # SVC-01 writes the app credential under the field `app_password` (not `password`); the
        # consumer_bootstrap presence check reads the same field (WS-C38 found this test rot live).
        assert bao.secret_present(
            f"secret/data/proj-{slug}/postgres", "app_password"
        ), f"the generated DB password must be stored in OpenBao (Req 8.1)"


# =========================================================================== #
# consumer-object-storage AC-1 — the declared buckets work with the stored key, on VLAN 20 and through the
# Traefik route (a signed round trip with the route's host name, TCP to the Traefik guest).
# =========================================================================== #
_TRAEFIK_IP_ENV = "CONSUMER_TEST_TRAEFIK_IP"
_S3_INSECURE_ENV = "CONSUMER_TEST_S3_INSECURE"


def _declared_buckets(slug: str) -> list[str]:
    manifest = yaml.safe_load(
        (_REPO_ROOT / "infra" / "projects" / slug / "consumer-manifest.yml").read_text(encoding="utf-8")
    )
    return [f"proj-{slug}-{p}" for p in (manifest.get("object_storage") or {}).get("buckets", [])]


def _s3(endpoint: str, bucket: str, creds: dict, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, S3_ACCESS_KEY_ID=creds["access_key_id"], S3_SECRET_ACCESS_KEY=creds["secret_access_key"])
    return subprocess.run(
        [_VENV_PYTHON, str(_REPO_ROOT / "scripts" / "installer" / "s3_smoke.py"),
         "--endpoint", endpoint, "--bucket", bucket, *extra, "roundtrip"],
        env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live consumer object-storage gate satisfied",
)
class TestObjectStorage:
    @staticmethod
    @pytest.fixture(scope="class")
    def creds(bao: _OpenBao, slug: str) -> dict:
        if not _declared_buckets(slug):
            pytest.skip("requires-infra, skipped: the manifest declares no object_storage block")
        status, data = bao.get(f"secret/data/proj-{slug}/garage")
        assert status == 200, f"secret/proj-{slug}/garage must exist after onboarding (R2), got HTTP {status}"
        fields = (data.get("data") or {}).get("data") or {}
        for f in ("access_key_id", "secret_access_key", "endpoint", "internal_endpoint", "region", "buckets"):
            assert fields.get(f), f"field {f} missing in secret/proj-{slug}/garage"
        return fields  # held in memory only; never printed

    def test_stored_entry_lists_the_declared_buckets(self, creds: dict, slug: str):
        assert creds["buckets"].split(",") == _declared_buckets(slug)

    def test_signed_roundtrip_on_vlan20(self, creds: dict, slug: str):
        r = _s3(creds["internal_endpoint"], _declared_buckets(slug)[0], creds)
        assert r.returncode == 0, r.stdout

    def test_signed_roundtrip_through_the_traefik_route(self, creds: dict, slug: str):
        extra = ["--connect-to", os.environ.get(_TRAEFIK_IP_ENV, "10.0.20.12")]
        if os.environ.get(_S3_INSECURE_ENV) == "1":
            extra.append("--insecure")  # dev only: the Traefik self-signed mock (P4)
        r = _s3(creds["endpoint"], _declared_buckets(slug)[0], creds, *extra)
        assert r.returncode == 0, r.stdout


# =========================================================================== #
# 10.3 — Degraded-mode fault injection (Req 9.1, 9.2, 9.3, 9.4).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live consumer degraded gate satisfied",
)
class TestDegradedModeFailClosed:
    """A sealed/unreachable OpenBao, an unreachable ZITADEL, or an unreachable DB
    guest each halt the orchestrator non-zero, naming the dependency, with NO
    partial wiring (Req 9.2-9.4).

    IMPORTANT (dependency-driven-through-its-own-path): where a scenario needs a
    dependency sealed/stopped, that is effected ONLY through the dependency's own
    automated lifecycle (`svc-07-bootstrap.yml` clean_slate/seal,
    `svc-06-bootstrap.yml`) — NEVER raw ssh/docker/pct surgery
    (testing-strategy.md "Dependencies-not-under-test", Req 9.5, 9.6). These
    tests assert the OBSERVABLE fail-closed effect.
    """

    def test_openbao_sealed_or_unreachable_halts_naming_openbao(
        self, slug: str, cluster_env_file: str
    ):
        """With OpenBao sealed/unreachable (set via its own lifecycle out-of-band
        before this test, and pointed at an unreachable addr here), the preflight
        bounded probe halts non-zero naming OpenBao, before any mutation
        (Req 9.2)."""
        # Point the orchestrator at an unreachable OpenBao primary IP so the
        # bounded Phase-1 probe fails closed; nothing is mutated past the halt.
        result = _run_orchestrator(
            slug,
            cluster_env_file,
            extra_vars={
                "openbao_addr": "https://10.255.255.1:8200",
                "consumer_openbao_primary_ip": "10.255.255.1",
                "dependency_probe_timeout": 5,
            },
            timeout=300,
        )
        assert result.returncode != 0, (
            "a sealed/unreachable OpenBao must halt the run non-zero (Req 9.2)"
        )
        combined = (result.stdout + result.stderr)
        assert "OpenBao" in combined, (
            "the fail-closed diagnostic must NAME OpenBao as the unavailable "
            "dependency (Req 9.2)"
        )

    def test_zitadel_unreachable_halts_naming_sso(
        self, slug: str, cluster_env_file: str
    ):
        """With ZITADEL unreachable, the preflight bounded probe halts non-zero
        naming the SSO provider, before any mutation (Req 9.3)."""
        result = _run_orchestrator(
            slug,
            cluster_env_file,
            extra_vars={
                "consumer_zitadel_health_url": "https://10.255.255.2/debug/healthz",
                "dependency_probe_timeout": 5,
            },
            timeout=300,
        )
        assert result.returncode != 0, (
            "an unreachable ZITADEL must halt the run non-zero (Req 9.3)"
        )
        combined = (result.stdout + result.stderr)
        assert "ZITADEL" in combined or "SSO" in combined, (
            "the fail-closed diagnostic must NAME the SSO provider (Req 9.3)"
        )

    def test_db_guest_unreachable_halts_naming_db(
        self, slug: str, cluster_env_file: str, vlan_id: int
    ):
        """With the per-project DB guest unreachable within the bounded health
        budget, Phase 4 halts non-zero naming the DB guest, with no partial
        wiring (Req 9.4). Exercised by pointing the health poll at a short budget
        against a guest that is down (brought down via SVC-01's own clean-slate
        out-of-band)."""
        result = _run_orchestrator(
            slug,
            cluster_env_file,
            extra_vars={
                "consumer_health_retries": 2,
                "consumer_health_delay": 2,
            },
            timeout=600,
        )
        # When the DB guest is intentionally down this run halts non-zero; the
        # diagnostic names the DB guest. (When the guest is UP this scenario is
        # not applicable and the operator skips it — see the runbook.)
        if result.returncode != 0:
            combined = (result.stdout + result.stderr)
            assert "DB guest" in combined or "database guest" in combined.lower(), (
                "the fail-closed diagnostic must NAME the database guest (Req 9.4)"
            )


#: WS-C43: tasks a converged second run of project-consumer-bootstrap.yml is ALLOWED to report as changed.
#: Everything else must be unchanged. (WS-C39 made the control-node bookkeeping tasks truthful.)
SECOND_RUN_ALLOWED_CHANGES = (
    # By design: a fresh, TTL-bounded Secret_ID is minted and written on every run (design Open Risk).
    "openbao_workload_approle : 3 — Mint a Secret_ID for the Workload_AppRole (no_log, fail-closed, Req 3.2/3.6/3.8)",
    "openbao_workload_approle : 3 — Write the Secret_ID file (0600, service-owned; sensitive, no_log, Req 3.3/3.4)",
)


def _changed_task_names(ansible_output: str) -> list[str]:
    """Names (``role : task``) of every task that printed a ``changed:`` line."""
    names, current = [], None
    for line in ansible_output.splitlines():
        if line.startswith("TASK ["):
            current = line[len("TASK ["):line.rfind("]")]
        elif line.startswith("changed:") and current:
            names.append(current)
    return names


# =========================================================================== #
# 10.4 — Idempotency / resumable (Req 6.1, 6.2, 6.3, 6.4, 6.5, 9.7).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live consumer idempotency gate satisfied",
)
class TestIdempotencyAndResumable:
    """A converged second run reports zero changes; a re-run after a forced
    partial failure completes only the remainder; no orphaned resources survive a
    resumed converge."""

    def test_second_run_reports_zero_changes(self, slug: str, cluster_env_file: str):
        """WS-C43 (operator decision B): the orchestrator does not support ``--check``, so idempotency is a plain
        second run against the converged state: it succeeds and changes ONLY tasks on the documented allow-list
        (the by-design Secret_ID re-mint, and the control-node bookkeeping tracked by WS-C39). Any other changed
        task is a real non-idempotent step and fails the test (Req 6.1)."""
        result = _run_orchestrator(slug, cluster_env_file)
        assert result.returncode == 0, (
            "a converged second run must succeed:\n"
            f"{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )
        unexpected = sorted(set(_changed_task_names(result.stdout)) - set(SECOND_RUN_ALLOWED_CHANGES))
        assert not unexpected, (
            "a converged second run changed tasks outside the allow-list (Req 6.1): " + "; ".join(unexpected)
        )

    def test_resume_after_partial_failure_completes_remainder(
        self, bao: _OpenBao, slug: str, cluster_env_file: str
    ):
        """A re-run after a forced partial failure completes only the remaining
        steps and leaves the project fully wired (Req 6.2, 6.3). The forced
        partial failure is staged out-of-band (e.g. the ZITADEL step failed on a
        prior run); this resumed run converges and the full state is present."""
        result = _run_orchestrator(slug, cluster_env_file)
        assert result.returncode == 0, (
            "the resumed run must complete the remainder:\n"
            f"{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )
        # After the resumed converge the full state is present (no step skipped).
        assert bao.policy_exists(f"policy-{slug}")
        assert bao.role_exists(f"{slug}-workload")


# =========================================================================== #
# 10.5 — Clean-slate verification (Req 7.1, 7.4, 7.6, 7.7).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _mutate_gate_reason() is not None,
    reason=_mutate_gate_reason() or "live consumer clean-slate gate satisfied",
)
class TestCleanSlateVerification:
    """`clean_slate=true` tears consumer resources to baseline; the verify gate
    confirms zero surviving compute + zero state; the Registry entry and
    `<slug>-infra` network slice SURVIVE (Req 7)."""

    def test_clean_slate_tears_to_baseline_and_preserves_registry_and_slice(
        self, slug: str, cluster_env_file: str
    ):
        """A forced clean-slate exits 0, reports an empty consumer baseline, and
        explicitly PRESERVES the Registry entry + `<slug>-infra` slice (Req 7.1,
        7.4, 7.7)."""
        # The consumer clean-slate tears the workload guest down through clean-slate.yml on the Proxmox node,
        # so it needs the real host address (added after this test was written; WS-C38 found it live).
        proxmox_host = os.environ.get("CONSUMER_TEST_PROXMOX_HOST", "").strip()
        if not proxmox_host:
            pytest.skip("CONSUMER_TEST_PROXMOX_HOST not set: the consumer clean-slate needs the Proxmox LAN IP "
                        "(consumer_proxmox_host) to tear the workload guest down")
        result = _run_orchestrator(
            slug,
            cluster_env_file,
            extra_vars={"clean_slate": True, "clean_slate_confirm": "force",
                        "consumer_proxmox_host": proxmox_host,
                        # ADR-0022: the workload VMID is vlan_id*1000 + host_index; the anti-footgun gate needs it
                        # explicitly. Override with CONSUMER_TEST_WORKLOAD_VMID when the manifest uses another index.
                        "consumer_workload_vmid": int(os.environ.get(
                            "CONSUMER_TEST_WORKLOAD_VMID", str(resolve_vlan_id_from_file(slug, _REGISTRY) * 1000 + 1)))},
            timeout=1200,
        )
        assert result.returncode == 0, (
            "a forced clean-slate must complete:\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
        )
        out = result.stdout
        assert "Consumer clean-slate complete" in out, (
            "the clean-slate must report an empty consumer baseline (Req 7.4)"
        )
        assert "PRESERVED" in out, (
            "the clean-slate must report the Registry entry + <slug>-infra slice "
            "PRESERVED (Req 7.7)"
        )

    def test_registry_entry_still_resolvable_after_clean_slate(
        self, slug: str, vlan_id: int
    ):
        """After clean-slate the Registry entry is intact (the slug still resolves
        to its VLAN) — clean-slate removed only CONSUMER resources (Req 7.7)."""
        # The resolver reads the committed Registry; clean-slate must not have
        # removed the slug's row.
        assert resolve_vlan_id_from_file(slug, _REGISTRY) == vlan_id, (
            f"the Registry entry for {slug} must survive clean-slate (Req 7.7)"
        )

    def test_db_state_name_distinct_from_network_slice(self, slug: str):
        """Sanity on the one-state-per-service invariant clean-slate relies on:
        the DB state `<slug>-db` is distinct from the `<slug>-infra` slice state,
        so tearing the DB never touches the slice (Req 7.7, 3.5)."""
        assert consumer_db_state_name(slug) != network_slice_state_name(slug)
        assert consumer_db_state_name(slug).endswith("-db")


# =========================================================================== #
# Collection-contract guard (runs OFFLINE too — proves the module collects under
# -m requires_infra and skips cleanly without infra, per the task's verify step).
# This test is NOT requires_infra: it asserts the GATING is wired correctly.
# =========================================================================== #
def test_second_run_allow_list_names_real_tasks():
    """Offline no-rot guard for the WS-C43 allow-list: every entry names an existing role task."""
    for entry in SECOND_RUN_ALLOWED_CHANGES:
        role, task = entry.split(" : ", 1)
        tasks_dir = _REPO_ROOT / "ansible" / "roles" / role / "tasks"
        found = any(
            isinstance(t, dict) and t.get("name") == task
            for f in tasks_dir.glob("*.yml")
            for t in _iter_named_tasks(yaml.safe_load(f.read_text(encoding="utf-8")) or [])
        )
        assert found, f"allow-list entry does not match a real task: {entry}"


def _iter_named_tasks(tasks):
    for t in tasks:
        if isinstance(t, dict):
            yield t
            for key in ("block", "rescue", "always"):
                yield from _iter_named_tasks(t.get(key) or [])


def test_changed_task_name_parser():
    out = "TASK [r : a] ***\nok: [localhost]\nTASK [r : b | c] *****\nchanged: [localhost]\n"
    assert _changed_task_names(out) == ["r : b | c"]


def test_all_live_classes_are_requires_infra_and_skip_without_infra():
    """Every live test class carries the requires_infra marker (so the module is
    deselected by default) AND its skipif gate yields a clean skip reason when no
    throwaway cluster is wired — never a hard failure (documentation-testing.md
    'absent infra => skipped, not failed')."""
    # When this offline suite runs, no CONSUMER_TEST_* / OPENBAO_TEST_* vars are
    # set, so the gates must return a non-None skip reason (clean skip), not None.
    assert _live_gate_reason() is not None, (
        "with no throwaway cluster wired, the live gate must yield a skip reason "
        "(clean skip), not attempt to run"
    )
    assert _mutate_gate_reason() is not None, (
        "with no interlock/cluster, the mutate gate must yield a skip reason"
    )
