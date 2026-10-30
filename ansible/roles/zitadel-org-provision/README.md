# `zitadel-org-provision` — onboard a project to ZITADEL SSO

> **Type:** Developer/operator reference (content mode: *reference* + *how-to*).
> **Service:** SVC-06 (ZITADEL identity provider).
> **Spec:** [`zitadel-spa-pkce-onboarding`](../../../.kiro/specs/zitadel-spa-pkce-onboarding/)
> (two-client onboarding) — additive to the base
> [`svc-06-identity-provider`](../../../.kiro/specs/svc-06-identity-provider/) spec.

Idempotently provisions a platform project's ZITADEL **organization**, default
**roles**, and one or more **OIDC/API application registrations** by driving the
stdlib helper [`scripts/installer/zitadel_provision.py`](../../../scripts/installer/zitadel_provision.py),
then captures each created client secret into **OpenBao** (SVC-07) at a per-app
KV path. The helper is pure-Python/stdlib (no `requests`), idempotent
(already-exists = success), and reads its provisioning token only from the
`ZITADEL_PROVISION_TOKEN` env var (never argv).

---

## What the two-client onboarding adds

Historically this role registered **exactly one** confidential-WEB app named
`<slug>-webapp` per run. The two-client onboarding keeps that as the **default**
and adds:

- a `zop_app_registrations` **list** so one run can register **multiple apps of
  multiple types** in the **same org + project** — e.g. a public **SPA** (React
  PWA) and its companion **API** (NestJS backend);
- new helper CLI flags `--app-type` / `--auth-method`;
- per-app OpenBao capture at a **unique path per app** (SPA has no secret, so
  nothing is written for it).

Everything else is byte-for-byte unchanged when no list is supplied (R4.3/R6.1).

---

## Helper CLI reference — `--app-type` / `--auth-method`

The helper selects the ZITADEL app shape from two flags. Both are optional with
backward-compatible defaults.

| Flag | Values | Default | Notes |
|---|---|---|---|
| `--app-type` | `spa`, `web`, `api` | `web` | Selects the ZITADEL app shape. Omitting it = pre-existing confidential-web behavior. |
| `--auth-method` | `none`, `basic`, `jwt` | **derived** from `--app-type` when omitted | `spa→none`, `web→basic`, `api→jwt`. |

Only these three `(app_type, auth_method)` combinations are **valid**; any other
pair exits non-zero (code 2) naming the pair on stderr:

| `--app-type` | `--auth-method` | Result |
|---|---|---|
| `web` | `basic` | **DEFAULT** — confidential web, client secret, posted to `apps/oidc` |
| `spa` | `none` | public SPA / PKCE, **no** client secret, posted to `apps/oidc` |
| `api` | `jwt` | API resource server, JWT-profile key material, posted to `apps/api` |

- `argparse` rejects out-of-domain values (e.g. `--app-type foo`) before any
  network call.
- The provisioning token is **never** a flag — it is read only from
  `ZITADEL_PROVISION_TOKEN` in the child-process environment.

### `--help` (offline)

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/python scripts/installer/zitadel_provision.py --help
```

---

## JSON output (additive)

Each helper invocation emits one JSON object on stdout. Keys are **additive** —
existing callers keep every pre-existing key unchanged; `app_type` and
`auth_method` are added:

<!-- doctest: display-only -->
```json
{
  "org":     { "name": "proj-dronefleet", "id": "<org-id>", "created": true },
  "project": { "name": "dronefleet", "id": "<project-id>", "created": true },
  "roles_created": ["admin", "member", "viewer"],
  "oidc_app": {
    "app_name": "dronefleet-spa",
    "app_type": "spa",
    "auth_method": "none",
    "client_id": "<client-id>",
    "client_secret": null,
    "already_existed": false
  }
}
```

- **SPA** (`app_type: spa`) **always** emits `client_secret: null` — a public
  client holds no secret (R1.3).
- **Confidential web** and **API** emit their created-once credential material in
  `client_secret` (the generic "secret the role captures" key), **only** at first
  creation; a re-run (`already_existed: true`) re-emits no secret.

---

## API client bearer-validation mechanism

An `api` registration creates a ZITADEL **API application** (resource server) at
`POST /management/v1/projects/{project_id}/apps/api` with
`authMethodType = API_AUTH_METHOD_TYPE_PRIVATE_KEY_JWT` — the **JWT-profile**
method. The backend uses the created-once key material to validate the bearer
tokens the SPA presents (JWKS signature verification, or token introspection for
opaque tokens). See the full consumer-side detail — including how the API reads
the **project-roles claim** from the validated access token — in the
[role-claim reference](./ROLE-CLAIM-REFERENCE.md).

> The exact `apps/api` credential field name and whether JWKS vs introspection is
> the validation path are **live-confirm** items (spec Open Risk 2) — see the
> role-claim reference.

---

## Role variables (two-client onboarding)

Added by this feature (all backward-compatible — see `defaults/main.yml`):

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `zop_app_registrations` | list | `[]` | Apps to onboard this run. Empty ⇒ synthesize the legacy single `<slug>-webapp` web entry. |
| `zop_default_app_name` | string | `{{ zop_project_slug }}-webapp` | Name of the synthesized legacy default entry. |
| `zop_default_app_type` | string | `web` | App type of the synthesized default. |
| `zop_default_auth_method` | string | `basic` | Auth method of the synthesized default. |
| `zop_valid_app_types` | list | `[spa, web, api]` | Accepted entry `app_type` values (R4.5 assertion). |
| `zop_app_type_default_auth_method` | map | `{spa: none, web: basic, api: jwt}` | Per-type default auth method when an entry omits it. |
| `zop_openbao_app_secret_path_tmpl` | string | `secret/proj-<slug>/zitadel/oidc/{app_name}` | Per-app KV path template (unique per app). |

Each `zop_app_registrations` entry (an **App_Registration**):

<!-- doctest: display-only -->
```yaml
zop_app_registrations:
  - name: "dronefleet-spa"          # unique within the project
    app_type: spa                   # spa | web | api
    auth_method: none               # optional; derived from app_type when omitted
    redirect_uris: ["https://dronefleet.platform.example.test/callback"]
  - name: "dronefleet-api"
    app_type: api
    auth_method: jwt
    redirect_uris: []               # resource servers have no redirect URIs
```

---

## Usage

### Default (legacy single confidential-web app)

No list ⇒ onboard exactly `<slug>-webapp` (confidential web), unchanged behavior.
The token is read from OpenBao by default (auto-minted at ZITADEL first-init); no
`ZITADEL_PROVISION_TOKEN` is required. `OPENBAO_ADMIN_TOKEN` must be in the env.

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel-org-provision.yml -e zop_project_slug=dronefleet
```

### Two-client onboarding (SPA + API in one run)

Supply a `zop_app_registrations` list (here via an extra-vars file):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel-org-provision.yml \
  -e zop_project_slug=dronefleet \
  -e @onboard-dronefleet-two-client.yml
```

Both apps land in the **same** org `proj-dronefleet` and project `dronefleet`.
The API client's secret is written to
`secret/proj-dronefleet/zitadel/oidc/dronefleet-api`; the SPA writes **nothing**
(no secret). A re-run is a no-op success (already-exists).

> Full provision-then-test walkthrough (stand ZITADEL up from nothing, then run
> the live two-client test against it) lives in the
> [SVC-06 install runbook](../../../infra/projects/svc-06-identity-provider/INSTALL-RUNBOOK.md#a6--two-client-onboarding-spa--api-provision-then-test).

---

## Degraded modes

- **OpenBao unwired** (`zop_openbao_addr` empty): the per-app secret write +
  read-back are **skipped** with a per-app warning; the run is **not** failed.
  Because ZITADEL emits a client secret only at creation time, a confidential/API
  secret not captured on first creation is not recoverable by re-running —
  rotate the client secret once OpenBao is wired.
- **ZITADEL unreachable / API error:** the helper exits non-zero (3), the role
  surfaces the rc and stops; no secret is written for a failed app. A single POST
  per app means a failed create leaves nothing partial to clean up.
- **Neither token source yields a value:** the role fails fast naming **both**
  the `ZITADEL_PROVISION_TOKEN` env var and the OpenBao `provision_token` field
  before any registration (R5.7).

All token/secret-handling tasks carry `no_log: true` (R5.5).

---

## Related documentation

- **Role-claim shape** (SPA/API consumer reference): [`ROLE-CLAIM-REFERENCE.md`](./ROLE-CLAIM-REFERENCE.md).
- **Install runbook / provision-then-test walkthrough:**
  [SVC-06 install runbook](../../../infra/projects/svc-06-identity-provider/INSTALL-RUNBOOK.md).
- **Helper source:** [`scripts/installer/zitadel_provision.py`](../../../scripts/installer/zitadel_provision.py).
- **Doctest tiers:** [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md).
