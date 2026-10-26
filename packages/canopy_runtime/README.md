# canopy-runtime

Django-free schema for an agent's **capability manifests** (`capability.yaml`) and the
`canopy-runtime publish-capabilities` command that sends them to canopy-web.

No agent ships a runtime spec file: secrets are declared once in the Canopy vault
(`canopy agent vault`), and an agent is prepared by canopy's `canopy agent prepare`.
Capabilities publish straight from their manifests.

## Capabilities

A capability is a machine (or a pool of slots) the agent leases from canopy-web
(`docs/architecture/capabilities.md` in canopy-web). The agent declares each one in its own
repo as a `capability.yaml`, a `CapabilityManifest` (version 1, `kind: ec2-ephemeral` — a
machine per lease — or `kind: pool` — one named `slots` entry per lease, no machine).
Publish them as part of the agent's deploy:

```bash
canopy-runtime publish-capabilities --dir infra/capabilities --agent <slug> [--base-url URL] [--token PAT] [--dry-run]
canopy-runtime publish-capabilities --manifest a/capability.yaml --manifest b/capability.yaml --agent <slug>
```

`--dir` takes every `<dir>/*/capability.yaml`; `--manifest` names one file and repeats.
It validates every manifest and `PUT`s the whole list to `/api/agents/<slug>/capabilities`
(exit 0 published, 1 refused or unreachable, 2 invalid arguments or manifest). canopy-web
validates with the same model.

**The list is the agent's complete set.** canopy-web retires any capability the list leaves
out and releases its live leases, so a publish that misses a manifest kills that
capability's pool. That is why `--dir` is the form to use, and why it refuses a directory
with no manifests (and one holding a misnamed `capability.yml`) instead of publishing an
empty list.

`--spec` (the pre-2026-10-26 form) is retired and exits 2 with this usage.
