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

Any other top-level service key — or a **duplicate** of one of these — is
rejected at preflight, naming the offending entry (Req 1.7). The only permitted
top-level keys are `slug`, `database`, `sso`, and `secrets`.

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
| *(top-level key)* | one of `slug` / `database` / `sso` / `secrets` | 1.7 | unknown/duplicate key → reject naming it |

### `app_type` / `auth_method` combinations

The `app_type` values are reused verbatim from the `zitadel-spa-pkce-onboarding`
spec. The typical valid combinations:

| `app_type` | `auth_method` | Typical use |
|---|---|---|
| `public-spa` | `none` | Browser single-page app (PKCE, no client secret) |
| `confidential-web` | `basic` | Server-rendered web app with a client secret |
| `api` | `jwt-profile` | Machine/API client authenticating with a JWT profile |

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
