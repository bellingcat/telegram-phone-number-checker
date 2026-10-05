from telegram_phone_number_checker.queue_store import QueueStore
from telegram_phone_number_checker.validator_worker import ValidatorWorker
from telethon.errors import FloodWaitError


def test_application_restart_during_import_preserves_unknown_row_without_double_finish(tmp_path, monkeypatch):
    """A cancelled dispatched request stays quarantined across a new application."""
    import asyncio
    from threading import Event
    import time
    from types import SimpleNamespace
    from telegram_phone_number_checker.durable_web import DurableValidatorApplication
    from telegram_phone_number_checker.user_session import UserSessionService

    monkeypatch.setenv('API_ID', '1')
    monkeypatch.setenv('API_HASH', 'hash')
    monkeypatch.setenv('TELEGRAM_SESSION_PATH', str(tmp_path / 'telegram-session'))

    class Client:
        def __init__(self, block):
            self.block = block
            self.connected = False
            self.started = Event()
            self.requests = []
        async def connect(self):
            self.connected = True
        def is_connected(self):
            return self.connected
        async def is_user_authorized(self):
            return True
        async def disconnect(self):
            self.connected = False
        async def __call__(self, request):
            self.requests.append(request)
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            return SimpleNamespace(imported=[], users=[], retry_contacts=[])

    def wait_for(predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('application did not reach expected durable state')

    database = tmp_path / 'validator.db'
    first_client = Client(block=True)
    first_session = UserSessionService(client_factory=lambda *args, **kwargs: first_client)
    first = DurableValidatorApplication(database, gateway=first_session, authorization=first_session)
    try:
        job_id = first.enqueue('+79990000001\n+79990000002')
        first.store.set_import_batch_size(job_id, 1)
        assert first.begin_authorization('+79991112233')
        wait_for(lambda: first_session.snapshot()['state'] == 'AUTHORIZED')
        assert first.start(job_id)
        assert first_client.started.wait(3)
        with first.store._connect() as connection:
            old_claim = dict(connection.execute("SELECT * FROM job_items WHERE status='IN_PROGRESS'").fetchone())
        assert old_claim['lease_owner'] == first.owner
        assert old_claim['lease_expires_at'] > time.time()
        # The normal application close path still runs; shorten only the owned
        # session's request drain so this deterministic blocked fake is cancelled.
        first_session.close(timeout=.01)
    finally:
        first.close()
    assert not first._worker_thread.is_alive()
    assert not first_client.connected
    assert first.store.rows(job_id)[0]['status'] == 'UNKNOWN_OUTCOME_REVIEW'
    assert len(first.store.item_attempts(job_id, str(old_claim['id']))) == 1

    next_client = Client(block=False)
    next_session = UserSessionService(client_factory=lambda *args, **kwargs: next_client)
    restarted = DurableValidatorApplication(database, gateway=next_session, authorization=next_session)
    try:
        wait_for(lambda: restarted.store.rows(job_id)[1]['status'] == 'UNRESOLVABLE_OR_PRIVACY')
        rows = restarted.store.rows(job_id)
        assert len(rows) == 2
        assert rows[0]['status'] == 'UNKNOWN_OUTCOME_REVIEW'
        assert rows[1]['status'] == 'UNRESOLVABLE_OR_PRIVACY'
        assert len(next_client.requests) == 1
        assert next_client.requests[0].contacts[0].phone == '+79990000002'
        # A response arriving from the former owner cannot overwrite quarantine.
        stale = {**old_claim, 'phone': old_claim['normalized_phone'], 'owner': first.owner, 'account_key': 'default'}
        restarted.store.finish_claim(stale, {'id': 123, 'username': 'late_response'})
        assert restarted.store.rows(job_id) == rows
        assert len(restarted.store.item_attempts(job_id, rows[0]['_item_id'])) == 1
        assert len(restarted.store.item_attempts(job_id, rows[1]['_item_id'])) == 1
        with restarted.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM job_items WHERE lease_owner IS NOT NULL").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM worker_state WHERE lease_owner IS NOT NULL").fetchone()[0] == 0
        assert restarted.snapshot(job_id)['state'] != 'COMPLETED'
    finally:
        restarted.close()


class Gateway:
    def __init__(self):
        self.imports = []
        self.resolves = []

    def import_phones(self, phones):
        self.imports.append(phones)
        return {phones[0]: {"retry": True, "error": "Retry later"}}

    def resolve_phone(self, phone):
        self.resolves.append(phone)
        return {"id": 1, "username": "resolved"}


def test_retry_contacts_return_to_import_queue_without_resolve_phone(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    assert store.start(job_id) is True
    gateway = Gateway()
    worker = ValidatorWorker(store, gateway, "test-worker")

    assert worker.run_once() is True
    assert store.rows(job_id)[0]["status"] == "PENDING_RETRY"
    assert store.rows(job_id)[0]["method"] == "IMPORT"
    assert worker.run_once() is False
    assert gateway.imports == [["+79991234567"]]
    assert gateway.resolves == []


def test_worker_persists_temporary_gateway_failure_for_retry(tmp_path):
    class FailingGateway:
        def import_phones(self, phones):
            raise RuntimeError("network unavailable")

        def resolve_phone(self, phone):
            raise AssertionError("not called")

    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.start(job_id)

    assert ValidatorWorker(store, FailingGateway(), "test-worker").run_once() is True
    assert store.rows(job_id)[0]["status"] == "RETRYABLE_ERROR"


class BatchStore:
    """Small store double that documents the worker/store boundary."""

    def __init__(self, claims):
        self.claims = list(claims)
        self.finished = []
        self.direct = []
        self.import_sizes = []
        self.flood_waits = []
        self.cleanup_claims = []
        self.cleaned = []
        self.cleanup_retries = []

    def claim_batch(self, owner, limit):
        if not self.claims:
            return []
        job_id = self.claims[0]["job_id"]
        selected = [claim for claim in self.claims if claim["job_id"] == job_id][:limit]
        selected_ids = {claim["id"] for claim in selected}
        self.claims = [claim for claim in self.claims if claim["id"] not in selected_ids]
        return selected

    def finish_batch(self, claims, results):
        self.finished.extend((claim["id"], results[claim["phone"]]) for claim in claims)

    def finish_import_batch(self, claims, results):
        retry_claims = [claim for claim in claims if results[claim['phone']].get('retry')]
        self.finish_batch([claim for claim in claims if claim not in retry_claims], results)
        if retry_claims:
            self.defer_import_retries(retry_claims, 'retry_contacts')
            self.set_import_batch_size(claims[0]['job_id'], 5)

    def defer_import_retries(self, claims, reason):
        self.direct.extend(claim["id"] for claim in claims)

    def retry_claim(self, claim, error):
        raise AssertionError(f"FloodWait must not become a retry: {error}")

    def set_import_batch_size(self, job_id, size):
        self.import_sizes.append((job_id, size))

    def defer_account_floodwait(self, claims, seconds):
        self.flood_waits.append(([claim["id"] for claim in claims], seconds))

    def record_import_batch_success(self, job_id):
        pass

    def claim_cleanup_batch(self, owner, limit):
        return self.cleanup_claims.pop(0) if self.cleanup_claims else None

    def finish_cleanup(self, claim):
        self.cleaned.append(claim["id"])

    def retry_cleanup(self, claim, error):
        self.cleanup_retries.append((claim["id"], str(error)))


def _claim(item_id, phone, *, method="IMPORT", job_id="job-1"):
    return {
        "id": item_id,
        "phone": phone,
        "normalized_phone": phone,
        "method": method,
        "job_id": job_id,
        "owner": "test-worker",
        "account_key": "default",
    }


def test_worker_imports_at_most_ten_unique_phones_and_reuses_duplicate_result():
    claims = [_claim(item_id, "+79990000001") for item_id in range(1, 12)]
    store = BatchStore(claims)

    class Gateway:
        def __init__(self):
            self.imports = []

        def import_phones(self, phones):
            self.imports.append(phones)
            return {phones[0]: {"id": 1, "username": "same"}}

        def resolve_phone(self, phone):
            raise AssertionError("not called")

    gateway = Gateway()
    worker = ValidatorWorker(store, gateway, "test-worker")
    assert worker.run_once() is True
    assert worker.run_once() is True

    assert gateway.imports == [["+79990000001"]]
    assert [item_id for item_id, _ in store.finished] == list(range(1, 12))


def test_worker_does_not_reuse_a_cached_phone_result_for_another_job():
    store = BatchStore([
        _claim(1, "+79990000001", job_id="job-1"),
        _claim(2, "+79990000001", job_id="job-2"),
    ])

    class Gateway:
        def __init__(self):
            self.imports = []

        def import_phones(self, phones):
            self.imports.append(phones)
            return {phones[0]: {"id": len(self.imports)}}

        def resolve_phone(self, phone):
            raise AssertionError("not called")

    gateway = Gateway()
    worker = ValidatorWorker(store, gateway, "test-worker")
    assert worker.run_once() is True
    assert worker.run_once() is True

    assert gateway.imports == [["+79990000001"], ["+79990000001"]]


def test_worker_reduces_later_import_batches_after_retry_contacts():
    store = BatchStore([_claim(1, "+79990000001"), _claim(2, "+79990000002")])

    class Gateway:
        def import_phones(self, phones):
            return {
                phones[0]: {"retry": True, "error": "Retry later"},
                phones[1]: {"error": "No response"},
            }

        def resolve_phone(self, phone):
            raise AssertionError("not called")

    assert ValidatorWorker(store, Gateway(), "test-worker").run_once() is True

    assert store.direct == [1]
    assert store.import_sizes == [("job-1", 5)]


def test_worker_persists_exact_floodwait_without_marking_item_retryable():
    store = BatchStore([_claim(1, "+79990000001")])

    class Gateway:
        def import_phones(self, phones):
            raise FloodWaitError(None, 47)

        def resolve_phone(self, phone):
            raise AssertionError("not called")

    assert ValidatorWorker(store, Gateway(), "test-worker").run_once() is True

    assert store.flood_waits == [([1], 47)]


def test_worker_processes_durable_cleanup_separately_from_validation():
    store = BatchStore([])
    store.cleanup_claims.append({"id": 9, "telegram_user_ids": [123, 456]})

    class Gateway:
        def import_phones(self, phones):
            raise AssertionError("not called")

        def resolve_phone(self, phone):
            raise AssertionError("not called")

        def cleanup_contacts(self, user_ids):
            assert user_ids == [123, 456]

    assert ValidatorWorker(store, Gateway(), "test-worker").run_once() is True
    assert store.cleaned == [9]


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def test_same_worker_legacy_privacy_migration_imports_instead_of_using_direct_cache(tmp_path):
    clock = Clock()
    store = QueueStore(tmp_path / 'validator.db', now=clock)
    phone = '+79990000001'
    job_id = store.create_job(phone)
    with store._connect() as connection:
        connection.execute("UPDATE job_items SET method = 'DIRECT_LOOKUP' WHERE job_id = ?", (job_id,))

    class Gateway:
        def __init__(self):
            self.resolves, self.imports = [], []
        def resolve_phone(self, number):
            self.resolves.append(number)
            return {'error': 'No response, the phone number is not on Telegram or has blocked contact adding.'}
        def import_phones(self, phones):
            self.imports.append(phones)
            return {phone: {'id': 123, 'username': 'import_match'}}

    gateway = Gateway()
    worker = ValidatorWorker(store, gateway, 'worker')
    assert store.start(job_id)
    assert worker.run_once()
    assert store.rows(job_id)[0]['status'] == 'UNRESOLVABLE_OR_PRIVACY'
    assert store.pause(job_id)
    assert store.requeue_legacy_direct_rows(job_id) == 1
    assert store.rows(job_id)[0]['next_attempt_at'] == 1060
    clock.value = 1060
    assert worker.run_once() is False  # Due alone never resumes a paused job.
    assert store.start(job_id)
    assert worker.run_once()
    assert gateway.resolves == [phone]
    assert gateway.imports == [[phone]]
    row = store.rows(job_id)[0]
    assert row['status'] == 'FOUND'
    assert row['username'] == 'import_match'
    assert [(attempt['method'], attempt['outcome']) for attempt in store.item_attempts(job_id, row['_item_id'])] == [
        ('DIRECT_LOOKUP', 'UNRESOLVABLE_OR_PRIVACY'),
        ('DIRECT_LOOKUP', 'UNRESOLVABLE_OR_PRIVACY'),
        ('IMPORT', 'FOUND'),
    ]


def test_unknown_outcome_after_dispatched_import_is_not_replayed(tmp_path):
    from telegram_phone_number_checker.user_session import RequestOutcomeUnknownError
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567\n+79991234567")
    store.start(job_id)

    class UnknownGateway:
        import_calls = 0

        def import_phones(self, phones):
            self.import_calls += 1
            raise RequestOutcomeUnknownError("transport error with sensitive details")

    gateway = UnknownGateway()
    worker = ValidatorWorker(store, gateway, "test-worker")
    worker.run_once()
    assert [row["status"] for row in store.rows(job_id)] == ["UNKNOWN_OUTCOME_REVIEW"] * 2
    assert store.events(job_id)[-1]["kind"] == "UNKNOWN_OUTCOME"
    reopened = QueueStore(store.path)
    assert ValidatorWorker(reopened, gateway, "worker-b").run_once() is False
    assert gateway.import_calls == 1
    with store._connect() as connection:
        attempts = connection.execute("SELECT * FROM job_item_attempts").fetchall()
        assert [row["outcome"] for row in attempts] == ["UNKNOWN_OUTCOME"] * 2
        assert all("sensitive" not in row["detail"] for row in attempts)


def test_revoked_session_pauses_job_without_incrementing_normal_retry_count(tmp_path):
    from telegram_phone_number_checker.user_session import UserSessionUnavailableError
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.start(job_id)

    class RevokedGateway:
        def import_phones(self, phones):
            raise UserSessionUnavailableError("OTP=secret password=secret")

    worker = ValidatorWorker(store, RevokedGateway(), "test-worker")
    worker.run_once()
    assert store.snapshot(job_id)["state"] == "WAITING_AUTHORIZATION"
    assert store.snapshot(job_id)["import_retry_count"] == 0
    with store._connect() as connection:
        assert connection.execute("SELECT attempts FROM job_items").fetchone()[0] == 0
    assert worker.run_once() is False
    assert "secret" not in str(store.rows(job_id)) + str(store.events(job_id))
    store.reconcile()
    assert store.snapshot(job_id)["state"] == "WAITING_AUTHORIZATION"
    # Authorization alone does not touch the queue; the operator must explicitly start.
    class AuthorizedGateway:
        calls = 0
        def import_phones(self, phones):
            self.calls += 1
            return {phones[0]: {"id": 1}}
    gateway = AuthorizedGateway()
    authorized_worker = ValidatorWorker(store, gateway, "test-worker")
    assert authorized_worker.run_once() is False
    # The application confirms its AUTHORIZED session before explicit resume.
    assert store.start(job_id, authorization_confirmed=True) is True
    assert authorized_worker.run_once() is True
    assert gateway.calls == 1


def test_not_dispatched_request_uses_recoverable_path(tmp_path):
    from telegram_phone_number_checker.user_session import RequestNotDispatchedError
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    class Gateway:
        def import_phones(self, phones):
            raise RequestNotDispatchedError("secret raw error")
    ValidatorWorker(store, Gateway(), "worker").run_once()
    assert store.rows(job_id)[0]["status"] == "RETRYABLE_ERROR"
    assert "secret" not in str(store.rows(job_id))


def test_five_ready_import_batches_run_before_cleanup(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("\n".join(f"+79990000{i:03d}" for i in range(7)))
    store.start(job_id)
    store.set_import_batch_size(job_id, 1)
    with store._connect() as connection:
        connection.execute("INSERT INTO contact_cleanup(job_id, telegram_user_id, state, created_at) VALUES (?, 100, 'PENDING', 0)", (job_id,))
    class Gateway:
        calls = []
        def import_phones(self, phones):
            self.calls.append("IMPORT")
            return {phone: {"id": index + 1} for index, phone in enumerate(phones)}
        def cleanup_contacts(self, user_ids):
            self.calls.append("CLEANUP")
    gateway = Gateway()
    worker = ValidatorWorker(store, gateway, "worker")
    for _ in range(6):
        assert worker.run_once() is True
    assert gateway.calls == ["IMPORT"] * 5 + ["CLEANUP"]


def test_paused_job_with_pending_cleanup_issues_no_request(tmp_path):
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    claim = store.claim_next("worker")
    store.finish_claim(claim, {"id": 123})
    store.pause(job_id)
    class Gateway:
        def cleanup_contacts(self, user_ids):
            raise AssertionError("paused job made a Telegram request")
    assert ValidatorWorker(store, Gateway(), "worker").run_once() is False


def test_unknown_cleanup_outcome_is_not_replayed(tmp_path):
    from telegram_phone_number_checker.user_session import RequestOutcomeUnknownError
    store = QueueStore(tmp_path / "validator.db")
    job_id = store.create_job("+79991234567")
    store.start(job_id)
    store.finish_claim(store.claim_next("worker"), {"id": 123})
    class Gateway:
        def cleanup_contacts(self, user_ids):
            raise RequestOutcomeUnknownError("raw secret")
    worker = ValidatorWorker(store, Gateway(), "worker")
    assert worker.run_once() is True
    assert worker.run_once() is False
    assert 'неизвестен' in store.snapshot(job_id)['message']
    with store._connect() as connection:
        assert connection.execute("SELECT state FROM contact_cleanup").fetchone()[0] == "UNKNOWN_OUTCOME_REVIEW"
        assert connection.execute("SELECT outcome FROM job_item_attempts ORDER BY id DESC").fetchone()[0] == "UNKNOWN_OUTCOME"


def test_cleanup_budget_survives_worker_restart(tmp_path):
    store = QueueStore(tmp_path / 'validator.db')
    job_id = store.create_job('\n'.join(f'+79990000{i:03d}' for i in range(7)))
    store.start(job_id)
    store.set_import_batch_size(job_id, 1)
    class Gateway:
        def __init__(self):
            self.calls = []
        def import_phones(self, phones):
            self.calls.append('IMPORT')
            return {phone: {'id': index + 123} for index, phone in enumerate(phones)}
        def cleanup_contacts(self, user_ids):
            self.calls.append('CLEANUP')
    gateway = Gateway()
    for _ in range(6):
        reopened = QueueStore(store.path)
        assert ValidatorWorker(reopened, gateway, 'worker').run_once() is True
    assert gateway.calls == ['IMPORT'] * 5 + ['CLEANUP']


def test_cleanup_authorization_pause_does_not_consume_failure_budget(tmp_path):
    from telegram_phone_number_checker.user_session import UserSessionUnavailableError
    store = QueueStore(tmp_path / 'validator.db')
    job_id = store.create_job('+79991234567')
    store.start(job_id)
    store.finish_claim(store.claim_next('worker'), {'id': 123})
    class Gateway:
        def cleanup_contacts(self, user_ids):
            raise UserSessionUnavailableError('raw OTP secret')
    worker = ValidatorWorker(store, Gateway(), 'worker')
    assert worker.run_once() is True
    assert store.snapshot(job_id)['state'] == 'WAITING_AUTHORIZATION'
    assert worker.run_once() is False
    with store._connect() as connection:
        cleanup = dict(connection.execute('SELECT * FROM contact_cleanup').fetchone())
        assert cleanup['state'] == 'PENDING'
        assert cleanup['attempts'] == 0
        assert 'secret' not in cleanup['last_error']


def test_browser_login_requires_explicit_resume_after_revocation(tmp_path):
    import threading
    import time
    from urllib.parse import urlencode
    from urllib.request import urlopen
    from telegram_phone_number_checker.durable_web import DurableValidatorApplication, create_server
    from telegram_phone_number_checker.user_session import UserSessionUnavailableError

    class Session:
        state = 'AUTHORIZED'
        import_calls = 0
        successful_imports = 0

        def snapshot(self):
            return {'state': self.state, 'message': 'Test session'}

        def import_phones(self, phones):
            self.import_calls += 1
            if self.import_calls == 1:
                self.state = 'NOT_AUTHORIZED'
                raise UserSessionUnavailableError('revoked')
            self.successful_imports += 1
            return {phones[0]: {'id': 123}}

        def cleanup_contacts(self, user_ids):
            pass

        def start_authorization(self, phone):
            self.state = 'CODE_SENT'
            return True

        def submit_code(self, code):
            self.state = 'AUTHORIZED'
            return True

    def wait_for(predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('worker did not reach the expected durable state')

    session = Session()
    app = DurableValidatorApplication(tmp_path / 'validator.db', gateway=session, authorization=session)
    server = create_server(app, 0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f'http://127.0.0.1:{server.server_port}'
    try:
        job_id = app.enqueue('+79991234567')
        assert app.start(job_id)
        wait_for(lambda: app.store.snapshot(job_id)['state'] == 'WAITING_AUTHORIZATION')
        for path, values in [('/auth/begin', {'phone_number': '+79990000001'}), ('/auth/code', {'code': '12345'})]:
            with urlopen(base_url + path, data=urlencode(values).encode(), timeout=2) as response:
                assert response.status == 200
        assert session.snapshot()['state'] == 'AUTHORIZED'
        assert app.store.snapshot(job_id)['state'] == 'WAITING_AUTHORIZATION'
        assert session.successful_imports == 0
        assert app._worker.run_once() is False
        with urlopen(base_url + f'/jobs/{job_id}/resume', data=b'', timeout=2) as response:
            assert response.status == 200
        wait_for(lambda: app.store.rows(job_id)[0]['status'] == 'FOUND_NO_USERNAME')
        assert session.successful_imports == 1
    finally:
        server.shutdown()
        server_thread.join(timeout=2)
        server.server_close()
        app.close()


def test_completed_import_and_cleanup_budget_commit_before_crash(tmp_path):
    import pytest

    class CrashAfterFifthCommit(QueueStore):
        def finish_batch(self, claims, results, **kwargs):
            super().finish_batch(claims, results, **kwargs)
            if self.snapshot(claims[0]['job_id'])['processed'] == 5:
                raise SystemExit('simulated crash after durable outcome commit')

    store = CrashAfterFifthCommit(tmp_path / 'validator.db')
    job_id = store.create_job('\n'.join(f'+79990000{i:03d}' for i in range(12)))
    store.start(job_id)
    store.set_import_batch_size(job_id, 1)

    class Gateway:
        def __init__(self):
            self.calls = []
        def import_phones(self, phones):
            self.calls.append('IMPORT')
            return {phone: {'id': 123} for phone in phones}
        def cleanup_contacts(self, user_ids):
            self.calls.append('CLEANUP')

    gateway = Gateway()
    worker = ValidatorWorker(store, gateway, 'worker')
    for _ in range(4):
        assert worker.run_once()
    with pytest.raises(SystemExit):
        worker.run_once()
    reopened = QueueStore(store.path)
    assert reopened.snapshot(job_id)['processed'] == 5
    assert reopened.snapshot(job_id)['import_batch_size'] == 5
    assert reopened.snapshot(job_id)['import_success_streak'] == 0
    assert reopened.cleanup_due() is True
    assert ValidatorWorker(reopened, gateway, 'worker').run_once()
    assert gateway.calls == ['IMPORT'] * 5 + ['CLEANUP']


def test_import_accounting_failure_rolls_back_outcomes_and_evidence(tmp_path):
    import sqlite3
    import pytest
    store = QueueStore(tmp_path / 'validator.db')
    job_id = store.create_job('+79991234567')
    store.start(job_id)
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER fail_import_accounting BEFORE UPDATE OF import_success_streak ON jobs
            WHEN NEW.import_success_streak > OLD.import_success_streak
            BEGIN SELECT RAISE(ABORT, 'simulated accounting write failure'); END""")
    class Gateway:
        def import_phones(self, phones):
            return {phones[0]: {'id': 123}}
    with pytest.raises(sqlite3.IntegrityError):
        ValidatorWorker(store, Gateway(), 'worker').run_once()
    assert store.rows(job_id)[0]['status'] == 'IN_PROGRESS'
    assert store.snapshot(job_id)['processed'] == 0
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM job_item_attempts').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM contact_cleanup').fetchone()[0] == 0
        assert connection.execute('SELECT import_batches_since_cleanup FROM worker_state').fetchone()[0] == 0


def test_partial_import_retry_and_budget_share_outcome_commit(tmp_path):
    import pytest
    class CrashAfterOutcomeCommit(QueueStore):
        def finish_batch(self, claims, results, **kwargs):
            super().finish_batch(claims, results, **kwargs)
            raise SystemExit('simulated crash after partial response commit')
    clock = Clock()
    store = CrashAfterOutcomeCommit(tmp_path / 'validator.db', now=clock)
    job_id = store.create_job('+79991234567\n+79991234568')
    store.start(job_id)
    class Gateway:
        def import_phones(self, phones):
            return {phones[0]: {'id': 123}, phones[1]: {'retry': True}}
    with pytest.raises(SystemExit):
        ValidatorWorker(store, Gateway(), 'worker').run_once()
    reopened = QueueStore(store.path, now=clock)
    assert [row['status'] for row in reopened.rows(job_id)] == ['FOUND_NO_USERNAME', 'PENDING_RETRY']
    assert reopened.rows(job_id)[1]['next_attempt_at'] == 1060
    assert reopened.snapshot(job_id)['import_batch_size'] == 5
    assert reopened.snapshot(job_id)['import_success_streak'] == 0
    with reopened._connect() as connection:
        assert connection.execute('SELECT import_batches_since_cleanup FROM worker_state').fetchone()[0] == 1


def test_inflight_import_and_cleanup_floodwait_cannot_undo_operator_pause(tmp_path):
    import threading
    for operation in ('IMPORT', 'CLEANUP'):
        clock = Clock()
        store = QueueStore(tmp_path / f'{operation}.db', now=clock)
        job_id = store.create_job('+79991234567')
        store.start(job_id)
        if operation == 'CLEANUP':
            store.finish_claim(store.claim_next('worker'), {'id': 123})
        started, release = threading.Event(), threading.Event()
        class Gateway:
            calls = 0
            def request(self):
                self.calls += 1
                started.set()
                assert release.wait(2)
                raise FloodWaitError(None, 230)
            def import_phones(self, phones):
                self.request()
            def cleanup_contacts(self, user_ids):
                self.request()
        gateway = Gateway()
        worker = ValidatorWorker(store, gateway, 'worker')
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(worker.run_once()), daemon=True)
        thread.start()
        try:
            assert started.wait(2)
            assert store.pause(job_id)
        finally:
            release.set()
            thread.join(timeout=2)
        assert not thread.is_alive()
        assert outcomes == [True]
        assert store.snapshot(job_id)['state'] == 'PAUSED'
        clock.value = 1300
        assert worker.run_once() is False
        assert store.snapshot(job_id)['state'] == 'PAUSED'
        assert gateway.calls == 1
