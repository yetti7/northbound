# Hardcover setup and operations

Hardcover integration is optional. Northbound can accept manual book submissions
without it. This guide covers integration setup and operating constraints; use
[Self-hosting](SELF_HOSTING.md) for installation and
[Backup and restore](BACKUP_RESTORE.md) for recovery procedures.

## Two connections, two owners

| Connection | Purpose | Method |
| --- | --- | --- |
| Group | Shared catalog/search/metadata, including submission and BOTM lookup | Group-owned scoped API key with `read:catalog:data` and `read:catalog:search` |
| Reader | That Reader's Personal TBR catalog lookup and optional personal library sync | OAuth recommended when configured; personal scoped PAT/API key remains supported |

Reader work never falls back to a Group credential. Group connections cannot sync
Readers' libraries. Platform Owner setup cannot impersonate Readers, reveal their
tokens or enable their consent. Group credential editing requires the existing
Group Owner/Moderator/Platform Owner authority; Host/Reader status alone does not grant it.

## Configure this installation's OAuth app

Each installation brings its own Developer App; Northbound has no shared hosted
OAuth client. The Platform Owner configures it.

1. Preserve production `DJANGO_SECRET_KEY` and the installation encryption key
   described in [configuration](SELF_HOSTING.md#preserve-the-encryption-key).
   Keep `DJANGO_DEBUG=0`.
2. Set `NORTHBOUND_URL` to the public origin, e.g. `https://northbound.example.org`,
   without a path, query, fragment or embedded credentials. OAuth requires HTTPS
   except for loopback development. Configure the [proxy](REVERSE_PROXY.md) consistently.
3. Open **Platform Administration → Settings → Hardcover OAuth** and create a
   [Hardcover Developer App](https://hardcover.app/account/developer-apps/new).
   Select **Web app (server-side)**: confidential client, client secret + PKCE.
   Leave **Device Authorization Grant OFF**.
4. Copy Northbound's read-only Website URL and Redirect URI exactly. They derive
   from `NORTHBOUND_URL`, not the request Host. The callback suffix is exactly
   `/account/hardcover/oauth/callback/`, including the trailing slash. For this example:
   `https://northbound.example.org/account/hardcover/oauth/callback/`.
5. Allow exactly `read:catalog`, `read:library`, `write:library`. Do not add
   unrestricted, account, review or catalog-editing permissions.
6. Save the Client ID and one-time Client Secret directly in Northbound and enable
   OAuth. The secret is encrypted and never redisplayed; leaving it blank on later
   edits preserves the existing secret. Save creation/rotation secrets securely,
   never in public logs, issues or documentation.
7. Confirm **Configured**. This means local configuration/decryption checks passed,
   not that a live provider connection was tested. Readers connect themselves from
   My Account. Disabled/unconfigured/needs-attention OAuth does not replace or
   disable the separate Group and Reader API-key methods.

The provider's [authorization-server metadata](https://api.hardcover.app/.well-known/oauth-authorization-server)
is a first-party reference. Northbound uses authorization code with PKCE S256 and
confidential-client authentication. Operators do not need to perform manual token
exchanges or paste tokens into diagnostic commands.

## Reader consent and synchronization

A Reader may connect using OAuth, or **Advanced: scoped API key** with
`read:catalog`, `read:library`, `write:library`. The PAT connection check tests
catalog access; it does not inspect PAT scopes or expiry or prove library-write
permissions. An expired/restricted key may need replacement.

**Connection is not consent.** Both **Sync completed books** and **Sync Northbound
completion dates** default OFF. The Reader enables their own preferences;
completion-date consent depends on completed-book consent.

Only new eligible approval transitions occurring after completed-book consent is
already enabled may enter the pipeline. Turning consent ON never queues previously
approved submissions. Connecting, reconnecting, restarting and **Sync Now** do not
scan old reading history. There is no historical backfill, bulk export or Hardcover
history import. Northbound approval and scoring do not depend on provider availability.

For an eligible matched book, sync reads the current library state, creates a missing
library entry or marks an existing entry Read. Already-Read entries are not duplicated.
Date consent allows a distinct read occurrence for that submission. Distinct approved
rereads remain distinct occurrences; later corrections target only the stored mapped
occurrence, not unrelated same-book/date history. Catalog, rating, review and deletion
writes are not supported.

**Sync Now** wakes eligible work already in the queue; it neither creates historical
work nor performs a provider exchange inline. My Account displays pending, retry,
reconnect and reconciliation/recovery status.

## Failures, reconnect and disconnect

Transient failures use bounded automatic retries; permanent errors stop.
The worker normally polls every 30 seconds, taking up to 20 events per batch.
Transient retry delays are 1, 2, 4, 8 and 16 minutes; the sixth failed attempt stops
for recovery review. Do not launch additional worker/application containers.

A possibly completed remote write is not blindly repeated when the result is
uncertain. Missing/mismatched saved read identifiers and ambiguous creates remain
blocked. Reconnect and Sync Now do not release ambiguous, exhausted or restored work.
V1 does not provide an automatic resolver for unprovable outcomes.

OAuth refresh happens automatically when needed. Failed or ambiguous refresh can
require reconnect instead of reusing a potentially spent token. A repaired connection
can resume eligible existing credential-blocked work when consent remains enabled;
it does not authorize new history export.

Disconnect disables the local connection and consent before best-effort OAuth
revocation. If revocation fails, also remove the authorization in Hardcover's
Authorized Apps. Already-in-flight remote requests cannot be recalled. Stored
provider mappings/history are not deleted by disconnect.

## Origin, secret and logging changes

Changing `NORTHBOUND_URL` requires updating the Developer App's exact Website and
Redirect URI and the saved Northbound configuration. A Client ID change requires
Readers to reconnect. Rotating the Client Secret within the same app does not
automatically discard Reader authorizations; reconnect if the provider rejects them.

Protected credentials are encrypted at rest. Keep the original installation key,
as explained in [encryption configuration](SELF_HOSTING.md#preserve-the-encryption-key).
Never post OAuth codes/state, tokens, Client Secrets, raw provider responses, debug
tracebacks or browser HAR captures in public issues.

Credential-bearing HTTP calls do not follow redirects. Production web access logs
omit query strings and referrers; configure every upstream logger to omit callback
queries, Authorization headers and request bodies too. See [Reverse proxies](REVERSE_PROXY.md).

## Restores and operational verification

Follow [Backup and restore](BACKUP_RESTORE.md), including the mandatory forced
safeguard after external SQLite/PostgreSQL restores. The original key recovers
ciphertext usability, but restored OAuth authorization still requires reconnect.
Unfinished/blocked work is quarantined, preserving provider mappings and attempt
history. Restore cannot safely undo remote writes or refresh rotations.

For an installation acceptance check, use one deliberately authorized test Reader:
connect with consent OFF, enable consent explicitly, approve one new eligible book,
and verify the intended library/date result. Check an already-Read book and a distinct
reread without exporting old history. Test restoration only in an isolated environment
with provider writes prevented. Local checks are not a substitute for validating
your deployed container, database and provider integration.

Container `/health/` is database-only. Hardcover outages do not make it fail, and
a healthy web container does not prove Reader synchronization succeeded.
