# mailcatcher — shared dev mail catcher (Mailpit) Terraform root

One unprivileged LXC (`mailcatcher-20-01`, `10.0.20.21`, VMID `20011`) on the shared-services VLAN 20,
running [Mailpit](https://mailpit.axllent.org) as a Compose stack. A **dev-only permanent mock stand-in**
for a real SMTP relay so ZITADEL's email-verification flow works on a cluster with no SMTP.

- Install and test: [`INSTALL-RUNBOOK.md`](./INSTALL-RUNBOOK.md)
- Spec: [`.kiro/specs/dev-mail-catcher/`](../../../.kiro/specs/dev-mail-catcher/)
- Decision: [ADR-0025](../../../.kiro/decisions/0025-shared-dev-mail-catcher-and-zitadel-smtp-mock.md)
- Mock catalogue: [Mock 7](../../../.kiro/steering/mock-stand-ins.md)

Terraform state name `mailcatcher` (one state per service). Identity is the non-catalog
`role_code = "mailcatcher"` (ADR-0021); the host index (11) comes from the
[Host_Index_Authority ledger](../../platform-foundation/host-indices.yaml). No secret lives here.
