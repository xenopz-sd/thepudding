# Deferred Acceptance-Criteria Ledger (WS-A1)

> **What this is.** A read-only audit of every implemented-service and
> orchestration `validation.md` in the repository, recording the live-evidence
> status of each acceptance criterion (AC) / functional requirement (FR) and
> classifying every *deferred* one as either a legitimate dependency block or a
> real accept-and-move-on gap. It is the single source of truth for "what did we
> actually prove live vs. what did we write down and defer." It feeds the
> methodology work items in `PLANNING.md` (WS-M*, WS-A2, WS-C*).
>
> **Scope.** Implemented-service + orchestration specs only. The `fix-*` specs
> are deliberately excluded — they are symptoms, not instances of the
> accept-with-deferral pattern this audit is about. Where a `fix-*` spec *closed*
> a deferral it is cited inline.
>
> **This is analysis, not a spec.** No code, no cluster, no `validation.md` was
> modified by this audit. The reconciliation actions it recommends (fixing stale
> docs, running deferred live tiers) are backlog items below, not done work.

## Evidence vocabulary

| Tag | Meaning |
|---|---|
| **CONFIRMED-live** | Exercised against real infrastructure (throwaway cluster) and passed. |
| **CONFIRMED-offline-only** | Proven by the offline suite (unit / property / swap-gate / render / `terraform validate`) but never run live. |
| **CONFIRMED-doc** | Satisfied by a decision/runbook artifact (e.g. a documented fallback), not a runnable test. |
| **DEFERRED(legit-blocked)** | Deferred because a hard dependency (usually SVC-08/EJBCA, or a GPU/SVC-23) does not exist yet. A mock stands in, fail-closed. Not a gap. |
| **DEFERRED(real-gap)** | Deferred by choice — accept-and-move-on. The acceptance was never actually closed and nothing external blocks it. These are the backlog. |
| **CLOSED-by-descope** | No longer a requirement; removed by a constitution decision-log entry. Not a gap. |
| **CLOSED-by-later-work** | A later spec/ADR closed it; the `validation.md` may still read as open (stale doc). |
| **flip-now-available** | Was dependency-blocked; the dependency now exists, so the opt-in flip is possible but has never been live-verified. Same risk class as a real-gap. |

---

## Per-service AC tables

### SVC-01 — SQL database (PostgreSQL)

| AC / FR | Evidence status | Classification | Note |
|---|---|---|---|
| FR-1..FR-5 (provision, SDN, compute, inventory, bring-up skeleton) | CONFIRMED-live | — | TF + provisioning layer proven live. |
| FR-6 OpenBao credential write | DEFERRED | **real-gap** | Live run used `openbao_addr=''` — the credential was never written to OpenBao. |
| FR-7 / AC-7 MariaDB full DB bring-up | DEFERRED | **real-gap** | Only the Terraform layer ran live; local-backend state-collision artifact + the role is still a skeleton. |
| gap(c) PostGIS / TimescaleDB extension images | DEFERRED | **real-gap** (deliberate) | Only `pgvector` is wired; the other extension images are declared-not-wired. |
| FR-8 clean-slate (`svc01_clean_slate`, `clean_slate=true`) | DEFERRED | **real-gap** | Reset half never exercised live; guests were torn down by hand (the anti-pattern the installer contract forbids). |
| gap(d) VLAN-20 `host_index=0` collision | CLOSED-by-later-work | — | Addressed by ADR-0015 host-index authority. `validation.md` predates the fix. |

### SVC-06 — Identity provider (ZITADEL)

| AC | Evidence status | Classification | Note |
|---|---|---|---|
| AC-1 deploy / health | CONFIRMED-live | — | |
| AC-2 org + OIDC-app provisioning | CONFIRMED-live | — | Closed by v0.14.0 AC-3 work, but **§4 still lists it as open** — stale doc. |
| AC-3 interactive login (Login V1) | CONFIRMED-live | — | v0.14.0 §7 full round-trip to callback + code. |
| AC-4 MFA-enforceable | DEFERRED | **real-gap** | Only "config verified"; never live-proven that login is actually blocked without a second factor. → **WS-A2**. |
| AC-5 secrets only in OpenBao | CONFIRMED-live | — | |
| AC-6 metrics / logs | CONFIRMED-live | — | Metrics endpoint wired; dcgm n/a. |
| AC-7 Keycloak fallback | CONFIRMED-doc | — | Documented fallback, legitimately doc-only. |
| AC-8 one-command install + clean-slate | CONFIRMED-live | — | Bring-up + teardown both exercised. |
| AC-9 | CONFIRMED-live | — | |
| backing-DB `sslmode=disable` | DEFERRED | **legit-blocked** | Blocked on SVC-08/EJBCA cert. |

> **Stale-doc finding (SVC-06):** §4 lists "org-provisioning deferred" and "real
> Traefik ingress not exercised" as open, but §7 (v0.14.0) closed both. §4 was
> never reconciled with §7.

### SVC-07 — Secrets manager (OpenBao), two specs

**Installer level (`svc-07-automated-installer`)**

| Property / AC | Evidence status | Classification | Note |
|---|---|---|---|
| From-scratch bring-up | CONFIRMED-live | — | 2026-09-30 `failed=0`; re-confirmed by this session's v0.14.0 run w/ `openbao_mint_admin_token`. |
| Property 7 idempotent re-run | DEFERRED | **real-gap** | Second-run `changed=0` never proven live. |
| Properties 5,6 clean-slate round-trip | CONFIRMED-live (partial) | — | Recorded PENDING, but this session ran svc07 clean-slate to empty (1070/1071 destroyed, state emptied). **Doc lags reality.** |

**Contract level (`svc-07-secrets-manager` §5)**

| Tier | Evidence status | Classification | Note |
|---|---|---|---|
| `requires_infra` contract tier (SEC.* rows, OpenBao role-shape, auto-unseal contract, live degraded) | DEFERRED | **real-gap** | Written but never run live — the entire live contract tier. |
| Ansible check-mode idempotency (Req 13) | DEFERRED | **real-gap** (offline-able) | Needs ansible + sudo; runnable offline, never run. |
| AWS / Garage engine (Req 3.4–3.6) | CLOSED-by-descope | — | AWS engine dropped per constitution decision-log. Not a gap. |

> **Stale-doc finding (SVC-07, strong):** `svc-07-automated-installer`
> `validation.md` is **internally contradictory** — Merge-Readiness + "Live
> from-scratch acceptance" say LIVE-VALIDATED `failed=0`, while Known-Gaps §1 says
> "NOT PERFORMED" and §4 says the run "halts at bug #7 unsealer readiness race."
> Never reconciled as live state advanced across progressive runs. This is the
> clearest instance of the methodology defect (see cross-cutting findings).

### SVC-09 — Reverse proxy (Traefik)

| FR | Evidence status | Classification | Note |
|---|---|---|---|
| FR-1,4,5,6,7,8,9,10,11,12 | CONFIRMED-live | — | Clean bring-up, real `edge` owned, 301 redirect, auto-discovery 200, self-signed mock. FR-7 wired but not forward-auth-live. |
| FR-2 Let's Encrypt real issuance | DEFERRED | **legit-blocked** | Blocked on SVC-08/DNS; Mock 3 stands in fail-closed. |
| FR-3 internal CA | DEFERRED | **legit-blocked** | Blocked on SVC-08. |
| Real ZITADEL dashboard forward-auth | DEFERRED | **flip-now-available** | SVC-06 now deployed (v0.14.0); `traefik_dashboard_auth_disabled=false` flip is possible but opt-in and never live-verified against live ZITADEL. Same class as SVC-06 AC-4. |
| `traefik:v3.6` pin | — | — | Deliberate, not a gap. |

> **Positive note:** SVC-09 is the cleanest-audited service — its deferrals are
> genuinely dependency-blocked, not accept-and-move-on.

### SVC-17/18 — Observability (Prometheus / Grafana / Loki)

| AC | Evidence status | Classification | Note |
|---|---|---|---|
| AC-1 preflight/provision | CONFIRMED-live (partial) | — | |
| AC-2 three-guest layout, distinct IPs | CONFIRMED-live | — | `.18/.19/.20`, ledger rows distinct. |
| AC-3 4/4 `up == 1` | CONFIRMED-live | — | Closed across 3 obs bugfixes; zero manual `prometheus.yml` edit. |
| AC-4 exporters up | CONFIRMED-live | — | node_exporter + cAdvisor + Promtail healthy on all 3 guests. |
| AC-5 Grafana via Traefik | CONFIRMED-live | — | Originally BLOCKED; RESOLVED by ADR-0018 (traefik-cross-guest-ingress). Doc explicitly flipped BLOCKED→CONFIRMED. |
| AC-10 clean-slate empty-baseline | CONFIRMED-live | — | Full two-way empty-baseline gate, `failed=0`. |
| AC-6 logs queryable in Grafana via Loki | DEFERRED | **real-gap** (bounded, not blocked) | In-Grafana functional assertion not re-exercised after ingress unblocked. |
| AC-7 baseline/test alert fires to receiver | DEFERRED | **real-gap** (bounded) | |
| AC-8 full stack idempotent 2nd run `changed=0` | DEFERRED | **real-gap** | Cross-guest route path alone proven `changed=0`; full stack not. |
| AC-9 fully-green one-command from-scratch | DEFERRED | **real-gap** | Orchestrated path hit a first-run transient a direct re-run cleared — chase the transient. |
| dcgm GPU metrics (Mock C) | DEFERRED | **legit-blocked** | No GPU / SVC-23 absent. |

> **Positive note:** SVC-17/18 `validation.md` is the best-reconciled doc in the
> repo — it flips AC-5 explicitly as the follow-up closes it and tracks the three
> obs bugfixes that closed AC-3. Direct contrast with SVC-07.

### Orchestration + cross-guest + workload-auth specs

| Spec | Live status | Classification | Note |
|---|---|---|---|
| svc-06-cross-guest-ingress | CONFIRMED-live | — | task 6.2 DONE+PASSED (ZITADEL via Traefik h2c cleartext hop). `zitadel-org-provision` Phase-3 smoke failure = out-of-scope pre-existing role defect, later closed by v0.14.0 `fix-zitadel-onboarding-role-keys-assertion`. |
| svc-07-cross-guest-ingress / traefik-cross-guest-ingress | CONFIRMED-live | — | Grafana `https://…` → HTTP 302 through Traefik on `.12`. Shared mechanism for OpenBao + ZITADEL. |
| platform-prerequisites-bootstrap | CONFIRMED-live | — | §5 known-limits both LEGIT: (1) token file mode 755-not-0600 = environmental CIFS mode-display artifact; (2) cross-service clean-slate scoping deferred to "Fix B" = platform-clean-slate. |
| project-consumer-onboarding | DEFERRED | **real-gap** (mostly closed by session) | Reqs 1–11 offline-CONFIRMED; all Task-10 requires-infra (10.1–10.5) recorded PENDING (AGENT_LIVE_AUTHORIZED unset at authoring). dronefleet onboarding + clean-slate WERE exercised live this session — **doc lags reality**. |
| openbao-workload-auth | DEFERRED | **real-gap** | Offline tier (swap-gate/structure/render/contract/doc) proven; requires-infra tasks 8.1–8.3 PENDING — 4 fail-closed runtime scenarios (sealed Primary → no rendered secret file; Secret_ID invalid/expired; `OPENBAO_ADMIN_TOKEN` unset; `policy-<slug>` absent). Fail-closed STRUCTURE proven offline; runtime behaviour never run live. |

---

## Real-gap backlog (one row per accept-and-move-on gap)

These are the gaps that are **not** dependency-blocked and **not** already
closed — each needs a live run or a wiring fix to actually close its acceptance.

| # | Service | Gap | What closes it |
|---|---|---|---|
| G1 | SVC-01 | FR-6 OpenBao credential write never ran (`openbao_addr=''`) | Live bring-up with OpenBao addr set; assert credential in `secret/…`. |
| G2 | SVC-01 | FR-7/AC-7 MariaDB full DB bring-up (skeleton role + state collision) | Finish the MariaDB role; live bring-up past the TF layer. |
| G3 | SVC-01 | PostGIS / TimescaleDB extension images not wired | Wire the extension images or descope explicitly in the spec. |
| G4 | SVC-01 | FR-8 clean-slate reset never run live | Run `svc01_clean_slate -e clean_slate=true` live to empty-baseline. |
| G5 | SVC-06 | AC-4 MFA-enforceable only config-verified | Live login proving a second factor is actually required. → **WS-A2**. |
| G6 | SVC-07 | Property 7 installer idempotent re-run | Second live run asserting `changed=0`. |
| G7 | SVC-07 | Entire `requires_infra` contract tier written-never-run | Run SEC.* / role-shape / auto-unseal / live-degraded against a live Primary. |
| G8 | SVC-07 | Ansible check-mode idempotency (Req 13) | Run the offline-able idempotency tests (ansible + sudo). |
| G9 | SVC-09 | Real ZITADEL dashboard forward-auth flip | Flip `traefik_dashboard_auth_disabled=false` against live ZITADEL; verify auth. (flip-now-available) |
| G10 | SVC-17/18 | AC-6 logs queryable in Grafana via Loki | In-Grafana Loki query assertion, live. |
| G11 | SVC-17/18 | AC-7 baseline/test alert fires to receiver | Fire a test alert; assert receiver delivery. |
| G12 | SVC-17/18 | AC-8 full stack idempotent 2nd run | Second live run of the full stack asserting `changed=0`. |
| G13 | SVC-17/18 | AC-9 fully-green one-command from-scratch (first-run transient) | Reproduce + fix the orchestrated-path transient. |
| G14 | consumer-onboarding | Task-10 (10.1–10.5) recorded PENDING though exercised this session | Re-run under armed interlock OR reconcile the doc to the session's live evidence. |
| G15 | openbao-workload-auth | Tasks 8.1–8.3 fail-closed runtime never run live | Run the 4 fail-closed scenarios against a live sealed/unsealed Primary. |
| G16 | svc-07-automated-installer | **Internally contradictory doc** — Merge-Readiness says LIVE-VALIDATED `failed=0`; Known-Gaps §1 says NOT PERFORMED; §4 says halts at unsealer race. | Rewrite to the current live truth (from-scratch passes; clean-slate exercised this session; contract tier still pending). |
| G17 | svc-06-identity-provider | **Stale doc** — §4 lists org-provisioning + real Traefik ingress as open; §7 (v0.14.0) closed both. | Reconcile §4 against §7. |
| G18 | svc-01-sql-database | **Stale doc** — gap(d) host_index collision listed open; closed by ADR-0015. | Mark gap(d) CLOSED-by ADR-0015. |
| G19 | svc-07-automated-installer | **Stale doc** — clean-slate round-trip recorded PENDING; exercised live this session. | Record the session's live clean-slate evidence. |
| G20 | project-consumer-onboarding | **Stale doc** — Task-10 recorded PENDING; dronefleet onboarding + clean-slate exercised live this session. | Record the session's live evidence or re-run under armed interlock. |

G1–G15 are missing-live-evidence / incomplete-wiring gaps; G16–G20 are the
stale-doc reconciliation gaps (the methodology defect class below). All twenty are
the single actionable backlog this audit produces.

---

## Stale-doc / needs-reconcile (methodology defect class — backlog rows G16–G20)

`validation.md` files that were **not updated as live state advanced**, so they
now mis-report their own status. This is itself a defect class — a reviewer
trusting the doc would reach the wrong merge decision. Feeds **WS-M3**
(live-acceptance lifecycle gate) and argues for a `validation.md` linter/reconcile
step.

The actionable reconcile work lives in the backlog above as **G16–G20** — this
section is the *finding* (why those rows exist); the rows are the *action*. Doing
the reconciliation is backlog work, deliberately **not** performed by this
read-only audit.

---

## Legit-blocked list (dependency, not a gap — do not action until the dependency lands)

| Spec | Deferred AC | Blocking dependency |
|---|---|---|
| SVC-06 | backing-DB `sslmode=disable` | SVC-08 / EJBCA cert |
| SVC-09 | FR-2 Let's Encrypt real issuance | SVC-08 / DNS (Mock 3 fail-closed) |
| SVC-09 | FR-3 internal CA | SVC-08 |
| SVC-17/18 | dcgm GPU metrics (Mock C) | GPU hardware / SVC-23 |

---

## Headline findings

1. **The deferral pattern is systemic but mostly benign.** Of the deferred ACs,
   the large majority are either **dependency-blocked** (SVC-08/EJBCA, GPU/SVC-23
   — legitimate, mocked fail-closed) or **already closed** by later work this
   session and in v0.14.0 (SVC-07 clean-slate, consumer onboarding, SVC-06
   org-provision/AC-3, SVC-17/18 AC-3/AC-5).

2. **The genuine accept-and-move-on gaps are concentrated**, not scattered: the
   SVC-07 live contract tier (G7), SVC-06 AC-4 MFA (G5 = WS-A2), and the SVC-01
   service completion (G1–G4). SVC-17/18's AC-6/7/8/9 (G10–G13) are bounded and
   unblocked — cheap to close with one good live run.

3. **Stale/unreconciled `validation.md` is its own defect class.** SVC-07's
   installer doc is internally contradictory; several docs lag the live reality
   this session established. A reviewer trusting these docs would merge on false
   evidence. This is the strongest argument in the audit for a
   live-acceptance-lifecycle gate (WS-M3) — the `validation.md` must be updated
   in the same step that advances live state, and ideally a linter should flag a
   doc whose Merge-Readiness and Known-Gaps sections disagree.

4. **"PENDING live" ≠ "not done."** Multiple ACs marked PENDING were in fact
   exercised live this session; the docs simply were not updated. Closing the
   stale-doc gap would reclassify G14 and part of G6/G7 immediately.
