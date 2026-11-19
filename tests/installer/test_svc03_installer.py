"""SVC-03 Garage installer — offline structural contract (spec svc-03-object-storage).

Binds the requirements to the committed Ansible:
* R11/R12 thin four-phase orchestrator with a gated clean-slate branch;
* R4 ports bound to the guest's own VLAN-20 IP, 3902 not published;
* R2 layout applied only when a node lacks a role (re-run makes no layout change);
* R6 secret hygiene: every task touching a secret is no_log, and no secret VALUE
  is ever placed on argv (the token travels as `-e NAME` + task environment);
* R9/R10 bucket provision: input validation, read+write never owner, OpenBao path;
* R7 scrape row; R12 platform catalog row and VMIDs agree with the play and ledger.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml
from _orchestrator_prestep import phase_tasks  # noqa: E402  (SVC-10 D5 pre-step)

_ROOT = Path(__file__).resolve().parents[2]
_PB = _ROOT / "ansible" / "playbooks"
_ROLES = _ROOT / "ansible" / "roles"


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _iter_tasks(tasks):
    for t in tasks or []:
        if isinstance(t, dict):
            yield t
            for k in ("block", "rescue", "always"):
                yield from _iter_tasks(t.get(k))


def _role_tasks(role: str, name: str = "main.yml") -> list[dict]:
    return list(_iter_tasks(_load(_ROLES / role / "tasks" / name)))


def _play_vars() -> dict:
    return _load(_PB / "svc-03-bootstrap.yml")[0]["vars"]


# --- R11/R12 orchestrator ------------------------------------------------------


def test_orchestrator_is_thin_with_clean_slate_first_and_phases_gated():
    play = _load(_PB / "svc-03-bootstrap.yml")[0]
    assert play["hosts"] == "localhost" and play["any_errors_fatal"] is True
    top = phase_tasks(play["tasks"])
    roles = [t["block"][0]["ansible.builtin.import_role"]["name"] for t in top]
    assert roles == ["svc03_clean_slate", "svc03_preflight", "svc03_provision", "svc03_bootstrap", "svc03_handoff"]
    assert top[0]["when"] == "clean_slate | default(false) | bool"
    for t in top[1:]:
        assert t["when"] == "not (clean_slate | default(false) | bool)"
        assert "ansible.builtin.fail" in t["rescue"][0]


def test_play_constants_agree_with_ledger_and_computed_vmids():
    v = _play_vars()
    ledger = _load(_ROOT / "infra" / "platform-foundation" / "host-indices.yaml")
    idx = sorted(e["host_index"] for e in ledger[20] if e.get("svc_code") == "svc03")
    assert idx == [12, 13, 14]
    assert v["svc03_vmids"] == [20 * 1000 + i for i in idx], "ADR-0022"
    assert v["svc03_guest_ips"] == [f"10.0.20.{10 + i}" for i in idx], "NET-00 §3"


def test_platform_catalog_row_matches_the_play():
    rows = _load(_ROLES / "platform_clean_slate" / "defaults" / "main.yml")["platform_clean_slate_services"]
    row = next(r for r in rows if r["service"] == "svc03")
    assert row["bootstrap_playbook"] == "ansible/playbooks/svc-03-bootstrap.yml"
    assert row["verify_vmids"] == _play_vars()["svc03_vmids"]
    assert row["verify_tf_root"] == "infra/projects/svc-03-object-storage"
    assert row["generated_inventory"] == _play_vars()["svc03_generated_inventory"]


def test_clean_slate_assembles_three_targets_and_removes_the_s3_route():
    tasks = _role_tasks("svc03_clean_slate")
    names = [t["name"] for t in tasks]
    guard = names.index("Clean-Slate | Refuse the reset unless the throwaway guard AUTHORIZED it")
    destroy = next(i for i, t in enumerate(tasks) if "clean-slate.yml" in str(t.get("ansible.builtin.command", "")))
    assert guard < destroy
    assemble = next(t for t in tasks if "Assemble" in t["name"])
    assert assemble["loop"] == "{{ svc03_vmids }}"
    assert "proxmox_virtual_environment_container.node" in assemble["ansible.builtin.set_fact"]["svc03_clean_slate_targets"]
    removal = next(t for t in tasks if "Remove the delegated" in t["name"])
    route_name = next(t for t in _role_tasks("garage_install", "route.yml"))["vars"]["route_name"]
    assert removal["ansible.builtin.file"]["path"].endswith(f"/{route_name}-route.yml")


# --- R4 ports --------------------------------------------------------------------


def test_compose_ports_are_bound_to_the_guest_vlan20_ip():
    task = next(t for t in _role_tasks("garage_install") if "docker-compose-app" in str(t))
    svc = task["vars"]["dca_services"][0]
    assert svc["image"] == "{{ garage_image }}"
    for p in svc["ports"]:
        assert p.startswith("{{ garage_bind_ip }}:"), p
        assert "0.0.0.0" not in p
    assert any("garage_web_port" in p for p in svc["ports"]), "WS-C51 publishes the website port"
    d = _load(_ROLES / "garage_install" / "defaults" / "main.yml")
    assert d["garage_bind_ip"] == "{{ ansible_host }}"
    assert {d["garage_s3_port"], d["garage_rpc_port"], d["garage_admin_port"], d["garage_web_port"]} == {
        3900, 3901, 3903, 3902}


def test_garage_toml_renders_replication_three_and_the_secrets_from_the_environment(monkeypatch):
    monkeypatch.setenv("GARAGE_RPC_SECRET", "ab" * 32)
    monkeypatch.setenv("GARAGE_ADMIN_TOKEN", "adm-token-placeholder")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)

    def _lookup(kind, name):
        assert kind == "ansible.builtin.env"
        import os
        return os.environ[name]

    tpl = env.from_string((_ROLES / "garage_install" / "templates" / "garage.toml.j2").read_text())
    out = tpl.render(lookup=_lookup, garage_replication_factor=3, garage_consistency_mode="consistent",
                     garage_rpc_port=3901, garage_bind_ip="10.0.20.22", garage_s3_port=3900,
                     garage_s3_region="garage", garage_s3_host="s3.example.test", garage_admin_port=3903,
                     garage_web_port=3902, garage_web_root_domain="web.example.test")
    assert "replication_factor = 3" in out
    assert 'rpc_public_addr = "10.0.20.22:3901"' in out
    assert f'rpc_secret = "{"ab" * 32}"' in out
    assert 'admin_token = "adm-token-placeholder"' in out
    assert "metrics_token" not in out, "metrics stay open on VLAN 20 like the other exporters (R7)"
    assert 'root_domain = ".web.example.test"' in out and 'bind_addr = "0.0.0.0:3902"' in out, "WS-C51"


def test_config_file_is_0600_and_no_log():
    t = next(t for t in _role_tasks("garage_install") if "ansible.builtin.template" in t)
    assert t["ansible.builtin.template"]["mode"] == "0600" and t["no_log"] is True


# --- R2 layout idempotency --------------------------------------------------------


def test_layout_is_staged_and_applied_only_for_nodes_without_a_role():
    tasks = _role_tasks("garage_install", "layout.yml")
    for name in ("Layout | Stage storage roles for the nodes without one", "Layout | Apply the staged layout (version + 1)"):
        t = next(t for t in tasks if t["name"] == name)
        assert t["when"] == "(garage_layout_missing | length) > 0"
        assert t["run_once"] is True
    status = next(t for t in tasks if t["name"].startswith("Layout | Wait until every node is connected"))
    assert "until" in status and "retries" in status, "P3: polled readiness, never a sleep"
    assert not any("ansible.builtin.pause" in t or "wait_for" in str(t.get("ansible.builtin.wait_for", "")) for t in tasks)


# --- R6 secret hygiene --------------------------------------------------------------

_SECRET_MARKERS = ("garage_bp_rot_new", "svc03_rpc_secret", "svc03_admin_token", "svc03_openbao_admin_token", "garage_bp_openbao_token",
                   "secretAccessKey", "GARAGE_RPC_SECRET", "GARAGE_ADMIN_TOKEN", "garage_smoke_key")


def _all_secret_tasks():
    files = [_ROLES / r / "tasks" / f for r, f in (
        ("svc03_bootstrap", "main.yml"), ("svc03_preflight", "main.yml"), ("garage_install", "main.yml"),
        ("garage_bucket_provision", "provision.yml"), ("garage_bucket_provision", "teardown.yml"),
        ("garage_bucket_provision", "rotate.yml"))]
    files.append(_PB / "garage-health.yml")
    for f in files:
        doc = _load(f)
        tasks = doc[0]["tasks"] if f.parent == _PB else doc
        for t in _iter_tasks(tasks):
            yield f.name, t


def test_every_task_that_touches_a_secret_value_is_no_log():
    seen = 0
    for fname, t in _all_secret_tasks():
        if "block" in t or "ansible.builtin.debug" in t or "ansible.builtin.fail" in t:
            continue
        body = {k: v for k, v in t.items() if k not in ("name", "when", "no_log")}
        text = str(body)
        if any(m in text for m in _SECRET_MARKERS) and "ansible.builtin.assert" not in t:
            seen += 1
            assert t.get("no_log") is True, f"{fname}: {t['name']} handles a secret without no_log"
    assert seen >= 10, "non-vacuity"


def test_no_secret_value_is_ever_on_argv():
    for fname, t in _all_secret_tasks():
        cmd = t.get("ansible.builtin.command")
        if not cmd:
            continue
        argv = str(cmd.get("argv", ""))
        assert "BAO_TOKEN=" not in argv, f"{fname}: {t['name']} puts the token value on argv"
        for m in ("svc03_rpc_secret", "svc03_admin_token", "garage_bp_openbao_token", "secretAccessKey"):
            assert m not in argv, f"{fname}: {t['name']} puts {m} on argv"


def test_cluster_secret_path_has_the_canonical_shape_and_is_reused_not_regenerated():
    d = _load(_ROLES / "svc03_bootstrap" / "defaults" / "main.yml")
    assert d["svc03_cluster_secret_path"] == "{{ svc03_openbao_kv_mount }}/proj-shared-garage/garage"
    gen = next(t for t in _role_tasks("svc03_bootstrap") if t["name"].startswith("Bootstrap | Generate"))
    assert gen["when"] == "svc03_secret_missing | bool", "a re-run must reuse the stored secrets"
    put = next(t for t in _role_tasks("svc03_bootstrap") if t["name"].startswith("Bootstrap | Store"))
    assert put["when"] == "svc03_secret_missing | bool"


def test_preflight_fails_closed_without_an_openbao_token():
    t = next(t for t in _role_tasks("svc03_preflight") if "Fail closed when no OpenBao admin token" in t["name"])
    assert t["ansible.builtin.assert"]["that"] == ["svc03_openbao_token_present | bool"]
    assert "when" not in t, "SVC-03 has no offline mode"


# --- R9/R10 bucket provision ----------------------------------------------------------


def _validation_regexes():
    t = _role_tasks("garage_bucket_provision", "provision.yml")[0]
    that = t["ansible.builtin.assert"]["that"]
    slug = re.search(r"match\('(.+?)'\)", that[0]).group(1)
    purpose = re.search(r"reject\('match', '(.+?)'\)", that[3]).group(1)
    return slug, purpose


def test_bucket_input_validation_accepts_and_rejects_the_right_names():
    slug, purpose = _validation_regexes()
    for good in ("dronefleet", "ml-video", "a1"):
        assert re.match(slug, good)
    for bad in ("Drone", "-x", "x-", "a" * 21, "a_b", ""):
        assert not re.match(slug, bad) or re.match(slug, bad).end() != len(bad), bad
    for good in ("artifacts", "flightlogs", "v2"):
        assert re.match(purpose, good)
    # No hyphen (consumer-object-storage defect fix): slug `a` + `b-x` would equal slug `a-b` + `x`.
    for bad in ("Logs", "a_b", "-a", "flight-logs", "b-x", "a" * 33):
        assert not re.match(purpose, bad) or re.match(purpose, bad).end() != len(bad), bad


def test_project_key_gets_read_write_never_owner_and_lands_in_openbao():
    tasks = _role_tasks("garage_bucket_provision", "provision.yml")
    allow = next(t for t in tasks if "Grant read+write" in t["name"])
    argv = " ".join(allow["ansible.builtin.command"]["argv"])
    assert "'read': true, 'write': true, 'owner': false" in argv
    create = next(t for t in tasks if "Create the project key" in t["name"])
    assert create["when"] == "(garage_bp_key_matches | length) == 0", "never rotate on a re-run"
    d = _load(_ROLES / "garage_bucket_provision" / "defaults" / "main.yml")
    assert d["garage_bp_secret_path"] == "{{ garage_bp_openbao_kv_mount }}/proj-{{ project_slug }}/garage"
    assert d["garage_bp_key_name"] == "proj-{{ project_slug }}-cikey"
    store = next(t for t in tasks if "Store the project credentials" in t["name"])
    assert store["when"] == "garage_bp_store_needed | bool" and store["no_log"] is True


# --- R7 scrape row ------------------------------------------------------------------------


def test_garage_scrape_target_row():
    rows = _load(_ROLES / "observability_stack" / "defaults" / "main.yml")["observability_builtin_scrape_targets"]
    row = next(r for r in rows if r["service"] == "garage")
    assert row["service_role"] == "garage" and row["port"] == 3903 and row["project"] == "shared"
    main = (_ROOT / "infra" / "projects" / "svc-03-object-storage" / "main.tf").read_text()
    assert main.count('service_name = "garage"') == 3, "service_role in the inventory comes from service_name"


# --- consumer-object-storage R5: teardown ----------------------------------------------


def test_role_dispatches_on_a_validated_action():
    tasks = _role_tasks("garage_bucket_provision")
    assert tasks[0]["ansible.builtin.assert"]["that"] == ["garage_bp_action in ['provision', 'teardown', 'rotate']"]
    inc = {t["ansible.builtin.include_tasks"]: t["when"] for t in tasks if "ansible.builtin.include_tasks" in t}
    assert inc == {"provision.yml": "garage_bp_action == 'provision'", "teardown.yml": "garage_bp_action == 'teardown'",
                   "rotate.yml": "garage_bp_action == 'rotate'"}
    assert _load(_ROLES / "garage_bucket_provision" / "defaults" / "main.yml")["garage_bp_action"] == "provision"


def test_teardown_targets_key_grants_and_declared_buckets_never_a_prefix():
    tasks = _role_tasks("garage_bucket_provision", "teardown.yml")
    text = str(tasks)
    assert "ListBuckets" not in text and "startswith" not in text, "prefix matching would catch slug a-b for slug a"
    targets = next(t for t in tasks if t["name"].startswith("Teardown | Compute the target bucket ids"))
    expr = targets["ansible.builtin.set_fact"]["garage_bp_td_targets"]
    assert "buckets" in expr and "garage_bp_td_declared" in expr


def test_teardown_empties_before_deleting_and_verifies_an_empty_baseline():
    names = [t["name"] for t in _role_tasks("garage_bucket_provision", "teardown.yml")]
    order = [names.index(n) for n in (
        "Teardown | Empty each bucket over S3 (s3_smoke.py purge, credentials in env only)",
        "Teardown | Delete each bucket",
        "Teardown | Delete the project key and any temporary key",
        "Teardown | Delete the OpenBao entry, all versions (no_log)",
        "Teardown | GATE — no bucket, key or OpenBao entry of the project remains",
    )]
    assert order == sorted(order)
    purge = next(t for t in _role_tasks("garage_bucket_provision", "teardown.yml") if "s3_smoke.py purge" in t["name"])
    assert purge["delegate_to"] == "localhost" and purge["no_log"] is True
    assert purge["become"] is False, "the play is become: true; the control-node step must not sudo (found live)"
    assert set(purge["environment"]) == {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"}
    assert "metadata" in str(next(t for t in _role_tasks("garage_bucket_provision", "teardown.yml")
                                  if "Delete the OpenBao entry" in t["name"])["ansible.builtin.command"])


# --- WS-C55: key rotation -------------------------------------------------------------------


def test_rotation_is_opt_in_with_a_cadence_knob():
    d = _load(_ROLES / "garage_bucket_provision" / "defaults" / "main.yml")
    assert d["garage_bp_action"] == "provision" and d["garage_bp_rotate_min_age_days"] == 0
    inc = {t["ansible.builtin.include_tasks"]: t["when"] for t in _role_tasks("garage_bucket_provision")
           if "ansible.builtin.include_tasks" in t}
    assert inc["rotate.yml"] == "garage_bp_action == 'rotate'"


def test_rotation_order_never_leaves_the_project_without_a_working_stored_key():
    tasks = _role_tasks("garage_bucket_provision", "rotate.yml")
    names = [t["name"] for t in tasks]
    order = [names.index(n) for n in (
        "Rotate | Create the next key (no_log)",
        "Rotate | Grant the next key the current key's permissions on each bucket",
        "Rotate | Prove the next key can list every granted bucket (credentials in env only)",
        "Rotate | Store the next key in OpenBao as a new version (no_log)",
        "Rotate | VERIFY — OpenBao now holds the next key's id (no_log)",
        "Rotate | Delete the old key (revokes it)",
        "Rotate | Give the next key the canonical name",
        "Rotate | VERIFY — exactly one key with the canonical name, and it is the new one",
    )]
    assert order == sorted(order), "revoke only after the new key is stored and proven"
    block = next(t for t in tasks if t["name"] == "Rotate | Rotation steps")
    assert block["when"] == "garage_bp_rot_due | bool"
    store = next(t for t in tasks if t["name"].startswith("Rotate | Store the next key"))
    assert "kv patch" in str(store["ansible.builtin.command"]), "patch keeps the other fields and the history"
    prove = next(t for t in tasks if t["name"].startswith("Rotate | Prove"))
    assert prove["become"] is False and prove["delegate_to"] == "localhost"


def test_rotation_finishes_an_interrupted_rotation_instead_of_failing():
    tasks = _role_tasks("garage_bucket_provision", "rotate.yml")
    names = [t["name"] for t in tasks]
    rename = next(t for t in tasks if t["name"].startswith("Rotate | Finish an interrupted rotation — rename"))
    assert "(garage_bp_rot_current | length) == 0" in rename["when"]
    assert any("garage_bp_rot_resume_probe.stdout" in w for w in rename["when"]), "only when OpenBao holds it"
    stop = next(t for t in tasks if t["name"] == "Rotate | Stop after finishing an interrupted rotation")
    assert stop["ansible.builtin.meta"] == "end_play", "end_host would let run_once continue on the next node"
    assert names.index(stop["name"]) < names.index("Rotate | Fail closed unless exactly one current key exists")


# --- WS-C51: static website hosting ----------------------------------------------------------


def test_website_hosting_is_opt_in_per_bucket_and_reconciled():
    d = _load(_ROLES / "garage_bucket_provision" / "defaults" / "main.yml")
    assert d["website_purposes"] == [] and d["garage_bp_web_port"] == 3902
    tasks = _role_tasks("garage_bucket_provision", "website.yml")
    reconcile = next(t for t in tasks if t["name"] == "Website | Reconcile website access per bucket")
    assert "websiteAccess" in str(reconcile["ansible.builtin.command"]) and "when" in reconcile, "change only on drift"
    route = next(t for t in tasks if t["name"].startswith("Website | Render the website Route_Fragments"))
    assert route["ansible.builtin.include_role"]["name"] == "traefik_route"
    assert route["vars"]["route_host_rule"] == "{{ garage_bp_web_bucket }}.{{ garage_bp_web_root_domain }}"
    assert route["vars"]["route_backend_url"].startswith("http://{{ garage_bp_web_backend_ip }}"), \
        "the backend IP is bound before delegation, never ansible_host inside the delegated role"
    remove = next(t for t in tasks if t["name"].startswith("Website | Remove the Route_Fragment"))
    assert remove["vars"]["ansible_connection"] == "ssh"
    validate = _role_tasks("garage_bucket_provision", "provision.yml")[0]["ansible.builtin.assert"]["that"]
    assert "(website_purposes | difference(bucket_purposes) | length) == 0" in validate


def test_teardown_removes_website_routes():
    names = [t["name"] for t in _role_tasks("garage_bucket_provision", "teardown.yml")]
    assert names.index("Teardown | Remove any website Route_Fragment of the deleted buckets (WS-C51)") < names.index(
        "Teardown | GATE — no bucket, key or OpenBao entry of the project remains")
