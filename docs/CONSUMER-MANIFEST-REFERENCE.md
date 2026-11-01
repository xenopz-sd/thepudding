# Consumer_Manifest — format reference

This is the **reference** for the per-project Consumer_Manifest
(`infra/projects/<slug>/consumer-manifest.yml`): the precise schema the
[Consumer_Onboarding_Orchestrator](../ansible/playbooks/project-consumer-bootstrap.yml)
reads to decide which catalog services a project consumes and with what
parameters. It is a reference document (field-level facts), not a how-to — for
the end-to-end walkthrough see the
[Consumer Journey Runbook](CONSUMER-JOURNEY.md).

- **Spec:** [`.kiro/specs/project-consumer-onboarding/`](../.kiro/specs/project-consumer-onboarding/) (design.md "Data Models")
- **Validator (authoritative):** [`scripts/installer/consumer_manifest.py`](../scripts/installer/consumer_manifest.py) — the schema below is enforced by this validator; it is the source of truth if this doc and the code ever disagree.

---

## File location and shape

One YAML document per project at `infra/projects/<slug>/consumer-manifest.yml`.
The `slug` field is **mandatory**; every service block is **optional** (a project
may consume any subset). `vlan_id` is **never** declared here — it is resolved
from the Registry for the slug (Req 2.3).

## Enumerated consumable services

The manifest may declare exactly these top-level service blocks, and no others:

| Top-level key | Catalog service | Block below |
|---|---|---|
| `database` | SVC-01 (per-project PostgreSQL/MariaDB) | [`database`](#database-block-svc-01) |
| `sso` | SVC-06 (ZITADEL org + app registrations) | [`sso`](#sso-block-svc-06) |
| `secrets` | SVC-07 (OpenBao policy/path + workload AppRole) | [`secrets`](#secrets-block-svc-07) |
| `workload` | the per-project **workload guest** (an LXC on the project VLAN to run the app) | [`workload`](#workload-block) |

Any other top-level service key — or a **duplicate** of one of these — is
rejected at preflight, naming the offending entry (Req 1.7). The only permitted
top-level keys are `slug`, `database`, `sso`, `secrets`, and `workload`.

---

## Schema field table

| Field | Type / constraint | Req | Validation rule |
|---|---|---|---|
| `slug` | string, `^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$` | 1.5 | lowercase-hyphenated, ≤20 chars; must match a Registry entry |
| `database.db_name` | string, non-empty | 1.2 | identifier-safe |
| `database.db_role` | string, non-empty | 1.2 | identifier-safe |
| `database.engine` | enum `postgres` \| `mariadb` | 1.2 | default `postgres` |
| `database.tier` | enum `small` \| `medium` | 1.2 | default `small` |
| `sso.org_name` | string, non-empty | 1.3 | ZITADEL organization name |
| `sso.app_registrations[].name` | string, non-empty | 1.3 | app registration name |
| `sso.app_registrations[].app_type` | enum `public-spa` \| `confidential-web` \| `api` | 1.3 | reuses the `zitadel-spa-pkce-onboarding` app_type values |
| `sso.app_registrations[].auth_method` | enum `none` \| `basic` \| `jwt-profile` | 1.3 | valid combo per the sibling spec |
| `sso.app_registrations[].redirect_uris` | list of strings | 1.3 | required for `public-spa` / `confidential-web` |
| `secrets.policy` | string, non-empty | 1.4 | OpenBao ACL policy name |
| `secrets.path` | string, `secret/...` | 1.4 | OpenBao KV path prefix |
| `secrets.workload_token_ttl` | integer > 0 | 1.4 | ≤ `workload_token_max_ttl` |
| `secrets.workload_token_max_ttl` | integer > 0 | 1.4 | |
| `secrets.secret_id_ttl` | integer > 0 | 1.4 | |
| `secrets.secret_id_num_uses` | integer ≥ 0 | 1.4 | `0` = unlimited uses within the TTL |
| `workload.enabled` | bool | 2.1 | default `false`; `false`/absent = no workload guest |
| `workload.tier` | enum `small` \| `medium` | 2.1 | default `small` (reuses the `database` sizing bands) |
| `workload.count` | integer ≥ 1 | 2.1 | default `1`; `>1` reserved for a later increment |
| *(top-level key)* | one of `slug` / `database` / `sso` / `secrets` / `workload` | 1.7 | unknown/duplicate key → reject naming it |

### `app_type` / `auth_method` combinations

The `app_type` values are reused verbatim from the `zitadel-spa-pkce-onboarding`
spec. The typical valid combinations:

| `app_type` | `auth_method` | Typical use |
|---|---|---|
| `public-spa` | `none` | Browser single-page app (PKCE, no client secret) |
| `confidential-web` | `basic` | Server-rendered web app with a client secret |
| `api` | `jwt-profile` | Machine/API client authenticating with a JWT profile |

### `workload` block

The optional `workload` block provisions a **workload guest** — an LXC on the
project's own VLAN (100+), within the project's existing per-project Terraform
root/state — to run the application's Compose stack (the agent sidecar + the app
container). It answers "where does my app run?" with a documented, provisioned
guest instead of defaulting to the database guest. Its three fields are all
optional with documented defaults (Req 2.1):

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | `true` provisions the guest; `false`/absent provisions **nothing** |
| `tier` | `small` | sizing band `small` \| `medium` (reuses the `database` tiers verbatim) |
| `count` | `1` | number of workload guests; `>1` is schema-accepted but **reserved for a later increment** (a single guest is the validated path) |

**Strict no-op when absent or disabled (Req 2.3).** A manifest with no `workload`
block — or `workload.enabled: false` — provisions no workload guest and behaves
exactly as before this block existed. The gate is a strict no-op; adding the
block is additive and backward compatible.

**Derived, never hand-chosen addressing.** When enabled, the guest is placed on
the project VLAN with its `host_index` read from the per-VLAN host-index ledger
([`infra/platform-foundation/host-indices.yaml`](../infra/platform-foundation/host-indices.yaml),
ADR-0015) and its IP computed via the NET-00 formula
`cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)` — never typed by hand. Its
hostname follows the ADR-0021 **non-catalog role-code** form
`workload-<vlan_id>-<instance>` (e.g. `workload-100-01`) and its `tags` attribute
is `proj-<slug>-workload`. A workload guest is deliberately **not** a catalog
`SVC-NN` slot: the `proxmox-compute` module identifies it by the stable role code
`workload` rather than overloading `svc_code` (ADR-0021).

For the supported topologies (dedicated guest vs. co-locate on the DB guest vs.
bring-your-own host) and the end-to-end last-mile walkthrough, see the
[Consumer Journey Runbook](CONSUMER-JOURNEY.md#the-last-mile--where-your-workload-runs-b).

---

## Annotated example (placeholders only)

```yaml
# infra/projects/<slug>/consumer-manifest.yml
slug: dronefleet                    # REQUIRED — lowercase, hyphenated, <=20 chars (Req 1.5)

database:                           # OPTIONAL — SVC-01 (Req 1.2)
  db_name: dronefleet               # per-project PostgreSQL database name
  db_role: dronefleet_app           # the least-privilege app role
  engine: postgres                  # postgres (default) | mariadb
  tier: small                       # small | medium

sso:                                # OPTIONAL — SVC-06 (Req 1.3)
  org_name: proj-dronefleet         # ZITADEL organization name
  app_registrations:                # list; app_type reuses zitadel-spa-pkce-onboarding values
    - name: dronefleet-spa
      app_type: public-spa          # public-spa | confidential-web | api
      auth_method: none             # none | basic | jwt-profile
      redirect_uris: ["https://dronefleet.<platform-domain>/oauth/callback"]
    - name: dronefleet-api
      app_type: api
      auth_method: jwt-profile

secrets:                            # OPTIONAL — SVC-07 (Req 1.4)
  policy: policy-dronefleet         # OpenBao ACL policy name
  path: secret/data/dronefleet      # OpenBao KV path prefix
  workload_token_ttl: 3600          # workload AppRole token_ttl (seconds)
  workload_token_max_ttl: 14400     # workload AppRole token_max_ttl (seconds)
  secret_id_ttl: 86400              # Secret_ID usage TTL (seconds)
  secret_id_num_uses: 0             # 0 = unlimited uses within TTL

workload:                           # OPTIONAL — per-project workload guest (Req 2.1)
  enabled: true                     # false/absent (default) => no guest, strict no-op
  tier: small                       # small (default) | medium
  count: 1                          # >=1 (default 1); >1 reserved for a later increment
```

---

## Validation (offline)

The orchestrator validates the manifest at preflight before any mutation. You
can run the same validator standalone — it is offline and side-effect-free
(reads the file only), exits non-zero naming the first offending field (Req 1.6)
or the offending/duplicate/unknown service entry (Req 1.7):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/python scripts/installer/consumer_manifest.py \
  --check infra/projects/dronefleet/consumer-manifest.yml
```

---

## Workload-readable DB credential path (authoritative)

> This section is the **single source of truth** for where a database-declaring
> project's DB credential is readable by its `<slug>-workload` AppRole. The
> consumer→deploy walkthrough ([Consumer Journey Runbook](CONSUMER-JOURNEY.md))
> cross-references this section rather than restating it.

A project that declares a `database` block has its DB credential written by the
`postgres-server` role to the **source record** `secret/proj-<slug>/postgres`
(fields `app_role`, `app_password`, `host`, `port`, `db_name`, …). That `proj-`
prefix is **outside** `policy-<slug>` — the per-project ACL grants capabilities on
exactly the five KV v2 sub-paths under the bare `<slug>` prefix
(`secret/{data,metadata,delete,undelete,destroy}/<slug>/*`) and nothing else — so
the `<slug>-workload` AppRole cannot read the source record directly.

Onboarding therefore **bridges** the credential into the workload-readable tree.
The agreed path the workload reads is:

| What | Value |
|---|---|
| KV path | `secret/data/<slug>/db` |
| Field | `database_url` |
| Value | a composed `postgresql://<app_role>:<app_password>@<host>:<port>/<db_name>` connection string |
| Workload env var | `DATABASE_URL` |

This path lives **inside** the existing `policy-<slug>` boundary, so no policy
change is needed, and it is the same `secret/data/<slug>` prefix the manifest's
`secrets.path` already advertises. The bridge is idempotent and **never rotates**
the source password — it reads the value already stored at
`secret/proj-<slug>/postgres` and re-composes the URL, writing only when absent or
changed.

The `openbao-agent` sidecar resolves it automatically via the default DB mapping
`{ kv_subpath: db, kv_field: database_url, env_key: DATABASE_URL }` (a
`consumer_bootstrap` role default, **not** a manifest key — the top-level
allowlist `{slug, database, sso, secrets}` is unchanged). A non-default workload
may override the mapping deliberately.

### Confirming the bridged path (live, placeholders only)

After onboarding a database-declaring project, an operator can confirm the
workload-readable path resolves by logging in with the `<slug>-workload` AppRole
and reading the agreed path. Substitute placeholders with values from your
environment — never commit real tokens or hostnames (see
[`documentation-testing.md`](../.kiro/steering/documentation-testing.md) placeholder rules).

<!-- doctest: requires-infra -->
```bash
# Reads the bridged DB credential on the agreed workload path.
# <SLUG>            — the project slug
# <WORKLOAD_TOKEN>  — a token from a `<slug>-workload` AppRole login (never committed)
bao kv get -field=database_url secret/data/<SLUG>/db
```

The source record at `secret/proj-<SLUG>/postgres` is **not** reachable by the
workload AppRole by design (least-privilege); a read of it with a workload token
returns a policy denial — that is correct, and is exactly why the bridge exists.
