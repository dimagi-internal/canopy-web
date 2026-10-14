# canopy-runtime

Django-free schema for an agent's **declarative runtime spec** — the `runtime.yaml`
an agent ships in its own repo that says *what it needs to run in any environment*.

```python
from canopy_runtime import load_runtime_yaml

spec = load_runtime_yaml(open("runtime.yaml").read())
spec.plugins   # [PluginRef(name="canopy", …), …]
spec.engine    # "emdash" | "cloud_p" | "any"
spec.secrets   # ["canopy-pat", "echo-gog"]  ← reference NAMES, never values
```

## Why it lives here

It's part of the **Agent Runtime Registry**
(`docs/superpowers/specs/2026-07-20-agent-runtime-registry-design.md`). Three layers
hold three kinds of data:

| Layer | Holds | This library |
|-------|-------|--------------|
| **agent repo** | the declarative spec (`runtime.yaml`) | ← defines + validates its shape |
| **canopy-web** | repo pointer + secret *references* + tenant | serves `GET /api/agents/{slug}/runtime` |
| **secret store** | the actual values (1Password / Secrets Manager) | resolved by the reconciler from a name |

canopy-web never parses the spec — the **reconciler** (RS2) reads it from the repo,
which is why the schema is a standalone installable: the reconciler runs on a bare
cloud box that never installs Django.

## Hard rules the schema enforces

- **`extra="forbid"`** on every model — a typo'd key fails the agent's PR instead of
  silently never provisioning.
- **`secrets` is reference names only** — there is no field anywhere that holds a
  secret value, and a validator rejects the obvious leaks (`=`, spaces, over-long).

See `canopy_runtime/example_runtime.yaml` for a fully-documented example.

## Capabilities

`runtime.yaml` may declare **capabilities** — machines the agent leases from canopy-web
(`docs/architecture/capabilities.md` in canopy-web):

```yaml
capabilities:
  - manifest: infra/capabilities/android-emulator/capability.yaml   # relative to runtime.yaml
```

Each manifest is a `CapabilityManifest` (version 1, `kind: ec2-ephemeral`). Publish them as
part of the agent's deploy:

```bash
canopy-runtime publish-capabilities --spec runtime.yaml --agent <slug> [--base-url URL] [--token PAT] [--dry-run]
```

It validates every manifest and `PUT`s the whole list to `/api/agents/<slug>/capabilities`
(exit 0 published, 1 refused or unreachable, 2 invalid spec). canopy-web validates with the
same model.
