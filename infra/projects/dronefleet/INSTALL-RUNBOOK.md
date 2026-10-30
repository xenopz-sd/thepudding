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
