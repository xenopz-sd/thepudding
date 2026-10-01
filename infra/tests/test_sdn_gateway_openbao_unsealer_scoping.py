"""Offline (Tier-1) lock-in guards for the OpenBao unsealer seal-API firewall scoping.
Spec: openbao-unsealer-firewall-scoping; ADR-0007 rev 2 (guest-NIC enforcement).

=== WHY THESE EXIST ===
SVC-07 requires that ONLY the OpenBao primary (10.0.20.10) may reach the Transit
unsealer's seal API (10.0.20.11:8200); every other VLAN-20 source must be denied at
the Proxmox firewall. That scoping is implemented as two `pve-firewall` rules in the
`sdn_gateway` role (tasks/openbao_unsealer_scoping.yml). Because the primary and
unsealer share the VLAN-20 bridge `p20`, a CLUSTER firewall rule cannot filter that
intra-VLAN traffic (L2-switched, never hits the host chains); the rules are written
at the UNSEALER GUEST firewall scope (`/nodes/<node>/lxc/<vmid>/firewall/rules`)
instead. Gated OFF by default so merging is a runtime no-op; inert until the
operator applies the unsealer NIC firewall (Terraform) AND enables the pve-firewall
master switch (ADR-0007 rev 2).

These guards parse the committed role artifacts (PyYAML + text) — no live
OpenBao/Proxmox — and lock in:
  * the enable/teardown/master-switch flags default to false (off-by-default;
    merge is a no-op) — Req 3;
  * the scoping task carries an ACCEPT primary->unsealer:8200 rule AND a DROP
    to-unsealer:8200 rule, both managed-comment tagged, ALLOW before DROP — Req 1, 2;
  * neither write carries `failed_when: false` (fail-closed) — Req 1.3 / NFR-2;
  * the scoping task writes at the GUEST firewall scope (/nodes/.../lxc/.../firewall/rules),
    NOT /cluster/firewall/rules — Req 2.1 (correct enforcement point);
  * node + vmid default empty and the task fails closed if unset — no hardcoded guest;
  * main.yml includes the scoping task gated on the enable flag — Req 3.2.

Run:
  PYTHONPATH=infra/platform-foundation/scripts \
    ~/venv/devinfra/bin/pytest infra/tests/test_sdn_gateway_openbao_unsealer_scoping.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "sdn_gateway"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_SCOPING = _ROLE / "tasks" / "openbao_unsealer_scoping.yml"
_TEARDOWN = _ROLE / "tasks" / "openbao_unsealer_scoping_teardown.yml"
_BASELINE = _ROLE / "tasks" / "guest_firewall_baseline.yml"
_BASELINE_TD = _ROLE / "tasks" / "guest_firewall_baseline_teardown.yml"
_MAIN = _ROLE / "tasks" / "main.yml"

_ALLOW_COMMENT = "sdn_gateway: ALLOW openbao primary -> unsealer 8200 (managed)"
_DENY_COMMENT = "sdn_gateway: DENY non-primary -> unsealer 8200 (managed)"


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _argv_of(task: dict) -> list[str]:
    """Return the argv list of a command task, or []."""
    cmd = task.get("ansible.builtin.command") or task.get("command") or {}
    if isinstance(cmd, dict):
        return [str(x) for x in (cmd.get("argv") or [])]
    return []


class TestFlagsOffByDefault:
    """Req 3 — merging the feature must change nothing at runtime."""

    def setup_method(self):
        self.defaults = _load_yaml(_DEFAULTS)

    @pytest.mark.parametrize(
        "flag",
        [
            "sdn_gateway_openbao_unsealer_scoping_enabled",
            "sdn_gateway_openbao_unsealer_scoping_teardown",
            "sdn_gateway_pve_firewall_enable",
        ],
    )
    def test_gate_defaults_false(self, flag):
        assert flag in self.defaults, f"{flag} must be declared in defaults/main.yml"
        assert self.defaults[flag] is False, (
            f"{flag} MUST default to false (off-by-default; merge is a runtime "
            f"no-op / high-blast-radius switch never auto-flipped) — got "
            f"{self.defaults[flag]!r}"
        )

    def test_primary_and_unsealer_ips_declared(self):
        assert self.defaults["sdn_gateway_openbao_primary_ip"] == "10.0.20.10"
        assert self.defaults["sdn_gateway_openbao_unsealer_ip"] == "10.0.20.11"
        assert int(self.defaults["sdn_gateway_openbao_unsealer_seal_port"]) == 8200

    def test_node_and_vmid_default_empty_not_hardcoded(self):
        """node + vmid identify a SPECIFIC guest — they must NOT be hardcoded to a
        node name / VMID; they default empty and the task fails closed if unset,
        sourcing the real values from the Terraform-generated inventory (Fix 1)."""
        assert self.defaults["sdn_gateway_openbao_unsealer_node"] in ("", None), (
            "sdn_gateway_openbao_unsealer_node must default empty (no hardcoded node)"
        )
        assert self.defaults["sdn_gateway_openbao_unsealer_vmid"] in ("", None), (
            "sdn_gateway_openbao_unsealer_vmid must default empty (no hardcoded VMID)"
        )


class TestScopingRules:
    """Req 1, Req 2 — the two scoped rules, ordered ALLOW-before-DROP, fail-closed."""

    def setup_method(self):
        self.tasks = _load_yaml(_SCOPING)
        self.text = _SCOPING.read_text(encoding="utf-8")

    def _find_create_task(self, action: str) -> dict:
        for t in self.tasks:
            argv = _argv_of(t)
            if "create" in argv and f"--action={action}" in argv:
                return t
        raise AssertionError(f"no pvesh create task with --action={action} found")

    def test_allow_primary_to_unsealer_8200(self):
        t = self._find_create_task("ACCEPT")
        argv = _argv_of(t)
        assert "--type=in" in argv
        assert "--proto=tcp" in argv
        assert "--source={{ sdn_gateway_openbao_primary_ip }}" in argv
        assert "--dest={{ sdn_gateway_openbao_unsealer_ip }}" in argv
        assert "--dport={{ sdn_gateway_openbao_unsealer_seal_port }}" in argv
        assert "--comment={{ sdn_gateway_ob_allow_comment }}" in argv
        # the comment var resolves to the managed ALLOW comment literal
        assert _ALLOW_COMMENT in self.text

    def test_drop_any_other_source_to_unsealer_8200(self):
        t = self._find_create_task("DROP")
        argv = _argv_of(t)
        assert "--type=in" in argv
        assert "--proto=tcp" in argv
        assert "--dest={{ sdn_gateway_openbao_unsealer_ip }}" in argv
        assert "--dport={{ sdn_gateway_openbao_unsealer_seal_port }}" in argv
        assert "--comment={{ sdn_gateway_ob_deny_comment }}" in argv
        # the comment var resolves to the managed DENY comment literal
        assert _DENY_COMMENT in self.text
        # the DROP is not scoped to a single source (it denies everyone else)
        assert not any(a.startswith("--source=") for a in argv), (
            "the DROP rule must NOT carry a --source (it denies all other sources)"
        )

    def test_drop_created_before_allow_so_allow_lands_on_top(self):
        """Correct FINAL evaluation order = ALLOW above DROP (Req 2.2).

        `pvesh create /cluster/firewall/rules` IGNORES --pos and always prepends the
        new rule at pos 0 (verified live on the cluster + multiple Proxmox reports).
        So to end up with ALLOW ABOVE DROP, the DROP must be CREATED FIRST and the
        ALLOW CREATED SECOND (the ALLOW is then prepended above the DROP). This test
        locks in that reversed creation order — the earlier "ALLOW appears first in
        the file" assumption produced pos0=DROP/pos1=ALLOW live, which would drop the
        primary's own seal traffic once the firewall is enabled.
        """
        deny_i = self.text.index("--action=DROP")
        allow_i = self.text.index("--action=ACCEPT")
        assert deny_i < allow_i, (
            "the DROP rule must be CREATED before the ALLOW rule in the task file, so "
            "that pvesh's prepend-at-pos-0 leaves the ALLOW above the DROP"
        )

    def test_writes_are_fail_closed(self):
        """No `failed_when: false` on either pvesh create (Req 1.3 / NFR-2)."""
        for t in self.tasks:
            if "create" in _argv_of(t):
                assert "failed_when" not in t, (
                    f"scoping write {t.get('name')!r} must hard-fail — no "
                    f"failed_when override permitted"
                )

    def test_no_secret_material_referenced(self):
        lowered = self.text.lower()
        for needle in ("token", "secret_id", "root_token", "unseal_key"):
            assert needle not in lowered, f"scoping task must reference no {needle!r}"

    def test_rules_written_at_guest_scope_not_cluster(self):
        """Req 2.1 — the enforcement point is the unsealer GUEST NIC firewall.

        A cluster-level rule cannot filter same-bridge intra-VLAN traffic, so the
        rules must target /nodes/<node>/lxc/<vmid>/firewall/rules, not
        /cluster/firewall/rules (ADR-0007 rev 2, the live-found correction).
        """
        # the guest-scoped path is computed from node + vmid vars
        assert "/nodes/{{ sdn_gateway_openbao_unsealer_node }}/lxc/" in self.text
        assert "/firewall/rules" in self.text
        assert "sdn_gateway_openbao_unsealer_vmid" in self.text
        # and the WRITES (the pvesh argv, not the explanatory comments) target that
        # computed guest path, never the cluster scope.
        for action in ("ACCEPT", "DROP"):
            a = _argv_of(self._find_create_task(action))
            assert "{{ sdn_gateway_ob_rules_path }}" in a, (
                f"the {action} create must target the guest-scoped rules path var"
            )
            assert "/cluster/firewall/rules" not in a, (
                f"the {action} create must NOT write to /cluster/firewall/rules — "
                f"that scope cannot filter same-bridge intra-VLAN guest traffic"
            )
        # the read step must also target the guest scope, not the cluster
        read_cmds = [
            (t.get("ansible.builtin.command") or t.get("command") or {}).get("cmd", "")
            for t in self.tasks
        ]
        assert any("{{ sdn_gateway_ob_rules_path }}" in str(c) for c in read_cmds), (
            "the rules read must target the guest-scoped path"
        )
        assert not any("/cluster/firewall/rules" in str(c) for c in read_cmds), (
            "the rules read must NOT target /cluster/firewall/rules"
        )

    def test_fails_closed_when_node_or_vmid_unset(self):
        """The task must assert node+vmid are set before writing (no wrong-guest write)."""
        assert any(
            (t.get("ansible.builtin.assert") or t.get("assert"))
            and "sdn_gateway_openbao_unsealer_vmid" in str(t)
            and "sdn_gateway_openbao_unsealer_node" in str(t)
            for t in self.tasks
        ), "scoping task must fail closed (assert) when node/vmid are unset"


class TestGatedInclude:
    """Req 3.2 — main.yml includes the scoping task gated on the enable flag."""

    def test_main_includes_scoping_gated(self):
        tasks = _load_yaml(_MAIN)
        found = False
        for t in tasks:
            inc = t.get("ansible.builtin.include_tasks") or t.get("include_tasks")
            if inc == "openbao_unsealer_scoping.yml":
                found = True
                when = t.get("when", "")
                assert "sdn_gateway_openbao_unsealer_scoping_enabled" in str(when), (
                    "the scoping include must be gated on "
                    "sdn_gateway_openbao_unsealer_scoping_enabled"
                )
        assert found, "main.yml must include openbao_unsealer_scoping.yml"

    def test_main_includes_teardown_gated(self):
        tasks = _load_yaml(_MAIN)
        for t in tasks:
            inc = t.get("ansible.builtin.include_tasks") or t.get("include_tasks")
            if inc == "openbao_unsealer_scoping_teardown.yml":
                when = str(t.get("when", ""))
                assert "sdn_gateway_openbao_unsealer_scoping_teardown" in when
                return
        raise AssertionError("main.yml must include the teardown task, gated")


class TestTeardownReversible:
    """Req 4 — teardown removes exactly the two managed rules, fail-closed."""

    def setup_method(self):
        self.tasks = _load_yaml(_TEARDOWN)
        self.text = _TEARDOWN.read_text(encoding="utf-8")

    def test_teardown_targets_both_managed_comments(self):
        assert _ALLOW_COMMENT in self.text
        assert _DENY_COMMENT in self.text

    def test_teardown_delete_is_fail_closed(self):
        for t in self.tasks:
            if "delete" in _argv_of(t):
                assert "failed_when" not in t, (
                    "teardown delete must hard-fail — no failed_when override"
                )


class TestGuestFirewallBaselineEgress:
    """Req 8 — a firewalled guest must keep scoped EGRESS so it can still provision.

    Enabling `firewall=1` on a guest NIC (with pve-firewall on) makes Proxmox filter
    that guest's own traffic, which removes its egress unless guest-scoped rules
    exist: DNS, `apt update`, and image pulls all fail, so the guest cannot
    provision itself. Verified live — the firewalled unsealer could not resolve
    deb.debian.org while the non-firewalled primary could, which broke the `common`
    role's apt step and left Docker uninstalled.

    These guards lock in the REUSABLE baseline: it is generic over
    `sdn_gateway_firewalled_guests` (any guest / any VLAN / any future service),
    scoped to an explicit port allowlist rather than a blanket policy_out=ACCEPT,
    fail-closed, and ordered BEFORE the unsealer scoping.
    """

    def setup_method(self):
        self.tasks = _load_yaml(_BASELINE)
        self.text = _BASELINE.read_text(encoding="utf-8")
        self.defaults = _load_yaml(_DEFAULTS)
        self.main = _load_yaml(_MAIN)

    def test_registry_and_port_allowlists_declared(self):
        assert self.defaults["sdn_gateway_firewalled_guests"] == [], (
            "sdn_gateway_firewalled_guests must default to [] (no guest firewalled "
            "out of the box)"
        )
        tcp = self.defaults["sdn_gateway_guest_egress_tcp_ports"]
        udp = self.defaults["sdn_gateway_guest_egress_udp_ports"]
        # DNS + apt/registry egress is what provisioning actually needs.
        for port in (53, 80, 443):
            assert port in tcp, f"tcp/{port} must be in the egress allowlist"
        for port in (53, 123):
            assert port in udp, f"udp/{port} must be in the egress allowlist"

    def test_egress_rules_are_out_accept_and_port_scoped(self):
        """Egress rules must be `out`/ACCEPT and driven by the port allowlists —
        never a blanket policy_out=ACCEPT."""
        creates = [t for t in self.tasks if "create" in _argv_of(t)]
        assert creates, "baseline must create egress rules"
        for t in creates:
            argv = _argv_of(t)
            assert "--type=out" in argv, "egress rules must be type=out"
            assert "--action=ACCEPT" in argv, "egress rules must be ACCEPT"
            assert any(a.startswith("--dport=") for a in argv), (
                "egress rules must be port-scoped (--dport), not blanket"
            )
        # No blanket policy_out=ACCEPT in any actual command (comments may discuss it).
        all_args = " ".join(
            " ".join(_argv_of(t)) + str((t.get("ansible.builtin.command") or t.get("command") or {}).get("cmd", ""))
            for t in self.tasks
        )
        assert "policy_out" not in all_args, (
            "must NOT set a blanket policy_out=ACCEPT — egress is an explicit, "
            "reviewable port allowlist"
        )
        assert "firewall/options" not in all_args, (
            "the baseline must write RULES, not flip guest firewall options wholesale"
        )
        # both protocol families are covered
        assert "--proto=tcp" in self.text and "--proto=udp" in self.text

    def test_generic_over_the_guest_registry_not_hardcoded(self):
        """The baseline must be reusable for ANY guest/VLAN — driven by the loop
        variable, with no OpenBao/VLAN-20-specific or hardcoded node/vmid literal."""
        assert "sdn_gateway_fw_guest.node" in self.text
        assert "sdn_gateway_fw_guest.vmid" in self.text
        lowered = self.text.lower()
        for hardcoded in ("10.0.20.", "1071", "1070", "shrimp"):
            assert hardcoded not in lowered, (
                f"baseline must not hardcode {hardcoded!r} — it is generic over "
                f"sdn_gateway_firewalled_guests"
            )

    def test_writes_are_fail_closed(self):
        for t in self.tasks:
            if "create" in _argv_of(t):
                assert "failed_when" not in t, (
                    "egress baseline writes must hard-fail — no failed_when override"
                )

    def test_main_loops_baseline_before_scoping(self):
        """Egress must be ensured BEFORE the unsealer scoping: a guest needs egress
        to be provisioned at all."""
        includes = [
            t.get("ansible.builtin.include_tasks") or t.get("include_tasks")
            for t in self.main
        ]
        assert "guest_firewall_baseline.yml" in includes, (
            "main.yml must include guest_firewall_baseline.yml"
        )
        assert includes.index("guest_firewall_baseline.yml") < includes.index(
            "openbao_unsealer_scoping.yml"
        ), "the egress baseline must run BEFORE the unsealer scoping"

    def test_baseline_loops_over_the_registry(self):
        for t in self.main:
            inc = t.get("ansible.builtin.include_tasks") or t.get("include_tasks")
            if inc == "guest_firewall_baseline.yml":
                assert "sdn_gateway_firewalled_guests" in str(t.get("loop", "")), (
                    "the baseline include must loop over sdn_gateway_firewalled_guests"
                )
                return
        raise AssertionError("baseline include not found in main.yml")


class TestGuestFirewallBaselineTeardown:
    """ADR-0008 — the egress baseline must be removable (no orphaned rules)."""

    def setup_method(self):
        self.tasks = _load_yaml(_BASELINE_TD)
        self.text = _BASELINE_TD.read_text(encoding="utf-8")
        self.defaults = _load_yaml(_DEFAULTS)
        self.main = _load_yaml(_MAIN)

    def test_teardown_list_defaults_empty(self):
        assert self.defaults["sdn_gateway_firewalled_guests_teardown"] == [], (
            "teardown list must default to [] (removal is a deliberate opt-in)"
        )

    def test_teardown_removes_only_managed_egress_rules(self):
        # keys deletion on the managed egress comment prefix, fail-closed
        assert "ALLOW egress" in self.text
        deletes = [t for t in self.tasks if "delete" in _argv_of(t)]
        assert deletes, "teardown must delete rules"
        for t in deletes:
            assert "failed_when" not in t, "teardown delete must hard-fail"

    def test_main_wires_teardown_loop(self):
        for t in self.main:
            inc = t.get("ansible.builtin.include_tasks") or t.get("include_tasks")
            if inc == "guest_firewall_baseline_teardown.yml":
                assert "sdn_gateway_firewalled_guests_teardown" in str(t.get("loop", ""))
                return
        raise AssertionError("main.yml must include the baseline teardown, looped")
