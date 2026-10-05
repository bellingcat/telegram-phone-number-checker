from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
import time
from urllib.parse import urlencode
import asyncio
import logging
import os
import re
import signal
import subprocess
import sys
import textwrap

import pytest

from telegram_phone_number_checker import web
from telegram_phone_number_checker.durable_web import (
    DurableValidatorApplication,
    create_server as create_durable_server,
)
from telegram_phone_number_checker.queue_store import QueueStore
from telegram_phone_number_checker.user_session import UserSessionService


def _wait_for_lifecycle(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('lifecycle did not reach expected state')


@pytest.mark.parametrize('password_error', [False, True])
def test_browser_lifecycle_secrets_never_reach_database_csv_html_or_logs(tmp_path, monkeypatch, capfd, caplog, password_error):
    """Exercises real HTTP -> session handoff, including an error echoing secrets."""
    caplog.set_level(logging.DEBUG)
    otp, password = '847296135', 'task5-2fa-S3cret-20261005'
    monkeypatch.setenv('API_ID', '1')
    monkeypatch.setenv('API_HASH', 'hash')
    monkeypatch.setenv('TELEGRAM_SESSION_PATH', str(tmp_path / 'telegram-session'))

    class SecretClient(_TwoFactorAuthorizationClient):
        async def sign_in(self, phone=None, code=None, password=None):
            if password is not None and password_error:
                raise ValueError(f'echoed credentials {otp} {password}')
            await super().sign_in(phone, code, password)

    service = UserSessionService(client_factory=lambda *args, **kwargs: SecretClient())
    app = DurableValidatorApplication(tmp_path / 'validator.db', gateway=service, authorization=service)
    job_id = app.enqueue('+79990000001')
    server = create_durable_server(app, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    outputs = []
    try:
        for path, values, state in [
            ('/auth/begin', {'phone_number': '+79991112233'}, 'CODE_SENT'),
            ('/auth/code', {'code': otp}, 'PASSWORD_REQUIRED'),
            ('/auth/password', {'password': password}, 'ERROR' if password_error else 'AUTHORIZED'),
        ]:
            connection.request('POST', path, urlencode(values), {'Content-Type': 'application/x-www-form-urlencoded'})
            response = connection.getresponse()
            outputs.append(response.read().decode())
            assert response.status == 303
            _wait_for_lifecycle(lambda: service.snapshot()['state'] == state)
            for page_path in ['/', f'/jobs/{job_id}', f'/download.csv?job={job_id}']:
                connection.request('GET', page_path)
                response = connection.getresponse()
                assert response.status == 200
                outputs.append(response.read().decode())
        assert app.store.snapshot(job_id)['state'] == 'STAGED'
        with app.store._connect() as database:
            # Enumerates every current/future SQLite table, including events,
            # migration metadata, attempts, account and cleanup records.
            outputs.append('\n'.join(database.iterdump()))
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        app.close()
    captured = capfd.readouterr()
    outputs.extend([captured.out, captured.err, caplog.text])
    for output in outputs:
        assert otp not in output, 'OTP leaked into application output'
        assert password not in output, '2FA password leaked into application output'
    assert otp.encode() not in (tmp_path / 'validator.db').read_bytes()
    assert password.encode() not in (tmp_path / 'validator.db').read_bytes()


@pytest.mark.parametrize('credential', ['code', 'password'])
def test_browser_lifecycle_secrecy_detects_info_credential_logs(tmp_path, monkeypatch, capfd, caplog, credential):
    """A disposable client log must trip the real lifecycle secrecy assertion."""
    original_sign_in = _TwoFactorAuthorizationClient.sign_in

    async def sign_in_with_info_log(self, phone=None, code=None, password=None):
        value = code if credential == 'code' else password
        if value is not None:
            logging.getLogger(__name__).info('Synthetic credential: %s', value)
        return await original_sign_in(self, phone=phone, code=code, password=password)

    monkeypatch.setattr(_TwoFactorAuthorizationClient, 'sign_in', sign_in_with_info_log)
    message = 'OTP leaked' if credential == 'code' else '2FA password leaked'
    with pytest.raises(AssertionError, match=message):
        test_browser_lifecycle_secrets_never_reach_database_csv_html_or_logs(
            tmp_path, monkeypatch, capfd, caplog, password_error=False,
        )


def test_sigterm_lifecycle_exits_within_docker_grace_and_releases_active_import(tmp_path):
    """Runs the actual main/server/session/worker in a process receiving SIGTERM."""
    # Use the deployment's configured deadline rather than assuming a host
    # default (some Compose installations create a one-second StopTimeout).
    compose = (Path(__file__).resolve().parents[1] / 'compose.yaml').read_text()
    grace = re.search(r'^\s+stop_grace_period:\s*(\d+)s\s*$', compose, re.MULTILINE)
    assert grace is not None, 'Configure the Docker shutdown deadline explicitly'
    grace_seconds = int(grace.group(1))
    script = textwrap.dedent('''
        import asyncio
        import os
        from pathlib import Path
        import time
        from telegram_phone_number_checker import durable_web, main as upstream, web

        root = Path(os.environ['TELEGRAM_VALIDATOR_DATA_DIR'])
        class Client:
            connected = False
            async def connect(self): self.connected = True
            def is_connected(self): return self.connected
            async def is_user_authorized(self): return True
            async def disconnect(self): self.connected = False
            async def __call__(self, request):
                (root / 'dispatched').write_text('IMPORT dispatched')
                await asyncio.Event().wait()
        upstream.TelegramClient = lambda *args, **kwargs: Client()
        application_type = durable_web.DurableValidatorApplication
        def make_application(*args, **kwargs):
            app = application_type(*args, **kwargs)
            app.begin_authorization('+79991112233')
            deadline = time.monotonic() + 3
            while app.authorization.snapshot()['state'] != 'AUTHORIZED':
                if time.monotonic() > deadline: raise RuntimeError('authorization timed out')
                time.sleep(.01)
            job = app.enqueue('+79990000001')
            (root / 'job-id').write_text(job)
            assert app.start(job)
            return app
        durable_web.DurableValidatorApplication = make_application
        server_factory = durable_web.create_server
        def make_server(*args, **kwargs):
            kwargs['port'] = 0
            server = server_factory(*args, **kwargs)
            (root / 'port').write_text(str(server.server_port))
            return server
        durable_web.create_server = make_server
        web.main()
    ''')
    environment = {
        **os.environ, 'API_ID': '1', 'API_HASH': 'test-hash',
        'TELEGRAM_VALIDATOR_DATA_DIR': str(tmp_path), 'TELEGRAM_SESSION_PATH': str(tmp_path / 'telegram-session'),
        'TELEGRAM_VALIDATOR_PORT': '8010', 'TELEGRAM_VALIDATOR_HOST': '127.0.0.1',
    }
    process = subprocess.Popen([sys.executable, '-u', '-c', script], cwd=Path(__file__).resolve().parents[1],
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for_lifecycle(lambda: (tmp_path / 'dispatched').exists() and (tmp_path / 'port').exists(), timeout=5)
        connection = HTTPConnection('127.0.0.1', int((tmp_path / 'port').read_text()), timeout=2)
        connection.request('GET', '/')
        response = connection.getresponse()
        assert response.status == 200
        assert 'Telegram подключён.' in response.read().decode()
        connection.close()
        store = QueueStore(tmp_path / '.validator.db')
        job_id = (tmp_path / 'job-id').read_text()
        assert store.rows(job_id)[0]['status'] == 'IN_PROGRESS'
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=grace_seconds)
        assert time.monotonic() - started < grace_seconds
        assert process.returncode == 0, (stdout, stderr)
        assert 'Traceback' not in stderr
        restarted = QueueStore(tmp_path / '.validator.db')
        rows = restarted.rows(job_id)
        assert len(rows) == 1
        assert rows[0]['status'] == 'UNKNOWN_OUTCOME_REVIEW'
        assert [entry['outcome'] for entry in restarted.item_attempts(job_id, rows[0]['_item_id'])] == ['UNKNOWN_OUTCOME']
        with restarted._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM job_items WHERE lease_owner IS NOT NULL OR status='IN_PROGRESS'").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM worker_state WHERE lease_owner IS NOT NULL").fetchone()[0] == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


class _BlockingGateway:
    """Keeps one claimed item active so the durable UI can be inspected."""

    def __init__(self):
        from threading import Event

        self.called = Event()
        self.release = Event()

    def import_phones(self, phones):
        self.called.set()
        self.release.wait(timeout=2)
        return {phone: {"error": "No response"} for phone in phones}

    def resolve_phone(self, phone):
        return {"error": "No response"}


class _AuthorizationClient:
    def __init__(self):
        self.code_requested = False
        self.signed_in_with = ""
        self.disconnected = False
        self.authorized = False
        self.connected = False

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.code_requested = phone

    async def sign_in(self, phone, code):
        self.signed_in_with = (phone, code)
        self.authorized = True

    async def disconnect(self):
        self.disconnected = True
        self.connected = False


class _TwoFactorAuthorizationClient(_AuthorizationClient):
    def __init__(self):
        super().__init__()
        self.password = ""

    async def sign_in(self, phone=None, code=None, password=None):
        if password is None:
            raise web.upstream.errors.SessionPasswordNeededError(None)
        self.password = password
        self.authorized = True


def test_telegram_authorization_accepts_code_from_web_memory(monkeypatch):
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    client = _AuthorizationClient()
    authorization = UserSessionService(client_factory=lambda *args, **kwargs: client)

    assert authorization.start_authorization("+79991112233") is True
    for _ in range(20):
        if authorization.snapshot()["state"] == "CODE_SENT":
            break
        time.sleep(0.01)
    assert authorization.snapshot()["state"] == "CODE_SENT"
    assert authorization.submit_code("12345") is True
    for _ in range(20):
        if authorization.snapshot()["state"] == "AUTHORIZED":
            break
        time.sleep(0.01)

    assert client.code_requested == "+79991112233"
    assert client.signed_in_with == ("+79991112233", "12345")
    assert client.disconnected is False
    assert authorization.snapshot()["state"] == "AUTHORIZED"
    authorization.close()
    assert client.disconnected is True


def test_telegram_authorization_accepts_two_factor_password_from_web_memory(monkeypatch):
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    client = _TwoFactorAuthorizationClient()
    authorization = UserSessionService(client_factory=lambda *args, **kwargs: client)

    assert authorization.start_authorization("+79991112233") is True
    for _ in range(20):
        if authorization.snapshot()["state"] == "CODE_SENT":
            break
        time.sleep(0.01)
    assert authorization.submit_code("12345") is True
    for _ in range(20):
        if authorization.snapshot()["state"] == "PASSWORD_REQUIRED":
            break
        time.sleep(0.01)
    assert authorization.snapshot()["state"] == "PASSWORD_REQUIRED"
    assert authorization.submit_password("secret-password") is True
    for _ in range(20):
        if authorization.snapshot()["state"] == "AUTHORIZED":
            break
        time.sleep(0.01)

    assert client.password == "secret-password"
    assert authorization.snapshot()["state"] == "AUTHORIZED"
    authorization.close()


class _CodeSentAuthorization:
    def __init__(self):
        self.submitted_codes = []

    def snapshot(self):
        return {"state": "CODE_SENT", "message": "Введите код из Telegram."}

    def begin(self, phone_number=None):
        return True

    def submit_code(self, code):
        self.submitted_codes.append(code)
        return True

    def submit_password(self, password):
        return False


class _NotAuthorizedAuthorization:
    def __init__(self):
        self.phone_numbers = []

    def snapshot(self):
        return {"state": "NOT_AUTHORIZED", "message": "Подключите Telegram."}

    def begin(self, phone_number):
        self.phone_numbers.append(phone_number)
        return True

    def submit_code(self, code):
        return False

    def submit_password(self, password):
        return False


class _PasswordAuthorization:
    def __init__(self):
        self.passwords = []

    def snapshot(self):
        return {"state": "PASSWORD_REQUIRED", "message": "Введите пароль 2FA."}

    def begin(self, phone_number=None):
        return False

    def submit_code(self, code):
        return False

    def submit_password(self, password):
        self.passwords.append(password)
        return True


class _AuthorizedAuthorization:
    def snapshot(self):
        return {"state": "AUTHORIZED", "message": "Telegram подключён."}

    def begin(self, phone_number=None):
        return False

    def submit_code(self, code):
        return False

    def submit_password(self, password):
        return False


def test_durable_home_shows_telegram_phone_form_before_authorization(tmp_path):
    app = DurableValidatorApplication(
        tmp_path / "validator.db", authorization=_NotAuthorizedAuthorization()
    )

    page = app.render_home_page()

    assert 'action="/auth/begin"' in page
    assert 'name="phone_number"' in page


def test_durable_home_shows_telegram_code_form_when_authorization_waits(tmp_path):
    app = DurableValidatorApplication(
        tmp_path / "validator.db", authorization=_CodeSentAuthorization()
    )

    page = app.render_home_page()

    assert "Введите код Telegram" in page
    assert 'action="/auth/code"' in page


def test_durable_home_shows_two_factor_password_form_when_required(tmp_path):
    app = DurableValidatorApplication(
        tmp_path / "validator.db", authorization=_PasswordAuthorization()
    )

    page = app.render_home_page()

    assert "пароль 2FA" in page
    assert 'action="/auth/password"' in page


def test_durable_home_refreshes_while_telegram_verifies_otp(tmp_path):
    class _VerifyingCodeAuthorization:
        def snapshot(self):
            return {"state": "VERIFYING_CODE", "message": "Проверяем код Telegram…"}

        def begin(self, phone_number=None):
            return False

        def submit_code(self, code):
            return False

        def submit_password(self, password):
            return False

    app = DurableValidatorApplication(
        tmp_path / "validator.db", authorization=_VerifyingCodeAuthorization()
    )

    page = app.render_home_page()

    assert "Проверяем код Telegram…" in page
    assert '<meta http-equiv="refresh" content="2">' in page


def test_durable_web_submits_telegram_code_from_browser(tmp_path):
    authorization = _CodeSentAuthorization()
    app = DurableValidatorApplication(tmp_path / "validator.db", authorization=authorization)
    server = create_durable_server(app, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/auth/code",
            body=urlencode({"code": "12345"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()

        assert response.status == 303
        assert response.getheader("Location") == "/"
        assert authorization.submitted_codes == ["12345"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_durable_web_submits_telegram_phone_from_browser(tmp_path):
    authorization = _NotAuthorizedAuthorization()
    app = DurableValidatorApplication(tmp_path / "validator.db", authorization=authorization)
    server = create_durable_server(app, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/auth/begin",
            body=urlencode({"phone_number": "+79991112233"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()

        assert response.status == 303
        assert response.getheader("Location") == "/"
        assert authorization.phone_numbers == ["+79991112233"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_durable_web_submits_two_factor_password_from_browser(tmp_path):
    authorization = _PasswordAuthorization()
    app = DurableValidatorApplication(tmp_path / "validator.db", authorization=authorization)
    server = create_durable_server(app, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/auth/password",
            body=urlencode({"password": "secret-password"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()

        assert response.status == 303
        assert response.getheader("Location") == "/"
        assert authorization.passwords == ["secret-password"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_durable_gateway_defers_contact_cleanup_to_the_durable_worker(monkeypatch):
    calls = []

    class Client(_AuthorizationClient):
        async def is_user_authorized(self):
            return True

    async def get_names_batch(client, phones, *, cleanup_contacts):
        calls.append(("import", phones, cleanup_contacts))
        return {phones[0]: {"id": 123, "_cleanup_contact_id": 123}}

    async def cleanup_imported_contacts(client, user_ids):
        calls.append(("cleanup", user_ids))

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch)
    monkeypatch.setattr(web.upstream, "cleanup_imported_contacts", cleanup_imported_contacts)
    gateway = UserSessionService(client_factory=lambda *args, **kwargs: Client())
    try:
        assert gateway.import_phones(["+79991234567"])["+79991234567"]["_cleanup_contact_id"] == 123
        gateway.cleanup_contacts([123])
        assert calls == [
            ("import", ["+79991234567"], False),
            ("cleanup", [123]),
        ]
    finally:
        gateway.close()


def test_durable_job_page_shows_partial_rows_counts_and_controls_while_running(tmp_path):
    gateway = _BlockingGateway()
    app = DurableValidatorApplication(
        tmp_path / "validator.db", gateway=gateway, authorization=_AuthorizedAuthorization()
    )
    job_id = app.enqueue("+79991234567\n123")

    assert app.start(job_id) is True
    assert gateway.called.wait(timeout=2)

    page = app.render_job_page(job_id)

    assert "Обработано номеров: 1 из 2" in page
    assert "INVALID_PHONE" in page
    assert "Этап:" in page
    assert "обработано в этапе:" in page
    assert "Статусы строк" in page
    assert f'action="/jobs/{job_id}/pause"' in page
    assert f'action="/jobs/{job_id}/cancel"' in page
    assert "stop-deferred" not in page
    gateway.release.set()


def test_durable_paused_job_page_exposes_resume_and_cancel_controls(tmp_path):
    app = DurableValidatorApplication(tmp_path / "validator.db", authorization=_AuthorizedAuthorization())
    job_id = app.enqueue("+79991234567")

    assert app.start(job_id) is True
    assert app.pause(job_id) is True

    page = app.render_job_page(job_id)

    assert f'action="/jobs/{job_id}/resume"' in page
    assert f'action="/jobs/{job_id}/cancel"' in page
    assert "stop-deferred" not in page


def test_durable_application_surfaces_json_migration_failure_without_starting_worker(tmp_path):
    legacy = tmp_path / "jobs.json"
    legacy.write_text("{not json", encoding="utf-8")

    app = DurableValidatorApplication(tmp_path / "validator.db", legacy_json_path=legacy)

    assert app.startup_error
    assert app.worker_started is False
    page = app.render_home_page()
    assert "не удалось перенести" in page.lower()
    assert 'id="queue-button" type="button" disabled' in page


def test_reads_custom_port_from_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_VALIDATOR_PORT", "8010")
    assert web.get_port() == 8010


def test_durable_web_stages_job_until_explicit_start(tmp_path):
    app = DurableValidatorApplication(tmp_path / "validator.db", authorization=_AuthorizedAuthorization())

    job_id = app.enqueue("+79991234567")

    assert app.snapshot(job_id)["started"] is False
    assert app.start(job_id) is True


def test_durable_web_keeps_job_staged_until_telegram_is_authorized(tmp_path):
    app = DurableValidatorApplication(
        tmp_path / "validator.db", authorization=_CodeSentAuthorization()
    )
    job_id = app.enqueue("+79991234567")

    assert app.start(job_id) is False
    assert app.snapshot(job_id)["started"] is False


def test_staged_job_shows_authorization_requirement_before_start(tmp_path):
    app = DurableValidatorApplication(
        tmp_path / "validator.db", authorization=_CodeSentAuthorization()
    )
    job_id = app.enqueue("+79991234567")

    page = app.render_job_page(job_id)

    assert "Авторизуйте Telegram перед запуском обработки." in page
    assert f'action="/jobs/{job_id}/start"' not in page


def test_durable_home_page_lists_a_staged_queue(tmp_path):
    app = DurableValidatorApplication(tmp_path / "validator.db")
    job_id = app.enqueue("+79991234567")

    page = app.render_home_page()

    assert f'href="/jobs/{job_id}"' in page
    assert "Сохранённые очереди" in page
    assert "STAGED" in page


def test_durable_job_shows_manual_confirmation_for_privacy_limited_row(tmp_path):
    database = tmp_path / "validator.db"
    store = QueueStore(database)
    job_id = store.create_job("+79089145093")
    store.start(job_id)
    claim = store.claim_next("worker-a")
    store.finish_claim(claim, {"error": "No response, the phone number is not on Telegram or has blocked phone lookup."})
    app = DurableValidatorApplication(database)

    page = app.render_job_page(job_id)

    assert "https://t.me/+79089145093?profile" in page
    assert f'action="/jobs/{job_id}/confirm/' in page
    assert "Подтвердить вручную" in page


def test_web_uses_one_batch_lookup_for_valid_phone_numbers(monkeypatch):
    batch_calls = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        batch_calls.append(phones)
        return {phone: {"error": "No response, the phone number is not on Telegram."} for phone in phones}

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)

    rows = asyncio.run(web._check_phone_batch("+79991234567\n+79990000000"))

    assert batch_calls == [["+79991234567", "+79990000000"]]
    assert [row["status"] for row in rows] == [
        "NOT_FOUND_OR_PRIVACY_RESTRICTED",
        "NOT_FOUND_OR_PRIVACY_RESTRICTED",
    ]


def test_web_keeps_company_context_for_each_phone(monkeypatch):
    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        return {
            phone: {"id": 123, "username": "test_user", "first_name": "Тест"}
            for phone in phones
        }

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)

    rows = asyncio.run(
        web._check_phone_batch(
            "[ООО Тест](https://example.test/company/1)\n+7 (999) 123-45-67"
        )
    )

    assert rows == [
        {
            "company_name": "ООО Тест",
            "company_link": "https://example.test/company/1",
            "phone": "+79991234567",
            "status": "FOUND",
            "telegram_id": "123",
            "username": "test_user",
            "name": "Тест",
            "telegram_link": "https://t.me/test_user",
            "detail": "",
        }
    ]


def test_web_waits_and_retries_after_telegram_flood_wait(monkeypatch):
    attempts = 0
    pauses = []
    progress = []

    class Client:
        async def disconnect(self):
            return None

    class FloodWait(Exception):
        seconds = 230

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FloodWait()
        return {phones[0]: {"id": 123, "username": "test_user"}}

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web, "FloodWaitError", FloodWait, raising=False)
    monkeypatch.setattr(web.asyncio, "sleep", sleep)

    rows = asyncio.run(
        web._check_phone_batch("+79991234567", progress=lambda *args: progress.append(args))
    )

    assert attempts == 2
    assert pauses == [231]
    assert rows[0]["status"] == "FOUND"
    assert any("230" in message for _, _, message in progress)


def test_web_resolves_retry_contacts_without_reimporting_them(monkeypatch):
    batch_calls = []
    resolved_phones = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        batch_calls.append(phones)
        return {phones[0]: {"retry": True, "error": "Retry later"}}

    async def resolve_phone(client, phone):
        resolved_phones.append(phone)
        return {"id": 123, "username": "resolved_user", "first_name": "Тест"}

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web.upstream, "resolve_phone", resolve_phone, raising=False)
    monkeypatch.setattr(web, "BATCH_PAUSE_SECONDS", 0)

    rows = asyncio.run(web._check_phone_batch("+79991234567"))

    assert batch_calls == [["+79991234567"]]
    assert resolved_phones == ["+79991234567"]
    assert rows[0]["status"] == "FOUND"
    assert rows[0]["username"] == "resolved_user"


def test_web_retries_contacts_requested_by_telegram(monkeypatch):
    attempts = 0
    resolved_phones = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        nonlocal attempts
        attempts += 1
        return {phones[0]: {"retry": True, "error": "Retry later"}}

    async def resolve_phone(client, phone):
        resolved_phones.append(phone)
        return {"id": 123, "username": "test_user"}

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web.upstream, "resolve_phone", resolve_phone, raising=False)

    rows = asyncio.run(web._check_phone_batch("+79991234567"))

    assert attempts == 1
    assert resolved_phones == ["+79991234567"]
    assert rows[0]["status"] == "FOUND"


def test_web_continues_with_other_batches_while_retry_contacts_wait(monkeypatch):
    calls = []
    resolved_phones = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        calls.append(phones)
        phone = phones[0]
        if phone == "+79990000001" and calls.count(phones) == 1:
            return {phone: {"retry": True, "error": "Retry later"}}
        return {phone: {"id": 123, "username": "test_user"}}

    async def resolve_phone(client, phone):
        resolved_phones.append(phone)
        return {"id": 123, "username": "test_user"}

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web.upstream, "resolve_phone", resolve_phone, raising=False)
    monkeypatch.setattr(web, "BATCH_SIZE", 1)
    monkeypatch.setattr(web, "BATCH_PAUSE_SECONDS", 0)

    rows = asyncio.run(web._check_phone_batch("+79990000001\n+79990000002"))

    assert calls == [["+79990000001"], ["+79990000002"]]
    assert resolved_phones == ["+79990000001"]
    assert [row["status"] for row in rows] == ["FOUND", "FOUND"]


def test_web_resolves_multiple_deferred_batches_one_by_one(monkeypatch):
    calls = []
    pauses = []
    resolved_phones = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        phone = phones[0]
        calls.append(phone)
        return {phone: {"retry": True, "error": "Retry later"}}

    async def resolve_phone(client, phone):
        resolved_phones.append(phone)
        return {"id": 123, "username": "test_user"}

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web.upstream, "resolve_phone", resolve_phone, raising=False)
    monkeypatch.setattr(web, "BATCH_SIZE", 1)
    monkeypatch.setattr(web, "BATCH_PAUSE_SECONDS", 0)
    monkeypatch.setattr(web.asyncio, "sleep", sleep)

    rows = asyncio.run(web._check_phone_batch("+79990000001\n+79990000002"))

    assert calls == ["+79990000001", "+79990000002"]
    assert resolved_phones == ["+79990000001", "+79990000002"]
    assert pauses == [web.RESOLVE_PHONE_PAUSE_SECONDS]
    assert [row["status"] for row in rows] == ["FOUND", "FOUND"]


def test_web_marks_directly_resolved_absence_without_reimporting(monkeypatch):
    calls = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        calls.append(phones)
        return {phones[0]: {"retry": True, "error": "Retry later"}}

    async def resolve_phone(client, phone):
        return {"error": "No response, the phone number is not on Telegram or has blocked phone lookup."}

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web.upstream, "resolve_phone", resolve_phone, raising=False)
    monkeypatch.setattr(web, "BATCH_SIZE", 1)
    monkeypatch.setattr(web, "BATCH_PAUSE_SECONDS", 0)

    rows = asyncio.run(web._check_phone_batch("+79991234567"))

    assert len(calls) == 1
    assert rows[0]["status"] == "NOT_FOUND_OR_PRIVACY_RESTRICTED"


def test_web_resolves_an_entire_deferred_batch_without_scheduling_another_import(monkeypatch):
    scheduled = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        return {phone: {"retry": True, "error": "Retry later"} for phone in phones}

    async def resolve_phone(client, phone):
        return {"id": 123, "username": "test_user"}

    async def sleep(seconds):
        return None

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web.upstream, "resolve_phone", resolve_phone, raising=False)
    monkeypatch.setattr(web.asyncio, "sleep", sleep)

    rows = asyncio.run(
        web._check_phone_batch(
            "+79991234567\n+79990000000",
            schedule_later=lambda phones: scheduled.extend(phones),
        )
    )

    assert scheduled == []
    assert [row["status"] for row in rows] == ["FOUND", "FOUND"]


def test_web_splits_eleven_phones_into_batches_of_ten(monkeypatch):
    batch_calls = []

    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        batch_calls.append(phones)
        return {phone: {"error": "No response"} for phone in phones}

    phones = [f"+799900000{number:02d}" for number in range(1, 12)]
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)
    monkeypatch.setattr(web, "BATCH_PAUSE_SECONDS", 0)

    asyncio.run(web._check_phone_batch("\n".join(phones)))

    assert batch_calls == [phones[:10], phones[10:]]


def test_csv_neutralizes_spreadsheet_formula_cells():
    csv_text = web.rows_to_csv(
        [
            {
                "phone": "+79991234567",
                "status": "FOUND",
                "telegram_id": "123",
                "username": "=HYPERLINK(\"https://example.test\")",
                "name": "@formula",
                "telegram_link": "https://t.me/test_user",
                "detail": "-danger",
            }
        ]
    )
    assert "'+79991234567" in csv_text
    assert "'=HYPERLINK" in csv_text
    assert "'@formula" in csv_text
    assert "'-danger" in csv_text


def test_job_state_survives_restart_and_keeps_completed_phone_results(tmp_path):
    state_path = tmp_path / "jobs.json"
    app = web.ValidatorApplication(state_path=state_path)
    job_id = app.enqueue("ООО Тест\n+79991234567\n+79990000000")
    app._update_job(
        job_id,
        started=True,
        processed=1,
        results_by_phone={"+79991234567": {"id": 123, "username": "test_user"}},
    )

    restored = web.ValidatorApplication(state_path=state_path)
    job = restored.snapshot(job_id)

    assert job["started"] is False
    assert job["results_by_phone"]["+79991234567"]["id"] == 123
    assert "перезапущен" in job["message"]


def test_deferred_csv_rows_keep_company_context():
    rows = web.deferred_rows(
        "ООО Тест\n+79991234567",
        ["+79991234567"],
    )

    assert rows[0]["company_name"] == "ООО Тест"
    assert rows[0]["status"] == "DEFERRED_NOT_CHECKED"


def test_web_queues_job_and_keeps_browser_responsive():
    calls = []

    def check_batch(raw: str):
        calls.append(raw)
        time.sleep(0.05)
        return [
            {
                "company_name": "ООО Тест",
                "company_link": "",
                "phone": "+79991234567",
                "status": "FOUND",
                "telegram_id": "123",
                "username": "test_user",
                "name": "Тест",
                "telegram_link": "https://t.me/test_user",
                "detail": "",
            }
        ]

    app = web.ValidatorApplication(check_batch=check_batch)
    server = web.create_server(app, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/check",
            body=urlencode({"phone_numbers": "+79991234567", "queue_confirmation": "yes"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        job_url = response.getheader("Location")
        assert job_url.startswith("/jobs/")
        assert calls == []

        connection.request("GET", job_url)
        response = connection.getresponse()
        page = response.read().decode()
        assert "Запустить обработку" in page

        connection.request("POST", f"{job_url}/start")
        response = connection.getresponse()
        response.read()
        assert response.status == 303

        for _ in range(20):
            connection.request("GET", job_url)
            response = connection.getresponse()
            page = response.read().decode()
            if "Готово." in page:
                break
            time.sleep(0.02)
        assert response.status == 200
        assert "ООО Тест" in page
        assert "FOUND" in page
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_can_stop_deferred_contacts_without_dropping_them(monkeypatch):
    class Client:
        async def disconnect(self):
            return None

    async def login(*args):
        return Client()

    async def get_names_batch(client, phones):
        return {phones[0]: {"retry": True, "error": "Retry later"}}

    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("PHONE_NUMBER", "+79990000000")
    monkeypatch.setattr(web.upstream, "login", login)
    monkeypatch.setattr(web.upstream, "get_names_batch", get_names_batch, raising=False)

    rows = asyncio.run(
        web._check_phone_batch("+79991234567", skip_deferred=lambda: True)
    )

    assert rows[0]["status"] == "DEFERRED_NOT_CHECKED"


def test_web_does_not_queue_without_explicit_confirmation():
    calls = []

    def check_batch(raw: str):
        calls.append(raw)
        return []

    app = web.ValidatorApplication(check_batch=check_batch)
    server = web.create_server(app, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/check",
            body=urlencode({"phone_numbers": "+79991234567"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        page = response.read().decode()
        assert response.status == 200
        assert "Нажмите «Поставить в очередь»" in page
        time.sleep(0.05)
        assert calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_job_page_keeps_the_submitted_input_visible():
    page = web.render_page(
        job={
            "id": "job-1",
            "raw": 'ООО Тест\n+7 (999) 123-45-67',
            "total": 1,
            "processed": 0,
            "message": "Задача поставлена в очередь.",
            "rows": [],
            "error": "",
            "complete": False,
        }
    )

    assert 'ООО Тест\n+7 (999) 123-45-67' in page


def test_web_form_renders_results_and_downloads_csv():
    def check_batch(raw: str):
        assert raw == "+79991234567"
        return [
            {
                "phone": "+79991234567",
                "status": "FOUND",
                "telegram_id": "123",
                "username": "test_user",
                "name": "Тест Пользователь",
                "telegram_link": "https://t.me/test_user",
                "detail": "",
            }
        ]

    app = web.ValidatorApplication(check_batch=check_batch)
    server = web.create_server(app, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        body = urlencode({"phone_numbers": "+79991234567", "queue_confirmation": "yes"})
        connection.request(
            "POST",
            "/check",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        job_url = response.getheader("Location")
        connection.request("POST", f"{job_url}/start")
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        for _ in range(20):
            connection.request("GET", job_url)
            response = connection.getresponse()
            page = response.read().decode()
            if "Готово." in page:
                break
            time.sleep(0.02)
        assert response.status == 200
        assert "FOUND" in page
        assert "https://t.me/test_user" in page

        connection.request("GET", f"/download.csv?job={job_url.rsplit('/', 1)[1]}")
        response = connection.getresponse()
        csv_text = response.read().decode()
        assert response.status == 200
        assert "telegram_link" in csv_text
        assert "https://t.me/test_user" in csv_text
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_form_shows_configuration_problem_without_calling_telegram():
    def check_batch(raw: str):
        raise web.ConfigurationError("Не заданы API_ID, API_HASH, PHONE_NUMBER в .env")

    app = web.ValidatorApplication(check_batch=check_batch)
    server = web.create_server(app, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        body = urlencode({"phone_numbers": "+79991234567", "queue_confirmation": "yes"})
        connection.request(
            "POST",
            "/check",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        job_url = response.getheader("Location")
        connection.request("POST", f"{job_url}/start")
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        for _ in range(20):
            connection.request("GET", job_url)
            response = connection.getresponse()
            page = response.read().decode()
            if "Не заданы API_ID" in page:
                break
            time.sleep(0.02)
        assert response.status == 200
        assert "API_ID" in page
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_form_shows_authorization_problem_instead_of_dropping_response():
    def check_batch(raw: str):
        raise EOFError("EOF when reading a Telegram code")

    app = web.ValidatorApplication(check_batch=check_batch)
    server = web.create_server(app, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        body = urlencode({"phone_numbers": "+79991234567", "queue_confirmation": "yes"})
        connection.request(
            "POST",
            "/check",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        job_url = response.getheader("Location")
        connection.request("POST", f"{job_url}/start")
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        for _ in range(20):
            connection.request("GET", job_url)
            response = connection.getresponse()
            page = response.read().decode()
            if "Требуется авторизация Telegram" in page:
                break
            time.sleep(0.02)
        assert response.status == 200
        assert "Требуется авторизация Telegram" in page
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_uses_durable_data_directory_from_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_VALIDATOR_DATA_DIR", "/data")

    assert web.get_data_dir() == Path("/data")


def test_uses_configured_listen_host(monkeypatch):
    monkeypatch.setenv("TELEGRAM_VALIDATOR_HOST", "0.0.0.0")

    assert web.get_host() == "0.0.0.0"


def test_default_durable_application_shares_session_and_closes_it(tmp_path):
    from telegram_phone_number_checker.user_session import UserSessionService
    app = DurableValidatorApplication(tmp_path / 'validator.db')
    try:
        assert isinstance(app.gateway, UserSessionService)
        assert app.authorization is app.gateway
    finally:
        app.close()
    assert not app._worker_thread.is_alive()


def test_browser_authorization_does_not_start_waiting_job(tmp_path):
    class Session(_CodeSentAuthorization):
        def __init__(self):
            super().__init__()
            self.state = 'CODE_SENT'
            self.calls = []
        def snapshot(self):
            return {'state': self.state, 'message': 'Введите код из Telegram.'}
        def submit_code(self, code):
            self.state = 'AUTHORIZED'
            return True
        def import_phones(self, phones):
            self.calls.append(phones)
            return {phone: {'error': 'No response'} for phone in phones}
        def cleanup_contacts(self, ids):
            pass
        def close(self):
            pass
    session = Session()
    app = DurableValidatorApplication(tmp_path / 'validator.db', gateway=session, authorization=session)
    try:
        job_id = app.enqueue('+79991234567')
        assert not app.start(job_id)
        assert app.submit_authorization_code('12345')
        time.sleep(.05)
        assert session.calls == []
        assert app.snapshot(job_id)['state'] == 'STAGED'
        assert app.start(job_id)
    finally:
        app.close()


def test_web_main_sigterm_shutdown_runs_on_helper_thread_and_closes_application(monkeypatch, tmp_path):
    import signal
    from threading import Event, get_ident
    from telegram_phone_number_checker import durable_web
    events = []
    handler = {}
    stopped = Event()
    main_thread = get_ident()
    class App:
        def __init__(self, *args, **kwargs):
            pass
        def close(self):
            events.append('app-close')
    class Server:
        def serve_forever(self):
            events.append('serve')
            handler['sigterm'](signal.SIGTERM, None)
            assert stopped.wait(2)
            events.append('serve-stopped')
        def shutdown(self):
            assert get_ident() != main_thread
            events.append('shutdown')
            stopped.set()
        def server_close(self):
            events.append('server-close')
    previous_handler = object()
    monkeypatch.setattr(signal, 'getsignal', lambda *_: previous_handler)
    def install(sig, callback):
        if callback is not previous_handler:
            handler['sigterm'] = callback
    monkeypatch.setattr(signal, 'signal', install)
    monkeypatch.setattr(durable_web, 'DurableValidatorApplication', App)
    monkeypatch.setattr(durable_web, 'create_server', lambda *args, **kwargs: Server())
    monkeypatch.setenv('TELEGRAM_VALIDATOR_DATA_DIR', str(tmp_path))
    web.main()
    assert events == ['serve', 'shutdown', 'serve-stopped', 'server-close', 'app-close']


@pytest.fixture
def review_web_app(tmp_path, monkeypatch):
    # All review scenarios use a temporary database and perform no Telegram IO.
    monkeypatch.setattr(DurableValidatorApplication, '_run_worker', lambda self: self._stop.wait())
    app = DurableValidatorApplication(tmp_path / 'validator.db', gateway=_BlockingGateway(), authorization=_AuthorizedAuthorization())
    job_id = app.enqueue('+79990000000\n+79991111111\n+79992222222')
    app.store.start(job_id)
    app.store.pause(job_id)
    with app.store._connect() as connection:
        connection.execute("UPDATE job_items SET method='DIRECT_LOOKUP', status='UNRESOLVABLE_OR_PRIVACY', api_status='UNRESOLVABLE_OR_PRIVACY' WHERE job_id=? AND ordinal=0", (job_id,))
        connection.execute("UPDATE job_items SET status='UNKNOWN_OUTCOME_REVIEW', api_status='UNKNOWN_OUTCOME' WHERE job_id=? AND ordinal=1", (job_id,))
        connection.execute("UPDATE job_items SET status='FOUND', api_status='FOUND', username='known_user', telegram_link='https://t.me/known_user' WHERE job_id=? AND ordinal=2", (job_id,))
    yield app, job_id
    app.close()


def test_paused_job_page_exposes_legacy_direct_and_unknown_outcome_review_actions(review_web_app):
    app, job_id = review_web_app
    page = app.render_job_page(job_id)
    assert f'action="/jobs/{job_id}/requeue-legacy-direct"' in page
    assert 'Вернуть старые прямые проверки в очередь импорта' in page
    assert f'action="/jobs/{job_id}/requeue-unknown-outcomes"' in page
    assert 'UNKNOWN_OUTCOME_REVIEW' in page
    assert 'Результат предыдущего запроса неизвестен' in page
    assert 'https://t.me/+79990000000?profile' in page


@pytest.mark.parametrize('blocked_by', ['RUNNING', 'item', 'cleanup'])
def test_legacy_direct_review_page_hides_actions_and_gives_exact_block_reason(review_web_app, blocked_by):
    app, job_id = review_web_app
    with app.store._connect() as connection:
        if blocked_by == 'RUNNING':
            connection.execute("UPDATE jobs SET state='RUNNING' WHERE id=?", (job_id,))
        elif blocked_by == 'item':
            connection.execute("UPDATE job_items SET status='IN_PROGRESS' WHERE job_id=? AND ordinal=2", (job_id,))
        else:
            connection.execute("INSERT INTO contact_cleanup(job_id, telegram_user_id, state, created_at) VALUES (?, 123, 'IN_PROGRESS', 999)", (job_id,))
    page = app.render_job_page(job_id)
    assert f'action="/jobs/{job_id}/requeue-legacy-direct"' not in page
    assert f'action="/jobs/{job_id}/requeue-unknown-outcomes"' not in page
    assert ('PAUSED' if blocked_by == 'RUNNING' else 'Активные запросы Telegram: 1') in page


@pytest.mark.parametrize('action', ['requeue-legacy-direct', 'requeue-unknown-outcomes'])
@pytest.mark.parametrize('blocked_by', [None, 'RUNNING', 'item', 'cleanup'])
def test_legacy_direct_and_unknown_review_http_actions_are_explicit_and_atomic(review_web_app, action, blocked_by):
    app, job_id = review_web_app
    with app.store._connect() as connection:
        if blocked_by == 'RUNNING':
            connection.execute("UPDATE jobs SET state='RUNNING' WHERE id=?", (job_id,))
        elif blocked_by == 'item':
            connection.execute("UPDATE job_items SET status='IN_PROGRESS' WHERE job_id=? AND ordinal=2", (job_id,))
        elif blocked_by == 'cleanup':
            connection.execute("INSERT INTO contact_cleanup(job_id, telegram_user_id, state, created_at) VALUES (?, 123, 'IN_PROGRESS', 999)", (job_id,))
    before = app.store.rows(job_id)
    server = create_durable_server(app, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection('127.0.0.1', server.server_port)
    try:
        connection.request('POST', f'/jobs/{job_id}/{action}')
        response = connection.getresponse()
        body = response.read().decode()
        if blocked_by:
            assert response.status == 409
            assert ('PAUSED' if blocked_by == 'RUNNING' else 'Активные запросы Telegram: 1') in body
            assert app.store.rows(job_id) == before
        else:
            assert response.status == 303
            assert response.getheader('Location') == f'/jobs/{job_id}'
            connection.request('GET', f'/jobs/{job_id}')
            response = connection.getresponse()
            page = response.read().decode()
            assert 'Возвращено в очередь импорта: 1.' in page
            assert app.store.snapshot(job_id)['state'] == 'PAUSED'
            rows = app.store.rows(job_id)
            changed_index = 0 if action == 'requeue-legacy-direct' else 1
            assert rows[changed_index]['status'] == 'PENDING_RETRY'
            assert rows[2] == before[2]
            connection.request('GET', f'/download.csv?job={job_id}')
            response = connection.getresponse()
            csv = response.read().decode()
            assert response.status == 200
            assert 'PENDING_RETRY' in csv and 'https://t.me/known_user' in csv
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_paused_review_page_explains_active_claim_even_without_remaining_review_rows(review_web_app):
    app, job_id = review_web_app
    with app.store._connect() as connection:
        connection.execute("UPDATE job_items SET status='IN_PROGRESS' WHERE job_id=? AND ordinal=0", (job_id,))
        connection.execute("UPDATE job_items SET status='FOUND', api_status='FOUND' WHERE job_id=? AND ordinal=1", (job_id,))
    page = app.render_job_page(job_id)
    assert 'Активные запросы Telegram: 1. Дождитесь завершения перед возвратом в очередь.' in page
    assert f'action="/jobs/{job_id}/requeue-legacy-direct"' not in page
    assert f'action="/jobs/{job_id}/requeue-unknown-outcomes"' not in page


def test_privacy_ambiguity_page_separates_outcomes_and_explains_retry(review_web_app):
    app, job_id = review_web_app
    with app.store._connect() as connection:
        connection.execute("UPDATE job_items SET status='PENDING_RETRY', method='IMPORT', next_attempt_at=?, detail='Telegram отложил импорт; номер сохранён для повторного импорта.' WHERE job_id=? AND ordinal=1", (time.time() + 300, job_id))
    page = app.render_job_page(job_id)
    assert 'Подтверждено API: 1' in page
    assert 'API не определил из-за приватности: 1' in page
    assert 'Повторный импорт ожидают: 1' in page
    assert 'Требуется повторная авторизация: 0' in page
    assert 'не доказывает отсутствие аккаунта' in page
    assert 'Следующая попытка импорта через' in page
    assert 'Telegram отложил импорт' in page
    assert 'Оценка до завершения' not in page


def test_authorization_wait_page_requires_explicit_resume_after_browser_login(review_web_app):
    app, job_id = review_web_app
    class Authorization(_CodeSentAuthorization):
        state = 'CODE_SENT'
        def snapshot(self):
            return {'state': self.state, 'message': 'Введите код Telegram.'}
        def submit_code(self, code):
            self.state = 'AUTHORIZED'
            return True
    app.authorization = Authorization()
    with app.store._connect() as connection:
        connection.execute("UPDATE jobs SET state='WAITING_AUTHORIZATION' WHERE id=?", (job_id,))
    page = app.render_job_page(job_id)
    assert 'Требуется повторная авторизация: 1' in page
    assert 'Возобновить после авторизации' in page
    assert f'action="/jobs/{job_id}/resume"' not in page
    assert app.start(job_id) is False
    assert app.submit_authorization_code('12345') is True
    assert app.store.snapshot(job_id)['state'] == 'WAITING_AUTHORIZATION'
    assert app.store.claim_next('other-worker') is None
    page = app.render_job_page(job_id)
    assert f'action="/jobs/{job_id}/resume"' in page
    assert 'Возобновить после авторизации' in page
    assert app.start(job_id) is True
    assert app.start(job_id) is False
    assert sum(event['kind'] == 'STARTED' for event in app.store.events(job_id)) == 2


@pytest.mark.parametrize('outcome, reason, deadline', [
    ('retry', 'Ошибка запроса очистки Telegram; повтор запланирован.', 1120),
    ('floodwait', 'Telegram ограничил очистку контактов.', 1300),
    ('unknown', 'Результат очистки неизвестен; требуется решение оператора.', None),
])
def test_cleanup_wait_page_exposes_real_cleanup_reason_and_deadline(review_web_app, monkeypatch, outcome, reason, deadline):
    app, _ = review_web_app
    app.store.now = lambda: 1000
    monkeypatch.setattr(time, 'time', lambda: 1000)
    job_id = app.enqueue('+79993333333\n+79994444444')
    app.store.start(job_id)
    app.store.finish_claim(app.store.claim_next('worker'), {'id': 123})
    claim = app.store.claim_cleanup('worker')
    assert claim is not None
    if outcome == 'retry':
        app.store.retry_cleanup(claim, RuntimeError('OTP=secret password=secret'))
    elif outcome == 'floodwait':
        app.store.defer_cleanup_floodwait(claim, 300)
    else:
        app.store.defer_cleanup_unknown_outcome(claim)
    snapshot = app.store.snapshot(job_id)
    assert snapshot['cleanup_waits'] == [{
        'state': 'UNKNOWN_OUTCOME_REVIEW' if outcome == 'unknown' else 'PENDING',
        'reason': reason, 'next_attempt_at': deadline, 'count': 1,
    }]
    page = app.render_job_page(job_id)
    assert reason in page
    assert 'Оценка до завершения' not in page
    assert 'ожидает worker' not in page
    assert 'secret' not in page
    if deadline is not None:
        assert 'Следующая попытка очистки через' in page
        assert f'data-next-attempt-at="{deadline}"' in page
    else:
        assert 'Автоматический повтор очистки не назначен.' in page


def test_other_job_page_shows_account_floodwait_and_hides_eta(review_web_app, monkeypatch):
    app, _ = review_web_app
    clock = [1000]
    app.store.now = lambda: clock[0]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    other_job = app.enqueue('+79993333333\n+79994444444')
    app.store.start(other_job)
    app.store.finish_claim(app.store.claim_next('worker'), {'error': 'No response'})
    app.store.pause(other_job)
    flood_job = app.enqueue('+79995555555')
    app.store.start(flood_job)
    clock[0] = 1010
    app.store.defer_account_floodwait([app.store.claim_next('worker')], 300)
    app.store.start(other_job)
    page = app.render_job_page(other_job)
    assert 'Telegram ограничил запросы для всего аккаунта; ожидание сохранено.' in page
    assert 'следующая попытка через 5 мин. 0 сек.' in page
    assert 'Оценка до завершения' not in page
    assert 'ожидает worker' not in page
