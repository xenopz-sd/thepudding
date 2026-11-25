# PLANNING.md — Audit & Methodology-Improvement Planning

> **Status: planning only.** This file scopes work we will turn into specs
> (`.kiro/specs/<name>/`) afterwards. Nothing here is implemented. No cluster or
> `main` change follows from this document directly — each work stream below
> enters the normal `spec-lifecycle.md` flow (requirements → design → tasks →
> validation) on its own branch.
>
> It covers three things the operator asked for:
> 1. **Methodology analysis** — why there are so many `fix-*` specs, and how to
>    reduce the need for them (more accurate specs, better tests, steering, MCP,
>    skills).
> 2. **Deferred-but-accepted AC audit** — the original audit trigger.
> 3. **Eight concrete carry-over items** surfaced during the v0.14.0 work
>    (teardown bugs, discovery gap, workload clean-slate, operator-user path,
>    §5b handler flush, openbao-agent dev-TLS wiring, SVC-06 spec-format).
>
> **How we will use this file:** review/prioritize together, then convert each
> "Work stream" into a spec (or a decision-log entry where it is a decision, not
> code). Work-stream IDs (WS-n) are stable handles for that conversion.

---

## Part A — Evidence gathered (read-only)

### A.1 Spec-count signal

- **~98 spec directories total; ~56 are `fix-*`.** Over half of all specs are
  bugfixes. That ratio is the core symptom this planning addresses.
- **Cluster distribution** (by fix-spec name):
  - **SVC-07 / OpenBao (~25):** unsealer bootstrap/readiness/reseal/leadership,
    primary health/recreate/seal-token, clean-slate residue (terraform_data,
    proxmox-host default), metrics listener/port, onboard path/KV/registry/TLS,
    sysctl/swap, SDN NIC double-tag, transit-token handoff.
  - **Consumer / workload (~12):** db-bridge KV path, db-secret workload path,
    manifest app-type mapping, onboarding prerequisites, ingress/OIDC, workload
    creds dir/scope/staging, datastore autodetect, hello-workload cold-boot
    RSF race / CGI secret visibility.
  - **ZITADEL / SVC-06 (~9):** clean-slate backing-db / provision-token residue,
    org-provision delegate_to crash, compose ingress version-gate, db app-password
    mismatch, role-keys assertion, org-provision production-path TLS, login-v1.
  - **Platform / cross-cutting (~10):** cluster-env endpoint-path / quote-strip,
    vlan-host-index collision, sdn-privilege model, sdn-gateway project-vlan,
    live-apply backend-init, offline terraform-plan tests, proxmox-compute VMID.

> Counts are directory-name classification, not a line-by-line read of all 56
> specs; clusters and sub-themes are accurate. Exact per-cause counts are part of
> WS-A1.

### A.2 Recurring bug sub-themes (the pattern behind the clusters)

Highest-value prevention targets:

1. **Clean-slate / teardown residue** — terraform_data left in state, backing-DB
   not chained, provision-token KV residue, proxmox-host default missing, the
   fabric-destroy TLS bug (WS-C1), the discovery gap (WS-C2), workload guest not
   destroyed (WS-C3). Teardown is under-specified and under-tested relative to
   bring-up.
2. **Delegated-task / connection wiring** — `delegate_to` to an IP-not-in-inventory
   running interpreter discovery, `BAO_ADDR`/host-header, `ansible_connection: ssh`
   vs `local`, and **`TF_VAR_proxmox_insecure` not propagated** (WS-C1). A whole
   fix-class is "the delegated command ran on the wrong host / with the wrong
   transport / without the insecure flag."
3. **First-run / ordering transients** — cold-boot RSF race, unsealer leadership
   race, db-install first-run transient, obs bootstrap first-run. Ordering and
   readiness gating that only a live wall-clock exercises.
4. **Cross-guest ingress** — the same `edge`-is-host-local discovery gap hit
   Grafana, then OpenBao, then ZITADEL as separate fix/feature specs (ADR-0018)
   rather than being designed once up front.
5. **Path / KV-shape mismatches** — KV v2 path, workload secret path, projects.yaml
   path, bound-claims maptype. Producer↔consumer string/structure contracts that
   no offline test pinned.

### A.3 Why the fixes themselves are high-quality (so the problem is upstream)

Each `fix-*/tasks.md` follows bug-condition test (fails on unfixed code) →
preservation test (pins baseline) → fix → re-verify → four-root offline suite,
with shared-symbol discipline. The fix *mechanism* is not the problem. The problem
is **where and when defects are caught**: almost all surface in a **live
`requires-infra` run**, after the spec and offline gate passed. That points at:
- specs/designs that don't model runtime wiring (delegation, TLS posture,
  readiness, teardown) concretely enough to test offline, and
- validation that accepts a service as done with the live acceptance **deferred**
  (see Part C).

### A.4 Tooling baseline (levers available)

- **MCP: empty** (`.kiro/settings/mcp.json` = `{"mcpServers": {}}`). Greenfield —
  a read-only Proxmox/ZITADEL/OpenBao MCP could let specs assert live API shapes
  at authoring time instead of discovering them in a live run.
- **No skills / hooks directory** under `.kiro/`. Another greenfield lever.
- **Steering is extensive and good** (19 files). Gaps are specific, not systemic —
  targeted additions beat rewrites.

---

## Part B — Methodology work streams (reduce the need for fixes)

> Goal: catch defects at spec/offline time, not in a late live run. Ordered by
> estimated leverage.

### WS-M1 — Runtime-wiring contract tests (highest leverage)
**Problem:** A.2 #2 and #5 (delegation/transport/TLS wiring and producer→consumer
path/KV contracts) are caught live because nothing offline pins them.
**Proposal:** a steering addition + a reusable offline test kit asserting, for
every delegated task: the delegate target resolves to an inventory host OR carries
explicit `ansible_connection`/`ansible_user`/`ansible_python_interpreter`; every
Terraform-invoking task's env carries the full `TF_VAR_*` set incl.
`proxmox_insecure`; every producer/consumer secret path is a single shared
constant, not two hand-written strings. **Deliverable:** `testing-strategy.md`
section + a `tests/installer/test_delegation_wiring.py`-style drift-guard other
services adopt. **Spec type:** steering + small offline-test feature.

### WS-M2 — Teardown parity with bring-up (clean-slate as a first-class contract) — **DONE ✅**
**Problem:** A.2 #1 — teardown is the biggest fix cluster. `service-installer.md`
§4 defines a clean-slate contract but it is under-tested and the platform
orchestrator has real bugs (WS-C1/C2/C3). **Proposal:** extend §4 into a testable
contract: every service clean-slate must (a) be discoverable by the platform
orchestrator, (b) chain its backing guests, (c) verify an empty baseline incl.
non-guest `terraform_data`, (d) run under the same env (incl. insecure) as
bring-up. Add a cross-service offline guard that every `svc-NN` with a bootstrap
has a registered clean-slate + discovery entry. **Spec type:** steering + offline
guard; WS-C1/C2/C3 are its first consumers.

> **DONE** (`.kiro/specs/teardown-parity-contract/`, based on the WS-C2 branch):
> `service-installer.md` §4.1 "Teardown-parity (mechanically enforced)" added +
> the cross-service offline guard `tests/installer/test_teardown_parity.py`
> (FR-1 clean-slate role+routing for every bootstrap; FR-2 shared-tenancy →
> platform-catalog membership; non-vacuity + no-rot). The guard immediately caught
> a real gap WS-C2 missed — **SVC-17/18 (`obs`) was a shared implemented service
> with a full clean-slate contract but uncatalogued** — which WS-M2 closes by
> adding the `svc17` catalog row. Scope note: the guard works at the
> bootstrap/role/catalog level; the WS-C3-class "every provisioned Terraform root
> has a teardown" check (per-root TF introspection) is a noted future WS-M2
> extension, not built. (The per-service clean-slate legs (b)/(c)/(d) above were
> delivered by WS-C2/C3 + the earlier residue-sweep fixes; WS-M2 is the
> discovery/parity guard that stops regression.)

### WS-M3 — "Live-acceptance-is-not-optional" gate in the lifecycle
**Problem:** Part C — services accepted with the live AC deferred, so the defect
surfaces later as a `fix-*`. **Proposal:** amend `spec-lifecycle.md` /
`testing-strategy.md` so a `requires-infra` AC may be marked **DEFERRED** only with
an explicit tracked backlog entry AND the service README/index marking it
"live-unverified" — never silently "done". Add a validation.md linter/skill that
flags any AC whose evidence is "PENDING LIVE"/"gated"/"deferred" without a linked
backlog item. **Spec type:** steering + validation-completeness check (hook or
skill). Directly prevents the AC-3 class.

### WS-M4 — Design-time live-shape capture (MCP / recorded fixtures)
**Problem:** A.2 #3/#4 and many ZITADEL/OpenBao fixes came from not knowing the
real API/field shape at authoring time (loginVersion oneof, instance loginV2
feature, KV v2 path, Raft leadership field). **Proposal:** evaluate a **read-only
MCP server** (Proxmox + ZITADEL + OpenBao) the agent queries during spec authoring
to pin real shapes into the design, plus committed recorded API-shape fixtures the
offline contract tests assert against. **Spec type:** spec/ADR (boundary-touching:
new tool + credentials) — decided with operator; MCP off by default, read-only.
The lever the operator named explicitly.

### WS-M5 — Cross-guest / cross-cutting design patterns captured once
**Problem:** the cross-guest ingress discovery gap (A.2 #4) was re-discovered three
times. **Proposal:** a short steering/pattern doc ("platform runtime patterns":
cross-guest ingress via file-provider route, delegated-task wiring, readiness
gating, dev-TLS posture) referenced by `cross-cutting-categories.md`, so a new
service's design must state how it handles each known pattern. **Spec type:**
steering (pattern catalogue).

### WS-M6 — Bugfix-workflow skill + spec-completeness hook (consolidate what works)
**Problem:** the bugfix methodology is good but lives implicitly in each
`tasks.md`. **Proposal:** capture it as a reusable **skill** (bug-condition →
preservation → fix → four-root suite → shared-symbol search) and a
**spec-completeness hook** checking a new `requirements.md` has the mandatory
cross-cutting analysis (all 7 categories) and a Day-0/teardown AC. Lower leverage
(doesn't reduce *incoming* bugs) but reduces variance and effort per fix.

---

## Part C — Deferred-but-accepted AC audit (the original trigger)

**Hypothesis confirmed by A.1/A.3 and a validation.md sweep:** the "accept with
live deferred" pattern is systemic, not isolated to SVC-06 AC-3.

### Confirmed candidates (read-only grep of `svc-*/validation.md`)
- **SVC-07 secrets-manager:** §5 "Known gaps" — "Live tier not executed"; SEC.*
  rows "VERIFIED (shape); live … WRITTEN, gated".
- **SVC-07 automated-installer:** idempotent re-run / clean-slate round-trip
  "PENDING LIVE"; §Known Gaps task 13.5 "NOT PERFORMED".
- **SVC-01 sql-database:** FR-6 OpenBao write "PARTIAL (live gap)"; FR-7 MariaDB
  "PASS offline / live gap"; clean_slate "not exercised live" (torn down by hand —
  the anti-pattern testing-strategy forbids).
- **SVC-09 reverse-proxy:** FR-2/FR-3 LE/internal-CA deferred to SVC-08 (likely
  legit — dependency absent, mock stands in); confirm it's marked, not silent.
- **SVC-06 identity-provider:** AC-3 login (closed in v0.14.0); **AC-4 MFA
  enforceable — never live-verified**; old "org/OIDC-app provisioning deferred
  live" line.
- **SVC-06 / SVC-07 cross-guest-ingress, SVC-17/18:** "PENDING (live)" legends.

### WS-A1 — Deferred-AC audit (produce the ledger) — STARTED
**Deliverable:** a per-service table — every AC, evidence status (CONFIRMED-live /
CONFIRMED-offline-only / DEFERRED), and whether a deferral is (a) legitimately
blocked on an absent dependency, or (b) an accepted gap that should be a
live-verification backlog item. Output: an audit report doc + one backlog entry per
real gap. **Analysis, not code** — do it first; it feeds WS-M3 with real examples
and tells us which of SVC-01/06/07 need a live re-acceptance pass. **Spec type:**
audit doc (no behavior change), then specs for any gap needing code.

### WS-A2 — AC-4 (SVC-06 MFA enforceable) live verification — ✅ DONE + LIVE-VALIDATED (`.kiro/specs/svc06-mfa-live-acceptance/`; enforced MFA blocks no/skipped/wrong factor, correct TOTP passes)
Carved out because already named. **Scope LOCKED (operator-agreed):** the
acceptance is **prove MFA actually blocks a login live** — a login attempt that
supplies correct primary credentials but **no/invalid second factor must be
DENIED** against the live ZITADEL on the throwaway cluster, with the org MFA
login-policy set. The current state ("config verified") is explicitly **not**
acceptance; the live deny is. Formal re-scope of AC-4 is **not** chosen — we
verify. **Spec type:** live validation re-run (requires-infra), governed by
`AGENT_LIVE_AUTHORIZED` + the throwaway guard; needs the cluster rebuilt (see
"Live re-acceptance appetite" open question).

---

## Part D — Concrete carry-over items (from the v0.14.0 session)

> Specific, mostly small, mostly root-caused. Each becomes a `fix-*` spec (or a
> decision/backlog entry where noted). Several are first consumers of Part-B.

| ID | Item | Root cause (confirmed) | Type | Feeds |
|----|------|------------------------|------|-------|
| **WS-C1** | ~~Platform clean-slate fabric-destroy TLS bug~~ **RECLASSIFIED — stale root cause, see §D.1** | Recorded cause was "`platform_clean_slate_tf_env` omits `TF_VAR_proxmox_insecure`". **Refuted by read-only diagnosis:** committed v0.14.0 code's `platform_clean_slate_tf_env` *carries* `TF_VAR_proxmox_insecure` (default `true` via the `platform_clean_slate_proxmox_validate_certs: false` toggle), structurally identical to the working `platform_api_tf_env` and explicitly commented to mirror it. This is the documented platform-wide pattern (`cluster.env.example`: "There is NO cluster-env variable for this"), fixed for SVC-07 under `fix-cluster-env-endpoint-path` (TLS half). The live `x509` was almost certainly a pre-that-fix transient. | **live re-verify (not a fix-\*)** — rides along the next teardown | — |
| **WS-C2** | Platform clean-slate service-discovery gap — **FIXED + LIVE-VALIDATED ✅** | The `platform_clean_slate_services` catalog had one row (svc07), so svc06/svc09 were structurally undiscoverable. **Fixed:** added svc06 + svc09 rows + generalized the child-run to thread per-row `extra_vars` (JSON `-e`), so svc06's child gets `svc06_zitadel_vmid`. **Live-validated** on throwaway `shrimp`: platform clean-slate discovered `['svc07','svc09','svc06']` and tore all three down via their own clean-slates (`pct list` empty). Spec: `.kiro/specs/fix-platform-clean-slate-service-discovery/`. (Ride-along: confirmed WS-C1 is NOT a TLS bug; surfaced WS-C12.) | `fix-*` spec (DONE) | WS-M2 (canonical case, proven) |
| **WS-C3** | Consumer clean-slate doesn't destroy the workload guest — **FIXED + LIVE-VALIDATED ✅** | `consumer_clean_slate` composed the DB's own clean-slate (ADR-0011) and reversed OpenBao/ZITADEL objects, but **never destroyed the workload LXC**. **Fixed:** the clean-slate now composes `terraform_clean_slate` (via `clean-slate.yml`) for the workload guest, reverse-order before the DB, with a residue sweep + empty-baseline gate + the throwaway-guard consult. **Live-validated** on throwaway `shrimp`: `workload-100-01` (VMID 100001) provisioned, then torn down by the consumer clean-slate → `pct list` no 100001 + empty `dronefleet-workload` state; idempotent re-run `changed=0`. Spec: `.kiro/specs/fix-consumer-clean-slate-workload-guest-destroy/` (requirements/design/tasks/validation). | `fix-*` spec (DONE) | WS-M2 (canonical case, proven) |
| **WS-C4** ✅ **DONE + LIVE-VALIDATED** (code atom: per-org `ORG_USER_MANAGER` machine user minted at SSO onboarding, operator playbook + app path, teardown parity, live ACs 1-4; `.kiro/specs/svc06-user-provisioning/`) | Operator-provisioned SVC-06 user path | "How an external app adds login users" is an explicit scope exclusion in SVC-06 requirements; no supported operator path exists. | **requirements re-scope + spec** (decision first) | WS-M5 |
| **WS-C5** ✅ **DONE + LIVE-VALIDATED** | §5b openbao-agent handler-flush-on-abort | The recreate handler can't fire when the compose reconcile fails before handler flush (play abort), so a stale crash-looping sidecar survives re-runs. | `fix-*` spec | WS-M1 |
| **WS-C6** ✅ **DONE + LIVE-VALIDATED** | openbao-agent dev-TLS default wiring | The consumer workload-deploy path doesn't wire `openbao_primary_tls_skip_verify` from the dev self-signed posture by default, so the sidecar can't verify the OpenBao cert and crash-loops. | `fix-*` spec | WS-M1, WS-M5 |
| **WS-C7** | SVC-06 spec format — **✅ DONE / OBSOLETE** | Premise no longer holds: SVC-06's `.kiro/specs` `requirements.md`/`tasks.md` are now well-formed + WS-M6-compliant and the repo-root requirements doc matches the 15-section template; the v0.14.0 format drift was resolved by later cleanup (likely the AC-3 reopen). Closed without a reformat (no fabricated churn). See F.2. | ~~`docs/chore` spec~~ no action | — |
| **WS-C8** ✅ **DONE + LIVE-VALIDATED** | `consumer_preflight` ZITADEL health probe intermittently flaky | Surfaced during WS-C3 live (§D.2). The `ansible.builtin.uri` probe to `https://auth.<domain>/debug/healthz` failed on several onboarding attempts while a standalone identical probe + 20/20 sustained `curl` returned 200 and the ZITADEL container was stable (restartcount 0). Blocks full onboarding intermittently. | `fix-*` spec | WS-M1 (runtime-wiring/readiness) |
| **WS-C9** ✅ **DONE + LIVE-VALIDATED** | Consumer DB drift: live flow bypassed the ADR-0019 `db/` wrapper (premise corrected 2026-11-05: `dronefleet/db` was never orphaned dead code) | **Fixed:** an opt-in `svc01_consumer_wrapper` (default off) routes the consumer provision + clean-slate through `infra/projects/<slug>/db/` (state `<slug>-db`, vlan_id/host_index from registry/ledger); `_TEMPLATE/db` is the copy source and a missing `<slug>/db` fails closed with the copy command; the shared clean-slate now tolerates a root with no state file (live-caught). SVC-06's backing DB path is unchanged. ADR-0019 amended. Live on throwaway: clean-slate, fresh onboarding to `dronefleet-db.tfstate`, hello-workload deploy and an idempotent re-run all `failed=0`. Spec: `.kiro/specs/fix-consumer-db-wrapper-routing/`. | `fix-*` spec (DONE) | WS-M2 (teardown parity) |
| **WS-C10** ✅ **DONE + LIVE-VALIDATED** (shared read-only `sdn_vnet_realized` role in the svc01/06/07/09/obs preflights; `.kiro/specs/fix-sdn-vnet-realized-preflight/`) | SDN VNet realization can be lost (committed-but-not-live bridge) | Surfaced during WS-C3 live (§D.2). `p100` was in SDN config but not realized as a live host bridge ("bridge 'p100' does not exist" at container start); re-running the project slice's `proxmox_sdn_applier` (`-replace`) realized it. A health-check/guard that the applier actually realized the bridge (not just committed config) would catch this. | `fix-*` spec | WS-M1 |
| **WS-C11** ✅ **DONE + LIVE-VALIDATED** (premise corrected: the scope IS inherited; the defect was mid-run refusal timing; fixed with an up-front `--also-service` pre-check; `.kiro/specs/fix-orchestrator-composed-guard-scope/`) | Orchestrators propagate `AGENT_LIVE_AUTHORIZED` but not the `AGENT_LIVE_AUTHORIZED_SERVICES` scope | Surfaced during WS-C3 live (§D.2). A multi-service orchestrator composes svc01/svc06/svc07 children, but only the global arm (not the per-service scope) is propagated — a narrow scope makes a child's guard consult REFUSE ("service X out of scope"). Leaving the scope unset is the working path; the propagation gap is a hardening item. | `fix-*` spec | WS-M1 |
| **WS-C12** ✅ **DONE + LIVE-VALIDATED** (platform fabric teardown handles zone occupants: fail-closed halt, attached-guest refusal, API removal, slice-state cleanup; plus three live-found teardown fixes; `.kiro/specs/fix-platform-fabric-zone-occupants/`) | Platform fabric-destroy blocked by VNets not in the foundation state | Surfaced during the WS-C2 live run. The platform-foundation `terraform destroy` can't delete the SDN zone `vlanzone` while VNets reference it, but those VNets aren't in the foundation state to be destroyed first: the foundation state holds ONLY `proxmox_sdn_zone_vlan.foundation` — the shared `p20` VNet + subnet are NOT tracked there (despite the decision-log stating the foundation root owns the VLAN-20 VNet), and leftover per-project VNets (e.g. `p103`) also reference the zone. The fabric teardown halts. Downstream of WS-C2 (shared-service discovery, which succeeded); a platform-foundation state/ownership gap. | `fix-*` spec | WS-M2 |
| **WS-C13** ✅ **DONE** (via WS-C60) | Revisit the committed dev self-signed TLS defaults once a CA exists | `consumer_bootstrap` carries a committed `consumer_openbao_tls_self_signed: true` default (control-plane `bao` calls to the Primary). While the only Primary cert is the self-signed mock this is the only working value for external dev projects, and it affects no production traffic, but it conflicts with platform-runtime-patterns P4 (a skip is never a committed default). **When a CA exists (SVC-08/EJBCA or a trusted-CA cert on the Primary):** flip it to the WS-C6 resolution order (explicit flag, then the `OPENBAO_TLS_SELF_SIGNED` cluster env-file key, then verify), and remove that key from dev env files (`.env.throwaway`, `cluster.dev.env`) so the deploy paths verify with no code change. Operator-agreed 2026-11-05 (WS-C6 follow-up). | `fix-*` spec (after a CA exists) | WS-M5 (P4) |
| **WS-C14** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-platform-clean-slate-zone-and-firewall-residue/`) | Cluster firewall rules that survive a full platform clean-slate | The WS-C12 live run left cluster firewall rules behind after a full teardown: the standing isolation DENY rules (by design, not per-VLAN teardown-able) and the opt-in OpenBao unsealer scoping rules (ADR-0008). They reference no live object, but a "full teardown" does not return the cluster to an empty baseline. **DECIDED 2026-11-07 (operator): standing DENY rules are platform-owned BASELINE.** Remaining work (`fix-*` spec, test-first): document them as the expected residue and make the clean-slate verify gate ignore exactly those rules and nothing else; remove the unsealer scoping rules via the existing path when enabled. The original question was: are the standing DENY rules platform-owned baseline (keep, document as the expected residue and make the clean-slate verify gate ignore exactly them) or removable (add an opt-in removal step)? Remove the unsealer scoping rules via the existing `guest_firewall_baseline_teardown` path when they were enabled. Source: `.kiro/specs/fix-platform-fabric-zone-occupants/validation.md` known limitations. | decision, then `fix-*` spec | WS-C12 |
| **WS-M4 Inc.2 sign-off** ✅ **DONE 2026-11-07** | Operator confirmed ADR-0024 Option B (CLI, not MCP) | ADR-0024 is now Accepted. Implementation stays WS-M4 Inc.2b, gated on staleness. | none | - |
| **Live-work prerequisite (note)** | Throwaway cluster rebuild | The throwaway cluster `shrimp` was torn down by the operator on 2026-11-07 and is used only by the agent; rebuild it from scratch before any live acceptance. Any `requires-infra` acceptance (WS-M4 Inc.2b live AC, WS-C13 verification, future atoms) first needs a fresh bring-up through the platform and service bootstrap playbooks under the `AGENT_LIVE_AUTHORIZED` interlock. Not a work item on its own. | n/a | - |
| **WS-C15** (**PARKED, operator 2026-11-07**) | Live re-run check for OIDC app reconcile-on-rerun (v0.14.0 feedback #3) | The offline contract tests prove `redirectUris`/`devMode`/`loginVersion` reconcile + the read-back gate against a scripted transport. **Outstanding live run:** on a throwaway cluster (guard armed), onboard a consumer project through `consumer_bootstrap`, change the manifest `redirect_uris` (and `dev_mode`), re-run, and confirm the live ZITADEL app converged with `changed` on the first re-run and `changed=0` on the second. Closes the indirectly-confirmed `oidcConfig.redirectUris` shape. Spec: `.kiro/specs/fix-oidc-app-reconcile-on-rerun/`. | live acceptance (throwaway) | WS-M3 |
| **WS-C16** ✅ **DONE + LIVE-VALIDATED** (ZITADEL email-provider and org login-policy request shapes confirmed on the pinned `v4.18.0`; fixture `tests/fixtures/live-shapes/zitadel-email-provider-smtp.json` now `live_confirmed: true`; spec `.kiro/specs/dev-mail-catcher/`) | Live conformance of the ZITADEL SMTP + org login-policy request shapes (dev-mail-catcher) | Run on the `shrimp` throwaway cluster with the interlock armed and the guard authorizing. Confirmed live: `GET/POST/PUT/DELETE /admin/v1/email*` + `_activate`, and `GET/POST/PUT/DELETE /management/v1/policies/login` (POST when the org inherits the default, PUT when custom; the full-replace round trip changed no other field). **Defects found and fixed by the live run:** an untagged provider with identical host/sender was judged 'unchanged' so `--replace-existing` never applied the ownership marker (helper bug, test-first fix); responses omit `tls` when false; an absent provider is HTTP 404. | live acceptance (throwaway) | WS-M4 / WS-M3 |
| **WS-C17** ✅ **DONE + LIVE-VALIDATED, with the exceptions listed in WS-C19** (spec `.kiro/specs/dev-mail-catcher/`) | Live acceptance of the shared dev mail catcher | Live on `shrimp` (interlock armed, guard authorized, dependencies stood up only through their own bootstraps): from-scratch `mailcatcher-bootstrap.yml` (first run, exit 0); health gate; independent probes (SMTP `220` banner, UI 401 unauthenticated / 200 with the generated credential, directly and through Traefik, ports bound to the VLAN-20 IP only, pinned image digest running, auth file `0600`, password in no log); `zitadel-smtp.yml` add / idempotent re-run (`changed=0`) / flag-off no-op / role-level remove + idempotent remove; fail-closed with the catcher not answering and with the UI unprotected (both `changed=0`); **a real hosted self-registration whose verification code was read from the catcher and the login completed**; bootstrap re-run (credential reused, not rotated; substantive steps no-op); catcher clean-slate (guest gone, Traefik route fragment removed, Terraform state empty, other guests untouched). | live acceptance (throwaway) | WS-M3 |
| **WS-C18** | Default role for self-registered ZITADEL users | **DONE + LIVE-VALIDATED.** Decision: build it. New optional manifest key `sso.default_role` (a role key, or `false` to remove). `zitadel_provision.py` reconciles one platform-owned Actions v1 action (`platformDefaultRole`) on the Internal Authentication / Post Creation trigger that grants the role, preserving other actions, with a read-back gate; verified live with real hosted registrations (grant `viewer`/`member`, none after removal, idempotent). Org placement docs are WS-C21; the org-id handoff is WS-C27. Spec: `.kiro/specs/fix-zitadel-default-role-on-registration/`. | `feature-*` spec | none |
| **WS-C19** | Live paths of the dev mail catcher NOT run in the WS-C17 session | **DONE except one path** (see `.kiro/specs/dev-mail-catcher/validation.md`): (1) platform clean-slate discovery, (2) the acknowledgement variant (the forward-auth option was removed, WS-C23), (4) the real-provider refusal via the playbook, and (5) the second from-scratch bring-up all ran live on the throwaway cluster; (3) `allow_register` ran live through the `zitadel-org-provision` role, the full consumer orchestrator is **WS-C24**. The session also found and fixed (test-first) two defects (a censored refusal message and a login-policy-only change reported as `changed=0`) and raised **WS-C25**. | live acceptance (throwaway) | WS-M3 |
| **WS-C20** | `platform-bootstrap` gateway phase fails on a clean cluster and leaves a dangling host interface stanza (found live during WS-C17) | **DONE.** `sdn_gateway/tasks/configure.yml` now probes and asserts that `p<vlan>` exists BEFORE writing `sdn-gw-<vlan>.cfg`, so a missing bridge leaves the host unchanged. Test-first (`tests/installer/test_sdn_gateway_no_dangling_stanza.py`, 3 failed before) and verified live on the emptied throwaway cluster (VLAN 20 configured, VLAN 100 stopped with no stanza, re-run `changed=0`). Spec: `.kiro/specs/fix-sdn-gateway-dangling-stanza/`. Related: v0.14.0 feedback #2. | `fix-*` spec | WS-M1 |
| **WS-C26** | Should the gateway phase serve registry-active project VLANs whose network slice is not applied? | **DONE + LIVE-VALIDATED.** Decision: the platform bring-up skips them with a loud warning (`sdn_gateway_skip_unrealized_project_vlans=true`, surfaced in its output); the explicit `sdn-gateway.yml` stays fail-closed (default false); the shared VLAN 20 is never skipped. `platform-bootstrap.yml -e bootstrap_scope=host,api` now completes on a cluster with registry rows and no slices. Spec: `.kiro/specs/fix-platform-gateway-skip-unrealized-vlans/`. | `fix-*` spec | none |
| **WS-C21** | Document the ZITADEL org-placement rule for EXTERNAL project authors | **DONE (docs).** New section "SSO consumption: which org does a new user land in?" in `docs/CONSUMER-JOURNEY.md` (the `urn:zitadel:iam:org:id:<id>` scope, the default-org failure mode, how to get the org id, the `allow_register` and mail prerequisites, and that no role is granted), cross-linked from the `sso.login_policy.allow_register` row of `docs/CONSUMER-MANIFEST-REFERENCE.md`. Spec: `.kiro/specs/docs-consumer-org-placement/`. | docs | none |
| **WS-C27** | Surface the project org id in the onboarding handoff | **DONE + LIVE-VALIDATED.** `zitadel-org-provision` prints one `ZITADEL_ORG_ID=<id>` marker; `consumer_bootstrap` extracts only the digits; the handoff banner shows `SSO org id:` with the exact `urn:zitadel:iam:org:id:<id>` scope. Live (WS-C30): the printed id equals the id read from the ZITADEL admin API, the raw marker is not echoed, no secret in the log. Spec: `.kiro/specs/feature-consumer-handoff-org-id/`. | `feature-*` spec | none |
| **WS-C30** | Live acceptance for WS-C27, WS-C28 and WS-C29 on a throwaway cluster | **DONE for WS-C27 and WS-C28; nothing to run for WS-C29** (specification only; its live acceptance belongs to WS-C31). The operator allowed the stack on `shrimp` to be reset. It was torn down (two orphan guests of a VLAN-103 project and its VNet removed by hand, the rest through the platform clean-slate), then rebuilt from scratch through the service bootstraps (platform, SVC-07, SVC-09, SVC-06, mail catcher: all exit 0). See the validation files of both specs. | live acceptance (throwaway) | none |
| **WS-C33** ✅ **DONE + LIVE-VALIDATED** (same spec) | Platform clean-slate gate misses an untracked leftover SDN zone | Found during the WS-C30 reset: after the platform clean-slate reported an empty baseline (`failed=0`), the SDN zone `vlanzone` still existed (empty, no VNets) because the foundation Terraform state in this checkout did not track it (the stack had been created from another state), and the verification gate checks only bridge/VNet absence. A subsequent foundation `terraform apply` would have collided with it; I deleted the empty zone through the PVE API before the bring-up. **To do:** make the gate (and the zone-occupant step) also report a surviving zone that no state tracks, and either remove it under the same opt-in as occupants or halt naming it; test-first. | `fix-*` spec | none |
| **WS-C34** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-svc01-openbao-write-and-role-idempotency/`) | Deferred-AC ledger triage: SVC-01 live gaps | FR-8 clean-slate was already live-proven (ledger row closed docs-only). FR-6 credential write confirmed live; the live re-run exposed two real defects, fixed test-first: the credential was re-written on every run (KV version 1 to 2) and both DB role tasks were hard-coded `changed`. After the fix the child `postgres.yml` re-run is `changed=0` and the KV version stays put. Note: the ledger's 'Property 7' row belongs to SVC-07 (see WS-C36), not SVC-01. Control-node bookkeeping tasks of the installer (`terraform init`, inventory regeneration, known_hosts block) still report changed by design: see WS-C39. | live acceptance (throwaway) | WS-M3 |
| **WS-C35** | Deferred-AC ledger triage: SVC-01 feature gaps (MariaDB bring-up, PostGIS/TimescaleDB images) | **Product choice, build only when a project needs it.** MariaDB `svc01` role is a skeleton; only `pgvector` is wired. Source: ledger FR-7/AC-7, gap(c). | `feature-*` spec (on demand) | none |
| **WS-C36** ✅ **DONE (first live run + fixes)** (spec `.kiro/specs/fix-svc07-openbao-metrics-prefix-and-live-tier/`) | Deferred-AC ledger triage: SVC-07 contract tier | First live run of the contract tier found a real monitoring defect (OpenBao emitted `vault_*` metrics, so the OpenBaoSealed alerts could never fire; plus a stale-series false-positive) fixed test-first, and test rot (descoped AWS test, token-create precondition) turned into explicit skips. Check-mode idempotency is green in CI (ledger row closed). Live confirmation of the prefix on a from-scratch install rides on WS-C37. Remaining pieces: WS-C40, WS-C41. | live acceptance (throwaway) | WS-M3 |
| **WS-C37** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-obs-grafana-admin-and-alert-delivery/`) | Deferred-AC ledger triage: SVC-17/18 observability ACs | AC-6, AC-7, AC-9 confirmed live; AC-8 confirmed on the guest side (control-node bookkeeping is WS-C39). The runs found and fixed two real defects test-first: the documented one-command install left Grafana on `admin`/`admin` (OpenBao unwired by default, empty password rendered), and Prometheus could never deliver alerts (Alertmanager targeted as `localhost`). Also confirmed live: OpenBao now exposes `openbao_core_unsealed` (WS-C36 fix). Raised WS-C42. | live acceptance (throwaway) | WS-M3 |
| **WS-C38** ✅ **DONE + LIVE-VALIDATED** | Deferred-AC ledger triage: project-consumer-onboarding evidence | Task 10 of `project-consumer-onboarding` run live for the first time as a suite: 11 passed, 1 strict xfail (`--check` re-run, WS-C43). Two stale test assertions fixed (`app_password` field; clean-slate host and workload VMID). `validation.md` and the ledger updated. SVC-06 MFA stays under WS-A2. Findings: WS-C43, WS-C44, WS-C45, plus a WS-C42 data point. | live acceptance (throwaway) | WS-M3 |
| **WS-C39** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-installer-truthful-bookkeeping/`) | Make the installers' control-node bookkeeping tasks report truthfully | `terraform init` is changed only on a download; the inventory generator leaves an identical file untouched (`inventory unchanged`); known_hosts goes through the new shared `known_hosts_refresh` role (touches the file only when the guest's keys changed); the ZITADEL dev helper staging is not a change. Live: second re-runs of SVC-07, SVC-09, SVC-06, mail catcher, SVC-17/18 and SVC-01 are `changed=0`; the consumer changes only the by-design Secret_ID. | `fix-*` spec | WS-C34 |
| **WS-C40** (second run DONE, rest blocked) (spec `.kiro/specs/svc07-live-tier-second-run/`) | Run the rest of SVC-07's live contract tier | Second live run: metrics, both non-destructive auto-unseal tests and the onboard idempotency test now pass (runner gained per-guest docker endpoints, a delegate host and the TLS posture; the seal-failure log assertion and SVC-07 Req 5.6 corrected to OpenBao's real message). The two negative auto-unseal arms leave the unsealer Shamir-sealed, so they are gated behind `OPENBAO_TEST_ALLOW_SEAL` (see WS-C47). **Still blocked:** PBS, Garage (SVC-03, now built: the OpenBao-side S3 checks can be wired next), a runner-reachable Prometheus URL, an OpenBao Traefik route plus VLAN-20 vantage, an audit directory visible to the runner, PyJWT in the venv, a child-token-capable token, a database rotate connection. | live acceptance (throwaway) | WS-M3 |
| **WS-C41** ✅ **DONE + LIVE-VALIDATED** (ADR-0027, spec `.kiro/specs/fix-svc07-rerun-transit-token/`) | SVC-07 re-run needs the Bootstrap_Transit_Token; onboard roles broke without a delegate host on ansible-core 2.21 | Operator decision B. (1) A re-run reads the seal token from the primary's `config.hcl` over SSH (no_log, never persisted), skips the fresh-bootstrap-only drift guard and token capture, and requires a valid existing `OPENBAO_ADMIN_TOKEN` (lookup-self) instead. Live: two re-runs `failed=0` (second changes only WS-C39 bookkeeping); an invalid admin token halts with the clean-slate remedy. (2) `openbao_project_onboard` and `openbao_workload_approle` no longer set connection vars to `omit`; the no-delegate branch reuses the host's own values. Live: the consumer (delegate) path `failed=0`; the standalone fixture now passes the former failure point. | `fix-*` spec + ADR | WS-C36 |
| **WS-C42** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-clean-slate-generated-inventory/`) | Stale generated inventories on the control node feed scrape discovery | Each service clean-slate and the consumer clean-slate now removes its own generated inventory after its empty-baseline gate, through the new shared `generated_inventory_cleanup` role (the shared SVC-01 file only when it lists the torn-down VMID); an offline glob guard covers new clean-slate roles. Found live and fixed: the consumer clean-slate read `OPENBAO_ADMIN_TOKEN` only from the process environment. Live: dronefleet and mail catcher teardowns removed their files; the shared SVC-01 file was kept. | `fix-*` spec | none |
| **WS-C43** ✅ **DONE + LIVE-VALIDATED** | The consumer orchestrator does not run under `--check` | Operator decision B: `--check` is documented as unsupported (dronefleet runbook, Part 3); the 10.4 idempotency test is a plain second run asserting `failed=0` and that only tasks on `SECOND_RUN_ALLOWED_CHANGES` changed (Secret_ID re-mint by design, plus WS-C39 bookkeeping), with an offline guard that every allow-list entry names a real task. Live: PASSED on `shrimp`. | test + docs | WS-C38 |
| **WS-C44** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-consumer-compose-project-vlan-gateway/`) | From-scratch consumer onboarding fails until the platform gateway is re-run | Operator decision: option A. The consumer provision phase now composes the `platform_gateway` role after the network slice exists (host from `consumer_proxmox_host` or `PROXMOX_HOST_LAN_IP`, fail-closed). Live: from-scratch platform rebuild, then one consumer command `failed=0` with `p100` served; re-run unchanged. | `fix-*` spec | WS-C26 |
| **WS-C45** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-consumer-openbao-token-resolution/`) | Consumer flow reads `OPENBAO_ADMIN_TOKEN` only from the process environment | Preflight now resolves it from the environment, else the cluster env file, and fails fast when wired and empty; all consumer uses, the two composed OpenBao roles (new optional input, env default kept) and the ZITADEL child take the resolved value. Live: onboarding with the token only in the env file `failed=0`; an env file without it stops at preflight. | `fix-*` spec | none |
| **WS-C46** ✅ **DONE (mitigated)** (spec `.kiro/specs/fix-test-determinism-hypothesis-deadline/`) | Intermittent single failure in `tests/installer` | Never reproduced (further local runs and all CI runs green). Mitigated at the most likely cause: every test root loads a Hypothesis profile with no per-example deadline, and the root `pytest.ini` adds `-rf` so any future failure is named. A guard test pins both. If it recurs, raise a new item with the named test. | `fix-*` | none |
| **WS-C47** ✅ **DECIDED 2026-11-08: keep as is** (operator) | The throwaway bring-up keeps no Shamir keys, so any unsealer restart needs a full SVC-07 clean-slate | No key persistence is added. An unsealer restart or host reboot on a throwaway cluster is recovered by the platform clean-slate plus the services' own bootstraps; the destructive auto-unseal test arms stay opt-in behind `OPENBAO_TEST_ALLOW_SEAL` (WS-C40). Revisit only if this recovery becomes a routine cost. | decision | WS-C40 |
| **WS-C48** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-consumer-db-vmid-default/`) | Consumer DB VMID default is the example project's `1101` | The default is now `0` (computed `vlan_id*1000+host_index`); a standing DB keeps the VMID recorded in its own generated inventory (file-mediated, never Terraform state); an explicit `-e consumer_svc01_vmid` wins; the clean-slate refuses to guess. Live: dronefleet from scratch got `100000`, a re-run kept it, a second project got `101000`, both torn down cleanly. | `fix-*` spec | none |
| **WS-C49** ✅ **DONE** | Consumer-facing documentation current for external projects | README gains "Use the platform from your project"; `docs/CONSUMER-JOURNEY.md` gains Step 0 (who does what, required running services, cluster env keys, VMIDs), the gateway/token/`--check`/re-run facts of WS-C43..C45, the exact OIDC secret path and issuer, and an inline teardown command; the manifest reference gains `sso.roles`, `sso.project_role_assertion` and the `expose` block; dronefleet runbook commands use the real orchestrator variable `consumer_svc01_vmid` (the documented `svc01_vmid` was silently ignored); broken `TESTING.md` links and stale statuses fixed; CONTRIBUTING explains how to get involved. | docs | none |
| **WS-C50** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/consumer-object-storage/`) | Object storage in the consumer manifest | `object_storage.buckets` in the manifest; Phase 3 composes `garage-bucket-provision.yml`; handoff prints `secret/proj-<slug>/garage`; the consumer clean-slate composes the new `garage_bp_action=teardown` (empties and deletes buckets, deletes key and OpenBao entry, gates on no residue). Live: dronefleet onboarded from scratch with a bucket, signed S3 round trips on VLAN 20 and through the Traefik route, second run changed only the allow-listed Secret_ID tasks, clean-slate removed a bucket holding two objects, a second teardown was a no-op. Also fixed: hyphenated purposes let two projects map to one bucket name. | feature spec | SVC-03 |
| **WS-C51** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-03-website-hosting/`) | SVC-03 static website hosting (FR-5) | Garage `[s3_web]` on port 3902; per-bucket opt-in (`website_purposes` / manifest `object_storage.website`) enables website access and renders a Traefik route `Host(<bucket>.web.<domain>)`; other buckets reconciled to private; teardown removes the routes. Live: page served anonymously through Traefik, non-website bucket 404, off/on reconcile, re-run `changed=0`, teardown clean. | feature | SVC-03 |
| **WS-C52** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-03-alerts/`) | SVC-03 alerts | Six Garage rules in the SVC-17/18 baseline (node down, quorum lost, under-replicated, data volume > 80 %, resync errors, S3 5xx rate), proven with `promtool test rules` and live (a stopped node fired `GarageStorageNodeDown` in Prometheus and Alertmanager, restart resolved it). Log shipping moved to WS-C58. | feature | SVC-03, SVC-17/18 |
| **WS-C58** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-17-18-service-guest-exporters/`) | Service-guest metrics and logs | SVC-17/18 runs node_exporter, cAdvisor (:9338) and Promtail on every generated-inventory guest and generates per-guest node/cAdvisor targets with registry-derived `project` labels; stale targets removed. Proven by a full from-scratch rebuild (38/38 targets up, logs from every guest). Unblocks WS-C61's expiry/CRL alert (textfile collector still to add). | feature | SVC-17/18 |
| **WS-C53** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-03-admin-api-firewall/`) | Restrict the SVC-03 admin API (3903) | In-guest firewall on every Garage node: a `DOCKER-USER` jump (original destination port 3903, original direction) to a `GARAGE-ADMIN` chain that admits only `garage_admin_allowed_sources` (default the Prometheus guest), applied by a systemd unit ordered after Docker; the installer verifies the rule; the health check runs on the guests. Live: the control node and the Traefik guest time out, Prometheus and the node itself get 200, targets stay up, the rules survive a Docker restart, a re-run is `changed=0`, a clean-slate and from-scratch bring-up re-applies it. | feature | ADR-0008 |
| **WS-C54** ✅ **DONE** (operator chose Scality CloudServer, 2026-11-15) | Replace the SVC-03 MinIO fallback and validate it once | MinIO CE is archived upstream (no images since October 2025, repo archived April 2026, Docker Hub images deleted September 2026). Fallback is now Scality CloudServer (Paris, Apache-2.0, single-node, no replication of its own). `tests/installer/test_s3_fallback_cloudserver.py` (requires-infra) ran the platform's S3 client contract against the pinned `ghcr.io/scality/cloudserver:9.5.5` image: create bucket, round trip, list, purge, delete, and cross-account refusal all passed. Catalog, SVC-03 requirements, `tech.md`, runbook and decision log updated. | decision + live check | SVC-03 |
| **WS-C55** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-03-key-rotation/`) | SVC-03 project key rotation | `garage_bp_action=rotate`: new key with the same grants, proven, stored as a new OpenBao version, old key deleted, canonical name kept; `garage_bp_rotate_min_age_days` for a scheduled 180-day cadence; an interrupted rotation is finished by the next run. Live: old key 403, new key reads a pre-rotation object, a 180-day run on a fresh key is a no-op, a simulated interruption was resumed. | feature | SVC-03 |
| **WS-C56** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-08-internal-pki/`) | SVC-08 internal PKI on the OpenBao PKI engine | Root + intermediate on SVC-07, ACME for Traefik routes (DNS-01 via SVC-10), installer-issued certs for endpoints not behind Traefik, root distribution, Mocks 1/3/B replaced by the real path, verification on (WS-C59, WS-C60, closes WS-C13). AC-1..AC-7 live, including a full platform teardown and from-scratch rebuild (2026-11-18). | feature spec | WS-C57, WS-C13 |
| **WS-C59** 🟡 **PARTLY DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-08-internal-pki/` tasks 8-9) | SVC-08 consumers: Traefik ACME and verification on | DONE, live 2026-11-17: OpenBao listener certs from the CA (SIGHUP reload); primary transit seal client `tls_ca_cert` after a live chain probe of the unsealer; Traefik `internal` resolver (DNS-01 via PowerDNS, `defaultGeneratedCert` wildcard, EC256, PowerDNS key read from OpenBao by the installer into a 0600 env file, decision-log row 'installer-time secret delivery'); Grafana `obs_tls_real_ca_available` from the gate; `PlatformCertExpiringSoon`/`PlatformCertExpired` on Traefik's cert metric; SVC-07 clean-slate removes the dead CA artifacts. REMAINING (WS-C60): openbao-agent sidecars verify with the root + WS-C13 default flip; `common` applies `pki_trust` to new guests; scheduled renewal + expiry alert for the push-issued OpenBao listener certs (90 days; renewed only by a `svc-08-bootstrap.yml` re-run today); CRL-staleness alert (needs WS-C58 node_exporter on service guests). | feature | WS-C56, WS-C13 |
| **WS-C60** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-08-internal-pki/` task 11) | SVC-08 remaining consumers | Primary listener cert with `127.0.0.1` SAN + `BAO_CACERT` in the primary container (loopback CLI verifies); openbao-agent `ca_cert` from the published root (Agent authenticates and renders with verification); WS-C13 closed (consumer default = WS-C6 resolution order; `OPENBAO_TLS_SELF_SIGNED` removed from the throwaway env file); `pki_trust` in `common`; SVC-08 trust over every generated inventory; consumer STEP 4 now passes the platform domain to ZITADEL org provisioning (latent defect). | feature | WS-C59 |
| **WS-C61** (backlog; operator decisions 2026-11-17) | SVC-08 listener-cert renewal and expiry alert | (1) **Renewal** of the push-issued OpenBao listener certs (90 days, reissued at 30 days left only when `svc-08-bootstrap.yml` runs): backlog, not urgent — the throwaway cluster is rebuilt several times a week, so certs never age; choose the mechanism (control-node timer vs. on-guest Agent PKI template) when a long-lived cluster exists. (2) **Expiry alert** for those certs (and CRL staleness): important but not blocking for a dev environment; does not have to wait for WS-C58 (any metric source that reaches Prometheus will do). (3) Stale `_acme-challenge` TXT values from interrupted challenges: **leave them** (operator decision; harmless, SVC-10 keeps not managing TXT). | feature | WS-C60 |
| **WS-C62** (backlog, low priority; operator decision 2026-11-17) | Token values on `docker exec` argv | 81 grandfathered `-e BAO_TOKEN=<value>` call sites in 18 files (`openbao_init_unseal` root/unsealer tokens, `openbao_project_onboard`, `openbao_workload_approle`, consumer, ZITADEL, observability, `postgres-server`, `svc01_handoff`, `svc06_clean_slate`, `zitadel_smtp_catcher`) put the token on the guest's process argv during the call. Accepted today (single-tenant, root-only guests; no process auditing). Convert to the name-only form (`-e BAO_TOKEN` + task `environment:`) if process auditing is enabled, a guest becomes shared, or for consistency; needs a from-scratch SVC-07 live run for the init path. New code is already held to the name-only form by `tests/installer/test_no_token_value_on_argv.py` (exact per-file counts; lower them as sites are converted). Misleading 'never on argv' comments on those sites were corrected. | fix-* spec | - |
| **WS-C63** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-control-route-and-svc01-inventory/`) | Shared SVC-01 generated inventory is last-writer-wins | `svc-01-sql-database.generated.yml` holds whichever SVC-01 instance ran last (ZITADEL's backing DB or a project DB), so file-mediated consumers (SVC-10 records, SVC-17/18 file_sd, SVC-08 trust) lose the other instance. Seen in the 2026-11-18 rebuild: the `svc01-20-01` A record was deleted after the dronefleet DB install. Give each SVC-01 instance its own inventory file. | fix-* spec | - |
| **WS-C64** ✅ **(a)+(b) DONE + LIVE-VALIDATED** (spec `.kiro/specs/fix-control-route-and-svc01-inventory/`; option (c), a configurable base range, stays open) | Control-node VPN overlapping the platform address range | The platform uses `10.0.<vlan>.0/24` for every VLAN (NET-00), reached from a developer machine through a static route `10.0.0.0/16` (or per-VLAN /24s) via the Proxmox host (ADR-0004). A developer VPN that routes an overlapping range (seen: `10.0.0.0/16` at a lower metric) silently takes over: every guest becomes unreachable mid-run and installers fail with SSH timeouts (2026-11-19 rebuild, SVC-03 step). Options: (a) a preflight check in the installers that `ip route get 10.0.20.10` leaves via the Proxmox host and fails fast naming the conflicting interface; (b) document per-VLAN /24 routes, which win over a /16 by prefix length; (c) make the platform base range configurable (a NET-00 change, ADR). Start with (a)+(b). | decision + fix-* spec | ADR-0004, NET-00 |
| **WS-C65** (backlog; operator decision 2026-11-25) | SVC-06 Login V2 (`zitadel-login` container) | Requirements drafted at `.kiro/specs/svc06-login-v2/requirements.md` (no design or tasks yet): add the separate `ghcr.io/zitadel/zitadel-login` container to the SVC-06 Compose topology, route `/ui/v2/login` through Traefik, wire the login client's service-user credential, and enable the instance Login V2 feature. Not urgent: interactive login works on Login V1 (constitution default, live-validated in `svc-06-identity-provider/validation.md` §7). Next step: design.md. | feature spec | SVC-06, SVC-09 |
| **WS-C66** (backlog; operator decision 2026-11-25) | SVC-23 model serving (vLLM) | Design drafted at `.kiro/specs/svc-23-vllm-model-serving/design.md`; requirements.md and tasks.md are missing (spec-lifecycle order: requirements first). Catalogue requirements: `requirements/SVC-23-model-serving-requirements.md`. Needs a GPU-capable guest; DCGM metrics are deferred today (Mock C). Next step: requirements.md reconciled with the existing design. | feature spec | SVC-07, SVC-09, SVC-17/18 |
| **WS-C67** (backlog, optional; operator decision 2026-11-25) | Golden HCL-vs-Python cross-check for the host-index collision guard | `fix-vlan-host-index-collision` task 13 (optional, deferred): a test that diffs the `proxmox-compute` `peer_host_indices` precondition verdict against `assert_unique_host_indices` over golden index sets. The seeded-collision `terraform plan` rejection already covers the behaviour; this guards against drift between the two implementations. | test | - |
| **WS-C68** (backlog; operator decision 2026-11-25) | Verify ZITADEL TLS on the onboarding provisioning path | `zop_base_url_tls_skip_verify` (role `zitadel-org-provision`) still defaults to `true`, so project onboarding calls ZITADEL through Traefik with `--insecure` even with SVC-08 installed, and `consumer_bootstrap` does not forward the variable to its child run. Default it to verify when the platform root is published (as the other TLS mocks do), forward it, and pass the platform CA to the helper; needs a live onboarding run. | fix-* spec | SVC-08, SVC-06 |
| **WS-C69** (backlog; found 2026-11-25) | SVC-06 clean-slate reads the OpenBao admin token from the process environment only | `svc06_clean_slate` ("Clear the stale ZITADEL provision_token field") uses `lookup('env', 'OPENBAO_ADMIN_TOKEN')` with no fallback to the cluster env file, unlike the SVC-06 bootstrap since WS-C45. Run without the variable exported, the clean-slate destroys both guests and then fails at the token clear (seen live; a second run with the token exported completed). Resolve the token like the other installers, and clear the field before destroying the guests or make the partial state explicit. | fix-* spec | SVC-06 |
| **WS-C70** (backlog; found 2026-11-25) | Local false failure of the accessible-root-readme scope guard | `infra/tests/test_accessible_root_readme_links.py::test_git_diff_scope_guard` diffs `main...HEAD` plus working-tree and untracked changes against that feature's documentation-only allowlist, so locally it fails on every branch with commits and on any dirty tree; `test_offline_plan_preservation.py::TestPreExistingSuiteBaseline::test_no_pre_existing_test_fails` inherits the failure. CI skips it (no local `main` ref), which hides the problem. Scope the guard to its own feature branch or retire it. | test fix | - |
| **WS-C71** (backlog; found 2026-11-25) | SVC-17/18 first-run Phase-3 failure, cause hidden by `no_log` | In the 2026-11-25 from-scratch rebuild, `svc-17-18-bootstrap.yml` failed in Phase 3 at the `observability.yml` child run (`no_log`), about 4 s after the Prometheus, Grafana and Loki containers started; the re-run converged (`changed=1`, then `failed=0` through the rest of the order). The 2026-11-24 rebuild passed first time. The cause is hidden because the child run is `no_log` and SVC-17/18 has no debug capture like SVC-07's `svc07_debug_instrumentation`. Add an equivalent opt-in (captures a failing child too), then find and fix the readiness gap (compare `fix-obs-bootstrap-first-run-transient`). | fix-* spec | SVC-17/18 |
| **Closed: `fix-svc07-unsealer-raft-leadership-readiness`** (operator decision 2026-11-25) | Bug #10 investigation record | Parked 2026-09-24 with its leadership-timing hypothesis falsified by live evidence; the real cause (a chown-driven Compose recreate) was fixed by `fix-svc07-unsealer-volume-ownership-recreate`. Kept as an investigation record only; its open checkpoint (task 6) will not be done. | - | - |
| **WS-C57** ✅ **DONE + LIVE-VALIDATED** (spec `.kiro/specs/svc-10-internal-dns/`) | SVC-10 internal DNS (PowerDNS), zone = `PLATFORM_DOMAIN` in the cluster env file | One LXC `svc10-20-01` (`10.0.20.25`): pdns-auth 5.1.5 + pdns-recursor 5.4.9; `*.<domain>` and `*.web.<domain>` to Traefik, one A record per guest synced from the generated inventories (stale ones deleted, recursor cache wiped); guests keep their resolver; API key in OpenBao `secret/proj-shared-dns/pdns`, API/metrics firewalled to Traefik, Prometheus and the admin CIDR; two alerts; the installer prints the split-DNS commands. `PLATFORM_DOMAIN` is now the default of every installer's platform-domain variable (`platform_domain` role). Live: bring-up, `changed=0` re-run, stale-record removal, `apt` with DNS down, restart recovery, clean-slate, Prometheus targets up, full platform rebuild on the new domain. Consumer containers using the recursor is decided with WS-C56. | feature spec | ADR-0028 |
| **WS-C22** | Re-check Mailpit against CVE-2026-21859 (SSRF in `/proxy`) and set an image-bump routine | **DONE.** The GitHub advisory API gives CVE-2026-21859 fixed in `1.28.1`; all 15 published Mailpit advisories have a first patched version at or below `1.30.6`, so the pinned `v1.33.0` is not affected by any. Recorded the table (`tests/fixtures/advisories/mailpit-advisories.json`), added a guard that fails if the pin falls below the highest fix (`tests/installer/test_mailcatcher_image_advisories.py`, with a mutation check), corrected the stale text, and added the bump routine to the runbook. Spec: `.kiro/specs/fix-mailpit-advisory-check/`. | `fix-*` spec | none |
| **WS-C28** | Bump the Mailpit pin to `v1.33.1` or later after it has soaked | **DONE + LIVE-VALIDATED.** Advisory query re-run (same 15, none new); tag and digest bumped together (digest confirmed two ways). Live (WS-C30): fresh install reports `v1.33.1`, runs the pinned digest, SMTP banner OK, UI 401/401/200, listeners only on the VLAN-20 IP. Caveat: only a few days of field time. Spec: `.kiro/specs/chore-mailpit-v1-33-1/`. | chore + live acceptance | none |
| **WS-C23** | Real proxy-side forward-auth for Route_Fragments (`traefik_route` middleware support) | **DONE + LIVE-VALIDATED (capability).** `traefik_route` gains optional, default-inert `route_forward_auth_address` / `route_forward_auth_response_headers`; unset renders byte-identically for all five consumers (golden-hash guard); malformed values are refused; verified live: 401 without credentials, 200 with, 500 when the auth server is down, idempotent. ADR-0018 addendum; spec `.kiro/specs/feature-traefik-route-forward-auth/`. **No service adopts it yet** - see WS-C29. | `feature-*` spec | none |
| **WS-C29** | Adopt forward-auth for a service route (needs a real authorization endpoint) | **BACKLOG (parked, unscheduled). SPEC DONE; implementation split to WS-C31.** Selected oauth2-proxy in front of ZITADEL (ADR-0026, **Proposed**, operator sign-off requested) and specified a shared non-catalog guest `authproxy` (VLAN 20, index 12, `10.0.20.22`, VMID 20012) in `.kiro/specs/svc-forward-auth-proxy/`. Facts verified from primary sources; binding constraints recorded: min version `7.15.2` (CVE-2026-34457, a critical health-check User-Agent auth bypass), never `--ping-user-agent`, `--reverse-proxy` only with `--trusted-proxy-ip`, secrets via the OpenBao Agent, an explicit authorization restriction. No code was written and nothing adopts forward-auth yet. | spec | none |
| **WS-C31** | Implement the shared forward-auth proxy service (oauth2-proxy) | **BACKLOG (parked, unscheduled).** Entry criteria: ADR-0026 (now Accepted, 2026-11-07) and a free throwaway cluster, so only scheduling remains. Per `.kiro/specs/svc-forward-auth-proxy/` tasks 5 to 9: Terraform root and ledger row, six roles and the 4-phase installer with clean-slate and platform catalog row, ZITADEL OIDC app registration, Agent-rendered secrets, advisory fixture and pin guard, offline contract tests, runbook and README row, then live acceptance and one pilot route. Sign-off done (ADR-0026 Accepted); the authorization restriction must be proven live before any route adopts it. | new spec implementation | WS-M3 |
| **WS-C32** | Browser SSO redirect UX for forward-auth routes (`traefik_route` extension) | **BACKLOG (parked, unscheduled); starts only after WS-C31 is merged.** A forwardAuth middleware alone returns the auth server's 401 to a browser. Browser SSO also needs the proxy's `/oauth2/` paths routed on the protected host and a 401 to sign-in redirect (Traefik `errors` middleware). `traefik_route` renders neither. **To do:** extend it additively and default-inert (byte-identical when unset, golden-hash guard, validated values), then live-verify the redirect. Depends on WS-C31. | `feature-*` spec | none |
| **WS-C24** | Run the FULL consumer orchestrator with `sso.login_policy.allow_register` and decide whether an SSO-only project needs a network slice | **DONE + LIVE-VALIDATED.** Decision: a manifest needs a network slice only when it declares a guest (`database` or an enabled `workload`); an SSO-only or secrets-only manifest skips the fail-closed VNet assert and the Phase-2 auto-create (the Registry slug is still resolved). The full `project-consumer-bootstrap.yml` ran live for an SSO-only manifest: `allow_register` false then true then `changed=0`, no VNet created. Spec: `.kiro/specs/fix-consumer-sso-only-no-network-slice/`. | `fix-*` spec | none |
| **WS-C25** | Platform clean-slate: try a non-reboot SDN apply before escalating to a host reboot | **DONE + LIVE-VALIDATED.** `platform_clean_slate` now applies the cluster SDN config (`pvesh set /cluster/sdn`) when the bridge survives destroy, re-probes with a bounded poll, and only then falls back to the reboot opt-in or the halt; `platform_clean_slate_sdn_apply=false` skips it. A live clean-slate completed with no manual step and no reboot. Spec: `.kiro/specs/fix-platform-clean-slate-sdn-apply/`; ADR-0011 addendum. | `fix-*` spec | none |

**Sequencing note for WS-C1/C2/C3:** these are the live-teardown blockers that
stopped the cluster teardown. WS-C2/C3 are the natural pilot for WS-M2 — fix them
*and* land the contract test that would have caught them. The cluster currently has
all service guests destroyed and only the SDN `vlanzone` surviving (operator
tearing down manually), so these are not urgent-live but should land before the
next from-scratch cycle.

### D.1 — Read-only diagnosis of WS-C1/C2/C3 (done before speccing)

Before converting the C-items to `fix-*` specs, each recorded root cause was
checked against the committed v0.14.0 code (read-only; `git log`/blame + direct
file reads, no cluster). Outcome:

- **WS-C1 — REFUTED / reclassified.** The recorded "insecure missing from the
  destroy env" is not true of the committed code. `platform_clean_slate_tf_env`
  (`ansible/roles/platform_clean_slate/defaults/main.yml`) derives
  `TF_VAR_proxmox_insecure` from `platform_clean_slate_proxmox_validate_certs`
  (default `false` → insecure `true`) and is attached to both the `terraform
  init` and `terraform destroy` subprocesses in `tasks/main.yml`. It mirrors the
  working apply path `platform_api_tf_env` exactly (the comment says so), and both
  follow the deliberate platform-wide "one `*_validate_certs` toggle governs both
  preflight and the provider" pattern that `cluster.env.example` documents and
  that `fix-cluster-env-endpoint-path` (TLS half) established for SVC-07.
  `git log` confirms the mirrored env was present in the *original*
  platform-clean-slate commit (a104572), not added by a later fix. The
  platform-clean-slate `validation.md`/`design.md` never list a TLS failure mode.
  **Conclusion:** the live `x509` was a transient from before the TLS-half fix
  propagated (or an operator `validate_certs=true`/stale-override run), not a
  standing defect. WS-C1 becomes a **live re-verify that rides along the next
  teardown** — confirm the SDN-fabric destroy completes clean with the committed
  defaults. **This is itself a specimen of the audit's stale-doc defect class**
  (recorded root cause ≠ committed code) — had we gone straight to a `fix-*`, the
  bug-condition test could not have failed.
  - *Latent, real, not urgent:* `cluster_env.py`'s `SCHEMA` has no
    `PROXMOX_INSECURE` row, so insecure is settable only via the `-e
    ...validate_certs=true` play var, never from `cluster.env`. This is a
    documented, deliberate choice today — a small enhancement spec if cluster.env
    should ever own it, but **not** WS-C1.
- **WS-C2 — CONFIRMED, root cause refined.** Discovery is catalog-driven
  (`platform_clean_slate_services`, one svc07 row); svc06/svc09 are undiscoverable
  because they have **no catalog row**, though their `svc06_clean_slate` /
  `svc09_clean_slate` roles exist. Real teardown gap. Minimal fix = add the
  missing rows; better fix = derive the catalog (feeds the WS-M2 "every `svc-NN`
  with a bootstrap has a registered clean-slate + discovery entry" guard, so a
  future service can't be silently missed).
- **WS-C3 — CONFIRMED, sharply root-caused.** `consumer_clean_slate` tears down
  the DB (composed, ADR-0011) + OpenBao/ZITADEL objects but never destroys the
  Terraform-provisioned workload LXC. Fix = compose a workload `terraform destroy`
  / clean-slate into `consumer_clean_slate`, mirroring the DB compose pattern, and
  extend its empty-baseline report to include the workload guest.

**Net:** WS-C2 and WS-C3 are real and are the WS-M2 pilots; **WS-C3 is the best
first atom** (self-contained, one role, a real provisioned guest to prove the
teardown against). WS-C1 is downgraded to a ride-along re-verify.

### D.2 — WS-C3 fixed + live-validated; four new carry-overs surfaced

WS-C3 is **implemented and live-validated** (spec
`.kiro/specs/fix-consumer-clean-slate-workload-guest-destroy/`, branch
`fix/consumer/clean-slate-workload-guest-destroy`). The consumer clean-slate now
destroys the workload guest via the owned `terraform_clean_slate` mechanism;
proven on throwaway `shrimp` (workload `100001` provisioned → torn down → `pct
list` clean + empty `dronefleet-workload` state; idempotent re-run `changed=0`).
A second, intended change landed too: the role became a destructive-live
orchestrator, so the mandatory throwaway-guard consult was added (the
`test_agent_live_wiring_coverage.py` meta-guard caught its absence — a good
example of the offline gate working).

The **full sanctioned onboarding** (operator's chosen path) stood up the real
dependency chain via each service's own installer — platform prereqs, **SVC-07
OpenBao** (from scratch, `failed=0` — a bonus live-confirmation of the SVC-07
bring-up), **SVC-06 ZITADEL**, **SVC-09 Traefik** + the ADR-0018 ZITADEL
cross-guest route. That exercise surfaced **four carry-overs unrelated to WS-C3**,
now rows **WS-C8–WS-C11**:

- **WS-C8** — ✅ DONE (bounded `until`/`retries`/`delay` poll, P3; `.kiro/specs/fix-consumer-preflight-zitadel-probe-retry/`) — `consumer_preflight` ZITADEL probe intermittently flaky (standalone
  probe + sustained curl 20/20 200, but the in-onboarding `uri` probe failed on
  several attempts).
- **WS-C9** — pre-existing stale TF state/lock drift in
  `infra/projects/dronefleet/db` (Oct 29) + orphaned guest 1101.
- **WS-C10** — ✅ DONE (see Part D row) — SDN VNet `p100` committed but not realized as a live bridge until
  the project applier was re-run.
- **WS-C11** — ✅ DONE (see Part D row) — orchestrators don't propagate the `AGENT_LIVE_AUTHORIZED_SERVICES`
  scope to composed children.

Because of WS-C8/WS-C9, the single-orchestrator-run full onboarding (DB + SSO +
workload together) was not completed end-to-end; the WS-C3 acceptance used a
focused workload-guest provision + the real consumer clean-slate (operator-agreed).
The teardown path exercised is byte-for-byte the WS-C3 change.

---

## Part E — Proposed sequencing (for discussion, not fixed)

1. **WS-A1 deferred-AC audit** (analysis, no code) — produces the ledger that
   scopes everything else and validates WS-M3. **(started)**
2. **WS-C1/C2/C3** teardown blockers — concrete, root-caused; pilot for…
3. **WS-M2** teardown-parity contract + **WS-M1** runtime-wiring contract tests —
   the two highest-leverage prevention streams, seeded by the C-items.
4. **WS-M3** ✅ **DONE** live-acceptance lifecycle gate — turned the audit's lesson
   into a rule + an offline pytest drift-guard.
5. **WS-M4** MCP / live-shape capture — larger, boundary-touching; decide scope
   with operator.
6. **WS-M5/M6, WS-C4/C5/C6/C7, WS-A2** — fold in as capacity allows.

---

## Open questions to resolve together before speccing

- **Audit output location:** a committed `docs/audit/` report, per-service
  validation.md addenda, or spec dirs? (affects WS-A1 — interim default below)
- **MCP scope (WS-M4):** a read-only Proxmox/ZITADEL/OpenBao MCP vs. committed
  recorded fixtures only?
- **WS-C4 user-path:** operator-provisioned (console/API Ansible task) vs. IdP
  brokering vs. keep as documented exclusion?
- **Live re-acceptance appetite:** for SVC-01/06/07 gaps the audit finds, re-run
  live now (needs the throwaway cluster rebuilt) or batch them?
- **Pipeline note:** the CI offline-gate pipeline already exists; no new trip-wire
  action is implied. Listed only so the audit doesn't re-flag it.

---

## WS-A1 working area (audit in progress)

Interim decision (revisit under "Open questions"): the audit ledger is being
written to **`docs/audit/deferred-ac-ledger.md`** as a committed report (no
behavior change). Each real gap gets one backlog row there; code-needing gaps then
spin out their own `fix-*`/validation specs.

Scope of services audited (implemented services with a `validation.md`): SVC-01,
SVC-06, SVC-07, SVC-09, SVC-17/18, plus the cross-guest-ingress and installer
sub-specs and the platform/consumer orchestration specs. Per-AC findings are filled
in as each validation.md is read.

---

## Part F — WS-M*/WS-C* spec-conversion plan

> **What this part is.** The concrete plan for turning each methodology (WS-M*)
> and carry-over (WS-C*) work stream into a spec under `.kiro/specs/<name>/` (or a
> decision-log entry where it is a decision, not code). Confirmed read-only:
> **no WS-M*/WS-C* spec dir exists yet** (grep of `.kiro/specs/**`), so all names
> below are free to create. This part adds no code and no steering yet — it is the
> agreed conversion map we execute from, one branch per row, each through the
> normal `spec-lifecycle.md` flow.
>
> **Audit cross-check that sharpened priorities.** A repo-wide grep confirmed the
> accept-with-deferral pattern is pervasive, not anecdotal: nearly every recent
> `fix-*` spec states its live-acceptance was "deliberately NOT executed in this
> spec" and left to a "follow-up live-acceptance path" (seen in
> `fix-svc01-openbao-credential-write-…`, `fix-svc07-external-copy-removal-…`,
> `fix-svc07-bootstrap-failure-diagnostics`, `fix-svc07-clean-slate-proxmox-host-default`,
> `fix-svc06-zitadel-compose-ingress-version-gate-decoupling`, and others). That
> is exactly the gap WS-M3 closes, so WS-M3 is raised in priority (now item 4,
> immediately after the teardown pilot proves WS-M1/M2).

### F.1 Spec-conversion map

| WS-ID | Proposed spec dir (or artifact) | Spec type | Depends on | First consumer / pilot |
|---|---|---|---|---|
| **WS-C1** | ~~`fix-platform-clean-slate-fabric-destroy-tls`~~ **reclassified (§D.1)** — no spec; a live re-verify ride-along | live re-verify (not a fix-\*) | next teardown | — |
| **WS-C2** | `.kiro/specs/fix-platform-clean-slate-service-discovery/` | `fix-*` (code) **— DONE, live-validated** | — | — (WS-M2 pilot, proven) |
| **WS-C3** | `.kiro/specs/fix-consumer-clean-slate-workload-guest-destroy/` | `fix-*` (code) | — | **first atom** (WS-M2 pilot) |
| **WS-M1** ✅ **DONE** | `.kiro/specs/runtime-wiring-contract-tests/` + `testing-strategy.md` edit + `tests/installer/test_runtime_wiring_contract.py` (offline drift-guard, 3 checks) | steering + offline-test feature | WS-C1/C3 (as motivating cases) | WS-C1 (TF-env/insecure, now locked in), WS-C5, WS-C6 |
| **WS-M2** | `.kiro/specs/teardown-parity-contract/` + `service-installer.md` §4.1 edit | steering + offline cross-service guard **— DONE** | WS-C2 (branch base) | WS-C2/C3 pilots; closed the svc17 catalog gap |
| **WS-M3** ✅ **DONE** | `.kiro/specs/live-acceptance-gate/` + `spec-lifecycle.md`/`testing-strategy.md` edit + `tests/installer/test_validation_completeness.py` (offline pytest drift-guard, **not** hook/skill — merge gate is CI-pytest; `.kiro/hooks` is greenfield) | steering + offline pytest guard | WS-A1 ledger (done) | the 5 stale-doc rows G16–G20; every future spec |
| **WS-M4 Inc.1** ✅ **DONE** | `.kiro/specs/live-shape-fixtures/` + `tests/fixtures/live-shapes/` + `tests/installer/test_live_shape_fixtures.py` + `testing-strategy.md` edit | offline-test feature (fixtures-only, **no ADR** — no new tool boundary) | WS-A1 (bug class) | the KV-v2 + loginVersion bug classes |
| **WS-M4 Inc.2** ✅ **ADR + spec DONE** (ADR-0024 Accepted 2026-11-07: read-only opt-in capture CLI, not a resident MCP) | `.kiro/decisions/0024-design-time-live-shape-capture.md` + `.kiro/specs/live-shape-capture/` | ADR + spec; boundary-touching (new tool + credentials) | operator confirms CLI-over-MCP | refreshes Inc.1 fixtures |
| **WS-M4 Inc.2b** | `scripts/installer/live_shape_capture.py` + offline tests + live read-only acceptance | implementation of ADR-0024 | starts when a fixture is stale or a new family is needed; needs a rebuilt throwaway cluster for the live AC | Inc.2 |
| **WS-M5** ✅ **DONE** | `.kiro/specs/platform-runtime-patterns/` + `.kiro/steering/platform-runtime-patterns.md` + `cross-cutting-categories.md` reference | steering (pattern catalogue) | WS-M1 (wiring pattern), ADR-0018 (ingress pattern) | WS-C4/C6 designs |
| **WS-M6 (enforcing half)** ✅ **DONE** | `.kiro/specs/spec-completeness-guard/` + `tests/installer/test_spec_completeness.py` + `testing-strategy.md` edit | offline pytest guard | WS-M3 (shares the completeness-guard machinery) | all future specs |
| **WS-M6 (skill + hook)** ✅ **DONE** | `.kiro/skills/bugfix-workflow/` (skill) + `.kiro/hooks/` spec-completeness hook | skill + hook (author-time, NOT CI) | the enforcing guard above | authoring ergonomics |
| **WS-C4 (ADR + re-scope)** ✅ **DONE** | `.kiro/decisions/0023-svc06-user-provisioning-path.md` + SVC-06 requirements re-scope + `.kiro/specs/svc06-user-provisioning/` | ADR (scope change + grant model) + requirements re-scope + spec skeleton (offline) | — (decided) | WS-M5; extends ADR-0016 |
| **WS-C4 (code)** ✅ **DONE** | `.kiro/specs/svc06-user-provisioning/` (impl: onboarding mint + operator task + external-app path + teardown + live acceptance) | `fix-*`/feature code; `requires-infra` live | ADR-0023 (above) | needs throwaway cluster |
| **WS-C5** ✅ **DONE + LIVE-VALIDATED** | `.kiro/specs/fix-openbao-agent-handler-flush-on-abort/` | `fix-*` (code) | — | WS-M1 |
| **WS-C6** ✅ **DONE + LIVE-VALIDATED** | `.kiro/specs/fix-openbao-agent-dev-tls-default-wiring/` | `fix-*` (code) | — | WS-M1, WS-M5 |
| **WS-C7** ✅ **DONE / OBSOLETE** | ~~`.kiro/specs/chore-svc06-spec-format/`~~ — no spec; premise resolved by prior cleanup (SVC-06 already conforms + passes the WS-M6 guard). See F.2. | n/a (closed, no reformat) | — | — |
| **WS-A2** ✅ **DONE + LIVE-VALIDATED** | SVC-06 `validation.md` live re-acceptance (AC-4) | live validation re-run (requires-infra) | cluster rebuilt | — |

### F.2 Per-stream drafting notes (what each spec must contain)

- **WS-C1 fabric-destroy TLS** — bug-condition test: a clean-slate TF-destroy env
  built by `platform_clean_slate_tf_env` is missing `TF_VAR_proxmox_insecure`;
  preservation test pins the bring-up env that *does* carry it; fix threads the
  insecure flag (and ensures `cluster_env.py` emits it) into the destroy
  subprocess; re-verify that `terraform plan -destroy` with `insecure=true`
  succeeds. This is the canonical WS-M1 motivating case (TF-env completeness).
- **WS-C2 service-discovery gap** — bug-condition: discovery over generated
  inventories returns only `svc07`; svc06/svc09 not picked up. Fix the discovery
  predicate; add a guard asserting every `svc-NN` with a generated inventory is
  discoverable. Canonical WS-M2 motivating case (discoverability leg of the
  clean-slate contract).
- **WS-C3 workload-guest destroy** — bug-condition: `consumer_clean_slate`
  reverses org/OpenBao/DB but never `pct destroy`s `workload-<vlan>-01`; fix adds
  the workload clean-slate playbook (canonical `clean-slate.yml`, target from
  inventory, not hand-passed). Canonical WS-M2 motivating case (chain-your-guests
  leg + empty-baseline incl. the workload LXC).
- **WS-M1 runtime-wiring contract tests** ✅ **DONE**
  (`.kiro/specs/runtime-wiring-contract-tests/`). Added a `testing-strategy.md`
  "Runtime-Wiring Contract" section + a reusable offline drift-guard
  `tests/installer/test_runtime_wiring_contract.py` with THREE checks:
  (1) every `delegate_to` task's target is offline-provably connectable —
  `localhost` / runtime-resolved (`inventory_hostname`/ternary) / inline
  connection vars / a same-file `add_host`-registered name — catching the
  live-caught `obs_clean_slate` bare-delegate-no-connection class; (2) every
  `*_tf_env` dict carries the full `{TF_VAR_proxmox_endpoint, _api_token,
  _insecure}` set, locking in the WS-C1 apply↔destroy parity; (3) every
  `*_secret_path*` template matches the canonical
  `(secret|{{ mount }})/(proj-|observability/)…` shape. Reusable **by glob** (a
  new role is covered with no test edit — adopt by being scanned, not by copying).
  Each check is non-vacuous + no-rot. Bug-condition proven for all three; full
  four-root offline suite green; the spec's own validation.md dogfoods the WS-M3
  gate. **Note:** check 3 is a structural-prefix consistency check, deliberately
  weaker than a single-shared-constant guarantee (the codebase hand-writes the
  same logical path in 4+ roles with different Jinja var names) — a documented
  limitation, not a tracked gap. WS-C5/C6 remain their own `fix-*` work-streams;
  WS-M1 provides the contract test for their class, not the fixes.
- **WS-M2 teardown-parity contract** — extend `service-installer.md` §4 into a
  testable contract with four legs (discoverable by the platform orchestrator /
  chains backing guests / verifies empty baseline incl. non-guest `terraform_data`
  / runs under the same env incl. insecure as bring-up). Cross-service offline
  guard: every `svc-NN` with a bootstrap has a registered clean-slate + discovery
  entry. WS-C1/C2/C3 land their guards here.
- **WS-M3 live-acceptance lifecycle gate** ✅ **DONE** (`.kiro/specs/live-acceptance-gate/`).
  Amended `spec-lifecycle.md` §5/§6 + `testing-strategy.md` (Validation Artifact +
  Non-Negotiable Rules): a `requires-infra`/live AC may be marked **DEFERRED** only
  with (a) a linked tracked backlog entry (a `WS-<id>` resolvable in `PLANNING.md`
  OR a `](../<dir>/)` link to the follow-up spec) and (b) the service
  README/implemented-services index marking it "live-unverified" — never silently
  "done". Added the validation.md completeness check as an **offline pytest
  drift-guard** `tests/installer/test_validation_completeness.py` (decided a pytest
  guard, NOT a hook/skill: the merge gate is CI-pytest and `.kiro/hooks` doesn't
  exist — corrects this doc's earlier "hook or skill" note). It flags any
  non-grandfathered AC reading "PENDING LIVE"/"gated"/"deferred" without a linked
  backlog item, and (soft, report-only) flags a doc whose Merge-Readiness and
  Known-Gaps sections disagree — correctly surfacing the SVC-07 G16 contradiction
  (`svc-07-automated-installer: properties 1`) without hard-failing the build.
  **Adoption = grandfather** (operator-approved): the pre-WS-M3 corpus (61 of 96
  validation.md defer without a link) is grandfathered via a documented,
  non-rotting `_GRANDFATHERED` allowlist with a blanket reason; the gate enforces
  **going-forward**. **Reconciling those 61 remains a tracked follow-up** (the
  G16–G20 stale-doc backlog + the piecemeal live re-acceptance) — explicitly NOT
  folded into WS-M3. Full four-root offline suite green; the spec's own
  validation.md dogfoods the gate (no deferred live AC). The 5 stale-doc rows
  G16–G20 are its first worked reconciliation examples.
- **WS-M4 design-time live-shape capture** — ADR first (boundary-touching: new
  tool + credentials). Decide read-only MCP (Proxmox + ZITADEL + OpenBao, off by
  default) vs. committed recorded API-shape fixtures only. If MCP: it stays
  read-only and opt-in, mirroring the `AGENT_LIVE_AUTHORIZED` posture. Open
  question below.
- **WS-M5 platform runtime-patterns** ✅ **DONE**
  (`.kiro/specs/platform-runtime-patterns/`). Added the steering pattern catalogue
  `.kiro/steering/platform-runtime-patterns.md` (inclusion: fileMatch on
  infra/ansible paths) capturing the four re-discovered runtime patterns — P1
  cross-guest HTTP ingress via the Traefik file-provider route (ADR-0018); P2
  delegated-task connection wiring (the WS-M1 contract + its offline guard); P3
  first-run/ordering readiness gating (the A.2 #3 transient cluster); P4
  dev-vs-prod TLS posture (WS-C6 + the SVC-09/06 self-signed mocks +
  security-standards) — each with Problem / Canonical shape / Where-it-lives /
  the design-time "a new service's design MUST state" question, citing (not
  restating) its authoritative source. Wired into `cross-cutting-categories.md`
  as a reference section (explicitly a **cross-link, not an 8th impact
  category** — the seven impact categories are unchanged), so a service design
  touching ingress / delegated tasks / readiness / dev-TLS must state how it
  handles the relevant pattern. Steering-only (no code/tests); four-root offline
  suite green; validation.md dogfoods the WS-M3 gate. **Honest limitation:** P1/P2
  are mechanically enforced (traefik_route+ADR-0018 / the WS-M1 guard) but P3/P4
  are design-review guidance only — turning them into offline guards is a possible
  future increment, not claimed here.
- **WS-M6 bugfix skill + spec-completeness hook** — **enforcing half DONE**
  (`.kiro/specs/spec-completeness-guard/`): the spec-completeness check is landed
  as the offline pytest guard `tests/installer/test_spec_completeness.py`, which
  asserts every `requirements.md` carries the mandatory 7-category cross-cutting
  analysis (check 1) and — for a *service* spec — a Day-0/teardown AC (check 2,
  **conditional**: a spec that self-declares the operator-install-doc
  "not applicable — no requires-infra tests" form is offline/methodology and
  exempt, so the guard imposes no false Day-0 requirement). Grandfathers the 14
  pre-WS-M6 non-compliant specs via an allowlist **computed by the guard's own
  detector** (self-consistent, non-rotting), enforcing going-forward; shares the
  WS-M3 completeness-guard machinery. Four-root offline suite green; the spec
  dogfoods both its own guard and the WS-M3 gate. **DEFERRED (operator decision,
  tracked):** (a) the bugfix-method **skill** (`.kiro/skills/`) — pure authoring
  guidance, no enforcement/CI, greenfield; (b) the author-time **hook**
  (`.kiro/hooks/`) — fires in the authoring session not CI, so it can advise but
  not gate a merge (the enforcing artifact is the pytest guard). Reconciling the
  14 grandfathered specs is a separate tracked follow-up.
- **WS-C4 user-provisioning path** — **ADR + re-scope DONE; code DEFERRED (tracked).**
  **ADR-0023** (`.kiro/decisions/0023-svc06-user-provisioning-path.md`, Accepted)
  fixes the grant model: the machine identity that creates human users holds
  **`ORG_USER_MANAGER` on its single org** (the narrowest ZITADEL role that manages
  users in one org — **NOT** `IAM_OWNER`, **NOT** `ORG_OWNER`), minted as a per-org
  machine user at `zitadel-org-provision` onboarding with a bounded PAT in the
  project's OpenBao path. The ADR records the sharp **contrast with ADR-0016**:
  org-*creation* is instance-level → `IAM_OWNER` by necessity, but
  user-create-*within-an-org* is org-level → `ORG_USER_MANAGER` suffices (two
  distinct identities; extends ADR-0016, supersedes none). The SVC-06 requirements
  are re-scoped (both paths in scope; public self-registration explicitly stays
  out), and `.kiro/specs/svc06-user-provisioning/` captures the design + the
  deferred code. **Deferred to a follow-up `requires-infra` code atom (WS-C4
  code):** the onboarding machine-user mint, the operator Ansible user-create task
  (a), the external-app programmatic path (b), teardown parity, and the live
  user-create + least-privilege-cross-org-refusal + login acceptance — tracked
  under WS-C4, gated by WS-M3 (never silently done). Overlaps SVC-06 org-provision;
  the code atom must coordinate so the two don't mint two different machine users
  for the same org.
- **WS-C5 handler-flush-on-abort** — ✅ **DONE + LIVE-VALIDATED** (Option B: drift-gated recovery recreate before the reconcile; `.kiro/specs/fix-openbao-agent-handler-flush-on-abort/`, live runs L1-L8). Original bug-condition: a compose reconcile failure
  before handler flush leaves a stale crash-looping sidecar across re-runs; fix
  ensures the recreate path fires (or is re-asserted) even on play abort. WS-M1
  consumer (readiness/abort wiring).
- **WS-C6 openbao-agent dev-TLS wiring** — ✅ **DONE + LIVE-VALIDATED** (A+B: fail-closed Primary-TLS preflight in `openbao-agent` + `OPENBAO_TLS_SELF_SIGNED` opt-in read from the cluster env file by the consumer deploy; default stays verify; `.kiro/specs/fix-openbao-agent-dev-tls-default-wiring/`). Original bug-condition: the consumer
  workload-deploy path doesn't wire `openbao_primary_tls_skip_verify` from the dev
  self-signed posture by default, so the sidecar can't verify the cert and
  crash-loops; fix wires the default. WS-M1 + WS-M5 consumer (dev-TLS posture
  pattern).
- **WS-C7 SVC-06 spec-format** — ✅ **DONE / OBSOLETE (no action needed).**
  Re-checked against the current repo: the premise no longer holds. The repo-root
  `requirements/SVC-06-identity-provider-requirements.md` already matches the
  15-section `requirements-template.md` exactly; the
  `.kiro/specs/svc-06-identity-provider/` `requirements.md` is the cleanest,
  most `spec-lifecycle.md`-conformant of the current specs (it has the full
  15-section structure incl. the mandatory cross-cutting analysis) and its
  `tasks.md` is well-formed; and SVC-06's requirements.md **passes the WS-M6
  spec-completeness guard** (all 7 cross-cutting categories + a Day-0/teardown AC,
  not grandfathered). The format drift WS-C7 was logged against during v0.14.0 was
  resolved by subsequent cleanup (most likely the AC-3 reopen). No committed
  `validate_spec_format` tool exists in the repo to re-run (it is a Kiro-side spec
  check referenced only in validation.md prose). Closed as obsolete rather than
  fabricating a cosmetic reformat of a compliant doc. (If a specific
  `validate_spec_format` warning resurfaces in the IDE, reopen with the exact
  warning.)

### F.3 Final sequencing (refines Part E)

1. **WS-A1 deferred-AC audit** — DONE. Ledger at `docs/audit/deferred-ac-ledger.md`
   (backlog G1–G20).
2. **WS-C3 then WS-C2** teardown blockers — concrete, confirmed, root-caused
   (§D.1); the pilot that proves WS-M2 against real bugs. **WS-C3 is the first
   atom** (self-contained; a real provisioned workload guest to prove teardown
   against). **WS-C1 is dropped from this step** — reclassified to a ride-along
   live re-verify (§D.1). Land before the next from-scratch cycle. (Cluster
   currently has service guests destroyed, SDN `vlanzone` surviving — not
   urgent-live, but these block a clean teardown.)
3. **WS-M2 teardown-parity contract** ✅ **DONE** + **WS-M1 runtime-wiring
   contract tests** ✅ **DONE** — the two highest-leverage prevention streams,
   seeded by the C-item pilots. Both landed as offline cross-service drift-guards
   (`tests/installer/test_teardown_parity.py`, `test_runtime_wiring_contract.py`)
   + their steering sections; WS-C1's TF-env/insecure parity is now locked in by
   WS-M1's check 2.
4. **WS-M3 live-acceptance lifecycle gate** — raised in priority (pervasive-pattern
   evidence). Turns the audit's central lesson into a rule + a validation.md
   completeness check; first worked examples are G16–G20.
5. **WS-M4 MCP / live-shape capture** — larger, boundary-touching; ADR + operator
   decision on scope.
6. **Fold in as capacity allows:** ~~WS-M5~~ ✅ **DONE** (steering pattern
   catalogue), ~~WS-M4 Inc.1~~ ✅ **DONE** (live-shape fixtures), ~~WS-M6~~
   ✅ **enforcing half DONE** (spec-completeness guard; skill + hook deferred), WS-C4
   (ADR first — grant model), WS-C5, WS-C6, WS-C7, and **WS-A2** (SVC-06 AC-4 live
   MFA-deny). Per the
   **piecemeal** decision, WS-A2 and every live-leg gap (G1/G4/G6/G7/G10–G13) is
   its own atom: offline-green → live-accept on the throwaway cluster → commit +
   merge that fix alone. Each atom stands up only the guests it needs; no batched
   rebuild-once cycle.

### F.4 Decisions settled with the operator

- **WS-C4 user-path — DECIDED: operator-provisioned, AND external applications
  must be able to provision human login users.** WS-C4 is therefore **code, not a
  doc-only re-scope**: the "no supported user-provisioning path" exclusion in
  SVC-06 requirements is removed and replaced by (a) an operator-provisioned path
  (console/API Ansible task) and (b) a programmatic path an external/consumer app
  can call to create a human user in its org. This widens WS-C4 beyond the
  original "operator user path" — it now carries a **machine-to-ZITADEL
  user-create capability** scoped to a consumer's own org (least-privilege, not
  `IAM_OWNER`). ADR first (the scope change + the grant model), then a spec. Feeds
  WS-M5 (the auth/least-privilege pattern) and overlaps the SVC-06 org-provision
  machine-user work (ADR-0016). **✅ ADR + re-scope LANDED (ADR-0023): the grant
  model is `ORG_USER_MANAGER` via a per-org machine user minted at onboarding (the
  org-level counterpart to ADR-0016's instance-level `IAM_OWNER` org-creation
  token); SVC-06 requirements re-scoped; `svc06-user-provisioning` spec written.
  The code (both paths + live acceptance) is the DEFERRED `requires-infra`
  follow-up atom, tracked under WS-C4.**
- **Live re-acceptance appetite — DECIDED: piecemeal.** Each fix is an atom: close
  its offline gate, run its live acceptance against the throwaway cluster, and on
  green **commit and merge that fix on its own** before starting the next. No
  batched "rebuild once, verify everything" cycle. Consequence: each `fix-*`/live
  spec owns its own throwaway-cluster bring-up of just the guests it needs
  (governed by `AGENT_LIVE_AUTHORIZED` + the throwaway guard), and WS-A2's AC-4
  MFA-deny is verified + merged as its own atom rather than deferred to a big
  cycle. This reinforces WS-M3: an atom is not "done" until its live acceptance is
  recorded, so there is no batched deferral to go stale.
- **Steering-edit batching — DECIDED: per-spec.** WS-M1/M2/M3/M5 each land their
  steering edit inside their own feature spec (traceable, smaller PRs). No single
  big steering-rewrite PR.

### F.5 WS-M4 shape — DECIDED: B first, then A

**WS-M4 design-time live-shape capture — DECIDED: Option B first, then Option A**
(i.e. converge on the combined Option C, staged).

- **Increment 1 (B — fixtures-only):** ✅ **DONE**
  (`.kiro/specs/live-shape-fixtures/`). Committed, sanitized API-shape fixtures
  under `tests/fixtures/live-shapes/` asserted by offline contract tests
  (`tests/installer/test_live_shape_fixtures.py`, framing 1a — the test imports
  the real emitter and asserts its emitted shape *equals* the fixture, so our
  drift fails offline at author time). **Shipped a deliberate 2-shape VERTICAL
  SLICE** (the two that caused merged-then-reopened bugs): **OpenBao KV-v2 read
  path** (`secret/data/<slug>/<sub>` infix + `.Data.data.<field>` nesting, vs the
  bare `bao kv` logical path — the `fix-consumer-db-bridge-kv-v2-path` class,
  asserted against `workload_auth.render_agent_hcl`) and **ZITADEL `loginVersion`
  oneof + the instance `loginV2.required` lever** (`fix-svc06-login-v1-ac3` /
  SVC-06 AC-3, asserted against `zitadel_provision.py`). Fully offline,
  hand-seeded from the already-live-confirmed shapes (no live infra, no new
  credentials), diff-reviewable; bug-conditions proven; full four-root offline
  suite green; added the "Live-Shape Fixtures" section to `testing-strategy.md`;
  the spec's own validation.md dogfoods the WS-M3 gate. The remaining ~5 fixture
  families (ZITADEL policy/role, OpenBao health/mount, Proxmox SDN/storage/node)
  are a later fixtures increment, not a WS-M4-Inc.1 gap. **Spec type:**
  offline-test feature (no ADR — no new tool boundary).
- **Increment 2 (A — read-only MCP):** a read-only Proxmox/ZITADEL/OpenBao MCP
  (off by default, opt-in, read verbs only) added **later** as the sanctioned way
  to *refresh* the Increment-1 fixtures (and let the agent peek at authoring
  time). This is the boundary-touching step → **ADR-gated** (new tool +
  credentials + dev→live path), decided + specced when fixture staleness proves
  costly enough to justify it. Landing B then A converges on Option C (fixtures =
  committed truth, MCP = refresh path) without paying the MCP surface cost up
  front.

**Sequencing:** WS-M4 Increment 1 (fixtures) folds in with WS-M5/M6 capacity;
Increment 2 (MCP) is a later ADR + spec, not part of the near-term teardown/
prevention push.
