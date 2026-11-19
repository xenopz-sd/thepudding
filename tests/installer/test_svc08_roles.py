"""SVC-08 offline role-shape tests: secret hygiene, wiring consistency with SVC-07, catalog row.
Spec: .kiro/specs/svc-08-internal-pki/ (task 4)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ROLES = REPO / "ansible" / "roles"
NEW_ROLES = ["pki_ca", "pki_issue", "pki_trust", "svc08_preflight", "svc08_bootstrap", "svc08_handoff", "svc08_clean_slate"]


def _tasks(path: Path):
    for t in yaml.safe_load(path.read_text()) or []:
        yield t
        for k in ("block", "rescue", "always"):
            for sub in t.get(k, []) or []:
                yield sub


def _all_task_files():
    for r in NEW_ROLES:
        for f in sorted((ROLES / r / "tasks").glob("*.yml")):
            yield r, f


def _defaults(role):
    return yaml.safe_load((ROLES / role / "defaults" / "main.yml").read_text())


def test_roles_exist_and_are_nonvacuous():
    files = list(_all_task_files())
    assert len(files) >= 8
    for r in NEW_ROLES:
        assert (ROLES / r / "meta" / "main.yml").is_file(), r


def test_token_never_on_argv_and_secret_tasks_are_no_log():
    for role, f in _all_task_files():
        for t in _tasks(f):
            text = yaml.safe_dump(t)
            argv = yaml.safe_dump(t.get("ansible.builtin.command", {}))
            # The admin/child token travels only in the task environment (name-only into docker exec).
            assert "pki_ca_admin_token" not in argv and "pki_bao_token" not in argv, (f.name, t.get("name"))
            if "BAO_TOKEN" in yaml.safe_dump(t.get("environment", {})):
                assert t.get("no_log") is True, (f.name, t.get("name"))
            if re.search(r"private_key|pki_issue_new_key|pki_issue_child_token|pki_ca_admin_token\s*:", text):
                assert t.get("no_log") is True or "include_role" in text or "assert" in text, (f.name, t.get("name"))


def test_private_key_written_0600_or_group_readable_only():
    d = _defaults("pki_issue")
    assert d["pki_issue_key_mode"] == "0600"
    boot = (ROLES / "svc08_bootstrap" / "tasks" / "main.yml").read_text()
    assert 'pki_issue_key_mode: "0640"' in boot  # OpenBao container group read, same as the mock TLS


def test_openbao_listener_paths_and_owner_match_svc07():
    ob = _defaults("openbao_install")
    boot = (ROLES / "svc08_bootstrap" / "tasks" / "main.yml").read_text()
    host_dir = ob["openbao_tls_host_dir"]
    assert ob["openbao_tls_cert_file"].endswith("/cert.pem") and ob["openbao_tls_key_file"].endswith("/key.pem")
    assert f"{{{{ svc08_openbao_tls_dir }}}}/cert.pem" in boot and f"{{{{ svc08_openbao_tls_dir }}}}/key.pem" in boot
    b = _defaults("svc08_bootstrap")
    assert b["svc08_openbao_tls_dir"] == host_dir
    assert b["svc08_openbao_uid"] == ob["openbao_container_uid"]
    assert b["svc08_openbao_gid"] == ob["openbao_container_gid"]
    ips = {x["ip"] for x in b["svc08_openbao_listeners"]}
    assert ips == {"10.0.20.10", ob["openbao_unsealer_ip"]}


def test_listener_flip_is_on_by_default():
    assert _defaults("svc08_bootstrap")["svc08_issue_openbao_listeners"] is True


def test_clean_slate_policies_cover_every_consumer_role():
    consumers = {c["name"] for c in _defaults("pki_ca")["pki_ca_consumers"]}
    policies = {p.removeprefix("pki-issue-") for p in _defaults("svc08_clean_slate")["svc08_cs_policy_names"]}
    assert consumers == policies


def test_gate_is_the_published_root_and_mock_flags_stay_default_off():
    # pki_issue / pki_trust key on the published root (PLATFORM_CA_CERT target); with none, nothing changes.
    assert "platform-root-ca.pem" in _defaults("pki_issue")["pki_issue_root_path"]
    assert "platform-root-ca.pem" in _defaults("pki_trust")["pki_trust_root_src"]
    for role, flag in [("openbao_install", "openbao_tls_self_signed"), ("traefik_install", "traefik_tls_self_signed")]:
        assert _defaults(role)[flag] is False


def test_platform_catalog_row_matches_bootstrap():
    d = yaml.safe_load((ROLES / "platform_clean_slate" / "defaults" / "main.yml").read_text())
    rows = [r for r in d["platform_clean_slate_services"] if r["service"] == "svc08"]
    assert len(rows) == 1
    assert rows[0]["bootstrap_playbook"] == "ansible/playbooks/svc-08-bootstrap.yml"
    assert (REPO / rows[0]["bootstrap_playbook"]).is_file()
    assert (REPO / rows[0]["verify_tf_root"]).is_dir()


def test_trust_role_is_noop_without_published_root():
    text = (ROLES / "pki_trust" / "tasks" / "main.yml").read_text()
    assert "when: pki_trust_src_stat.stat.exists" in text


def test_issue_verifies_before_writing():
    text = (ROLES / "pki_issue" / "tasks" / "main.yml").read_text()
    assert text.index("Check chain, SANs and validity") < text.index("Write the private key")


def test_platform_admin_template_carries_the_same_pki_grants_the_installer_appends():
    grants = (ROLES / "pki_ca" / "files" / "platform-admin-pki-grants.hcl").read_text().strip()
    tmpl = (ROLES / "openbao_init_unseal" / "templates" / "platform-admin.hcl.j2").read_text()
    assert grants in tmpl, "fresh installs and the SVC-08 append must grant the same stanza (ADR-0029)"
    assert 'path "auth/token/create/pki-issue"' in grants and "sudo" not in grants


def test_svc07_clean_slate_removes_the_dead_ca_artifacts():
    text = (ROLES / "svc07_clean_slate" / "tasks" / "main.yml").read_text()
    assert ".pki/platform-root-ca.pem" in text and "PLATFORM_CA_CERT" in text
    assert "svc-08-internal-pki.generated.yml" in text


def test_seal_client_verifies_only_after_a_live_chain_probe():
    tasks = (ROLES / "openbao_install" / "tasks" / "main.yml").read_text()
    tmpl = (ROLES / "openbao_install" / "templates" / "config.hcl.j2").read_text()
    assert tasks.index("live probe") < tasks.index("Template the primary config.hcl")
    assert "openbao_unsealer_ca_probe.rc | default(1)) == 0" in tasks
    assert tmpl.index("openbao_seal_verify_with_platform_ca") < tmpl.index('tls_skip_verify = "true"')


def test_traefik_internal_resolver_is_gated_and_ec():
    tmpl = (ROLES / "traefik_install" / "templates" / "traefik.yml.j2").read_text()
    dyn = (ROLES / "traefik_install" / "templates" / "dynamic.yml.j2").read_text()
    acme = (ROLES / "traefik_install" / "tasks" / "internal_acme.yml").read_text()
    assert "{% if traefik_internal_acme_enabled" in tmpl and "keyType: EC256" in tmpl
    assert "disableANSChecks: true" in tmpl and "insecureSkipVerify" not in tmpl
    assert "defaultGeneratedCert" in dyn and "resolver: internal" in dyn
    # mock 3 and the CA are mutually exclusive; the key file is 0600 and never on argv
    assert "not (traefik_tls_self_signed | bool)" in acme
    assert 'mode: "0600"' in acme and "BAO_TOKEN']" in acme


def test_grafana_real_ca_flag_derives_from_the_gate():
    text = (ROLES / "obs_bootstrap" / "tasks" / "main.yml").read_text()
    assert ".pki/platform-root-ca.pem" in text and "obs_tls_real_ca_available: true" in text
    assert "Fail closed when Mock B is set while the platform CA is published" in text


def test_only_the_primary_container_env_changes_with_the_ca():
    # A recreated unsealer stays sealed (Shamir keys are in memory only at init), so its env must not depend on the CA.
    tasks = (ROLES / "openbao_install" / "tasks" / "main.yml").read_text()
    assert 'dca_env: "{{ openbao_env_unsealer }}"' in tasks
    assert tasks.count("BAO_CACERT") >= 1 and "openbao_env_primary | combine" in tasks
    lst = {x["ip"]: x for x in _defaults("svc08_bootstrap")["svc08_openbao_listeners"]}
    assert lst["10.0.20.10"].get("loopback") is True and not lst["10.0.20.11"].get("loopback")


def test_common_role_applies_pki_trust_for_every_caller():
    text = (ROLES / "common" / "tasks" / "main.yml").read_text()
    assert "name: pki_trust" in text


def test_clean_slate_skips_openbao_steps_when_the_guest_is_gone():
    # Platform teardown: SVC-08's clean-slate runs after SVC-07's, when the OpenBao guest is gone. A templated
    # ignore_unreachable did not stop the abort live (2026-11-18), so the role probes SSH first and skips.
    cs = (ROLES / "svc08_clean_slate" / "tasks" / "main.yml").read_text()
    assert "wait_for" in cs and "svc08_cs_openbao_reachable" in cs
    assert cs.index("svc08_cs_openbao_reachable") < cs.index("Disable the PKI mounts")
    assert "not (svc08_cs_openbao_reachable.failed" in cs
    probe = next(t for t in _tasks(ROLES / "svc08_clean_slate" / "tasks" / "main.yml")
                 if t.get("register") == "svc08_cs_openbao_reachable")
    assert probe.get("ignore_errors") is True and "failed_when" not in probe  # failed_when:false hides the timeout
