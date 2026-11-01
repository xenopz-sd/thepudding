"""Offline structural smoke for the reference hello-workload stand-up.

Feature: consumer-last-mile-hello-workload §A (Req 1.1, 1.2, 1.5), task 4.3.
design.md "Component 4 — Offline layer", "Property 2 (fail-closed swap-gate)",
"Property 5 (secret-mediation preserved)".

This is the OFFLINE (Tier-1) structural assertion for the hello-workload smoke:
the reference Compose var set the `hello_workload` / `hello_workload_deploy`
roles thread into the EXISTING `openbao-agent` role wires the swap-gate correctly
and preserves secret-mediation — provable from the role YAML alone, with NO live
Ansible/Docker/OpenBao run.

It parses the role source with PyYAML and asserts, over the hello-workload
stand-up (the task-4.1 `hello_workload` var set composed into `openbao-agent`
per task 4.2):

 1. SWAP-GATE  — the workload `depends_on` the Agent sidecar with
    `condition: service_healthy` (fail-closed — Property 2).
 2. NO HOST PORT — the workload service publishes no `ports:` (Traefik-only
    ingress, integration-boundaries §1).
 3. TRAEFIK LABEL — a `traefik.enable=true` + a `Host()` router-rule label
    (ingress via labels, not a host port).
 4. RSF / DATABASE_URL — the workload layers the Agent-rendered
    Rendered_Secret_File as `extra_env_files` so `DATABASE_URL` is read from the
    RSF at container start; the agent mapping is the v0.11.3
    `{db, database_url, DATABASE_URL}` (`hello_workload_secret_mappings`), read
    ONLY from the RSF — never a committed/templated env value.
 5. /HEALTHZ HEALTHCHECK — the workload declares a Compose healthcheck probing
    `/healthz` (the task-4.1 CGI endpoint).
 6. NO MUTATING `bao` VERB — the hello-workload stand-up path performs no
    mutating OpenBao CLI verb (`bao write`/`put`/`policy write`/`secrets
    enable`/...). It only READS a secret via the Agent (Property 5 —
    secret-mediation preserved, no new OpenBao mutation).
 7. NO SECRET ECHOED — no `debug`/log in the stand-up path echoes a secret
    value; `no_log` is preserved on any secret-bearing task; the entrypoint/vars
    reference `DATABASE_URL` by KEY NAME only.

Mirrors the structural style of the sibling
``test_workload_auth_swap_gate.py`` (PyYAML parse of the rendered/role wiring)
WITHOUT duplicating its assertions — that test exercises the generic
openbao-agent swap-gate through the docker-compose-app template; THIS test is
specifically about the hello-workload reference's wiring (the `hello_workload`
var set + its composition into `openbao-agent`).

No secret VALUE appears anywhere — the only secret reference is the KV path/env
KEY the Agent renders at runtime into the 0600 RSF.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"

_HELLO_DEFAULTS = _ROLES / "hello_workload" / "defaults" / "main.yml"
_HELLO_DEPLOY_TASKS = _ROLES / "hello_workload_deploy" / "tasks" / "main.yml"
_HELLO_DEPLOY_DEFAULTS = _ROLES / "hello_workload_deploy" / "defaults" / "main.yml"
_AGENT_TASKS = _ROLES / "openbao-agent" / "tasks" / "main.yml"
_AGENT_DEFAULTS = _ROLES / "openbao-agent" / "defaults" / "main.yml"

# Mutating OpenBao CLI verbs. The stand-up path must READ via the Agent only
# (Property 5) — it must never itself mutate OpenBao. `bao read`/`bao status` are
# NON-mutating and allowed; everything below writes/enables/deletes state.
_MUTATING_BAO = re.compile(
    r"\bbao\s+("
    r"write|put|kv\s+put|delete|kv\s+delete|"
    r"policy\s+write|policy\s+delete|"
    r"secrets\s+enable|secrets\s+disable|secrets\s+tune|"
    r"auth\s+enable|auth\s+disable|auth\s+tune|"
    r"token\s+create|token\s+revoke|"
    r"operator\s+init|operator\s+unseal|operator\s+generate-root|"
    r"lease\s+revoke|unwrap"
    r")\b"
)


# --------------------------------------------------------------------------- #
# Helpers — load the role sources once.
# --------------------------------------------------------------------------- #
def _load_tasks(path: Path) -> list:
    """Parse a tasks/main.yml into a list of task dicts."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _load_defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _find_set_fact(tasks: list, key: str) -> dict:
    """Return the `set_fact` payload that defines ``key`` (raw, unresolved)."""
    for task in tasks:
        if not isinstance(task, dict):
            continue
        sf = task.get("ansible.builtin.set_fact") or task.get("set_fact")
        if isinstance(sf, dict) and key in sf:
            return sf
    raise AssertionError(f"no set_fact defining {key!r} found")


def _find_include_role_vars(tasks: list, role_name: str) -> dict:
    """Return the `vars:` passed to an include_role of ``role_name``."""
    for task in tasks:
        if not isinstance(task, dict):
            continue
        inc = task.get("ansible.builtin.include_role") or task.get("include_role")
        if isinstance(inc, dict) and inc.get("name") == role_name:
            return task.get("vars") or {}
    raise AssertionError(f"no include_role of {role_name!r} found")


@pytest.fixture(scope="module")
def hello_defaults() -> dict:
    return _load_defaults(_HELLO_DEFAULTS)


@pytest.fixture(scope="module")
def agent_tasks() -> list:
    return _load_tasks(_AGENT_TASKS)


@pytest.fixture(scope="module")
def agent_wiring(agent_tasks) -> dict:
    """The FIXED swap-gate workload wiring dict the openbao-agent role builds.

    This is the authoritative, caller-independent part of the workload service
    dict (design 4.2 ``openbao_agent_workload_service_wiring``) — asserting on it
    structurally avoids running Ansible's `combine()`/`set_fact` logic while
    still pinning the real wiring the hello-workload stand-up inherits.
    """
    sf = _find_set_fact(agent_tasks, "openbao_agent_workload_service_wiring")
    return sf["openbao_agent_workload_service_wiring"]


# --------------------------------------------------------------------------- #
# 1. SWAP-GATE — depends_on the Agent sidecar, condition: service_healthy.
# --------------------------------------------------------------------------- #
def test_workload_depends_on_agent_service_healthy(agent_wiring):
    """Property 2 (fail-closed): the workload starts only once the Agent is
    `service_healthy` — i.e. only once the Rendered_Secret_File exists. A
    sealed/unreachable Primary ⇒ no RSF ⇒ Agent never healthy ⇒ workload never
    starts."""
    depends = agent_wiring.get("depends_on") or {}
    assert "openbao-agent" in depends, (
        "the hello-workload must depend_on the openbao-agent sidecar; got "
        f"{depends!r}"
    )
    cond = depends["openbao-agent"]
    cond_value = cond.get("condition") if isinstance(cond, dict) else cond
    assert cond_value == "service_healthy", (
        "the swap-gate must be `condition: service_healthy` (fail-closed); got "
        f"{cond_value!r}"
    )


# --------------------------------------------------------------------------- #
# 2. NO HOST PORT — Traefik-only ingress (integration-boundaries §1).
# --------------------------------------------------------------------------- #
def test_workload_wiring_publishes_no_host_port(agent_wiring):
    """The fixed workload wiring declares no `ports:` entry — HTTP ingress is
    Traefik-only (integration-boundaries §1), never a published host port."""
    assert "ports" not in agent_wiring, (
        "the hello-workload wiring must publish NO host `ports:` (Traefik-only "
        f"ingress); got {agent_wiring.get('ports')!r}"
    )


def test_agent_tasks_render_no_workload_host_port(agent_tasks):
    """Belt-and-braces: the openbao-agent tasks file sets no `ports` key on the
    workload service dict anywhere (neither in the wiring set_fact nor the
    dca_services entry). The only sanctioned ingress is the Traefik label."""
    # The workload service dict is the wiring set_fact + base/optional combine;
    # none of those three parts may introduce a `ports` key.
    for key in (
        "openbao_agent_workload_service_wiring",
        "openbao_agent_workload_service_optional",
        "openbao_agent_workload_service",
    ):
        sf = _find_set_fact(agent_tasks, key)
        payload = sf[key]
        if isinstance(payload, dict):
            assert "ports" not in payload, (
                f"{key} must not introduce a `ports` key; got {payload!r}"
            )


# --------------------------------------------------------------------------- #
# 3. TRAEFIK LABEL — traefik.enable=true + a Host() router rule.
# --------------------------------------------------------------------------- #
def test_workload_carries_traefik_enable_and_host_rule(agent_wiring):
    """Ingress via Docker labels, not a host port (integration-boundaries §1):
    the workload wiring carries `traefik.enable=true` and a `Host()` router
    rule label."""
    labels = agent_wiring.get("labels") or []
    joined = " ".join(str(x) for x in labels)
    assert "traefik.enable=true" in joined, (
        f"the hello-workload must carry `traefik.enable=true`; got {labels!r}"
    )
    assert "traefik.http.routers" in joined and ".rule=Host(" in joined, (
        "the hello-workload must declare a Traefik `Host()` router-rule label; "
        f"got {labels!r}"
    )


# --------------------------------------------------------------------------- #
# 4. RSF / DATABASE_URL — extra_env_files (required: false) + v0.11.3 mapping.
# --------------------------------------------------------------------------- #
def test_workload_reads_rsf_via_extra_env_files_required_false(agent_wiring):
    """The workload layers the Agent-rendered Rendered_Secret_File as an
    `extra_env_files` entry (so `DATABASE_URL` is read from the RSF at container
    start), long-form `required: false` so Compose does not abort at its eager
    env_file existence check before the swap-gate runs."""
    extra = agent_wiring.get("extra_env_files")
    assert isinstance(extra, list) and extra, (
        f"the workload must layer the RSF via extra_env_files; got {extra!r}"
    )
    rsf_entry = next(
        (e for e in extra if isinstance(e, dict) and "rendered_secret_file" in str(e.get("path", ""))),
        None,
    )
    assert rsf_entry is not None, (
        "the RSF (openbao_agent_rendered_secret_file) must be the extra_env_files "
        f"source — the swap-gate seam; got {extra!r}"
    )
    assert rsf_entry.get("required") is False, (
        "the RSF entry must be long-form `required: false` (the file does not "
        f"exist until the Agent renders it); got {rsf_entry!r}"
    )


def test_hello_workload_secret_mapping_is_the_v0_11_3_db_bridge(hello_defaults):
    """Req 1.2 / Property 5: the agent secret mapping the reference threads into
    `openbao_agent_secret_mappings` is the v0.11.3 DB bridge
    `{db, database_url, DATABASE_URL}` — a KV READ reference + env KEY only, no
    secret value."""
    mappings = hello_defaults.get("hello_workload_secret_mappings")
    assert isinstance(mappings, list) and len(mappings) == 1, (
        f"expected exactly one DB mapping; got {mappings!r}"
    )
    m = mappings[0]
    assert m.get("kv_subpath") == "db", f"kv_subpath must be `db`; got {m!r}"
    assert m.get("kv_field") == "database_url", (
        f"kv_field must be `database_url` (the v0.11.3 bridge field); got {m!r}"
    )
    assert m.get("env_key") == "DATABASE_URL", (
        f"env_key must be `DATABASE_URL`; got {m!r}"
    )


def test_hello_workload_database_url_referenced_by_key_name_only(hello_defaults):
    """Req 1.2 / secret-mediation: DATABASE_URL is referenced as a KEY NAME, and
    the reference carries no committed/templated DB secret value. The workload
    reads it ONLY from the RSF at container start."""
    # The reference gates health on the env KEY name, never a value.
    assert hello_defaults.get("hello_workload_secret_env") == "DATABASE_URL", (
        "hello_workload_secret_env must name the key DATABASE_URL; got "
        f"{hello_defaults.get('hello_workload_secret_env')!r}"
    )
    # The non-secret per-container env must NOT carry a DATABASE_URL value — it
    # may name the key (HELLO_WORKLOAD_SECRET_ENV) but never assign the secret.
    env = hello_defaults.get("hello_workload_env") or {}
    assert "DATABASE_URL" not in env, (
        "the non-secret workload env must NOT set DATABASE_URL (it is rendered "
        f"into the RSF by the Agent, never templated); got {env!r}"
    )


def test_deploy_threads_default_db_mapping_into_agent():
    """Req 1.2: the hello_workload_deploy role threads
    `hello_workload_secret_mappings` into the openbao-agent role's
    `openbao_agent_secret_mappings` input — the ONLY secret source is the Agent
    (no new agent wiring)."""
    tasks = _load_tasks(_HELLO_DEPLOY_TASKS)
    inc_vars = _find_include_role_vars(tasks, "openbao-agent")
    mapping_expr = str(inc_vars.get("openbao_agent_secret_mappings", ""))
    assert "hello_workload_secret_mappings" in mapping_expr, (
        "hello_workload_deploy must pass hello_workload_secret_mappings as "
        f"openbao_agent_secret_mappings; got {inc_vars!r}"
    )


# --------------------------------------------------------------------------- #
# 5. /HEALTHZ HEALTHCHECK — a Compose healthcheck probing /healthz.
# --------------------------------------------------------------------------- #
def test_workload_declares_healthz_healthcheck(hello_defaults):
    """Req 1.1: the workload declares a Compose healthcheck probing `/healthz`
    (the task-4.1 CGI endpoint) — healthy only once DATABASE_URL is present."""
    hc = hello_defaults.get("hello_workload_healthcheck") or {}
    assert hc, "the reference must declare a hello_workload_healthcheck"
    test = hc.get("test")
    test_str = " ".join(str(x) for x in test) if isinstance(test, list) else str(test)
    assert "healthz" in test_str, (
        f"the healthcheck must probe the /healthz endpoint; got {test!r}"
    )


def test_deploy_threads_healthcheck_into_agent():
    """The /healthz healthcheck reaches the workload service via the deploy
    role's `openbao_agent_workload_healthcheck` pass-through — the workload-layer
    swap-gate signal, on top of the Agent's RSF-presence healthcheck."""
    tasks = _load_tasks(_HELLO_DEPLOY_TASKS)
    inc_vars = _find_include_role_vars(tasks, "openbao-agent")
    hc_expr = str(inc_vars.get("openbao_agent_workload_healthcheck", ""))
    assert "hello_workload_healthcheck" in hc_expr, (
        "hello_workload_deploy must pass hello_workload_healthcheck as "
        f"openbao_agent_workload_healthcheck; got {inc_vars!r}"
    )


# --------------------------------------------------------------------------- #
# 6. NO MUTATING `bao` VERB — only reads a secret via the Agent (Property 5).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [_HELLO_DEFAULTS, _HELLO_DEPLOY_TASKS, _HELLO_DEPLOY_DEFAULTS, _AGENT_TASKS, _AGENT_DEFAULTS],
    ids=lambda p: p.relative_to(_REPO_ROOT).as_posix(),
)
def test_standup_path_has_no_mutating_bao_verb(path):
    """Property 5 (secret-mediation preserved, no new OpenBao mutation): the
    hello-workload stand-up path (the hello_workload / hello_workload_deploy
    roles + the composed openbao-agent role) contains NO mutating OpenBao CLI
    verb — it only READS a secret via the Agent's AppRole auto_auth.

    `bao read`/`bao status` are non-mutating and allowed; `bao write`/`put`/
    `policy write`/`secrets enable`/`operator init`/etc. are not."""
    text = path.read_text(encoding="utf-8")
    hits = _MUTATING_BAO.findall(text)
    assert not hits, (
        f"{path.relative_to(_REPO_ROOT).as_posix()} contains a mutating OpenBao "
        f"verb in the hello-workload stand-up path: {hits!r}. The reference must "
        "only READ a secret via the Agent — no new OpenBao mutation (Property 5)."
    )


# --------------------------------------------------------------------------- #
# 7. NO SECRET ECHOED — no debug echoes a secret; no_log preserved.
# --------------------------------------------------------------------------- #
def _iter_tasks_recursive(tasks: list):
    """Yield every task dict, descending into block/rescue/always."""
    for task in tasks:
        if not isinstance(task, dict):
            continue
        yield task
        for nest in ("block", "rescue", "always"):
            if isinstance(task.get(nest), list):
                yield from _iter_tasks_recursive(task[nest])


def test_no_debug_echoes_a_secret_value_in_standup_path():
    """Req 5.3 / security-standards: no `debug` task in the stand-up path echoes
    a secret value. Any debug may reference DATABASE_URL by KEY NAME, but must
    not interpolate a secret value (e.g. a rendered RSF field / a role_id /
    secret_id / token)."""
    forbidden = re.compile(
        r"\.Data\.data\.|role_id|secret_id|openbao_admin_token|root_token|"
        r"database_url\s*}}|DATABASE_URL\s*}}",
        re.IGNORECASE,
    )
    for role_tasks in (_load_tasks(_HELLO_DEPLOY_TASKS), _load_tasks(_AGENT_TASKS)):
        for task in _iter_tasks_recursive(role_tasks):
            dbg = task.get("ansible.builtin.debug") or task.get("debug")
            if not isinstance(dbg, dict):
                continue
            msg = str(dbg.get("msg", "")) + str(dbg.get("var", ""))
            assert not forbidden.search(msg), (
                "a debug task in the stand-up path interpolates a secret value: "
                f"{msg!r}. Reference secrets by KEY NAME / path only (Req 5.3)."
            )


def test_no_log_preserved_on_agent_secret_bearing_include(agent_tasks):
    """Req 5.3 / security-standards (mirroring the sibling swap-gate test's
    no_log posture): the openbao-agent role resolves `no_log` from
    `openbao_no_log` (default TRUE) and NEVER hardcodes `no_log: false` on any
    task in the stand-up path. The reference inherits that posture — it adds no
    secret-bearing task of its own."""
    agent_defaults = _load_defaults(_AGENT_DEFAULTS)
    # The no_log knob exists and defaults closed (not dev-exposed).
    assert "openbao_no_log" in agent_defaults, (
        "openbao-agent must define openbao_no_log (the resolved no_log value)."
    )
    assert agent_defaults.get("openbao_dev_expose_secrets") is False, (
        "openbao_dev_expose_secrets must default to False so openbao_no_log is "
        "True (secrets hidden) — the non-negotiable acceptance/production default."
    )
    # No task in either role hardcodes no_log off.
    for role_tasks in (agent_tasks, _load_tasks(_HELLO_DEPLOY_TASKS)):
        for task in _iter_tasks_recursive(role_tasks):
            if "no_log" in task:
                assert task["no_log"] not in (False, "false", "no"), (
                    f"a task hardcodes no_log off: {task.get('name')!r} — the "
                    "secret-hygiene control must never be weakened (Req 5.3)."
                )


def test_deploy_threads_only_nonsecret_env_into_agent():
    """Req 5.3: the deploy role passes only the NON-secret per-container env
    (`hello_workload_env` — port/webroot/secret-env-NAME) into the agent role's
    `openbao_agent_workload_environment`; the DB secret itself never travels
    through a templated env, only the RSF."""
    tasks = _load_tasks(_HELLO_DEPLOY_TASKS)
    inc_vars = _find_include_role_vars(tasks, "openbao-agent")
    env_expr = str(inc_vars.get("openbao_agent_workload_environment", ""))
    assert "hello_workload_env" in env_expr, (
        "hello_workload_deploy must pass hello_workload_env (non-secret) as "
        f"openbao_agent_workload_environment; got {inc_vars!r}"
    )
    # And hello_workload_env carries no DATABASE_URL VALUE (only names it).
    hello_defaults = _load_defaults(_HELLO_DEFAULTS)
    env = hello_defaults.get("hello_workload_env") or {}
    assert "DATABASE_URL" not in env, (
        "hello_workload_env must not carry a DATABASE_URL value (it arrives via "
        f"the RSF); got {env!r}"
    )


# =========================================================================== #
# TASK 5.1 — the LIVE layer scaffold (requires_infra, skipped-not-failed) +
# the doctest-tier assertion over the hello-workload documentation.
#
# design.md "Component 4 — the hello-workload smoke" defines TWO layers:
#   * the OFFLINE (Tier-1) structural assertions above (task 4.3), and
#   * a `requires-infra` LIVE smoke (happy path + fail-closed swap-gate).
# Task 5.1 adds the live layer's TEST-SIDE SCAFFOLD here, so this one module
# carries BOTH tiers — matching the design's "two layers" and the repo's
# skip-not-fail contract. The live walkthrough PROSE (onboard → stand up →
# assert healthy; sealed-Primary → fail-closed) is task 5.2 (the install
# runbook); this scaffold is the gated acceptance hook, NOT run in the offline
# gate (`pytest.ini` deselects `requires_infra` by default).
# =========================================================================== #

import os  # noqa: E402

# --------------------------------------------------------------------------- #
# Env-var contract for the LIVE hello-workload smoke (referenced by NAME only;
# no secret value is ever echoed, logged, or placed in an assertion message).
# Mirrors the sibling `test_consumer_onboarding_integration.py` layered gate:
# the `requires_infra` marker is the OUTER gate (deselect by default); this
# skipif is the INNER "even when opted in, skip cleanly if not actually wired"
# gate (documentation-testing.md "absent infra => skipped, not failed").
# --------------------------------------------------------------------------- #
_LIVE_SLUG_ENV = "CONSUMER_TEST_SLUG"
_LIVE_CLUSTER_ENV = "CONSUMER_TEST_CLUSTER_ENV"
_LIVE_BAO_ADDR_ENV = "OPENBAO_TEST_ADDR"
_LIVE_BAO_TOKEN_ENV = "OPENBAO_TEST_TOKEN"
_LIVE_ADMIN_TOKEN_ENV = "OPENBAO_ADMIN_TOKEN"
_LIVE_WORKLOAD_URL_ENV = "HELLO_WORKLOAD_TEST_URL"  # the /healthz URL of the stood-up workload
_LIVE_ALLOW_MUTATE_ENV = "CONSUMER_TEST_ALLOW_MUTATE"
_LIVE_AGENT_AUTH_ENV = "AGENT_LIVE_AUTHORIZED"


def _hello_workload_live_gate_reason() -> str | None:
    """Return a skip reason if the live hello-workload smoke is not wired, else None.

    The `requires_infra` MARKER already deselects this by default; this inner
    gate makes a `-m requires_infra` opt-in run on an UNWIRED machine SKIP
    cleanly (never FAIL) — the `documentation-testing.md` "absent infra =>
    skipped, not failed" rule and Req 1.5 (offline => skipped-not-failed)."""
    if not os.environ.get(_LIVE_SLUG_ENV):
        return (
            f"requires-infra, skipped: {_LIVE_SLUG_ENV} not set (a throwaway "
            "slug onboarded with database+secrets+workload is required to stand "
            "up the hello-workload)"
        )
    if not os.environ.get(_LIVE_CLUSTER_ENV):
        return (
            f"requires-infra, skipped: {_LIVE_CLUSTER_ENV} not set (the "
            "Cluster_Env_File for the throwaway cluster)"
        )
    if not os.environ.get(_LIVE_BAO_ADDR_ENV) or not os.environ.get(_LIVE_BAO_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_LIVE_BAO_ADDR_ENV}/{_LIVE_BAO_TOKEN_ENV} "
            "not set (a live OpenBao read endpoint + token is required to drive "
            "the sealed/unsealed fail-closed assertion)"
        )
    if not os.environ.get(_LIVE_ADMIN_TOKEN_ENV):
        return (
            f"requires-infra, skipped: {_LIVE_ADMIN_TOKEN_ENV} not set (the "
            "platform-admin token the composed OpenBao onboarders authenticate "
            "with; the live stand-up fails closed without it)"
        )
    if not os.environ.get(_LIVE_WORKLOAD_URL_ENV):
        return (
            f"requires-infra, skipped: {_LIVE_WORKLOAD_URL_ENV} not set (the "
            "/healthz URL of the stood-up reference workload to probe)"
        )
    return None


def _hello_workload_mutate_gate_reason() -> str | None:
    """Skip reason for the live smoke that STANDS UP / SEALS real resources.

    Layered ON TOP of :func:`_hello_workload_live_gate_reason`: even with a
    wired cluster the mutating smoke stays skipped unless BOTH the
    `AGENT_LIVE_AUTHORIZED` interlock is armed AND `CONSUMER_TEST_ALLOW_MUTATE=1`
    — the agent live-run interlock (testing-strategy.md "Agent Live-Test
    Authorization") plus the per-suite safety interlock, so a read-only live run
    can never accidentally stand up / seal / tear down. NEVER runs in
    CI/acceptance/production; throwaway cluster only."""
    reason = _hello_workload_live_gate_reason()
    if reason is not None:
        return reason
    if os.environ.get(_LIVE_AGENT_AUTH_ENV) != "1":
        return (
            f"requires-infra, skipped: {_LIVE_AGENT_AUTH_ENV} interlock not armed "
            "(the hello-workload live smoke stands up + seals the Primary; it "
            "runs only on a throwaway cluster with the interlock armed)"
        )
    if os.environ.get(_LIVE_ALLOW_MUTATE_ENV) != "1":
        return (
            f"requires-infra, skipped: {_LIVE_ALLOW_MUTATE_ENV} != '1' (the "
            "per-suite safety interlock arming the stand-up/seal smoke)"
        )
    return None


@pytest.mark.requires_infra
class TestHelloWorkloadLiveSmoke:
    """The LIVE hello-workload smoke — the definition-of-done (Req 1.1-1.4, 1.6).

    DESELECTED by default (`pytest.ini` `addopts = -m "not requires_infra"`), so
    a bare offline `pytest tests/installer/test_hello_workload_smoke.py` never
    collects these — they report as DESELECTED, the offline structural tier
    above runs and passes. Under an explicit `-m requires_infra` opt-in on an
    UNWIRED machine they SKIP cleanly (never fail) via the layered gate above.

    The live body is deliberately MINIMAL/guarded — it asserts the acceptance
    the task-5.2 install-runbook walkthrough drives (happy path healthy +
    fail-closed), reading everything from the env-var contract (by NAME only,
    no secret value echoed). It is NOT part of the offline gate.

    Run (opt-in, throwaway cluster only, interlock armed):
      AGENT_LIVE_AUTHORIZED=1 CONSUMER_TEST_ALLOW_MUTATE=1 \\
        ~/venv/devinfra/bin/pytest -m requires_infra \\
        tests/installer/test_hello_workload_smoke.py -v
    """

    @pytest.mark.skipif(
        _hello_workload_live_gate_reason() is not None,
        reason=_hello_workload_live_gate_reason() or "live hello-workload smoke wired",
    )
    def test_hello_workload_healthy_happy_path(self):
        """Req 1.1/1.2/1.4: against a reachable, UNSEALED Primary the reference
        workload becomes healthy having read `DATABASE_URL` from the
        agent-rendered RSF, and the smoke reports the single
        "hello-workload healthy" definition-of-done signal.

        The read-only happy-path probe: GET the workload's /healthz and assert
        200 (healthy-iff-secret-present — the task-4.1 CGI contract). The
        stand-up that produced it is the task-5.2 runbook walkthrough."""
        import urllib.request

        url = os.environ[_LIVE_WORKLOAD_URL_ENV]
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (throwaway URL from env)
            status = resp.getcode()
        assert status == 200, (
            "the hello-workload /healthz must return 200 (healthy) once the "
            f"agent-rendered DATABASE_URL is present; got {status}. This is the "
            "single 'hello-workload healthy' definition-of-done signal (Req 1.4)."
        )

    @pytest.mark.skipif(
        _hello_workload_mutate_gate_reason() is not None,
        reason=_hello_workload_mutate_gate_reason() or "live fail-closed smoke armed",
    )
    def test_hello_workload_fail_closed_when_primary_sealed(self):
        """Req 1.3 (fail-closed swap-gate, Property 2): with the OpenBao Primary
        SEALED the Agent never renders the RSF, the sidecar never becomes
        healthy, and the workload never starts — /healthz is NOT 200.

        This is a MUTATING smoke (it seals the Primary and recreates the stack),
        so it is gated on the AGENT_LIVE_AUTHORIZED interlock + the per-suite
        mutate flag ON TOP of the live gate — throwaway cluster only. The seal /
        stand-up itself is driven by the task-5.2 runbook walkthrough via the
        dependency's OWN svc-07-bootstrap automated path (never ad-hoc docker
        surgery); this test asserts the OBSERVABLE fail-closed outcome.
        """
        import urllib.error
        import urllib.request

        url = os.environ[_LIVE_WORKLOAD_URL_ENV]
        healthy = False
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
                healthy = resp.getcode() == 200
        except (urllib.error.URLError, OSError):
            healthy = False
        assert not healthy, (
            "with the Primary SEALED the hello-workload must NOT be healthy — no "
            "RSF ⇒ sidecar never service_healthy ⇒ workload never starts "
            "(fail-closed swap-gate, Req 1.3). /healthz returned 200 while sealed."
        )


# =========================================================================== #
# TASK 5.1 — doctest-tier assertion over the hello-workload documentation.
#
# Req 1.5 / documentation-testing.md: the hello-workload documentation's
# runnable commands must carry the CORRECT doctest tiers — offline preflight
# (the `--help` / offline pytest invocation / shellcheck / `docker compose
# config` / `terraform validate`) carries `offline`; the live provisioning /
# smoke commands carry `requires-infra`. This asserts the tiers actually present
# in the `hello_workload` role README (task 4.1 added the annotations) are
# correct, and that no runnable fenced block is left UNANNOTATED (which would
# silently default to display-only and hide drift — the explicit
# documentation-testing.md anti-pattern).
#
# Reuses the SAME tier-parsing convention as the existing repo doctest-tier test
# (`infra/tests/test_accessible_root_readme_links.py`): the `<!-- doctest: TIER -->`
# annotation sits on the nearest preceding non-blank line (a single `<!-- cwd: -->`
# line may sit between the annotation and the fence). Mermaid is a diagram, not
# runnable, and is excluded.
# =========================================================================== #

_HELLO_README = _ROLES / "hello_workload" / "README.md"

_DOCTEST_RE = re.compile(r"<!--\s*doctest:\s*(offline|requires-infra|display-only)\b")
_CWD_RE = re.compile(r"<!--\s*cwd:")
#: Fence languages that are DIAGRAMS / non-runnable — excluded from the tier
#: requirement (mirrors the sibling doctest-tier test's `_NON_RUNNABLE_LANGS`).
_NON_RUNNABLE_LANGS = {"mermaid", "", "text", "json", "yaml", "yml", "hcl"}
#: Fence languages that ARE runnable shell commands subject to the tier rule.
_RUNNABLE_LANGS = {"bash", "sh", "shell", "console"}


def _resolve_doctest_tier(lines: list[str], open_idx: int) -> str | None:
    """Return the doctest tier governing the fence at ``open_idx``, or ``None``.

    Walk upward over at most a short prelude of ``cwd`` comment lines (no blank
    line permitted) until a ``doctest`` annotation is found — identical
    governance to the repo's existing doctest-tier parser."""
    k = open_idx - 1
    while k >= 0:
        line = lines[k].strip()
        hit = _DOCTEST_RE.search(line)
        if hit:
            return hit.group(1)
        if _CWD_RE.search(line):
            k -= 1
            continue
        return None
    return None


def _runnable_blocks_with_tiers(md_path: Path) -> list[tuple[int, str, str | None, str]]:
    """Parse ``md_path`` → list of (1-based open-fence line, lang, tier, body)
    for every RUNNABLE fenced block (shell languages)."""
    lines = md_path.read_text(encoding="utf-8").splitlines()
    out: list[tuple[int, str, str | None, str]] = []
    i, n = 0, len(lines)
    while i < n:
        m = re.match(r"^(```+|~~~+)\s*([A-Za-z0-9_+-]*)\s*$", lines[i].strip())
        if not m:
            i += 1
            continue
        marker = m.group(1)[:3]
        lang = m.group(2).lower()
        open_idx = i
        j = i + 1
        body: list[str] = []
        while j < n and not lines[j].strip().startswith(marker):
            body.append(lines[j])
            j += 1
        if lang in _RUNNABLE_LANGS:
            out.append(
                (open_idx + 1, lang, _resolve_doctest_tier(lines, open_idx), "\n".join(body))
            )
        i = j + 1
    return out


def test_hello_workload_readme_exists_with_annotated_runnable_blocks():
    """Req 1.5: the hello-workload doc (the role README task 4.1 authored) has
    at least one runnable block and every runnable block carries a doctest-tier
    annotation — no unannotated block silently defaulting to display-only."""
    assert _HELLO_README.is_file(), f"missing hello-workload README: {_HELLO_README}"
    blocks = _runnable_blocks_with_tiers(_HELLO_README)
    assert blocks, (
        "the hello-workload README must contain at least one runnable (bash/sh) "
        "fenced block (the offline preflight commands)"
    )
    unannotated = [(ln, lang) for (ln, lang, tier, _body) in blocks if tier is None]
    assert not unannotated, (
        "every runnable fenced block in the hello-workload README must carry a "
        f"<!-- doctest: TIER --> annotation (unannotated blocks at lines "
        f"{unannotated!r} would silently default to display-only and hide drift "
        "— documentation-testing.md)."
    )


def test_hello_workload_readme_preflight_blocks_are_offline_tier():
    """Req 1.5: the hello-workload README's runnable commands are OFFLINE
    PREFLIGHT (the offline pytest invocation + the shellcheck lint) — each MUST
    carry the `offline` tier, never `requires-infra` (they need no live infra).
    The live provisioning/smoke commands are the task-5.2 install-runbook
    walkthrough (`requires-infra`), NOT this README."""
    blocks = _runnable_blocks_with_tiers(_HELLO_README)
    for (line, lang, tier, body) in blocks:
        assert tier == "offline", (
            f"hello-workload README runnable block at line {line} (lang={lang}) "
            f"is an offline preflight command and MUST carry `offline`; got "
            f"{tier!r}. Body:\n{body}"
        )
    # Belt-and-braces: the known offline preflight commands are present and
    # tiered offline — the offline pytest invocation and the entrypoint lint.
    offline_bodies = "\n".join(b for (_ln, _lg, t, b) in blocks if t == "offline")
    assert "pytest" in offline_bodies, (
        "the hello-workload README must document the offline pytest invocation "
        "(the entrypoint structural check) under the `offline` tier"
    )


def test_hello_workload_readme_has_no_requires_infra_runnable_block():
    """Req 1.5 / tier-correctness: the hello-workload ROLE README is the offline
    preflight home — the `requires-infra` live walkthrough lives in the install
    runbook (task 5.2), NOT here. Assert this README declares no
    `requires-infra` runnable block (which would misplace the live smoke)."""
    blocks = _runnable_blocks_with_tiers(_HELLO_README)
    misplaced = [(ln, body) for (ln, _lg, t, body) in blocks if t == "requires-infra"]
    assert not misplaced, (
        "the hello-workload role README must carry only `offline` preflight "
        "commands; the `requires-infra` live provisioning/smoke walkthrough "
        f"belongs in the install runbook (task 5.2). Misplaced: "
        f"{[ln for (ln, _b) in misplaced]!r}"
    )
