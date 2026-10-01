# `terraform_clean_slate` role

Canonical **clean-slate reset** for a Terraform-provisioned Proxmox guest: `pct
destroy` the guest, then `terraform state rm` its resource, so a subsequent normal
`terraform apply` recreates it **truly fresh** — no surviving Docker volume, no
stale init state.

## Why this exists

These platform guests are single-role, and clean-slate is a **development** activity
(not a runtime one). OpenBao bring-ups repeatedly picked up a **surviving Docker data
volume** across "rebuilds" (the container was destroyed but its named volume
persisted), so `operator init` was skipped and the bootstrap never ran. A full
destroy + recreate of the guest is the robust reset.

The guests carry `lifecycle.prevent_destroy = true` (the accidental-destroy seatbelt),
which Terraform **cannot** toggle via a variable. So this role destroys the guest
**out-of-band** (`pct destroy`) and removes it from state (`terraform state rm`),
deliberately routing around the plan-time seatbelt **without weakening it** for normal
operations.

## Safety model (destructive tool)

- **Opt-in:** `clean_slate_enabled` defaults `false` → the role is a NO-OP.
- **Mandatory per-guest confirm token:** `clean_slate_confirm` MUST equal the
  target's `vmid`, or the role FAILS CLOSED before any destroy. A token matching one
  target does not authorise another.
- **Fail-closed:** a target missing `terraform_root` / `resource_address` / `node` /
  `vmid` aborts with a clear message; nothing is destroyed.
- **Loud:** the role prints exactly what it will destroy before acting.
- **Fail-safe ordering:** `pct destroy` runs FIRST, then `terraform state rm` — and
  `state rm` is reached only if the destroy did not hard-fail, so a pct failure never
  leaves a state-rm'd-but-still-running orphan.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `clean_slate_enabled` | `false` | Master opt-in. |
| `clean_slate_confirm` | `""` | Mandatory per-run token; must match each target's `vmid`. |
| `clean_slate_targets` | `[]` | List of `{name, terraform_root, resource_address, node, vmid}`. |

## Usage

Run against the Proxmox host inventory (the terraform step is delegated to
localhost). Recreate is a SEPARATE step — after the reset, run the normal
`terraform apply` with your cluster `-var` set.

```bash
~/venv/devinfra/bin/ansible-playbook -i ansible/inventory/proxmox-hosts.yml \
  ansible/playbooks/clean-slate.yml \
  --extra-vars "ansible_host=<real-proxmox-lan-ip>" \
  -e clean_slate_enabled=true -e clean_slate_confirm=1070 \
  -e '{"clean_slate_targets":[{"name":"svc07-20-01","terraform_root":"infra/projects/svc-07-secrets-manager","resource_address":"proxmox_virtual_environment_container.primary","node":"shrimp","vmid":1070}]}'
```

The role never runs `terraform apply`/`destroy`; `prevent_destroy` on the guests
stays intact.
