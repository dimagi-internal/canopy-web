# Host sites are the systems of record for their people

A host site (Connect Labs, HQ, ace-web, …; [host-sites.md](host-sites.md)) that brings people to canopy stays the **system of record** for them. canopy keeps, per person, only:

- an **anchor**: the `Contact` (`apps/contacts/models.py`), keyed by the site and the site's own id for the person (`app`, `external_id`), and its `Person`, keyed by the site's signer and that id. Never merged with another site's person;
- a **pointer** to where the host's record lives: `Contact.record_uri`, `<site.person_scheme>://<id>`;
- a **cache the host owns**: `Contact.host_profile` (with `host_profile_version`, `host_profile_signed_at`), a few labels an agent may see on every turn (stage, language, programme, consent), never history;
- the **working sessions** of its conversations, under retention (the site may set its own) and deleted when the host erases the person.

canopy never becomes the authority for anything the host owns. The workspace's own words about a person (`notes`, `attributes`) stay the workspace's; the host never writes them, and canopy never writes the host's fields from anywhere but the host's signed request.

Every mechanism below is additive within `canopy_sdk.contract.CONTRACT_VERSION`, and a host opts in through its `site.json` and its signed requests (`canopy_sdk.contacts`, `canopy_sdk.host.contacts`).

## The site declares where its records live

`site.json`'s `site` block carries `person_scheme` (`canopy_sdk.site::SITE_FACTS`; the Django host fills it from `CANOPY_HOST["PERSON_SCHEME"]`). canopy reads it LIVE from the verified, cached document, like `resource_schemes`, with no owner adopting it (`site_needs.person_scheme_for`): it widens nothing. No verifiable document, no scheme, and a `record_uri` is refused.

## The host keeps its records current: `POST /api/sites/contacts`

The body is a compact JWS signed by the site's own key (`Content-Type: application/jwt`, `typ` `canopy-contact-update+jwt`), checked like every other site-signed request: the site is resolved by name and workspace (`site_agents.site_for_request`), the signature by its registered keys, `aud` is the route's URL, at most 60 s, single-use `jti` (`apps/host_grants/contact_records.py::verify`). Up to 100 people per request.

- **Only the host's own people.** Every write is keyed by the verified site (`contacts.services.apply_host_record`): a site cannot reach a contact another site vouches for, nor an email or Slack contact, whatever `sub` it names.
- **Only three fields, only forward.** `display_name`, `record_uri`, `host_profile`, and only when `version` is newer than the stored `host_profile_version`; otherwise the item is `stale` and nothing changes, so retries and reordering are safe. This is the one path that overwrites `display_name` instead of filling a blank (`record_embed_visitor`): the site's signed word about its own field.
- **Created if missing, granted nothing.** A missing person is created (`source=embed`, grade `app_signed`, no conversation), so a host can register people before they first write in. Recording grants nothing, as for every contact.
- **Each item answered on its own** (`created` / `updated` / `stale` / `refused` with a reason), so one bad item never sinks the others. Item rules are the SDK's (`canopy_sdk.contacts::check_item`): `record_uri` under the declared scheme with a url-safe id, at most 256 characters; `profile` at most 40 keys and 4096 bytes.

## The host makes canopy forget: `POST /api/sites/contacts/erase`

Signed the same way (`typ` `canopy-contact-erase+jwt`, `subs`: up to 100 of the site's ids). For each person the site vouches for, `contacts.services.erase`:

- deletes their conversations with every word, file and turn in them, and records the transcripts as deleted so a runner cannot ship them back (`content_deletion.erase_session`). Unlike a person deleting their own words, it is never refused for a busy session: the system of record has said the person is gone;
- scrubs the words of any other turn they started, and deletes their contact tokens and act-as-you permissions (they go with the contact row);
- deletes the facts recorded about the person in that workspace; where another contact in the same workspace is the same `Person`, only the facts this contact's own turns or notes produced;
- deletes the `Person` when nothing else holds it (`services._person_held`: another workspace's contact, a login, facts elsewhere; only logs about the person do not count). Fails closed: an unknown relation keeps the person;
- leaves a `ContactErasure` tombstone keyed `(app, external_id)` with `erased_at`: the proof it happened, and the reason an update the host signed BEFORE the erasure is refused (`erased`) instead of bringing the person back. A later arrival creates a new contact; the tombstone stays.

Nothing of anyone else's goes: another site's contact with the same id, an email or Slack contact, or the same person's record in another workspace. Idempotent: a second erasure answers `not_found` and changes nothing.

## The host's retention

`site.retention_days` (`CANOPY_HOST["RETENTION_DAYS"]`) holds the site's contacts' conversations to the site's own period (`retention.services._sweep_site_contacts`, rule `site_rule`): read live like `person_scheme`, and applied whatever `CANOPY_RETENTION_ENFORCE` says, as the sensitive-data period is. A shorter workspace rule still applies when enforcement is on, so the shorter of the two wins.

## The agent asks the host about one person: `contact_scoped` and the subject statement

A contact has no passkey, so never an act-as-you grant (`HostGrant`), and an agent that serves a site on pages is normally not offered to its contacts. An agent owner (or admin) may mark the agent's entry for a site **`contact_scoped`** (`PUT /api/agents/{slug}/host-sites/{site}` with `contact_scoped: true`, `profile_settings.set_site`; parsed by `published_profiles.parse`). Then:

- `contact_api._for_contacts` offers the agent to that site's contacts (`published_profiles.contact_scoped`);
- a contact's turn on that entry may use `site_tools`, `site_call` and `site_read`, and the gateway runs its **subject mode** (`host_gateway._subject_context`): no grant, no token. Each request carries `Authorization: CanopySubject <jws>` (`typ` `canopy-subject+jwt`, signed by canopy's client key, the CIMD key `Canopy-Execution` already uses; `client_identity.subject_statement`) naming `{sub, site, agent, turn, session, capability}`, bound to canopy's DPoP key (`cnf.jkt`) and to the JSON-RPC request (`rpc`), 60 s, single-use; a DPoP proof with no `ath`; and `Canopy-Execution`, which now carries `sub` too (`execution.subject_of`).
- **The host authorizes every call against `sub`** (`canopy_sdk.host.verify_subject` → `Subject`). canopy's part is only to say truly who the turn is with.

The subject mode is reachable only when every one of these holds (`host_gateway.resolve`), each pinned by a negative test in `tests/test_contact_scoped_site_tools.py`: the turn's initiator is a contact; the turn's own profile (`Turn.capability`) names the conversation's site with `contact_scoped: true`; the site is live and names its MCP resource (`host_mcp_resource`); the contact is the SITE's own (`Contact.app` is that site, `source` embed, an `external_id`), not blocked; and the runner meets the conversation's requirements. `sub` is the contact's `external_id`, read from the turn's initiator, never from the tool's arguments. Anything else takes the grant path, which refuses a contact.

## The agent sees who the person is at the host

The caller envelope's `contact` block (`apps/harness/caller_context.py::_contact`) carries `external_id` (for a site's contact), `site`, `record_uri`, `host_profile` and `person_id`. The canopy plugin prints them as data about the person, never as instructions.

## Code map

| Question | Where |
|---|---|
| The fields | `apps/contacts/models.py` (`Contact.record_uri`, `host_profile`, …) |
| The wire (both halves) | `sdk/python/src/canopy_sdk/contacts.py`; the host's client `sdk/python/src/canopy_sdk/host/contacts.py` |
| Verify and apply an update | `apps/host_grants/contact_records.py`, `apps/host_grants/contact_records_api.py` |
| Write one person | `contacts.services.apply_host_record` |
| The site's declared scheme and retention | `site_needs.person_scheme_for`, `site_needs.retention_days_for` |
| Erase one person; the tombstone | `contacts.services.erase`, `ContactErasure`; a conversation `content_deletion.erase_session` |
| The site's retention | `retention.services._sweep_site_contacts` |
| What the agent is told | `apps/harness/caller_context.py::_contact` |
| Offering an agent to a site's contacts | `contact_api._for_contacts`, `published_profiles.contact_scoped` |
| The subject mode | `host_gateway._subject_context`, `client_identity.subject_statement`, `execution.subject_of`; the host's check `sdk/python/src/canopy_sdk/host/subject.py` |
