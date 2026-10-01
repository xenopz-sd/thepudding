# `openbao_init_unseal` Ansible role

One-time initialisation and auto-unseal wiring for OpenBao (SVC-07 Secrets
Manager). This role covers the **server-side bootstrap** of the two OpenBao
LXCs — it runs after [`openbao_install`](../openbao_install/README.md) has
templated `config.hcl` and brought up the Compose stacks.

It is distinct from the downstream `openbao-agent` sidecar every *other* service
runs (out of scope for SVC-07).

> **Task scope note:** this role implements the guard **and** the FULL bootstrap
> of **both** OpenBao instances — the unsealer and the primary:
>
> - **(b) a `bao status` / health guard** — reads `bao status` via `docker exec`,
>   derives `openbao_already_initialized`, and gates every init step so a re-run
>   against an already-initialised instance is a no-op reporting `changed=0`
>   (Req 13.3). The read is check-mode-safe (`check_mode: false`,
>   `changed_when: false`) so it evaluates even under `--check` and never itself
>   reports a change.
> - **(c) unsealer full Raft bootstrap** — implemented in
>   [`tasks/unsealer_bootstrap.yml`](./tasks/unsealer_bootstrap.yml), included
>   from `tasks/main.yml` and gated on `openbao_role == "unsealer"` **and**
>   `not openbao_already_initialized`. The role OWNS the unsealer's own
>   readiness-guarded Raft `operator init` (Shamir keys), threshold
>   `operator unseal`, post-unseal readiness wait, Transit-engine enable +
>   unseal-key `openbao-unseal` create at mount `transit/`, the `openbao-seal`
>   scoped policy, the periodic `Bootstrap_Transit_Token` mint, and the unsealer
>   root-token revoke. It no longer *assumes* an already-initialised unsealer.
>   See "The unsealer bootstrap flow" below. Per the design's sequence, the
>   **unsealer is bootstrapped first**, then the primary.
> - **(6.2) primary Raft init + Root_Token bootstrap + revoke + token rotation**
>   — implemented in [`tasks/primary_bootstrap.yml`](./tasks/primary_bootstrap.yml),
>   included from `tasks/main.yml` and gated on `openbao_role == "primary"` and
>   `not openbao_already_initialized`. See "The task-6.2 bootstrap flow" below.
>
> The KV/database/`aws` engines, `jwt`/`oidc` auth, and the audit device are
> **task 7.x**. Per the ordering contract, task 7.x's block runs **inside** the
> root-token window that 6.2 opens — at the marked extension point in
> `tasks/primary_bootstrap.yml`, **before** the Root_Token revoke — not at the
> end of `tasks/main.yml`.
>
> **Task 10.1 (observability)** adds the Prometheus (SVC-17) scrape-config
> contribution — see "The task-10.1 Prometheus scrape config" below. Unlike
> task 7.x it needs **no** Root_Token, so it runs at the end of `tasks/main.yml`
> (in `tasks/observability.yml`), outside the bootstrap window.

## The task-10.1 Prometheus scrape config (`tasks/observability.yml`)

SVC-07 contributes a Prometheus **scrape-config fragment** to the shared SVC-17
observability stack, mirroring how task 7.2 contributes the Promtail fragment to
Loki (SVC-18) rather than owning the collector. SVC-17 is a separate catalog
service; a full Prometheus role may not exist yet, so this task renders the
scrape-config drop-in SVC-07 owns (`templates/prometheus-openbao.yml.j2`) onto a
Prometheus scrape-config directory the SVC-17 Prometheus loads. It declares two
jobs (design's *Component topology*):

- **job `openbao`** — scrapes the **primary's**
  `/v1/sys/metrics?format=prometheus` over TLS at a `scrape_interval` of **≤ 30 s**
  (Req 10.2, 10.3), labelled `service=openbao`, `vlan=20`.
- **job `openbao_unsealer`** — scrapes the **unsealer** so
  `up{job="openbao_unsealer"}` is queryable; the `OpenBaoUnsealerUnreachable`
  alert (task 10.2) keys off that series (Req 10.2).

**Metrics auth.** OpenBao's `/v1/sys/metrics` requires a token by default. The
primary sets `unauthenticated_metrics_access = true` in its telemetry stanza
(`openbao_install` `config.hcl.j2`, via `openbao_telemetry_unauthenticated_metrics`),
so this scrape carries **no** bearer token — the endpoint exposes only
operational counters (no secret material) and the primary's `:8200` is reachable
only over the internal VLAN-20 path (Req 6.4 / Security AC 6), never through
Traefik for the metrics route. The authenticated-scrape alternative (a scoped
metrics-read token in a scrape `authorization.credentials_file`) is referenced
**by file path only** (`openbao_prometheus_scrape_bearer_token_file`), never a
committed literal — flip the flag to `false` and uncomment that line to use it.

**Sealed-metric guarantee (Req 10.8 / FD.7).** `openbao_core_unsealed = 0` is
exposed on the metrics endpoint even while the primary is **sealed** — this is
OpenBao-native (the metric is scrapeable without the server being unsealed), so
this task does not implement it; it only points a scrape at the endpoint. The
guarantee is **verified** by the degraded-mode tests (tasks 12.4 / 12.5).

## The unsealer bootstrap flow (`tasks/unsealer_bootstrap.yml`)

Runs once on the **unsealer only** (`openbao_role == "unsealer"` and
`not openbao_already_initialized`), mirroring the primary flow so the unsealer
path gains the same readiness guard and idempotency. It is what the orchestrator
(`svc-07-bootstrap.yml`) now **delegates** to, instead of hand-rolling the
`docker exec` init/unseal/policy/mint/revoke in the orchestrator without a
readiness guard (the root cause of bug #7). Ordered steps:

1. **Readiness wait BEFORE init** — poll `bao status` (via `docker exec`) until
   the API answers with a parseable body, `openbao_readiness_retries` ×
   `openbao_readiness_delay`s. If it never answers, fail with a **named
   readiness-timeout** (Req 1.2) rather than a raw `operator init` non-zero exit.
2. **`bao operator init`** (Raft) — the unsealer config has **no** seal stanza
   (it *is* the seal provider), so this yields **Shamir** unseal keys
   (`unseal_keys_b64`), not recovery keys. Shamir keys + Root_Token captured in
   in-memory `no_log` facts, never persisted.
3. **Threshold `operator unseal`** over the first `openbao_unsealer_key_threshold`
   Shamir keys, then a **post-unseal readiness wait** until `initialized:true`
   **and** `sealed:false` before any write (Req 1.3).
4. **Transit engine** — enable the Transit secrets engine (tolerates
   already-in-use) and create the unseal key `openbao-unseal` (create-if-absent).
   *Relocated here* from `tasks/main.yml`'s old `(c)` block so it runs inside the
   same guarded include, after the post-unseal readiness wait.
5. **`openbao-seal` scoped policy** — `bao policy write openbao-seal -` (stdin
   HCL) granting `update` on `transit/encrypt/openbao-unseal` and
   `transit/decrypt/openbao-unseal` **only** — never a root token as the seal
   token.
6. **Mint the periodic `Bootstrap_Transit_Token`** against `openbao-seal`
   (`-period=768h -field=token`), captured in an in-memory `no_log` fact.
7. **Handoff** — under the mint + dev-expose opt-in
   (`openbao_mint_seal_token` **and** `openbao_dev_expose_secrets`, with a
   non-empty `openbao_seal_token_handoff_path`), write the token to a **0600**
   control-node handoff file (`delegate_to: localhost`, `no_log`) so the
   orchestrator can read it across the child-process boundary, then delete it.
8. **Revoke** the unsealer root token (`bao token revoke -self`, with the
   **root** token — not the scoped seal token), then **scrub** the Root_Token,
   Shamir keys, and seal-token facts from memory.

**Root-token source reconciliation.** On a **fresh** init the writes in steps
4–6 authenticate with the **in-memory Root_Token** captured at step 2 (the
env-var `OPENBAO_UNSEALER_ROOT_TOKEN` is unset on a brand-new unsealer). The role
resolves `openbao_unsealer_effective_token` = the in-memory root token when
present, else the env-var root token — so the same tasks work on both the fresh
path and any already-configured path. Every secret-bearing task sets
`no_log: "{{ openbao_no_log }}"`.

## The task-6.2 bootstrap flow (`tasks/primary_bootstrap.yml`)

Runs once, after the unsealer's Transit engine is up, on the primary only:

1. **Resolve** the Bootstrap_Transit_Token from an operator-supplied,
   **never-committed** external source — the env var
   `OPENBAO_BOOTSTRAP_TRANSIT_TOKEN`, populated **either** by a local gitignored
   `.env` (homelab) **or** a GitLab CI/CD protected+masked variable (pipeline).
   Neither is hardcoded as the only source. If neither supplied it, the flow
   **fails closed** (Req 5.4; Security AC 1, 7).
2. **Wire** the token into the primary's `seal "transit"` stanza by re-invoking
   `openbao_install` (which owns the `config.hcl` template and its var set) with
   the resolved token — no template/var duplication here.
3. **`bao operator init`** on the primary (Raft) once; capture the Root_Token in
   an in-memory `no_log` fact (recovery keys are captured offline by the
   operator, never persisted by this automation).
4. **Wait** for the primary to report `initialized: true` **and** `sealed: false`
   (auto-unseal via the unsealer).
5. **Bootstrap** the `platform-admin` policy with the Root_Token (Security AC 2)
   and mint a tightly-scoped, short-TTL, non-renewable **KV-writer token** for
   the one transit-token KV path.
6. **Task 7.x window** — engine/auth/audit enablement runs here, before the
   revoke (see the ordering contract at the marker in that file).
7. **Revoke** the Root_Token (`bao token revoke -self`) and scrub it from memory
   (Security AC 1).
8. **Rotate** the token into KV `secret/platform/svc07/transit-token` using the
   scoped KV-writer token, then revoke that writer token too (Req 5.5).
9. **Remove** the external copy — strip the line from the gitignored `.env`
   (homelab) or DELETE the GitLab CI/CD variable (pipeline); if the source can't
   be reached, **fail loudly** rather than silently skip (Req 5.5; Security AC 7).
10. **Scrub** the token fact from memory.

Every task that touches the token / Root_Token / KV-writer token / API token
sets `no_log: true`. No secret value is ever committed, defaulted, or written to
a vars file — only the env-var *name* is configured.

## Ordering

Per the design's *Init/unseal/bootstrap sequence*, the **Transit unsealer is
bootstrapped first** (its Transit engine + minted seal token are what the
primary auto-unseals against), and only then is the primary's Raft store
initialised (task 6.2). This role expresses that order: the unsealer bootstrap
include (`unsealer_bootstrap.yml`) runs ahead of the task-6.2 primary-init
extension point. At the playbook level the two ordered plays in
`ansible/playbooks/openbao.yml` (unsealer first, then primary) make this a
structural guarantee.

## Role layout

```
openbao_init_unseal/
  defaults/main.yml         # addressing/naming vars, health-guard config, transit key/mount, unsealer-bootstrap + §7.x + §10.1 vars
  tasks/main.yml            # (b) status guard + (c) unsealer-bootstrap include; 6.2/7.x/10.1 extension points
  tasks/unsealer_bootstrap.yml  # unsealer Raft init + unseal + Transit + seal policy + seal-token mint + revoke
  tasks/primary_bootstrap.yml   # task 6.2 primary Raft init + bootstrap + revoke + token rotation
  tasks/engines_auth_audit.yml  # task 7.x KV/database/aws engines, jwt/oidc auth, file audit, Promtail
  tasks/observability.yml       # task 10.1 Prometheus (SVC-17) scrape-config fragment
  handlers/main.yml         # empty skeleton (6.2/7.x may add handlers)
  templates/
    platform-admin.hcl.j2       # task 6.2 break-glass policy
    promtail-openbao.yml.j2     # task 7.2 Loki (SVC-18) audit-log tail fragment
    prometheus-openbao.yml.j2   # task 10.1 Prometheus (SVC-17) scrape fragment
  meta/main.yml
  README.md
```

## The `bao status` guard (Req 13.3)

The guard reads `GET /v1/sys/health` for the current instance (selected by
`openbao_role`) and sets `openbao_already_initialized` from the body's
`initialized` flag. Every init task is gated on `not
openbao_already_initialized`, so:

- a **fresh** instance -> init proceeds (the unsealer runs its full
  `operator init` + unseal + Transit + seal-policy + mint + revoke; the primary
  runs its Raft init/bootstrap);
- an **already-initialised** instance -> init is skipped, the play reports
  `changed=0` (Req 13.3), and no token is revoked and no secret deleted (the
  guarantees task 6.3's check-mode test asserts).

The read is deliberately tolerant: it runs `bao status` INSIDE the target
container via `docker exec` (not a host-side `uri` GET — the primary publishes no
host port), tolerates a self-signed cert during bootstrap
(`-tls-skip-verify`), and treats a sealed/uninitialised/down instance as simply
"not initialised yet" rather than a hard failure (`failed_when: false`). (The
former `openbao_health_*` uri vars were removed when the guard moved to
`docker exec`.)

## Swap hardening

Swap hardening is handled per [`ADR-0006`](../../../.kiro/decisions/0006-openbao-swap-hardening-not-in-container-swappiness.md):
the "secret material never persists to plaintext swap" control is **encrypted
host swap** (operator-owned, host layer) plus the **per-container swap cap**
(`--memory-swappiness=0` / `lxc.cgroup2.memory.swap.max = 0` / Terraform
`swap = 0`) applied by `openbao_install` and the Terraform module. This role no
longer sets a host-global `vm.swappiness` — that write was unrunnable on an
unprivileged LXC, cross-tenant, and only a heuristic.

## Key variables

See [`defaults/main.yml`](./defaults/main.yml) for the full set. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `openbao_role` | `primary` | Selects the target instance (`primary` \| `unsealer`). |
| `openbao_primary_addr` | `https://10.0.20.10:8200` | Primary health-guard target. |
| `openbao_unsealer_addr` | `https://10.0.20.11:8200` | Unsealer health-guard target + seal endpoint. |
| `openbao_transit_mount_path` | `transit` | Transit engine mount on the unsealer. |
| `openbao_transit_key_name` | `openbao-unseal` | Unseal key name (must match the primary's seal stanza). |
| `openbao_transit_key_type` | `aes256-gcm96` | Transit key type. |
| `openbao_unsealer_key_shares` | `5` | Shamir shares for the unsealer's own `operator init`. |
| `openbao_unsealer_key_threshold` | `3` | Shamir threshold to unseal the unsealer. |
| `openbao_seal_policy_name` | `openbao-seal` | Scoped policy the seal token is minted against (encrypt/decrypt only). |
| `openbao_seal_token_period` | `768h` | Period of the minted `Bootstrap_Transit_Token`. |
| `openbao_mint_seal_token` | `false` | Opt-in: mint + hand off the seal token (parallels `openbao_mint_admin_token`). |
| `openbao_seal_token_handoff_path` | `""` | 0600 control-node handoff file (set by the orchestrator); empty = no handoff. |
| `openbao_readiness_retries` / `_delay` | `12` / `5` | Readiness-guard bounded-poll budget (retries × seconds). |

**No secret value lives in this role.** The Bootstrap_Transit_Token is *not* a
variable defaulted to a value — it arrives at bootstrap (task 6.2) from an
operator-supplied external source (a local gitignored `.env` or a GitLab CI/CD
protected+masked variable), is used once, then rotated into KV
(`secret/platform/svc07/transit-token`) with the external copy removed. No
Root_Token, recovery key, or unseal key is ever written to any file here.

## Verify the role (offline, Tier-1)

Lint the role's YAML and structure. Requires `ansible-lint`/`yamllint` on PATH;
invoke the project venv binaries by absolute path per `dev-workflow.md`.

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/ansible-lint roles/openbao_init_unseal
```

<!-- doctest: offline -->
<!-- cwd: ansible -->
```bash
~/venv/devinfra/bin/yamllint roles/openbao_init_unseal
```

> Note: `ansible`/`ansible-lint` are not currently installed in the project
> venv. Until they are, the role is verified offline by parsing every YAML file
> with PyYAML and asserting the structural contract (the status guard gates the
> init tasks; the unsealer Transit engine is enabled and its unseal key
> created). The Ansible check-mode idempotency test — asserting `changed=0`
> against an
> already-initialised fixture — is **task 6.3**.
