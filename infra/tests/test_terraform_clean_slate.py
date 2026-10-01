"""Offline (Tier-1) lock-in guards for the terraform_clean_slate reset role.
Spec: terraform-clean-slate-reset.

The canonical clean-slate reset (`pct destroy` + `terraform state rm`) is a
DESTRUCTIVE dev tool that routes around the guests' prevent_destroy seatbelt. These
guards parse the committed role artifacts (PyYAML) — no live cluster — and lock in:
off-by-default; destroy gated on the flag AND a matching per-guest confirm token;
pct-destroy ORDERED BEFORE terraform state rm; state rm delegated to localhost; and
NO terraform apply/destroy anywhere in the role (the recreate is a separate step).

Run:
  PYTHONPATH=infra/platform-foundation/scripts \
    ~/venv/devinfra/bin/pytest infra/tests/test_terraform_clean_slate.py -q
"""
from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "terraform_clean_slate"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_MAIN = _ROLE / "tasks" / "main.yml"
_RESET = _ROLE / "tasks" / "reset_target.yml"
_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "clean-slate.yml"


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _cmd(task: dict) -> str:
    c = task.get("ansible.builtin.command") or task.get("command") or {}
    if isinstance(c, dict):
        return str(c.get("cmd", "")) + " " + " ".join(str(x) for x in (c.get("argv") or []))
    return str(c)


class TestDefaults:
    def setup_method(self):
        self.d = _load(_DEFAULTS)

    def test_enabled_defaults_false(self):
        assert self.d["clean_slate_enabled"] is False, (
            "clean_slate_enabled MUST default false (destructive tool, opt-in)"
        )

    def test_confirm_defaults_empty(self):
        assert self.d["clean_slate_confirm"] in ("", None), (
            "clean_slate_confirm MUST default empty (mandatory per-run token)"
        )

    def test_targets_default_empty(self):
        assert self.d["clean_slate_targets"] == [], "no target destroyed by default"


class TestGatingAndGuards:
    def setup_method(self):
        self.main = _load(_MAIN)
        self.reset = _load(_RESET)
        self.reset_text = _RESET.read_text(encoding="utf-8")

    def test_reset_loop_gated_on_enabled_flag(self):
        inc = next(
            (t for t in self.main
             if (t.get("ansible.builtin.include_tasks") or t.get("include_tasks")) == "reset_target.yml"),
            None,
        )
        assert inc is not None, "main.yml must include reset_target.yml in a loop"
        assert "clean_slate_enabled" in str(inc.get("when", "")), (
            "the reset loop must be gated on clean_slate_enabled"
        )

    def test_confirm_token_asserted_against_target_vmid(self):
        # an assert task must compare clean_slate_confirm to the target vmid
        asserts = [t for t in self.reset if (t.get("ansible.builtin.assert") or t.get("assert"))]
        joined = yaml.safe_dump(asserts)
        assert "clean_slate_confirm" in joined and "clean_slate_target.vmid" in joined, (
            "a fail-closed assert must require clean_slate_confirm == target vmid"
        )

    def test_target_fields_asserted_present(self):
        joined = yaml.safe_dump([t for t in self.reset if (t.get("ansible.builtin.assert") or t.get("assert"))])
        for field in ("terraform_root", "resource_address", "node", "vmid"):
            assert field in joined, f"a fail-closed assert must require target.{field}"


class TestOrderingAndScope:
    def setup_method(self):
        self.reset_text = _RESET.read_text(encoding="utf-8")
        self.reset = _load(_RESET)

    def test_pct_destroy_before_state_rm(self):
        pct_i = self.reset_text.index("pct destroy")
        rm_i = self.reset_text.index("terraform state rm")
        assert pct_i < rm_i, (
            "pct destroy must run BEFORE terraform state rm (fail-safe: a pct failure "
            "must not leave a state-rm'd-but-still-running orphan)"
        )

    def test_state_rm_delegated_to_localhost(self):
        rm = next(
            (t for t in self.reset if "terraform state rm" in _cmd(t)),
            None,
        )
        assert rm is not None, "there must be a terraform state rm task"
        assert rm.get("delegate_to") == "localhost", (
            "terraform state rm must be delegated to localhost (state lives on the control node)"
        )
        # It must NOT inherit the play's become:true (that runs pct on the Proxmox
        # host). A control-node state op needs no root; inheriting become fails with
        # "sudo: a password is required" on the control node (live regression).
        assert rm.get("become") is False, (
            "terraform state rm (delegate_to localhost) must set become: false — it "
            "runs as the invoking user on the control node, not root"
        )

    def test_state_rm_chdir_is_absolute_not_relative(self):
        """The delegated state rm must chdir to an ABSOLUTE path. terraform_root is
        repo-relative; a relative chdir on localhost from an undefined cwd fails
        ("Unable to change directory before execution") — the live bug. The role
        resolves it against the repo root (playbook_dir|dirname|dirname)."""
        text = self.reset_text
        assert "clean_slate_tf_root_abs" in text, (
            "the role must resolve terraform_root to an absolute path before chdir"
        )
        # the resolution derives the repo root from playbook_dir (two levels up)
        assert "playbook_dir | dirname | dirname" in text, (
            "absolute resolution must be repo-root-relative (playbook_dir/../..)"
        )
        # and the state rm chdirs to the RESOLVED absolute var, not the raw relative one
        rm = next(t for t in self.reset if "terraform state rm" in _cmd(t))
        chdir = (rm.get("ansible.builtin.command") or rm.get("command") or {}).get("chdir", "")
        assert "clean_slate_tf_root_abs" in str(chdir), (
            "state rm chdir must use the resolved absolute path"
        )

    def test_state_rm_failed_when_is_defensive(self):
        """failed_when must guard .rc with a default: when the module fails to START
        (bad chdir), the result has no `rc` and a bare `.rc` raises 'dict has no
        attribute rc' instead of failing cleanly (the live bug)."""
        rm = next(t for t in self.reset if "terraform state rm" in _cmd(t))
        fw = " ".join(str(x) for x in (rm.get("failed_when") or []))
        assert "rc | default" in fw or "rc|default" in fw, (
            "failed_when must reference `clean_slate_state_rm.rc | default(...)`, not a bare .rc"
        )

    def test_no_terraform_apply_or_destroy_in_role(self):
        for f in (_MAIN, _RESET):
            text = f.read_text(encoding="utf-8")
            # allow the words in comments; forbid an actual apply/destroy COMMAND
            for t in _load(f):
                cmd = _cmd(t)
                assert "terraform apply" not in cmd, f"{f.name}: role must NOT run terraform apply (recreate is a separate step)"
                assert "terraform destroy" not in cmd, f"{f.name}: role must NOT run terraform destroy (prevent_destroy seatbelt kept; uses pct destroy + state rm)"


class TestPlaybook:
    def test_playbook_targets_proxmox_and_applies_role(self):
        plays = _load(_PLAYBOOK)
        play = plays[0]
        assert play.get("hosts") == "proxmox", "clean-slate.yml must target the proxmox host inventory"
        roles = [r.get("role") if isinstance(r, dict) else r for r in play.get("roles", [])]
        assert "terraform_clean_slate" in roles


class TestPreventDestroyStillLiteralTrue:
    """The seatbelt on the SVC-07 containers must stay a literal true (Req 6)."""

    def test_svc07_containers_keep_prevent_destroy_true(self):
        main_tf = (_REPO_ROOT / "infra" / "projects" / "svc-07-secrets-manager" / "main.tf").read_text(encoding="utf-8")
        assert main_tf.count("prevent_destroy = true") >= 2, (
            "both SVC-07 containers must keep `prevent_destroy = true` (literal) — the "
            "clean-slate routes around it via pct destroy + state rm, never by disabling it"
        )
