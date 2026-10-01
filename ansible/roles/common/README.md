# `common` role

Host-baseline Ansible role for the Developer Services Platform. It is the PF §9
baseline (hardening, timezone, unattended-upgrades) **and** the single source of
truth for the Docker CE + Compose-plugin install/pin on the platform. There is
exactly one Docker-install implementation, and it lives here — run **per host at
provision time** (see
[`ADR-0003`](../../../.kiro/decisions/0003-install-docker-per-host-via-common-role.md),
which supersedes ADR-0002).

Services boot the **stock `debian-12-standard`** LXC template that `pveam` ships,
and this role installs Docker on top. The role is idempotent:

- On a stock `debian-12-standard` host with no Docker, the role installs Docker CE
  + the Compose plugin (and applies the hardened baseline), leaving the host in a
  runnable Docker/Compose state.
- On a host where the role has already converged, a re-run is a genuine no-op
  (`changed=0`) for the Docker/Compose and baseline tasks — this is how drift is
  reconciled rather than churned.

There is no custom pre-baked Docker template and no build tooling at this stage —
the pre-bake fast path was deferred per ADR-0003 (the earlier
`debian-12-docker_amd64.tar.zst` / `infra/templates/` approach was rolled back
because its build/distribution apparatus was disproportionately complex for this
stage; this role — the hard part — is kept intact so pre-baking can return cheaply
later).

It is a peer of the `docker-compose-app` role — a host-baseline role, not a
Compose stack — which is the `structure.md`-sanctioned host-level exception.

## Supported platforms

Per the sanctioned template families (`tech.md`), declared in `meta/main.yml`:

- Debian 12 ("bookworm")
- Ubuntu 24.04 ("noble")

The Docker install path is exercised for Debian 12 in the
`lxc-docker-base-template` spec.

## Status

Built under
[`.kiro/specs/lxc-docker-base-template/`](../../../.kiro/specs/lxc-docker-base-template/).
The role is complete: standard layout + galaxy metadata, role variables, and the
baseline (`tasks/baseline.yml`) + Docker install/pin (`tasks/docker.yml`) +
Compose verification (`tasks/compose_verify.yml`) tasks. Offline validation
(`ansible-lint`, `yamllint`, `ansible-playbook --check --syntax-check`) is green;
the live provision/idempotency tiers are `requires-infra` (see the spec's
`validation.md`).

## Variables

Defined in `defaults/main.yml`:

- `common_timezone` — host timezone.
- `common_docker_version_pin` — the single pinned Docker CE / Compose-plugin
  version (`state: present`, never `latest`).
- `common_unattended_enabled` — enable unattended-upgrades (default `true`).
