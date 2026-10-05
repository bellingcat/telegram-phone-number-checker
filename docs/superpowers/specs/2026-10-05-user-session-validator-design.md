# User-session validator redesign

## Purpose

Run all validation through one Telegram **user-account** session that the operator authorizes in the local browser. The service must remain localhost-only and preserve the durable SQLite queue and session volume.

## Observed problem

The current `TelethonGateway` opens and closes a Telethon connection for every import, direct lookup, and cleanup request. The durable queue also moves `retry_contacts` to `resolvePhone`; that request is privacy-ambiguous and produced `UNRESOLVABLE_OR_PRIVACY` rows that must not be interpreted as account absence.

## Design

### Persistent user-session owner

A dedicated session owner runs one asyncio loop and one Telethon client for the authenticated user account. Validator requests are submitted to that owner serially. It reconnects only after a transport failure and reports an unavailable session explicitly to the queue instead of falling back to terminal input.

The existing browser authorization flow continues to create the Telethon session in `/data/telegram-session`. No bot token, bot account, additional account, or desktop UI automation is introduced.

### Rate-aware validation

Imports continue to use `contacts.ImportContactsRequest` with an adaptive batch size of 10, 5, or 1. Telegram `FloodWait` remains authoritative: its delay is persisted and shown in the UI.

When Telegram returns `retry_contacts`, the item remains an import candidate with a persisted retry time. It is not immediately sent to `resolvePhone`. Direct lookup remains available only as a separately labelled optional API probe, and its output stays privacy-ambiguous.

### Result model and interface

The interface distinguishes three categories:

- `FOUND_API`: Telegram returned a user in the API response.
- `PENDING_RETRY`: Telegram asked to retry later or imposed a rate delay.
- `UNRESOLVABLE_OR_PRIVACY`: the API did not make the number discoverable; it is not an absence claim.

Manual confirmation remains additive: the operator can mark a row `MANUAL_CHAT_CONFIRMED` only after personally opening a profile. This does not overwrite the API result.

### Migration of the paused job

The current paused job remains untouched until the operator selects a migration action. That action returns only `UNRESOLVABLE_OR_PRIVACY` rows whose method is `DIRECT_LOOKUP` to `PENDING_RETRY` for one user-session import retry, retaining the prior API result in the event history. Rows already returned by the import API remain unchanged.

### Reliability and testing

Tests cover reuse of a single client across consecutive validator operations, reconnect after a simulated transport failure, preservation of FloodWait, retry-contact routing without direct lookup, result labels, and migration of the current privacy-ambiguous rows. Docker smoke checks the local UI and durable session path.

## Limits

The persistent user session removes connection overhead and avoids the current false-negative fallback. It cannot make Telegram return records that Telegram withholds from that authenticated account, and it will not bypass server FloodWait.
