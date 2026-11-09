# Mail catcher Install Runbook — provision the shared dev mail sink from nothing, then test it

> **Status: implemented and LIVE-VALIDATED on a throwaway cluster** (ZITADEL `v4.18.0`, Mailpit `v1.33.0`), including the
> platform clean-slate discovery, the explicit-acknowledgement variant, the playbook refusal path and a second
> from-scratch bring-up ([WS-C19](../../../PLANNING.md)). One path remains tracked: `allow_register` through the full
> consumer orchestrator ([WS-C24](../../../PLANNING.md)).
> Closed by [WS-C16 and WS-C17](../../../PLANNING.md).

This is the **provision-then-test spine** for the shared dev mail catcher ([Mailpit](https://mailpit.axllent.org),
MIT): it takes you from an empty throwaway Proxmox cluster to a running catcher on VLAN 20, then verifies
it. It exists because the dev cluster has **no SMTP**: a user who self-registers in ZITADEL stays in
`USER_STATE_INITIAL` waiting for a verification code that can never be delivered. The catcher receives
that mail so you can read the code in its web UI and finish the flow.

> **Dev-only permanent mock stand-in** for a real SMTP relay (catalogue: Mock 7 in
> [`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md)). It is **never on by default** and is
> never turned on by a merge or by another service. ZITADEL is only pointed at it by an explicit,
> separate command (section A.4). Decision record: [ADR-0025](../../../.kiro/decisions/0025-shared-dev-mail-catcher-and-zitadel-smtp-mock.md);
> spec: [`dev-mail-catcher`](../../../.kiro/specs/dev-mail-catcher/).

> **Placeholders only.** Every hostname and credential here is a placeholder. The UI password is generated
> by the installer, written to your gitignored cluster env file at mode `0600`, and **never printed**
> (`documentation-testing.md`, `security-standards.md`).

> **Doctest tiers.** Offline preflight commands carry `<!-- doctest: offline -->`; the `terraform apply`,
> the Ansible runs, the health run and the teardown carry `<!-- doctest: requires-infra -->`. Tier rules
> are owned by [`documentation-testing.md`](../../../.kiro/steering/documentation-testing.md); the runbook
> location by [`structure.md`](../../../.kiro/steering/structure.md).

---

## ⚠ Throwaway cluster only

The live steps provision, mutate and tear down real infrastructure. Run them **only against a throwaway
Proxmox SDN test cluster**. Agent-driven live runs additionally require `AGENT_LIVE_AUTHORIZED` and the
throwaway guard (`testing-strategy.md`).

---

## What you get

| Item | Value |
|---|---|
| Guest | `mailcatcher-20-01`, unprivileged LXC, VLAN 20, `10.0.20.21` (Host_Index_Authority index 11), VMID `20011` |
| Identity | non-catalog `role_code: mailcatcher` ([ADR-0021](../../../.kiro/decisions/0021-proxmox-compute-first-class-non-catalog-guest-identity.md)); no catalog slot exists for a mail sink |
| SMTP | `10.0.20.21:1025`, no auth, no TLS (dev only), bound to the VLAN-20 IP, never `0.0.0.0` |
| Web UI | `https://mail.<platform-domain>/` through Traefik (file-provider route); basic auth by default |
| Image | `axllent/mailpit` pinned to an exact version **and** digest (never `latest`) |
| Retention | 500 messages / 7 days, disposable |
| Outward mail | **never**: no relay/forward option is ever set, and a test forbids it |

## A. Developer-B automated path — one command, provision-then-test

### A.0 — Offline preflight (no cluster needed)

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
cd infra/projects/mailcatcher
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/localhost.yml \
  ansible/playbooks/mailcatcher-bootstrap.yml
```

<!-- doctest: offline -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/pytest tests/installer/test_mailcatcher_install.py \
  tests/installer/test_zitadel_smtp_catcher.py \
  tests/installer/test_zitadel_allow_register.py -q
~/venv/devinfra/bin/pytest infra/tests/test_mailcatcher_terraform_shape.py -q
```

### A.1 — Prerequisites

- The VLAN-20 fabric (`p20`) from the [platform install runbook](../../platform-foundation/INSTALL-RUNBOOK.md);
  the preflight fails closed naming `p20` if it is absent.
- The ledger row (`role_code: mailcatcher`, `host_index: 11`) in
  [`host-indices.yaml`](../../platform-foundation/host-indices.yaml) (already committed).
- Traefik (SVC-09) standing, for the web-UI route. Bring it up through its own installer if needed
  ([SVC-09 runbook](../svc-09-reverse-proxy/INSTALL-RUNBOOK.md)); never by hand.

### A.2 — One-command install (preflight → provision → bootstrap → health)

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/mailcatcher-bootstrap.yml \
  -e cluster_env_file=cluster.dev.env
```

The orchestrator runs **Preflight** (env resolve, Proxmox auth, `p20` realized-bridge check, datastore
autodetect, throwaway-guard consult when agent-driven), **Provisioning** (`terraform apply` of the single
LXC, inventory generation, `known_hosts`), **Bootstrap** (generates the UI password if your env file has
none and persists it as `MAILCATCHER_UI_PASSWORD` at `0600`; the `common` role; the Mailpit stack and the
Traefik route) and **Handoff** (the health gate, then the banner).

**UI access control fails closed.** The install refuses to run unless the UI is protected: native basic
auth (default) or the explicit acknowledgement
`-e mailcatcher_ui_auth_disabled=true`. The health gate checks it live: an unauthenticated request must be
refused (HTTP 401) and the generated credential must be accepted. (A proxy-side forward-auth option is not offered:
`traefik_route` cannot attach a middleware, so it could not be enforced; backlog WS-C23.)

### A.3 — Verify

The Handoff already ran the health gate (container running, SMTP banner on `10.0.20.21:1025`, UI
access-control posture). To re-run it alone:

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/mailcatcher.generated.yml \
  ansible/playbooks/mailcatcher-health.yml \
  -e mailcatcher_container_name=mailcatcher-mailpit-1 \
  -e mailcatcher_ui_auth_expected=true
```

### A.4 — Point ZITADEL at it (explicit, separate, opt-in)

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/zitadel-smtp.yml -e zitadel_smtp_dev_catcher=true
```

This fails closed if the catcher is not provisioned or not answering. It **refuses to replace a real
SMTP/HTTP email provider** (the catcher's provider is tagged; anything else is left alone), is idempotent,
and verifies by read-back. To undo (only removes the catcher's own provider):
`-e zitadel_smtp_remove=true`. It needs a ZITADEL IAM_OWNER token from `ZITADEL_PROVISION_TOKEN` or, if
unset, OpenBao (with `OPENBAO_ADMIN_TOKEN` exported).

### A.5 — Read a verification code (the dead-end this fixes)

1. Open `https://mail.<platform-domain>/` and sign in as user `mailcatcher` with the password stored under
   `MAILCATCHER_UI_PASSWORD` in your cluster env file.
2. Self-register a user in the ZITADEL hosted login. The initialization/verification mail appears in the UI;
   copy the code into the ZITADEL prompt.

> **Which org does the user land in?** Verified live on ZITADEL `v4.18.0`: a self-registered user lands in the org
> named by the authorization request. **With** the scope `urn:zitadel:iam:org:id:<org id>` the user is created in that
> (project) org; **without** it the user lands in the instance **default** org. So an app that wants its users in its
> own project org must request that scope. ZITADEL has no automatic default-role grant on self-registration, so such a
> user still carries no project role claim; that is a tracked follow-up ([WS-C18](../../../PLANNING.md)). To stop
> self-registration instead, set `sso.login_policy.allow_register: false` in the consumer manifest
> ([reference](../../../docs/CONSUMER-MANIFEST-REFERENCE.md)).
>
> **What the live run showed.** Registering through the hosted login produced an "Initialize User" mail from the
> instance SMTP provider; it appeared in the catcher; entering its code activated the user and the login completed
> with an authorization code issued.

### A.6 — Clean-slate reset

<!-- doctest: requires-infra -->
<!-- cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/localhost.yml \
  ansible/playbooks/mailcatcher-bootstrap.yml \
  -e clean_slate=true -e clean_slate_confirm=force \
  -e mailcatcher_proxmox_host=<real-proxmox-lan-ip>
```

The reset destroys the guest, removes the Traefik route fragment (so no dangling route points at the freed
IP), residue-sweeps the Terraform state, and verifies an empty baseline two ways (`pct list` and
`terraform state list`). The platform clean-slate also discovers this service through its catalog row.

---

## Degraded modes

| Situation | What happens | What you see |
|---|---|---|
| Catcher down, flag on | ZITADEL cannot send mail; ZITADEL itself stays up | users report "code not received"; health play fails |
| Flag on, catcher not provisioned | `zitadel-smtp.yml` refuses | message names `mailcatcher-bootstrap.yml` |
| Real email provider already active | helper refuses (exit 4), changes nothing | message says a non-catcher provider is active |
| UI unprotected | install and health gate both fail | message names the three accepted controls |

## Image bumps and security advisories
The image is pinned to an exact tag **and** digest. Mailpit has had a steady stream of advisories (SSRF in the proxy, link
check and HTML check; unbounded buffering; WebSocket origin handling), so treat every bump as a security review:

1. List the published advisories for the project (GitHub advisory API, which returns structured affected and first-patched
   versions; the HTML advisory pages do not):
   `GET https://api.github.com/advisories?affects=github.com/axllent/mailpit&ecosystem=go&per_page=50`.
2. Add any new advisory to `tests/fixtures/advisories/mailpit-advisories.json` (CVE, GHSA, summary, `first_patched`).
3. Run `~/venv/devinfra/bin/pytest tests/installer/test_mailcatcher_image_advisories.py`. It fails while the pinned tag in
   `ansible/roles/mailcatcher_install/defaults/main.yml` is below the highest first-patched version.
4. To bump: choose the new tag, resolve its digest from the registry, update `mailcatcher_image_tag` and
   `mailcatcher_image_digest` together, then run the live probes in A.3 (SMTP banner, UI 401 unauthenticated and 200 with the
   credential, ports bound only to the VLAN-20 IP) on a throwaway cluster.

Status at 2026-11-07: pinned `v1.33.0` is above every published first-patched version (highest `1.30.6`). `v1.33.1`
(published the same day) adds one unadvised hardening fix (limits on remote stylesheets fetched by HTML check); bumping to it
is tracked as WS-C28.

## Known limitations

- Captured mail from **every** project is co-mingled in one UI. Acceptable for a dev tool.
- Mailpit's maintainer location was not verified, so it is treated as a flagged non-EU exception
  ([ADR-0025](../../../.kiro/decisions/0025-shared-dev-mail-catcher-and-zitadel-smtp-mock.md)). Any SMTP sink is a
  drop-in replacement; the guest identity is tool-neutral.
- Turning the flag off does **not** remove the provider; use `zitadel_smtp_remove=true`.
- The ZITADEL SMTP and login-policy request shapes are confirmed live on `v4.18.0`
  ([fixture](../../../tests/fixtures/live-shapes/zitadel-email-provider-smtp.json)). A different ZITADEL major may differ.
- A re-run of the installer reports a few `changed` bookkeeping tasks (`terraform init`, inventory regeneration,
  `known_hosts`); the substantive steps (Terraform apply, `common`, Mailpit) report no change.
- If the catcher is torn down while ZITADEL still points at it, mail silently fails; remove the provider first
  (`zitadel_smtp_remove=true`), then clean-slate the catcher.

## Carried operator notes (from the v0.14.0 feedback)

- Prisma/DB **migrations run on-VLAN as the PostgreSQL superuser**: the production backend image has no
  `prisma` CLI and the least-privilege app role has no DDL. The superuser password is under the project's
  `secret/proj-<slug>/postgres` KV path, not the workload bridge path.
- On a node without `local-zfs`, set `PROXMOX_DATASTORE_ID=local-lvm` in the cluster env file for the
  workload path.
