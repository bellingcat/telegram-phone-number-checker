# Persistent User-Session Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate batches through one browser-authorized Telegram user account with durable, rate-aware imports and truthful privacy-limited results.

**Architecture:** A single `UserSessionService` owns one asyncio loop, one Telethon user client, and the browser authorization sequence. The durable SQLite queue remains authoritative for claims, FloodWait, retry state, immutable per-item attempt history, cleanup, and recovery. Imports and cleanup run serially through that owner; unknown-outcome requests are never replayed automatically.

**Tech Stack:** Python 3.12, Telethon 1.33, SQLite, stdlib `asyncio`/`threading`/`http.server`, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-05-user-session-validator-design.md`

## Global Constraints

- Use one browser-authorized Telegram **user account** at `/data/telegram-session`; no bot, additional account, or Telegram Desktop UI automation.
- Keep service access on `127.0.0.1` and retain queue/session data in `tg-validatorForSelf_data`.
- Never write OTP, 2FA passwords, or raw authorization errors containing them to SQLite, events, CSV, logs, or HTML.
- Preserve server-supplied `FloodWait` exactly. Do not parallelize around or bypass it.
- `UNRESOLVABLE_OR_PRIVACY` is API ambiguity, never proof of account absence.
- Preserve the current paused job until the operator explicitly chooses migration; no automatic restart after migration.

## Review Focus

- Missing/revoked user session pauses affected jobs and performs no terminal prompt or repeated API request.
- A transport failure after dispatch produces a non-claimable unknown-outcome review state, never an automatic replay of `ImportContactsRequest` or cleanup.
- Legacy direct-lookup rows, including pending rows, cannot run the obsolete fallback after migration.
- Cleanup work cannot starve imports or double Telegram RPC volume under normal throughput.
- Stop/start during an in-flight request keeps a recoverable lease and does not leak OTP/2FA secrets.

---

## File Structure

- Create `telegram_phone_number_checker/user_session.py`: single session owner, browser authorization, serialized API execution, handoff, reconnect and shutdown.
- Modify `telegram_phone_number_checker/durable_web.py`: use the session owner, render authorization/result state, migration action, and deterministic shutdown.
- Modify `telegram_phone_number_checker/validator_worker.py`: classify request outcomes, defer import retries, and schedule cleanup after imports.
- Modify `telegram_phone_number_checker/queue_store.py`: attempt history, `PENDING_RETRY`, non-claimable unknown-outcome review, adaptive import state, paused-job migration, summary counts, and authorization pause.
- Modify `telegram_phone_number_checker/web.py`: wire application shutdown and SIGTERM-safe stop handling.
- Create `tests/test_user_session.py`; modify `tests/test_validator_worker.py`, `tests/test_queue_store.py`, `tests/test_web.py`, and `README.md`.

### Task 1: One session owner for authorization and validation

**Files:**
- Create: `telegram_phone_number_checker/user_session.py`
- Modify: `telegram_phone_number_checker/durable_web.py:18-158,161-195`
- Modify: `telegram_phone_number_checker/web.py:760-779`
- Create: `tests/test_user_session.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: `UserSessionService.start_authorization(phone: str) -> bool`, `submit_code(code: str) -> bool`, `submit_password(password: str) -> bool`, `snapshot() -> dict[str, str]`, `import_phones(phones: list[str]) -> dict[str, dict]`, `resolve_phone(phone: str) -> dict`, `cleanup_contacts(user_ids: list[int]) -> None`, `close(timeout: float = 5.0) -> None`.
- Produces: `UserSessionUnavailableError`, `RequestNotDispatchedError`, and `RequestOutcomeUnknownError`.
- Consumes: existing Telethon request helpers in `main.py`, the durable worker gateway protocol, and browser form routes.

- [ ] **Step 1: Write failing service tests**

```python
def test_one_authorized_client_serves_two_import_batches_and_cleanup():
    service = UserSessionService(client_factory=fake_factory)
    service.import_phones(["+79990000001"])
    service.import_phones(["+79990000002"])
    service.cleanup_contacts([1])
    assert fake_factory.created == 1
    assert fake_client.connect_calls == 1


def test_authorization_handoff_keeps_same_client_for_first_import():
    service.start_authorization("+79991112233")
    service.submit_code("12345")
    assert wait_until(lambda: service.snapshot()["state"] == "AUTHORIZED")
    service.import_phones(["+79990000001"])
    assert fake_factory.created == 1


def test_pre_dispatch_failure_recreates_authorized_client_for_later_retry():
    with pytest.raises(RequestNotDispatchedError):
        service.import_phones(["+79990000001"])
    service.import_phones(["+79990000001"])  # later durable retry
    assert fake_factory.created == 2
    assert fake_factory.clients[1].authorized is True


def test_revoked_session_raises_user_session_unavailable_without_upstream_login(monkeypatch):
    with pytest.raises(UserSessionUnavailableError):
        service.import_phones(["+79990000001"])
    assert upstream.login_calls == 0
```

Add a shutdown test that starts one blocking operation, calls `close()`, and proves the operation returns a recoverable exception and the client disconnects once.

- [ ] **Step 2: Run the new service tests to verify they fail**

Run: activate the project virtual environment, then `python -m pytest -q tests/test_user_session.py`

Expected: FAIL because `UserSessionService` does not exist.

- [ ] **Step 3: Implement the unified session owner**

Run one asyncio loop in one background thread. The service owns the only Telethon client for both phone/OTP/2FA and validation. Browser authorization uses `send_code_request`, `sign_in`, and optional password on that same loop; it exposes `AUTHORIZED` only after the client remains connected and `is_user_authorized()` succeeds. Validation operations submit coroutines serially to that loop and never call `upstream.login` or `input()`.

On a connect failure before any request dispatch, invalidate and dispose the broken client, then raise `RequestNotDispatchedError`; the service does not replay the operation. On the next due durable claim, it must create/connect one fresh client and verify `is_user_authorized()` before dispatch. On a disconnect/error after the coroutine is dispatched, raise `RequestOutcomeUnknownError`; do not replay it in the service. `close()` sets a stop flag, rejects new work, waits up to its timeout for the active operation, disconnects the client, and stops the loop.

- [ ] **Step 4: Wire service lifecycle into the application**

Replace `TelegramAuthorization` plus short-lived `TelethonGateway` with a shared `UserSessionService`; retain the existing browser endpoints and dependency injection for tests. Add `DurableValidatorApplication.close()`. In `web.main()`, install a SIGTERM handler that starts a short helper thread to call `server.shutdown()`; never invoke `shutdown()` on the `serve_forever()` thread. Use `finally` after `serve_forever()` returns to call application close after the server stops accepting requests. A successful browser authorization leaves jobs in `WAITING_AUTHORIZATION`; it never submits work itself. The later explicit resume path is enabled only for an `AUTHORIZED` service snapshot.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest -q tests/test_user_session.py tests/test_web.py`

Expected: PASS.

```bash
git add telegram_phone_number_checker/user_session.py telegram_phone_number_checker/durable_web.py telegram_phone_number_checker/web.py tests/test_user_session.py tests/test_web.py
git commit -m "feat: own Telegram user session persistently"
```

### Task 2: Durable request outcome, import retry, and cleanup scheduling

**Files:**
- Modify: `telegram_phone_number_checker/queue_store.py:466-523,536-622,624-742,744-924`
- Modify: `telegram_phone_number_checker/validator_worker.py:16-175`
- Modify: `tests/test_queue_store.py`
- Modify: `tests/test_validator_worker.py`

**Interfaces:**
- Produces: `QueueStore.record_item_attempt(...) -> None`, `defer_import_retries(claims: list[dict], reason: str) -> None`, `defer_unknown_outcome(claims: list[dict], method: str) -> None`, `pause_for_authorization(claims: list[dict]) -> None`, `record_import_batch_success(job_id: str) -> None`, and `claim_cleanup_batch(owner: str, limit: int) -> dict | None`.
- Produces: claimable nonterminal row status `PENDING_RETRY` and non-claimable `UNKNOWN_OUTCOME_REVIEW`; ready SQL accepts only `PENDING_RETRY` when its persisted `next_attempt_at` is due.

- [ ] **Step 1: Write failing worker/store tests**

```python
def test_retry_contacts_return_to_import_queue_without_resolve_phone():
    worker.run_once()
    assert gateway.resolve_calls == []
    assert store.rows(job_id)[0]["status"] == "PENDING_RETRY"
    assert store.rows(job_id)[0]["method"] == "IMPORT"


def test_unknown_outcome_after_dispatched_import_is_not_replayed():
    worker.run_once()
    assert gateway.import_calls == 1
    assert store.rows(job_id)[0]["status"] == "UNKNOWN_OUTCOME_REVIEW"
    assert store.events(job_id)[-1]["kind"] == "UNKNOWN_OUTCOME"
    assert worker.run_once() is False


def test_revoked_session_pauses_job_without_incrementing_normal_retry_count():
    worker.run_once()
    assert store.snapshot(job_id)["state"] == "WAITING_AUTHORIZATION"
```

Add tests for a 230-second `FloodWait`, five successful batches restoring 1 → 5 then 5 → 10, cancellation of `PENDING_RETRY`, five ready import batches running before one cleanup batch, a paused job with pending cleanup issuing no Telegram request, and revoke → `WAITING_AUTHORIZATION` → browser login → explicit resume → one import request.

- [ ] **Step 2: Run those tests to verify they fail**

Run: `python -m pytest -q tests/test_validator_worker.py tests/test_queue_store.py -k 'retry or unknown or authorization or cleanup or flood or batch'`

Expected: FAIL because retry contacts currently enter `DIRECT_LOOKUP` and cleanup has strict priority.

- [ ] **Step 3: Implement safe outcome routing**

Create `job_item_attempts` before routing any request outcome, with item id, timestamp, method, outcome, detail, and source; append immutable records for terminal API results, retries, FloodWait, unknown outcomes, and migrations. Add `PENDING_RETRY` to claim, duplicate, pause, cancel, and recovery predicates. For `retry_contacts`, keep `method='IMPORT'`, set retry delays to 60 seconds, then 300 seconds, then 900 seconds for all later retries, and append an `IMPORT_RETRY` attempt record. On `RequestOutcomeUnknownError`, set non-claimable `UNKNOWN_OUTCOME_REVIEW`, append an `UNKNOWN_OUTCOME` record, and require an explicit operator requeue action before any new import. On `RequestNotDispatchedError`, use the ordinary recoverable path. On `UserSessionUnavailableError`, release claims and set the job `WAITING_AUTHORIZATION` without increasing ordinary failure counters.

Add `import_success_streak` to `jobs`; reset it on retry, unknown outcome, or FloodWait. Five retry-free import batches restore batch size from 1 → 5 or 5 → 10. Persist all server `FloodWait` values exactly and reset the streak.

Replace cleanup-first behavior with an import budget of five completed import batches before one `claim_cleanup_batch` call; `claim_cleanup_batch` must join `jobs` and select only `QUEUED` or `RUNNING` jobs. Cleanup remains durable, serial, and subject to the same FloodWait handling.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest -q tests/test_validator_worker.py tests/test_queue_store.py`

Expected: PASS.

```bash
git add telegram_phone_number_checker/queue_store.py telegram_phone_number_checker/validator_worker.py tests/test_queue_store.py tests/test_validator_worker.py
git commit -m "fix: preserve safe import retries"
```

### Task 3: Paused-job migration and explicit unknown-outcome review

**Files:**
- Modify: `telegram_phone_number_checker/queue_store.py:50-120,291-348,849-924`
- Modify: `telegram_phone_number_checker/durable_web.py:237-300,380-470`
- Modify: `tests/test_queue_store.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: the `job_item_attempts` schema and `record_item_attempt` created in Task 2.
- Produces: `item_attempts(job_id: str, item_id: str) -> list[dict]`, `requeue_legacy_direct_rows(job_id: str) -> int`, and `requeue_unknown_outcomes(job_id: str) -> int`.
- Produces: `POST /jobs/<job_id>/requeue-legacy-direct` and `POST /jobs/<job_id>/requeue-unknown-outcomes`, accepted only for a paused job with no active claims.

- [ ] **Step 1: Write failing migration/audit tests**

```python
def test_migration_requeues_legacy_pending_and_privacy_direct_rows_with_history():
    moved = store.requeue_legacy_direct_rows(job_id)
    assert moved == 2
    assert pending_direct["status"] == "PENDING_RETRY"
    assert privacy_direct["method"] == "IMPORT"
    assert store.item_attempts(job_id, privacy_direct["_item_id"])[-1]["outcome"] == "UNRESOLVABLE_OR_PRIVACY"


def test_migration_keeps_manual_confirmation_and_api_matches_unchanged():
    assert store.requeue_legacy_direct_rows(job_id) == 0
```

Add page tests for both migration actions only when paused/no claims, including an unknown-outcome review row; add HTTP tests proving a running or in-progress job moves no row.

- [ ] **Step 2: Run migration tests to verify they fail**

Run: `python -m pytest -q tests/test_queue_store.py tests/test_web.py -k 'legacy_direct or item_attempts'`

Expected: FAIL because immutable item history and migration do not exist.

- [ ] **Step 3: Implement audit-safe migration**

Use the immutable attempt history created in Task 2. Retain `api_status` as the current last API outcome, but do not treat it as history.

For an eligible paused job with zero active claims, migrate every legacy `DIRECT_LOOKUP` row in `PENDING`, `RETRYABLE_ERROR`, or `UNRESOLVABLE_OR_PRIVACY` to `PENDING_RETRY` + `IMPORT` + `next_attempt_at=now+60`. Exclude rows with `MANUAL_CHAT_CONFIRMED` and API-confirmed rows. Append a row-level migration attempt and job-level summary event. Separately, move `UNKNOWN_OUTCOME_REVIEW` rows to `PENDING_RETRY` only through `requeue_unknown_outcomes`; preserve their unknown-outcome attempt history. Do not auto-resume.

- [ ] **Step 4: Render and verify the operator action**

Expose `Вернуть старые прямые проверки в очередь импорта` only for an eligible paused job. If a claim is still in progress, show the exact reason and no action. On success, display the moved count and retain a CSV-downloadable current status.

Run: `python -m pytest -q tests/test_queue_store.py tests/test_web.py`

Expected: PASS.

```bash
git add telegram_phone_number_checker/queue_store.py telegram_phone_number_checker/durable_web.py tests/test_queue_store.py tests/test_web.py
git commit -m "feat: migrate legacy direct validation safely"
```

### Task 4: Truthful result summary and operator flow

**Files:**
- Modify: `telegram_phone_number_checker/queue_store.py:252-304`
- Modify: `telegram_phone_number_checker/durable_web.py:200-214,380-440`
- Modify: `README.md`
- Modify: `tests/test_queue_store.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: `QueueStore.result_summary(job_id: str) -> dict[str, int]` with `api_confirmed`, `pending_retry`, `privacy_ambiguous`, `pending_import`, `waiting_authorization`, and `unknown_outcome_review`.
- Modifies: `QueueStore.start(job_id)` accepts `WAITING_AUTHORIZATION` only when the application has confirmed `UserSessionService.snapshot()["state"] == "AUTHORIZED"`.

- [ ] **Step 1: Write failing summary/UI tests**

```python
def test_result_summary_separates_api_confirmation_from_privacy_and_retry():
    assert store.result_summary(job_id) == {
        "api_confirmed": 2,
        "pending_retry": 3,
        "privacy_ambiguous": 4,
        "pending_import": 5,
        "waiting_authorization": 0,
        "unknown_outcome_review": 1,
    }
```

Add page assertions for `Подтверждено API`, `API не определил из-за приватности`, `Повторный импорт ожидают`, and `Требуется повторная авторизация`; assert it never labels a privacy result as absent. For `WAITING_AUTHORIZATION`, assert the page shows `Возобновить после авторизации`, rejects resume while the service is not authorized, and accepts one explicit resume only after the service is `AUTHORIZED`.

- [ ] **Step 2: Run summary tests to verify they fail**

Run: `python -m pytest -q tests/test_queue_store.py tests/test_web.py -k 'result_summary or privacy_ambiguity or authorization'`

Expected: FAIL because `result_summary` is missing.

- [ ] **Step 3: Implement summary, copy, and documentation**

Count `FOUND` and `FOUND_NO_USERNAME` as API-confirmed, `PENDING_RETRY` separately, `UNRESOLVABLE_OR_PRIVACY` separately, `UNKNOWN_OUTCOME_REVIEW` separately, and pending `IMPORT` rows separately. Suppress a confident completion ETA whenever retry, unknown-outcome review, FloodWait, or authorization wait exists; show the stored reason and next attempt instead. For `WAITING_AUTHORIZATION`, show `Возобновить после авторизации`; validate the current `AUTHORIZED` session before calling `QueueStore.start()`, and never resume it automatically after login. Document browser authorization, paused-job migration, manual confirmation, privacy ambiguity, unknown-outcome operator requeue, and Docker stop/start.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest -q tests/test_queue_store.py tests/test_web.py`

Expected: PASS.

```bash
git add telegram_phone_number_checker/queue_store.py telegram_phone_number_checker/durable_web.py README.md tests/test_queue_store.py tests/test_web.py
git commit -m "feat: explain user-session validation outcomes"
```

### Task 5: Full lifecycle and Docker acceptance

**Files:**
- Modify: `tests/test_user_session.py`
- Modify: `tests/test_validator_worker.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: verified local Docker lifecycle evidence.

- [ ] **Step 1: Write a failing end-to-end lifecycle test**

```python
def test_paused_legacy_job_migrates_authorizes_and_resumes_without_new_client():
    # It migrates legacy direct rows, waits for authorization, imports with one client,
    # preserves an unknown outcome, and leaves manually confirmed rows unchanged.
    assert fake_factory.created == 1
```

Add a restart test that stops the application while one import lease is active, opens the same database in a new application, and proves no item is lost or double-finished. Add an integration test that sends SIGTERM during an active request and proves the process exits within Docker's configured grace period, without hanging `serve_forever()` or leaking its active lease. Add a secrecy test that submits distinctive OTP and 2FA values, then asserts they appear in none of the SQLite tables/events, CSV output, application logs, or rendered HTML.

- [ ] **Step 2: Run lifecycle tests to verify they fail**

Run: `python -m pytest -q tests/test_user_session.py tests/test_validator_worker.py tests/test_web.py -k 'lifecycle or restart'`

Expected: FAIL until Tasks 1-4 are integrated.

- [ ] **Step 3: Run full acceptance and commit**

Run:

```bash
python -m pytest -q
docker compose up -d --build
curl --silent --show-error --fail http://127.0.0.1:8010/ > /tmp/tg-validator-home.html
docker compose stop
docker compose start
curl --silent --show-error --fail http://127.0.0.1:8010/ > /tmp/tg-validator-restarted.html
```

Expected: all tests pass; both HTTP calls return 200; `docker compose ps` reports `tg-validatorForSelf` running; the named volume preserves the queue/session paths across stop/start.

```bash
git add tests/test_user_session.py tests/test_validator_worker.py tests/test_web.py
git commit -m "test: cover persistent user-session lifecycle"
```

## Self-Review

- Spec coverage: Task 1 owns the one user session and secure handoff; Task 2 owns attempt history, rate-aware scheduling, and unknown outcomes; Task 3 owns migration and explicit unknown-outcome review; Task 4 owns truthful operator output; Task 5 proves restart behavior.
- CTO blockers: pending legacy direct rows, unknown request outcomes, revoked sessions and their explicit resume transition, pre-dispatch client recovery, durable attempt history, authorization handoff, helper-thread SIGTERM shutdown, cleanup starvation, visible retry policy, secrecy, and full acceptance coverage each have an explicit task and test.
- Type consistency: the worker continues to consume the existing synchronous gateway protocol while the session owner encapsulates its asyncio loop.
- Scope: no multi-account dispatch, bot workflow, or client UI automation is included.
