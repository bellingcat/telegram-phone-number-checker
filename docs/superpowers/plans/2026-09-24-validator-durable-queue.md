# Durable Validator Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace JSON job snapshots with a restart-safe SQLite queue while preserving the current local form, job page and CSV workflow.

**Architecture:** `queue_store.py` owns SQLite schema, migration, transactions and leases. `validator_worker.py` claims ready work from that store and serializes Telegram calls; `web.py` renders store snapshots and sends user controls to it. Batch imports remain limited to ten numbers, while `retry_contacts` move to persisted direct lookup at least 3.1 seconds apart.

**Tech Stack:** Python 3.9+, standard-library `sqlite3` and `http.server`, asyncio, Telethon, pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-validator-durable-queue-design.md`

## Global Constraints

- Bind only to `127.0.0.1`; never serve credentials, sessions or raw phone numbers in events.
- One configured Telegram account and one database-backed account lease; no account failover or rotation.
- SQLite uses WAL, foreign keys, full synchronous mode, busy timeout and no network operation inside a transaction.
- Preserve every input ordinal and its company context in table and CSV, including duplicate normalized phones.
- `UNRESOLVABLE_OR_PRIVACY` is terminal and inconclusive, never a definite absence.
- Direct lookups wait at least 3.1 seconds across restarts; FloodWait blocks the complete account.

## Review Focus

- A process crash after a Telegram request must not mark that number absent or discard it.
- A second local server process must not take the same session while the first worker lease is live.
- User cancellation during a FloodWait must prevent later requests while preserving prior results.
- JSON migration of the existing 1,010-row task must retain all original rows and intentional deferred stops.
- CSV generated during work must contain all rows, not only results finished at the time of export.

---

### Task 1: SQLite store and schema

**Files:**
- Create: `telegram_phone_number_checker/queue_store.py`
- Create: `tests/test_queue_store.py`

**Interfaces:**
- Produces `QueueStore(path: Path, now: Callable[[], float] = time.time)`.
- Produces `QueueStore.create_job(raw: str) -> str`, `snapshot(job_id: str) -> dict`, and `rows(job_id: str) -> list[dict[str, str]]`.

- [ ] **Step 1: Write failing schema and persistence tests**

```python
def test_store_creates_every_input_ordinal_and_restores_it(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("ООО Тест\n+79991234567\n+79991234567\nне номер")
    assert store.snapshot(job_id)["total"] == 3
    assert [row["phone"] for row in store.rows(job_id)] == [
        "+79991234567", "+79991234567", "не номер"
    ]
    assert QueueStore(tmp_path / "validator.db").snapshot(job_id)["total"] == 3
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_queue_store.py::test_store_creates_every_input_ordinal_and_restores_it -q`

Expected: FAIL because `queue_store` does not exist.

- [ ] **Step 3: Implement minimal schema and read/write API**

```python
class QueueStore:
    def create_job(self, raw: str) -> str: ...
    def snapshot(self, job_id: str) -> dict: ...
    def rows(self, job_id: str) -> list[dict[str, str]]: ...
```

Create `jobs`, `job_items`, `job_events`, `worker_state`, `contact_cleanup`, and `schema_meta`; enforce `UNIQUE(job_id, ordinal)`, configure SQLite pragmas and add ready/retry/expired-lease indexes.

- [ ] **Step 4: Run targeted tests**

Run: `pytest tests/test_queue_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram_phone_number_checker/queue_store.py tests/test_queue_store.py
git commit -m "feat: add durable validator queue store"
```

### Task 2: JSON migration and recovery reconciliation

**Files:**
- Modify: `telegram_phone_number_checker/queue_store.py`
- Modify: `tests/test_queue_store.py`

**Interfaces:**
- Produces `QueueStore.migrate_json(path: Path) -> None` and `QueueStore.reconcile(owner: str) -> None`.

- [ ] **Step 1: Write failing migration tests**

```python
def test_migration_preserves_found_deferred_and_unprocessed_ordinals(tmp_path):
    source = tmp_path / "jobs.json"
    source.write_text(json.dumps({"jobs": {"job": legacy_job}}), encoding="utf-8")
    store = QueueStore(tmp_path / "validator.db")
    store.migrate_json(source)
    assert [row["status"] for row in store.rows("job")] == [
        "FOUND", "DEFERRED_NOT_CHECKED", "PENDING"
    ]
    store.migrate_json(source)
    assert len(store.rows("job")) == 3
```

- [ ] **Step 2: Run migration tests to verify failure**

Run: `pytest tests/test_queue_store.py -k migration -q`

Expected: FAIL because migration and checksum handling are absent.

- [ ] **Step 3: Implement explicit precedence mapping and reconciliation**

Map legacy `skipped`, successful IDs, no-response privacy, retry/deferred, FloodWait, generic error, invalid and absent result in the exact spec order. Store a source checksum in `schema_meta`; keep source JSON unchanged. Reconcile expired worker/job/item leases and move stale running work to `QUEUED` or `WAITING_RATE_LIMIT` without browser input.

- [ ] **Step 4: Add malformed JSON and lease recovery tests**

```python
def test_malformed_json_leaves_store_in_diagnostic_mode(tmp_path): ...
def test_reconcile_reclaims_only_expired_item_lease(tmp_path): ...
```

- [ ] **Step 5: Run targeted tests and commit**

Run: `pytest tests/test_queue_store.py -q`

Expected: PASS.

```bash
git add telegram_phone_number_checker/queue_store.py tests/test_queue_store.py
git commit -m "feat: migrate validator jobs into sqlite"
```

### Task 3: Leases, rate ledger and user controls

**Files:**
- Modify: `telegram_phone_number_checker/queue_store.py`
- Modify: `tests/test_queue_store.py`

**Interfaces:**
- Produces `claim_next(owner: str) -> ClaimedWork | None`, `record_flood_wait(seconds: int)`, `pause(job_id: str)`, `resume(job_id: str)`, and `cancel(job_id: str)`.

- [ ] **Step 1: Write failing ownership, limiter and cancellation tests**

```python
def test_live_account_lease_excludes_second_process(tmp_path): ...
def test_direct_lookup_deadline_survives_store_reopen(tmp_path): ...
def test_cancel_marks_unclaimed_rows_without_changing_found_rows(tmp_path): ...
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_queue_store.py -k 'lease or deadline or cancel' -q`

Expected: FAIL because claim/control methods do not exist.

- [ ] **Step 3: Implement transactional claim and control operations**

Use `BEGIN IMMEDIATE` for account/job/item claim, renew owner lease, respect account-wide `next_available_at`, and persist `next_import_at`/`next_direct_lookup_at` before a call. Pause blocks claims; cancel atomically terminalizes allowed non-final rows and emits an event.

- [ ] **Step 4: Run targeted tests and commit**

Run: `pytest tests/test_queue_store.py -q`

Expected: PASS.

```bash
git add telegram_phone_number_checker/queue_store.py tests/test_queue_store.py
git commit -m "feat: add validator queue leases and controls"
```

### Task 4: Durable Telegram worker

**Files:**
- Create: `telegram_phone_number_checker/validator_worker.py`
- Modify: `telegram_phone_number_checker/main.py`
- Create: `tests/test_validator_worker.py`

**Interfaces:**
- Produces `ValidatorWorker(store: QueueStore, gateway: TelegramGateway, owner: str)` and `run_once() -> bool`.
- `TelegramGateway.import_phones(phones: list[str]) -> dict[str, dict]`, `resolve_phone(phone: str) -> dict`, and `cleanup_contacts(ids: list[int]) -> None`.

- [ ] **Step 1: Write failing worker tests**

```python
def test_retry_contacts_move_to_persisted_direct_lookup(store, gateway): ...
def test_floodwait_blocks_another_job_for_the_same_account(store, gateway): ...
def test_cleanup_failure_does_not_downgrade_found_item(store, gateway): ...
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_validator_worker.py -q`

Expected: FAIL because worker module does not exist.

- [ ] **Step 3: Implement gateway adapter and worker state machine**

Adapt existing Telethon calls. Import no more than ten untouched phones; send `retry_contacts` directly to direct lookup without re-import. Persist every response, record cleanup work for imported contacts, use a wakeable rate-limit wait, and release/renew leases correctly. The direct limiter is at least 3.1 seconds and persists before a request.

- [ ] **Step 4: Run worker tests and commit**

Run: `pytest tests/test_validator_worker.py tests/test_batch_lookup.py -q`

Expected: PASS.

```bash
git add telegram_phone_number_checker/validator_worker.py telegram_phone_number_checker/main.py tests/test_validator_worker.py
git commit -m "feat: process validator queue durably"
```

### Task 5: Web integration and partial CSV

**Files:**
- Modify: `telegram_phone_number_checker/web.py`
- Modify: `telegram_phone_number_checker/web_service.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- `ValidatorApplication` uses `QueueStore` and `ValidatorWorker` instead of JSON state.
- Add POST routes `/jobs/<id>/pause`, `/jobs/<id>/resume`, `/jobs/<id>/cancel`.

- [ ] **Step 1: Write failing HTTP tests**

```python
def test_job_page_shows_phase_counts_and_partial_csv(tmp_path): ...
def test_pause_resume_cancel_routes_change_only_the_target_job(tmp_path): ...
def test_csv_contains_every_input_row_before_completion(tmp_path): ...
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_web.py -k 'partial or pause or cancel' -q`

Expected: FAIL because routes and store-backed rendering are absent.

- [ ] **Step 3: Integrate store snapshots without changing the entry flow**

Keep explicit queue confirmation, input preservation, company parsing, `/jobs/<id>`, `/download.csv`, and local-only server binding. Render phase, confirmed-account count, terminal/inconclusive totals and exact wait countdown from the store. Use one worker thread that polls the durable store; make pause/cancel wake its waiting condition.

- [ ] **Step 4: Run HTTP tests and commit**

Run: `pytest tests/test_web.py tests/test_web_service.py -q`

Expected: PASS.

```bash
git add telegram_phone_number_checker/web.py telegram_phone_number_checker/web_service.py tests/test_web.py
git commit -m "feat: expose durable validator queue in web ui"
```

### Task 6: Migration smoke and final verification

**Files:**
- Modify: `README.md`
- Modify: `tests/test_web.py`

- [ ] **Step 1: Write a migration-to-HTTP smoke test**

```python
def test_legacy_json_job_is_visible_and_resumable_from_web(tmp_path): ...
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_web.py::test_legacy_json_job_is_visible_and_resumable_from_web -q`

Expected: FAIL before final migration wiring.

- [ ] **Step 3: Document the one-time automatic migration**

Explain that startup imports existing `.validator-jobs.json` into `.validator.db`, retains JSON as backup, and keeps Telegram credentials/session data outside the database.

- [ ] **Step 4: Run all verification**

Run: `pytest -q && python -m compileall -q telegram_phone_number_checker && git diff --check`

Expected: PASS, clean compilation, and no whitespace errors.

- [ ] **Step 5: Smoke the local server without Telegram calls and commit**

Run: start `telegram-validator-web` on a free local port, GET `/check`, POST a saved job with explicit queue confirmation, GET its page, and GET its partial CSV.

```bash
git add README.md tests/test_web.py
git commit -m "docs: explain validator queue recovery"
```
