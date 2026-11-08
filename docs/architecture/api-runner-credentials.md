# API: runner credentials and sign-in

Rules the schema cannot express for how a cloud runner signs in and gets its credentials (laptops use ambient auth). Part of the [api surface](api-surface.md) topic. Routes, bodies and status codes are in `apps/harness/api_runners_credentials.py`, `api_runners_mints.py`, `api_turn_agent_token.py`, `apps/agents/api_vault_keeper.py`, `apps/capabilities/api.py` and the OpenAPI schema. The model is [runner-credentials.md § A cloud runner's Claude credentials](runner-credentials.md#a-cloud-runners-claude-credentials-one-ordered-list).

Gate names are `route_gates.py`'s: `runner-admin` is `can_administer_runner` (a person); `runner` is the box itself, with its runner token (or its owner's token, where a route says so).

## No route takes a plaintext secret
- A token or API key is added by signing it in or by sealing it into the runner's vault; never pasted into a route. `PATCH …/credentials/{key}` changes only the name (a `value` is 422). `POST …/vault/entries` returns only the entry and where the browser seals its value (`secret_name`, `vault_id`, `kv`); 422 when the runner's vault has no key. The status routes return masked booleans and a `vault_secret` name, never a value (`GET …/credential/status` is a true read and never bumps `updated_at`/`updated_by`).
- A `token` sign-in names its entry (`mint/entry`), the box seals the token into the vault, and `mint/result {vault_key}` is done only if the vault holds it. A token sent in `mint/result` fails the sign-in, unstored (`api_runners_mints.py::post_runner_mint_result`). A `box` login stays on the box; canopy-web holds only whether it is signed in and as whom.

## Reading credentials back
- `GET …/credential/resolve` and `PUT …/vault/values` are the box only: the box's own runner token (403 `runner_only` for a person's token or session) with a device proof of an active key on that runner (401 `device_proof_required`). Values are `zk1:` ciphertext; each value handed out is logged as a `RunnerCredentialRead`. Not an MCP tool (`runner_vault_resolve.py`).
- `GET …/credential` returns the list the box uses with no value (`cascade`, `logins`, `vault_open`).
- `POST …/vault` (`runner-admin`, step-up from a browser, a token allowed) makes and attaches the runner's own vault ([vault.md § A runner's own vault](vault.md#a-runners-own-vault)); a runner on a credential set is attached to the set's vault instead.

## The ordered list
`PUT …/credentials/order` must name every entry exactly once, else 422 and nothing is applied (`runners.py::order_runner_credentials`). `DELETE …/credentials/{key}` is refused (422) for the last token or API key. The box uses the first entry that works. The `…/logins*` and `…/credential/swap` routes are the older spellings of the same list. All writes are `runner-admin` settings changes.

## Browser sign-in relay (`api_runners_mints.py`)
- canopy-web only relays: it never holds the PKCE verifier and is never the OAuth client. Starting supersedes any unfinished attempt (a stalled sign-in must not block later ones); a `box` sign-in is refused (409) for a runner on a credential set; an unknown `login_key` is 404.
- `mint/claim` is polled by the box and returns work only when the box owes it; reading a code consumes it (a spent code is never redelivered). `mint/code` (`runner-admin`) is 409 unless the runner is waiting for one, 422 if empty. `mint/url`, `mint/entry`, `mint/result` are 409 when no sign-in is in progress.
- Google sign-in for an agent (`…/google-mint/…`): the box keeps the verifier and sends ciphertext only; a value sealed to someone who may not read the agent, or for another Google account than the agent's, is refused.
- **Device-code sign-ins** (`POST …/sign-in-request`, `sign_in_requests.py`): the push goes only to the runner's owner and only for an AWS device-authorization host, because approving a device code grants access to whoever started it. `reason` is required. The first reported outcome wins.

## What a turn the runner holds may fetch
All of these require a turn this runner has claimed and is executing, else 404, and re-check that the box may still hold the agent (an owner can be demoted mid-turn).
- **GitHub identity** (`api_runners_credentials.py::turn_github_token`, `delegations.github_refusal`; [runner-credentials.md § The GitHub identity a turn gets](runner-credentials.md#the-github-identity-a-turn-gets)). No token is returned: `delegated` names the vault entry the box opens from its own resolve. A confined (read-mode or host-site) turn and a one-person review turn are never given it, and there is no shared fallback (409 with the reason, also when the delegating person is no longer an admin).
- **Agent canopy login** (`api_turn_agent_token.py`; [§ The canopy login a turn gets](runner-credentials.md#the-canopy-login-a-turn-gets-turn-scoped-agent-token)). Only with the runner's own runner token; a PAT, its owner's included, is 403. Expires within 12 h and is revoked when the turn stops executing there (`scope=session` ties it to a live session instead). 409 for a direct turn, a confined turn, or an agent with no active login. Each issue is logged as a credential read.
- **Attachment bytes** (`turn_attachment_content`). Authorized by holding the turn, not by the box owner being able to read the chat. Only attachments already sent in that turn's chat; every refusal is the same 404. In a sensitive conversation only a file this turn claimed, once: the bytes are deleted as they are served (`private_attachments.take_for_runner`).
- `GET …/github-readiness` (live GitHub check per routed agent) and `GET …/agents` (`routing.agents_routed_to`, what the box's refresh prepares) are `runner`.

## The keeper (`apps/agents/api_vault_keeper.py`, `apps/capabilities/api.py::put_slot_secret_reseal`)
The box decides whom to trust and re-seals; canopy-web only lists. A re-seal (delegated credential or pool-slot secret) is refused for a box that did not hold the value (403 `zk_not_a_holder`), for a recipient that is not a current machine or reader (422), for one that drops a holder (422), and when the value changed since the box read it (409 `zk_value_changed`). Wraps only add copies, never replace one; pins are writable only by a box holding the current key over the version it read (409 `keeper_pins_stale`). `POST …/delegated-checks` is from a box that runs the agent, for an entry sealed to it; it changes nothing about who the entry is sealed to. Slot secrets: [capabilities.md](capabilities.md) §6.
