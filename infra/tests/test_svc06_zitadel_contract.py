"""Offline contract tests for the SVC-06 zitadel-server role.

Slice-3 Task 15 (spec: svc-06-identity-provider) — design.md §3.2 /
requirements.md FR-1/FR-6, §8, §12, §13. OFFLINE config-validation of the
Ansible CONFIGURATION layer: the `zitadel-server` role (defaults + tasks +
secret-env template), parsed with PyYAML / inspected as text. No infra — always
runs.

What is asserted (the SVC-06 ZITADEL contract):
  * HTTP ingress via Traefik labels ONLY — no published host `ports:` for HTTP
    (integration-boundaries §1 / FR-1); attaches the `edge` network.
  * The ZITADEL_MASTERKEY + backing-DB password come from OpenBao (`bao` CLI) and
    live in a 0600 secret env layered via `extra_env_files` — NEVER the non-secret
    docker-compose-app `.env` (secret hygiene, §8). Secret-handling tasks no_log.
  * The masterkey is generated-ONCE (read-from-OpenBao-first; persisted only when
    absent) — never regenerated on a converged run (§6, non-rotatable).
  * EXTERNAL* / TLS settings present (match the Traefik endpoint — §6/§12).
  * Prometheus metrics enabled (§13).
  * Image pinned (never `latest`), backing a PostgreSQL 14-18 engine (§12).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc06_zitadel_contract.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "zitadel-server"
_TASKS = _ROLE / "tasks" / "main.yml"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_SECRET_ENV = _ROLE / "templates" / "zitadel.secret.env.j2"


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _task_text(t: dict) -> str:
    return str(t).lower()


def _defaults() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- #
# Image pin (tech.md: never latest).
# --------------------------------------------------------------------------- #
def test_image_pinned_not_latest():
    img = str(_defaults().get("zitadel_image", ""))
    assert img.startswith("ghcr.io/zitadel/zitadel:"), "ZITADEL image must be the official ghcr image"
    tag = img.rsplit(":", 1)[-1]
    assert tag and tag != "latest", f"ZITADEL image must be pinned to a specific tag, not 'latest' (got {tag!r})"


# --------------------------------------------------------------------------- #
# Traefik-only HTTP ingress — no host port (FR-1 / integration-boundaries §1).
# --------------------------------------------------------------------------- #
def test_http_ingress_via_traefik_labels_no_host_port():
    text = _TASKS.read_text(encoding="utf-8")
    assert "traefik.enable=true" in text, "ZITADEL must register with Traefik via Docker labels (FR-1)"
    assert "traefik.http.routers.zitadel.rule=host(" in text.lower() or "routers.{{ zitadel_traefik_router }}.rule=host(" in text.lower(), (
        "ZITADEL must declare a Traefik Host router rule"
    )
    # The ZITADEL service dict must NOT publish an HTTP host port — Traefik is the
    # only ingress path. (A `ports:` on the zitadel service would be the violation.)
    tasks = _load_tasks(_TASKS)
    for t in tasks:
        if "ansible.builtin.include_role" in t:
            spec = t["ansible.builtin.include_role"]
            if isinstance(spec, dict) and spec.get("name") == "docker-compose-app":
                services = t.get("vars", {}).get("dca_services", [])
                for svc in services:
                    if isinstance(svc, dict) and svc.get("name") == "zitadel":
                        assert "ports" not in svc, (
                            "the ZITADEL compose service must NOT publish a host port — HTTP ingress is via "
                            "Traefik labels only (integration-boundaries §1, FR-1)"
                        )
                        nets = str(svc.get("networks", []))
                        assert ("edge" in nets or "zitadel_traefik_network" in nets), (
                            "ZITADEL must attach the edge network (literal or via zitadel_traefik_network)"
                        )


# --------------------------------------------------------------------------- #
# Secrets via OpenBao + 0600 extra_env_files, never the non-secret .env (§8).
# --------------------------------------------------------------------------- #
def test_masterkey_and_db_password_from_openbao_not_env():
    text = _TASKS.read_text(encoding="utf-8")
    # The masterkey + DB password are read/written via the `bao` CLI docker-exec.
    assert "bao" in text and "docker" in text and "exec" in text, (
        "ZITADEL secrets must be read/written via the `bao` CLI docker-exec pattern (OpenBao, SVC-07)"
    )
    assert "zitadel_openbao_secret_path" in text, "masterkey must be stored under the OpenBao secret path"
    # The non-secret dca_env must NOT carry the masterkey or a DB password.
    tasks = _load_tasks(_TASKS)
    for t in tasks:
        if "ansible.builtin.include_role" in t:
            spec = t["ansible.builtin.include_role"]
            if isinstance(spec, dict) and spec.get("name") == "docker-compose-app":
                dca_env = str(t.get("vars", {}).get("dca_env", {})).lower()
                assert "masterkey" not in dca_env, "dca_env (non-secret .env) must NOT carry the masterkey (§8)"
                assert "password" not in dca_env, "dca_env (non-secret .env) must NOT carry a DB password (§8)"
                # The secret env is layered via extra_env_files on the zitadel service.
                services = t.get("vars", {}).get("dca_services", [])
                zsvc = next((s for s in services if isinstance(s, dict) and s.get("name") == "zitadel"), {})
                assert "extra_env_files" in zsvc, (
                    "the ZITADEL service must layer the 0600 secret env via extra_env_files"
                )


def test_secret_env_is_0600_and_carries_masterkey_and_db_password():
    text = _TASKS.read_text(encoding="utf-8")
    assert "zitadel.secret.env" in text and '"0600"' in text, (
        "the masterkey + DB password must be written to a 0600 runtime secret env on the guest (§8)"
    )
    tmpl = _SECRET_ENV.read_text(encoding="utf-8")
    assert "ZITADEL_MASTERKEY=" in tmpl, "the secret env template must set ZITADEL_MASTERKEY"
    assert "ZITADEL_DATABASE_POSTGRES_USER_PASSWORD=" in tmpl, (
        "the secret env template must set the backing-DB user password"
    )


def test_secret_handling_tasks_are_no_log():
    tasks = _load_tasks(_TASKS)
    for t in tasks:
        body = _task_text(t)
        if any(m in body for m in ("masterkey", "password", "bao_token")):
            # A pure DIAGNOSTIC task (ansible.builtin.fail / assert) that only
            # NAMES a secret-related concept (a KV path/field or var name) in its
            # message but carries NO secret VALUE is exempt — forcing no_log on a
            # `fail`/`assert` would suppress the very diagnostic it exists to emit
            # (e.g. the fail-fast "OpenBao read empty while wired" guard, whose
            # message names only the non-secret KV path+field). Spec:
            # fix-svc06-zitadel-db-app-password-mismatch.
            is_pure_diagnostic = (
                any(k in t for k in ("ansible.builtin.fail", "fail",
                                     "ansible.builtin.assert", "assert"))
                and "set_fact" not in t
                and "ansible.builtin.command" not in t
                and "ansible.builtin.shell" not in t
                and "ansible.builtin.template" not in t
                and "ansible.builtin.copy" not in t
            )
            if is_pure_diagnostic:
                continue
            # read/write/generate/bind/template tasks that touch secret material.
            if ("set_fact" in body or "bao" in body or "secret.env" in body or "kv" in body):
                assert t.get("no_log") is True, (
                    f"secret-handling task must be no_log: true: {t.get('name')!r}"
                )


# --------------------------------------------------------------------------- #
# Masterkey generated ONCE (read-first; persist only when absent) — §6.
# --------------------------------------------------------------------------- #
def test_masterkey_is_generated_once_not_regenerated():
    text = _TASKS.read_text(encoding="utf-8")
    # There must be a READ of the existing masterkey before generation, and the
    # persist step must be gated on the masterkey being absent (check-then-write).
    assert "read any existing masterkey" in text.lower(), (
        "the role must READ any existing masterkey from OpenBao before generating (never blind-regenerate — §6)"
    )
    assert "when newly generated" in text.lower() or "rc | default(1)) != 0" in text, (
        "the masterkey persist step must be gated on absence (check-then-write), so a converged run does not "
        "regenerate it (§6 non-rotatable)"
    )


# --------------------------------------------------------------------------- #
# External endpoint / TLS + metrics (§6/§12/§13).
# --------------------------------------------------------------------------- #
def test_external_endpoint_and_tls_settings_present():
    text = _TASKS.read_text(encoding="utf-8")
    for key in ("ZITADEL_EXTERNALDOMAIN", "ZITADEL_EXTERNALPORT", "ZITADEL_EXTERNALSECURE"):
        assert key in text, f"ZITADEL config must set {key} to match the Traefik endpoint (§6/§12)"
    d = _defaults()
    assert d.get("zitadel_external_secure") is True, "zitadel_external_secure must default true (HTTPS endpoint)"


def test_metrics_enabled():
    text = _TASKS.read_text(encoding="utf-8")
    assert "ZITADEL_METRICS_ENABLED" in text, "ZITADEL must expose Prometheus metrics (§13)"
    assert _defaults().get("zitadel_metrics_enabled") is True, "zitadel_metrics_enabled must default true (§13)"


# --------------------------------------------------------------------------- #
# Backing DB wiring points at the SVC-01 shared-zitadel instance (§3.3 / §12).
# --------------------------------------------------------------------------- #
def test_backing_db_env_points_at_svc01_instance():
    text = _TASKS.read_text(encoding="utf-8")
    for key in ("ZITADEL_DATABASE_POSTGRES_HOST", "ZITADEL_DATABASE_POSTGRES_DATABASE",
                "ZITADEL_DATABASE_POSTGRES_USER_USERNAME"):
        assert key in text, f"ZITADEL must configure {key} to reach its backing SVC-01 Postgres (§12)"
    d = _defaults()
    assert d.get("zitadel_db_slug") == "shared-zitadel", (
        "the backing DB slug must be shared-zitadel (the SVC-01 instance SVC-06 provisions)"
    )
