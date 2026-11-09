"""Offline contract tests for the shared dev mail catcher (Mailpit) — spec dev-mail-catcher, ADR-0025.

Covers requirements R2 (VLAN-bound ports), R9 (UI access control FAILS CLOSED), R13 (never relay
or forward captured mail), image pinning, secret hygiene (no password on argv, ``no_log``), the
Traefik Route_Fragment (P1) and its clean-slate removal (P2), the health gate's live check of the
access control, and the platform teardown catalog row. Pure offline: the real Jinja expressions are
evaluated with Ansible's templater.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _ROOT / "ansible" / "roles"
_PLAYBOOKS = _ROOT / "ansible" / "playbooks"
_INSTALL = _ROLES / "mailcatcher_install"


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _tasks(role: str, file: str = "main.yml"):
    return [t for t in _load(_ROLES / role / "tasks" / file) if isinstance(t, dict)]


def _defaults(role: str) -> dict:
    return _load(_ROLES / role / "defaults" / "main.yml")


_ENV_LOOKUP = "lookup('ansible.builtin.env', 'MAILCATCHER_UI_PASSWORD')"


def _resolved(d: dict) -> dict:
    """Resolve nested ``{{ }}`` references between a role's own defaults (Ansible does this lazily)."""
    out = dict(d)
    for _ in range(4):
        out = {k: (_tpl(v, out) if isinstance(v, str) and "{{" in v else v) for k, v in out.items()}
    return out


def _task(tasks, fragment: str):
    return next(t for t in tasks if fragment in str(t.get("name")))


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _code_lines(path: Path) -> str:
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))


def _all_mailcatcher_files() -> list[Path]:
    files = [p for r in _ROLES.glob("mailcatcher_*") for p in r.rglob("*") if p.is_file()]
    files += [_PLAYBOOKS / n for n in ("mailcatcher.yml", "mailcatcher-health.yml", "mailcatcher-bootstrap.yml")]
    return files


# --------------------------------------------------------------------------- #
# Non-vacuity
# --------------------------------------------------------------------------- #
def test_the_roles_and_playbooks_exist():
    for r in ("install", "preflight", "provision", "bootstrap", "handoff", "clean_slate"):
        assert (_ROLES / f"mailcatcher_{r}" / "tasks" / "main.yml").is_file(), r
    for n in ("mailcatcher.yml", "mailcatcher-health.yml", "mailcatcher-bootstrap.yml"):
        assert (_PLAYBOOKS / n).is_file(), n


# --------------------------------------------------------------------------- #
# Image pin
# --------------------------------------------------------------------------- #
def test_image_is_pinned_to_an_exact_version_and_digest_never_latest():
    d = _defaults("mailcatcher_install")
    assert d["mailcatcher_image_repo"] == "axllent/mailpit"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", d["mailcatcher_image_tag"]), "exact patch version, not v1 / v1.33 / latest"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", d["mailcatcher_image_digest"])
    rendered = _tpl(d["mailcatcher_image"], {k: v for k, v in d.items()})
    assert rendered == f"axllent/mailpit:{d['mailcatcher_image_tag']}@{d['mailcatcher_image_digest']}"
    assert "latest" not in rendered and "edge" not in rendered


# --------------------------------------------------------------------------- #
# R2: ports are published on the guest's own VLAN-20 IP, never 0.0.0.0
# --------------------------------------------------------------------------- #
def test_ports_are_bound_to_the_vlan20_ip_only():
    d = _defaults("mailcatcher_install")
    assert d["mailcatcher_bind_ip"] == "10.0.20.21"
    deploy = _task(_tasks("mailcatcher_install"), "Deploy the Compose stack")
    svc = deploy["vars"]["dca_services"][0]
    assert len(svc["ports"]) == 2
    for port in svc["ports"]:
        assert port.startswith("{{ mailcatcher_bind_ip }}:"), port
    rendered = [_tpl(p, d) for p in svc["ports"]]
    assert rendered == ["10.0.20.21:1025:1025", "10.0.20.21:8025:8025"]


def test_zero_zero_zero_zero_never_appears_in_any_mailcatcher_file():
    for p in _all_mailcatcher_files():
        assert "0.0.0.0" not in _code_lines(p), f"{p.relative_to(_ROOT)} binds/uses 0.0.0.0"


# --------------------------------------------------------------------------- #
# R13: never relay or forward captured mail outward
# --------------------------------------------------------------------------- #
# MP_ALLOW_INTERNAL_HTTP_REQUESTS would re-allow Mailpit's link-check/screenshot/proxy features to
# fetch internal (non-public) IPs: SSRF hardening against the class of CVE-2026-21859 (/proxy).
_FORBIDDEN = ("MP_SMTP_RELAY", "MP_SMTP_FORWARD", "smtp-relay", "smtp-forward", "MP_WEBHOOK_URL",
              "MP_ALLOW_INTERNAL_HTTP_REQUESTS", "allow-internal-http-requests")


def test_no_relay_forward_or_webhook_option_is_ever_set():
    for p in _all_mailcatcher_files():
        code = _code_lines(p)
        for bad in _FORBIDDEN:
            assert bad not in code, f"{p.relative_to(_ROOT)} sets forbidden outward-mail option {bad}"


def test_the_rendered_environment_is_exactly_the_expected_non_secret_set():
    d = _defaults("mailcatcher_install")
    deploy = _task(_tasks("mailcatcher_install"), "Deploy the Compose stack")
    env_expr = deploy["vars"]["dca_services"][0]["environment"]
    off = _tpl(env_expr, {**d, "mailcatcher_has_ui_password": False})
    on = _tpl(env_expr, {**d, "mailcatcher_has_ui_password": True})
    assert set(off) == {"MP_MAX_MESSAGES", "MP_MAX_AGE", "MP_DISABLE_VERSION_CHECK", "MP_LABEL"}
    assert "MP_ALLOW_INTERNAL_HTTP_REQUESTS" not in off, "SSRF hardening: internal-IP fetches stay blocked"
    assert set(on) == set(off) | {"MP_UI_AUTH_FILE"}
    assert on["MP_UI_AUTH_FILE"] == "/data/authfile"
    assert off["MP_DISABLE_VERSION_CHECK"] == "true", "no outbound call to GitHub"
    assert "MP_SMTP_AUTH_ACCEPT_ANY" not in on


def test_auth_file_is_mounted_read_only_only_when_native_auth_is_on():
    d = _resolved(_defaults("mailcatcher_install"))
    deploy = _task(_tasks("mailcatcher_install"), "Deploy the Compose stack")
    vol_expr = deploy["vars"]["dca_services"][0]["volumes"]
    assert _tpl(vol_expr, {**d, "mailcatcher_has_ui_password": False}) == []
    assert _tpl(vol_expr, {**d, "mailcatcher_has_ui_password": True}) == [
        "/opt/compose/mailcatcher/data/authfile:/data/authfile:ro"]


# --------------------------------------------------------------------------- #
# R9: UI access control FAILS CLOSED
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pw,ack,allowed", [
    (False, False, False),   # nothing configured -> refuse
    (True, False, True),     # native basic auth
    (False, True, True),     # explicit named dev acknowledgement
])
def test_install_fails_closed_unless_access_controlled_or_acknowledged(pw, ack, allowed):
    assert_task = _task(_tasks("mailcatcher_install"), "Fail closed unless the UI is access-controlled")
    cond = assert_task["ansible.builtin.assert"]["that"][0]
    got = _tpl("{{ " + cond.strip().replace("\n", " ") + " }}", {
        "mailcatcher_has_ui_password": pw, "mailcatcher_ui_auth_disabled": ack})
    assert bool(got) is allowed


def test_no_unwired_access_control_option_is_accepted():
    """Bug condition (found in WS-C19 prep): ``mailcatcher_forward_auth_address`` satisfied the
    fail-closed assert, but nothing attaches a forwardAuth middleware to the Route_Fragment
    (``traefik_route`` has no middleware support), so setting it left the UI OPEN while the install
    reported it as protected: fail-open dressed as a control. An accepted control must be wired;
    until forward-auth is genuinely implemented the option must not exist."""
    for p in _all_mailcatcher_files():
        low = _code_lines(p).lower()
        assert "forward_auth" not in low and "forward-auth" not in low, (
            f"{p.relative_to(_ROOT)} still offers an unwired forward-auth control")


def test_every_accepted_control_changes_the_rendered_deployment():
    """Native auth must visibly change the container (auth file env + mount); the acknowledgement is
    the explicit absence of both. Nothing else may unlock the install."""
    cond = _task(_tasks("mailcatcher_install"), "Fail closed unless")["ansible.builtin.assert"]["that"][0]
    names = set(re.findall(r"mailcatcher_\w+", cond))
    assert names == {"mailcatcher_has_ui_password", "mailcatcher_ui_auth_disabled"}, names


def test_defaults_are_closed():
    d = _defaults("mailcatcher_install")
    assert d["mailcatcher_ui_auth_disabled"] is False
    assert "mailcatcher_forward_auth_address" not in d


def test_the_default_open_acknowledgement_flag_is_not_set_anywhere_by_default():
    for p in _all_mailcatcher_files():
        for line in _code_lines(p).splitlines():
            assert not re.search(r"mailcatcher_ui_auth_disabled\s*:\s*(true|True)\b", line), p


# --------------------------------------------------------------------------- #
# Secret hygiene: hash on the guest, never argv, always no_log
# --------------------------------------------------------------------------- #
def test_password_file_is_0600_no_log_and_hashed_deterministically():
    t = _task(_tasks("mailcatcher_install"), "Write the UI password file")
    copy = t["ansible.builtin.copy"]
    assert copy["mode"] == "0600" and t["no_log"] is True
    # The test templater cannot load the env lookup plugin (it works in a real run, as in
    # svc-09-bootstrap.yml), so substitute it with a literal; the lookup call shape is asserted.
    assert _ENV_LOOKUP in copy["content"], "password must come from the process ENVIRONMENT"
    content = copy["content"].replace(_ENV_LOOKUP, "'unit-test-password-not-real'")
    d = _resolved(_defaults("mailcatcher_install"))
    first = _tpl(content, d)
    second = _tpl(content, d)
    assert first == second, "deterministic salt: no restart churn on re-run"
    assert first.startswith("mailcatcher:$6$"), "SHA-512 crypt, a format Mailpit accepts"
    assert "unit-test-password-not-real" not in first, "cleartext must never reach the file"


def test_every_task_touching_the_password_is_no_log():
    for role, frag in [("mailcatcher_install", "Resolve the UI access-control mode"),
                       ("mailcatcher_install", "Write the UI password file"),
                       ("mailcatcher_bootstrap", "Look up MAILCATCHER_UI_PASSWORD"),
                       ("mailcatcher_bootstrap", "Generate a random UI password"),
                       ("mailcatcher_bootstrap", "Persist MAILCATCHER_UI_PASSWORD"),
                       ("mailcatcher_bootstrap", "Mailpit install"),
                       ("mailcatcher_bootstrap", "Scrub the UI credential"),
                       ("mailcatcher_handoff", "Read the UI credential"),
                       ("mailcatcher_handoff", "Run the guest health gate")]:
        assert _task(_tasks(role), frag).get("no_log") is True, f"{role}: {frag}"


def test_password_never_travels_on_argv_only_in_the_environment():
    for role, frag in [("mailcatcher_bootstrap", "Mailpit install"),
                       ("mailcatcher_handoff", "Run the guest health gate")]:
        t = _task(_tasks(role), frag)
        argv = " ".join(str(a) for a in t["ansible.builtin.command"]["argv"])
        assert "PASSWORD" not in argv.upper() and "password" not in argv.lower(), f"{role} leaks the secret on argv"
        assert "MAILCATCHER_UI_PASSWORD" in t["environment"]


def test_credential_is_persisted_to_the_gitignored_env_file_at_0600():
    t = _task(_tasks("mailcatcher_bootstrap"), "Persist MAILCATCHER_UI_PASSWORD")
    li = t["ansible.builtin.lineinfile"]
    assert li["mode"] == "0600" and li["create"] is True
    assert "cluster_env_file" in li["path"]
    assert "MAILCATCHER_UI_PASSWORD" in li["regexp"]


def test_banner_never_prints_the_password():
    banner = _task(_tasks("mailcatcher_handoff"), "Print the Developer B completion summary banner")
    text = yaml.dump(banner["ansible.builtin.debug"]["msg"])
    assert "mailcatcher_handoff_ui_password" not in text and "mailcatcher_ui_password" not in text
    assert "value never printed" in text
    assert "zitadel_smtp_dev_catcher=true" in text, "banner prints the explicit, separate enable command"


# --------------------------------------------------------------------------- #
# P1: Route_Fragment for the UI, removed on teardown (P2)
# --------------------------------------------------------------------------- #
def test_ui_route_is_delegated_through_the_traefik_route_role():
    t = _task(_tasks("mailcatcher_install"), "Route_Fragment onto the Traefik guest")
    assert t["ansible.builtin.include_role"]["name"] == "traefik_route"
    v = t["vars"]
    d = _resolved(_defaults("mailcatcher_install"))
    assert v["route_name"] == "mailcatcher" and v["route_tls"] is True and v["route_entrypoint"] == "websecure"
    assert _tpl(v["route_backend_url"], d) == "http://10.0.20.21:8025"
    assert _tpl(v["route_host_rule"], d) == "mail.platform.example.test"


def test_clean_slate_removes_the_route_fragment_with_a_connectable_delegate():
    tasks = _tasks("mailcatcher_clean_slate")
    rm = _task(tasks, "Remove the delegated web-UI Route_Fragment")
    assert rm["ansible.builtin.file"]["state"] == "absent"
    assert rm["ansible.builtin.file"]["path"].endswith("/mailcatcher-route.yml")
    reg = _task(tasks, "Register the Traefik guest")
    assert reg["ansible.builtin.add_host"]["name"] == rm["delegate_to"]
    assert reg["ansible.builtin.add_host"]["ansible_connection"] == "ssh"
    assert "unreachable" in " ".join(rm["failed_when"]), "a real failure condition, not a silent false success"


# --------------------------------------------------------------------------- #
# Health gate live-verifies the access control (R9)
# --------------------------------------------------------------------------- #
def test_health_gate_asserts_401_unauthenticated_when_auth_is_expected():
    plays = _load(_PLAYBOOKS / "mailcatcher-health.yml")
    tasks = plays[0]["tasks"]
    anon = _task(tasks, "Stage 3a — the UI answers an unauthenticated")
    assert anon["ansible.builtin.uri"]["status_code"] == [200, 401]
    assert plays[0]["hosts"] == "mailcatcher"
    chk = _task(tasks, "Assert Stage 3a")
    expr = " ".join(chk["ansible.builtin.assert"]["that"][0].split())
    for expected_auth, want in ((True, 401), (False, 200)):
        got = _tpl("{{ " + expr.replace("(mailcatcher_ui_anon.status | default(0)) ==", "(%d) ==" % want) + " }}",
                   {"mailcatcher_ui_auth_expected": expected_auth})
        assert got is True
    cred = _task(tasks, "Stage 3b")
    assert cred["no_log"] is True and cred["ansible.builtin.uri"]["force_basic_auth"] is True


# --------------------------------------------------------------------------- #
# Installer wiring: 4 phases, clean-slate routing, catalog row
# --------------------------------------------------------------------------- #
def test_bootstrap_has_four_phases_and_routes_clean_slate():
    play = _load(_PLAYBOOKS / "mailcatcher-bootstrap.yml")[0]
    assert play["hosts"] == "localhost" and play["any_errors_fatal"] is True
    blocks = [t for t in play["tasks"] if "block" in t]
    imported = [b["block"][0]["ansible.builtin.import_role"]["name"] for b in blocks]
    assert imported == ["mailcatcher_clean_slate", "mailcatcher_preflight", "mailcatcher_provision",
                       "mailcatcher_bootstrap", "mailcatcher_handoff"]
    assert "not (clean_slate" not in blocks[0]["when"] and "clean_slate" in blocks[0]["when"]
    for b in blocks[1:]:
        assert "not (clean_slate" in b["when"]
    assert play["vars"]["mailcatcher_vmid"] == 20011 == 20 * 1000 + 11


def test_platform_catalog_row_matches_the_orchestrator_and_vmid():
    rows = _defaults("platform_clean_slate")["platform_clean_slate_services"]
    row = next(r for r in rows if r["bootstrap_playbook"].endswith("mailcatcher-bootstrap.yml"))
    assert row["verify_vmids"] == [20011]
    assert row["verify_tf_root"] == "infra/projects/mailcatcher"
    assert row["generated_inventory"] == "ansible/inventory/mailcatcher.generated.yml"
    assert row["proxmox_host_var"] == "mailcatcher_proxmox_host"
    assert "terraform_data.ledger_index_assert" in row["state_ignore_patterns"]


def test_provision_targets_the_mailcatcher_root_and_passes_the_role_scoped_variables():
    text = (_ROLES / "mailcatcher_provision" / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "-var=mailcatcher_vmid=" in text and "-var=mailcatcher_template_file_id=" in text
    assert "-var=mailcatcher_datastore_id=" in text
    d = _defaults("mailcatcher_provision")
    assert d["mailcatcher_tf_root"].endswith("infra/projects/mailcatcher")
    assert set(d["mailcatcher_tf_env"]) >= {"TF_VAR_proxmox_endpoint", "TF_VAR_proxmox_api_token",
                                            "TF_VAR_proxmox_insecure"}


_SVC09_ONLY = ("acme", "letsencrypt", "dashboard", "traefik.yml", "traefik_tls_self_signed",
               "traefik_dashboard", "traefik_metrics", "edge network", "svc09", "svc-09", "1090", "10.0.20.12:")


def test_no_svc09_specific_concepts_survive_in_the_derived_phase_roles():
    """The phase roles were derived from SVC-09's; none of its Traefik-only concepts may remain."""
    for r in ("preflight", "provision", "bootstrap", "handoff", "clean_slate"):
        for p in (_ROLES / f"mailcatcher_{r}").rglob("*.yml"):
            low = _code_lines(p).lower()
            for bad in _SVC09_ONLY:
                assert bad not in low, f"{p.relative_to(_ROOT)} still contains SVC-09-specific '{bad}'"
