# Changelog

All notable changes to the Developer Services Platform are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-10-30

The **per-project consumer path** lands and is proven end-to-end on the throwaway
cluster: a project declares the catalog services it consumes in a
`consumer-manifest.yml`, and the manifest-driven `project-consumer-bootstrap.yml`
orchestrator wires them by COMPOSING the existing per-service onboarders — never
reimplementing them. Three feature specs make this real: **OpenBao workload
AppRole auth** (`openbao-workload-auth`), **ZITADEL SPA/PKCE + two-client
onboarding** (`zitadel-spa-pkce-onboarding`), and the **consumer onboarding
orchestrator + per-project SVC-01 consumer root** (`project-consumer-onboarding`).
A full live acceptance for the `dronefleet` project — a dedicated SVC-01 DB on its
own VLAN 100 coexisting with the shared services on VLAN 20, the OpenBao isolation
triple, a workload AppRole, and a two-client SSO org (public SPA + API) in one
ZITADEL org/project — completed `failed=0`. Seven correctness fixes surfaced by
that live acceptance also land, each fixed in its own spec (never hand-patched).
Minor version bump: all additive (the per-project consumer vertical slice + its
onboarders) plus fixes — no breaking changes. The implemented-services set is
unchanged (SVC-01/06/07/09 + SVC-17/18 + platform prerequisites); this release
adds the per-project CONSUMER path on top of them.

### Added
- **Consumer onboarding orchestrator + per-project SVC-01 consumer root** (spec
  `project-consumer-onboarding`): a per-project PostgreSQL guest on the project's
  OWN VLAN (100+) via the `db/` SVC01_Consumer_Root, plus the manifest-driven
  `ansible/playbooks/project-consumer-bootstrap.yml` 4-phase orchestrator that
  reads a project's `consumer-manifest.yml` and wires its declared DB + SSO +
  secrets by COMPOSING the per-service onboarders (feature-gated per manifest
  block, idempotent/resumable). Ships `scripts/installer/consumer_manifest.py`
  (pure-Python schema validator) and the `dronefleet` scaffold project
  (VLAN 100) with its `INSTALL-RUNBOOK.md`. The first per-project-VLAN SVC-01
  consumer; live-validated end-to-end on the throwaway cluster.
- **OpenBao workload AppRole auth path** (spec `openbao-workload-auth`): the
  `openbao_workload_approle` onboarder mints `auth/approle/role/<slug>-workload`
  bound to `[policy-<slug>]` with manifest-driven TTLs, plus the agent-side
  AppRole login/credential-fetch role, handing the `role_id`/`secret_id` off over
  the PF FR-8 first-secret channel (0600 host file or GitLab CI/CD variable). The
  workload fetches its DB credential + OIDC client secret from OpenBao via its own
  Agent/AppRole at container start — never a plaintext secret in a repo.
  Live-validated.
- **ZITADEL SPA/PKCE + two-client onboarding** (spec
  `zitadel-spa-pkce-onboarding`): the `zitadel-org-provision` role +
  `zitadel_provision.py` helper now onboard MULTIPLE app registrations of multiple
  types (public SPA with PKCE + `NONE` auth, and an API resource server) in ONE
  org/project in a single run, via the `zop_app_registrations` list — a superset
  of the legacy single confidential-web `<slug>-webapp` default (preserved for an
  empty list). Live-validated.

### Fixed
- **Consumer manifest → role app_type/auth_method vocabulary gap** (spec
  `fix-consumer-manifest-app-type-mapping`): `consumer_bootstrap` STEP 4 now maps
  the manifest's descriptive vocabulary (`public-spa`/`confidential-web`/`api`,
  `none`/`basic`/`jwt-profile`) to the `zitadel-org-provision` role's short forms
  (`spa`/`web`/`api`, `none`/`basic`/`jwt`) at the composition boundary — a
  single-source-of-truth mapping with a fail-closed per-entry assert — instead of
  passing the descriptive values through untranslated (which the role rejected
  `unrecognized app_type 'public-spa'`). Manifest schema and role accepted-set
  unchanged; `name`/`redirect_uris` preserved. Live-caught, live-validated.
- **ZITADEL org-provision production-path TLS verification** (spec
  `fix-zitadel-org-provision-production-path-insecure-tls`): the production
  (control-node → Traefik HTTPS) helper invocation now appends a gated
  `--insecure` (new `zop_base_url_tls_skip_verify`, default true for the
  self-signed `traefik_tls_self_signed` mock reality, flip to false once SVC-08 /
  Let's-Encrypt real certs land) so SSO onboarding no longer fails
  `CERTIFICATE_VERIFY_FAILED` against Traefik's self-signed mock cert. The
  dev-guest HTTP path and the helper are unchanged. Live-caught, live-validated.
- **SVC-06 clean-slate OpenBao provision_token residue** (spec
  `fix-svc06-clean-slate-provision-token-residue`): a SVC-06 clean-slate now
  clears the stale ZITADEL `provision_token` FIELD from OpenBao (a field-scoped
  `bao kv patch` — the co-resident `masterkey` and the sibling backing-DB
  `app_password` are left intact, NEVER a whole-entry delete), gated on OpenBao
  reachability with a de-masked failure. Without this, a clean-slate left the
  prior instance's token in OpenBao (a different, untouched guest) and
  zitadel-server's absolute persist idempotency refused to replace it on the next
  re-bootstrap, so a consumer reading it got HTTP 401 `Errors.Token.Invalid`. Now
  the re-bootstrap persists a fresh, coherent PAT. Live-caught, live-validated
  (masterkey confirmed 32 bytes intact after the clear).
- **SVC-06 clean-slate backing-DB residue** (spec
  `fix-svc06-clean-slate-backing-db-residue`): the backing-DB identity
  (`svc06_db_vmid`/`svc06_db_project_slug`/`svc06_db_vlan_id`) is hoisted to
  play-level `vars` so it is visible on the `clean_slate=true` branch (it was only
  in `svc06_provision/defaults`, unloaded on that branch, so VMID 1102 was never
  destroyed), plus a hardened empty-baseline gate (pre-survivor assert + whole-
  token `select('in', stdout.split())`). Live-caught, live-validated.
- **Composed OpenBao onboarder primary delegation** (spec
  `fix-composed-openbao-onboarder-primary-delegation`): `openbao_project_onboard`
  and `openbao_workload_approle` now carry a `delegate_to` ternary on every
  `docker exec`/`cp` task so that when composed on `hosts: localhost` they reach
  the OpenBao primary over SSH (instead of running on the control node →
  `No such container`); the empty default preserves the standalone run-in-place
  behavior byte-for-byte. Live-caught, live-validated.
- **Composed workload-cred handoff writable path** (spec
  `fix-composed-workload-creds-dir-writable-path`): the composed
  `openbao_workload_approle` file-channel handoff writes the workload credentials
  under `$HOME/.openbao-workload/<slug>` owned by the invoking user (not the
  standalone role's `/etc/openbao-workload/<slug>` + root), so the unprivileged
  control-node consumer run can write them. Live-caught, live-validated.
- **SVC-01 per-instance local Terraform state** (spec
  `fix-svc01-local-backend-per-instance-state`): the SVC-01 local backend path is
  now per-instance (`<svc01_state_name>.tfstate`) so two SVC-01 instances (the
  shared-zitadel backing DB and a per-project consumer DB) do not share one state
  file — honoring one-state-per-service.

### Changed
- **README** — the per-project consumer-onboarding row in the implemented-paths
  index is updated to reflect the end-to-end live validation (two-client SSO +
  workload AppRole + per-project-VLAN DB coexistence proven, not merely built).

## [0.10.0] - 2026-10-28

The fifth implemented service lands: **SVC-17/18 (Prometheus + Grafana + Loki
observability)** on three VLAN-20 LXC guests, wiring a v1 scrape set across the four
already-built services at `up==1`. Alongside it, **cross-guest Traefik ingress is
generalized** — each HTTP backend on a separate guest from Traefik now renders and
delegate-copies its own file-provider static route (ADR-0018), converting SVC-06
(ZITADEL, h2c) and SVC-07 (OpenBao, https) onto the mechanism and completing the
http/https/h2c three-scheme set without Docker Swarm. A single authoritative
**dependency-ordered all-services from-scratch walkthrough + reverse-order teardown**
is added to `docs/TESTING.md` and proven live on the throwaway cluster. Six
correctness fixes surfaced by that from-scratch validation also land. Minor version
bump: all additive (one new implemented service, one new cross-cutting ingress
feature, new operator docs) plus fixes — no breaking changes. This release brings the
implemented-services set to SVC-01/06/07/09 + SVC-17/18 + platform prerequisites.

### Added
- **SVC-17/18 — Observability stack (Prometheus + Grafana + Loki)** (spec
  `svc-17-18-observability`, ADR-0017): the shared metrics/dashboards/logs stack on
  THREE VLAN-20 LXC guests (Prometheus+Alertmanager, Grafana, Loki) at
  Host_Index_Authority indices 8/9/10 (`10.0.20.18`/`.19`/`.20`). Prometheus with a
  30-day local TSDB + `file_sd`-generated scrape targets; Grafana fronted by Traefik;
  Loki + Promtail shipping fleet-wide logs; node_exporter/cAdvisor fleet exporters. The
  v1 scrape set wires up the four already-built services (SVC-01/06/07/09) at `up==1`.
  Ships three PERMANENT default-off mock stand-ins (Grafana OIDC until a client is
  registered in SVC-06; self-signed TLS until the SVC-08 CA; GPU `dcgm-exporter` until
  an SVC-23 GPU host) that fail closed when off. Own Terraform root +
  `svc-17-18-bootstrap.yml` 4-phase orchestrator + `svc-17-18-health.yml` +
  `INSTALL-RUNBOOK.md`. Live-validated on the throwaway cluster.
- **ADR-0017**
  (`.kiro/decisions/0017-svc-17-18-three-lxc-split-and-mock-flip-convention.md`): the
  three-LXC split (not the catalog's one-VM) honoring the host-index ledger, the
  per-slot svc_code split (svc17 prometheus/grafana, svc18 loki), and the three
  default-off mock-to-real flip convention.
- **Cross-guest Traefik ingress via file-provider static routes** (specs
  `traefik-cross-guest-ingress`, `svc-07-cross-guest-ingress`,
  `svc-06-cross-guest-ingress`, ADR-0018): each HTTP backend on a SEPARATE guest from
  Traefik now renders its own Route_Fragment (one `http.routers` + one `http.services`
  pointing at the backend's VLAN-20 `IP:port`) and delegate-copies it onto the Traefik
  guest's file-provider dir — solving cross-guest discovery (a host-local `edge` Docker
  network cannot span guests) WITHOUT Docker Swarm (rejected: violates the Compose-only
  constitution). SVC-07 OpenBao (verified https backend) and SVC-06 ZITADEL (h2c
  backend) converted to this mechanism, completing the http/https/h2c three-scheme set.
  `integration-boundaries.md` §1 clarified: a VLAN-20-internal port bound to the
  backend's own VLAN-20 IP and consumed only by Traefik is NOT a second ingress.
  Live-validated.
- **ADR-0018**
  (`.kiro/decisions/0018-traefik-cross-guest-ingress-via-file-provider-static-routes.md`):
  the file-provider static-route mechanism + the §1 clarification; Docker Swarm overlay
  rejected.
- **All-services provision walkthrough** (spec `all-services-provision-walkthrough`): a
  single authoritative dependency-ordered from-scratch bring-up + reverse-order
  teardown of every implemented service in `docs/TESTING.md` (platform-foundation →
  SVC-07 → SVC-09 → SVC-06 incl. its backing SVC-01 → SVC-17/18), each step
  tier-annotated and linking its INSTALL-RUNBOOK, proven live on the shrimp throwaway
  cluster. Documents the two default-off dev mocks required pre-SVC-06/pre-SVC-08 and
  the OpenBao-admin-token resolution. The README roadmap is reconciled with the box,
  and the walkthrough is cross-linked from the SVC-01/06/09/17-18 runbooks.

### Changed
- **`integration-boundaries.md` §1** clarified (not rewritten) per ADR-0018: a
  VLAN-20-internal backend port bound to the backend's own VLAN-20 IP and consumed only
  by Traefik is the same private-VLAN-bound-port exception the fleet exporters and Loki
  already use — Traefik stays the sole LAN-facing ingress.
- **SVC-01 / SVC-17/18 OpenBao admin-token resolution** now resolves
  `OPENBAO_ADMIN_TOKEN` process-env → cluster-env-file (so the one-command bring-up
  works without a manual export); SVC-06 still reads it from the process env only.
  `docs/TESTING.md`'s prerequisite + the SVC-01 install runbook are updated to the
  accurate per-service picture.

### Fixed
- **SVC-01 cold-first-run DB-install diagnosability** (spec
  `fix-svc01-db-install-first-run-transient`): the investigation DISPROVED a suspected
  cold-start "transient" (a clean sanctioned-path run converges first-try); the real
  defect was that a wired-but-empty `OPENBAO_ADMIN_TOKEN` failed inside a `no_log`
  Phase-3 step with a generic censored error. Fixed with a fail-fast preflight gate
  (names the var + remedy, secret-free) + a cluster-env-file token fallback, plus a
  fail-fast on an empty `project_slug`. Live-validated; offline guard test added.
- **SVC-17/18 scrape-target multi-inventory discovery + `__param_format` template bug**
  (spec `fix-obs-scrape-targets-multi-inventory`): the configurator now merges every
  generated inventory as SEPARATE `-i` flags so the V1 scrape-set service_role groups
  are visible. Live-validated.
- **SVC-17/18 AC-3 V1_Scrape_Set `up==1`** (spec `fix-obs-ac3-targets-up`): the four
  built services scraped at `up==1` (traefik + zitadel + openbao TLS). Live-validated.
- **SVC-17/18 one-command bring-up OpenBao admin-token bridging** (spec
  `fix-obs-bootstrap-first-run-transient`): the one-command flow bridges
  `OPENBAO_ADMIN_TOKEN` from the cluster env file (previously the child received an
  empty token → delegated `bao kv put` 403). Live-validated.
- **SVC-07 OpenBao `:8200` metrics port-publish** (spec
  `fix-svc07-openbao-metrics-port-publish`): decoupled the metrics port-publish from
  the edge path (AC-3 4/4). Live-validated.
- **SVC-06 org-provision `delegate_to` crash + homelab guest-run path wiring** (spec
  `fix-svc06-org-provision-delegate-to-crash`): fixed. Live-validated.

### Known limitations
- SVC-17/18's Grafana OIDC (SVC-06 client), real TLS (SVC-08 CA), and GPU metrics
  (SVC-23) remain default-off mocks until those dependencies land — by design,
  fail-closed when off.
- SVC-06 still reads `OPENBAO_ADMIN_TOKEN` from the process env only (no
  cluster-env-file fallback yet); its from-scratch bring-up requires the token
  exported.
- Onboarding a sample project (dronefleet/VLAN 100) with a standalone per-project
  SVC-01 is deferred to a future spec (no existing root wires a per-project VNet);
  SVC-01 is validated here via the ZITADEL backing DB.

## [0.9.0] - 2026-10-22

The first substantive feature release since v0.7.x: it stands up **three new
implemented services** — **SVC-09 (Traefik reverse proxy)**, **SVC-01 (PostgreSQL)**,
and **SVC-06 (ZITADEL identity provider)** — adds an **automated offline CI
test/lint gate**, and completes the SVC-06 bring-up end-to-end so a from-scratch
install onboards its first org with **no human-minted token**
(`svc06-org-provision-token-wiring`, ADR-0016). Three correctness fixes surfaced
by the all-services from-scratch validation also land
(`fix-vlan-host-index-collision` / ADR-0015,
`fix-svc07-clean-slate-terraform-data-residue`,
`fix-svc06-zitadel-db-app-password-mismatch`). SVC-07 and the platform
prerequisites were already released; this release brings the implemented-services
set to SVC-01/06/07/09 + platform prerequisites. Minor version bump: all additive
(new services, new CI gate, new feature) plus fixes — no breaking changes.

### Added
- **SVC-09 — Reverse proxy (Traefik)** (spec `svc-09-reverse-proxy`, ADR-0013): the
  shared HTTP/HTTPS ingress + TLS-termination edge on one LXC — a `traefik_install`
  role with a static `traefik.yml` (web/websecure entrypoints, internal metrics,
  `exposedByDefault: false`, read-only Docker socket, HTTP→HTTPS redirect), Docker-label
  backend auto-discovery, a persistent `acme` volume, and ownership of the real
  external `edge` Docker network. Ships two PERMANENT default-off mock stand-ins for
  its not-yet-built dependencies — self-signed TLS (SVC-08 CA) and a dashboard with
  forward-auth disabled (SVC-06 SSO) — that fail closed when off and flip to the real
  path once the dependency lands. Terraform root `infra/projects/svc-09-reverse-proxy/`
  (own state, `prevent_destroy` acme seatbelt) + `INSTALL-RUNBOOK.md`. Live-validated.
- **SVC-01 — SQL database (PostgreSQL)** (spec `svc-01-sql-database`): a
  dedicated-per-project `postgres:17` + `postgres_exporter` stack, delivered as a
  REUSABLE Terraform root (per-slug state) and a `postgres-server` Ansible role behind
  a thin `svc-01-bootstrap.yml` 4-phase orchestrator parameterized by `project_slug`.
  Least-privilege per-project app role, per-flag pgvector/PostGIS/TimescaleDB, nightly
  `pg_dump` backup, and all credentials stored in OpenBao. A `mariadb-server` skeleton
  is selectable via `db_engine` (strict-EU alternative). `INSTALL-RUNBOOK.md`.
  Live-validated on the throwaway cluster.
- **SVC-06 — Identity provider (ZITADEL)** (spec `svc-06-identity-provider`,
  ADR-0014): the shared OIDC/SAML SSO backbone — one ZITADEL LXC (image `v4.18.0`) on
  VLAN 20 fronted by Traefik, backed by its OWN dedicated SVC-01 PostgreSQL instance;
  masterkey + OIDC client secrets held only in OpenBao; idempotent per-project
  org/OIDC-app onboarding via a pure-Python management-API helper. Ships two PERMANENT
  default-off mocks (self-signed TLS, placeholder `edge` network) for dev/offline
  targets. Stood up end-to-end on the throwaway cluster (OIDC discovery HTTP 200,
  issuer `https://auth.platform.example.test`). `INSTALL-RUNBOOK.md`.
- **Automated offline CI test/lint gate** (spec `ci-offline-test-pipeline`): a
  `.gitlab-ci.yml` that runs the offline verification as a BLOCKING gate on every MR
  to `main` (and on `main` after merge) — the four pytest roots as SEPARATE invocations
  (same-named `conftest.py` collision), `yamllint`, `ansible-playbook --syntax-check`,
  and `terraform fmt -check` + `validate` (`-backend=false`, never touching remote
  state). Offline by construction (`pytest.ini` `-m "not requires_infra"`); the
  live/`requires-infra` tier is excluded. The two branch-context meta-tests
  (`scope_guard`, `no_pre_existing_test_fails`) are deselected only on MR/feature
  pipelines and run in full on `main`. Toolchain pinned (Python 3.12 + `requirements.txt`,
  Ansible + Galaxy collections, pinned Terraform). This is the automated realization of
  the offline gate that `testing-strategy.md`'s trip-wire flagged as warranted.
- **SVC-06 zero-human-token org-provision** (spec `svc06-org-provision-token-wiring`,
  ADR-0016): ZITADEL seeds a dedicated `provision-sa` machine user and emits its
  PAT at first-init; `zitadel-server` stores it in OpenBao
  (`secret/proj-shared-zitadel/zitadel` field `provision_token`,
  gen-once/read-back mirroring the masterkey); `zitadel-org-provision` reads it
  from OpenBao by default, with the `ZITADEL_PROVISION_TOKEN` env override
  preserved for manual/CI. A from-scratch bring-up onboards the first org + OIDC
  app with no human-minted token; an idempotent re-run never re-mints. New
  `infra/tests/test_svc06_provision_token_wiring.py`. Live-validated on the
  throwaway cluster.
- **ADR-0016**
  `.kiro/decisions/0016-zitadel-provisioning-token-is-instance-scoped-by-necessity.md`
  (+ project-constitution Decision Log row): the provisioning PAT is
  `IAM_OWNER`-scoped by ZITADEL necessity — creating a new org is instance-level,
  no narrower grant exists, and FirstInstance has no narrower option. Blast radius
  bounded by a dedicated non-human machine user + a bounded `Pat.ExpirationDate` +
  OpenBao-only storage + `no_log` + emit-file scrub.
- **Implemented-services index + CHANGELOG** updated for the newly-shipped
  SVC-01/06/09 (and the token-wiring feature): the `docs/README.md` index lists all
  four implemented services, each linking its `INSTALL-RUNBOOK.md` and spec per
  `structure.md` (the root README already listed them).
- **Per-VLAN host-index ledger** `infra/platform-foundation/host-indices.yaml`
  (ADR-0015) documented in the platform-foundation README.

### Changed
- **`scripts/installer/zitadel_provision.py`** — switched to the ZITADEL v4
  organization API paths (`/v2/organizations`, `/v2/organizations/_search`; the old
  `/management/v1/orgs` returns 404 on v4.18.0), plus non-secret
  `--host-header`/`--insecure` dev-transport knobs (the access token still travels
  only in the child-process env, never argv). The production Traefik path is
  unchanged.
- **Documentation** — the `docs/README.md` implemented-services index is completed
  and its topology diagram IP corrected (ZITADEL `10.0.20.15`, not `.10`);
  `docs/TESTING.md` "where to go deeper" extended to the SVC-01/06/09 runbooks plus
  a zero-human-token note; the root README SVC-06 row notes the no-human-token
  onboarding.

### Fixed
- **SVC-06 ZITADEL backing-DB SASL auth (28P01)** (spec
  `fix-svc06-zitadel-db-app-password-mismatch`): `svc06_openbao_addr` defaulted to
  `""`, disabling the whole OpenBao credential handoff while SVC-01 still set a
  random `shared_zitadel_app` role password — ZITADEL then presented an empty
  password and crash-looped. Fixed by defaulting `svc06_openbao_addr` to the
  OpenBao primary `10.0.20.10` at all three layers, plus a fail-fast in
  `zitadel-server` when OpenBao is wired but the password read is empty.
  Live-validated.
- **Service clean-slate left `terraform_data` residue in state** (spec
  `fix-svc07-clean-slate-terraform-data-residue`): a reported-clean service
  clean-slate left non-guest `terraform_data` resources (the host-index
  ledger-check + `input_guards` anchors) in Terraform state, so the platform
  halt-before-fabric check correctly refused teardown and forced a manual
  `terraform state rm`. Fixed with a provider-free full-state sweep + a tightened
  empty-baseline gate across the svc07/svc09/svc01/svc06 clean-slate roles.
  Live-validated.
- **VLAN-20 host-index/IP collision** (spec `fix-vlan-host-index-collision`,
  ADR-0015): SVC-07's OpenBao primary and SVC-09's Traefik both defaulted to
  `host_index = 0` → both `10.0.20.10`, so Traefik was unreachable when
  co-deployed. Fixed with a committed per-VLAN host-index ledger
  (`host-indices.yaml`) feeding a `proxmox-compute` plan-time uniqueness
  precondition; SVC-09 moved to `host_index = 2` (`10.0.20.12`). General to every
  multi-root-per-VLAN subnet. Live-validated: all five VLAN-20 guests on distinct
  IPs.

### Known limitations
- The SVC-06 provisioning token is `IAM_OWNER`-scoped by ZITADEL necessity
  (ADR-0016) — a leaked token is instance-admin-capable until its PAT expiry;
  mitigated by bounded expiry (operator-shortenable), OpenBao-only storage, and a
  dedicated non-human identity.

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

[0.11.0]: https://git.xenopz.com/xenopzsd_internal/devinfra/-/compare/v0.10.0...v0.11.0
[0.10.0]: https://git.xenopz.com/xenopzsd_internal/devinfra/-/compare/v0.9.0...v0.10.0
[0.9.0]: https://git.xenopz.com/xenopzsd_internal/devinfra/-/compare/v0.8.0...v0.9.0
[0.2.0]: https://git.xenopz.com/xenopzsd_internal/devinfra/-/compare/v0.1.0...v0.2.0
[0.1.0]: https://git.xenopz.com/xenopzsd_internal/devinfra/-/releases/v0.1.0
