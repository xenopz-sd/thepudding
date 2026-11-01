# SVC01_Consumer_Root Install Runbook — provision a per-project DB from nothing, then test it

This is the **provision-then-test spine** for the per-project SVC-01 consumer
database (the [`db/`](db) root of a Consumer_Project). It takes you from an empty
throwaway Proxmox cluster to a running PostgreSQL on the project's **own** VLAN
(100+), then verifies it against the instance you just built. This is the first
per-project-VLAN SVC-01 consumer path (SVC-01 previously ran only as the shared
ZITADEL backing DB on VLAN 20).

- **Spec:** [`.kiro/specs/project-consumer-onboarding/`](../../../.kiro/specs/project-consumer-onboarding/)
- **ADRs:** [ADR-0019](../../../.kiro/decisions/0019-per-project-svc01-consumer-root-pattern.md) (per-project consumer root), [ADR-0020](../../../.kiro/decisions/0020-manifest-driven-consumer-onboarding-orchestrator.md) (orchestrator)
- **Root README:** [`db/README.md`](db/README.md)
- **End-to-end consumer journey:** [`../../../docs/CONSUMER-JOURNEY.md`](../../../docs/CONSUMER-JOURNEY.md)

> `dronefleet` is the scaffold/test slug. For a real project, copy
> `infra/projects/dronefleet/db` to `infra/projects/<your-slug>/db`, add the
> project to the Registry + host-index ledger, and substitute `<slug>` below.

> **Placeholders only.** DB passwords are generated at provision, stored in
> OpenBao (`secret/proj-<slug>/postgres`), and never committed or echoed. OIDC
> client secrets are OpenBao-mediated the same way.

> **Doctest tiers.** Offline preflight carries `<!-- doctest: offline -->`; the
> apply / bootstrap / health / test / teardown carry
> `<!-- doctest: requires-infra -->`. Tier rules:
> [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md);
> runbook location: [`structure.md`](../../../.kiro/steering/structure.md).

---

## ⚠ Throwaway cluster only

The live steps provision, mutate, and tear down real infrastructure. Run them
**only** against a throwaway / ephemeral Proxmox cluster listed in
[`scripts/installer/throwaway-clusters.yml`](../../../scripts/installer/throwaway-clusters.yml),
with the `AGENT_LIVE_AUTHORIZED` interlock armed and the fail-closed throwaway
guard authorizing — **never** in CI, acceptance, or production
([testing-strategy.md "Agent Live-Test Authorization"](../../../.kiro/steering/testing-strategy.md)).

Any dependency-not-under-test (OpenBao for the secret writes) is stood
up/reset/re-bootstrapped **only** via its own `svc-NN-bootstrap.yml` automated
path — never raw `ssh`/`docker`/`pct` surgery.

---

## Prerequisites

1. The project slug is recorded in the Registry
   [`infra/platform-foundation/projects.yaml`](../../platform-foundation/projects.yaml)
   (`dronefleet -> vlan_id 100`), assigned by the onboarding script + MR flow.
2. The project's **network slice** (`<slug>-infra` root — the `_TEMPLATE` copy at
   `infra/projects/<slug>/`) has been applied, so the project VNet `p<vlan>`
   exists. The SVC-01 preflight fails closed naming the missing `p<vlan>` if not.
3. The project's **host-index ledger row** is present in
   [`infra/platform-foundation/host-indices.yaml`](../../platform-foundation/host-indices.yaml)
   (`VLAN 100: svc01 host_index 0`). A missing row fails the plan at the
   `proxmox-compute` Guard 4 precondition (Req 3.7) rather than defaulting.
4. The project venv exists: `~/venv/devinfra/bin/python` (dev-workflow.md).

---

## Part 1 — Offline preflight (no cluster needed, Tier-1)

Validate the SVC01_Consumer_Root Terraform and the manifest with no
infrastructure or credentials:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
terraform -chdir=infra/projects/dronefleet/db fmt -check -recursive
terraform -chdir=infra/projects/dronefleet/db init -backend=false
terraform -chdir=infra/projects/dronefleet/db validate
```

Validate the Consumer_Manifest (side-effect-free):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/python scripts/installer/consumer_manifest.py \
  --check infra/projects/dronefleet/consumer-manifest.yml
```

Resolve the slug's VLAN from the Registry (offline, reads `projects.yaml` only):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/python scripts/installer/consumer_registry.py \
  --resolve dronefleet
```

Parse-check the orchestrator play and run the offline consumer suite:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml ansible/playbooks/project-consumer-bootstrap.yml
~/venv/devinfra/bin/pytest \
  tests/installer/test_consumer_manifest.py \
  tests/installer/test_consumer_registry.py \
  tests/installer/test_consumer_derivation.py \
  tests/installer/test_consumer_swap_gate.py -q
```

---

## Part 2 — Provision the per-project DB from nothing (Tier-2, requires-infra)

### 2.1 — Export the throwaway-cluster credentials (offline; sets env only)

Setting environment variables contacts nothing, so this block is `offline`; the
`terraform`/`ansible` steps that consume it are the `requires-infra` steps.

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
export TF_VAR_proxmox_endpoint="${PROXMOX_SDN_TEST_ENDPOINT}"
export TF_VAR_proxmox_api_token="${PROXMOX_SDN_TEST_TOKEN}"
export TF_VAR_proxmox_insecure="true"   # self-signed throwaway cert
export AGENT_LIVE_AUTHORIZED=1           # throwaway interlock (per-run; never committed)
```

### 2.2 — One-command provision + bootstrap (compose `svc-01-bootstrap.yml`)

The SVC01_Consumer_Root is stood up by the sanctioned consumer orchestrator,
which composes the SVC-01 orchestrator for the DB. `project_slug` is the only
identity you pass — `vlan_id` is resolved from the Registry and `host_index` from
the ledger (never hand-picked). Pick a free `svc01_vmid`.

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/project-consumer-bootstrap.yml \
  -e cluster_env_file=cluster.throwaway.env \
  -e project_slug=dronefleet \
  -e svc01_vmid=1101
```

This runs the four phases: preflight (manifest + Registry + bounded dependency
probes) → provision (the `db/` apply on VLAN 100 + the `postgres-server` role) →
bootstrap (DB + least-priv app role, generated password → OpenBao, the OpenBao
isolation triple + `<slug>-workload` AppRole, the ZITADEL org/SPA/API clients) →
health/handoff (the completion summary).

> Alternatively, to provision **only** the DB layer (not the full consumer
> onboarding), apply the `db/` root directly via the composed SVC-01 path:
>
> <!-- doctest: requires-infra -->
> <!-- cwd: . -->
> ```bash
> ~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
>   ansible/playbooks/svc-01-bootstrap.yml \
>   -e cluster_env_file=cluster.throwaway.env \
>   -e project_slug=dronefleet -e vlan_id=100 -e svc01_vmid=1101 -e tier=small
> ```

### 2.3 — Confirm the DB guest is up on the project VLAN

The DB host IP is derived: `cidrhost("10.0.100.0/24", 10 + 0) = 10.0.100.10`.
A bounded reachability check against the freshly-provisioned guest:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc01-health.yml \
  -e project_slug=dronefleet -e vlan_id=100
```

---

## Part 3 — Run the `requires-infra`-gated tests against what you just built

Wire the test environment to the instance the walkthrough **just created** (not
a pre-existing one), then run the deselected-by-default live suite. Reference
credentials by env-var name only — never paste a secret.

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
# Point the live tests at the instance built in Part 2 (env only — offline).
export CONSUMER_TEST_SLUG=dronefleet
export CONSUMER_TEST_CLUSTER_ENV=cluster.throwaway.env
export CONSUMER_TEST_DB_HOST=10.0.100.10
export CONSUMER_TEST_DB_PORT=5432
export CONSUMER_TEST_ALLOW_MUTATE=1       # arm the apply/destroy tests (throwaway only)
# OPENBAO_ADMIN_TOKEN / OPENBAO_TEST_ADDR / OPENBAO_TEST_TOKEN come from the
# throwaway cluster's gitignored .env — referenced by name, never echoed.
```

Run the live consumer integration suite (opt-in via `-m requires_infra`):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest -m requires_infra \
  tests/installer/test_consumer_onboarding_integration.py -v
```

With no cluster wired these tests **skip cleanly** (never fail) — the
"absent infra ⇒ skipped, not failed" rule. With the throwaway cluster wired they
assert: the DB guest is reachable on VLAN 100 (10.1), the full orchestrator run
wires DB + OpenBao triple + `<slug>-workload` AppRole + ZITADEL clients with the
secrets stored in OpenBao (10.2), the degraded-mode halts name their dependency
(10.3), a converged re-run reports zero changes (10.4), and clean-slate tears to
baseline while preserving the Registry entry + `<slug>-infra` slice (10.5).

---

## Part 4 — Clean-slate teardown (Tier-2, requires-infra)

Return the consumer resources to a clean baseline. This orchestrates the DB's
**own** clean-slate (ADR-0011) and reverses the OpenBao/ZITADEL onboarding via
the onboarders' own paths. It **never** removes the Registry entry or the
`<slug>-infra` network slice (Req 7.7).

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/project-consumer-bootstrap.yml \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e cluster_env_file=cluster.throwaway.env \
  -e project_slug=dronefleet \
  -e svc01_vmid=1101
```

The verification gate asserts zero surviving consumer compute + zero Terraform
state before reporting a clean baseline; a failed destroy or surviving resource
halts without reporting clean (Req 7.4–7.6). After it completes, re-install is
safe (re-run Part 2).

---

## Part 5 — Hello-workload: the last-mile definition-of-done (Tier-2, requires-infra)

Part 2 provisions a per-project database and reports `failed=0`. That is
**onboarded**, not yet **deployable** — the gap this part closes. The
hello-workload smoke is the provision-then-test spine for the consumer *last
mile*: onboard a project **with a workload guest**, stand up the reference
workload via the Agent sidecar, and prove it reaches `/healthz` **healthy only
after reading its `DATABASE_URL` from the Agent-rendered Rendered_Secret_File** —
then prove the **fail-closed** half (Primary sealed ⇒ no secret ⇒ workload never
starts). A green happy-path `/healthz` 200 **is** the onboarding
definition-of-done signal.

- **Spec:** [`.kiro/specs/consumer-last-mile-hello-workload/`](../../../.kiro/specs/consumer-last-mile-hello-workload/)
- **ADR:** [ADR-0021](../../../.kiro/decisions/0021-proxmox-compute-first-class-non-catalog-guest-identity.md) (the `proxmox-compute` non-catalog `role_code = workload` guest identity)
- **Reference workload role:** [`ansible/roles/hello_workload/README.md`](../../../ansible/roles/hello_workload/README.md)
- **Stand-up composition:** [`ansible/playbooks/hello-workload-deploy.yml`](../../../ansible/playbooks/hello-workload-deploy.yml) (composes the existing [`openbao-agent`](../../../ansible/roles/openbao-agent/README.md) role — no new agent wiring)
- **Live smoke (test side):** [`tests/installer/test_hello_workload_smoke.py`](../../../tests/installer/test_hello_workload_smoke.py) (`TestHelloWorkloadLiveSmoke`)

> **Dependency-not-under-test = OpenBao; drive it only through its own
> installer.** This walkthrough SEALS and UNSEALS the OpenBao Primary to exercise
> the fail-closed gate. OpenBao is a dependency of this test, not its subject — so
> it is stood up / sealed / unsealed / reset **exclusively** through SVC-07's own
> [`svc-07-bootstrap.yml`](../../../ansible/playbooks/svc-07-bootstrap.yml)
> automated path (and its documented seal/clean-slate contract — see the
> [SVC-07 runbook](../svc-07-secrets-manager/INSTALL-RUNBOOK.md)), **never** ad-hoc
> `ssh … docker compose down` / `docker volume rm` / `pct` / container `exec`
> surgery (`testing-strategy.md` "Dependencies-not-under-test MUST be driven
> through their own automated path"; `service-installer.md` single-approved-
> orchestrator rule). Raw `ssh`/`docker` against OpenBao here is for **read-only
> diagnosis only** (deciding which automated path to run).

> The **⚠ Throwaway cluster only** preamble at the top of this runbook applies to
> every live step below in full — `AGENT_LIVE_AUTHORIZED` armed + the fail-closed
> throwaway guard authorizing, never CI/acceptance/production. This part is the
> `requires-infra` tier and is **NOT** run in the offline gate.

### Prerequisites (in addition to Part 1's)

- SVC-07 (OpenBao Primary + unsealer) is stood up, initialised, and **unsealed**
  via its own [`svc-07-bootstrap.yml`](../../../ansible/playbooks/svc-07-bootstrap.yml)
  path, reachable on `10.0.20.10` (Primary) per ADR-0004; the `OPENBAO_ADMIN_TOKEN`
  the composed onboarders authenticate with has been minted into the throwaway
  cluster's gitignored env file.
- The dronefleet host-index ledger carries the **workload** row on VLAN 100
  (`role_code: workload`, `host_index: 1` → `10.0.100.11`, ADR-0015 + ADR-0021)
  in [`infra/platform-foundation/host-indices.yaml`](../../platform-foundation/host-indices.yaml);
  a missing row fails the `proxmox-compute` peer-index precondition at plan time
  rather than defaulting.

### 5.1 — Enable the `workload` block in the Consumer_Manifest (offline)

The workload guest is a **strict no-op unless the manifest declares
`workload.enabled: true`** (Req 2.3). The committed `dronefleet` manifest keeps
it disabled; for this walkthrough add the optional block (placeholder values —
reuse the DB sizing tiers):

```yaml
# infra/projects/dronefleet/consumer-manifest.yml  (append this block)
workload:
  enabled: true     # false/absent => no workload guest (identical to pre-feature)
  tier: small       # small | medium
  count: 1          # >=1; a single guest is the validated path
```

Re-validate the manifest offline (side-effect-free — contacts nothing):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/python scripts/installer/consumer_manifest.py \
  --check infra/projects/dronefleet/consumer-manifest.yml
```

Offline-preflight the optional workload Terraform root (no backend, no infra):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
terraform -chdir=infra/projects/dronefleet/workload fmt -check -recursive
terraform -chdir=infra/projects/dronefleet/workload init -backend=false
terraform -chdir=infra/projects/dronefleet/workload validate
```

### 5.2 — Onboard dronefleet WITH the workload guest (requires-infra)

Export the throwaway-cluster credentials (env only — contacts nothing, so
`offline`; the `ansible`/`terraform` steps that consume them are the
`requires-infra` steps):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
export TF_VAR_proxmox_endpoint="${PROXMOX_SDN_TEST_ENDPOINT}"
export TF_VAR_proxmox_api_token="${PROXMOX_SDN_TEST_TOKEN}"
export TF_VAR_proxmox_insecure="true"   # self-signed throwaway cert
export AGENT_LIVE_AUTHORIZED=1           # throwaway interlock (per-run; never committed)
```

Run the sanctioned consumer orchestrator. Because the manifest now declares
`workload.enabled: true`, Phase 2 (`consumer_provision`, task 3.1) applies the
`infra/projects/dronefleet/workload/` root (state `dronefleet-workload`) and
regenerates the inventory to include the workload guest on **VLAN 100,
host_index 1 → `10.0.100.11`** (the ledger row); with the block absent it is a
strict no-op:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/project-consumer-bootstrap.yml \
  -e cluster_env_file=cluster.throwaway.env \
  -e project_slug=dronefleet \
  -e svc01_vmid=1101
```

This wires DB + secrets + the `dronefleet-workload` AppRole, and (workload
enabled) provisions the workload guest. Its IP is derived, never hand-chosen:
`cidrhost("10.0.100.0/24", 10 + 1) = 10.0.100.11`; its hostname is the ADR-0021
role-code form `workload-100-01`.

### 5.3 — Stand up the Agent sidecar + reference workload (requires-infra)

Stand up the reference hello-workload via the thin composition playbook. It
targets the `workload` inventory group by default (the provisioned guest's
`service_role`), composing the existing `openbao-agent` role — no new agent
wiring:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/dronefleet-workload.generated.yml \
  ansible/playbooks/hello-workload-deploy.yml \
  -e project_slug=dronefleet -e hello_workload_enabled=true
```

> **Co-locate override (no dedicated guest).** If you onboarded WITHOUT a
> `workload` block (the co-locate topology), stand the reference up on the DB
> guest instead by pointing the play at the consumer inventory and overriding the
> target group:
>
> <!-- doctest: requires-infra -->
> <!-- cwd: . -->
> ```bash
> ~/venv/devinfra/bin/ansible-playbook \
>   -i ansible/inventory/dronefleet-consumer.generated.yml \
>   ansible/playbooks/hello-workload-deploy.yml \
>   -e project_slug=dronefleet -e hello_workload_target=postgres
> ```

The Agent sidecar authenticates via the `dronefleet-workload` AppRole, renders
`DATABASE_URL` (from `secret/data/dronefleet/db` field `database_url` — the
v0.11.3 bridge) into the 0600 Rendered_Secret_File, becomes `service_healthy`,
and only THEN does the workload container start (the `depends_on … service_healthy`
swap-gate). Ingress is Traefik-only via a `Host()` label — no host port.

### 5.4 — Happy path: `/healthz` healthy having read `DATABASE_URL` (requires-infra, Req 1.1/1.2)

Wire the live-test env to the instance you JUST built (env only — `offline`).
Reference every credential by env-var **name**; never paste a secret value. These
variable names are the exact contract the task-5.1 live tests consume:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
export CONSUMER_TEST_SLUG=dronefleet
export CONSUMER_TEST_CLUSTER_ENV=cluster.throwaway.env
# The /healthz URL of the workload stood up in 5.3 (through Traefik, placeholder host).
export HELLO_WORKLOAD_TEST_URL="https://hello.dronefleet.platform.example.test/cgi-bin/healthz"
# A live OpenBao read endpoint + token (drives the sealed/unsealed assertion) and
# the platform-admin token the composed onboarders authenticate with. All come
# from the throwaway cluster's gitignored env — referenced by NAME, never echoed.
export OPENBAO_TEST_ADDR="${OPENBAO_TEST_ADDR}"
export OPENBAO_TEST_TOKEN="${OPENBAO_TEST_TOKEN}"
export OPENBAO_ADMIN_TOKEN="${OPENBAO_ADMIN_TOKEN}"
```

Run the live happy-path smoke against the freshly-built workload. The
`requires_infra` marker is the outer gate (deselected offline by default); with
the env above wired it GETs `/healthz` and asserts **200** — the single
"hello-workload healthy" definition-of-done signal (Req 1.4):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest -m requires_infra \
  tests/installer/test_hello_workload_smoke.py::TestHelloWorkloadLiveSmoke::test_hello_workload_healthy_happy_path -v
```

With no cluster wired this **skips cleanly** (never fails) — the "absent infra ⇒
skipped, not failed" rule (Req 1.5).

### 5.5 — Fail-closed: Primary sealed ⇒ workload never starts (requires-infra, Req 1.3)

Now prove the degraded-mode half. **Seal the OpenBao Primary through SVC-07's own
automated path** — never ad-hoc `docker`/`pct` surgery. The Primary is
Transit-auto-unsealed by the unsealer, so the sanctioned way to force a sealed
Primary for this throwaway test is to drive SVC-07's own bootstrap/seal contract
(see the [SVC-07 runbook](../svc-07-secrets-manager/INSTALL-RUNBOOK.md) seal /
`clean_slate` path); e.g. reset SVC-07 to a sealed baseline via its orchestrator:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-07-bootstrap.yml \
  -e cluster_env_file=cluster.throwaway.env \
  -e clean_slate=true -e clean_slate_confirm=force
```

With the Primary sealed, recreate the workload stack (re-run the stand-up from
5.3) so the Agent attempts a fresh render against the sealed Primary:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/dronefleet-workload.generated.yml \
  ansible/playbooks/hello-workload-deploy.yml \
  -e project_slug=dronefleet -e hello_workload_enabled=true
```

Run the mutating fail-closed smoke. It is gated on **`AGENT_LIVE_AUTHORIZED=1`**
(the agent live-run interlock) **and `CONSUMER_TEST_ALLOW_MUTATE=1`** (the
per-suite safety interlock) ON TOP of the live env from 5.4 — throwaway cluster
only. It asserts `/healthz` is **NOT** 200: no RSF ⇒ sidecar never
`service_healthy` ⇒ workload never starts:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
export CONSUMER_TEST_ALLOW_MUTATE=1   # arm the mutating fail-closed smoke (throwaway only)
export AGENT_LIVE_AUTHORIZED=1        # agent live-run interlock (per-run; never committed)
```

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest -m requires_infra \
  tests/installer/test_hello_workload_smoke.py::TestHelloWorkloadLiveSmoke::test_hello_workload_fail_closed_when_primary_sealed -v
```

### 5.6 — Restore + teardown (requires-infra)

**Unseal the Primary through SVC-07's own automated path** — re-run the SVC-07
bootstrap orchestrator (its from-scratch bring-up re-initialises + unseals the
Primary via the unsealer's Transit key), never a hand-run `bao operator unseal`
against the dependency outside its installer:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-07-bootstrap.yml \
  -e cluster_env_file=cluster.throwaway.env
```

Tear the project (including the workload guest) back to a clean baseline. The
workload guest is **non-anchor** (stateless) and lives in the project's own
`dronefleet-workload` state, so the project's clean-slate (Part 4) tears it down
with the rest of the project — no orphan, one-state-per-service (Req 2.5):

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/project-consumer-bootstrap.yml \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e cluster_env_file=cluster.throwaway.env \
  -e project_slug=dronefleet \
  -e svc01_vmid=1101
```

A green happy-path `/healthz` 200 (5.4) is the onboarding **definition-of-done**:
a cleanly-onboarded project boots a workload that reads its OpenBao-mediated
secret and reports healthy, and fails closed (5.5) when the secret source is
sealed.
