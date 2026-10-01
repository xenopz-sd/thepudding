# Changelog

All notable changes to the Developer Services Platform are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-10-01

A **documentation-only** release that polishes the Developer-B front-door
`README.md` ahead of making the repository public. Minor version bump: no code,
provisioning, Ansible, or runtime behavior changes, and no breaking changes — the
bump reflects the substantive rework of the public entry point, not an API change.

### Changed
- **README overhaul for the public launch.** Reworked the accessible front-door
  README: added a table of contents; reframed "cluster" as "Proxmox instance" and
  added an explicit definition of a **project** (your own custom-developed software
  deployed on the same instance); sharpened the "proof is in the deployment"
  framing (build against real, already-deployed services from day one, not mocks);
  expanded the VLAN table with subnet and example-address columns; added an
  **"Addressing by example"** table (how `svc01-100-01` / `10.0.100.10` is derived)
  and a **"How Terraform keeps projects apart"** roots-and-state explainer using an
  apartment-building analogy; and removed the maintainer-oriented "Development"
  section from the consumer front door (the in-depth guidance remains in
  `docs/README.md`).
- **Contributing / mirror positioning updated.** The note now states the primary
  repository at `git.xenopz.com` is **private**, so merge requests are not accepted
  yet, while still welcoming contributions via `CONTRIBUTING.md`.
- **Link + convention integrity preserved.** The implemented-services index keeps
  its **Spec** column (platform prerequisites and SVC-07 each link both their
  `INSTALL-RUNBOOK.md` and their `.kiro/specs/` directory, per `structure.md`); the
  Documentation table's testing-guide link was corrected to `./docs/TESTING.md` and
  the maintainer-README pointer (`./docs/README.md`) re-added. The offline
  link-integrity test (`infra/tests/test_accessible_root_readme_links.py`) covers
  all of this and passes.

### Notes
- One human follow-up the offline test cannot assert: eyeball the GitHub render to
  confirm the `<picture>` light/dark logo switch and the Mermaid diagrams display
  correctly before publishing. Both degrade gracefully on renderers lacking
  support (single-logo `<img>` fallback; Mermaid renders as fenced text).

## [0.7.6] - 2026-10-01

A **documentation-only** release. It restructures the repository's front door so
the public GitHub release mirror leads with an accessible, consumer-oriented
(Developer-B) README, while the in-depth maintainer (Developer-A) documentation
is preserved under `docs/`. No code, provisioning, Ansible, or runtime behavior
changes; no breaking changes; no architectural-boundary or security-posture
changes.

### Added
- **Accessible Developer-B root `README.md` ("The Pudding").** A new front-door
  README aimed at platform consumers landing on the read-only GitHub release
  mirror: what the platform is, a quick-start path (offline + `requires-infra`
  command blocks), an at-a-glance architecture with a Mermaid topology diagram,
  the Tier-1 roadmap, a documentation map, and release-mirror positioning that
  routes development/issues/MRs to the primary repository. It retains the
  implemented-services index (platform prerequisites + SVC-07), each entry linking
  its `INSTALL-RUNBOOK.md` and its spec directory, and references light/dark logo
  assets under `docs/assets/`.
- **Offline link-integrity test** (`infra/tests/test_accessible_root_readme_links.py`,
  12 checks) as the automated evidence for the restructure: file/asset presence,
  `docs/` path prefix-disjointness, intra-repo link + image + anchor-fragment
  resolution (GitHub slugification; external hosts skipped), no-links-into-the-
  staging-bundle, doctest-tier annotation coverage with the two bring-up blocks
  tagged `requires-infra`, implemented-services-index link integrity, and a
  documentation-only git-diff scope guard. Spec:
  `.kiro/specs/accessible-root-readme/`.

### Changed
- **Developer-A documentation relocated into `docs/`.** The previous root
  `README.md` → `docs/README.md` and the root `TESTING.md` → `docs/TESTING.md`,
  with every repository-root-relative link re-prefixed so it still resolves from
  the new depth, and the mutual README↔TESTING links kept as same-folder links.
  Content is otherwise preserved verbatim. The new root README links to both via
  `./docs/README.md` and `./docs/TESTING.md`.
- **Staging bundle removed.** The `The Pudding GitHub README bundle/` staging
  folder was deleted after its README basis and logo assets were extracted to
  their authoritative locations, leaving a single source of truth for the front
  door.

### Notes
- One human follow-up the offline test cannot assert (recorded in the spec's
  `validation.md`): eyeball the GitHub render to confirm the `<picture>`
  light/dark logo switch and the Mermaid diagrams display correctly. Both degrade
  gracefully (single-logo `<img>` fallback; Mermaid renders as fenced text) on
  renderers lacking support.

## [0.7.5] - 2026-10-01

First substantive release since v0.7.0 (v0.7.1–v0.7.4 were no-op webhook-test
tags with identical trees). This is a **patch** release: two Ansible bug fixes in
the SVC-07 OpenBao bring-up plus a large, honest **validation/documentation
reconciliation** — the live acceptance evidence for the SVC-07 end-to-end
from-scratch milestone and its supporting fixes was captured from real
throwaway-cluster runs and recorded across the affected specs, and the spec task
lists were reconciled to match what is actually on `main`. No feature additions,
no breaking changes, no architectural-boundary or security-posture changes.

### Fixed
- **Unsealer readiness probe crashed on empty `bao status` output.** The
  `openbao_init_unseal` role's readiness/JSON-probe expressions used the
  empty-stdout-unsafe Jinja idiom `... | trim | first`, which raises
  `No first item, sequence was empty` on an empty/transient `bao status` body
  (including when `-tls-skip-verify` is omitted against a self-signed cert and the
  TLS error goes to stderr) — aborting the `until` poll instead of retrying.
  Replaced with the empty-safe slice `(... | trim)[:1]` at all 8 readiness/
  status/mounts probe sites in `unsealer_bootstrap.yml`, with a new offline
  regression guard (`tests/installer/test_unsealer_readiness_empty_stdout_guard.py`).
  Behaviour is preserved for every non-empty JSON body; only the empty case now
  retries rather than crashing. Spec:
  `.kiro/specs/fix-svc07-unsealer-readiness-empty-stdout/`.
- **`openbao_init_unseal` sysctl task used the wrong collection FQCN.** Corrected
  to resolve from the pinned `ansible.posix` collection, guarded by the
  `ansible-lint fqcn[canonical]` rule. Spec:
  `.kiro/specs/fix-openbao-sysctl-posix-collection/`.

### Changed
- **Validation evidence + spec task-list reconciliation (no behavior change).**
  Live acceptance from throwaway-cluster runs was recorded, and task lists closed
  to match `main`, across: the Terraform→Ansible handoff (`terraform-ansible-handoff`,
  live provision→generate→configure, Req 6.1–6.3); the SVC-07 automated installer
  from-scratch acceptance (`svc-07-automated-installer`); the primary health-check
  reachability and recreate-churn milestone (`fix-svc07-primary-health-check-reachability`,
  `fix-svc07-primary-recreate-churn` — `docker events` no-post-bootstrap-recreate
  proof); the live-apply backend-init fix (`fix-live-apply-backend-init`); the SDN
  privilege model per ADR-0001 (`fix-sdn-privilege-model`); the SDN NIC double-tag
  fix (`fix-svc07-sdn-nic-double-tag`); the unsealer bootstrap delegation with the
  bug-#9 end-to-end closure (`svc07-unsealer-bootstrap-delegation`); OpenBao swap
  hardening per ADR-0006 (`fix-openbao-swap-hardening`, live unprivileged-LXC init
  re-test); the cluster-env quote-stripping and endpoint-path installer fixes
  (`fix-cluster-env-quote-stripping`, `fix-cluster-env-endpoint-path`); the
  per-host Docker install model per ADR-0003 (`lxc-docker-base-template`, live
  P1/P2); the agent live-test authorization gate (`agent-live-test-authorization`);
  the installer simplification (`svc07-installer-simplification`); and the
  provision-then-test documentation navigation (`docs-provision-then-test-navigation`).
- **Known residual (documented, not blocking):** the live idempotent-re-run
  (`changed=0` against an already-converged cluster) is deferred — the SVC-07
  orchestrator re-run fail-closes by design on an already-initialised unsealer
  without a supplied `OPENBAO_BOOTSTRAP_TRANSIT_TOKEN`; the structural idempotency
  gate is offline-verified. Recorded in the affected specs' `validation.md`.

## [0.7.4] - 2026-11-06

Maintenance release with **no code, documentation, or behavior change** since
v0.7.3. Cut solely to emit another release/tag event for verifying the project's
release webhook integration. The tree is identical to v0.7.3 apart from this
changelog entry.

### Changed
- Nothing functional. This is a no-op release used to trigger and validate the
  release webhook; if you are on v0.7.3 there is nothing to upgrade for.

## [0.7.3] - 2026-11-06

Maintenance release with **no code, documentation, or behavior change** since
v0.7.2. Cut solely to emit another release/tag event for verifying the project's
release webhook integration. The tree is identical to v0.7.2 apart from this
changelog entry.

### Changed
- Nothing functional. This is a no-op release used to trigger and validate the
  release webhook; if you are on v0.7.2 there is nothing to upgrade for.

## [0.7.2] - 2026-11-06

Maintenance release with **no code, documentation, or behavior change** since
v0.7.1. Cut solely to emit a second release/tag event for verifying the project's
release webhook integration. The tree is identical to v0.7.1 apart from this
changelog entry.

### Changed
- Nothing functional. This is a no-op release used to trigger and validate the
  release webhook; if you are on v0.7.1 there is nothing to upgrade for.

## [0.7.1] - 2026-11-06

Maintenance release with **no code, documentation, or behavior change** since
v0.7.0. Cut solely to emit a release/tag event for verifying the project's release
webhook integration. The tree is identical to v0.7.0 apart from this changelog
entry.

### Changed
- Nothing functional. This is a no-op release used to trigger and validate the
  release webhook; if you are on v0.7.0 there is nothing to upgrade for.

## [0.7.0] - 2026-11-06

Adds **Platform Install Simplification** — the post-v1 milestone that reduces the
from-nothing platform bring-up, as experienced by a platform user (Developer B),
to a minimal, documented command set: a **guided config-authoring wizard** plus a
**two-command** bring-up shape, without weakening any architectural boundary,
security posture, or the agent-live throwaway-guard flow. The core design decision
(ADR-0012) is an **author-then-apply** split: interactive prompting is confined to
a single up-front, non-cluster-mutating config-authoring step that produces/
validates the env file and STOPS; the automation phase stays prompt-free,
file-driven, and fail-closed at preflight, and the env file remains the single
source of truth (a fully non-interactive hand-edit / pre-provisioned-file path
stays first-class). Validated by the full offline suite; no live acceptance was
required (the wizard is non-mutating and offline-testable, and the two-command
shape reuses bring-up logic already live-validated by v0.5.0/v0.6.0). Spec:
`.kiro/specs/platform-install-simplification/`.

### Added
- **Guided env-fill wizard** `scripts/installer/cluster_env_wizard.py` — reuses
  `cluster_env.py`'s `SCHEMA` + `validate()` (no schema duplication), prompts for
  the non-secret cluster parameters and writes them to the gitignored env file at
  mode 0600, validates the result, and stops. Invoked by absolute venv path; it
  performs no host/API/Terraform mutation and triggers no bring-up. Offered as one
  way to author the env file — hand-editing or a pre-provisioned file remains
  first-class.
- **Admin-credential hygiene** — the admin bootstrap credential is prompted
  NO-ECHO (`getpass`), held in memory only, and NEVER written to disk or echoed;
  only the least-privilege minted `terraform@pve` token is persisted (0600), by
  the existing identity phase.
- **ADR-0012** `.kiro/decisions/0012-config-authoring-step-permitted-outside-the-prompt-free-automation-phase.md`
  (+ project-constitution Decision Log row) recording the scoped `service-installer.md`
  §2.2 relaxation with invariants I1 (single source of truth) and I2
  (author-then-apply separation).
- **Open-source distribution classification** recorded in `project-constitution.md`
  (previously absent), noting the hybrid self-hosted/internally-operated posture.
- **README platform-user Quickstart** — a new, minimal-friction
  `## Quick start (platform users)` section (guided env-fill + the two bring-up
  commands) linking to the install runbook for depth; the existing developer
  quickstart is renamed `## Development quick start`. Existing OSS entry-point docs
  (`LICENSE.md` Apache-2.0, `CONTRIBUTING.md`, `SECURITY.md`) verified and linked
  from the README Documentation map.
- **Offline tests** `tests/installer/test_cluster_env_wizard.py` (12 tests)
  pinning correctness properties P1–P4: automation-never-prompts, env-file-is-
  single-source-of-truth / schema-reuse, admin-credential-never-persisted, and
  non-secret-authoring-complete-or-fails-closed — plus the mint-mode token-only
  exception.
- **`oss-doc-baseline-followup`** spec stub tracking the deferred `CODE_OF_CONDUCT`
  + the community-participation decision + a wider OSS-doc-baseline audit.
- Two new steering documents: `documentation-strategy.md` and
  `documentation-types.md` (documentation vocabulary + which docs a project
  requires).

### Changed
- `service-installer.md` §2.2 — the "no interactive variable prompts" ban is
  scoped to the automation phase; a single up-front, non-cluster-mutating
  config-authoring step is now explicitly permitted (invariants I1/I2). The
  Ansible-only orchestration rule is unchanged.

### Fixed
- **Mint-mode / API-token validation reconciliation** — `cluster_env.SCHEMA` marks
  the API token mandatory (correct for the non-mint path), but the live
  `platform_preflight` deliberately tolerates a blank token when the host bucket
  mints it first. The wizard now applies a narrow, mint-mode-only exception
  (tolerate a blank API token ONLY when identity-mint is opted in, by reusing
  `cluster_env.resolve` — no schema change), so the wizard's mint-mode happy path
  matches the real bring-up behavior; any other missing mandatory var, or a
  missing token when not minting, still fails closed at authoring time.

## [0.6.0] - 2026-11-05

Adds **Platform Clean-Slate** — the tear-DOWN counterpart to the v0.5.0
prerequisites bootstrap. A `-e clean_slate=true` branch on
`ansible/playbooks/platform-bootstrap.yml` (delegating to a new
`platform_clean_slate` role) returns the shared platform prerequisites layer to a
from-nothing baseline so the from-scratch provision path can be re-run cleanly,
satisfying the `service-installer.md` §4 clean-slate contract the bootstrap
orchestrator previously lacked. The core boundary (ADR-0011): the platform
clean-slate **orchestrates each service's OWN clean-slate** (child
`ansible-playbook` run) so per-service Terraform state stays authoritative and
isolated (`integration-boundaries.md` §5) — it never `pct destroy`s a service
guest nor `terraform destroy`/`state rm`s a service's per-service state, owning
only the `platform-foundation` state + host gateway config. Validated by the full
offline suite plus a live throwaway-cluster acceptance: clean-slate to a
verified-empty baseline (`p20` stays absent across a host reboot), then a
from-scratch bring-up + SVC-07 apply succeeding against the freshly-cleaned
prerequisites, and an operator-confirmed full manual run
(clean-slate → prerequisites → SVC-07) end-to-end. Spec:
`.kiro/specs/platform-clean-slate/`.

### Added
- **Clean-slate branch + role** — `ansible/playbooks/platform-bootstrap.yml`
  gains a `-e clean_slate=true` branch delegating to the new
  `ansible/roles/platform_clean_slate/` role, which runs the destroy-safe
  REVERSE order: throwaway-guard consult → confirmation → file-mediated
  Service_Discovery → per-service Service_Clean_Slate (child run) →
  Fabric_Teardown (`terraform destroy` of `platform-foundation` + `sdn_gateway`
  decommission) → Reboot_Teardown_Step (SDN-absence verify / opt-in reboot,
  no-false-clean) → three-way Verification_Gate.
- **Controls** (all default to the safe/off position): `clean_slate` (route to
  teardown), `clean_slate_confirm=force` (skip the interactive prompt),
  `platform_clean_slate_reboot=true` (opt-in host reboot to clear the stuck SDN
  zone — recommended on a throwaway cluster), `clean_slate_scope=all|fabric-only`,
  `platform_clean_slate_proxmox_host` (fail-closed if unset).
- **Install runbook** — `infra/platform-foundation/INSTALL-RUNBOOK.md` §5 gains
  an automated clean-slate section (5a, primary) reconciled with the manual
  fallback (5b), correct doctest tiers, and a `p20` create↔delete cross-reference.
- **ADR-0011** `.kiro/decisions/0011-platform-clean-slate-orchestrates-service-clean-slates.md`
  (+ project-constitution Decision Log row); an ADR-0010 addendum recording the
  reboot-as-teardown-step finding.
- **Offline drift-guards** — `tests/installer/test_platform_clean_slate_wiring.py`
  pinning correctness properties P1–P6 (guard-before-destruction,
  service-clean-slate-before-fabric-teardown, confirmation-unless-force,
  reboot-opt-in-default-OFF + no-false-clean, three-way gate, boundary invariant),
  and `tests/installer/test_sdn_gateway_vlan_set_normalization.py`. The agent-live
  meta-drift-guard recognizes the new destructive orchestrator role with no new
  leaf-exemption (second cross-service proof).

### Changed
- Bring-up phases in `platform-bootstrap.yml` switched `import_role`→`include_role`
  so a `clean_slate=true` run skips them entirely rather than pushing the phase
  gate down onto tasks (behavior-preserving for the normal bring-up path).
- `ansible/roles/sdn_gateway/` — VLAN-id sets are normalized (a string like `20`
  passed via `--extra-vars` is no longer iterated char-by-char) and the
  decommissioned set is subtracted from the served set, so a decommission of the
  shared VLAN 20 tears its stanza down instead of being re-served. Shared-role
  fix; affects the bring-up gateway path too.

### Fixed
- **Interactive confirmation prompt could never be confirmed** — the clean-slate
  `ansible.builtin.pause` used both `prompt:` and `seconds:` (timed mode discards
  typed input), so typing `yes` was ignored and the run always aborted; only
  `-e clean_slate_confirm=force` worked. Switched to a prompt-only pause (Option
  B); the P3 drift-guard now asserts no `seconds:`/`minutes:` so it cannot regress.
- **Stuck-SDN-zone reboot documentation** — the 5a runbook now recommends
  `-e platform_clean_slate_reboot=true` on the first run (the survive-then-halt
  case is the common one) and documents the fail-closed halt as expected behavior
  with its one-line remedy, instead of surfacing the flag only after a failed run.
- **Three live-found fixes** (all fail-closed — nothing wrongly destroyed): the
  Proxmox host address is threaded into each child clean-slate via a
  service-agnostic `proxmox_host_var` catalog field; the verification gate counts
  only REAL resources via per-row `state_ignore_patterns` (tolerating inert
  `terraform_data.*` module anchors) instead of "zero state lines"; and the SDN
  absence probe no longer misreads `Device "p20" does not exist` as present.

## [0.5.0] - 2026-11-04

Adds **Platform Prerequisites Bootstrap** — a single config-driven Ansible
orchestrator (`ansible/playbooks/platform-bootstrap.yml`) that takes a fresh
Proxmox cluster from nothing to a state where any catalog service can be
provisioned: the `terraform@pve` identity + token, the base LXC/VM template, the
shared SDN VLAN-20 fabric, and the Proxmox host L3 gateway — plus
developer-workstation reachability. Mirrors the SVC-07 installer UX (edit
`cluster.env`, run one command) and reuses existing mechanisms (the
`platform-foundation` Terraform root, the `sdn_gateway` role, `cluster_env.py`,
the agent-live throwaway guard) rather than reimplementing them. Validated by the
full offline suite plus a live from-nothing acceptance on the throwaway cluster
(host+api bootstrap → SVC-07 from-scratch apply reaching healthy → devmachine
apply idempotent). Spec: `.kiro/specs/platform-prerequisites-bootstrap/`.

### Added
- **Config-driven orchestrator** `ansible/playbooks/platform-bootstrap.yml` with
  selectable buckets via `-e bootstrap_scope=all|host|api|devmachine`
  (comma-list), `platform_preflight` running first for every invocation, and
  per-phase block/rescue naming.
- **Roles**: `platform_preflight` (resolve/validate cluster.env + fail-closed
  per-bucket & cross-bucket preconditions), `platform_pve_identity` (opt-in
  `pveum` mint of the least-privilege `terraform@pve` role/user/token, `no_log`,
  token persisted at 0600 — default OFF), `platform_host_prep` (idempotent
  `pveam` ensure-template), `platform_api` (child-process `terraform apply` of
  `platform-foundation`), `platform_gateway` (host L3 SDN gateway via the
  existing `sdn_gateway` role), `platform_devmachine` (emit-by-default /
  apply-after-GO static route + `~/.ssh/config` pin).
- **cluster.env schema extension** (`scripts/installer/cluster_env.py`) — new
  OPTIONAL host-prep / gateway / dev-machine rows (per-bucket mandatory-ness
  decided by the orchestrator), with the admin bootstrap credential + API token
  never surfaced in a human-facing message; delimited subsections in
  `cluster.env.example`.
- **Throwaway-guard consult** wired into all destructive phases (host, api,
  gateway), gated `when: agent_live_run`, before any mutation; the agent-live
  meta-drift-guard generalized to recognize destructive orchestrator PLAYBOOKS
  (first cross-service proof beyond SVC-07).
- **Install runbook** `infra/platform-foundation/INSTALL-RUNBOOK.md` — a
  provision-then-test walkthrough with correct doctest tiers, plus a manual
  clean-slate reset section (incl. the required host reboot to clear the stale
  SDN zone/bridge). README implemented-index entry.
- **Offline drift-guards** pinning the seven correctness properties (bucket
  selection, PVE-identity opt-in + secret hygiene + least-privilege,
  ensure-template idempotence, api token precondition, devmachine emit-default,
  guard wiring, cluster.env parse).

### Changed
- Extended `cluster_env.py` SCHEMA additively (new optional rows only) — no
  existing mandatory var, alias, or the pure parse/resolve/validate contract
  changed; SVC-07 callers unaffected.

### Fixed
- **Phase order identity→api→gateway (ADR-0010)** — the host L3 gateway binds
  `.1` to the SDN VNet bridge `p<vlan>` that the api bucket's `terraform apply`
  creates, so it is sequenced as its own phase AFTER the api bucket; the original
  host→api order could not satisfy this on a true clean slate. A preflight
  cross-bucket precondition fails closed if `host` runs without `api` and the
  bridge is absent.
- **Minted token needs `SDN.Use` (ADR-0009)** — the least-privilege
  `terraform@pve` role gained `SDN.Use` (guest-NIC attach to an existing VNet,
  per NET-00 §6) in addition to `SDN.Allocate`/`SDN.Audit`, so a service apply
  can place guests onto the shared VNet; never `Administrator`.

## [0.4.0] - 2026-09-27

Adds **Agent Live-Test Authorization** — a service-agnostic, off-by-default
safety control that lets the Kiro agent run destructive live operations
(from-scratch bring-ups, `clean_slate` resets, `terraform apply`/`destroy`)
against a **designated throwaway Proxmox cluster only**, behind a per-run
interlock and a machine-checked, fail-closed cluster-identity guard. Validated by
the full offline suite plus a live positive+negative acceptance on the throwaway
cluster. Spec: `.kiro/specs/agent-live-test-authorization/`.

### Added
- **Throwaway-cluster authorization guard** (`scripts/installer/throwaway_guard.py`)
  — a pure, offline-testable decision core (`decide` / `normalize_host` /
  `is_armed` / `parse_services_scope`) plus a thin `main()` CLI. AUTHORIZED only
  when the `AGENT_LIVE_AUTHORIZED` interlock is armed AND (any
  `AGENT_LIVE_AUTHORIZED_SERVICES` scope grants the service) AND the resolved
  cluster identity (normalized endpoint host + exact node name) is on the
  allowlist; fails CLOSED on any ambiguity. Reads only the non-secret identity —
  never a credential.
- **Committed, non-secret allowlist** (`scripts/installer/throwaway-clusters.yml`)
  of throwaway-cluster identities.
- **Isolated agent env source** — `.env.throwaway.example` (committed template;
  `.env.throwaway` gitignored) distinct from a developer's manual `.env`, so an
  automated agent run cannot target a developer's separate Proxmox. The interlock
  is an environment variable set per run, never a committed file key.
- **SVC-07 reference wiring** — the guard is consulted (consult + INFO decision
  log + rc-assert, gated `when: agent_live_run`) in `svc07_clean_slate` before
  any destroy and in `svc07_preflight` before the first Proxmox call.
- **Meta-drift-guard** (`tests/installer/test_agent_live_wiring_coverage.py`) —
  enforces that every destructive-live orchestrator entry point carries the guard
  consult, so a future service cannot ship an unguarded destructive path.
- **Steering** — a new "Agent Live-Test Authorization" subsection in
  `testing-strategy.md`, cross-referenced from `dev-workflow.md` and
  `security-standards.md`.

### Changed
- **`normalize_host` scheme comparison** is case-insensitive (`re.IGNORECASE`) so
  the allowlist host match honors the "scheme ignored" contract
  (`HTTPS://Node.LAN` == `https://node.lan`).
- **SVC-07 install runbook** gains an "Agent-driven acceptance (throwaway
  cluster)" walkthrough with correct doctest tiers, and the `.env.throwaway`
  operator SSH public key is documented as required for a from-scratch bring-up.

### Security
- The control only NARROWS what may act on live infrastructure; unarmed operator
  runs are byte-for-byte unchanged. The interlock is a local-development
  affordance — never in CI/acceptance/production. The guard is defense-in-depth
  atop operator-scoped Proxmox tokens: even a mis-scoped token cannot drive a
  destructive agent run against a non-allowlisted cluster. No secret value is
  read, logged, or embedded (pinned by a sentinel-token non-secrecy test).

### Known limitations
- Only SVC-07 is wired as the reference consumer; the meta-drift-guard enforces
  the pattern for future services. Cross-service resource-scoping on a shared
  throwaway cluster (one service's clean-slate vs another's guests) is a noted
  future follow-up, out of scope here.

## [0.3.0] - 2026-09-27

Maintainability rework of the **SVC-07 (OpenBao) Day-0 installer** — extracts the
~1900-line orchestrator monolith into five focused Ansible roles behind a
189-line thin orchestrator, with **zero behavioral change to the working install
path** plus four targeted hygiene/correctness fixes. Validated by the full
offline suite and a live from-scratch bring-up on the cluster (green Phase 4,
`PLAY RECAP ... failed=0`, exit 0). Spec:
`.kiro/specs/svc07-installer-simplification/`.

### Added
- **Five SVC-07 installer roles** — `svc07_preflight` (Phase 1), `svc07_provision`
  (Phase 2), `svc07_bootstrap` (Phase 3), `svc07_handoff` (Phase 4), and
  `svc07_clean_slate` (reset branch); the phase logic moved into them verbatim.
- **`svc07-health.yml` child configurator play** — the Phase-4 two-stage
  recreate-robust health gate (`docker inspect` → `docker exec bao status`) now
  runs `hosts: openbao` natively on the guest, invoked by `svc07_handoff` via
  `ansible-playbook -i {{ svc07_generated_inventory }}`.
- **Consolidated, guarded debug includes** — `svc07_bootstrap/tasks/debug_probes.yml`
  (Phase 3) and `ansible/playbooks/tasks/svc07-health-debug-probes.yml` (Phase 4),
  each imported once under `when: svc07_debug_instrumentation | default(false) | bool`.
- **Offline drift-guard tests** pinning correctness properties P1–P8 in
  `tests/installer/` (thin play, entry unchanged, secret hygiene, no-delegate_to
  health, swap-gate, health child-run selection, clean-slate purge, debug
  consolidation), plus a regression guard for the child-play var-scope fix.

### Changed
- **`svc-07-bootstrap.yml` thinned to a 189-line orchestrator** — exactly five
  `import_role` calls wrapped in per-phase `block`/`rescue` phase-naming; no inline
  phase tasks. The Developer B entry command is unchanged.
- **`terraform_clean_slate`** now issues `pct destroy <vmid> --purge` (scoped to the
  two SVC-07 VMIDs 1070/1071) so a guest's backing volumes are purged with it — no
  more manual Proxmox-GUI deletion before a from-scratch reinstall.
- **`INSTALL-RUNBOOK.md`** updated for the five-role layout, the child-play health
  gate, `--purge` clean-slate, and the restored secret-hygiene posture (doctest
  tiers preserved); README implemented-services index extended with this spec.

### Fixed
- **Secret hygiene restored** — the primary-bootstrap task is `no_log: true` again
  (the baseline carried a preserved `no_log: false` debug edit that revealed the
  Admin token in the run log); every token-handling task defaults to `no_log: true`,
  with dev exposure only via off-by-default opt-ins routed to 0600 gitignored
  artifacts.
- **Child-play variable scope** — the `svc07_handoff` role bridges the health-gate
  knobs (`svc07_openbao_primary_container_name`, `svc07_openbao_bao_bin`,
  `svc07_openbao_primary_tls_skip_verify`, retry budget) into the standalone
  `svc07-health.yml` child play via `-e`; without this the child play (which does
  not load the role's defaults) threw `'svc07_openbao_primary_container_name' is
  undefined` at argv render — caught by the live acceptance and now guarded offline.

### Security
- No secret value reaches any log at the default posture; the completion banner
  references `OPENBAO_ADMIN_TOKEN` by name only. Clean-slate destruction stays
  scoped to the two SVC-07 VMIDs — never a global/cross-tenant volume prune.

## [0.2.0] - 2026-09-22

First platform release delivering **SVC-07 (OpenBao) secrets manager** end-to-end,
the **Terraform → Ansible provisioning handoff**, and the **SDN VLAN network
foundation**, validated from-scratch on the live cluster.

### Added
- **SVC-07 Secrets Manager (OpenBao)** — full buildout: two-LXC primary + Transit
  unsealer topology, Ansible roles (`openbao_install`, `openbao_init_unseal`,
  `openbao_project_onboard`), one-time bootstrap (init/unseal/engines/audit,
  Root_Token revoke), and per-project onboarding creating the isolation triple
  (`policy-<slug>`, `auth/jwt/role/<slug>-ci`, KV path). Includes the SVC-07
  Terraform root and its `INSTALL-RUNBOOK.md` provision-then-test walkthrough.
- **Opt-in platform-admin token bootstrap mint** — homelab/no-ZITADEL onboarding
  path (off by default; obtains admin tokens via OIDC/ZITADEL in production).
- **Terraform → Ansible handoff** — one-directional, file-mediated dynamic
  inventory generator (`generate_inventory.py`) rendered from `terraform output`.
- **SDN VLAN gateway reachability** (`sdn_gateway` role, ADR-0004) — Proxmox host
  as L3 router/firewall for SDN VLAN subnets, developer static-route path.
- **Canonical clean-slate reset** (`terraform_clean_slate`) — guarded `pct destroy`
  + `terraform state rm` for from-scratch rebuilds.
- **Repository governance files** — `LICENSE.md` (Apache-2.0), `SECURITY.md`,
  `CONTRIBUTING.md`, `AI_USAGE.md`, `NOTICE.md`.

### Changed
- **LXC base template / Docker install** (ADR-0003, supersedes ADR-0002) — services
  boot the stock `debian-12-standard` template; the `common` role installs Docker +
  Compose per host (no custom pre-baked template).
- **OpenBao swap hardening** (ADR-0006) — encrypted host swap + per-container
  `memory.swap.max=0`; removed the in-container `vm.swappiness` write and
  `IPC_LOCK`, pinned `disable_mlock = true`.

### Fixed
- **Onboard `projects.yaml` path** — re-anchored the registry default two levels up
  to the repo root (was resolving to a non-existent `ansible/infra/...` path).
- **Onboard TLS verification** — every `bao` call now conditionally splices
  `-tls-skip-verify` (gated on the mock-cert toggle); added an off-by-default
  `openbao_dev_expose_secrets` debug affordance.
- **Onboard jwt-role write** — sends `bound_claims` as a JSON map on stdin instead
  of a stringified `key=value` positional (OpenBao rejected the string with a 400).
- **Platform-admin token mint** — use `-orphan` so the Root_Token revoke does not
  cascade-revoke it.
- Onboard registry read and clean-slate state ops correctly delegate to the control
  node with `become: false`; OpenBao audit stanza fixed to two-label HCL; SVC-07
  metrics listener telemetry sub-block; unsealer bootstrap fixes.

### Security
- Unsealer seal-API intra-VLAN scoping investigated and recorded as a known
  limitation (ADR-0007/ADR-0008); NIC-firewall approach reverted after live testing.

### Known limitations
- Real GitLab CI-JWT / ZITADEL OIDC IdP wiring is deferred (homelab uses the opt-in
  admin-token mint until SVC-06/ZITADEL is deployed).
- A mock-cert (`openbao_tls_self_signed=true`) onboard run must pass that flag
  explicitly; auto-deriving the mock-cert state is a recorded follow-up.

## [0.1.0] - 2026-09-04

### Added
- First stable release of the Developer Services Platform foundation (Proxmox +
  Terraform `bpg/proxmox` + Ansible; SDN VLAN-per-project addressing; offline
  test suite made offline-by-construction).

[0.2.0]: https://git.xenopz.com/xenopzsd_internal/devinfra/-/compare/v0.1.0...v0.2.0
[0.1.0]: https://git.xenopz.com/xenopzsd_internal/devinfra/-/releases/v0.1.0
