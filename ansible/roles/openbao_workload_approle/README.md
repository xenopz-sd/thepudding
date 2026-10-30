# `openbao_workload_approle` Ansible role

Control-plane role for the **openbao-workload-auth** feature — the downstream
workload→OpenBao authentication path SVC-07's server-side spec deliberately left
out. For one project **slug** (read READ-ONLY from `projects.yaml`) it writes to
the **primary** OpenBao instance via `docker exec` to enable the **AppRole** auth
method, create a per-project **Workload_AppRole**, and hand off its
`role_id`/`secret_id` over the sanctioned first-secret channel. It is idempotent
on its control-plane writes — a re-run leaves `approle/` and the role unchanged
(`changed=0`) — and it fails closed everywhere a credential or dependency is
missing.

It runs **day-2**, after [`openbao_install`](../openbao_install/README.md) and
[`openbao_init_unseal`](../openbao_init_unseal/README.md) have brought up,
initialised/unsealed the primary and enabled the KV v2 engine, **and** after
[`openbao_project_onboard`](../openbao_project_onboard/README.md) has created the
`policy-<slug>` isolation triple the Workload_AppRole binds to. The data-plane
half — the `bao agent` sidecar that consumes the minted `role_id`/`secret_id` and
renders secrets into a file the workload reads at start — is the companion
`openbao-agent` role.

## What it writes (Task 3)

For `project_slug=<slug>`:

1. **The AppRole auth method at `approle/`** — enabled **once**, check-then-write
   (Req 1.1–1.4). One `bao auth list` read decides enable-vs-skip; after an
   enable the method is re-read and a mount of type `approle` at `approle/` is
   asserted present.

2. **`auth/approle/role/<slug>-workload`** — the per-project Workload_AppRole
   (Req 2.1–2.7), bound to **exactly** `[policy-<slug>]`, a **renewable** service
   token with `token_ttl`/`token_max_ttl` from the workload vars (NOT the
   `≤3600`/non-renewable CI ceiling of the sibling `<slug>-ci` jwt role). The
   `-workload` suffix (matching
   [`scripts/installer/workload_auth.py`](../../../scripts/installer/workload_auth.py)`::workload_role_name`)
   keeps it distinct from `<slug>-ci` (Req 2.4). Check-then-write: written only
   when absent or diverged, then re-read and asserted.

3. **The `role_id` (non-secret) and a freshly-minted `secret_id`** — read and
   minted from the AppRole endpoints (Req 3.1, 3.2), the `secret_id` bounded by
   `secret_id_ttl`/`secret_id_num_uses`, minted with `no_log` and fail-closed so
   a Primary error leaves **no** partial credential file (Req 3.8).

4. **The `role_id`/`secret_id` handoff** — over the sanctioned first-secret
   channel (Req 3.3): a **0600** service-owned host file outside any git tree
   (`file`, homelab), or a **GitLab CI/CD protected+masked** variable (`gitlab`,
   pipeline — PF FR-8). The `secret_id` is never written to a git-tracked file, a
   vars file, or a Terraform output (Req 3.5).

## Idempotency & fail-closed — check-then-write

Every control-plane write is gated by a read:

- **approle/** — `bao auth list`; enabled only when absent, re-read and verified
  after enabling; a re-run reports `changed=0` (Req 1.3).
- **Workload_AppRole** — `bao read auth/approle/role/<slug>-workload` (missing →
  "absent"); the stored `token_policies`/`token_ttl`/`token_max_ttl`/`token_type`
  are field-compared against the desired contract and the role is written only
  when **absent or diverged**, then re-read and asserted (Req 2.6, 2.7).
- **TTL / num_uses guards** — the token TTL pair and the Secret_ID inputs are
  asserted valid **before any write** (Req 2.3, 3.7), mirroring the shared pure
  validators `validate_token_ttls` / `validate_secret_id_inputs`.
- **policy-`<slug>`** — asserted **present** (fail closed with a "run
  `openbao_project_onboard` first" message) before the role is created; never
  created here (Req 2.5).

> The `secret_id` is **re-minted every run** by design — it is an un-rereadable,
> TTL/num_uses-bounded credential, so re-minting is the correct remedy for a
> leaked/expired one (design.md "Open risks and trade-offs"). The `role_id`
> (stable, non-secret) is read idempotently.

## `projects.yaml` is READ-ONLY here

The slug is already recorded in `projects.yaml` by the network-foundation
onboarding flow (at the platform-foundation level per `structure.md`). This role
**reads** it (`project_slug`, and optionally validates it is a recorded slug) and
**never writes, extends, or re-derives the registry**.

## Authentication — platform-admin token, NOT the Root_Token

The Root_Token is revoked after bootstrap, so this day-2 role authenticates with
a **platform-admin** token supplied at runtime from an operator-controlled,
**never-committed** environment variable (`OPENBAO_ADMIN_TOKEN` by default) — a
gitignored `.env` for a homelab run, or a GitLab CI/CD protected+masked variable
for a pipeline run. The token value is never defaulted, never written to a vars
file, and every task carrying it (or a `bao` read/write whose exec env carries
it) sets `no_log: "{{ openbao_no_log }}"`. The role fails closed if the token is
absent. All `bao` calls run via `docker exec` against the primary container
(consistent with the sibling roles). The dev-only `openbao_dev_expose_secrets`
opt-in fails closed in `ci`/`acc`/`prod` (Req 7.4).

## Role layout

```
openbao_workload_approle/
  defaults/main.yml          # slug/addressing/approle/workload-TTL vars, admin-token env-var name, handoff vars
  tasks/main.yml             # STEP 0-9: validate -> admin-token -> TTL guards -> enable approle -> assert policy -> role -> role-id -> secret-id -> handoff -> scrub
  handlers/main.yml          # empty skeleton
  templates/role-id.env.j2   # the 0600 on-host role-id file (non-secret, runtime path only)
  templates/secret-id.env.j2 # the 0600 on-host secret-id file (sensitive, runtime path only)
  meta/main.yml
  README.md
```

## Key variables

See [`defaults/main.yml`](./defaults/main.yml) for the full set. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `project_slug` | *(required)* | The project to mint the Workload_AppRole for (read-only from `projects.yaml`). |
| `openbao_admin_token_env_var` | `OPENBAO_ADMIN_TOKEN` | Env-var NAME the platform-admin token is read from (never the value). |
| `openbao_approle_mount_path` | `approle` | AppRole auth mount path, enabled once at `approle/` (Req 1.1). |
| `openbao_workload_role_name` | `{{ project_slug }}-workload` | Workload_AppRole name, distinct from `<slug>-ci` (Req 2.4). |
| `openbao_project_policy_name` | `policy-{{ project_slug }}` | The existing isolation policy the role binds to (asserted present, Req 2.5). |
| `openbao_workload_token_ttl` | `3600` | Renewable token TTL (seconds, > 0) (Req 2.2). |
| `openbao_workload_token_max_ttl` | `86400` | Renewable token max TTL (seconds, ≥ `token_ttl`) (Req 2.2). |
| `openbao_workload_secret_id_ttl` | `86400` | Secret_ID usage TTL (seconds, > 0) (Req 3.6, 3.7). |
| `openbao_workload_secret_id_num_uses` | `0` | Secret_ID num-uses (≥ 0; 0 = unlimited) (Req 3.6, 3.7). |
| `openbao_workload_handoff` | `file` | Handoff channel: `file` (0600 host file) or `gitlab` (protected+masked var) (Req 3.3). |
| `openbao_workload_creds_dir` | `/etc/openbao-workload/{{ project_slug }}` | Service-owned dir, outside any git tree, for the 0600 credential files. |
| `openbao_dev_expose_secrets` | `false` | Dev-only opt-in; flips `no_log` off for a single run; fails closed in ci/acc/prod (Req 7.4). |
| `openbao_no_log` | `{{ not (openbao_dev_expose_secrets | bool) }}` | Resolved `no_log` value for secret-bearing tasks (Req 7.2). |
| `openbao_primary_tls_skip_verify` | `{{ openbao_tls_self_signed | default(false) | bool }}` | Splice `-tls-skip-verify` for the self-signed mock cert (same expression as siblings). |

**No secret value lives in this role.** The Role_ID is non-secret config; the
Secret_ID and Admin_Token are read at runtime (from the Primary and a
never-committed env var respectively), carried under `no_log`, and scrubbed from
facts at STEP 9.

## Minting flow

```mermaid
---
config:
  layout: elk
---
sequenceDiagram
    actor Op as Operator / CI
    participant A as openbao_workload_approle
    participant P as Primary OpenBao
    participant FS as Host (0600 creds) / GitLab var

    Op->>A: run with project_slug=<slug> (+ OPENBAO_ADMIN_TOKEN in env)
    A->>A: validate slug (READ-ONLY projects.yaml); resolve + assert Admin_Token (fail closed)
    A->>A: validate TTL / num_uses inputs (fail before any write)
    A->>P: bao auth list (check-then-write)
    alt approle/ absent
        A->>P: bao auth enable approle (changed=1)
        A->>P: re-read + assert type=approle at approle/
    else approle/ present
        A-->>Op: skip enable (changed=0)
    end
    A->>P: bao policy read policy-<slug> (assert present, else fail closed)
    A->>P: read auth/approle/role/<slug>-workload (check-then-write)
    alt role absent or diverged
        A->>P: write role (policies=[policy-<slug>], renewable, ttl/max_ttl)
        A->>P: re-read + assert contract
    else role matches
        A-->>Op: skip role write (changed=0)
    end
    A->>P: read role-id (non-secret)
    A->>P: write -f secret-id (mint, no_log, fail closed)
    A->>FS: write role_id + secret_id (0600 service-owned) / GitLab protected+masked var
    A->>A: scrub secret_id + admin_token facts
    A-->>Op: report minted role (role_id visible; secret_id never logged)
```

## Verify the role (offline, Tier-1)

Lint the role's YAML and structure. `ansible`/`ansible-lint` are not currently
installed in the project venv; until they are, the role is verified offline by
parsing every YAML file with PyYAML and confirming the check-then-write /
`no_log` / no-secret-literal structure (no token defaulted to a literal, every
secret-bearing task carries `no_log: "{{ openbao_no_log }}"`). The policy/render
contract test and the Ansible check-mode idempotency test (asserting `changed=0`
for an already-minted role) are **tasks 6.x / 7.x** of this spec.

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/ansible-lint roles/openbao_workload_approle
```

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/yamllint roles/openbao_workload_approle
```

## Live verification (requires-infra) — the self-signed mock-cert Primary

The live acceptance for this role runs against a real SVC-07 Primary via
`tests/installer/test_workload_auth_integration.py` and the provision-then-test
walkthrough in
[`../../../infra/projects/svc-07-secrets-manager/INSTALL-RUNBOOK.md`](../../../infra/projects/svc-07-secrets-manager/INSTALL-RUNBOOK.md)
§6 (gated on `AGENT_LIVE_AUTHORIZED` + the throwaway guard; never CI/acc/prod).

The homelab Primary serves the **self-signed mock cert**
(`openbao_tls_self_signed=true`, no SVC-08/EJBCA). Because
`openbao_primary_tls_skip_verify` derives from `openbao_tls_self_signed` (default
**false**), a live run against a mock-cert Primary **must** set
`openbao_tls_self_signed=true` or every `bao` docker-exec call fails
`x509: certificate signed by unknown authority` (censored to a bare non-zero rc
under `no_log`). This is exactly how the sibling `openbao_project_onboard` path is
driven. The runbook's manual role runs pass `-e openbao_tls_self_signed=true`; the
integration harness threads it into every role run by default
(`_DEFAULT_ROLE_EXTRA_VARS`), overridable per-test for a real-EJBCA-cert Primary.
The production default stays `false` — the skip is a dev/mock affordance only.
