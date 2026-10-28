# SVC-09 Install Runbook — provision Traefik from nothing, then run its live checks

This is the **provision-then-test spine** for SVC-09 (Traefik): it takes you from
an empty throwaway Proxmox cluster to a running shared Traefik edge on VLAN 20,
and then verifies the ingress against the instance you just built. It is the
"build the live thing, then test what you built" half that the
[`../../../docs/TESTING.md`](../../../docs/TESTING.md) tier table assumes exists.

> To stand this service up as part of the whole platform (dependency-ordered, from
> an empty cluster), see the [all-services walkthrough](../../../docs/TESTING.md#deploy-every-implemented-service-from-scratch-all-services-walkthrough)
> in the testing guide.

There are **two paths**. Section **A** is the **Developer B** fast path: one
Ansible orchestrator command (`svc-09-bootstrap.yml`) that packages the whole
Day-0 bring-up — Preflight → Provisioning → Bootstrap → Handoff — then health-gates
what it built. Sections **0–3** are the **Developer A** manual reference spine for
diagnosing an individual phase.

It is deliberately **thin**: every step links into the existing SVC-09 docs
(the Terraform-root [`README.md`](./README.md), the `traefik_install` role, and
the mock catalogue in
[`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md)) rather than
re-explaining them.

> **Placeholders only.** Every token and hostname here is a placeholder. The
> day-2 secrets Traefik uses (a DNS-01 provider token, the ZITADEL forward-auth
> client secret) are referenced by name only and come from OpenBao at deploy
> time — never committed or echoed (`documentation-testing.md`,
> `security-standards.md`).

> **Doctest tiers.** Offline preflight commands carry `<!-- doctest: offline -->`;
> the `terraform apply`, the Ansible role runs, the health run, and the teardown
> carry `<!-- doctest: requires-infra -->`. Tier rules are owned by
> [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md);
> the install-runbook location convention by
> [`structure.md`](../../../.kiro/steering/structure.md).

---

## ⚠ Throwaway cluster only

The live steps provision, mutate, and tear down real infrastructure. Run them
**only against a throwaway / ephemeral Proxmox SDN test cluster** — never a shared
or production cluster. Section 3's teardown runs `terraform destroy`, which
deletes the Traefik LXC (and, with it, the `acme.json` cert store on its rootfs).

---

## Dependencies and the mock stand-ins (SVC-08 / SVC-06 not built yet)

SVC-09 depends on two services that do not exist yet, and ships **permanent,
default-off mock stand-ins** for each (per
[`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md)):

| Real dependency | Mock stand-in (opt-in, default OFF) | What the mock provides |
|---|---|---|
| **SVC-08 / EJBCA + Let's Encrypt** (TLS certs) | `traefik_tls_self_signed=true` (Mock 3) | A self-signed default cert so `websecure` serves HTTPS before any CA exists. Clients use `curl -k` / `validate_certs:false`. No public trust chain. |
| **SVC-06 / ZITADEL** (dashboard forward-auth) | `traefik_dashboard_auth_disabled=true` (Mock 4) | The dashboard on an internal-only route with forward-auth inert. Never anonymous `:8080`. |

On a throwaway bring-up before SVC-08/SVC-06 exist, enable **both** mocks. In
production leave both OFF: deploy SVC-08 (or make Let's Encrypt reachable) and
SVC-06, and the real resolver + real forward-auth take over with no change to
Traefik's config. With the TLS mock OFF and no CA reachable, the role **fails
closed** at render with a clear remedy — it never silently serves no TLS, and the
dashboard is never left anonymous.

---

## A. Developer-B automated path — one command, provision-then-test

Every command appears **exactly once**, in run order: offline preflight, then the
single-command install, then the health verification, then the clean-slate reset.

### A.0 — Offline preflight (no cluster needed)

Validate the SVC-09 Terraform root the orchestrator's Provisioning phase applies:

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/svc-09-reverse-proxy
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

Parse-check the orchestrator playbook (compiles the play without connecting):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-09-bootstrap.yml
```

Run the SVC-09 offline suite (shape, config render, mock drift-guards):

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest infra/tests/test_svc09_terraform_shape.py \
  infra/tests/test_svc09_traefik_config_render.py -q
~/venv/devinfra/bin/pytest tests/installer/test_svc09_mocks.py \
  tests/installer/test_svc09_thin_orchestrator_play.py \
  tests/installer/test_svc09_swap_gate.py \
  tests/installer/test_svc09_secret_hygiene.py -q
```

### A.1 — Prerequisite: the VLAN-20 fabric

SVC-09 attaches to the shared VLAN-20 VNet `p20`, created by the
platform-foundation layer. Bring the platform prerequisites up first if `p20`
does not exist (see the
[platform install runbook](../../platform-foundation/INSTALL-RUNBOOK.md)); the
SVC-09 Preflight phase fails closed naming `p20` if it is absent.

### A.2 — One-command install (provision → bootstrap → health)

On a throwaway cluster before SVC-08/SVC-06 exist, enable both mocks:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-09-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env \
  -e traefik_tls_self_signed=true \
  -e traefik_dashboard_auth_disabled=true
```

The orchestrator runs Preflight (cluster.env resolve, Proxmox auth, `p20` gate,
datastore autodetect), Provisioning (`terraform apply` of the single Traefik LXC,
inventory generation, known_hosts), Bootstrap (the `common` role then the
`traefik.yml` configurator run — static config, the real `edge` network, the
Compose stack), and Handoff (the `svc09-health.yml` gate + the completion
banner).

### A.3 — Verify the ingress (health, redirect, discovery)

The Handoff phase already ran `svc09-health.yml` (container Running + the
HTTP→HTTPS redirect on the `web` entrypoint). To additionally prove backend
auto-discovery, start a labelled test container on the `edge` network and confirm
Traefik routes to it within seconds, no restart:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/svc-09-reverse-proxy.generated.yml \
  ansible/playbooks/svc09-health.yml \
  -e svc09_traefik_container_name=traefik-traefik-1
```

### A.4 — Clean-slate reset

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-09-bootstrap.yml \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e svc09_proxmox_host=<real-proxmox-lan-ip>
```

The reset pct-destroys the Traefik LXC and removes it from Terraform state, then
verifies an empty baseline two ways (`pct list` + `terraform state list`).

---

## 0–3. Developer-A manual reference spine

- **0. Fabric gate.** Confirm `p20` exists (`pvesh get /cluster/sdn/vnets`); if
  absent, apply platform-foundation first.
- **1. Provision.** `terraform apply` the SVC-09 root (one LXC, `svc09-20-01`,
  `10.0.20.12` — `host_index 2`, moved from the colliding `0`/`.10`; see the
  host-index ledger `infra/platform-foundation/host-indices.yaml`). The
  `acme.json` store lives on the LXC rootfs (persistent); a `prevent_destroy`
  seatbelt stops an accidental replace from discarding it. **Note:** because the
  Traefik IP moved from `.10` to `.12`, an already-deployed `.10` Traefik guest
  cannot be apply-moved in place (the `prevent_destroy` seatbelt hard-errors on
  the replace) — relocating it is a conscious one-time re-provision via this
  service's own clean-slate contract, and `acme.json` is re-issuable.
- **2. Configure.** The `common` role installs Docker + Compose; `traefik_install`
  renders the static `traefik.yml`, creates and owns the real `edge` network, and
  brings up the Compose stack (host ports `80`/`443`, read-only Docker socket,
  persistent acme volume).
- **3. Health.** `svc09-health.yml` gates on the container being Running and the
  `web` entrypoint serving the HTTP→HTTPS redirect (no CA assumed under the TLS
  mock).

---

## SVC-07 handoff: flip the OpenBao Traefik affordances to real

Once Traefik owns the real `edge` network, SVC-07's Traefik affordances become
live:

- SVC-07's placeholder `openbao_mock_edge_network` (Mock 2) must stay **off** on
  any host where Traefik runs — Traefik owns the real `edge` now (coexistence
  caveat). The OpenBao primary's dormant `traefik.*` labels become live ingress.
- SVC-07's through-Traefik health check can be turned on to verify the real
  ingress route end-to-end:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/svc-07-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env \
  -e svc07_health_check_via_traefik=true
```

This leaves SVC-07's own code unchanged — only the default-off affordance is
flipped on now that its real dependency (Traefik) exists.
