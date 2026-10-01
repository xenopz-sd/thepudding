"""Offline (Tier-1) lock-in guards for the opt-in platform-admin token mint.
Spec: openbao-admin-token-bootstrap.

=== WHY THESE EXIST ===
The day-2 `openbao_project_onboard` role reads a platform-admin token from
OPENBAO_ADMIN_TOKEN, but the primary bootstrap writes the platform-admin POLICY and
never mints a TOKEN bound to it — and the production way to get one (OIDC -> ZITADEL)
is unavailable in a homelab bring-up, so onboarding was stranded. The fix adds an
OPT-IN (default OFF) mint inside the primary bootstrap's root-token window, bound to
the existing platform-admin policy, no_log-gated, surfaced only under the dev-expose
opt-in, ordered before the Root_Token revoke.

These guards parse the committed role artifacts (PyYAML + text) — no live OpenBao —
and lock in: off-by-default; policy-bound (not root); no_log-gated; revealed only
under openbao_dev_expose_secrets; and ordered BEFORE the Root_Token revoke.

Run:
  PYTHONPATH=infra/platform-foundation/scripts \
    ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_admin_token_mint.py -q
"""
from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_BOOTSTRAP = _ROLE / "tasks" / "primary_bootstrap.yml"


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _argv_str(task: dict) -> str:
    cmd = task.get("ansible.builtin.command") or task.get("command") or {}
    if isinstance(cmd, dict):
        return " ".join(str(x) for x in (cmd.get("argv") or [])) + " " + str(cmd.get("cmd", ""))
    return ""


def _mint_task(tasks: list[dict]) -> dict:
    for t in tasks:
        if "Mint the opt-in platform-admin token" in str(t.get("name", "")):
            return t
    raise AssertionError("platform-admin mint task not found in primary_bootstrap.yml")


class TestOffByDefault:
    def test_flag_defaults_false(self):
        d = _load(_DEFAULTS)
        assert d["openbao_mint_admin_token"] is False, (
            "openbao_mint_admin_token MUST default false — production uses OIDC/ZITADEL "
            "and never mints a static admin token"
        )

    def test_ttl_declared_and_renewable_value(self):
        d = _load(_DEFAULTS)
        assert str(d["openbao_admin_token_ttl"]).endswith("h"), "ttl should be an hours value"


class TestMintShape:
    def setup_method(self):
        self.tasks = _load(_BOOTSTRAP)
        self.text = _BOOTSTRAP.read_text(encoding="utf-8")
        self.mint = _mint_task(self.tasks)

    def test_gated_on_the_optin_flag(self):
        assert "openbao_mint_admin_token" in str(self.mint.get("when", "")), (
            "the mint must be gated on openbao_mint_admin_token"
        )

    def test_bound_to_platform_admin_policy_not_root(self):
        # the argv is a Jinja folded scalar (a template string, not a parsed list),
        # so assert on the raw text of the mint task's command block.
        block = self.text[self.text.index("Mint the opt-in platform-admin token"):]
        block = block[: block.index("Capture the platform-admin token")]
        assert "'token', 'create'" in block, "the mint must run `bao token create`"
        assert "-policy=" in block and "openbao_platform_admin_policy_name" in block, (
            "the mint must bind the token to the platform-admin policy"
        )
        assert "'-renewable=true'" in block, "the day-2 admin token must be renewable"
        assert "'-orphan'" in block, (
            "the mint MUST use -orphan — otherwise the token is a child of the "
            "Root_Token and the STEP-5 root revoke cascade-revokes it (403 on "
            "lookup-self moments after minting — verified live)"
        )
        # created FROM the root token (BAO_TOKEN=openbao_root_token) but NOT bound to
        # a 'root' policy.
        assert "openbao_root_token" in block, "the mint authenticates with the Root_Token"
        assert "-policy=root" not in block, "the token must NOT be bound to root"

    def test_mint_and_surface_are_no_log_gated(self):
        # the mint + capture + reveal tasks carry no_log gated on openbao_no_log
        relevant = [
            t for t in self.tasks
            if "platform-admin token" in str(t.get("name", ""))
        ]
        assert relevant, "expected the mint/capture/surface tasks"
        for t in relevant:
            # debug reveal + command mint + set_fact capture must be no_log-gated,
            # EXCEPT the value-free "value hidden" report which need not be.
            if "value hidden" in str(t.get("name", "")).lower():
                continue
            assert "openbao_no_log" in str(t.get("no_log", "")), (
                f"{t.get('name')!r} must be no_log: {{{{ openbao_no_log }}}}"
            )

    def test_value_revealed_only_under_dev_expose(self):
        reveal = next(
            (t for t in self.tasks if "Reveal the platform-admin token" in str(t.get("name", ""))),
            None,
        )
        assert reveal is not None, "expected a dev-expose reveal task"
        when = str(reveal.get("when", ""))
        assert "openbao_dev_expose_secrets" in when, (
            "the token VALUE must be revealed only under openbao_dev_expose_secrets"
        )
        # and the reveal message actually carries the token value var
        msg = str((reveal.get("ansible.builtin.debug") or reveal.get("debug") or {}).get("msg", ""))
        assert "openbao_admin_token_value" in msg

    def test_mint_ordered_before_root_token_revoke(self):
        mint_i = self.text.index("Mint the opt-in platform-admin token")
        revoke_i = self.text.index("Revoke the Root_Token")
        assert mint_i < revoke_i, (
            "the platform-admin mint must occur BEFORE the STEP-5 Root_Token revoke "
            "(it needs the live Root_Token)"
        )

    def test_token_fact_is_scrubbed(self):
        assert "Scrub the platform-admin token fact" in self.text, (
            "the minted token fact must be scrubbed from memory after surfacing"
        )
