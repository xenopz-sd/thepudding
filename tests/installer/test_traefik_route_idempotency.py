"""Offline idempotency guard for the traefik_route Route_Mechanism.

Task 1.5 (spec: traefik-cross-guest-ingress) — requirements.md NFR1; design.md
§"Component 1 — Route_Mechanism" (behavior 3: "Idempotent by content — the
template module reports changed=0 when the rendered fragment is byte-identical")
and the Testing Strategy "Idempotency test".

NFR1 requires the Route_Mechanism to be idempotent: a second converged run
reports ``changed=0`` and does not duplicate or churn the Route_Fragment. The
mechanism achieves this two ways, and this test pins BOTH:

  1. CONTENT idempotency (render byte-identical): rendering
     ``ansible/roles/traefik_route/templates/traefik-route-fragment.yml.j2``
     twice with the SAME representative var set produces byte-for-byte identical
     output. Since ``ansible.builtin.template`` only reports ``changed`` when the
     rendered bytes differ from what is already on the target, a byte-stable
     render is exactly what makes a second converged run a no-op (changed=0).

  2. STRUCTURAL idempotency (content-idempotent module): ``tasks/main.yml`` writes
     the fragment with ``ansible.builtin.template`` — a content-idempotent module
     by nature — NOT a ``copy``-with-content / ``lineinfile`` / ``blockinfile`` /
     ``shell``/``command`` append that would churn or duplicate on re-run.

Pure-structure, offline: a Jinja render of the role template + a PyYAML parse of
the role task file — no infra, no terraform/ansible binary. Reuses the same
offline Jinja harness pattern as tests/installer/test_obs_mocks.py (``_make_env``
with the ``bool`` filter stand-in). No cross-tree import (the same-named conftest
collision rule forbids one; the harness is a small local copy, like every sibling
``*_mocks.py``/``test_obs_*`` test).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_traefik_route_idempotency.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "traefik_route"
_FRAGMENT_TMPL = _ROLE / "templates" / "traefik-route-fragment.yml.j2"
_TASKS = _ROLE / "tasks" / "main.yml"


# --------------------------------------------------------------------------- #
# Faithful offline Jinja harness (local copy of the test_obs_mocks.py harness;
# the four pytest roots run as SEPARATE invocations and the shared same-named
# conftest.py forbids a cross-tree import — mirrors every *_mocks.py sibling).
# --------------------------------------------------------------------------- #
def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["trim"] = lambda s: str(s).strip()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    return env


def _render(template_path: Path, variables: dict) -> str:
    assert template_path.is_file(), f"expected template at {template_path}"
    env = _make_env()
    tmpl = env.from_string(template_path.read_text(encoding="utf-8"))
    return tmpl.render(**variables)


def _representative_vars() -> dict:
    """The Grafana cross-guest route — the concrete live-confirmed case
    (design §"Interface — Route_Fragment schema", R4.2). Fully resolved (the
    consuming role supplies route_host_rule/route_backend_url already expanded),
    so the render depends on no unresolved Ansible magic vars."""
    return {
        "route_name": "grafana",
        "route_host_rule": "grafana.platform.example.test",
        "route_backend_url": "http://10.0.20.19:3000",
        "route_entrypoint": "websecure",
        "route_tls": True,
    }


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
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


# =========================================================================== #
# 1. CONTENT idempotency — two renders with unchanged inputs are byte-identical.
# =========================================================================== #
def test_second_render_is_byte_identical(tmp_path):
    """NFR1: a second render of the Route_Fragment with UNCHANGED inputs produces
    byte-identical output, so ansible.builtin.template would report changed=0
    (no duplicate / no churn)."""
    variables = _representative_vars()

    first = _render(_FRAGMENT_TMPL, variables)
    second = _render(_FRAGMENT_TMPL, variables)

    assert first == second, (
        "two renders of traefik-route-fragment.yml.j2 with the SAME inputs must be "
        "byte-for-byte identical — a non-deterministic render would make every "
        "converged run report changed (churn), violating NFR1"
    )
    # Byte-level check too (encodings / trailing bytes), not just str equality.
    assert first.encode("utf-8") == second.encode("utf-8"), (
        "the two renders must be identical at the byte level (NFR1 idempotency)"
    )

    # And the byte-stable output is actually the fragment we expect: writing it to
    # disk twice leaves an identical file (what template's changed-detection sees).
    dest = tmp_path / "grafana-route.yml"
    dest.write_text(first, encoding="utf-8")
    bytes_after_first = dest.read_bytes()
    dest.write_text(second, encoding="utf-8")
    bytes_after_second = dest.read_bytes()
    assert bytes_after_first == bytes_after_second, (
        "re-writing the rendered fragment with unchanged inputs must not change the "
        "file on disk (template changed=0 on the second converged run, NFR1)"
    )


def test_render_is_valid_single_router_single_service_yaml():
    """Sanity: the byte-stable render is well-formed Traefik v3 YAML with exactly
    one router + one service — so the idempotency guarantee is about a REAL,
    parseable fragment, not an accidentally-empty one."""
    rendered = _render(_FRAGMENT_TMPL, _representative_vars())
    doc = yaml.safe_load(rendered)

    routers = (doc.get("http") or {}).get("routers") or {}
    services = (doc.get("http") or {}).get("services") or {}
    assert list(routers.keys()) == ["grafana"], (
        f"the fragment must carry exactly one http.routers entry; got {list(routers)}"
    )
    assert list(services.keys()) == ["grafana"], (
        f"the fragment must carry exactly one http.services entry; got {list(services)}"
    )
    servers = services["grafana"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://10.0.20.19:3000"}], (
        "the service loadBalancer must point at the backend's VLAN-20 IP:port"
    )


# =========================================================================== #
# 2. STRUCTURAL idempotency — the fragment is written with a content-idempotent
#    module (ansible.builtin.template), never copy-content/lineinfile/shell churn.
# =========================================================================== #
_FRAGMENT_DEST_SUFFIX = "{{ route_name }}-route.yml"
_CHURN_MODULES = (
    "ansible.builtin.lineinfile",
    "lineinfile",
    "ansible.builtin.blockinfile",
    "blockinfile",
    "ansible.builtin.shell",
    "shell",
    "ansible.builtin.command",
    "command",
    "ansible.builtin.raw",
    "raw",
)


def test_fragment_written_with_content_idempotent_template_module():
    """tasks/main.yml must render the Route_Fragment with ansible.builtin.template
    (content-idempotent by nature — changed=0 on a byte-identical render), not a
    copy-with-content / lineinfile / blockinfile / shell append that would churn
    or duplicate the fragment on a second run (NFR1)."""
    tasks = _load_task_file(_TASKS)

    fragment_writers = [
        t
        for t in tasks
        if "ansible.builtin.template" in t
        and str(t["ansible.builtin.template"].get("dest", "")).endswith(
            _FRAGMENT_DEST_SUFFIX
        )
    ]
    assert len(fragment_writers) == 1, (
        "tasks/main.yml must write the Route_Fragment with exactly one "
        "ansible.builtin.template task whose dest ends with "
        f"'{_FRAGMENT_DEST_SUFFIX}'; found {len(fragment_writers)}"
    )

    writer = fragment_writers[0]
    spec = writer["ansible.builtin.template"]
    assert spec.get("src") == "traefik-route-fragment.yml.j2", (
        "the fragment writer must render traefik-route-fragment.yml.j2 (the "
        "content-idempotent template), got src=" + repr(spec.get("src"))
    )

    # No churn-prone module may target the fragment path — a copy with inline
    # `content:`, a lineinfile/blockinfile, or a shell/command append would each
    # re-churn or duplicate on re-run.
    for t in tasks:
        for mod in _CHURN_MODULES:
            if mod not in t:
                continue
            body = str(t[mod])
            assert "route" not in body or "-route.yml" not in body, (
                f"the Route_Fragment must not be written/appended via '{mod}' "
                "(not content-idempotent — would churn/duplicate on re-run, NFR1); "
                "use ansible.builtin.template"
            )

    # A copy module, if present at all, must NOT carry an inline `content:` that
    # re-renders the fragment (that would defeat template's changed-detection).
    for t in tasks:
        for mod in ("ansible.builtin.copy", "copy"):
            if mod not in t:
                continue
            cspec = t[mod] or {}
            dest = str(cspec.get("dest", ""))
            if dest.endswith(_FRAGMENT_DEST_SUFFIX):
                assert "content" not in cspec, (
                    "the Route_Fragment must not be written with copy+content "
                    "(use ansible.builtin.template for content idempotency, NFR1)"
                )
