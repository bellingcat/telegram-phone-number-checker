# Durable queue for the local Telegram validator

## Objective

Keep the existing local form and job page while making every submitted number
durable, observable and resumable. A job must never silently lose an item or
turn a Telegram limitation into a negative result.

The implementation remains a local, single-account service. It does not move
work to another account when Telegram applies a limit.

## Current constraints and observed failure

The web app currently keeps job state in `.validator-jobs.json`, executes the
worker in a daemon thread, and derives job progress from an in-memory result
map. This is adequate for short batches but has weak crash recovery and no
durable per-item state. `contacts.importContacts` can return `retry_contacts`
because of a server-side limitation. Retrying that method for the same items
can leave a long batch looking stuck.

Telegram documents `contacts.resolvePhone` as a lookup that does not import a
contact. It must be rate limited to one request every three seconds, and the
same negative reply can represent no account or phone-lookup privacy. The
result therefore cannot be reported as definite absence.

## Product behaviour

The user keeps the current flow:

1. Paste company headers and numbers, then explicitly click `Поставить в очередь`.
2. Open the saved job and click `Запустить обработку`.
3. View progress and download CSV from the same job page.

The job page adds only operational detail:

- counts by final and non-final status;
- current phase (`импорт пакетами`, `прямая проверка`, `ожидание Telegram`);
- current and remaining count in the phase, observed speed and ETA;
- exact FloodWait countdown when Telegram provides one;
- `Пауза`, `Продолжить`, and `Отменить` controls; cancellation preserves all
  completed rows and labels untouched rows `CANCELLED`.

Results are exported at any time directly from `job_items`. The page table and
partial CSV include every input row, including duplicates under different
companies, with its current status, selected validation method and reason.

## State model

Replace JSON persistence with one local SQLite database, `.validator.db`,
using the Python standard library `sqlite3`. No server or new external runtime
is introduced.

### Tables

`jobs`

- `id`, `raw_input`, `state`, `phase`, `message`;
- `created_at`, `started_at`, `updated_at`, `completed_at`;
- `total_rows`, `completed_rows`, `paused_at`, `resume_after`;
- `import_batch_size`, `import_retry_count`, `last_error`.

`job_items`

- `id`, `job_id`, `ordinal` and company context (`company_name`,
  `company_link`);
- `raw_phone`, `normalized_phone`;
- `status`, `method`, `detail`, `attempts`, `next_attempt_at`;
- `lease_owner`, `lease_expires_at` for an in-flight request;
- Telegram fields (`telegram_id`, `username`, `name`, `telegram_link`);
- `started_at`, `completed_at`.

`job_events`

- `id`, `job_id`, `created_at`, `kind`, `message`, `processed`, `total`.

`worker_state`

- one row for the configured account: `account_key`, `lease_owner`,
  `lease_expires_at`, `next_import_at`, `next_direct_lookup_at`,
  `next_available_at`, `updated_at`.

`contact_cleanup`

- `id`, `job_id`, `telegram_user_id`, `state`, `attempts`,
  `next_attempt_at`, `last_error`, `created_at`, `completed_at`;
- uniqueness on `(job_id, telegram_user_id)`. Cleanup is independent of the
  validation item state: a failed delete never downgrades `FOUND` and never
  schedules another import for the same phone.

`schema_meta`

- `schema_version` and migration markers, including the checksum of a migrated
  JSON source.

`ordinal` is unique within a job. Several company rows may refer to the same
normalized phone; the worker performs one lookup and atomically applies its
outcome to every still-pending matching row.

The connection sets `PRAGMA journal_mode=WAL`, `foreign_keys=ON`,
`synchronous=FULL`, and `busy_timeout`. Before each claim, a process acquires
and renews the account lease using `BEGIN IMMEDIATE`. A second service process
cannot use the same Telegram session until that lease expires. Short database
transactions never contain network activity. The schema has indexes for ready
items, retry times and expired item leases. `jobs` also has `run_lease_owner`
and `run_lease_expires_at` so a stale `RUNNING` state is recoverable.

## State transitions

Job states are `QUEUED`, `RUNNING`, `WAITING_RATE_LIMIT`, `PAUSED`,
`CANCELLED`, `COMPLETED`, and `FAILED`.

An item is `PENDING`, `IN_PROGRESS`, `FOUND`, `FOUND_NO_USERNAME`,
`UNRESOLVABLE_OR_PRIVACY`, `INVALID_PHONE`, `RETRYABLE_ERROR`,
`API_ERROR_FINAL`, `DEFERRED_NOT_CHECKED`, or `CANCELLED`. Only
`FOUND`, `FOUND_NO_USERNAME`, `UNRESOLVABLE_OR_PRIVACY`, `INVALID_PHONE`,
`API_ERROR_FINAL`, `DEFERRED_NOT_CHECKED`, and `CANCELLED` contribute to
completed progress. The page reports both processed rows and confirmed
accounts; an inconclusive row is never presented as a missing account.

Temporary network/RPC errors enter `RETRYABLE_ERROR` with retry times of 30,
120 and 600 seconds. After three failures they become `API_ERROR_FINAL`.
`FloodWait` is not an error attempt: its server-supplied delay puts the account
and the affected job into `WAITING_RATE_LIMIT`.

Every state mutation is committed in one SQLite transaction. Before a request,
the worker persists `IN_PROGRESS`, its item lease, and the next allowed request
time; after a reply it persists results and an event in one transaction. On
restart, only expired unfinished item leases return to `PENDING`, increment
their attempt count and add a recovery event. A crash after sending a request
but before saving its reply can cause one repeat after recovery. Exactly-once
external delivery is therefore not promised, and no repeated item becomes a
negative result solely for that reason.

At startup and before every scheduler claim, one reconciliation transaction
releases an expired account lease, reclaims expired item leases, and converts a
stale `RUNNING` job to `WAITING_RATE_LIMIT` when `resume_after` is still in the
future or `QUEUED` otherwise. The scheduler claims it normally without a
browser action. A non-expired lease is never reclaimed by another process.

## Single-account worker

One durable worker owns the configured Telegram session and claims ready work
from SQLite. It never issues simultaneous Telegram requests. Its account lease
and global timing ledger apply to every job, including after a restart.

1. It takes up to ten unique `PENDING` valid numbers and calls
   `contacts.importContacts`.
2. Imported users become `FOUND` or `FOUND_NO_USERNAME`. Missing responses are
   recorded as `UNRESOLVABLE_OR_PRIVACY`, because privacy may hide an existing
   account.
3. Items returned in `retry_contacts` move to the `DIRECT_LOOKUP` phase. They
   are not re-imported in a loop.
4. Direct lookup calls `contacts.resolvePhone` one number at a time, with at
   least 3.1 seconds between calls. A positive response becomes `FOUND` or
   `FOUND_NO_USERNAME`; `PHONE_NOT_OCCUPIED` remains
   `UNRESOLVABLE_OR_PRIVACY` because Telegram uses it for private lookups too.
5. A `FloodWait` stores `WAITING_RATE_LIMIT`, the exact `resume_after`, and
   `worker_state.next_available_at`. It blocks all Telegram requests for the
   account, including other jobs. The scheduler resumes automatically after
   it. Waiting is wakeable, so pause or cancel takes effect without waiting for
   the full FloodWait timer.
6. Every successful import records imported contact IDs as `contact_cleanup`
   work. `DeleteContactsRequest` marks that work done; a cleanup failure is a
   visible retryable cleanup event, so interrupted runs do not silently leave
   contacts in the operator's address book. It cannot change a completed
   validation status or trigger import.

The import batch starts at ten and may reduce to five, then one after an import
batch returns any `retry_contacts`; the reduction applies only to later,
untouched import candidates and resets to ten for the next job. Returned
`retry_contacts` always use direct lookup and are never re-imported. The batch
size never increases above ten.

Pause stops new requests after the currently sent request returns and preserves
that result. Cancel does the same, then atomically labels all remaining
`PENDING`, expired `IN_PROGRESS`, and `RETRYABLE_ERROR` rows `CANCELLED`.

## Migration and recovery

On first start the application detects `.validator-jobs.json` and imports each
saved job in one transaction. Existing result maps, including completed rows
from the current 1,010-number job, become final item rows. Deferred rows become
`PENDING` for direct lookup and otherwise unprocessed rows become `PENDING` for
import. Rows intentionally stopped by the user (`skip_deferred`) become
`DEFERRED_NOT_CHECKED` and are never automatically resumed. Every original
input ordinal becomes exactly one item row. The JSON source is kept unchanged
and migration is recorded as
`JSON_MIGRATED` with its checksum.

Migration uses the following precedence for every original ordinal:

| Source condition | Migrated item state and method |
| --- | --- |
| invalid normalized phone | `INVALID_PHONE`, no Telegram method |
| `results_by_phone[phone].skipped` or `skip_deferred` plus deferred phone | `DEFERRED_NOT_CHECKED`, no automatic retry |
| stored successful `id` | `FOUND` or `FOUND_NO_USERNAME`, preserving Telegram fields |
| stored `No response` / privacy result | `UNRESOLVABLE_OR_PRIVACY` |
| stored `retry: true`, retry message, or deferred phone | `PENDING`, `DIRECT_LOOKUP` |
| stored FloodWait / `waiting_until` without a final item result | `PENDING`, method selected from the prior phase; job keeps `resume_after` |
| stored generic error | `RETRYABLE_ERROR`, with an immediate first retry |
| no stored result | `PENDING`, `IMPORT` |

The first matching row wins in that order. A job with `waiting_until` starts as
`WAITING_RATE_LIMIT` until that timestamp; otherwise it starts as `QUEUED`.

`schema_meta` versioning and source checksums make migration idempotent. If
migration or JSON parsing fails, the service does not delete or overwrite JSON
and starts in a visible diagnostic mode without a worker. The database is
backed up before every future schema migration.

## Security and retention

The server remains bound to `127.0.0.1`. Telegram credentials and session
files stay outside the database and are never served in HTML, CSV, logs or
events. Event logs contain counts and state changes, not phone numbers.

The database stores phone data locally because that is necessary for resume and
CSV export. A configurable retention command will remove completed jobs and
their rows; the initial default is no automatic deletion so ongoing local work
is not lost.

## Verification

Automated tests must cover:

- migration of existing JSON with completed and deferred entries;
- idempotent migration, `skip_deferred` preservation, and malformed JSON
  diagnostic mode;
- restart while importing, direct lookup and FloodWait;
- process lease conflict, expired-lease recovery, and concurrent HTTP actions;
- crash after claim and after simulated request reply;
- persistent import/direct lookup limiter across restart and account-wide
  FloodWait blocking;
- no duplicate Telegram request for duplicate normalized phone values;
- direct fallback for every `retry_contacts` item and a 3.1-second limiter;
- retryable error transition, bounded retries, and contact-cleanup failure;
- pause, resume and cancel without losing completed results or issuing a new
  request after the control is observed;
- partial and final CSV preserving company context and all input rows;
- HTTP smoke: explicit queue confirmation, queued job, start, progress,
  pause/resume/cancel and CSV endpoints.

Manual live validation uses a deliberately small user-authorized sample. It
records only aggregate counts and statuses, never credentials or raw numbers.
