"""Live CONTRACT + idempotency tests for the ``openbao_project_onboard`` role.

Task 9.2 (spec: svc-07-secrets-manager) — requirements.md Requirements 1.2, 1.3,
1.5, 1.6, 13.4; design.md "Testing Strategy §2 (Contract tests — Policy
contract)" and "§1 (Ansible check-mode idempotency)".

=== WHAT THIS FILE CONTAINS (two tests, two established patterns) ===

1) POLICY CONTRACT (Req 1.2, 1.3, 1.6) — ``TestOpenBaoProjectPolicyContract``.
   Mirrors task 7.4's ``test_svc07_openbao_engine_contract.py``: a live/
   ``requires_infra`` test against a REAL OpenBao, class-level ``skipif`` on the
   SAME OpenBao env-var contract (``OPENBAO_TEST_ADDR`` / ``OPENBAO_TEST_TOKEN``),
   a stdlib-``urllib`` HTTP client, disposable ``ctest-<uuid>`` slugs, teardown
   in a ``finally`` block. It writes ``policy-<slug>`` in EXACTLY the shape the
   role's ``templates/policy-slug.hcl.j2`` produces (rendered inline here so the
   assertion does not depend on the role's Jinja var wiring), mints a token bound
   ONLY to that policy, and asserts against the REAL policy engine:
     * the bound token can CRUD (create/read/update/delete + list) under
       ``secret/data/<slug>/*`` and list ``secret/metadata/<slug>/`` (Req 1.2,
       1.6 — the KV path exists and is usable);
     * the bound token is DENIED (403) on any path outside the five prefixes —
       another project's ``secret/data/<otherslug>/*`` and the ``secret/data/``
       root listing — returning NO other project's data or metadata (Req 1.3,
       1.6 — cross-project isolation).

2) ONBOARD IDEMPOTENCY (Req 1.5, 13.4) — ``TestOpenBaoProjectOnboardIdempotency``.
   Mirrors task 6.3's ``test_svc07_openbao_init_unseal_idempotency.py`` shape
   (prime-then-check, PLAY RECAP parse, ``requires_ansible`` gate) BUT — because
   the onboard role's writes are check-then-write against a LIVE ``bao`` via
   ``docker exec``, an "already-onboarded" slug cannot be simulated offline
   without a full ``bao``-over-``docker-exec`` stub — it is ALSO gated
   ``requires_infra`` + the SAME ``OPENBAO_TEST_*`` env-var contract, per the
   task's explicit "prefer consistency and determinism; if you make it
   requires_infra, gate it like the contract test" guidance. It onboards a
   disposable slug for real (prime), captures the stored ``policy-<slug>`` body
   and the KV metadata ``current_version``, re-runs the role in ``--check``, and
   asserts: the play recap reports ``changed=0``, the policy document is
   byte-identical, and the KV secret ``current_version`` is untouched (Req 1.5,
   13.4). It additionally needs ``docker`` + a reachable primary container, so it
   gates on those too and skips cleanly when absent.

=== WHY BOTH ARE requires_infra (offline-skip determinism) ===

Both tests SKIP CLEANLY when the ``OPENBAO_TEST_*`` contract is absent (the
documentation-testing steering rule "absent infra => skipped, not failed"). They
gate on the SAME OpenBao vars task 7.4 already added to the preservation
baseline's ``_REQUIRES_INFRA_ENV_VARS`` scrub-list, so in the offline baseline
they skip DETERMINISTICALLY. ``test_offline_plan_preservation.py`` is updated in
lockstep (``_EXPECTED_SKIPPED`` bumped, representative nodes pinned) so the
baseline stays green — see that file's comments.

SECURITY (security-standards.md — this IS the secrets manager): the admin token
is read from the env and threaded ONLY into request headers / a ``no_log`` play
var; it is NEVER logged, placed on a command line in an assertion message, or
echoed. Every resource uses a disposable ``ctest-<uuid>`` prefix and is torn
down in ``finally`` so the throwaway instance is left clean and a re-run never
collides.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_onboard_contract.py -v
      (skips cleanly with no OPENBAO_TEST_* env vars; runs against a live
       throwaway OpenBao when they are set, opting into the live tier with
       `-m requires_infra`.)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# Env-var contract — the SAME one task 7.4's engine contract test uses, named in
# the `<SERVICE>_TEST_<THING>` style of the existing PROXMOX_*_TEST_* vars.
# --------------------------------------------------------------------------- #
_ADDR_ENV = "OPENBAO_TEST_ADDR"
_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_SKIP_TLS_ENV = "OPENBAO_TEST_SKIP_TLS_VERIFY"

_KV_MOUNT = "secret"  # the single KV v2 engine mount (Req 1.1)

# The five KV v2 sub-paths policy-<slug> scopes to, mirrored from the role's
# defaults (`openbao_project_kv_subpaths`) and templates/policy-slug.hcl.j2.
_KV_SUBPATHS = ("data", "metadata", "delete", "undelete", "destroy")
# The capability set granted on each sub-path (role's
# `openbao_project_policy_capabilities`).
_POLICY_CAPABILITIES = ("create", "read", "update", "delete", "list")


def _live_gate_reason() -> str | None:
    """Skip reason if the live OpenBao gate is unsatisfied, else None.

    Requires BOTH ``OPENBAO_TEST_ADDR`` and ``OPENBAO_TEST_TOKEN``; identical
    clean-skip contract to the sibling engine/auth/audit contract test.
    """
    if not os.environ.get(_ADDR_ENV):
        return (
            f"requires-infra, skipped: {_ADDR_ENV} not set (a live throwaway "
            "OpenBao API endpoint is required for the policy contract / onboard "
            "idempotency tests)"
        )
    if not os.environ.get(_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_TOKEN_ENV} not set (a platform-admin / "
            "root-scoped token on the throwaway OpenBao is required to write the "
            "contract policy and mint the bound token)"
        )
    return None


def _tls_context() -> ssl.SSLContext | None:
    """TLS context for API calls; skips verify by default (self-signed test cert)."""
    skip = os.environ.get(_SKIP_TLS_ENV, "1").strip().lower() not in ("0", "false", "no", "")
    if not skip:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _render_policy_hcl(slug: str) -> str:
    """Render ``policy-<slug>`` EXACTLY as the role's policy-slug.hcl.j2 produces.

    The role templates one ``path "<mount>/<sub>/<slug>/*" { capabilities = [...] }``
    stanza per KV v2 sub-path, capabilities the create/read/update/delete/list
    set — and NOTHING outside the five prefixes (Req 1.2, 1.3). Rendered inline
    here (the task allows "the equivalent inline") so the live assertion does not
    depend on the role's Jinja var wiring.
    """
    caps = ", ".join(json.dumps(c) for c in _POLICY_CAPABILITIES)
    stanzas = [
        f'path "{_KV_MOUNT}/{sub}/{slug}/*" {{\n  capabilities = [{caps}]\n}}'
        for sub in _KV_SUBPATHS
    ]
    return "\n".join(stanzas) + "\n"


class _OpenBao:
    """Minimal live OpenBao HTTP client.

    The token is placed ONLY in the ``X-Vault-Token`` header — never logged,
    never on a command line, never returned in an error message.
    """

    def __init__(self, addr: str, token: str) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._ctx = _tls_context()

    def request(
        self, method: str, path: str, body: dict | None = None, *, token: str | None = None
    ) -> tuple[int, Any]:
        """Issue an API call. ``token`` overrides the admin token (used to call
        as the freshly-minted, policy-bound token)."""
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, method=method, data=data)
        req.add_header("X-Vault-Token", token if token is not None else self._token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
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
            return exc.code, parsed

    def get(self, path: str, *, token: str | None = None) -> tuple[int, Any]:
        return self.request("GET", path, token=token)

    def list(self, path: str, *, token: str | None = None) -> tuple[int, Any]:
        return self.request("LIST", path, token=token)

    def post(self, path: str, body: dict, *, token: str | None = None) -> tuple[int, Any]:
        return self.request("POST", path, body, token=token)

    def delete(self, path: str, *, token: str | None = None) -> tuple[int, Any]:
        return self.request("DELETE", path, token=token)

    def write_policy(self, name: str, policy_hcl: str) -> None:
        self.post(f"sys/policies/acl/{name}", {"policy": policy_hcl})

    def read_policy(self, name: str) -> tuple[int, Any]:
        return self.get(f"sys/policies/acl/{name}")

    def delete_policy(self, name: str) -> None:
        self.delete(f"sys/policies/acl/{name}")

    def create_child_token(self, policies: list[str]) -> str:
        """Mint a child token bound to exactly ``policies`` and return its value.

        ``no_default_policy=true`` ensures the token carries ONLY the project
        policy, so the deny assertions are not masked by the ``default`` policy's
        capabilities.
        """
        status, data = self.post(
            "auth/token/create",
            {"policies": policies, "no_default_policy": True, "ttl": "10m"},
        )
        assert status in (200, 204), f"token create should succeed, got {status}: {data}"
        return data["auth"]["client_token"]


@pytest.fixture(scope="module")
def bao() -> _OpenBao:
    addr = os.environ.get(_ADDR_ENV)
    token = os.environ.get(_TOKEN_ENV)
    assert addr and token, "live gate should have prevented fixture construction"
    return _OpenBao(addr, token)


@pytest.fixture()
def ctest_slug() -> str:
    """A disposable, collision-proof project slug for one test."""
    return f"ctest-{uuid.uuid4().hex[:12]}"


# =========================================================================== #
# 1) POLICY CONTRACT — Req 1.2, 1.3, 1.6 (INFRA-GATED live test).
# =========================================================================== #
@pytest.mark.requires_infra
@pytest.mark.skipif(
    _live_gate_reason() is not None,
    reason=_live_gate_reason() or "live OpenBao policy-contract gate satisfied",
)
class TestOpenBaoProjectPolicyContract:
    """``policy-<slug>`` enforces per-project KV isolation on a REAL OpenBao
    (Req 1.2, 1.3, 1.6).

    Writes the policy in the role's rendered shape, mints a token bound ONLY to
    it, and asserts CRUD+list inside the slug's five prefixes and DENY outside
    them (no other project's data/metadata returned). Everything is torn down in
    ``finally`` so the throwaway instance is left clean.
    """

    def test_bound_token_can_crud_in_prefix_and_is_denied_outside(
        self, bao: _OpenBao, ctest_slug: str
    ):
        slug = ctest_slug
        other_slug = f"{ctest_slug}-other"
        policy_name = f"policy-{slug}"

        created_policies: list[str] = []
        seeded_other = False
        try:
            # --- Arrange: write policy-<slug> in the role's rendered shape ---- #
            bao.write_policy(policy_name, _render_policy_hcl(slug))
            created_policies.append(policy_name)

            # Seed another project's secret with the ADMIN token so the deny
            # assertions can prove "no other project's data returned" — the
            # bound token must not be able to read what is really there.
            status, _ = bao.post(
                f"{_KV_MOUNT}/data/{other_slug}/private",
                {"data": {"cross_project_marker": "must-not-be-readable"}},
            )
            assert status in (200, 204), f"seeding other project's secret failed: {status}"
            seeded_other = True

            # Mint a token bound ONLY to policy-<slug> (no default policy).
            bound_token = bao.create_child_token([policy_name])

            kv_data = f"{_KV_MOUNT}/data/{slug}/app/config"

            # --- CRUD inside secret/data/<slug>/* (Req 1.2) ------------------- #
            # CREATE
            status, _ = bao.post(
                kv_data, {"data": {"k": "v1"}}, token=bound_token
            )
            assert status in (200, 204), (
                f"bound token must CREATE under secret/data/{slug}/* (Req 1.2); "
                f"got {status}"
            )
            # READ (returns the value it just wrote)
            status, data = bao.get(kv_data, token=bound_token)
            assert status == 200, (
                f"bound token must READ under secret/data/{slug}/* (Req 1.2); got {status}"
            )
            assert data["data"]["data"]["k"] == "v1", "read must return the written value"
            # UPDATE
            status, _ = bao.post(
                kv_data, {"data": {"k": "v2"}}, token=bound_token
            )
            assert status in (200, 204), (
                f"bound token must UPDATE under secret/data/{slug}/* (Req 1.2); got {status}"
            )
            status, data = bao.get(kv_data, token=bound_token)
            assert data["data"]["data"]["k"] == "v2", "update must be reflected on read"

            # LIST secret/metadata/<slug>/ (Req 1.2, 1.6) — the KV path exists
            # and is enumerable by the bound token.
            status, data = bao.list(f"{_KV_MOUNT}/metadata/{slug}", token=bound_token)
            assert status == 200, (
                f"bound token must LIST secret/metadata/{slug}/ (Req 1.2/1.6); got {status}"
            )
            assert "app" in (data.get("data", {}).get("keys") or []), (
                "the just-written key must appear under secret/metadata/<slug>/"
            )

            # DELETE (soft-delete the version) under secret/data/<slug>/* (Req 1.2)
            status, _ = bao.delete(kv_data, token=bound_token)
            assert status in (200, 204), (
                f"bound token must DELETE under secret/data/{slug}/* (Req 1.2); got {status}"
            )

            # --- DENY outside the five prefixes (Req 1.3, 1.6) ---------------- #
            # Another project's data path — must be 403, and MUST NOT return the
            # seeded cross-project marker.
            status, data = bao.get(
                f"{_KV_MOUNT}/data/{other_slug}/private", token=bound_token
            )
            assert status == 403, (
                f"bound token must be DENIED (403) on another project's "
                f"secret/data/{other_slug}/* (Req 1.3); got {status}: {data}"
            )
            assert "cross_project_marker" not in json.dumps(data), (
                "a denied cross-project read must NOT leak the other project's "
                "secret data (Req 1.3/1.6)"
            )
            # The KV data ROOT (above any slug) — must be denied, no listing of
            # other projects' prefixes.
            status, data = bao.list(f"{_KV_MOUNT}/metadata", token=bound_token)
            assert status == 403, (
                f"bound token must be DENIED (403) listing the KV metadata ROOT "
                f"(Req 1.3); got {status}: {data}"
            )
            assert other_slug not in json.dumps(data), (
                "a denied root listing must NOT reveal other projects' prefixes "
                "(Req 1.3/1.6)"
            )
            # A sibling top-level path entirely outside the five prefixes.
            status, _ = bao.get("sys/policies/acl", token=bound_token)
            assert status == 403, (
                "bound token must be DENIED on sys/* (outside the five KV "
                f"prefixes, Req 1.3); got {status}"
            )
        finally:
            # Teardown: destroy both projects' KV trees + the policy, with the
            # admin token, so the throwaway instance is left clean.
            bao.delete(f"{_KV_MOUNT}/metadata/{slug}/app/config")
            if seeded_other:
                bao.delete(f"{_KV_MOUNT}/metadata/{other_slug}/private")
            for name in created_policies:
                bao.delete_policy(name)


# =========================================================================== #
# 2) ONBOARD IDEMPOTENCY — Req 1.5, 13.4 (INFRA-GATED live --check test).
# =========================================================================== #
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
_ANSIBLE_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_FIXTURE_DIR = _THIS_DIR / "fixtures" / "svc07_openbao_onboard_idempotency"
_PLAYBOOK = _FIXTURE_DIR / "onboard_slug.yml"
_INVENTORY = _FIXTURE_DIR / "inventory.ini"

#: PLAY RECAP line: "<host> : ok=.. changed=N unreachable=.. failed=.. ..."
_RECAP_RE = re.compile(
    r"^\S+\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)",
    re.MULTILINE,
)


def _ansible_playbook_binary() -> str | None:
    """An ``ansible-playbook`` binary path, or None. PATH first, then venv."""
    on_path = shutil.which("ansible-playbook")
    if on_path:
        return on_path
    venv_candidate = Path.home() / "venv" / "devinfra" / "bin" / "ansible-playbook"
    if venv_candidate.is_file() and os.access(venv_candidate, os.X_OK):
        return str(venv_candidate)
    return None


def _docker_available() -> bool:
    """Whether a usable ``docker`` CLI is present (the role's writes are docker
    exec against the primary container)."""
    return shutil.which("docker") is not None


def _onboard_gate_reason() -> str | None:
    """Skip reason for the idempotency test if any precondition is unmet.

    It needs (a) the live OpenBao env-var contract (same as the contract test),
    (b) an ``ansible-playbook`` binary, and (c) a ``docker`` CLI reaching the
    primary container. Absent any of these => SKIP cleanly (never fail).
    """
    live = _live_gate_reason()
    if live is not None:
        return live
    if _ansible_playbook_binary() is None:
        return "requires ansible-playbook, not installed in venv"
    if not _docker_available():
        return (
            "requires the docker CLI (the openbao_project_onboard role runs its "
            "bao writes via `docker exec` against the primary container)"
        )
    return None


def _parse_recap(output: str) -> dict[str, int]:
    match = _RECAP_RE.search(output)
    assert match is not None, f"no PLAY RECAP found in ansible output:\n{output}"
    return {k: int(v) for k, v in match.groupdict().items()}


def _run_onboard(binary: str, *, slug: str, admin_token: str, check_mode: bool):
    """Invoke the fixture onboard playbook once for ``slug``.

    The admin token is passed via the environment (``OPENBAO_ADMIN_TOKEN``, the
    role's default env-var name) so it never lands on a command line; the play
    reads it with an ``env`` lookup and ``no_log``. Slug validation against
    ``projects.yaml`` is disabled (the disposable ctest slug is not registered),
    matching the role's ``openbao_validate_slug_in_registry`` knob.
    """
    cmd = [
        binary,
        "-i", str(_INVENTORY),
        str(_PLAYBOOK),
        "-e", f"project_slug={slug}",
        "-e", "openbao_validate_slug_in_registry=false",
    ]
    if check_mode:
        cmd.append("--check")

    env = dict(os.environ)
    env["ANSIBLE_ROLES_PATH"] = str(_ANSIBLE_ROLES_DIR)
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_LOCALHOST_WARNING"] = "False"
    env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"
    # The role reads the platform-admin token from this env var (no_log).
    env["OPENBAO_ADMIN_TOKEN"] = admin_token

    return subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.requires_infra
@pytest.mark.skipif(
    _onboard_gate_reason() is not None,
    reason=_onboard_gate_reason() or "onboard idempotency gate satisfied",
)
class TestOpenBaoProjectOnboardIdempotency:
    """A ``--check`` run for an already-onboarded slug is a full no-op (Req 1.5,
    13.4): ``changed=0``, policy document byte-identical, no secret version
    touched.

    Because the role's writes are check-then-write against a LIVE ``bao`` via
    ``docker exec``, "already-onboarded" is established by a real prime run
    against the live throwaway OpenBao (there is no offline stand-in), then the
    same role is re-run in ``--check`` and asserted a no-op. Everything is torn
    down in ``finally``.
    """

    def test_check_run_for_onboarded_slug_is_changed_zero_and_byte_identical(
        self, bao: _OpenBao, ctest_slug: str
    ):
        binary = _ansible_playbook_binary()
        assert binary is not None  # guaranteed by the gate
        admin_token = os.environ[_TOKEN_ENV]
        slug = ctest_slug
        policy_name = f"policy-{slug}"

        try:
            # Phase 1 — PRIME: onboard the slug for real (first run legitimately
            # changes; only its success is required).
            prime = _run_onboard(
                binary, slug=slug, admin_token=admin_token, check_mode=False
            )
            assert prime.returncode == 0, (
                "priming onboard run failed:\n"
                f"STDOUT:\n{prime.stdout}\nSTDERR:\n{prime.stderr}"
            )
            prime_recap = _parse_recap(prime.stdout + prime.stderr)
            assert prime_recap["failed"] == 0 and prime_recap["unreachable"] == 0, (
                f"priming run had failures: {prime_recap}\n{prime.stdout}\n{prime.stderr}"
            )

            # Capture the post-prime state we require to stay untouched:
            #  - the stored policy-<slug> body (byte-identical, Req 1.5)
            #  - the KV metadata current_version (no secret version touched, 13.4)
            status, pol = bao.read_policy(policy_name)
            assert status == 200, f"policy-{slug} must exist after prime; got {status}"
            policy_before = pol["data"]["policy"]

            status, meta = bao.get(f"{_KV_MOUNT}/metadata/{slug}")
            assert status == 200, (
                f"KV metadata for {slug} must exist after prime (Req 1.6); got {status}"
            )
            version_before = meta["data"].get("current_version")

            # Phase 2 — the assertion: a --check re-run is a full no-op.
            check = _run_onboard(
                binary, slug=slug, admin_token=admin_token, check_mode=True
            )
            assert check.returncode == 0, (
                "check onboard run failed:\n"
                f"STDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}"
            )
            recap = _parse_recap(check.stdout + check.stderr)
            assert recap["failed"] == 0 and recap["unreachable"] == 0, (
                f"check run had failures/unreachable: {recap}\n{check.stdout}\n{check.stderr}"
            )
            assert recap["changed"] == 0, (
                "openbao_project_onboard must report changed=0 in --check for an "
                "already-onboarded slug (Req 1.5, 13.4), got "
                f"changed={recap['changed']}.\nSTDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}"
            )

            # Policy document byte-identical (Req 1.5).
            status, pol_after = bao.read_policy(policy_name)
            assert status == 200
            assert pol_after["data"]["policy"] == policy_before, (
                "the stored policy-<slug> document must be byte-identical after a "
                "--check re-run (Req 1.5)"
            )

            # No secret version touched (Req 13.4): current_version unchanged.
            status, meta_after = bao.get(f"{_KV_MOUNT}/metadata/{slug}")
            assert status == 200
            assert meta_after["data"].get("current_version") == version_before, (
                "the KV metadata current_version must be unchanged — the re-run "
                "must touch no secret version (Req 13.4)"
            )
        finally:
            # Teardown with the admin token: destroy the KV tree, the jwt role,
            # and the policy so the throwaway instance is left clean.
            bao.delete(f"{_KV_MOUNT}/metadata/{slug}")
            bao.delete(f"auth/jwt/role/{slug}-ci")
            bao.delete_policy(policy_name)


# =========================================================================== #
# 3) OFFLINE contract — the registry read is CONTROL-NODE-delegated.
#    Spec: fix-openbao-onboard-registry-read (Req 1.1, 2.1).
#
#    projects.yaml is a control-node repo artifact; the onboard slug-validation
#    slurp MUST run on the control node (delegate_to: localhost), or it fails
#    "File not found" on the target guest (live regression that blocked a real
#    SVC-07 bring-up). This is a pure YAML parse — no infra — so it is NOT
#    requires_infra and runs in the default offline suite.
# =========================================================================== #
import yaml as _yaml  # noqa: E402  (local import; keeps the infra tests' import block intact)

_ONBOARD_TASKS = _ANSIBLE_ROLES_DIR / "openbao_project_onboard" / "tasks" / "main.yml"


def _onboard_tasks() -> list[dict]:
    return _yaml.safe_load(_ONBOARD_TASKS.read_text(encoding="utf-8"))


def _slurp_src(task: dict) -> str:
    s = task.get("ansible.builtin.slurp") or task.get("slurp") or {}
    return str(s.get("src", "")) if isinstance(s, dict) else ""


def test_registry_read_is_delegated_to_localhost():
    """The projects.yaml slug-validation slurp must be delegated to the control
    node — it reads a repo artifact that does not exist on the guest (Req 1.1) —
    AND must set become:false (the play runs become:true for docker exec/policy
    writes on the guest; a delegated read on the control node needs no root, and
    inheriting become fails with 'sudo: a password is required' on localhost)."""
    tasks = _onboard_tasks()
    registry_reads = [
        t for t in tasks
        if "projects_yaml_path" in _slurp_src(t) or "projects.yaml" in _slurp_src(t)
    ]
    assert registry_reads, "onboard role must slurp projects.yaml for slug validation"
    for t in registry_reads:
        assert t.get("delegate_to") == "localhost", (
            "the projects.yaml read must carry `delegate_to: localhost` — it is a "
            "control-node repo artifact, not present on the target guest (the read "
            "otherwise fails 'File not found' on the primary LXC)."
        )
        assert t.get("become") is False, (
            "the delegated projects.yaml read must set `become: false` — it runs as "
            "the invoking user on the control node; inheriting the play's become:true "
            "fails with 'sudo: a password is required' on localhost."
        )


def test_policy_tmp_slurp_is_not_delegated():
    """Complementary invariant: the /tmp/<policy>.hcl slurp must NOT be delegated —
    that file is templated onto the GUEST earlier in the same run, so it is read
    host-locally. Guards against a blanket delegate that would break it."""
    tasks = _onboard_tasks()
    tmp_reads = [t for t in tasks if _slurp_src(t).startswith("/tmp/")]
    assert tmp_reads, "onboard role reads the rendered policy from /tmp for byte-compare"
    for t in tmp_reads:
        assert t.get("delegate_to") in (None, ""), (
            "the /tmp/<policy>.hcl slurp must stay host-local (no delegate_to): the "
            "policy is templated onto the guest in this same run."
        )


# =========================================================================== #
# 4) OFFLINE bug-condition — the default projects.yaml path anchors to the
#    repo root (two levels up from playbook_dir), NOT one level up.
#    Spec: fix-openbao-onboard-projects-yaml-path (Property 1 / Req 2.1, 2.2).
#
#    The onboard play lives at ansible/playbooks/openbao.yml, so playbook_dir is
#    <repo>/ansible/playbooks — TWO directories below the repo root. A single
#    `../` in openbao_projects_yaml_path therefore anchors at <repo>/ansible/,
#    normalizing to the NON-EXISTENT <repo>/ansible/infra/platform-foundation/
#    projects.yaml and failing the delegated read "File not found" even for a
#    registered slug. The correct anchoring is `../../` (repo root), matching the
#    sibling openbao_init_unseal `{{ playbook_dir }}/../../.env` convention.
#
#    Pure YAML parse + os.path.normpath — no infra — so it runs in the default
#    offline suite. This test FAILS on the unfixed single-`../` default (the
#    counterexample that proves the bug) and PASSES once the default is
#    re-anchored two levels up.
# =========================================================================== #
_ONBOARD_DEFAULTS = _ANSIBLE_ROLES_DIR / "openbao_project_onboard" / "defaults" / "main.yml"


def test_projects_yaml_default_anchors_to_repo_root():
    """The default `openbao_projects_yaml_path` must resolve to the repo-root
    `infra/platform-foundation/projects.yaml` that actually exists in the
    checkout (Property 1 / Req 2.1, 2.2).

    The onboard play sits at `ansible/playbooks/openbao.yml`, so `playbook_dir`
    is `<repo>/ansible/playbooks` — two levels below the repo root. Substituting
    that representative `playbook_dir` into the default and normalizing it must
    yield the repo-root registry. On the UNFIXED single-`../` default this
    normalizes instead to the non-existent
    `<repo>/ansible/infra/platform-foundation/projects.yaml`, which is the
    counterexample proving the bug."""
    defaults = _yaml.safe_load(_ONBOARD_DEFAULTS.read_text(encoding="utf-8"))
    raw_default = str(defaults["openbao_projects_yaml_path"]).strip()

    # The representative playbook_dir for the onboard play (ansible/playbooks is
    # two levels below the repo root), used to expand the {{ playbook_dir }} token.
    playbook_dir = str(_REPO_ROOT / "ansible" / "playbooks")
    resolved = raw_default.replace("{{ playbook_dir }}", playbook_dir)
    normalized = os.path.normpath(resolved)

    expected = str(_REPO_ROOT / "infra" / "platform-foundation" / "projects.yaml")
    assert normalized == expected, (
        "the default openbao_projects_yaml_path must normalize to the repo-root "
        f"registry {expected!r}, but it normalized to {normalized!r} "
        f"(raw default: {raw_default!r}). A single `../` anchors one directory "
        "too high because playbook_dir is <repo>/ansible/playbooks (two levels "
        "below the repo root) — two `../` are required to reach the repo root."
    )
    assert Path(normalized).is_file(), (
        f"the normalized default path {normalized!r} must point at a file that "
        "actually exists in the checkout — the mis-anchored default points at "
        "<repo>/ansible/infra/platform-foundation/projects.yaml, which does not "
        "exist (the 'File not found' cause)."
    )


def test_projects_yaml_default_matches_two_levels_up_sibling_convention():
    """PRESERVATION (Property 2 / Req 2.3): the onboard default must anchor the
    registry two levels up from `{{ playbook_dir }}`, matching the established
    sibling convention (`openbao_init_unseal`'s `{{ playbook_dir }}/../../.env`
    and `terraform_clean_slate`'s `playbook_dir | dirname | dirname`).

    Concretely, the raw default string — with the `{{ playbook_dir }}` token
    stripped — must contain a `../../` segment relative to that token and must
    NOT anchor at only a single `../`. This pins the corrected depth and guards
    against a regression back to the defective single-`../` anchoring. It FAILS
    on the unfixed default (which has only one `../`) and PASSES once the default
    is re-anchored two levels up."""
    defaults = _yaml.safe_load(_ONBOARD_DEFAULTS.read_text(encoding="utf-8"))
    raw_default = str(defaults["openbao_projects_yaml_path"]).strip()

    # The portion of the path that follows the {{ playbook_dir }} token, with any
    # surrounding whitespace / leading separator normalized away. This is the
    # relative-anchoring segment whose `../` depth we are pinning.
    _TOKEN = "{{ playbook_dir }}"
    assert _TOKEN in raw_default, (
        "the default openbao_projects_yaml_path is expected to be anchored on the "
        f"{_TOKEN!r} magic variable, but it was: {raw_default!r}"
    )
    relative_part = raw_default.split(_TOKEN, 1)[1].lstrip("/ ")

    assert relative_part.startswith("../../"), (
        "the default openbao_projects_yaml_path must anchor TWO levels up "
        f"(`{_TOKEN}/../../infra/platform-foundation/projects.yaml`), matching the "
        "sibling openbao_init_unseal (`{{ playbook_dir }}/../../.env`) and "
        "terraform_clean_slate (`playbook_dir | dirname | dirname`) convention — "
        "the onboard play sits at ansible/playbooks/, two levels below the repo "
        f"root. The relative segment after {_TOKEN} was {relative_part!r} (raw "
        f"default: {raw_default!r}); a single `../` anchors one directory too high."
    )


# =========================================================================== #
# 5) OFFLINE bug-condition — every onboard `bao` call must conditionally splice
#    `-tls-skip-verify` gated on `openbao_primary_tls_skip_verify`.
#    Spec: fix-openbao-onboard-tls-skip-verify (Property 1 / Req 1.1, 1.3, 2.1,
#    2.2).
#
#    The onboard role issues every OpenBao operation as a `bao` CLI command over
#    `docker exec` against the PRIMARY container. Against the homelab self-signed
#    MOCK cert (openbao_tls_self_signed=true), a `bao` call that omits
#    `-tls-skip-verify` fails `x509: certificate signed by unknown authority`;
#    because the task carries `no_log: true` the error is censored to "the
#    command exited with a non-zero return code". The sibling openbao_init_unseal
#    role handles this correctly — it defines
#      openbao_primary_tls_skip_verify: "{{ openbao_tls_self_signed | default(false) | bool }}"
#    and conditionally splices `-tls-skip-verify` AFTER the `bao` subcommand
#    keyword and BEFORE positionals into every `bao` docker-exec argv.
#
#    This is a deterministic static-file bug — no infra — so it runs in the
#    default offline suite. The property is UNIVERSAL over every `bao`-invoking
#    command task in the onboard role: each such task's argv must (a) mention
#    `-tls-skip-verify` and (b) gate it on `openbao_primary_tls_skip_verify`;
#    and the toggle must be present in the role's defaults/main.yml.
#
#    EXPECTED ON UNFIXED CODE: this section FAILS — none of the 8 `bao` command
#    tasks splice the flag, and openbao_primary_tls_skip_verify is undefined in
#    the onboard defaults. Those are the counterexamples that prove the bug.
#    It PASSES once the fix adds the toggle + splices the flag into every call.
# =========================================================================== #

# The `bao` binary token is `openbao_bao_bin` ("bao"); in an argv it appears as
# the token immediately AFTER the `docker exec ... <container>` prefix. Match on
# the role's own variable NAME so the detection tracks the role (not a hardcoded
# "bao" literal) AND works for BOTH argv spellings:
#   * the plain literal list (unfixed): the element is the string
#     `"{{ openbao_bao_bin }}"` (the var name is a substring); and
#   * the Jinja splice scalar (fixed): the element is the bare identifier
#     `openbao_bao_bin` inside `{{ [...] }}` (the var name is again a substring).
# The bare variable name `openbao_bao_bin` is a substring of both, so matching on
# it recognises the `bao` tasks in either form while still excluding the
# `docker cp` task (which references `docker`, not `openbao_bao_bin`).
_BAO_BIN_TOKEN = "openbao_bao_bin"
_TLS_SKIP_FLAG = "-tls-skip-verify"
_TLS_SKIP_TOGGLE = "openbao_primary_tls_skip_verify"


def _command_stdin_raw(task: dict):
    """Return a `command` task's `stdin` parameter (a scalar string), or None.

    Like `argv`, `stdin` is a parameter of the `ansible.builtin.command` module,
    so it is nested UNDER the module dict — not a top-level task keyword. Handles
    both module spellings (`ansible.builtin.command` / `command`).
    """
    cmd = task.get("ansible.builtin.command")
    if cmd is None:
        cmd = task.get("command")
    if not isinstance(cmd, dict):
        return None
    return cmd.get("stdin")


def _command_argv_raw(task: dict):
    """Return a `command` task's raw `argv` (list or scalar string), or None.

    Handles both module spellings (`ansible.builtin.command` / `command`) and
    both argv forms:
      * the plain literal YAML list the UNFIXED onboard role uses (each token on
        its own line — yaml.safe_load yields a Python list); and
      * the sibling role's Jinja splice scalar (`argv: >- {{ [...] + (...) }}`,
        which yaml.safe_load yields as a single folded string).
    """
    cmd = task.get("ansible.builtin.command")
    if cmd is None:
        cmd = task.get("command")
    if not isinstance(cmd, dict):
        return None
    return cmd.get("argv")


def _argv_text(argv) -> str:
    """Flatten an argv (list or scalar) into a single string for substring checks."""
    if argv is None:
        return ""
    if isinstance(argv, str):
        return argv
    if isinstance(argv, (list, tuple)):
        return " ".join(str(x) for x in argv)
    return str(argv)


def _is_bao_command_task(task: dict) -> bool:
    """True iff the task is an `ansible.builtin.command` whose argv invokes `bao`.

    A `bao` call is a docker-exec whose argv contains the `openbao_bao_bin`
    token AFTER a `docker` + `exec` prefix. This deliberately EXCLUDES the
    `docker cp` task (invokes `docker`, not `bao`; no TLS handshake) and every
    non-command task (template/slurp/set_fact/assert/debug)."""
    argv = _command_argv_raw(task)
    if argv is None:
        return False
    text = _argv_text(argv)
    return "docker" in text and "exec" in text and _BAO_BIN_TOKEN in text


def _bao_command_tasks() -> list[dict]:
    return [t for t in _onboard_tasks() if _is_bao_command_task(t)]


def test_onboard_defines_primary_tls_skip_verify_toggle():
    """The onboard role's defaults/main.yml must define
    `openbao_primary_tls_skip_verify` (Property 1 / Req 1.3, 2.2).

    On UNFIXED code this FAILS — the toggle is absent from the onboard role
    entirely (zero matches under the role), which is the first counterexample
    proving the root cause (no toggle to splice)."""
    defaults = _yaml.safe_load(_ONBOARD_DEFAULTS.read_text(encoding="utf-8"))
    assert _TLS_SKIP_TOGGLE in defaults, (
        f"the onboard role's defaults/main.yml must define {_TLS_SKIP_TOGGLE!r} "
        "so every `bao` docker-exec argv can conditionally splice "
        f"{_TLS_SKIP_FLAG!r} against the self-signed mock cert (Req 1.3, 2.2). "
        "It is UNDEFINED on the unfixed role — the counterexample proving the "
        "missing-toggle root cause. The sibling openbao_init_unseal defines it "
        'as "{{ openbao_tls_self_signed | default(false) | bool }}".'
    )


def test_onboard_toggle_matches_sibling_default_byte_for_byte():
    """PRESERVATION-adjacent (Req 3.5): the onboard toggle default must equal the
    sibling openbao_init_unseal default byte-for-byte, so the two roles agree on
    the flag without a shared group_vars file.

    On UNFIXED code this FAILS — the onboard toggle is undefined, so there is no
    string to compare (KeyError-guarded into an explicit assertion failure)."""
    sibling_defaults_path = (
        _ANSIBLE_ROLES_DIR / "openbao_init_unseal" / "defaults" / "main.yml"
    )
    sibling = _yaml.safe_load(sibling_defaults_path.read_text(encoding="utf-8"))
    onboard = _yaml.safe_load(_ONBOARD_DEFAULTS.read_text(encoding="utf-8"))

    assert _TLS_SKIP_TOGGLE in sibling, (
        f"sanity: the sibling openbao_init_unseal must define {_TLS_SKIP_TOGGLE!r}"
    )
    sibling_value = str(sibling[_TLS_SKIP_TOGGLE]).strip()

    assert _TLS_SKIP_TOGGLE in onboard, (
        f"the onboard role must define {_TLS_SKIP_TOGGLE!r} to compare against "
        "the sibling default; it is UNDEFINED on the unfixed role."
    )
    onboard_value = str(onboard[_TLS_SKIP_TOGGLE]).strip()

    assert onboard_value == sibling_value, (
        f"the onboard {_TLS_SKIP_TOGGLE!r} default must equal the sibling "
        f"openbao_init_unseal default byte-for-byte (Req 3.5). "
        f"onboard={onboard_value!r}, sibling={sibling_value!r}."
    )


def test_every_onboard_bao_call_conditionally_skips_tls_verify():
    """UNIVERSAL bug-condition (Property 1 / Req 1.1, 2.1, 2.2): for ALL
    `bao`-invoking command tasks in the onboard role, the task's argv must both
    mention `-tls-skip-verify` AND gate it on `openbao_primary_tls_skip_verify`.

    On UNFIXED code this FAILS — all eight `bao` command tasks build a plain
    literal argv list with NO `-tls-skip-verify` and NO toggle gating, so the
    first `bao` call ("9.1 — List existing policies") fails cert verification
    against the mock cert. Each failing task name is reported as a counterexample.
    It PASSES once every `bao` argv is rewritten to the sibling's splice form."""
    bao_tasks = _bao_command_tasks()
    assert bao_tasks, (
        "sanity: the onboard role must contain at least one `bao` docker-exec "
        "command task (it creates the isolation triple via `bao` CLI calls)."
    )

    missing_flag: list[str] = []
    missing_gate: list[str] = []
    for t in bao_tasks:
        name = str(t.get("name", "<unnamed task>"))
        text = _argv_text(_command_argv_raw(t))
        if _TLS_SKIP_FLAG not in text:
            missing_flag.append(name)
        if _TLS_SKIP_TOGGLE not in text:
            missing_gate.append(name)

    assert not missing_flag and not missing_gate, (
        "every `bao`-invoking onboard task must conditionally splice "
        f"{_TLS_SKIP_FLAG!r} gated on {_TLS_SKIP_TOGGLE!r} (Property 1 / "
        "Req 1.1, 2.1, 2.2), placed after the `bao` subcommand keyword and "
        f"before positionals. Of {len(bao_tasks)} `bao` command task(s):\n"
        f"  - tasks MISSING {_TLS_SKIP_FLAG!r}: {missing_flag}\n"
        f"  - tasks NOT gated on {_TLS_SKIP_TOGGLE!r}: {missing_gate}\n"
        "These are the counterexamples proving the bug — the unfixed role builds "
        "each argv as a plain literal list with no conditional flag, so against "
        "the self-signed mock cert the `bao` calls fail 'x509: certificate "
        "signed by unknown authority' (censored by no_log to 'non-zero return "
        "code')."
    )

# =========================================================================== #
# 6) OFFLINE preservation — baseline invariants the TLS-skip fix must NOT touch.
#    Spec: fix-openbao-onboard-tls-skip-verify (Property 2 / Req 3.1, 3.2, 3.3,
#    3.4, 3.5).
#
#    Observation-first methodology: these assertions capture patterns OBSERVED
#    on the UNFIXED role, so they PASS now (baseline behaviour to preserve) and
#    must keep passing after the fix. They complement section 5's bug-condition
#    tests (which FAIL pre-fix by design):
#
#      * `no_log: true` remains on EVERY `bao` command task in the onboard role.
#        The flag splice adds no secret to the command line, so the
#        secret-suppression posture (Req 3.1) must be untouched — holds on the
#        unfixed role (all 8 `bao` tasks already set no_log: true) and after fix.
#      * the sibling openbao_init_unseal defaults DEFINE the toggle with the
#        exact default expression `{{ openbao_tls_self_signed | default(false) |
#        bool }}` (Req 3.5). This is the SIBLING-SIDE presence check only — the
#        onboard-side parity assertion belongs to the fix-check in task 3.2
#        (test_onboard_toggle_matches_sibling_default_byte_for_byte, section 5),
#        because the onboard toggle does not exist yet on unfixed code. Here we
#        pin ONLY that the sibling source-of-truth the fix mirrors is present and
#        byte-for-byte as expected — which holds on unfixed code.
#      * the existing onboard registry-read / idempotency invariants
#        (test_registry_read_is_delegated_to_localhost,
#        test_projects_yaml_default_*) are untouched — relied on via the existing
#        section-3/4 tests + the full offline suite (Req 3.2, 3.3, 3.4). Not
#        re-asserted here to avoid duplicating those tests; running the whole
#        offline suite confirms they still pass.
#
#    Pure YAML parse — no infra — so these run in the default offline suite.
#    EXPECTED ON UNFIXED CODE: every assertion in this section PASSES.
# =========================================================================== #

# The exact toggle default expression both roles must share (Req 3.5). Kept as a
# module constant so the sibling-side presence check here and the onboard-side
# parity check in section 5 pin the identical string.
_TLS_SKIP_TOGGLE_DEFAULT_EXPR = "{{ openbao_tls_self_signed | default(false) | bool }}"

_SIBLING_INIT_UNSEAL_DEFAULTS = (
    _ANSIBLE_ROLES_DIR / "openbao_init_unseal" / "defaults" / "main.yml"
)


def test_preservation_every_onboard_bao_task_keeps_no_log_true():
    """PRESERVATION (Property 2 / Req 3.1): every `bao`-invoking command task in
    the onboard role keeps its secrets HIDDEN BY DEFAULT.

    Each `bao` docker-exec call carries the platform-admin token in its exec
    environment (`-e BAO_TOKEN=...`), so `no_log` is what keeps that token out of
    the log. Splicing `-tls-skip-verify` adds NO secret to the command line and
    must not weaken this posture.

    The role has since adopted the sibling openbao_init_unseal DEV-ONLY
    secret-exposure toggle (security-standards.md "Secrets handling"): every
    secret-bearing task now sets `no_log: "{{ openbao_no_log }}"` instead of a
    static `no_log: true`. `openbao_no_log` DEFAULTS to true (it is
    `not (openbao_dev_expose_secrets | bool)` and openbao_dev_expose_secrets
    defaults to false), so the "secrets hidden by default" invariant is preserved:
    an ordinary run still censors the token, and only an explicit, per-run
    `-e openbao_dev_expose_secrets=true` reveals it on a throwaway cluster.

    So this preservation check accepts a `no_log` value that is EITHER boolean
    True (the original static posture) OR the secure-by-default toggle expression
    `"{{ openbao_no_log }}"`. It does NOT accept an unconditional False or any
    other value — that would silently expose the token and break the
    NON-NEGOTIABLE acceptance/production posture."""
    bao_tasks = _bao_command_tasks()
    assert bao_tasks, (
        "sanity: the onboard role must contain at least one `bao` docker-exec "
        "command task (it creates the isolation triple via `bao` CLI calls)."
    )

    # "Secrets hidden by default" is satisfied by either the original static
    # `no_log: true` OR the secure-by-default toggle `no_log: "{{ openbao_no_log }}"`
    # (openbao_no_log defaults to true). Anything else (False, absent, an unrelated
    # expression) fails — the invariant is that secrets stay hidden by default.
    _SECURE_NO_LOG_VALUES = (True, "{{ openbao_no_log }}")

    missing_no_log: list[str] = []
    for t in bao_tasks:
        name = str(t.get("name", "<unnamed task>"))
        if t.get("no_log") not in _SECURE_NO_LOG_VALUES:
            missing_no_log.append(name)

    assert not missing_no_log, (
        "every `bao`-invoking onboard task must keep secrets HIDDEN BY DEFAULT "
        "(Req 3.1) — either `no_log: true` or the secure-by-default toggle "
        '`no_log: "{{ openbao_no_log }}"` (openbao_no_log defaults to true). '
        "The exec environment carries the platform-admin token, and the "
        f"`{_TLS_SKIP_FLAG}` splice adds no secret to the command line, so the "
        "secret-suppression posture must be unchanged. Of "
        f"{len(bao_tasks)} `bao` command task(s), these have a `no_log` value "
        f"that does NOT keep secrets hidden by default: {missing_no_log}. "
        "A failure here means a `bao` task lost its `no_log` guard or wired it "
        "to an expression that could expose the token by default."
    )


def test_preservation_sibling_toggle_default_present_byte_for_byte():
    """PRESERVATION (Property 2 / Req 3.5): the sibling openbao_init_unseal
    defaults DEFINE `openbao_primary_tls_skip_verify` with the exact default
    expression the fix mirrors byte-for-byte.

    This pins the SIBLING-SIDE source of truth only. The onboard-side parity
    assertion (onboard default == sibling default) is the fix-check in task 3.2
    (`test_onboard_toggle_matches_sibling_default_byte_for_byte`, section 5),
    which FAILS pre-fix because the onboard toggle does not exist yet — so it is
    deliberately NOT asserted here. What we CAN assert on unfixed code, and what
    must stay true, is that the sibling default is present and unchanged:
    `{{ openbao_tls_self_signed | default(false) | bool }}`. It PASSES now (the
    sibling already defines it) and after the fix (the fix does not touch the
    sibling role)."""
    sibling = _yaml.safe_load(
        _SIBLING_INIT_UNSEAL_DEFAULTS.read_text(encoding="utf-8")
    )
    assert _TLS_SKIP_TOGGLE in sibling, (
        f"the sibling openbao_init_unseal defaults must define {_TLS_SKIP_TOGGLE!r} "
        "— it is the source of truth the onboard fix mirrors (Req 3.5). Its "
        "absence would mean the sibling role regressed."
    )
    sibling_value = str(sibling[_TLS_SKIP_TOGGLE]).strip()
    assert sibling_value == _TLS_SKIP_TOGGLE_DEFAULT_EXPR, (
        f"the sibling openbao_init_unseal {_TLS_SKIP_TOGGLE!r} default must be "
        f"{_TLS_SKIP_TOGGLE_DEFAULT_EXPR!r} byte-for-byte (Req 3.5) — this is the "
        "expression the onboard fix must reuse verbatim so the two roles agree "
        f"without a shared group_vars file. It was {sibling_value!r}."
    )

# =========================================================================== #
# 7) OFFLINE bug-condition + targeted preservation — the jwt-role WRITE must
#    send its request body as a JSON object on stdin, NOT as `key=value`
#    positionals, because `bound_claims` is a map[string]interface{} field.
#    Spec: fix-openbao-onboard-bound-claims-maptype (Property 1 / Req 1.1, 1.2;
#    Property 2 / Req 3.1, 3.2).
#
#    The jwt-role write task ("9.1 — Write auth/jwt/role/<slug>-ci") builds a
#    `bao write <path> key=value key=value …` positional invocation. One field,
#    `bound_claims`, is passed as `'bound_claims=' ~ (openbao_jwt_bound_claims |
#    to_json)` — a STRINGIFIED JSON positional. OpenBao's `bound_claims` schema
#    type is `map[string]interface{}`, and `bao write key=value` delivers every
#    value as a STRING; OpenBao refuses to convert the stringified JSON into a
#    map and returns HTTP 400 ("error converting input for field 'bound_claims':
#    '' expected type 'map[string]interface {}', got unconvertible type
#    'string'"), aborting the onboard run before the role is created.
#
#    The CLI-sanctioned fix is JSON-on-stdin: `docker exec -i … bao write <path>
#    -` with the full request body assembled as a JSON object and fed via the
#    task's `stdin:` key — mirroring the sibling openbao_init_unseal role's
#    `bao policy write <name> -` + `stdin:` idiom (which uses `-i` for exactly
#    this reason).
#
#    This is a deterministic single-task static-file bug — no infra — so it runs
#    in the default offline suite. The property is scoped to the concrete
#    jwt-role WRITE task in the role's main.yml (a static YAML parse).
#
#    EXPECTED ON UNFIXED CODE: the BUG-CONDITION assertions in
#    `test_jwt_role_write_uses_json_stdin_not_stringified_positional` FAIL — the
#    unfixed write uses the `bound_claims=` positional, has no `-i`, no bare `-`,
#    and no `stdin:`. Those are the counterexamples proving the bug. The TARGETED
#    PRESERVATION assertions in
#    `test_jwt_role_write_preserves_tls_splice_and_no_log` PASS on unfixed code
#    (the current write already carries the TLS splice + no_log) and must keep
#    passing after the fix.
# =========================================================================== #

# The register name that uniquely identifies the jwt-role WRITE task (as opposed
# to the jwt-role READ `openbao_jwt_role_read` and the post-write re-read
# `openbao_jwt_role_verify`).
_JWT_ROLE_WRITE_REGISTER = "openbao_jwt_role_write"
# Role-field tokens that only the WRITE carries (the reads request `-format=json`
# and a path, never these body fields) — a secondary robustness signal.
_JWT_WRITE_BODY_TOKENS = ("role_type", "token_type")
# The stringified-positional form the bug uses (must be ABSENT after the fix).
_BOUND_CLAIMS_POSITIONAL = "bound_claims="


def _jwt_role_write_task() -> dict:
    """Locate the jwt-role WRITE task robustly.

    It is the `bao` command task that (a) targets `auth/<jwt_mount>/role/…`,
    (b) invokes the `write` subcommand — identified by
    `register: openbao_jwt_role_write` and/or the presence of the body-field
    tokens `role_type`/`token_type` in its argv/stdin — so it is NOT the
    jwt-role READ (`register: openbao_jwt_role_read`) nor the post-write re-read
    (`register: openbao_jwt_role_verify`), both of which use the `read`
    subcommand and carry neither `role_type` nor `token_type`.
    """
    candidates: list[dict] = []
    for t in _bao_command_tasks():
        text = _argv_text(_command_argv_raw(t))
        # Also fold in the stdin content (present only on the fixed form) so the
        # body-token signal works in either spelling. `stdin` is a parameter of
        # the command module (nested under it), like `argv` — not a top-level key.
        stdin = _command_stdin_raw(t)
        if isinstance(stdin, str):
            text = text + " " + stdin
        targets_role = ("/role/" in text) and (
            "openbao_project_jwt_role_name" in text
        )
        if not targets_role:
            continue
        is_write = (t.get("register") == _JWT_ROLE_WRITE_REGISTER) or all(
            tok in text for tok in _JWT_WRITE_BODY_TOKENS
        )
        if is_write:
            candidates.append(t)

    assert len(candidates) == 1, (
        "expected EXACTLY ONE jwt-role WRITE task (register "
        f"{_JWT_ROLE_WRITE_REGISTER!r} and/or body tokens "
        f"{_JWT_WRITE_BODY_TOKENS}), targeting auth/<jwt_mount>/role/<slug>-ci, "
        f"but found {len(candidates)}: "
        f"{[t.get('name', '<unnamed>') for t in candidates]}. The locator must "
        "not match the jwt-role READ or the post-write re-read."
    )
    return candidates[0]


def test_jwt_role_write_uses_json_stdin_not_stringified_positional():
    """BUG CONDITION (Property 1 / Req 1.1, 1.2): the jwt-role WRITE must NOT
    pass `bound_claims` as a stringified `key=value` positional, and MUST use the
    JSON-on-stdin form (`docker exec -i … bao write <path> -` with a `stdin:`
    JSON object carrying `bound_claims` as a map).

    On UNFIXED code this FAILS — the write builds a `bao write <path> …
    bound_claims=<json-string> …` positional argv with no `-i`, no bare `-`, and
    no `stdin:` key. Those are the counterexamples proving the bug:
      * argv contains the positional `'bound_claims=' ~ (openbao_jwt_bound_claims
        | to_json)` (substring `bound_claims=` present); and
      * no `-i` in the `docker exec` prefix, no bare `-` positional, no `stdin:`.
    It PASSES once the write is rewritten to JSON-on-stdin.
    """
    write = _jwt_role_write_task()
    argv = _command_argv_raw(write)
    argv_text = _argv_text(argv)

    # --- (a) the stringified positional must be ABSENT ---------------------- #
    assert _BOUND_CLAIMS_POSITIONAL not in argv_text, (
        "the jwt-role WRITE must NOT pass bound_claims as a stringified "
        f"`key=value` positional — the substring {_BOUND_CLAIMS_POSITIONAL!r} "
        "must be absent from the write argv (bound_claims is a "
        "map[string]interface{} field; `bao write key=value` delivers it as a "
        "STRING and OpenBao 400s). COUNTEREXAMPLE (unfixed): the argv contains "
        f"the positional `'bound_claims=' ~ (openbao_jwt_bound_claims | "
        f"to_json)`. Full write argv: {argv_text!r}"
    )

    # --- (b) `-i` must be present in the docker exec prefix ----------------- #
    # Match `-i` as a distinct argv token (list form) or as a standalone token
    # in the folded Jinja-scalar form, avoiding a false match inside another
    # word (e.g. `-format`).
    has_dash_i = ("'-i'" in argv_text) or ('"-i"' in argv_text) or (
        isinstance(argv, (list, tuple)) and "-i" in [str(x) for x in argv]
    )
    assert has_dash_i, (
        "the jwt-role WRITE must add `-i` to its `docker exec` prefix so stdin "
        "is forwarded into the container (without `-i` the container's `bao` "
        "sees empty stdin and OpenBao 400s 'missing data' — the exact reason the "
        "sibling openbao_init_unseal stdin policy write uses `-i`). COUNTEREXAMPLE "
        f"(unfixed): no `-i` in the prefix. Full write argv: {argv_text!r}"
    )

    # --- (c) a bare `-` positional must follow the role path ---------------- #
    has_bare_dash = ("'-'" in argv_text) or ('"-"' in argv_text) or (
        isinstance(argv, (list, tuple)) and "-" in [str(x) for x in argv]
    )
    assert has_bare_dash, (
        "the jwt-role WRITE must use a bare `-` positional after the role path "
        "(`bao write <path> -`) so the request body is read from stdin. "
        f"COUNTEREXAMPLE (unfixed): no bare `-` positional. Full write argv: "
        f"{argv_text!r}"
    )

    # --- (d) a `stdin:` key must render a JSON object with bound_claims as map  #
    # `stdin` is a parameter of the `ansible.builtin.command` module (nested
    # under it), exactly like `argv` — read it from the module dict, not the
    # top-level task keys.
    stdin = _command_stdin_raw(write)
    assert isinstance(stdin, str) and stdin.strip(), (
        "the jwt-role WRITE must carry a `stdin:` key with the full request body "
        "as a JSON object. COUNTEREXAMPLE (unfixed): the write task has no "
        f"`stdin:` key at all. Command keys: "
        f"{sorted((write.get('ansible.builtin.command') or write.get('command') or {}).keys())}"
    )
    # The stdin is a Jinja expression that renders a dict `| to_json`. We can't
    # execute Jinja here, but we can assert it assembles a JSON object that
    # includes `bound_claims` sourced from the map fact `openbao_jwt_bound_claims`
    # — i.e. bound_claims is a MAP (the fact), not a stringified positional.
    assert "bound_claims" in stdin and "openbao_jwt_bound_claims" in stdin, (
        "the `stdin:` JSON body must include `bound_claims` sourced from the map "
        "fact `openbao_jwt_bound_claims` (so it is delivered as a JSON map, not a "
        f"string). stdin content: {stdin!r}"
    )
    assert "to_json" in stdin, (
        "the `stdin:` body must be serialized as JSON (expected a `| to_json` "
        f"filter on the assembled request-body object). stdin content: {stdin!r}"
    )


def test_jwt_role_write_preserves_tls_splice_and_no_log():
    """TARGETED PRESERVATION (Property 2 / Req 3.1, 3.2): the jwt-role WRITE task
    specifically must keep conditionally splicing `-tls-skip-verify` gated on
    `openbao_primary_tls_skip_verify`, and keep `no_log: "{{ openbao_no_log }}"`
    secure-by-default — both before AND after the fix.

    This PASSES on unfixed code (the current write already carries both) and must
    keep passing after the rewrite to JSON-on-stdin. It complements the universal
    section-5/6 checks with a pin on the one task this fix rewrites.
    """
    write = _jwt_role_write_task()
    argv_text = _argv_text(_command_argv_raw(write))

    assert _TLS_SKIP_FLAG in argv_text, (
        "the jwt-role WRITE must still mention `-tls-skip-verify` (Req 3.1); "
        f"write argv: {argv_text!r}"
    )
    assert _TLS_SKIP_TOGGLE in argv_text, (
        "the jwt-role WRITE must still GATE `-tls-skip-verify` on "
        f"{_TLS_SKIP_TOGGLE!r} (Req 3.1); write argv: {argv_text!r}"
    )
    assert write.get("no_log") == "{{ openbao_no_log }}", (
        "the jwt-role WRITE must keep `no_log: \"{{ openbao_no_log }}\"` "
        f"secure-by-default (Req 3.2); got no_log={write.get('no_log')!r}"
    )
