# SVC-08 (ADR-0029): PKI engine management and the scoped child-token role for issuance.
path "pki/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "pki_int/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "auth/token/roles/pki-issue" {
  capabilities = ["create", "read", "update", "delete"]
}
path "auth/token/create/pki-issue" {
  capabilities = ["update"]
}
