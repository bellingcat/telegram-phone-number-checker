import json

import pytest

from telegram_phone_number_checker.queue_store import QueueStore


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


def test_result_summary_separates_api_confirmation_from_privacy_and_retry(tmp_path):
    store = QueueStore(tmp_path / 'validator.db', now=Clock())
    job_id = store.create_job('\n'.join(f'+799900000{i:02d}' for i in range(15)))
    statuses = ['FOUND', 'FOUND_NO_USERNAME'] + ['PENDING_RETRY'] * 3 + ['UNRESOLVABLE_OR_PRIVACY'] * 4 + ['PENDING'] * 5 + ['UNKNOWN_OUTCOME_REVIEW']
    with store._connect() as connection:
        for ordinal, status in enumerate(statuses):
            connection.execute('UPDATE job_items SET status=? WHERE job_id=? AND ordinal=?', (status, job_id, ordinal))
    assert store.result_summary(job_id) == {
        'api_confirmed': 2, 'pending_retry': 3, 'privacy_ambiguous': 4,
        'pending_import': 5, 'waiting_authorization': 0, 'unknown_outcome_review': 1,
    }


def test_result_summary_counts_unfinished_authorization_wait_rows(tmp_path):
    store = QueueStore(tmp_path / 'validator.db', now=Clock())
    job_id = store.create_job('+79990000000\n+79991111111')
    with store._connect() as connection:
        connection.execute("UPDATE job_items SET status='FOUND' WHERE job_id=? AND ordinal=0", (job_id,))
        connection.execute("UPDATE jobs SET state='WAITING_AUTHORIZATION' WHERE id=?", (job_id,))
    summary = store.result_summary(job_id)
    assert summary['api_confirmed'] == 1
    assert summary['waiting_authorization'] == 1
    assert summary['pending_import'] == 1


def test_authorization_resume_requires_explicit_application_confirmation(tmp_path):
    store = QueueStore(tmp_path / 'validator.db', now=Clock())
    job_id = store.create_job('+79990000000')
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET state='WAITING_AUTHORIZATION' WHERE id=?", (job_id,))
    assert store.start(job_id) is False
    assert store.snapshot(job_id)['state'] == 'WAITING_AUTHORIZATION'
    assert store.start(job_id, authorization_confirmed=True) is True
    assert store.start(job_id, authorization_confirmed=True) is False


@pytest.mark.parametrize('obstacle', ['PENDING_RETRY', 'RETRYABLE_ERROR', 'UNKNOWN_OUTCOME_REVIEW', 'WAITING_AUTHORIZATION', 'WAITING_RATE_LIMIT', 'floodwait', 'unknown_cleanup'])
def test_result_summary_suppresses_eta_for_unresolved_waits(tmp_path, obstacle):
    store = QueueStore(tmp_path / 'validator.db', now=Clock(1100))
    job_id = store.create_job('+79990000000\n+79991111111')
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET state='RUNNING', started_at=1000 WHERE id=?", (job_id,))
        connection.execute("UPDATE job_items SET status='FOUND' WHERE job_id=? AND ordinal=0", (job_id,))
        if obstacle.startswith('WAITING_'):
            connection.execute('UPDATE jobs SET state=? WHERE id=?', (obstacle, job_id))
        elif obstacle == 'floodwait':
            connection.execute('UPDATE jobs SET resume_after=1200 WHERE id=?', (job_id,))
        elif obstacle == 'unknown_cleanup':
            connection.execute("INSERT INTO contact_cleanup(job_id, telegram_user_id, state, created_at) VALUES (?, 123, 'UNKNOWN_OUTCOME_REVIEW', 1000)", (job_id,))
        else:
            connection.execute('UPDATE job_items SET status=? WHERE job_id=? AND ordinal=1', (obstacle, job_id))
    assert store.snapshot(job_id)['eta_seconds'] is None


def test_account_floodwait_suppresses_other_jobs_eta_and_exposes_effective_wait(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / 'validator.db', now=clock)
    other_job = store.create_job('+79990000000\n+79991111111')
    store.start(other_job)
    store.finish_claim(store.claim_next('worker'), {'error': 'No response'})
    store.pause(other_job)
    flood_job = store.create_job('+79992222222')
    store.start(flood_job)
    clock.value = 1010
    store.defer_account_floodwait([store.claim_next('worker')], 300)
    store.start(other_job)
    snapshot = store.snapshot(other_job)
    assert snapshot['state'] == 'QUEUED'
    assert snapshot['resume_after'] is None
    assert snapshot['eta_seconds'] is None
    assert snapshot['account_wait_until'] == 1310
    assert snapshot['effective_wait_until'] == 1310
    assert store.claim_next('worker') is None
    clock.value = 1311
    snapshot = store.snapshot(other_job)
    assert snapshot['account_wait_until'] is None
    assert snapshot['effective_wait_until'] is None
    assert snapshot['eta_seconds'] == 311


def test_store_creates_every_input_ordinal_and_restores_it(tmp_path):
    database = tmp_path / "validator.db"
    store = QueueStore(database)

    job_id = store.create_job("ООО Тест\n+79991234567\n+79991234567\n799")

    assert store.snapshot(job_id)["total"] == 3
    assert [row["phone"] for row in store.rows(job_id)] == [
        "+79991234567",
        "+79991234567",
        "799",
    ]
    assert QueueStore(database).snapshot(job_id)["total"] == 3


def test_migration_preserves_found_deferred_and_unprocessed_ordinals(tmp_path):
    source = tmp_path / "jobs.json"
    source.write_text(
        json.dumps(
            {
                "jobs": {
                    "legacy": {
                        "id": "legacy",
                        "raw": "ООО Тест\n+79991234567\n+79990000000\n+79991111111",
                        "results_by_phone": {
                            "+79991234567": {"id": 1, "username": "known"},
                        },
                        "deferred_phones": ["+79990000000"],
                        "skip_deferred": True,
                        "waiting_until": None,
                    }
                },
                "last_rows": [],
            }
        ),
        encoding="utf-8",
    )
    store = QueueStore(tmp_path / "validator.db")

    store.migrate_json(source)

    assert store.snapshot("legacy")["state"] == "STAGED"
    assert [row["status"] for row in store.rows("legacy")] == [
        "FOUND",
        "DEFERRED_NOT_CHECKED",
        "PENDING",
    ]
    store.migrate_json(source)
    assert len(store.rows("legacy")) == 3


def test_migration_keeps_the_legacy_deduplicated_row_count(tmp_path):
    source = tmp_path / "jobs.json"
    source.write_text(
        json.dumps({"jobs": {"legacy": {"id": "legacy", "raw": "+79991234567\n+79991234567"}}}),
        encoding="utf-8",
    )

    store = QueueStore(tmp_path / "validator.db")
    store.migrate_json(source)

    assert store.snapshot("legacy")["total"] == 1
    assert [row["phone"] for row in store.rows("legacy")] == ["+79991234567"]


def test_live_account_lease_excludes_second_process_and_cancel_preserves_found(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567\n+79990000000")
    assert store.start(job_id) is True

    first = store.claim_next("worker-a")

    assert first["phone"] == "+79991234567"
    assert store.claim_next("worker-b") is None
    store.finish_claim(first, {"id": 1, "username": "known"})
    store.cancel(job_id)
    assert [row["status"] for row in store.rows(job_id)] == ["FOUND", "CANCELLED"]


def test_cancel_keeps_an_already_sent_claim_and_saves_its_late_result(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    claim = store.claim_next("worker-a")

    store.cancel(job_id)
    store.finish_claim(claim, {"id": 1, "username": "late"})

    assert store.snapshot(job_id)["state"] == "CANCELLED"
    assert store.rows(job_id)[0]["status"] == "FOUND"


def test_expired_in_progress_import_lease_is_unknown_and_not_replayed(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    assert store.start(job_id) is True
    first_claim = store.claim_next("worker-a")

    clock.value += 61
    recovered_claim = QueueStore(tmp_path / "validator.db", now=clock).claim_next("worker-b")

    assert recovered_claim is None
    assert store.rows(job_id)[0]["status"] == "UNKNOWN_OUTCOME_REVIEW"
    with store._connect() as connection:
        assert connection.execute("SELECT outcome FROM job_item_attempts WHERE item_id = ?", (first_claim['id'],)).fetchone()[0] == "UNKNOWN_OUTCOME"


def test_reconcile_requeues_an_expired_running_job_lease_without_item_loss(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567\n+79990000000")
    assert store.start(job_id) is True
    store.claim_next("worker-a")

    clock.value += 61
    store.reconcile()

    snapshot = store.snapshot(job_id)
    assert snapshot["state"] == "QUEUED"
    assert snapshot["counts"]["PENDING"] == 1
    assert snapshot["counts"]["UNKNOWN_OUTCOME_REVIEW"] == 1


def test_floodwait_blocks_every_job_and_automatically_resumes_after_deadline(tmp_path):
    clock = Clock()
    database = tmp_path / "validator.db"
    store = QueueStore(database, now=clock)
    first_job = store.create_job("+79991234567")
    second_job = store.create_job("+79990000000")
    assert store.start(first_job) is True
    assert store.start(second_job) is True
    claim = store.claim_next("worker-a")

    store.defer_account_floodwait([claim], seconds=90)

    reopened = QueueStore(database, now=clock)
    assert reopened.snapshot(first_job)["state"] == "WAITING_RATE_LIMIT"
    assert reopened.snapshot(first_job)["resume_after"] == 1_090.0
    assert reopened.claim_next("worker-b") is None

    clock.value += 91
    resumed_claim = reopened.claim_next("worker-b")

    assert resumed_claim["phone"] == "+79991234567"
    assert reopened.snapshot(first_job)["state"] == "RUNNING"


def test_cleanup_floodwait_returns_cleanup_to_the_durable_queue(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    claim = store.claim_next("worker-a")
    store.finish_claim(claim, {"id": 123, "username": "known"})
    cleanup = store.claim_cleanup("worker-a")

    store.defer_cleanup_floodwait(cleanup, seconds=90)

    assert store.claim_cleanup("worker-b") is None
    clock.value += 91
    recovered = store.claim_cleanup("worker-b")
    assert recovered["telegram_user_ids"] == [123]


def test_renew_claims_keeps_a_slow_request_owned(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    claim = store.claim_next("worker-a")

    clock.value += 30
    store.renew_claims([claim])
    clock.value += 31

    assert store.claim_next("worker-b") is None


def test_malformed_json_keeps_source_and_enters_diagnostic_mode(tmp_path):
    source = tmp_path / "jobs.json"
    source.write_text("{bad json", encoding="utf-8")
    store = QueueStore(tmp_path / "validator.db")

    store.migrate_json(source)

    diagnostic = store.migration_diagnostic()
    assert source.read_text(encoding="utf-8") == "{bad json"
    assert diagnostic["active"] is True
    assert "не удалось" in diagnostic["message"].lower()


def test_manual_chat_confirmation_keeps_api_status_and_adds_phone_link(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79089145093")
    store.start(job_id)
    claim = store.claim_next("worker-a")
    store.finish_claim(claim, {"error": "No response, the phone number is not on Telegram or has blocked phone lookup."})

    before = store.rows(job_id)[0]
    assert before["api_status"] == "UNRESOLVABLE_OR_PRIVACY"
    assert before["manual_confirmation"] == "NOT_CONFIRMED"
    assert before["telegram_link"] == "https://t.me/+79089145093?profile"

    assert store.confirm_manual_chat(job_id, before["_item_id"]) is True

    after = store.rows(job_id)[0]
    assert after["status"] == "UNRESOLVABLE_OR_PRIVACY"
    assert after["api_status"] == "UNRESOLVABLE_OR_PRIVACY"
    assert after["manual_confirmation"] == "MANUAL_CHAT_CONFIRMED"


def test_import_retry_backoff_is_durable_and_remains_import(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567\n+79991234567")
    store.start(job_id)
    for delay in (60, 300, 900, 900):
        claims = store.claim_batch("worker", 10)
        assert len(claims) == 1
        store.defer_import_retries(claims, "Telegram requested retry_contacts")
        assert [row["status"] for row in store.rows(job_id)] == ["PENDING_RETRY"] * 2
        assert store.rows(job_id)[0]["method"] == "IMPORT"
        assert store.rows(job_id)[0]["next_attempt_at"] == clock.value + delay
        reopened = QueueStore(store.path, now=clock)
        assert reopened.claim_next("worker") is None
        clock.value += delay
        store = reopened
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_item_attempts WHERE outcome='IMPORT_RETRY'").fetchone()[0] == 8


def test_pending_retry_cancel_and_pause_preserve_durable_delay(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    store.defer_import_retries([store.claim_next("worker")], "retry_contacts")
    store.pause(job_id)
    clock.value += 100
    assert QueueStore(store.path, now=clock).claim_next("worker") is None
    assert store.rows(job_id)[0]["next_attempt_at"] == 1060
    store.cancel(job_id)
    assert store.rows(job_id)[0]["status"] == "CANCELLED"


def test_five_successful_import_batches_restore_batch_size_and_resets_streak(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.set_import_batch_size(job_id, 1)
    for expected in (5, 10):
        for _ in range(4):
            store.record_import_batch_success(job_id)
        assert store.snapshot(job_id)["import_batch_size"] == (1 if expected == 5 else 5)
        store.record_import_batch_success(job_id)
        assert store.snapshot(job_id)["import_batch_size"] == expected
        assert store.snapshot(job_id)["import_success_streak"] == 0
    store.start(job_id)
    store.record_import_batch_success(job_id)
    store.defer_import_retries([store.claim_next("worker")], "retry_contacts")
    assert store.snapshot(job_id)["import_success_streak"] == 0


def test_230_second_floodwait_is_exact_and_records_immutable_attempt(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    store.record_import_batch_success(job_id)
    store.defer_account_floodwait([store.claim_next("worker")], 230)
    assert store.snapshot(job_id)["resume_after"] == 1230
    assert store.snapshot(job_id)["import_success_streak"] == 0
    assert store.rows(job_id)[0]["next_attempt_at"] == 1230
    with store._connect() as connection:
        attempt = dict(connection.execute("SELECT * FROM job_item_attempts").fetchone())
        assert attempt["outcome"] == "FLOOD_WAIT"
        assert "230" in attempt["detail"]
    clock.value = 1229
    assert QueueStore(store.path, now=clock).claim_next("worker") is None
    clock.value = 1230
    assert store.claim_next("worker") is not None


def test_terminal_results_and_json_migration_append_attempt_history(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    source = tmp_path / "jobs.json"
    source.write_text(json.dumps({"jobs": {"legacy": {"raw": "+79991234567", "results_by_phone": {"+79991234567": {"id": 1}}}}}))
    store.migrate_json(source)
    store.migrate_json(source)
    job_id = store.create_job("+79991234567\n+79991234567")
    store.start(job_id)
    store.finish_claim(store.claim_next("worker"), {"id": 2})
    with store._connect() as connection:
        attempts = [dict(row) for row in connection.execute("SELECT * FROM job_item_attempts ORDER BY id")]
        assert len(attempts) == 3
        assert attempts[0]["source"] == "JSON_MIGRATION"
        assert [row["outcome"] for row in attempts] == ["FOUND_NO_USERNAME"] * 3
    store.record_item_attempt(int(store.rows(job_id)[0]["_item_id"]), "IMPORT", "REVIEW_NOTE", "kept", "OPERATOR")
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_item_attempts").fetchone()[0] == 4


def test_cleanup_claim_excludes_every_non_running_job_state(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    for state in ("STAGED", "PAUSED", "WAITING_AUTHORIZATION", "WAITING_RATE_LIMIT", "CANCELLED", "COMPLETED", "FAILED"):
        job_id = store.create_job("+79991234567")
        with store._connect() as connection:
            connection.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job_id))
            connection.execute("INSERT INTO contact_cleanup(job_id, telegram_user_id, state, created_at) VALUES (?, 123, 'PENDING', 0)", (job_id,))
    assert store.claim_cleanup_batch("worker", 10) is None


def test_expired_direct_lookup_lease_is_unknown_after_restart(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    with store._connect() as connection:
        connection.execute("UPDATE job_items SET method = 'DIRECT_LOOKUP'")
    store.claim_next("worker-a")
    clock.value += 61
    reopened = QueueStore(store.path, now=clock)
    assert reopened.claim_next("worker-b") is None
    assert reopened.rows(job_id)[0]["status"] == "UNKNOWN_OUTCOME_REVIEW"
    assert 'неизвестен' in reopened.snapshot(job_id)['message']


def test_expired_cleanup_lease_is_unknown_and_keeps_job_unresolved(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    store.finish_claim(store.claim_next("worker-a"), {"id": 123})
    store.claim_cleanup_batch("worker-a", 100)
    clock.value += 61
    reopened = QueueStore(store.path, now=clock)
    assert reopened.claim_cleanup_batch("worker-b", 100) is None
    reopened.finalize_ready_jobs()
    assert reopened.snapshot(job_id)["state"] != "COMPLETED"
    with store._connect() as connection:
        assert connection.execute("SELECT state FROM contact_cleanup").fetchone()[0] == "UNKNOWN_OUTCOME_REVIEW"
        assert connection.execute("SELECT outcome FROM job_item_attempts ORDER BY id DESC").fetchone()[0] == "UNKNOWN_OUTCOME"


def test_pending_retry_requires_persisted_due_time(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    with store._connect() as connection:
        connection.execute("UPDATE job_items SET status = 'PENDING_RETRY', next_attempt_at = NULL")
    assert store.claim_next("worker") is None


def test_additive_schema_upgrade_preserves_paused_queue_without_resume(tmp_path):
    import sqlite3
    clock = Clock()
    database = tmp_path / "validator.db"
    store = QueueStore(database, now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    store.pause(job_id)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE job_item_attempts")
        connection.execute("ALTER TABLE jobs DROP COLUMN import_success_streak")
        connection.execute("ALTER TABLE job_items DROP COLUMN import_retry_count")
        connection.execute("ALTER TABLE worker_state DROP COLUMN import_batches_since_cleanup")
        connection.execute("ALTER TABLE contact_cleanup DROP COLUMN item_id")
    reopened = QueueStore(database, now=clock)
    assert reopened.snapshot(job_id)['state'] == 'PAUSED'
    assert reopened.rows(job_id)[0]['status'] == 'PENDING'
    assert reopened.claim_next('worker') is None
    with reopened._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM job_item_attempts').fetchone()[0] == 0


def test_duplicate_terminal_result_does_not_overwrite_unknown_evidence(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567\n+79991234567")
    store.start(job_id)
    claim = store.claim_next('worker')
    store.defer_unknown_outcome([claim], 'IMPORT')
    store.finish_claim(claim, {'id': 123})
    assert [row['status'] for row in store.rows(job_id)] == ['UNKNOWN_OUTCOME_REVIEW'] * 2
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM job_item_attempts').fetchone()[0] == 2


def test_only_actual_failure_counts_toward_terminal_retry_limit(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / "validator.db", now=clock)
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    for _ in range(4):
        store.defer_import_retries([store.claim_next('worker')], 'retry_contacts')
        clock.value = store.rows(job_id)[0]['next_attempt_at']
    for status, delay in [('RETRYABLE_ERROR', 30), ('RETRYABLE_ERROR', 120), ('API_ERROR_FINAL', 600)]:
        store.retry_claim(store.claim_next('worker'), RuntimeError('raw details'))
        assert store.rows(job_id)[0]['status'] == status
        clock.value += delay
    with store._connect() as connection:
        assert connection.execute('SELECT attempts FROM job_items').fetchone()[0] == 3


@pytest.mark.parametrize('state', ['PAUSED', 'WAITING_AUTHORIZATION'])
@pytest.mark.parametrize('operation', ['IMPORT', 'CLEANUP'])
def test_late_floodwait_preserves_explicit_pause_across_deadline(tmp_path, state, operation):
    clock = Clock()
    store = QueueStore(tmp_path / 'validator.db', now=clock)
    job_id = store.create_job('+79991234567')
    store.start(job_id)
    claim = store.claim_next('worker')
    if operation == 'CLEANUP':
        store.finish_claim(claim, {'id': 123})
        claim = store.claim_cleanup_batch('worker', 10)
    if state == 'PAUSED':
        assert store.pause(job_id)
    else:
        with store._connect() as connection:
            connection.execute("UPDATE jobs SET state = 'WAITING_AUTHORIZATION', message = 'Explicit authorization wait' WHERE id = ?", (job_id,))
    if operation == 'IMPORT':
        store.defer_account_floodwait([claim], 230)
    else:
        store.defer_cleanup_floodwait(claim, 230)
    assert store.snapshot(job_id)['state'] == state
    assert store.snapshot(job_id)['resume_after'] == 1230
    clock.value = 1300
    reopened = QueueStore(store.path, now=clock)
    reopened.reconcile()
    assert reopened.snapshot(job_id)['state'] == state
    assert reopened.claim_next('worker-b') is None
    assert reopened.claim_cleanup_batch('worker-b', 10) is None


@pytest.mark.parametrize('result', [
    {'error': 'LEGACY_OTP_91234_2FA_DISTINCTIVE_TEST_MARKER'},
    {'skipped': True, 'error': 'LEGACY_OTP_91234_2FA_DISTINCTIVE_TEST_MARKER'},
    {'error': 'No response LEGACY_OTP_91234_2FA_DISTINCTIVE_TEST_MARKER'},
])
def test_json_migration_never_copies_raw_auth_errors_into_database(tmp_path, result):
    store = QueueStore(tmp_path / 'validator.db')
    source = tmp_path / 'jobs.json'
    source.write_text(json.dumps({'jobs': {'legacy': {'raw': '+79991234567', 'results_by_phone': {'+79991234567': result}}}}))
    store.migrate_json(source)
    with store._connect() as connection:
        dumped_database = '\n'.join(connection.iterdump())
        history = str([dict(row) for row in connection.execute('SELECT * FROM job_item_attempts')])
    assert 'LEGACY_OTP_91234_2FA_DISTINCTIVE_TEST_MARKER' not in dumped_database
    assert 'LEGACY_OTP_91234_2FA_DISTINCTIVE_TEST_MARKER' not in history
    assert 'LEGACY_OTP_91234_2FA_DISTINCTIVE_TEST_MARKER' in source.read_text()


def test_repeated_import_response_commit_does_not_double_count_batch(tmp_path):
    store = QueueStore(tmp_path / 'validator.db')
    job_id = store.create_job('+79991234567')
    store.start(job_id)
    claims = store.claim_batch('worker', 10)
    result = {claims[0]['phone']: {'id': 123}}
    store.finish_import_batch(claims, result)
    store.finish_import_batch(claims, result)
    assert store.snapshot(job_id)['import_success_streak'] == 1
    with store._connect() as connection:
        assert connection.execute('SELECT import_batches_since_cleanup FROM worker_state').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM job_item_attempts').fetchone()[0] == 1


def _legacy_review_job(store):
    job_id = store.create_job('\n'.join(f'+7999000000{i}' for i in range(8)))
    store.start(job_id)
    store.pause(job_id)
    with store._connect() as connection:
        for ordinal, status, method, api_status, manual in [
            (0, 'PENDING', 'DIRECT_LOOKUP', '', 'NOT_CONFIRMED'),
            (1, 'RETRYABLE_ERROR', 'DIRECT_LOOKUP', 'RETRYABLE_ERROR', 'NOT_CONFIRMED'),
            (2, 'UNRESOLVABLE_OR_PRIVACY', 'DIRECT_LOOKUP', 'UNRESOLVABLE_OR_PRIVACY', 'NOT_CONFIRMED'),
            (3, 'UNRESOLVABLE_OR_PRIVACY', 'DIRECT_LOOKUP', 'UNRESOLVABLE_OR_PRIVACY', 'MANUAL_CHAT_CONFIRMED'),
            (4, 'FOUND', 'DIRECT_LOOKUP', 'FOUND', 'NOT_CONFIRMED'),
            (5, 'PENDING', 'DIRECT_LOOKUP', 'FOUND_NO_USERNAME', 'NOT_CONFIRMED'),
            (6, 'UNRESOLVABLE_OR_PRIVACY', 'IMPORT', 'UNRESOLVABLE_OR_PRIVACY', 'NOT_CONFIRMED'),
            (7, 'UNKNOWN_OUTCOME_REVIEW', 'IMPORT', 'UNKNOWN_OUTCOME', 'NOT_CONFIRMED'),
        ]:
            connection.execute('UPDATE job_items SET status=?, method=?, api_status=?, manual_confirmation=?, detail=? WHERE job_id=? AND ordinal=?',
                               (status, method, api_status, manual, 'legacy raw details +79990000000 secret', job_id, ordinal))
    return job_id


def test_legacy_direct_migration_requeues_only_unconfirmed_rows_with_safe_history(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / 'validator.db', now=clock)
    job_id = _legacy_review_job(store)
    before = store.rows(job_id)
    store.record_item_attempt(int(before[2]['_item_id']), 'DIRECT_LOOKUP', 'UNRESOLVABLE_OR_PRIVACY', 'safe previous evidence')

    assert store.requeue_legacy_direct_rows(job_id) == 3

    rows = store.rows(job_id)
    assert [row['status'] for row in rows[:3]] == ['PENDING_RETRY'] * 3
    assert [row['method'] for row in rows[:3]] == ['IMPORT'] * 3
    assert [row['next_attempt_at'] for row in rows[:3]] == [1060.0] * 3
    assert [row['api_status'] for row in rows[:3]] == ['PENDING', 'RETRYABLE_ERROR', 'UNRESOLVABLE_OR_PRIVACY']
    assert rows[3:] == before[3:]
    assert store.snapshot(job_id)['state'] == 'PAUSED'
    assert store.claim_next('worker') is None
    history = store.item_attempts(job_id, rows[2]['_item_id'])
    assert history[0]['detail'] == 'safe previous evidence'
    assert history[-1]['outcome'] == 'UNRESOLVABLE_OR_PRIVACY'
    assert history[-1]['method'] == 'DIRECT_LOOKUP'
    assert history[-1]['source'] == 'LEGACY_DIRECT_MIGRATION'
    assert 'secret' not in str(history) and '+79990000000' not in str(history)
    assert '3' in store.events(job_id)[-1]['message']
    assert store.requeue_legacy_direct_rows(job_id) == 0
    assert len(store.item_attempts(job_id, rows[2]['_item_id'])) == 2


def test_item_attempts_is_scoped_ordered_and_survives_reopen(tmp_path):
    store = QueueStore(tmp_path / 'validator.db')
    job_id = store.create_job('+79990000000')
    other_id = store.create_job('+79991111111')
    item_id = store.rows(job_id)[0]['_item_id']
    store.record_item_attempt(int(item_id), 'IMPORT', 'UNKNOWN_OUTCOME', 'unknown')
    store.record_item_attempt(int(item_id), 'IMPORT', 'REVIEW_NOTE', 'reviewed', 'OPERATOR')
    reopened = QueueStore(store.path)
    assert [attempt['outcome'] for attempt in reopened.item_attempts(job_id, item_id)] == ['UNKNOWN_OUTCOME', 'REVIEW_NOTE']
    assert reopened.item_attempts(other_id, item_id) == []
    assert reopened.item_attempts(job_id, 'not-an-id') == []


def test_requeue_unknown_outcomes_preserves_evidence_and_unknown_cleanup(tmp_path):
    store = QueueStore(tmp_path / 'validator.db', now=Clock())
    job_id = _legacy_review_job(store)
    before = store.rows(job_id)
    item_id = before[7]['_item_id']
    store.record_item_attempt(int(item_id), 'IMPORT', 'UNKNOWN_OUTCOME', 'unknown evidence')
    with store._connect() as connection:
        connection.execute("INSERT INTO contact_cleanup(job_id, telegram_user_id, item_id, state, created_at, last_error) VALUES (?, 123, ?, 'UNKNOWN_OUTCOME_REVIEW', 999, 'unknown cleanup')", (job_id, int(item_id)))
        cleanup_before = dict(connection.execute('SELECT * FROM contact_cleanup').fetchone())

    assert store.requeue_unknown_outcomes(job_id) == 1

    rows = store.rows(job_id)
    assert rows[:7] == before[:7]
    assert rows[7]['status'] == 'PENDING_RETRY'
    assert rows[7]['method'] == 'IMPORT'
    assert rows[7]['api_status'] == 'UNKNOWN_OUTCOME'
    assert rows[7]['next_attempt_at'] == 1060.0
    history = store.item_attempts(job_id, item_id)
    assert history[0]['outcome'] == 'UNKNOWN_OUTCOME'
    assert history[0]['detail'] == 'unknown evidence'
    assert history[-1]['outcome'] == 'UNKNOWN_OUTCOME_REQUEUED'
    assert history[-1]['source'] == 'OPERATOR'
    assert store.snapshot(job_id)['state'] == 'PAUSED'
    assert store.requeue_unknown_outcomes(job_id) == 0
    with store._connect() as connection:
        assert dict(connection.execute('SELECT * FROM contact_cleanup').fetchone()) == cleanup_before


@pytest.mark.parametrize('action', ['requeue_legacy_direct_rows', 'requeue_unknown_outcomes'])
@pytest.mark.parametrize('blocked_by', ['RUNNING', 'item', 'cleanup'])
def test_requeue_legacy_direct_and_unknown_actions_reject_unpaused_or_active_claims(tmp_path, action, blocked_by):
    store = QueueStore(tmp_path / 'validator.db')
    job_id = _legacy_review_job(store)
    with store._connect() as connection:
        if blocked_by == 'RUNNING':
            connection.execute("UPDATE jobs SET state='RUNNING' WHERE id=?", (job_id,))
        elif blocked_by == 'item':
            connection.execute("UPDATE job_items SET status='IN_PROGRESS', lease_owner='worker' WHERE job_id=? AND ordinal=6", (job_id,))
        else:
            connection.execute("INSERT INTO contact_cleanup(job_id, telegram_user_id, state, created_at, lease_owner) VALUES (?, 123, 'IN_PROGRESS', 999, 'worker')", (job_id,))
    before = store.rows(job_id)
    events = store.events(job_id)
    with pytest.raises(ValueError, match='PAUSED' if blocked_by == 'RUNNING' else 'Активные запросы Telegram: 1'):
        getattr(store, action)(job_id)
    assert store.rows(job_id) == before
    assert store.events(job_id) == events
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM job_item_attempts').fetchone()[0] == 0


def test_legacy_direct_migration_keeps_only_confirmed_rows_unchanged(tmp_path):
    store = QueueStore(tmp_path / 'validator.db')
    job_id = _legacy_review_job(store)
    with store._connect() as connection:
        connection.execute('DELETE FROM job_items WHERE job_id=? AND ordinal NOT IN (3, 4, 5)', (job_id,))
    before = store.rows(job_id)
    assert store.requeue_legacy_direct_rows(job_id) == 0
    assert store.rows(job_id) == before
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM job_item_attempts').fetchone()[0] == 0


@pytest.mark.parametrize('action', ['requeue_legacy_direct_rows', 'requeue_unknown_outcomes'])
def test_review_requeue_rolls_back_rows_and_evidence_on_audit_failure(tmp_path, monkeypatch, action):
    store = QueueStore(tmp_path / 'validator.db')
    job_id = _legacy_review_job(store)
    before = store.rows(job_id)
    events = store.events(job_id)
    record_attempt = store.record_item_attempt

    def record_then_fail(*args, **kwargs):
        record_attempt(*args, **kwargs)
        raise RuntimeError('audit storage failure')

    monkeypatch.setattr(store, 'record_item_attempt', record_then_fail)
    with pytest.raises(RuntimeError, match='audit storage failure'):
        getattr(store, action)(job_id)
    assert store.rows(job_id) == before
    assert store.events(job_id) == events
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM job_item_attempts').fetchone()[0] == 0


@pytest.mark.parametrize('review_action', ['requeue_legacy_direct_rows', 'requeue_unknown_outcomes'])
def test_requeued_item_latest_import_outcome_replaces_api_status_and_preserves_history(tmp_path, review_action):
    clock = Clock()
    store = QueueStore(tmp_path / 'validator.db', now=clock)
    job_id = store.create_job('+79990000000\n+79990000000')
    store.start(job_id)
    store.pause(job_id)
    unknown = review_action == 'requeue_unknown_outcomes'
    old_status = 'UNKNOWN_OUTCOME_REVIEW' if unknown else 'UNRESOLVABLE_OR_PRIVACY'
    old_outcome = 'UNKNOWN_OUTCOME' if unknown else 'UNRESOLVABLE_OR_PRIVACY'
    old_method = 'IMPORT' if unknown else 'DIRECT_LOOKUP'
    with store._connect() as connection:
        connection.execute('UPDATE job_items SET status=?, api_status=?, method=? WHERE job_id=?',
                           (old_status, old_outcome, old_method, job_id))
    for row in store.rows(job_id):
        store.record_item_attempt(int(row['_item_id']), old_method, old_outcome, 'Prior immutable evidence')
    assert getattr(store, review_action)(job_id) == 2
    history_before_response = {row['_item_id']: store.item_attempts(job_id, row['_item_id']) for row in store.rows(job_id)}
    assert [row['api_status'] for row in store.rows(job_id)] == [old_outcome] * 2

    assert store.start(job_id) is True
    assert store.claim_batch('worker', 10) == []
    clock.value += 60
    claims = store.claim_batch('worker', 10)
    assert len(claims) == 1
    store.finish_import_batch(claims, {'+79990000000': {'id': 123, 'username': 'confirmed'}})

    for row in QueueStore(store.path, now=clock).rows(job_id):
        assert row['status'] == 'FOUND'
        assert row['api_status'] == 'FOUND'
        history = store.item_attempts(job_id, row['_item_id'])
        assert history[:-1] == history_before_response[row['_item_id']]
        assert history[0]['outcome'] == old_outcome
        assert history[-1]['outcome'] == 'FOUND'
        assert history[-1]['method'] == 'IMPORT'


@pytest.mark.parametrize('result, outcome', [
    ({'id': 123, 'username': 'confirmed'}, 'FOUND'),
    ({'id': 123}, 'FOUND_NO_USERNAME'),
    ({'error': 'No response'}, 'UNRESOLVABLE_OR_PRIVACY'),
])
def test_direct_terminal_response_updates_current_api_status_for_claim_and_duplicates(tmp_path, result, outcome):
    store = QueueStore(tmp_path / 'validator.db', now=Clock())
    job_id = store.create_job('+79990000000\n+79990000000')
    with store._connect() as connection:
        connection.execute("UPDATE job_items SET method='DIRECT_LOOKUP', api_status='UNKNOWN_OUTCOME' WHERE job_id=?", (job_id,))
    store.start(job_id)
    claim = store.claim_next('worker')
    store.finish_claim(claim, result)
    rows = store.rows(job_id)
    assert [row['status'] for row in rows] == [outcome] * 2
    assert [row['api_status'] for row in rows] == [outcome] * 2
    for row in rows:
        assert store.item_attempts(job_id, row['_item_id'])[-1]['outcome'] == outcome
