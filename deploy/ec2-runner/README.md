# Canopy cloud runner on EC2 (CloudFormation)

Infrastructure-as-code for an ephemeral EC2 box that runs a **`kind=cloud`** canopy
runner: it pairs with canopy-web, claims harness `Turn`s whose target is in its
capabilities, runs `claude -p` on the turn's prompt, streams the assistant/tool
output into the `TurnEvent` ledger, and finishes the turn. This is the first real
**canopy cloud runner** (Wave 4 SP2b) — the deployed counterpart to the in-process
stub executor.

Everything is declared in **`runner.cfn.yaml`** (CloudFormation, matching
`deploy/aws/canopy-web.cfn.yaml`): the instance, security group, IAM role, and a
CFN-managed key pair. The box configures itself via **cloud-init** (no imperative
SSH provisioning), and reads its two secrets from **Secrets Manager** using the
instance role at boot — secrets are never baked into the template or copied over the
wire.

## Files
- `runner.cfn.yaml` — the whole stack (instance + SG + IAM role + key pair + cloud-init).
- `cloud_runner.py` — the self-contained (stdlib-only) runner. `up.sh` splices it into the template as base64 at deploy time (single source of truth).
- `secrets.sh` — put/update the two secrets in Secrets Manager (values read from a file/stdin, never shell history).
- `up.sh` — validate + render + `cloudformation deploy`; pulls the private key from SSM for SSH.
- `down.sh` — `delete-stack` (add `--purge-secrets` to also remove the secrets).

`*.pem` and the rendered template are gitignored.

## Secrets (in Secrets Manager, under `canopy/cloud-runner/`)
- `canopy/cloud-runner/canopy-pat` — a canopy-web PAT (the runner pairs + claims as this user).
- `canopy/cloud-runner/claude-oauth-token` — a **dedicated** claude setup-token (`CLAUDE_CODE_OAUTH_TOKEN`). Mint with `claude setup-token` as `ace@dimagi-ai.com` (Max subscription). It's long-lived and non-rotating, so the runner is self-sufficient after one bootstrap. **Do not copy ace-web's live OAuth blob** — its refresh tokens rotate on every use, so a second consumer gets invalidated (verified: it 401s / can't refresh).

## Run
```bash
cd deploy/ec2-runner
aws sso login --profile labs                         # you, once
./secrets.sh canopy ~/pat.txt                        # canopy-web PAT  (once)
./secrets.sh claude ~/claude-token.txt               # valid claude token (once)
./up.sh                                              # deploy the stack (~3 min to fully boot)
```
Watch it come up / work:
```bash
ssh -i canopy-cloud-runner-key.pem ubuntu@<ip> 'journalctl -u canopy-runner -f'
# cloud-init progress: ... 'sudo cat /var/log/cloud-init-output.log'
```

## Prove it end to end
Enqueue a turn the runner can claim (target must be in its caps — default
`RunnerProjects=canopy-web`):
```bash
curl -sS -X POST "https://labs.connect.dimagi.com/canopy/api/harness/turns/" \
  -H "Authorization: Bearer <canopy-pat>" -H 'Content-Type: application/json' \
  -d '{"project":"canopy-web","workspace":"dimagi","origin":"api","prompt":"Reply with a one-line hello.","idempotency_key":"ec2-smoke-1"}'
```
The runner claims it, runs claude, and events + result land at
`GET /api/harness/turns/<id>` and stream live over the SP1 realtime `turn.{id}` socket.

## Tear down (it's billed hourly)
```bash
./down.sh                    # delete the stack (keeps the secrets for next time)
./down.sh --purge-secrets    # also delete the secrets
```

## Config (CloudFormation parameters)
Override with `EXTRA_PARAMS='Key=Val Key=Val' ./up.sh`:
`InstanceType` (t3.medium), `CanopyBaseUrl`, `RunnerProjects`, `RunnerAgents`,
`RunnerWorkspace` (dimagi), `RunnerName`. `SshCidr` is set to your IP automatically.

## Updating the runner code
`up.sh` splices `cloud_runner.py` into the template's UserData as base64, but
**CloudFormation applies a UserData change to an existing instance as a
stop/start — it does NOT re-run cloud-init.** cloud-init's `write_files` (and
everything else in the `#cloud-config` block) only runs on an instance's
*first* boot. So editing `cloud_runner.py` and running `./up.sh` again updates
the *template*, but a box that's already up keeps running the old bytes on
disk until it's replaced — a silent deploy gap, not a redeploy.

Until there's a real re-provisioning mechanism, ship a runner-code change by
recycling the stack:
```bash
./down.sh   # keeps the secrets (no --purge-secrets)
./up.sh     # fresh instance, fresh cloud-init, new code
```

## Notes
- **Ephemeral by design.** The claude token lives only in Secrets Manager + in
  `/opt/canopy-runner/runner.env` (chmod 600) on the box; `down.sh` removes the box.
  The env is re-fetched from Secrets Manager on every `systemctl restart`, so a
  rotated token is picked up without a redeploy.
- **Runner identity:** the runner pairs on first boot and caches its id in
  `~/.canopy-cloud-runner.json`; a restart reuses the same runner row.
