# svc-03-object-storage — SVC-03 Garage Terraform root

Three unprivileged LXC guests (`svc03-20-01`..`03`, `10.0.20.22`..`24`, VMIDs `20012`..`20014`) on the shared-services
VLAN 20, each with a dedicated data volume, running a three-node [Garage](https://garagehq.deuxfleurs.fr) S3 cluster
(replication factor 3).

- Install and test: [`INSTALL-RUNBOOK.md`](./INSTALL-RUNBOOK.md)
- Spec: [`.kiro/specs/svc-03-object-storage/`](../../../.kiro/specs/svc-03-object-storage/)
- Domain requirements: [`requirements/SVC-03-object-storage-requirements.md`](../../../requirements/SVC-03-object-storage-requirements.md)

Terraform state name `svc-03-object-storage` (one state per service). The host indices (12/13/14) come from the
[Host_Index_Authority ledger](../../platform-foundation/host-indices.yaml) and are cross-checked by
`terraform_data.ledger_index_assert`. The guests carry `prevent_destroy`; replace them only through the SVC-03
clean-slate. No secret lives here.
