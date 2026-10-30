# ZITADEL Role-Claim Shape — reference for SPA + API consumers

> **Type:** Interface / reference documentation (content mode: *reference*).
> **Audience:** developers integrating a consuming **SPA** (React PWA) and its
> companion **API** (NestJS backend) against a project onboarded by the
> `zitadel-org-provision` role (SVC-06).
> **Spec:** [`zitadel-spa-pkce-onboarding`](../../../.kiro/specs/zitadel-spa-pkce-onboarding/)
> (Requirement 7, design "Role-claim data model").

This document states the exact claim ZITADEL emits for a user's project roles,
its JSON structure, how the platform's four roles map into it, and the step by
which a consuming SPA (reads the ID token) and API (reads the access token) each
resolve a user's roles — including how each decides a user has **no** project
roles.

> **⚠ LIVE-CONFIRM items (pending a throwaway-cluster run under
> `AGENT_LIVE_AUTHORIZED`).** Two facts below are design assumptions grounded in
> the ZITADEL scopes/claims documentation and **must be confirmed against the
> live ZITADEL v4 instance** before this reference is treated as final. They are
> marked **`[LIVE-CONFIRM]`** inline and correspond to the spec's Open Risks 1 & 2:
>
> 1. **Open Risk 1 — the exact claim name** (`urn:zitadel:iam:org:project:roles`
>    vs the project-scoped `urn:zitadel:iam:org:project:{projectid}:roles`),
>    which scope triggers which form, and whether the flat or scoped form is
>    emitted by default on v4.
> 2. **Open Risk 2 — the `apps/api` credential-field/endpoint shape** for the API
>    resource-server client (the created-once credential field name and whether
>    JWKS or token introspection is the validation path). This governs *how* the
>    API validates the bearer token that carries the claim, documented at the end.

---

## 1. Claim name

ZITADEL emits a user's project roles under a URN-namespaced claim. There are two
forms:

- **Flat (project-unscoped) form** — all of the user's roles across the project,
  under the claim key quoted verbatim:

  ```
  urn:zitadel:iam:org:project:roles
  ```

- **Project-scoped form** — the roles for one specific project, keyed by the
  project's ID, quoted verbatim (substitute the real numeric project ID for
  `{projectid}`):

  ```
  urn:zitadel:iam:org:project:{projectid}:roles
  ```

  This scoped form is requested by adding the project-roles scope for that
  project ID to the authorization request. **`[LIVE-CONFIRM]`** Which form is
  present by default, and the exact scope string that triggers the scoped form,
  must be verified against the live ZITADEL v4 instance (Open Risk 1).

Both forms carry the **same value structure** (Section 2); only the claim key
and the breadth of roles differ.

*(Content synthesized from the ZITADEL scopes/claims documentation and rephrased
for licensing compliance.)*

---

## 2. JSON structure of the role claim

| Aspect | Value |
|---|---|
| **Claim value type** | a JSON **object** (map), not an array or a string |
| **Keys** | each key is a **role key** (the `roleKey` created on the ZITADEL project) |
| **Value per role key** | a JSON **object** mapping each granting **org ID** to that org's **primary domain** (`{ "<orgId>": "<orgDomain>" }`) |

So the claim is a two-level map: *role key → { org ID → org domain }*. A role key
appears **only** when the user actually holds that role; a user with no roles has
an **empty object** (or the claim is absent entirely — see Section 5).

### 2.1 Concrete example payload — flat form `[display-only]`

A user who holds the `owner` and `collaborator` roles in the `proj-dronefleet`
organization:

<!-- doctest: display-only -->
```json
{
  "urn:zitadel:iam:org:project:roles": {
    "owner":        { "277843019283456789": "proj-dronefleet.platform.example.test" },
    "collaborator": { "277843019283456789": "proj-dronefleet.platform.example.test" }
  }
}
```

### 2.2 Concrete example payload — project-scoped form `[display-only]`

The same two roles, requested with the project-scoped scope for project ID
`277843019283456789`:

<!-- doctest: display-only -->
```json
{
  "urn:zitadel:iam:org:project:277843019283456789:roles": {
    "owner":        { "277843019283456789": "proj-dronefleet.platform.example.test" },
    "collaborator": { "277843019283456789": "proj-dronefleet.platform.example.test" }
  }
}
```

### 2.3 Concrete example — user with no project roles `[display-only]`

<!-- doctest: display-only -->
```json
{
  "urn:zitadel:iam:org:project:roles": {}
}
```

> The org ID / org domain values above are **illustrative placeholders**
> (`277843019283456789`, `proj-dronefleet.platform.example.test`) — never real
> tenant identifiers. The IDs you observe live will differ.

---

## 3. Platform-role → claim-key mapping

The platform defines four project roles. Each maps to a **claim key** (the
ZITADEL `roleKey`) by which that role is identified in the claim object:

| Platform role | Claim key (role key) | Identified in the claim by |
|---|---|---|
| **Owner** | `owner` | presence of the key `owner` in the claim object |
| **Collaborator** | `collaborator` | presence of the key `collaborator` |
| **Observer** | `observer` | presence of the key `observer` |
| **Admin** | `admin` | presence of the key `admin` |

A user holds a role **iff** the corresponding claim key is present in the claim
object. The value under the key identifies *which org* granted it; for the
single-org-per-project model the granting org is the project's own
`proj-<slug>` organization.

> **Role-key alignment note (spec Open Risk 3).** The `zitadel-org-provision`
> role currently provisions the default role keys `admin / member / viewer` (see
> `defaults/main.yml` → `zop_roles`). To emit the four platform roles above, the
> provisioned `zop_roles` must be aligned to `owner / collaborator / observer /
> admin` **or** the consuming app must map the existing `admin / member / viewer`
> keys onto the platform roles. Whichever is chosen, the role keys actually
> provisioned MUST match the keys a consumer checks for — document the choice in
> the onboarding run and keep this table in sync with `zop_roles`.

---

## 4. How each consumer reads the claim

The SPA and the API read the role claim from **different tokens**. This is the
central integration fact: a browser SPA authenticates the user and reads the **ID
token**; the API is a resource server and reads the **access token** presented as
a bearer credential.

### 4.1 Consuming SPA — reads the **ID token**

A browser-based SPA (public client, Authorization Code + PKCE, no secret) obtains
an **ID token** at the end of the login flow. Step by step:

1. Complete the Authorization Code + PKCE flow against ZITADEL; receive the
   **ID token** (a signed JWT) and an access token.
2. Decode the ID token payload (validate its signature against ZITADEL's JWKS
   first — never trust an unverified token).
3. Read the role claim by its exact key — `urn:zitadel:iam:org:project:roles`
   (or the project-scoped key from Section 1) — from the decoded payload.
4. The value is the *role key → { org ID → org domain }* object of Section 2.
   The set of **keys** present is the set of roles the user holds; gate UI on
   those keys using the mapping in Section 3.

> The project-roles claim is present in the ID token when the project-roles
> scope is included in the authorization request. **`[LIVE-CONFIRM]`** Confirm on
> the live instance that the scope needed to surface the claim in the **ID
> token** (vs only the userinfo endpoint) is requested by the SPA (Open Risk 1).

### 4.2 Consuming API — reads the **access token**

The NestJS backend is a **resource server**: it never runs a login flow. It
receives the SPA's **access token** as an `Authorization: Bearer <token>` header
and validates it. Step by step:

1. Extract the bearer token from the `Authorization` header of the incoming
   request.
2. Validate the token — see Section 6 for the API client's bearer-validation
   mechanism (JWKS signature verification or token introspection).
3. From the **validated** token's claims, read the role claim by the same exact
   key used by the SPA (`urn:zitadel:iam:org:project:roles` or the project-scoped
   key).
4. The value is the same *role key → { org ID → org domain }* object. The set of
   **keys** present is the set of roles the caller holds; authorize the API
   operation against those keys using the mapping in Section 3.

> **`[LIVE-CONFIRM]`** Whether the project-roles claim is carried inside the JWT
> **access token** directly, or must be resolved via the token-introspection
> response, depends on the ZITADEL access-token type and must be confirmed live
> (Open Risks 1 & 2). If introspection is the path, the API reads the claim from
> the introspection JSON rather than from a decoded JWT.

---

## 5. Determining "no project roles"

The absence of any role is represented in one of two equivalent ways, and **both
consumers must treat both as "no project roles":**

- the role claim **key is present but its value object is empty** (`{}`), or
- the role claim **key is absent from the token entirely**.

| Consumer | Source token | "No project roles" is determined when… |
|---|---|---|
| **SPA** | ID token | after decoding the validated ID token, the role claim is **missing** OR its value object has **zero keys** → render the no-roles / unauthorized UI state |
| **API** | access token | after validating the bearer token, the role claim is **missing** OR its value object has **zero keys** → deny the role-gated operation (treat as unauthorized, not as an error) |

Neither consumer may infer a default role from the absence of the claim. Absent
or empty ⇒ the user holds **no** project roles, full stop.

---

## 6. API client bearer-validation mechanism

The API resource-server client is registered by the helper as a ZITADEL **API
application** (endpoint `POST /management/v1/projects/{project_id}/apps/api`,
`authMethodType = API_AUTH_METHOD_TYPE_PRIVATE_KEY_JWT` — the **JWT-profile**
method, chosen so no long-lived shared secret is embedded in the backend). The
API client uses this registration to validate the bearer tokens the SPA presents:

- **JWT-profile (default, this design):** ZITADEL returns **key material** once at
  creation. The backend uses it to authenticate to ZITADEL's token endpoint and,
  from there, validates presented access tokens — verifying their signature
  against ZITADEL's **JWKS**, or calling the **token-introspection** endpoint for
  opaque tokens.
- **Fallback:** `API_AUTH_METHOD_TYPE_BASIC` (client-credentials / introspection)
  is held as a documented fallback if JWT-profile proves unsuitable live.

> **`[LIVE-CONFIRM]`** The precise `apps/api` endpoint path, the **created-once
> credential field name** ZITADEL returns, and whether **JWKS** or
> **introspection** is the sanctioned validation path must be verified against
> the live ZITADEL v4 instance (Open Risk 2). The helper emits whatever
> created-once credential material ZITADEL returns in the `client_secret` field
> of its JSON result (the generic "the secret the role captures" key), which the
> role writes to OpenBao at the API client's unique per-app path.

---

## Related documentation

- Role usage, CLI flags, and JSON output: the
  [`zitadel-org-provision` role README](./README.md).
- Provision-then-test walkthrough (two-client onboarding, live test):
  [SVC-06 install runbook](../../../infra/projects/svc-06-identity-provider/INSTALL-RUNBOOK.md).
- Helper source: [`scripts/installer/zitadel_provision.py`](../../../scripts/installer/zitadel_provision.py).
