# SVC-06 Install Runbook — provision ZITADEL (backing DB + IdP) from nothing, then test it

This is the **provision-then-test spine** for SVC-06 (the shared ZITADEL identity
provider): it takes you from an empty throwaway Proxmox cluster to a running
ZITADEL — backed by its own dedicated PostgreSQL (an SVC-01 instance) and fronted
by Traefik — then verifies the OIDC discovery endpoint against the instance you
just built.

> To stand this service up as part of the whole platform (dependency-ordered, from
> an empty cluster), see the [all-services walkthrough](../../../docs/TESTING.md#deploy-every-implemented-service-from-scratch-all-services-walkthrough)
> in the testing guide.

Section **A** is the **Developer B** fast path (one Ansible command). Sections
**0–4** are the **Developer A** manual spine.

> **Placeholders only.** The `ZITADEL_MASTERKEY`, the backing-DB password, and
> every OIDC client secret are generated/stored in OpenBao
> (`secret/proj-shared-zitadel/...`) and never committed or echoed.

> **Doctest tiers.** Offline preflight carries `<!-- doctest: offline -->`; the
> apply/install/health/teardown carry `<!-- doctest: requires-infra -->`. Tier
> rules: [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md);
> runbook location: [`structure.md`](../../../.kiro/steering/structure.md).

---

## ⚠ Throwaway cluster only

The live steps provision, mutate, and tear down real infrastructure. Run them
**only against a throwaway / ephemeral Proxmox cluster**. Section 4's teardown
`terraform destroy`s the ZITADEL LXC **and** orchestrates the backing DB's own
clean-slate (its data volume).

---

## Prerequisites

- The shared VLAN-20 SDN VNet `p20` must exist (created by the platform-foundation
  layer). SVC-06 Preflight fails closed naming it if absent.
- **SVC-01** (the reusable Postgres root/role) must be present — SVC-06 stands up
  its backing DB by running `svc-01-bootstrap.yml` as a child.
- **SVC-09 (Traefik)** is the production HTTP ingress. When it is **not** on the
  target (dev/offline), use the two default-off mocks (below) so ZITADEL still
  serves HTTPS + attaches an `edge` network:
  - `-e zitadel_tls_self_signed=true` — self-signed cert (control-node generated).
  - `-e zitadel_mock_edge_network=true` — placeholder `edge` network.
  Both are **OFF** in acceptance/production (Traefik owns edge + real TLS). See
  [`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md) Mock 5 + Mock 6.

---

## A. Developer-B automated path — one command, provision-then-test

### A.0 — Offline preflight (no cluster needed)

Validate the ZITADEL Terraform root:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/svc-06-identity-provider
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

Parse-check the orchestrator + helper and run the SVC-06 offline suite:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml ansible/playbooks/svc-06-bootstrap.yml
~/venv/devinfra/bin/python -c "import ast; ast.parse(open('scripts/installer/zitadel_provision.py').read())"
~/venv/devinfra/bin/pytest infra/tests/test_svc06_terraform_shape.py \
  infra/tests/test_svc06_zitadel_contract.py -q
~/venv/devinfra/bin/pytest tests/installer/test_svc06_backing_db.py \
  tests/installer/test_svc06_mocks.py tests/installer/test_zitadel_provision.py \
  tests/installer/test_svc06_thin_orchestrator_play.py \
  tests/installer/test_svc06_swap_gate.py tests/installer/test_svc06_secret_hygiene.py -q
~/venv/devinfra/bin/pytest infra/tests/test_svc06_provision_token_wiring.py -q
```

### A.1 — One-command install (backing DB → ZITADEL → org → health)

Stand ZITADEL up end-to-end (pick a free VMID for the ZITADEL guest; the backing
DB VMID defaults to 1102). In dev/offline (no Traefik), add the two mock flags.
**No `ZITADEL_PROVISION_TOKEN` is required** — the provisioning credential is
auto-minted at first-init and stored in OpenBao, and the org-onboarding step
reads it from there (ADR-0016):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-06-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env \
  -e svc06_zitadel_vmid=1103 \
  -e zitadel_tls_self_signed=true -e zitadel_mock_edge_network=true
```

The orchestrator runs Preflight (cluster.env, Proxmox auth, `p20` gate, datastore
autodetect), Provisioning (a **child `svc-01-bootstrap`** that stands up the
backing DB `proj-shared-zitadel` with its own isolated state, then
`terraform apply` of the ZITADEL LXC), Bootstrap (the `common` role, then
`zitadel.yml` — the ZITADEL stack with masterkey + DB creds from OpenBao — then
`zitadel-org-provision` to onboard a first org + OIDC app), and Handoff
(`svc06-health.yml` OIDC-discovery gate + banner).

> **Credential handoff is wired by default.** `svc06_openbao_addr` defaults to
> the SVC-07 OpenBao primary's VLAN-20 address (`10.0.20.10`), so a standard
> bring-up points BOTH the backing-DB child (`openbao_addr`, SVC-01's write gate)
> and the ZITADEL run (`zitadel_openbao_addr`, ZITADEL's read gate) at the SAME
> OpenBao source. SVC-01 therefore STORES the `shared_zitadel_app` password it
> ALTERs onto the role at `secret/proj-shared-zitadel/postgres` (field
> `app_password`), and ZITADEL READS that identical password — the three chain
> points agree and ZITADEL authenticates. `OPENBAO_ADMIN_TOKEN` must be present
> in the environment so both child runs can reach OpenBao. If OpenBao is wired
> but the read comes back empty, the `zitadel-server` role now **fails fast**
> naming that KV path+field rather than crash-looping on SQLSTATE 28P01. Override
> `svc06_openbao_addr=""` ONLY for a deliberate dev/offline run (then also supply
> an explicit `zitadel_db_password_override`). Spec:
> [`fix-svc06-zitadel-db-app-password-mismatch`](../../../.kiro/specs/fix-svc06-zitadel-db-app-password-mismatch/).

### A.2 — Onboard a project to SSO (Day-2, idempotent)

> **No human-minted token needed.** The ZITADEL management-API provisioning
> credential is obtained **automatically at first-init** and stored in OpenBao
> (spec [`svc06-org-provision-token-wiring`](../../../.kiro/specs/svc06-org-provision-token-wiring/),
> ADR-0016). During the ZITADEL stack's `start-from-init`, ZITADEL's
> `FirstInstance` config seeds a dedicated non-human machine user
> (`provision-sa`, `IAM_OWNER` by ZITADEL necessity for org-creation) with a
> bounded-expiry PAT and writes that PAT to a mounted emit file. The
> `zitadel-server` role reads it once, persists it to
> `secret/proj-shared-zitadel/zitadel` field `provision_token`, and scrubs the
> on-guest file. On a re-run the stored token is read back (never re-minted).
> The `zitadel-org-provision` role reads `provision_token` from OpenBao by
> default — so onboarding needs **no `ZITADEL_PROVISION_TOKEN`** in the env.

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
# Default path: no ZITADEL_PROVISION_TOKEN needed — the role reads the
# auto-minted token from OpenBao. OPENBAO_ADMIN_TOKEN must be in the env.
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel-org-provision.yml -e zop_project_slug=dronefleet
```

Creates org `proj-dronefleet`, default roles, and the `dronefleet-webapp` OIDC
app; a re-run is a no-op success (already-exists). The OIDC client secret is
written to OpenBao at `secret/proj-dronefleet/zitadel/oidc/dronefleet-webapp`.

**Manual / CI override.** To supply the provisioning token out-of-band instead
of reading it from OpenBao (e.g. a locked-down deployment, or a CI job with the
token in a masked variable), export `ZITADEL_PROVISION_TOKEN` — a non-empty env
value takes precedence over the OpenBao-stored token. It is never committed,
never passed on the argv, and `no_log` throughout:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
# Override path: a non-empty env token wins over the OpenBao value.
export ZITADEL_PROVISION_TOKEN='<service-account-PAT>'
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel-org-provision.yml -e zop_project_slug=dronefleet
```

### A.3 — Verify OIDC discovery resolves

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/svc-06-identity-provider.generated.yml \
  ansible/playbooks/svc06-health.yml
```

### A.4 — Clean-slate reset (ZITADEL guest + backing DB)

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-06-bootstrap.yml \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e svc06_zitadel_vmid=1103 -e svc06_proxmox_host=<real-proxmox-lan-ip>
```

Pct-destroys the ZITADEL LXC and orchestrates the backing DB's own clean-slate
(child `svc-01-bootstrap -e clean_slate=true`), then verifies an empty baseline
over both guests (`pct list` + `terraform state list`).

---

## A.5 — ZITADEL reachable through Traefik (cross-guest file-provider route)

This is the provision-then-test step for the **cross-guest ingress** behavior
introduced by the
[`traefik-cross-guest-ingress`](../../../.kiro/specs/traefik-cross-guest-ingress/)
spec and applied to ZITADEL by its SVC-06 follow-up
[`svc-06-cross-guest-ingress`](../../../.kiro/specs/svc-06-cross-guest-ingress/),
recorded in
[`ADR-0018`](../../../.kiro/decisions/0018-traefik-cross-guest-ingress-via-file-provider-static-routes.md)
(plus its SVC-06 `h2c` addendum). It closes the SVC-06 case that carried the
**same latent cross-guest discovery gap** as Grafana and OpenBao: the ZITADEL
server runs on its own guest `svc06-20-01` · `10.0.20.15`, Traefik on a
**separate** guest `svc09-20-01` · `10.0.20.12`, so a host-local `edge` Docker
network can never bridge them and Traefik never discovers ZITADEL's labels.

**How the real (default) path works.** The `zitadel-server` role, on the default
production path (`zitadel_mock_edge_network=false`):

1. **Publishes the ZITADEL VLAN20_Port** — binds the HTTP port `8080` to the
   guest's **own VLAN-20 IP** (`10.0.20.15:8080`), never `0.0.0.0` and never a LAN
   port. Reachable only on VLAN 20, consumed only by Traefik — the same
   private-VLAN-bound-port exception the OpenBao primary, the SVC-07 unsealer seal
   path, the fleet exporters (`:9100`/`:8080`) and Loki (`:3100`) already use
   (ADR-0018).
2. **Delegates a Traefik file-provider Route_Fragment** (`zitadel-route.yml`, one
   `http.routers` + one `http.services` entry pointing at `h2c://10.0.20.15:8080`)
   onto the Traefik_Guest's watched dynamic-config dir via the shared
   `traefik_route` role. **No SVC-09 `traefik_install` edit**, no cross-guest
   `edge` network, no orchestrator/overlay.

ZITADEL is the **simplest** of the three ADR-0018 backend schemes (`http` Grafana
/ `https` OpenBao / `h2c` ZITADEL). It serves **cleartext h2c** (HTTP/2 cleartext,
`ZITADEL_TLS_ENABLED=false`), so the Traefik→ZITADEL hop has **no backend TLS and
no certificate**. The `h2c://` scheme rides through the file-provider server `url`
verbatim — no new Route_Mechanism field — and because the hop is cleartext the
route carries **NO `serversTransport` block and NO backend cert** (unlike
OpenBao's verified `https` hop). TLS still terminates at Traefik on the
`websecure` entrypoint for the LAN-facing hop. Traefik hot-reloads the fragment
(watched file provider, no restart) and proxies
`https://auth.<platform-domain>/` to the ZITADEL VLAN20_Port over cleartext h2c on
VLAN 20.

> **Mock-edge is a dev/offline affordance only.** `zitadel_mock_edge_network=true`
> flips ZITADEL onto a **co-located-Traefik** path (attach the local `edge`
> network + emit the six Traefik discovery labels — including the
> `loadbalancer.server.scheme=h2c` label — **no** VLAN20_Port, **no** file-provider
> route). It is a permanent, default-off placeholder (Mock 6) per
> [`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md) and must
> **never** be enabled on a host where SVC-09 owns the real `edge` (ADR-0013). The
> separate `zitadel_tls_self_signed` (Mock 5) is an unrelated dev/offline concern
> (ZITADEL serving HTTPS itself when Traefik is absent) and is **not** part of the
> cross-guest backend hop. The real cross-guest route above is the default
> production posture.

> **PREREQUISITE — the SVC-01 `shared-zitadel` backing DB must be standing
> FIRST.** ZITADEL does **not** start or stay healthy without its dedicated
> PostgreSQL backing database (an SVC-01 instance, `shared-zitadel`, at
> `10.0.20.14`). Before the ZITADEL bring-up below, stand that DB up through its
> **own** installer — the SVC-01 bootstrap, which the A.1 `svc-06-bootstrap.yml`
> orchestrator already invokes as a child `svc-01-bootstrap` run (see section 1 of
> the Developer-A spine). Never hand-create the DB; drive it through its own
> automated path so the next run is reproducible. A missing or broken backing DB
> surfaces as a ZITADEL crash/health failure (hence a Traefik `502`/`504` for the
> route), not a route-config failure.

### A.5.0 — Offline preflight (render the route, no cluster needed)

Render-check the ZITADEL Route_Fragment (the `h2c://` backend URL verbatim, with
**no** `serversTransport` — the default-inert path) and the VLAN20_Port bind /
swap-gate structure. This runs offline against the role templates/tasks (no
Traefik, no cluster). Run from the `tests/installer` pytest root as its own
invocation (its `conftest.py` collides with the `infra/tests` root if collected
together — [`testing-strategy.md`](../../../.kiro/steering/testing-strategy.md) →
"Offline gate"):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest tests/installer/test_svc06_cross_guest.py -q
```

Compile-check the ZITADEL configurator play that drives the delegating role and
the VLAN20_Port publish (no connect):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel.yml
```

Format-check and validate the SVC-06 Terraform root (guest placement is unchanged
by this feature, but the offline TF gate is part of the provision-then-test
preflight — see [**A.0**](#a0--offline-preflight-no-cluster-needed) for the
`-backend=false` rationale):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/svc-06-identity-provider
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

### A.5.1 — Live check: ZITADEL through Traefik over h2c (requires-infra)

With the **SVC-01 `shared-zitadel` backing DB standing first** (prerequisite
above) and the A.1 bring-up run (ZITADEL publishing `10.0.20.15:8080` and
delegating `zitadel-route.yml` onto the Traefik_Guest's file-provider dir, Traefik
and platform-foundation standing), confirm the cross-guest route end-to-end. First
re-run the A.1 orchestrator against your throwaway cluster env-file so the
real-path ingress wiring is in place (substitute `<cluster-env-file>`, e.g.
`cluster.dev.env`):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-06-bootstrap.yml \
  -e cluster_env_file=<cluster-env-file> \
  -e svc06_zitadel_vmid=1103
```

Then verify a LAN request through Traefik reaches the ZITADEL server over the
VLAN-20 `h2c` hop. The OIDC discovery document
(`/.well-known/openid-configuration`) is **unauthenticated** (no token needed) and
returns ZITADEL's discovery JSON with HTTP 200, proxied to `h2c://10.0.20.15:8080`
over VLAN 20 — the **first `h2c` backend proven through the Route_Mechanism live**.
`--resolve` points the public `Host()` name at the Traefik_Guest
(`<traefik-guest-ip>`, `10.0.20.12`); `-k` tolerates the LAN-facing self-signed
cert when no real CA is present (the inner Traefik→ZITADEL hop is cleartext h2c, so
there is no backend cert to verify). Substitute `<platform-domain>` for your
deployment's base domain:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
curl -sk --resolve auth.<platform-domain>:443:<traefik-guest-ip> \
  https://auth.<platform-domain>/.well-known/openid-configuration
```

A `200` with the OIDC discovery JSON proves discovery is solved via the file
provider (not a host-local `edge`): Traefik learned the
`auth.<platform-domain> → h2c://10.0.20.15:8080` mapping from the delegated
Route_Fragment and reached the backend over VLAN 20 over the cleartext h2c hop. A
re-run of the A.1 bring-up is idempotent for the route (`changed=0` — the
byte-identical fragment is not re-written); the A.4 clean-slate removes the
delegated `zitadel-route.yml` from the Traefik_Guest so a torn-down ZITADEL leaves
no dangling route pointing at a freed VLAN-20 IP.

---

## 0–4. Developer-A manual reference spine

- **0. Fabric gate.** Confirm the shared VNet `p20` exists.
- **1. Backing DB.** Run `svc-01-bootstrap.yml` with
  `-e project_slug=shared-zitadel -e vlan_id=20 -e svc01_vmid=1102 -e host_index=4
  -e db_engine=postgres` → PostgreSQL `shared_zitadel` + role `shared_zitadel_app`,
  state `shared-zitadel-svc01-postgres`.
- **2. Provision ZITADEL.** `terraform apply` the SVC-06 root with
  `-var zitadel_vmid=1103 -var zitadel_host_index=5`. `prevent_destroy` guards the
  masterkey-stateful LXC.
- **3. Configure.** `common` installs Docker + Compose; `zitadel.yml` runs the
  `zitadel-server` role (masterkey generated-once + DB creds from OpenBao, Traefik
  labels, `ZITADEL_EXTERNAL*`/TLS matching the public endpoint, metrics). The
  backing-DB `app_password` is read from `secret/proj-shared-zitadel/postgres`
  (the SAME OpenBao source SVC-01's child write used, via the shared
  `svc06_openbao_addr` default `10.0.20.10`); a wired-but-empty read **fails fast**
  naming the KV path+field instead of presenting an empty password (SQLSTATE
  28P01).
- **4. Onboard + health.** The `zitadel-server` run also obtains the provisioning
  PAT ZITADEL emits at first-init and stores it at
  `secret/proj-shared-zitadel/zitadel` (field `provision_token`), scrubbing the
  on-guest emit file; `zitadel-org-provision` then reads that token from OpenBao
  (no human token needed) and creates the first org/OIDC app; `svc06-health.yml`
  gates on the container Running + OIDC discovery 200.

## Security notes

- HTTP ingress is via **Traefik labels only** — ZITADEL publishes **no host
  port**; Traefik terminates TLS (or the self-signed mock in dev).
- The `ZITADEL_MASTERKEY` is generated **once** and **never rotated** (a change
  requires a full data re-encryption); it lives only in OpenBao.
- The backing DB is a least-privilege SVC-01 instance with its own isolated state.
- The ZITADEL mgmt-API provisioning token is **auto-minted at first-init** as a
  dedicated non-human machine user's bounded-expiry PAT (`IAM_OWNER` by ZITADEL
  necessity for org-creation, ADR-0016), stored **only** in OpenBao at
  `secret/proj-shared-zitadel/zitadel` (field `provision_token`), `no_log`
  throughout, with the first-init emit file scrubbed after capture. The optional
  `ZITADEL_PROVISION_TOKEN` env override (manual/CI) and the OpenBao admin token
  are read from the environment, never a CLI flag or committed file.
- MFA (TOTP) is enforceable via org login policy — the single most important
  control, since a compromised ZITADEL admin compromises every OIDC consumer.
