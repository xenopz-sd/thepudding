# SVC-06 Identity Provider (ZITADEL) Terraform root — outputs.
#
# Service-specific outputs consumed by every downstream service's OIDC client
# config (issuer URL, host, ports) PLUS the generic `inventory_hosts` surface the
# Ansible inventory generator keys on. Non-secret facts only — OIDC client
# secrets + the masterkey live in OpenBao, never a Terraform output.

output "zitadel_host" {
  description = "The ZITADEL LXC's (svc06-20-01) computed static IPv4 (10.0.20.15), bare address with no CIDR suffix. Consumed by the Ansible dynamic inventory + downstream OIDC client config (NET-00 §3)."
  value       = module.zitadel.ipv4_address
}

output "zitadel_http_port" {
  description = "The ZITADEL HTTP port (8080) — web console, login UI, OIDC/SAML endpoints — served behind Traefik (requirements §12). Internal-only; external access is via Traefik over HTTPS."
  value       = 8080
}

output "zitadel_grpc_port" {
  description = "The ZITADEL gRPC management-API port (8081), used by zitadel_provision.py for automated org/application provisioning (requirements §12, FR-6)."
  value       = 8081
}

output "zitadel_issuer_url" {
  description = <<-EOT
    The OIDC issuer URL every downstream service points its OIDC client at
    (e.g. https://auth.<platform-domain>). Built from the platform domain the
    Traefik ingress serves ZITADEL on; the actual host rule is applied by the
    zitadel-server role's Traefik labels. Emitted here as the canonical issuer
    reference for consuming services' Terraform/Ansible (requirements §8/§12).
    The scheme is https because ZITADEL is always Traefik-fronted over TLS (or
    the self-signed mock in dev).
  EOT
  value       = "https://auth.${var.platform_domain}"
}

# Generic per-host inventory surface consumed by the Ansible inventory generator
# (ansible/inventory/generate_inventory.py; PF §8, integration-boundaries §4).
# One object per provisioned guest. `vmid` + `node_name` come from the CONCRETE
# container resource (NOT the module, whose vmid is a null stand-in), so the
# real provider-assigned VMID + node flow downstream to Ansible automatically.
# Only the ZITADEL guest is here; the backing DB guest is inventoried by its own
# SVC-01 apply (its own inventory_hosts output).
output "inventory_hosts" {
  description = "Generic per-host inventory surface (list of {hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by the Ansible inventory generator. The ZITADEL guest only; the backing DB is inventoried by its own SVC-01 instance. Non-secret facts only."
  value = [
    {
      hostname     = module.zitadel.hostname
      ipv4_address = module.zitadel.ipv4_address
      service_role = module.zitadel.service_role
      project_slug = module.zitadel.project_slug
      vmid         = proxmox_virtual_environment_container.zitadel.vm_id
      node_name    = proxmox_virtual_environment_container.zitadel.node_name
    },
  ]
}
