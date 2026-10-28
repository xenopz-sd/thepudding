"""Offline scrape-target generation test for SVC-17 (file_sd from inventory).

Task 5.2 (spec: svc-17-18-observability) — requirements.md FR-8 (file_sd only,
generated-not-hand-edited), FR-11 (the V1_Scrape_Set with project/service
labels). design.md §3.4 ("scrape what exists" — a service not present yields no
target file) and §Testing Strategy ("test_obs_scrape_targets.py — scrape-target-
file generation from a fixture inventory yields the four built-service targets
with project/service labels; a missing service yields no file (degrade)").

OFFLINE unit test for the Scrape_Target_File generation implemented in task 5.1:

    ansible/roles/observability_stack/tasks/scrape_targets.yml
    ansible/roles/observability_stack/tasks/scrape_targets_index_role.yml
    ansible/roles/observability_stack/templates/targets/service-target.yaml.j2

driven by the role's ``defaults/main.yml`` ``observability_builtin_scrape_targets``
table + an injectable ``observability_inventory_index`` fixture.

=== HOW THE OFFLINE RENDER WORKS ===

Mirrors the sibling ``test_obs_prometheus_render.py`` / ``test_obs_loki_render.py``
harness EXACTLY: load the role ``defaults/main.yml`` with PyYAML, resolve the
shallow jinja self-references with a fixed-point pass, then render each template
with a stock ``jinja2.Environment`` that registers the same Ansible-filter
stand-ins (``to_json`` → ``json.dumps``, ``bool`` → truthy-string, ``lower``,
``basename``).

The template under test (``templates/targets/service-target.yaml.j2``) takes a
single ``target_spec`` dict — the per-service row ENRICHED with a ``hosts`` list
of ``{ip, project}``. In the running role that enrichment is step 2 of
``tasks/scrape_targets.yml`` (resolve each built-service row's hosts from
``observability_inventory_index``, dropping absent services — the degrade), and
the per-host project derivation (literal vs the ``__from_inventory__`` sentinel)
is the same step. This test reproduces that pure ``index + table -> render_targets``
transform in Python (``_resolve_render_targets``) so the template is driven with
exactly the dicts the task would hand it, then asserts on the parsed file_sd
YAML the template emits.

The fabricated fixture index places the four built services on VLAN 20:
  * SVC-01 postgres  — dedicated-per-project; TWO project guests (mlvideo,
    dronefleet) so the per-host ``project`` label derivation + multi-host render
    are exercised (service_role group ``postgres`` carries two hosts, each in a
    distinct project slug group).
  * SVC-06 zitadel   — shared; :8080 /debug/metrics.
  * SVC-07 openbao   — shared; :8200 https /v1/sys/metrics ?format=prometheus.
  * SVC-09 traefik   — shared; :8082 default path/scheme.

=== WHAT IS ASSERTED (task 5.2 contract — FR-8 / FR-11) ===

  * all four present services produce a ``service-<service>.yaml`` file whose
    file_sd target is ``ip:port`` with ``project``/``service`` labels (FR-11).
  * the reserved discovery meta-labels render where applicable:
      - openbao : ``__scheme__: https``, ``__metrics_path__: /v1/sys/metrics``,
        ``__param_format: prometheus``.
      - zitadel : ``__metrics_path__: /debug/metrics`` (no __scheme__).
      - postgres / traefik : NO reserved meta-labels (http default /metrics).
  * DEGRADE: dropping a service from the fixture index yields NO target file for
    it, and never an error (design §3.4 "scrape what exists").
  * MULTI-HOST: the dedicated-per-project postgres service renders one file_sd
    target group PER project guest, each carrying its own derived ``project``
    label (``__from_inventory__`` sentinel honoured per host).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_obs_scrape_targets.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

# --------------------------------------------------------------------------- #
# Locate the observability_stack role relative to this test file (same anchor
# as the sibling render tests).
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "observability_stack"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_TARGET_TMPL = _ROLE_DIR / "templates" / "targets" / "service-target.yaml.j2"

#: The sentinel a built-service row uses to say "derive the project label per
#: host from the inventory index" (SVC-01 dedicated-per-project). Must match the
#: value in defaults/main.yml's observability_builtin_scrape_targets table.
_FROM_INVENTORY = "__from_inventory__"


# --------------------------------------------------------------------------- #
# Harness — identical to test_obs_prometheus_render.py / test_obs_loki_render.py.
# --------------------------------------------------------------------------- #
def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    env.filters["to_json"] = lambda v, **kw: json.dumps(v)
    return env


def _resolve_self_references(raw: dict) -> dict:
    env = _make_env()
    resolved = dict(raw)

    def _render_scalars(d: dict) -> tuple[dict, bool]:
        changed = False
        out = {}
        for key, val in d.items():
            if isinstance(val, str) and "{{" in val:
                try:
                    rendered = env.from_string(val).render(**resolved)
                except Exception:
                    rendered = val
                if rendered != val:
                    changed = True
                out[key] = rendered
            elif isinstance(val, list):
                new_list = []
                for item in val:
                    if isinstance(item, str) and "{{" in item:
                        try:
                            r = env.from_string(item).render(**resolved)
                        except Exception:
                            r = item
                        if r != item:
                            changed = True
                        new_list.append(r)
                    else:
                        new_list.append(item)
                out[key] = new_list
            else:
                out[key] = val
        return out, changed

    for _ in range(10):
        resolved, changed = _render_scalars(resolved)
        if not changed:
            break
    return resolved


@pytest.fixture(scope="module")
def role_vars() -> dict:
    assert _DEFAULTS.is_file(), f"expected role defaults at {_DEFAULTS}"
    raw = yaml.safe_load(_DEFAULTS.read_text())
    assert isinstance(raw, dict) and raw, "defaults/main.yml must be a non-empty mapping"
    return _resolve_self_references(raw)


@pytest.fixture(scope="module")
def builtin_targets(role_vars) -> list:
    """The authoritative V1 built-service table from defaults (FR-11)."""
    table = role_vars["observability_builtin_scrape_targets"]
    assert isinstance(table, list) and table, (
        "observability_builtin_scrape_targets must be a non-empty list in defaults"
    )
    services = {row["service"] for row in table}
    assert {"postgres", "zitadel", "openbao", "traefik"} <= services, (
        "the V1 built-service table must cover SVC-01/06/07/09 (FR-11); "
        f"found {services}"
    )
    return table


# --------------------------------------------------------------------------- #
# Fixture inventory index — the four built services present on VLAN 20.
# -----------------------------------------------------------------------------
# Shape matches what tasks/scrape_targets.yml assembles from the live inventory:
#   { "<service_role>": { "<hostname>": { "ip": ..., "project": ... } } }
# postgres is dedicated-per-project → two guests, each in a distinct project.
# The shared services carry project "shared" in the index (their built-service
# row also pins the literal "shared", so either way the label is "shared").
# --------------------------------------------------------------------------- #
def _fixture_inventory_index() -> dict:
    return {
        "postgres": {
            "svc01-100-01": {"ip": "10.0.100.11", "project": "mlvideo"},
            "svc01-101-01": {"ip": "10.0.101.11", "project": "dronefleet"},
        },
        "zitadel": {
            "svc06-20-01": {"ip": "10.0.20.14", "project": "shared"},
        },
        "openbao": {
            "svc07-20-01": {"ip": "10.0.20.10", "project": "shared"},
        },
        "traefik": {
            "svc09-20-01": {"ip": "10.0.20.12", "project": "shared"},
        },
    }


def _resolve_render_targets(builtin_table: list, index: dict) -> list:
    """Reproduce step 2 of tasks/scrape_targets.yml: enrich each built-service
    row with its present hosts, dropping absent/empty service_roles (degrade).

    This is the pure ``index + table -> render_targets`` transform the task
    performs before handing each row to the template as ``target_spec``.
    """
    render_targets = []
    for row in builtin_table:
        role_index = index.get(row["service_role"], {})
        if len(role_index) == 0:
            # DEGRADE: absent/empty service_role group → no file emitted.
            continue
        hosts = []
        for entry in role_index.values():
            if row["project"] == _FROM_INVENTORY:
                project = entry["project"]  # per-host derived (dedicated svc)
            else:
                project = row["project"]  # literal override (shared svc)
            hosts.append({"ip": entry["ip"], "project": project})
        render_targets.append(dict(row, hosts=hosts))
    return render_targets


def _render_target_file(target_spec: dict) -> list:
    """Render service-target.yaml.j2 for one enriched row; return the parsed
    file_sd list of target groups.
    """
    assert _TARGET_TMPL.is_file(), f"expected template at {_TARGET_TMPL}"
    env = _make_env()
    tmpl = env.from_string(_TARGET_TMPL.read_text())
    rendered = tmpl.render(target_spec=target_spec)
    doc = yaml.safe_load(rendered)
    assert isinstance(doc, list), (
        "a file_sd target file must parse as a YAML list of target groups; "
        f"got {type(doc).__name__}"
    )
    return doc


def _render_present(builtin_table: list, index: dict) -> dict:
    """Return { service -> parsed file_sd list } for every PRESENT service."""
    out = {}
    for row in _resolve_render_targets(builtin_table, index):
        out[row["service"]] = _render_target_file(row)
    return out


# --------------------------------------------------------------------------- #
# All four built services present → one file each with correct targets + labels.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def rendered(builtin_targets) -> dict:
    """{ service -> parsed file_sd list } for the full four-service fixture."""
    return _render_present(builtin_targets, _fixture_inventory_index())


class TestFourBuiltServicesRender:
    def test_all_four_services_emit_a_file(self, rendered):
        assert set(rendered) == {"postgres", "zitadel", "openbao", "traefik"}, (
            "all four present built services must emit a target file (FR-11); "
            f"got {sorted(rendered)}"
        )

    def test_each_target_group_has_project_and_service_labels(self, rendered):
        """FR-11: every file_sd target group carries project + service labels."""
        for service, groups in rendered.items():
            assert groups, f"{service} must have >= 1 target group"
            for g in groups:
                labels = g.get("labels", {})
                assert labels.get("service") == service, (
                    f"{service} target group must label service={service}; got {labels}"
                )
                assert labels.get("project"), (
                    f"{service} target group must carry a non-empty project label; got {labels}"
                )

    def test_targets_are_ip_colon_port(self, rendered, builtin_targets):
        """Each target is the guest VLAN-20 ip:port from the table + index."""
        ports = {row["service"]: row["port"] for row in builtin_targets}
        index = _fixture_inventory_index()
        for service, groups in rendered.items():
            expected_port = ports[service]
            role = next(r["service_role"] for r in builtin_targets if r["service"] == service)
            expected_ips = {e["ip"] for e in index[role].values()}
            rendered_targets = [t for g in groups for t in g["targets"]]
            # Every rendered target is <an expected ip>:<the service port>.
            for t in rendered_targets:
                ip, _, port = str(t).rpartition(":")
                assert ip in expected_ips, (
                    f"{service} target {t!r} ip must be one of {expected_ips}"
                )
                assert port == str(expected_port), (
                    f"{service} target {t!r} must use port {expected_port}"
                )

    def test_openbao_reserved_meta_labels(self, rendered):
        """SVC-07 OpenBao: https + /v1/sys/metrics + ?format=prometheus carried
        as the reserved discovery labels on the single shared guest."""
        groups = rendered["openbao"]
        assert len(groups) == 1, "openbao is shared → exactly one target group"
        labels = groups[0]["labels"]
        assert labels.get("__scheme__") == "https", (
            "OpenBao must scrape over HTTPS via the reserved __scheme__ label"
        )
        assert labels.get("__metrics_path__") == "/v1/sys/metrics", (
            "OpenBao must scrape /v1/sys/metrics via __metrics_path__"
        )
        assert labels.get("__param_format") == "prometheus", (
            "OpenBao must pass ?format=prometheus via __param_format"
        )
        assert labels.get("project") == "shared"
        assert labels.get("service") == "openbao"

    def test_zitadel_reserved_metrics_path_only(self, rendered):
        """SVC-06 ZITADEL: non-default /debug/metrics path, but default http
        scheme (no __scheme__) and no params."""
        groups = rendered["zitadel"]
        assert len(groups) == 1, "zitadel is shared → exactly one target group"
        labels = groups[0]["labels"]
        assert labels.get("__metrics_path__") == "/debug/metrics", (
            "ZITADEL must scrape /debug/metrics via the reserved __metrics_path__ label"
        )
        assert "__scheme__" not in labels, (
            "ZITADEL uses the default http scheme → no __scheme__ reserved label"
        )
        assert not any(k.startswith("__param_") for k in labels), (
            "ZITADEL declares no params → no __param_* reserved label"
        )

    def test_traefik_and_postgres_have_no_reserved_meta_labels(self, rendered):
        """SVC-09 Traefik + SVC-01 postgres scrape the default /metrics over
        http → NO reserved discovery labels, only project/service."""
        for service in ("traefik", "postgres"):
            for g in rendered[service]:
                labels = g["labels"]
                assert "__scheme__" not in labels, f"{service} must not set __scheme__"
                assert "__metrics_path__" not in labels, (
                    f"{service} must not set __metrics_path__ (default /metrics)"
                )
                assert not any(k.startswith("__param_") for k in labels), (
                    f"{service} must not set any __param_* label"
                )
                # Only the two ordinary labels remain.
                assert set(labels) == {"project", "service"}, (
                    f"{service} target group must carry exactly project+service labels; "
                    f"got {sorted(labels)}"
                )


# --------------------------------------------------------------------------- #
# DEGRADE — a missing service yields NO target file (design §3.4).
# --------------------------------------------------------------------------- #
class TestDegradeOnMissingService:
    def test_dropping_zitadel_yields_no_zitadel_file(self, builtin_targets):
        index = _fixture_inventory_index()
        del index["zitadel"]  # SVC-06 not (yet) built
        rendered = _render_present(builtin_targets, index)
        assert "zitadel" not in rendered, (
            "a service absent from the inventory index must yield NO target file "
            "(degrade — design §3.4 'scrape what exists')"
        )
        # The other three are unaffected — degrade is per-service, not fatal.
        assert set(rendered) == {"postgres", "openbao", "traefik"}, (
            f"the remaining built services must still render; got {sorted(rendered)}"
        )

    def test_empty_service_role_group_yields_no_file(self, builtin_targets):
        """An inventory that KNOWS the service_role group but it is empty (no
        guests) is also a degrade — no file, no error."""
        index = _fixture_inventory_index()
        index["openbao"] = {}  # group present but zero guests
        rendered = _render_present(builtin_targets, index)
        assert "openbao" not in rendered, (
            "an empty service_role group (zero guests) must yield no file (degrade)"
        )

    def test_entirely_empty_index_yields_nothing(self, builtin_targets):
        """No built services present at all → no files, no error (full degrade)."""
        rendered = _render_present(builtin_targets, {})
        assert rendered == {}, (
            "an empty inventory index must emit zero target files (degrade, not fail)"
        )


# --------------------------------------------------------------------------- #
# MULTI-HOST — dedicated-per-project postgres renders per-host project labels.
# --------------------------------------------------------------------------- #
class TestMultiHostDedicatedService:
    def test_postgres_renders_one_target_group_per_project_guest(self, builtin_targets):
        rendered = _render_present(builtin_targets, _fixture_inventory_index())
        groups = rendered["postgres"]
        assert len(groups) == 2, (
            "the dedicated-per-project postgres service with two project guests "
            f"must render two target groups; got {len(groups)}"
        )

    def test_postgres_each_host_keeps_its_own_derived_project_label(self, builtin_targets):
        """FR-11 + __from_inventory__ sentinel: each postgres guest keeps the
        per-host project derived from the inventory index, not a shared literal."""
        rendered = _render_present(builtin_targets, _fixture_inventory_index())
        groups = rendered["postgres"]
        # Map the rendered ip -> project label.
        ip_to_project = {}
        for g in groups:
            target = g["targets"][0]
            ip = str(target).rpartition(":")[0]
            ip_to_project[ip] = g["labels"]["project"]
        assert ip_to_project == {
            "10.0.100.11": "mlvideo",
            "10.0.101.11": "dronefleet",
        }, (
            "each postgres project guest must carry its own derived project label "
            f"(__from_inventory__ sentinel per host); got {ip_to_project}"
        )
        # And every postgres target group still labels service=postgres.
        assert all(g["labels"]["service"] == "postgres" for g in groups)

    def test_postgres_row_uses_from_inventory_sentinel(self, builtin_targets):
        """Guard: the assumption driving the multi-host test — SVC-01's row uses
        the __from_inventory__ sentinel (per-host project), not a literal."""
        pg = next(r for r in builtin_targets if r["service"] == "postgres")
        assert pg["project"] == _FROM_INVENTORY, (
            "SVC-01 postgres must derive project per host (__from_inventory__); "
            f"got {pg['project']!r}"
        )
