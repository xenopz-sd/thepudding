# Contributing

## Questions and issues

This public repository receives tagged releases from a private primary repository, so merge requests are not
accepted here yet. Questions, bug reports and feature requests are welcome as issues on the repository where you
found this release. For a suspected security
vulnerability, do **not** open a public issue; follow [`SECURITY.md`](./SECURITY.md).

## How changes are made

Every change follows the spec lifecycle in [`.kiro/steering/spec-lifecycle.md`](./.kiro/steering/spec-lifecycle.md)
(requirements, design, tasks, validation under `.kiro/specs/<feature>/`) and the test rules in
[`docs/TESTING.md`](./docs/TESTING.md): the four offline test roots must pass, and anything that touches a cluster
is validated on a throwaway Proxmox cluster only.

## AI-assisted contributions

Contributors may use AI-assisted development tools.

Contributors remain responsible for reviewing their contribution and for
ensuring that they have the rights required to submit it under the
Apache License 2.0.

AI-generated output must not be submitted solely on the assumption that
use of an AI system establishes ownership or license compatibility.
