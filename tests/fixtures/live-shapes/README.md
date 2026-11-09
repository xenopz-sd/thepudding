# Live-shape fixtures (WS-M4 Increment 1)

Committed, sanitized recordings of the **real external API/field shapes** this
platform's code depends on, so an offline contract test can assert that *our
code's assumption still matches the real shape* — catching the "we didn't know the
real shape at authoring time" bug class (PLANNING A.2 #3/#4/#5) at author time
instead of at live run.

Spec: `.kiro/specs/live-shape-fixtures/`. Steering: the "Live-shape fixtures"
section of `testing-strategy.md`.

## What a fixture is (and is not)

- **Is:** a small JSON record of the real shape — path templates, field
  accessors, oneof key names, endpoint strings, feature-flag body shape.
- **Is not:** a secret store. Fixtures carry **no secret values and no real
  hostnames** — only substitution placeholders (`{slug}`, `demo-slug`, empty
  `baseUri`).

## Framing (teeth, not documentation)

The paired test in `tests/installer/test_live_shape_fixtures.py` imports the
actual emitting code and asserts its emitted shape **equals** the fixture. If a
future edit (or an upstream change we transcribe) drifts our code away from the
recorded real shape, the test fails.

## Current fixtures (the vertical slice — 2 highest-pain shapes)

| Fixture | Real shape it records | Our code that assumes it |
|---|---|---|
| `openbao-kv-v2-read.json` | KV v2 read path `secret/data/<slug>/<sub>` + `.Data.data.<field>` nesting (vs bare `bao kv` logical path) | `scripts/installer/workload_auth.py::render_agent_hcl` / `openbao-agent/templates/agent.hcl.j2` |
| `zitadel-login-version.json` | per-app `loginVersion` oneof + the instance `loginV2.required` lever at `/v2/features/instance` | `scripts/installer/zitadel_provision.py` |
| `zitadel-user-management.json` | machine-user create, org-member `ORG_USER_MANAGER`, PAT mint/search/delete, human `_import`, user search/delete, and the observed least-privilege refusals (404/403) | `scripts/installer/zitadel_user_provision.py` (paired test in `tests/installer/test_zitadel_user_roles.py`) |

## How these are maintained

- **Now (Increment 1):** hand-seeded from the live-confirmed shapes already pinned
  in merged `fix-*` specs, role templates, and the committed golden `agent.hcl`.
  No live infrastructure is needed to build or run them.
- **Later (Increment 2, ADR-gated, not yet built):** a read-only
  Proxmox/ZITADEL/OpenBao MCP (off by default, opt-in) becomes the sanctioned way
  to *refresh* these fixtures against a live cluster. That is the
  boundary-touching step (new tool + credentials) and goes through its own ADR +
  spec — see `PLANNING.md` §F.5.
