"""SVC-10 internal DNS installer — offline contract (spec svc-10-internal-dns).

Binds the requirements to the committed Ansible:
* R9 thin four-phase orchestrator with the gated clean-slate branch first;
* D1/D6 ports bound to the guest's own VLAN-20 IP; pinned images; API key only in a
  0600 file, from OpenBao at the canonical path; every secret-bearing task no_log
  and no secret VALUE on argv;
* FR-3 no open resolver (recursor ACL is private networks only); no zone transfer;
* FR-5 API/metrics firewall sources;
* D3/FR-2 record sync delegations are connectable (P2) and verify an empty re-plan;
* R8 health gate stages; FR-7 split-DNS lines in the handoff banner;
* the platform catalog row, play VMID/IP and ledger agree.
A Docker-gated contract test runs the rendered configs in the pinned images and
resolves a wildcard name through the recursor -> authoritative chain.
"""
from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import jinja2
import pytest
import yaml

from _orchestrator_prestep import phase_tasks  # noqa: E402  (SVC-10 D5 pre-step)

_ROOT = Path(__file__).resolve().parents[2]
_PB = _ROOT / "ansible" / "playbooks"
_ROLES = _ROOT / "ansible" / "roles"
_LEDGER = _ROOT / "infra" / "platform-foundation" / "host-indices.yaml"


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


def _play() -> dict:
    return _load(_PB / "svc-10-bootstrap.yml")[0]


def _pdns_defaults(**over) -> dict:
    d = _load(_ROLES / "pdns_install" / "defaults" / "main.yml")
    d.update(over)
    return d


def _render(template: str, values: dict, env: dict[str, str] | None = None) -> str:
    """Render a role template with the role defaults (nested Jinja resolved), lookup('env') stubbed."""
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)

    def lookup(kind, name, default=""):
        assert kind == "ansible.builtin.env"
        return (env or {}).get(name, default)

    jenv.globals["lookup"] = lookup
    resolved = dict(values)
    for _ in range(3):
        resolved = {k: (jenv.from_string(v).render(**resolved) if isinstance(v, str) and "{{" in v else v)
                    for k, v in resolved.items()}
    src = (_ROLES / "pdns_install" / "templates" / template).read_text(encoding="utf-8")
    return jenv.from_string(src).render(**resolved)


# --- R9 orchestrator ------------------------------------------------------------------
def test_orchestrator_is_thin_with_clean_slate_first_and_phases_gated():
    play = _play()
    assert play["hosts"] == "localhost" and play["any_errors_fatal"] is True
    top = phase_tasks(play["tasks"])
    roles = [t["block"][0]["ansible.builtin.import_role"]["name"] for t in top]
    assert roles == ["svc10_clean_slate", "svc10_preflight", "svc10_provision", "svc10_bootstrap", "svc10_handoff"]
    assert top[0]["when"] == "clean_slate | default(false) | bool"
    for t in top[1:]:
        assert t["when"] == "not (clean_slate | default(false) | bool)"
        assert t["rescue"][0]["ansible.builtin.fail"]


def test_play_identity_matches_the_ledger_and_the_catalog_row():
    v = _play()["vars"]
    row = [e for e in _load(_LEDGER)[20] if e.get("svc_code") == "svc10"]
    assert len(row) == 1 and row[0]["host_index"] == 15 and not row[0].get("anchor")
    assert v["svc10_vmid"] == 20 * 1000 + 15
    assert v["svc10_guest_ips"] == [str(ipaddress.ip_network("10.0.20.0/24")[10 + 15])]
    cat = _load(_ROLES / "platform_clean_slate" / "defaults" / "main.yml")["platform_clean_slate_services"]
    rows = [r for r in cat if r["service"] == "svc10"]
    assert len(rows) == 1
    assert rows[0]["bootstrap_playbook"] == "ansible/playbooks/svc-10-bootstrap.yml"
    assert rows[0]["verify_vmids"] == [v["svc10_vmid"]]
    assert rows[0]["generated_inventory"] == v["svc10_generated_inventory"]
    assert rows[0]["proxmox_host_var"] == "svc10_proxmox_host"


# --- D1/D6 Compose, ports, images ----------------------------------------------------
def _compose_services() -> dict:
    task = next(t for t in _role_tasks("pdns_install") if t.get("ansible.builtin.include_role", {}).get("name")
                == "docker-compose-app")
    return {s["name"]: s for s in task["vars"]["dca_services"]}, task["vars"]


def test_every_published_port_is_bound_to_the_guest_vlan20_ip():
    services, _ = _compose_services()
    assert set(services) == {"auth", "recursor"}
    for svc in services.values():
        assert svc["ports"], svc["name"]
        for port in svc["ports"]:
            assert port.startswith("{{ pdns_bind_ip }}:"), f"{svc['name']}: {port} must bind the VLAN-20 IP"
    assert _pdns_defaults()["pdns_bind_ip"] == "10.0.20.25"


def test_zone_database_persists_in_a_named_volume():
    services, dca = _compose_services()
    assert dca["dca_volumes"] == ["pdns-data"]
    assert "pdns-data:/var/lib/powerdns" in services["auth"]["volumes"]


def test_images_are_pinned_by_version_and_digest():
    d = _pdns_defaults()
    for ref, digest in (("pdns_auth_image_ref", "pdns_auth_image_digest"),
                        ("pdns_recursor_image_ref", "pdns_recursor_image_digest")):
        assert ":latest" not in d[ref] and d[ref].rsplit(":", 1)[1][0].isdigit()
        assert d[digest].startswith("sha256:") and len(d[digest]) == 71


# --- Secrets ---------------------------------------------------------------------------
def test_api_key_path_is_canonical_and_never_in_defaults():
    d = _load(_ROLES / "svc10_bootstrap" / "defaults" / "main.yml")
    assert d["svc10_api_key_secret_path"] == "{{ svc10_openbao_kv_mount }}/proj-shared-dns/pdns"
    assert d["svc10_openbao_kv_mount"] == "secret"
    assert "api_key" not in d and "svc10_api_key" not in d


def test_auth_config_is_0600_and_carries_the_key_only_from_the_environment():
    t = next(t for t in _role_tasks("pdns_install") if t.get("ansible.builtin.template", {}).get("src") == "auth.conf.j2")
    assert t["ansible.builtin.template"]["mode"] == "0600" and t["no_log"] is True
    out = _render("auth.conf.j2", _pdns_defaults(), env={"PDNS_API_KEY": "k" * 40})
    assert "api-key=" + "k" * 40 in out


_SECRET_MARKERS = ("svc10_api_key", "PDNS_API_KEY", "svc10_openbao_admin_token", "BAO_TOKEN")


@pytest.mark.parametrize("role", ["svc10_bootstrap", "svc10_handoff", "svc10_preflight"])
def test_every_secret_bearing_task_is_no_log_and_keeps_values_off_argv(role):
    for t in _role_tasks(role):
        body = {k: v for k, v in t.items() if k not in ("name", "no_log", "when")}
        text = yaml.safe_dump(body)
        if any(m in text for m in ("svc10_api_key }}", "svc10_openbao_admin_token }}", "lookup('ansible.builtin.env', 'PDNS")):
            assert t.get("no_log") is True, f"{role}: {t.get('name')} handles a secret without no_log"
        cmd = t.get("ansible.builtin.command", {})
        argv = yaml.safe_dump(cmd.get("argv", ""))
        assert not re.search(r"svc10_api_key(?!_)", argv) and "admin_token }}" not in argv, (
            f"{role}: secret on argv in {t.get('name')}")


def test_child_runs_receive_the_key_through_the_environment():
    for role in ("svc10_bootstrap", "svc10_handoff"):
        runs = [t for t in _role_tasks(role) if "PDNS_API_KEY" in (t.get("environment") or {})]
        assert runs, role
        for t in runs:
            assert t["no_log"] is True


# --- FR-3 resolver policy, no transfers ------------------------------------------------
def test_recursor_is_not_an_open_resolver_and_forwards_only_the_platform_zone():
    d = _pdns_defaults(pdns_platform_domain="lab.example.test")
    doc = yaml.safe_load(_render("recursor.yml.j2", d))
    nets = [ipaddress.ip_network(n) for n in doc["incoming"]["allow_from"]]
    assert all(n.is_private or n.is_loopback for n in nets), nets
    assert doc["incoming"]["port"] == d["pdns_recursor_container_port"]
    fz = doc["recursor"]["forward_zones"]
    assert fz == [{"zone": "lab.example.test", "forwarders": ["10.0.20.25:5300"]}]
    assert doc["dnssec"]["negative_trustanchors"][0]["name"] == "lab.example.test"


def test_authoritative_server_never_transfers_or_notifies():
    out = _render("auth.conf.j2", _pdns_defaults(), env={"PDNS_API_KEY": "x" * 40})
    for line in ("disable-axfr=yes", "primary=no", "secondary=no", "api=yes"):
        assert line in out.splitlines(), line
    assert not any(ln.startswith("webserver-password") for ln in out.splitlines()), (
        "/metrics must stay readable without a password")


# --- FR-5 firewall ---------------------------------------------------------------------
def test_api_firewall_admits_traefik_prometheus_and_the_admin_cidr_only():
    d = _pdns_defaults(pdns_admin_source_cidr="192.0.2.0/24")
    script = _render("pdns-api-firewall.sh.j2", d)
    srcs = [ln.split("-s ")[1].split()[0] for ln in script.splitlines() if "-j RETURN" in ln]
    assert srcs == ["10.0.20.12/32", "10.0.20.18/32", "192.0.2.0/24"]
    assert 'PORTS="8081 8082"' in script and '-j DROP' in script
    assert "--ctdir ORIGINAL" in script
    no_admin = _render("pdns-api-firewall.sh.j2", _pdns_defaults())
    assert "192.0.2" not in no_admin


def test_firewall_script_is_valid_posix_sh(tmp_path):
    f = tmp_path / "fw.sh"
    f.write_text(_render("pdns-api-firewall.sh.j2", _pdns_defaults()))
    assert subprocess.run(["sh", "-n", str(f)], capture_output=True).returncode == 0


def test_firewall_is_verified_fail_closed():
    names = [t.get("name", "") for t in _role_tasks("pdns_install", "api_firewall.yml")]
    assert any("Fail closed" in n for n in names)


# --- D3/FR-2 record sync ---------------------------------------------------------------
def test_record_sync_delegations_carry_ssh_connection_vars():
    for t in _role_tasks("dns_records_sync"):
        if "delegate_to" in t:
            assert t["delegate_to"] == "{{ drs_dns_host }}"
            assert t["vars"]["ansible_connection"] == "ssh", t.get("name")


def test_record_sync_verifies_an_empty_re_plan():
    tasks = _role_tasks("dns_records_sync")
    verify = next(t for t in tasks if "VERIFY" in t.get("name", ""))
    assert verify["ansible.builtin.assert"]["that"] == ["(drs_plan.upsert | length) == 0",
                                                        "(drs_plan.delete | length) == 0"]
    finds = [t for t in tasks if "ansible.builtin.find" in t]
    assert finds and finds[0]["ansible.builtin.find"]["patterns"] == "{{ drs_inventory_pattern }}"
    assert _load(_ROLES / "dns_records_sync" / "defaults" / "main.yml")["drs_inventory_pattern"] == "*.generated.yml"


def test_record_sync_wipes_the_recursor_cache_after_a_change():
    tasks = _role_tasks("dns_records_sync")
    wipe = next(t for t in tasks if "Wipe the recursor cache" in t.get("name", ""))
    argv = wipe["ansible.builtin.command"]["argv"]
    assert argv[-2:] == ["wipe-cache", "{{ drs_platform_domain }}$"], "the trailing $ wipes the whole zone subtree"
    assert "upsert" in wipe["when"] and "delete" in wipe["when"]
    names = [t.get("name", "") for t in tasks]
    assert names.index(wipe["name"]) < next(i for i, n in enumerate(names) if "List the zone (after)" in n)


def test_bootstrap_ends_with_the_record_sync():
    tasks = _load(_ROLES / "svc10_bootstrap" / "tasks" / "main.yml")
    assert tasks[-1]["ansible.builtin.include_role"]["name"] == "dns_records_sync"


# --- R8 health gate, FR-7 handoff --------------------------------------------------------
def test_health_gate_covers_resolution_api_refusal_and_metrics():
    play = _load(_PB / "svc10-health.yml")[0]
    assert play["hosts"] == "dns"
    names = " ".join(t["name"] for t in play["tasks"])
    for stage in ("Stage 1", "Stage 2", "Stage 3a", "Stage 3b", "Stage 4"):
        assert stage in names
    refuse = next(t for t in play["tasks"] if "Stage 3a" in t["name"])
    assert refuse["ansible.builtin.uri"]["status_code"] == [401] and "headers" not in refuse["ansible.builtin.uri"]
    keyed = next(t for t in play["tasks"] if "Stage 3b" in t["name"])
    assert keyed["no_log"] is True


def test_handoff_prints_split_dns_for_linux_and_macos():
    text = (_ROLES / "svc10_handoff" / "tasks" / "main.yml").read_text(encoding="utf-8")
    # Linux: a global systemd-resolved drop-in (validated on the operator's machine). A per-link
    # `resolvectl dns <iface>` REPLACES the link's servers, sending every lookup to SVC-10.
    assert "/etc/systemd/resolved.conf.d/platform-dns.conf" in text
    assert "DNS={{ svc10_bind_ip }}" in text and "Domains=~{{ svc10_platform_domain }}" in text
    assert "resolvectl dns" not in text, "the per-link recipe replaces the link's DNS servers"
    # macOS: the directory may not exist yet.
    assert "mkdir -p /etc/resolver" in text
    assert "/etc/resolver/{{ svc10_platform_domain }}" in text
    # The banner is printed as JSON by Ansible, which doubles every backslash: a pasted
    # printf '\\n' or a trailing '\\' would write a broken file. Banner commands use none.
    banner = next(x for x in _load(_ROLES / "svc10_handoff" / "tasks" / "main.yml")
                  if "Print the Developer B" in x.get("name", ""))
    for line in banner["ansible.builtin.debug"]["msg"]:
        assert "\\" not in line, f"backslash in a banner command: {line!r}"


def test_clean_slate_has_no_traefik_route_step():
    text = (_ROLES / "svc10_clean_slate" / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "route.yml" not in text and "traefik_guest" not in text
    target = next(t for t in _role_tasks("svc10_clean_slate") if "Assemble" in t.get("name", ""))
    tgt = target["ansible.builtin.set_fact"]["svc10_clean_slate_targets"][0]
    assert tgt["resource_address"] == "proxmox_virtual_environment_container.dns"
    assert tgt["terraform_root"] == "infra/projects/svc-10-internal-dns"


# --- Contract: the rendered configs in the pinned images (Docker-gated) ------------------
def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _have_image(ref: str) -> bool:
    if subprocess.run(["docker", "image", "inspect", ref], capture_output=True).returncode == 0:
        return True
    return subprocess.run(["docker", "pull", "-q", ref], capture_output=True, timeout=300).returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="contract test needs a usable Docker daemon")
def test_rendered_configs_resolve_a_wildcard_through_the_recursor():
    d = _pdns_defaults()
    auth_img, rec_img = d["pdns_auth_image_ref"], d["pdns_recursor_image_ref"]
    if not (_have_image(auth_img) and _have_image(rec_img)):
        pytest.skip("pinned PowerDNS images unavailable offline")
    gw = subprocess.run(["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
                        capture_output=True, text=True).stdout.strip()
    if not gw:
        pytest.skip("no docker bridge gateway to publish on")
    domain = "contract.example.test"
    vals = _pdns_defaults(pdns_platform_domain=domain, pdns_bind_ip=gw, pdns_auth_dns_port=25300)
    td = Path(tempfile.mkdtemp(prefix="svc10_contract_"))
    names = [f"svc10-contract-auth-{time.time_ns()}", f"svc10-contract-rec-{time.time_ns()}"]
    try:
        (td / "auth.conf").write_text(_render("auth.conf.j2", vals, env={"PDNS_API_KEY": "c" * 40}))
        (td / "rec.yml").write_text(_render("recursor.yml.j2", vals))
        td.chmod(0o755)
        for f in td.iterdir():
            f.chmod(0o644)
        subprocess.run(["docker", "run", "-d", "--name", names[0], "-p", f"{gw}:25300:25300/udp",
                        "-v", f"{td}/auth.conf:/etc/powerdns/pdns.d/svc10.conf:ro", auth_img],
                       check=True, capture_output=True)
        subprocess.run(["docker", "run", "-d", "--name", names[1],
                        "-v", f"{td}/rec.yml:/etc/powerdns/recursor.d/svc10.yml:ro", rec_img],
                       check=True, capture_output=True)

        def ex(name, *args):
            return subprocess.run(["docker", "exec", name, *args], capture_output=True, text=True)

        for _ in range(30):
            if "Rcode:" in ex(names[0], "sdig", "127.0.0.1", "25300", domain, "SOA").stdout:
                break
            time.sleep(1)
        assert ex(names[0], "pdnsutil", "zone", "create", domain, vals["pdns_ns_name"]).returncode == 0
        ex(names[0], "pdns_control", "rediscover")
        assert ex(names[0], "pdnsutil", "rrset", "replace", domain, f"*.{domain}", "A", "300",
                  "10.0.20.12").returncode == 0
        rec_ip = subprocess.run(["docker", "inspect", "-f",
                                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", names[1]],
                                capture_output=True, text=True).stdout.strip()
        assert rec_ip, "recursor container has no bridge IP"
        answer = ""
        for _ in range(30):
            answer = ex(names[0], "sdig", rec_ip, str(vals["pdns_recursor_container_port"]),
                        f"anything.{domain}", "A", "recurse").stdout
            if "\tA\t10.0.20.12" in answer:
                break
            time.sleep(1)
        assert "\tA\t10.0.20.12" in answer, answer
    finally:
        subprocess.run(["docker", "rm", "-f", *names], capture_output=True)
        shutil.rmtree(td, ignore_errors=True)
