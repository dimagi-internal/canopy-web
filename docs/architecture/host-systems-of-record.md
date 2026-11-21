# Host sites are the systems of record for their people

A host site (Connect Labs, HQ, ace-web, …; [host-sites.md](host-sites.md)) that brings people to canopy stays the **system of record** for them. canopy keeps, per person, only:

- an **anchor**: the `Contact` (`apps/contacts/models.py`), keyed by the site and the site's own id for the person (`app`, `external_id`), and its `Person`, keyed by the site's signer and that id. Never merged with another site's person;
- a **pointer** to where the host's record lives: `Contact.record_uri`, `<site.person_scheme>://<id>`;
- a **cache the host owns**: `Contact.host_profile` (with `host_profile_version`, `host_profile_signed_at`), a few labels an agent may see on every turn (stage, language, programme, consent), never history;
- the **working sessions** of its conversations, under retention.

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

## The agent sees who the person is at the host

The caller envelope's `contact` block (`apps/harness/caller_context.py::_contact`) carries `external_id` (for a site's contact), `site`, `record_uri`, `host_profile` and `person_id`. The canopy plugin prints them as data about the person, never as instructions.

## Code map

| Question | Where |
|---|---|
| The fields | `apps/contacts/models.py` (`Contact.record_uri`, `host_profile`, …) |
| The wire (both halves) | `sdk/python/src/canopy_sdk/contacts.py`; the host's client `sdk/python/src/canopy_sdk/host/contacts.py` |
| Verify and apply an update | `apps/host_grants/contact_records.py`, `apps/host_grants/contact_records_api.py` |
| Write one person | `contacts.services.apply_host_record` |
| The site's declared scheme | `site_needs.person_scheme_for` |
| What the agent is told | `apps/harness/caller_context.py::_contact` |
