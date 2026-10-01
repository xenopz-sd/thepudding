# `openbao_project_onboard` Ansible role

Per-project onboarding for OpenBao (SVC-07 Secrets Manager). For one project
**slug** (read READ-ONLY from `projects.yaml`) it creates the per-project
**isolation triple** on the **primary** OpenBao instance and is fully
idempotent — a second run for the same slug is a byte-identical no-op that
touches no secret version and reports `changed=0` (Req 1.5, 13.4).

It runs **day-2**, after [`openbao_install`](../openbao_install/README.md) and
[`openbao_init_unseal`](../openbao_init_unseal/README.md) have brought up,
initialised/unsealed the primary, and enabled the KV v2 engine + the `jwt` auth
method (tasks 5.x/6.x/7.x). It is distinct from the downstream `openbao-agent`
sidecar every *other* service runs (out of scope for SVC-07).

## The isolation triple (task 9.1)

For `project_slug=<slug>`:

1. **`policy-<slug>`** — an ACL policy granting `create/read/update/delete/list`
   on **exactly** the five KV v2 sub-paths and **nothing** outside them
   (Req 1.2, 1.3):

   ```
   secret/data/<slug>/*      secret/metadata/<slug>/*
   secret/delete/<slug>/*    secret/undelete/<slug>/*
   secret/destroy/<slug>/*
   ```

   KV v2 splits an operation across those distinct API prefixes, so all five are
   needed to fully manage a project's own KV tree — and no sixth path, no
   wildcard above the slug, no capability on any other project's prefix. This is
   the single enforcement point for the path-prefix + policy per-project
   isolation model (OpenBao Community has no namespace primitive).

2. **`auth/jwt/role/<slug>-ci`** — the GitLab-CI login role (Req 4.2, 4.7):
   `bound_claims.project_path` = the project's repo path (plus optional
   `ref_type`/`ref`), `bound_audiences` = the platform audience, `token_ttl` and
   `token_max_ttl` **both ≤ 3600s**, **`renewable=false`**, and
   `token_policies=[policy-<slug>]`. The `jwt` *method* (`bound_issuer` +
   `oidc_discovery_url`) is configured once at bootstrap (task 7.2); this role
   creates the per-project *role* bound to it.

3. **`secret/data/<slug>/`** — the KV path exists (Req 1.6), created via
   `kv metadata put` (metadata-only) so it never creates or overwrites a secret
   version.

## Idempotency (Req 1.5, 13.4) — check-then-write

Every write is gated by a read, so a re-run for the same slug changes nothing:

- **policy** — `bao policy list` + `bao policy read`; the freshly-rendered HCL is
  byte-compared (whitespace-normalised) against the stored body, and the policy
  is written only when **absent or different**.
- **jwt role** — `bao read auth/jwt/role/<slug>-ci -format=json`; the stored role
  is field-compared against the desired TTLs / `bound_audiences` /
  `bound_claims` / `token_policies` / `user_claim`, and written only when
  **absent or different**. After a write, the stored role is re-read and the
  `bound_claims` (including any `ref_type`/`ref`) + TTL ceiling are asserted.
- **KV path** — `bao kv metadata get`; metadata is created only when **absent**,
  and via `kv metadata put` (metadata-only) so **no secret version** is ever
  created or bumped, on first run or re-run.

## `projects.yaml` is READ-ONLY here

The slug is the authoritative identifier already recorded in `projects.yaml` by
the network-foundation onboarding flow (at the platform-foundation level per
`structure.md`). This role **reads** it (`project_slug`, and optionally validates
it is a recorded slug) and **never writes, extends, or re-derives the registry**.

## Authentication — platform-admin token, NOT the Root_Token

The Root_Token is revoked after bootstrap (Security AC 1), so this day-2 role
authenticates with a **platform-admin** token supplied at runtime from an
operator-controlled, **never-committed** environment variable
(`OPENBAO_ADMIN_TOKEN` by default) — a gitignored `.env` for a homelab run, or a
GitLab CI/CD protected+masked variable for a pipeline run. The token value is
never defaulted, never written to a vars file, and every task carrying it (or a
`bao` read/write whose exec env carries it) sets `no_log: true`. The role fails
closed if the token is absent. All `bao` calls run via `docker exec` against the
primary container (consistent with the sibling roles).

## Role layout

```
openbao_project_onboard/
  defaults/main.yml            # slug/addressing/KV-subpath/jwt-role vars, admin-token env-var name
  tasks/main.yml               # validate -> policy -> jwt role -> KV path (all check-then-write)
  handlers/main.yml            # empty skeleton
  templates/policy-slug.hcl.j2 # the policy-<slug> ACL (five sub-paths only)
  meta/main.yml
  README.md
```

## Key variables

See [`defaults/main.yml`](./defaults/main.yml) for the full set. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `project_slug` | *(required)* | The project to onboard (read-only from `projects.yaml`). |
| `project_gitlab_path` | `{{ project_slug }}` | `bound_claims.project_path` for the jwt role (Req 4.7). |
| `openbao_admin_token_env_var` | `OPENBAO_ADMIN_TOKEN` | Env-var NAME the platform-admin token is read from (never the value). |
| `openbao_kv_mount` | `secret` | The single KV v2 mount (Req 1.1). |
| `openbao_project_kv_subpaths` | `[data, metadata, delete, undelete, destroy]` | The five KV v2 sub-paths `policy-<slug>` scopes to (Req 1.2). |
| `openbao_project_jwt_token_ttl` | `3600` | jwt `token_ttl` — must be ≤ 3600 (Req 4.2). |
| `openbao_project_jwt_token_max_ttl` | `3600` | jwt `token_max_ttl` — must be ≤ 3600 (Req 4.2). |
| `openbao_project_jwt_token_renewable` | `false` | jwt role is non-renewable (Req 4.2). |
| `openbao_project_jwt_bound_ref_type` | `""` | Optional GitLab `ref_type` bound_claim (Req 4.7). |
| `openbao_project_jwt_bound_ref` | `""` | Optional GitLab `ref` bound_claim (Req 4.7). |

**No secret value lives in this role.** It writes an ACL policy, a jwt role, and
KV metadata — none of which is a secret — and reads its platform-admin token
from a never-committed env var.

## Onboarding flow

```mermaid
---
config:
  layout: elk
---
sequenceDiagram
    actor Op as Operator
    participant A as openbao_project_onboard
    participant P as Primary OpenBao

    Op->>A: run with project_slug=<slug> (+ OPENBAO_ADMIN_TOKEN in env)
    A->>A: validate slug is recorded in projects.yaml (READ-ONLY)
    A->>P: bao policy list / read (check-then-write)
    alt policy-<slug> present and byte-identical
        A-->>Op: skip policy (changed=0) — idempotent (Req 1.5, 13.4)
    else absent or changed
        A->>P: write policy-<slug> (five KV sub-paths only, Req 1.2/1.3)
    end
    A->>P: read auth/jwt/role/<slug>-ci (check-then-write)
    alt role present and matching
        A-->>Op: skip role (changed=0)
    else absent or changed
        A->>P: write role (bound project_path, <=3600s, renewable=false, Req 4.2/4.7)
        A->>P: re-read + assert bound_claims / ref_type / ref
    end
    A->>P: kv metadata get; ensure secret/data/<slug>/ (metadata-only, Req 1.6)
    A-->>Op: report created/skipped resources (registry unchanged)
```

## Verify the role (offline, Tier-1)

Lint the role's YAML and structure. `ansible`/`ansible-lint` are not currently
installed in the project venv; until they are, the role is verified offline by
parsing every YAML file with PyYAML, rendering `policy-slug.hcl.j2` with a
fixture slug and asserting it grants **exactly** the five KV sub-paths and
nothing else, and confirming the check-then-write / `no_log` / no-secret-literal
structure. The policy contract test and the Ansible check-mode idempotency test
(asserting `changed=0` for an already-onboarded slug) are **task 9.2**.

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/ansible-lint roles/openbao_project_onboard
```

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/yamllint roles/openbao_project_onboard
```
