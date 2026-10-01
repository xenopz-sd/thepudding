"""Offline structural-contract tests for the task-6.2 bootstrap flow.

Task 6.2 (spec: svc-07-secrets-manager) — requirements.md Requirements 5.4, 5.5;
Security AC 1, 2, 7; design.md "Init/unseal/bootstrap sequence" and "Open Risks
(bootstrap chicken-and-egg)".

These are OFFLINE, dependency-light assertions over the ``openbao_init_unseal``
role's task-6.2 additions — ``tasks/primary_bootstrap.yml``, its include wiring
in ``tasks/main.yml``, the ``platform-admin.hcl.j2`` policy, and the role
defaults. They are NOT property-based tests and NOT ``requires_infra``: there is
no live OpenBao/Docker here. They pin the security-critical contract that the
task's brief and the secrets-manager steering demand:

  * the Bootstrap_Transit_Token is sourced from an operator-supplied,
    never-committed EXTERNAL location (an env var populated by a gitignored .env
    OR a GitLab CI protected+masked variable) — never a defaulted/committed
    literal (Req 5.4; Security AC 1, 7);
  * every task that handles the token / Root_Token / KV-writer token / API token
    sets ``no_log: true`` (Security discipline);
  * the init -> bootstrap-platform-admin -> (task-7.x window) -> revoke ->
    rotate-into-KV -> remove-external-copy ORDERING holds, with the revoke
    strictly after the platform-admin bootstrap and the task-7.x insertion point
    strictly BEFORE the revoke (ordering contract for task 7.x);
  * the external copy is removed once the KV copy is live, and the "cannot reach
    the source" path FAILS LOUDLY rather than silently skipping (Req 5.5;
    Security AC 7);
  * the primary-init steps are gated on ``openbao_role == "primary"`` AND
    ``not openbao_already_initialized`` (Req 13.3 idempotency);
  * no secret literal appears anywhere in the role or its templates.

The whole suite is offline and always runs (no gating).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_bootstrap_flow.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_MAIN = _ROLE_DIR / "tasks" / "main.yml"
_BOOTSTRAP = _ROLE_DIR / "tasks" / "primary_bootstrap.yml"
_ADMIN_TMPL = _ROLE_DIR / "templates" / "platform-admin.hcl.j2"

#: Variable names that carry secret material at runtime. Any task whose body
#: references one of these MUST set no_log: true.
_SECRET_REFS = (
    "openbao_bootstrap_token",
    "openbao_root_token",
    "openbao_kv_writer_token",
    "BAO_TOKEN=",
    "PRIVATE-TOKEN",
    "client_token",
    "root_token",
    "openbao_gitlab_token_env_var",
)


# --- 30th correction: no_log may be True OR the dev-opt-in template that hides by
# default. `no_log: "{{ openbao_no_log }}"` resolves to True unless a developer
# passes -e openbao_dev_expose_secrets=true for a single debugging run
# (security-standards.md "Secrets handling"). Both forms satisfy the
# "secrets hidden by default" discipline.
_NO_LOG_HIDDEN_BY_DEFAULT = (True, "{{ openbao_no_log }}")


def _no_log_hidden(task: dict) -> bool:
    return task.get("no_log") in _NO_LOG_HIDDEN_BY_DEFAULT


@pytest.fixture(scope="module")
def bootstrap_tasks() -> list[dict]:
    tasks = yaml.safe_load(_BOOTSTRAP.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and tasks, "primary_bootstrap.yml must be a task list"
    return tasks


@pytest.fixture(scope="module")
def defaults() -> dict:
    raw = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw
    return raw


def _task_body_str(task: dict) -> str:
    """YAML-dump a task minus its no_log flag, for secret-reference scanning."""
    return yaml.safe_dump({k: v for k, v in task.items() if k != "no_log"})


def _names(tasks: list[dict]) -> list[str]:
    return [t.get("name", "") for t in tasks]


def _index_of(tasks: list[dict], needle: str) -> int:
    for i, t in enumerate(tasks):
        if needle in t.get("name", ""):
            return i
    raise AssertionError(f"no task whose name contains {needle!r}; names={_names(tasks)}")


class TestTokenSourcingAndSecrecy:
    """Req 5.4; Security AC 1, 7 — external, never-committed source; no_log."""

    def test_token_never_defaulted(self, defaults):
        """No token value is defaulted anywhere in the role's defaults."""
        assert "openbao_transit_token" not in defaults
        assert "openbao_bootstrap_token" not in defaults
        # Only the ENV VAR NAME is defaulted, not a value.
        assert defaults["openbao_bootstrap_token_env_var"] == "OPENBAO_BOOTSTRAP_TRANSIT_TOKEN"

    def test_token_read_from_env_lookup(self, bootstrap_tasks):
        """The token is resolved via an env lookup of the configured var name."""
        idx = _index_of(bootstrap_tasks, "Resolve Bootstrap_Transit_Token")
        body = yaml.safe_dump(bootstrap_tasks[idx])
        assert "lookup" in body and "env" in body and "openbao_bootstrap_token_env_var" in body
        # It must NOT hardcode a specific var name literal or a value.
        assert _no_log_hidden(bootstrap_tasks[idx])

    def test_fails_closed_when_no_external_token(self, bootstrap_tasks):
        """An assert fails the run if no external source supplied the token."""
        idx = _index_of(bootstrap_tasks, "Assert the Bootstrap_Transit_Token was supplied")
        task = bootstrap_tasks[idx]
        assert "ansible.builtin.assert" in task
        that = task["ansible.builtin.assert"]["that"]
        assert any("length > 0" in c for c in that), that

    def test_no_log_hidden_by_default_flag(self):
        """openbao_dev_expose_secrets defaults false -> no_log hidden (30th).

        security-standards.md: secrets must be hidden by default; a developer may
        expose them for a single debugging run via -e openbao_dev_expose_secrets=
        true (ephemeral tokens only), never a committed default. Assert the role
        default keeps secrets hidden and the derived no_log negates the opt-in.
        """
        import yaml as _yaml
        defaults = _yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
        assert defaults.get("openbao_dev_expose_secrets") is False, (
            "openbao_dev_expose_secrets MUST default false so no_log stays on "
            "(secrets hidden) for acceptance/production"
        )
        # openbao_no_log is the negation of the opt-in, so default -> True (hidden).
        assert "not (openbao_dev_expose_secrets" in str(defaults.get("openbao_no_log")), (
            "openbao_no_log must derive from `not openbao_dev_expose_secrets` so "
            "the default hides secrets and only the explicit opt-in exposes them"
        )

    def test_every_secret_touching_task_sets_no_log(self, bootstrap_tasks):
        """Security discipline: no_log hidden-by-default on every token-handling task."""
        offenders = []
        for t in bootstrap_tasks:
            body = _task_body_str(t)
            touches = any(ref in body for ref in _SECRET_REFS)
            # The "fail loudly" reminder references only var NAMES + KV path, no
            # value, and must stay visible to the operator — exempt it explicitly.
            is_fail_reminder = "ansible.builtin.fail" in t
            if touches and not is_fail_reminder and not _no_log_hidden(t):
                offenders.append(t.get("name"))
        assert not offenders, f"secret-touching tasks missing no_log:true: {offenders}"

    def test_no_secret_literal_in_role_or_templates(self):
        """No real-looking token literal anywhere in the role tree."""
        pat = re.compile(r"(hvs\.[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9]{15,}|s\.[A-Za-z0-9]{20,})")
        for path in _ROLE_DIR.rglob("*"):
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".md"}:
                assert not pat.search(path.read_text(encoding="utf-8")), (
                    f"real-looking secret literal in {path}"
                )


class TestPolicyStagingOutsideConfigDir:
    """Policy HCL must NOT be staged in openbao_config_dir (27th correction).

    OpenBao's entrypoint loads the WHOLE `/openbao/config` dir as server config
    (`server -config=/openbao/config`), and that dir is bind-mounted into the
    container. A policy `.hcl` rendered there is parsed as server config and
    crash-loops the server (`error loading configuration ... platform-admin.hcl:
    permission denied`), which then fails the primary's `operator init`. The
    platform-admin policy must be staged in openbao_policy_staging_dir
    (/etc/openbao), NOT the config bind-mount.
    """

    def test_no_policy_hcl_rendered_or_copied_into_config_dir(self, bootstrap_tasks):
        offenders = []
        for tk in bootstrap_tasks:
            body = _task_body_str(tk)
            # A template dest or docker cp source pointing a *.hcl at the config
            # dir is the bug. The one legitimate config-dir file is config.hcl
            # (rendered by openbao_install, not here).
            if "openbao_config_dir" in body and "platform-admin.hcl" in body:
                offenders.append(tk.get("name"))
        assert not offenders, (
            "policy HCL must be staged in openbao_policy_staging_dir, not "
            f"openbao_config_dir (OpenBao loads that dir as server config): {offenders}"
        )

    def test_platform_admin_policy_staged_in_staging_dir(self, bootstrap_tasks):
        idx = _index_of(bootstrap_tasks, "Render the platform-admin policy HCL")
        dest = _task_body_str(bootstrap_tasks[idx])
        assert "openbao_policy_staging_dir" in dest, (
            "the platform-admin policy must render to openbao_policy_staging_dir "
            "(/etc/openbao), not the config bind-mount"
        )


class TestDockerExecStdin:
    """Any bao call fed via `stdin` must run `docker exec -i` (26th correction).

    `bao policy write <name> -` reads the policy HCL from stdin. `docker exec`
    forwards the parent's stdin into the container ONLY with `-i`; without it the
    container's `bao` sees empty stdin and OpenBao returns
    `400: 'policy' parameter not supplied or empty` (verified live). Guards every
    bootstrap task that pairs an `ansible.builtin.command` `stdin:` with a
    `docker exec` argv.
    """

    def test_every_stdin_fed_docker_exec_has_dash_i(self, bootstrap_tasks):
        offenders = []
        for tk in bootstrap_tasks:
            cmd = tk.get("ansible.builtin.command")
            if not isinstance(cmd, dict):
                continue
            if "stdin" not in cmd:
                continue
            argv = cmd.get("argv", "")
            argv_str = argv if isinstance(argv, str) else " ".join(map(str, argv))
            if "docker" in argv_str and "exec" in argv_str:
                # The rendered argv must include a standalone -i token for docker exec.
                if "'-i'" not in argv_str and '"-i"' not in argv_str and " -i " not in argv_str:
                    offenders.append(tk.get("name"))
        assert not offenders, (
            "stdin-fed `docker exec` tasks missing `-i` (stdin never reaches the "
            f"container -> `bao` gets empty input): {offenders}"
        )


class TestBootstrapOrdering:
    """design's Init/unseal/bootstrap sequence + the task-7.x ordering contract."""

    def test_canonical_order(self, bootstrap_tasks):
        """init < platform-admin < KV-writer mint < revoke < KV rotate-in < removal."""
        i_init = _index_of(bootstrap_tasks, "bao operator init")
        i_admin = _index_of(bootstrap_tasks, "Write the platform-admin policy")
        i_mint = _index_of(bootstrap_tasks, "Mint the short-lived")
        i_revoke = _index_of(bootstrap_tasks, "Revoke the Root_Token")
        i_rotate = _index_of(bootstrap_tasks, "Write the Bootstrap_Transit_Token into KV")
        i_remove_env = _index_of(bootstrap_tasks, "Remove the token line from the local")
        assert i_init < i_admin < i_mint < i_revoke < i_rotate < i_remove_env, (
            f"bad order: init={i_init} admin={i_admin} mint={i_mint} "
            f"revoke={i_revoke} rotate={i_rotate} remove={i_remove_env}"
        )

    def test_task7x_extension_point_is_before_the_revoke(self):
        """The task-7.x marker sits between platform-admin bootstrap and revoke."""
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        marker = "EXTENSION POINT — Task 7.x"
        revoke = "Revoke the Root_Token"
        assert marker in text, "task-7.x extension marker missing from primary_bootstrap.yml"
        assert text.index(marker) < text.index(revoke), (
            "task-7.x engine/auth/audit block must run BEFORE the Root_Token revoke "
            "(it needs the Root_Token) — ordering contract violated"
        )

    def test_root_token_scrubbed_after_revoke(self, bootstrap_tasks):
        """The Root_Token fact is emptied right after the revoke."""
        i_revoke = _index_of(bootstrap_tasks, "Revoke the Root_Token")
        i_scrub = _index_of(bootstrap_tasks, "Scrub the Root_Token")
        assert i_revoke < i_scrub
        scrub = bootstrap_tasks[i_scrub]
        assert scrub["ansible.builtin.set_fact"]["openbao_root_token"] == ""


class TestAutoUnsealWaitProbe:
    """The auto-unseal wait must poll INSIDE the primary container, not the host.

    Regression guard for the 22nd correction: the primary publishes NO host port
    (Traefik-labels-only, PF FR-7), so OpenBao listens only on the container
    network. Polling auto-unseal via `ansible.builtin.uri` against
    `openbao_primary_addr` (https://10.0.20.10:8200) runs on the primary LXC
    HOST, where nothing listens on that address — the request never yields a
    parseable body, so the wait times out even though the primary IS unsealed
    (observed live: `.json` undefined, assert `... .json is defined` failed).
    The wait+assert MUST instead read `bao status` via `docker exec` (the proven
    in-container path every other primary `bao` call uses).
    """

    def test_wait_polls_bao_status_via_docker_exec_not_host_uri(self, bootstrap_tasks):
        idx = _index_of(bootstrap_tasks, "Wait for primary initialized:true and sealed:false")
        task = bootstrap_tasks[idx]
        # It must be a command running `docker exec ... bao status`, NOT a uri.
        assert "ansible.builtin.command" in task, (
            "the auto-unseal wait must use ansible.builtin.command (docker exec), "
            "not ansible.builtin.uri against the host-unreachable primary address"
        )
        assert "ansible.builtin.uri" not in task
        argv = yaml.safe_dump(task["ansible.builtin.command"]["argv"])
        assert "docker" in argv and "exec" in argv, "must run via docker exec"
        assert "openbao_primary_container_name" in argv, "must target the primary container"
        assert "status" in argv, "must poll `bao status`"
        # It must NOT reach for the host-side HTTP address that has no listener.
        assert "openbao_primary_addr" not in _task_body_str(task), (
            "the wait must not target openbao_primary_addr — the primary publishes "
            "no host port, so that address is unreachable from where the task runs"
        )
        # Readiness is decided from the parsed JSON, tolerating `bao status`'s
        # non-zero rc when sealed (rc=2) rather than treating it as failure.
        assert task.get("failed_when") is False or task.get("failed_when") == "false", (
            "sealed `bao status` exits non-zero while auto-unseal settles — that "
            "is an in-progress state, so failed_when must be false"
        )
        until = yaml.safe_dump(task["until"])
        assert "sealed" in until and "initialized" in until and "from_json" in until

    def test_assert_reads_the_same_docker_exec_status(self, bootstrap_tasks):
        idx = _index_of(bootstrap_tasks, "Assert the primary reached initialized:true and sealed:false")
        task = bootstrap_tasks[idx]
        that = yaml.safe_dump(task["ansible.builtin.assert"]["that"])
        # The assert must evaluate the docker-exec status result, not the old uri json.
        assert "openbao_primary_status" in that
        assert "openbao_primary_health" not in that, (
            "the assert must read the docker-exec `bao status` result "
            "(openbao_primary_status), not the removed host-uri health probe"
        )
        assert "initialized" in that and "sealed" in that


class TestExternalCopyRemoval:
    """Req 5.5; Security AC 7 — external copy removed once KV copy is live."""

    def test_dotenv_and_gitlab_removal_paths_exist(self, bootstrap_tasks):
        i_env = _index_of(bootstrap_tasks, "Remove the token line from the local")
        i_gl = _index_of(bootstrap_tasks, "Delete the GitLab CI/CD protected variable")
        env_task = bootstrap_tasks[i_env]
        gl_task = bootstrap_tasks[i_gl]
        # .env removal uses lineinfile state:absent on the token var.
        assert env_task["ansible.builtin.lineinfile"]["state"] == "absent"
        assert "dotenv" in yaml.safe_dump(env_task["when"])
        # GitLab path deletes the variable via API DELETE.
        assert gl_task["ansible.builtin.uri"]["method"] == "DELETE"
        assert "gitlab" in yaml.safe_dump(gl_task["when"])

    def test_external_copy_removal_runs_on_control_node(self, bootstrap_tasks):
        """External-copy detect + removal must delegate_to localhost (33rd).

        The homelab .env (and the CI_* vars) live on the CONTROL NODE, not the
        primary LXC. The detect step's `is exists` and the `.env` lineinfile must
        therefore run on localhost — without delegation they tested/edited the
        (absent) path on the primary and always fell through to `manual`,
        fail-looding even on a correct homelab bring-up.
        """
        detect = bootstrap_tasks[_index_of(bootstrap_tasks, "Determine the external-copy removal path")]
        env_task = bootstrap_tasks[_index_of(bootstrap_tasks, "Remove the token line from the local")]
        gl_task = bootstrap_tasks[_index_of(bootstrap_tasks, "Delete the GitLab CI/CD protected variable")]
        assert detect.get("delegate_to") == "localhost", (
            "the external-copy source detection must run on the control node "
            "(where the .env / CI_* vars are)"
        )
        assert env_task.get("delegate_to") == "localhost", (
            "the .env token-line removal must run on the control node"
        )
        assert gl_task.get("delegate_to") == "localhost"

    def test_localhost_delegated_removal_disables_become(self, bootstrap_tasks):
        """The localhost-delegated external-copy tasks must set become: false (34th).

        The primary play runs become: true (root on the LXC). A delegate_to:
        localhost task inherits that, so it tries `sudo` on the CONTROL NODE and
        fails `sudo: a password is required` (the .env is owned by the invoking
        user; no passwordless root on the workstation). These control-node tasks
        must opt out of become.
        """
        for name in (
            "Determine the external-copy removal path",
            "Remove the token line from the local",
            "Delete the GitLab CI/CD protected variable",
        ):
            tk = bootstrap_tasks[_index_of(bootstrap_tasks, name)]
            assert tk.get("delegate_to") == "localhost", name
            assert tk.get("become") is False, (
                f"{name!r} delegates to localhost and must set become: false — "
                "inheriting the play's become:true fails with `sudo: a password "
                "is required` on the control node"
            )

    def test_dotenv_default_points_at_repo_root(self):
        """The homelab .env default resolves to the repo-root .env (33rd)."""
        import yaml as _yaml
        defaults = _yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
        path = str(defaults.get("openbao_bootstrap_dotenv_path", ""))
        # repo root is two levels up from playbook_dir (ansible/playbooks/) — the
        # runbook's `echo ... >> .env` is run from the repo root.
        assert "playbook_dir" in path and "../../.env" in path, (
            "openbao_bootstrap_dotenv_path must point at the repo-root .env "
            f"({{ playbook_dir }}/../../.env), got {path!r}"
        )

    def test_manual_path_fails_loudly(self, bootstrap_tasks):
        """If the source can't be reached, the role fails rather than skipping."""
        i_fail = _index_of(bootstrap_tasks, "Fail loudly if the external copy")
        task = bootstrap_tasks[i_fail]
        assert "ansible.builtin.fail" in task
        assert "manual" in yaml.safe_dump(task["when"])

    def test_removal_happens_after_kv_write(self, bootstrap_tasks):
        """Removal must be gated on the KV copy already being written."""
        i_rotate = _index_of(bootstrap_tasks, "Write the Bootstrap_Transit_Token into KV")
        i_env = _index_of(bootstrap_tasks, "Remove the token line from the local")
        i_gl = _index_of(bootstrap_tasks, "Delete the GitLab CI/CD protected variable")
        assert i_rotate < i_env and i_rotate < i_gl


class TestGatingAndWiring:
    """Req 13.3 idempotency gating + the include wiring in main.yml."""

    def test_include_is_gated_primary_and_not_initialized(self):
        tasks = yaml.safe_load(_MAIN.read_text(encoding="utf-8"))
        inc = next(
            t for t in tasks
            if t.get("ansible.builtin.include_tasks") == "primary_bootstrap.yml"
        )
        when = yaml.safe_dump(inc["when"])
        assert 'openbao_role == "primary"' in when
        assert "not openbao_already_initialized" in when

    def test_task7x_marker_preserved_in_main(self):
        """The Task 7.x signpost is still present in main.yml, intact."""
        text = _MAIN.read_text(encoding="utf-8")
        assert "EXTENSION POINT — Task 7.x" in text
        # And it points implementers at the in-window insertion site.
        assert "primary_bootstrap.yml" in text

    def test_kv_destination_path_is_the_spec_path(self, defaults):
        assert defaults["openbao_kv_mount"] == "secret"
        assert defaults["openbao_transit_token_kv_path"] == "platform/svc07/transit-token"


class TestPlatformAdminPolicy:
    """Security AC 2 — a break-glass platform-admin policy is bootstrapped."""

    def test_policy_template_exists_and_is_acl_only(self):
        text = _ADMIN_TMPL.read_text(encoding="utf-8")
        assert 'path "sys/policies/acl/*"' in text
        # It is an ACL doc — no token/secret value templated in.
        assert "token" not in text.lower() or "Root_Token" in text  # comment mentions only
        pat = re.compile(r"(hvs\.[A-Za-z0-9]{8,}|s\.[A-Za-z0-9]{20,})")
        assert not pat.search(text)
