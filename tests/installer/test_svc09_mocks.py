"""Offline drift-guards for the SVC-09 (Traefik) mock stand-ins.

Slice-1 Task 4 (spec: svc-09-reverse-proxy) — pins the two permanent, default-off
Traefik mock stand-ins per `.kiro/steering/mock-stand-ins.md`:

  * Mock 3 — ``traefik_tls_self_signed`` (stand-in for SVC-08/EJBCA + Let's
    Encrypt): default false, gated, idempotent (stat-the-target skip so a present
    cert fires no restart churn), control-node ``community.crypto`` generation.
  * Mock 4 — ``traefik_dashboard_auth_disabled`` (stand-in for SVC-06/ZITADEL
    forward-auth): default false, dashboard never anonymous ``:8080``, fail-closed
    at render when the mock is off AND no forward-auth address is configured.

Also asserts the catalogue rows for both mocks exist in `mock-stand-ins.md`, and
that SVC-07's existing mocks (Mock 1 self-signed cert, Mock 2 placeholder edge
network) are PRESERVED unchanged (the plan's non-negotiable rule).

Pure-structure, offline: PyYAML parse of the role defaults + role tasks, and a
text scan of the steering catalogue. No infra, no terraform binary.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc09_mocks.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "traefik_install"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_TASKS_MAIN = _ROLE_DIR / "tasks" / "main.yml"
_TLS_TASKS = _ROLE_DIR / "tasks" / "tls_self_signed.yml"
_CATALOGUE = _REPO_ROOT / ".kiro" / "steering" / "mock-stand-ins.md"


# --------------------------------------------------------------------------- #
# Task-dict helpers (local copies; conftest-collision rule — no cross-tree import).
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_task_file(path: Path) -> list[dict]:
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _defaults() -> dict:
    raw = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw, "traefik_install defaults must be a non-empty mapping"
    return raw


# --------------------------------------------------------------------------- #
# Mock flags default OFF.
# --------------------------------------------------------------------------- #
def test_both_mock_flags_default_false():
    d = _defaults()
    for flag in ("traefik_tls_self_signed", "traefik_dashboard_auth_disabled"):
        assert flag in d, f"{flag} must be declared in traefik_install defaults/main.yml"
        assert d[flag] is False, (
            f"{flag} must default to false — mocks are opt-in dev stand-ins, never the "
            f"production default (mock-stand-ins.md)"
        )


# --------------------------------------------------------------------------- #
# Mock 3 — self-signed TLS cert is gated + idempotent.
# --------------------------------------------------------------------------- #
def test_tls_self_signed_include_is_gated_on_the_flag():
    tasks = _load_task_file(_TASKS_MAIN)
    includes = [
        t for t in tasks
        if str(t.get("ansible.builtin.include_tasks", t.get("include_tasks", ""))).endswith("tls_self_signed.yml")
    ]
    assert includes, "tasks/main.yml must include tls_self_signed.yml (the Mock 3 generator)"
    for inc in includes:
        when = str(inc.get("when", ""))
        assert "traefik_tls_self_signed" in when, (
            "the tls_self_signed.yml include must be gated `when: traefik_tls_self_signed | bool` "
            "so the mock is opt-in (default off)"
        )


def test_tls_self_signed_tasks_file_exists_and_is_control_node_generated():
    assert _TLS_TASKS.is_file(), "tasks/tls_self_signed.yml must exist (the Mock 3 generator)"
    tasks = _load_task_file(_TLS_TASKS)
    mods = set()
    for t in tasks:
        mods |= set(t.keys())
    # Generation uses community.crypto on the control node (delegate_to: localhost).
    assert any("community.crypto" in m for m in mods), (
        "Mock 3 must generate the cert with community.crypto (control-node generation, like SVC-07)"
    )
    delegated = [t for t in tasks if str(t.get("delegate_to", "")) == "localhost"]
    assert delegated, (
        "Mock 3 generation tasks must run on the control node (delegate_to: localhost) — the "
        "minimal Traefik LXC lacks the Python cryptography lib"
    )


def test_tls_self_signed_is_idempotent_stat_skip():
    """Idempotency: the generate+copy block is skipped when a cert already
    exists (stat the target, set a `_generate` fact, gate on it) so a present
    cert fires no `Restart traefik` churn — exactly SVC-07's Mock 1 pattern."""
    tasks = _load_task_file(_TLS_TASKS)
    has_stat = any(("ansible.builtin.stat" in t or "stat" in t) for t in tasks)
    assert has_stat, "Mock 3 must stat the target cert to decide whether to (re)generate (idempotency)"
    # A gating fact driving the generate/copy tasks.
    gate_facts = [
        t for t in tasks
        if ("ansible.builtin.set_fact" in t or "set_fact" in t)
    ]
    assert gate_facts, "Mock 3 must set a `_generate` fact from the stat result (idempotency gate)"
    text = _TLS_TASKS.read_text(encoding="utf-8")
    assert "traefik_mock_tls_generate" in text, (
        "Mock 3 must gate generate+copy on a `traefik_mock_tls_generate` fact so a present cert is "
        "left untouched (no copy, no restart churn)"
    )


def test_tls_self_signed_key_tasks_no_log():
    """The private-key-handling tasks must set no_log: true (secret hygiene)."""
    tasks = _load_task_file(_TLS_TASKS)
    key_tasks = [
        t for t in tasks
        if "key.pem" in str(t).lower() and (
            "community.crypto.openssl_privatekey" in t
            or "ansible.builtin.copy" in t
            or "copy" in t
        )
    ]
    # At least the privatekey generation + the key copy must be no_log.
    no_log_key_tasks = [t for t in key_tasks if t.get("no_log") is True]
    assert no_log_key_tasks, (
        "the private-key generation/copy tasks in Mock 3 must set no_log: true (secret hygiene)"
    )


# --------------------------------------------------------------------------- #
# Mock 4 — dashboard-auth disabled: never anonymous, fail-closed when off.
# --------------------------------------------------------------------------- #
def test_dashboard_fails_closed_when_mock_off_and_no_forward_auth():
    """With the mock OFF and no forward-auth address, the role must fail closed
    (an assert) rather than serve the dashboard unauthenticated."""
    tasks = _load_task_file(_TASKS_MAIN)
    asserts = [t for t in tasks if ("ansible.builtin.assert" in t or "assert" in t)]
    fail_closed = []
    for t in asserts:
        when = str(t.get("when", ""))
        body = str(t).lower()
        if "traefik_dashboard_auth_disabled" in when and (
            "forward_auth" in body or "forwardauth" in body or "unauthenticated" in body
        ):
            fail_closed.append(t)
    assert fail_closed, (
        "tasks/main.yml must fail closed (assert) when the dashboard-auth mock is OFF and no "
        "forward-auth address is set — never a silently anonymous dashboard (FR-6/FR-10, Mock 4)"
    )


def test_dashboard_never_served_insecure_8080():
    """api.insecure must be false in the static config in BOTH mock postures —
    the dashboard is never on the raw :8080 host port."""
    static = (_ROLE_DIR / "templates" / "traefik.yml.j2").read_text(encoding="utf-8")
    assert "insecure: false" in static, (
        "traefik.yml must set api.insecure: false — the dashboard is never served on raw :8080 "
        "regardless of the dashboard-auth mock (FR-6)"
    )


# --------------------------------------------------------------------------- #
# Catalogue rows present in mock-stand-ins.md.
# --------------------------------------------------------------------------- #
def test_catalogue_has_both_new_mock_rows():
    text = _CATALOGUE.read_text(encoding="utf-8")
    assert "Mock 3 — self-signed TLS cert for Traefik" in text, (
        "mock-stand-ins.md must carry a Mock 3 catalogue row for traefik_tls_self_signed"
    )
    assert "Mock 4 — Traefik dashboard forward-auth disabled" in text, (
        "mock-stand-ins.md must carry a Mock 4 catalogue row for traefik_dashboard_auth_disabled"
    )
    for flag in ("traefik_tls_self_signed", "traefik_dashboard_auth_disabled"):
        assert flag in text, f"mock-stand-ins.md must name the {flag} flag in its catalogue"


def test_svc07_mocks_preserved_unchanged():
    """Non-negotiable: SVC-07's existing mocks must remain catalogued (never removed)."""
    text = _CATALOGUE.read_text(encoding="utf-8")
    assert "Mock 1 — self-signed TLS cert (stand-in for SVC-08 / EJBCA)" in text, (
        "SVC-07 Mock 1 (openbao_tls_self_signed) must remain in the catalogue — mocks are permanent"
    )
    assert "Mock 2 — placeholder `edge` network (stand-in for SVC-09 / Traefik)" in text, (
        "SVC-07 Mock 2 (openbao_mock_edge_network) must remain in the catalogue — mocks are permanent"
    )
    for flag in ("openbao_tls_self_signed", "openbao_mock_edge_network"):
        assert flag in text, f"SVC-07 mock flag {flag} must remain catalogued (never removed)"
