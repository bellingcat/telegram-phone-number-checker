"""Persistent owner tests at the Telethon boundary, without network or prompts."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
import time
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError, SessionPasswordNeededError, SessionRevokedError
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest, ResolvePhoneRequest

from telegram_phone_number_checker import main as upstream
from telegram_phone_number_checker.user_session import (
    UserSessionService, UserSessionUnavailableError,
    RequestNotDispatchedError, RequestOutcomeUnknownError,
)


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return False


class Client:
    def __init__(self, *, authorized=True, connect_error=None, request_error=None, block=False, two_factor=False):
        self.authorized = authorized
        self.connect_error = connect_error
        self.request_error = request_error
        self.block = block
        self.two_factor = two_factor
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.requests = []
        self.threads = set()
        self.request_started = Event()
        self.sign_ins = []

    async def connect(self):
        self.threads.add(get_ident())
        self.connect_calls += 1
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        self.threads.add(get_ident())
        return self.authorized

    async def send_code_request(self, phone):
        self.code_phone = phone

    async def sign_in(self, phone=None, code=None, password=None):
        self.sign_ins.append((phone, code, password))
        if self.request_error:
            raise self.request_error
        if self.two_factor and password is None:
            raise SessionPasswordNeededError(None)
        self.authorized = True

    async def disconnect(self):
        self.threads.add(get_ident())
        self.disconnect_calls += 1
        self.connected = False

    async def __call__(self, request):
        self.threads.add(get_ident())
        self.requests.append(request)
        self.request_started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.request_error:
            raise self.request_error
        if isinstance(request, ImportContactsRequest):
            return SimpleNamespace(imported=[], users=[], retry_contacts=[])
        if isinstance(request, ResolvePhoneRequest):
            return SimpleNamespace(users=[])
        assert isinstance(request, DeleteContactsRequest)


class Factory:
    def __init__(self, *clients):
        self.clients = list(clients)
        self.created = 0
        self.arguments = []

    def __call__(self, *args, **kwargs):
        self.arguments.append((args, kwargs))
        client = self.clients[self.created]
        self.created += 1
        return client


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv('API_ID', '1')
    monkeypatch.setenv('API_HASH', 'hash')
    monkeypatch.setenv('TELEGRAM_SESSION_PATH', '/data/telegram-session')
    monkeypatch.setattr(upstream, 'login', lambda *args: pytest.fail('Interactive login called'))


@pytest.fixture
def services():
    created = []
    def build(factory):
        service = UserSessionService(client_factory=factory)
        created.append(service)
        return service
    yield build
    for service in created:
        service.close(timeout=.1)


def test_one_authorized_client_serves_two_import_batches_and_cleanup(services):
    client = Client()
    factory = Factory(client)
    service = services(factory)
    first = service.import_phones(['+79990000001'])
    second = service.import_phones(['+79990000002'])
    service.cleanup_contacts([1])
    assert 'blocked contact adding' in first['+79990000001']['error']
    assert 'blocked contact adding' in second['+79990000002']['error']
    assert factory.created == 1
    assert client.connect_calls == 1
    assert len(client.threads) == 1
    assert factory.arguments[0][0][0] == '/data/telegram-session'
    assert factory.arguments[0][1]['flood_sleep_threshold'] == 0
    assert factory.arguments[0][1]['auto_reconnect'] is False
    assert factory.arguments[0][1]['request_retries'] == 0
    assert factory.arguments[0][1]['connection_retries'] == 0
    assert client.disconnect_calls == 0
    assert [type(request) for request in client.requests] == [ImportContactsRequest, ImportContactsRequest, DeleteContactsRequest]


def test_real_telethon_sender_response_loss_is_quarantined_without_replay(services, tmp_path):
    """Keep Telethon request/reconnect logic real; replace only network transport."""
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telegram_phone_number_checker.queue_store import QueueStore
    from telegram_phone_number_checker.validator_worker import ValidatorWorker

    clients, dispatched, reconnects = [], [], []

    class Transport:
        async def disconnect(self):
            pass

    def factory(session, api_id, api_hash, **options):
        # Fail promptly if defaults return, instead of contacting a real DC.
        assert options['auto_reconnect'] is False
        assert options['request_retries'] == 0
        client = TelegramClient(MemorySession(), api_id, api_hash, **options)
        clients.append(client)
        sender = client._sender

        async def connect():
            sender._connection = Transport()
            sender._user_connected = True

        async def authorized():
            return True

        async def forbidden_reconnect():
            reconnects.append(True)
            raise ConnectionError('unexpected transport replay')

        original_send = sender.send

        async def dispatch_then_lose_response():
            batch, _ = await sender._send_queue.get()
            for state in batch:
                dispatched.append(state.request)
                sender._pending_state[state.msg_id] = state
            sender._start_reconnect(ConnectionError('response lost after dispatch'))

        def send(request, ordered=False):
            future = original_send(request, ordered=ordered)
            asyncio.create_task(dispatch_then_lose_response())
            return future

        client.connect = connect
        client.is_user_authorized = authorized
        sender._connect = forbidden_reconnect
        sender.send = send
        return client

    service = services(factory)
    store = QueueStore(tmp_path / 'validator.db')
    job_id = store.create_job('+79990000001')
    assert store.start(job_id)
    worker = ValidatorWorker(store, service, 'worker')
    assert worker.run_once()
    assert store.rows(job_id)[0]['status'] == 'UNKNOWN_OUTCOME_REVIEW'
    assert [attempt['outcome'] for attempt in store.item_attempts(job_id, store.rows(job_id)[0]['_item_id'])] == ['UNKNOWN_OUTCOME']
    assert worker.run_once() is False
    assert ValidatorWorker(QueueStore(store.path), service, 'restarted').run_once() is False
    assert len(clients) == 1
    assert [type(request) for request in dispatched] == [ImportContactsRequest]
    assert reconnects == []
    assert not clients[0].is_connected()


def test_authorization_handoff_keeps_same_client_for_first_import(services):
    client = Client(authorized=False)
    factory = Factory(client)
    service = services(factory)
    assert service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'CODE_SENT')
    assert service.submit_code('12345')
    assert wait_until(lambda: service.snapshot()['state'] == 'AUTHORIZED')
    service.import_phones(['+79990000001'])
    assert factory.created == 1
    assert client.connect_calls == 1
    assert client.connected


def test_two_factor_handoff_and_invalid_submission_states(services):
    client = Client(authorized=False, two_factor=True)
    service = services(Factory(client))
    assert not service.submit_code('12345')
    assert not service.submit_password('password')
    assert not service.start_authorization('bad phone')
    assert service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'CODE_SENT')
    assert not service.start_authorization('+79991112233')
    assert service.submit_code('12345')
    assert wait_until(lambda: service.snapshot()['state'] == 'PASSWORD_REQUIRED')
    assert service.submit_password('private-password')
    assert wait_until(lambda: service.snapshot()['state'] == 'AUTHORIZED')
    assert client.sign_ins[-1] == (None, None, 'private-password')
    assert 'private-password' not in str(service.snapshot())
    assert client.disconnect_calls == 0


def test_pre_dispatch_failure_recreates_authorized_client_for_later_retry(services):
    broken, replacement = Client(connect_error=ConnectionError('transport detail')), Client()
    factory = Factory(broken, replacement)
    service = services(factory)
    with pytest.raises(RequestNotDispatchedError):
        service.import_phones(['+79990000001'])
    assert not broken.requests
    assert factory.created == 1  # no automatic replay
    service.import_phones(['+79990000001'])
    assert factory.created == 2
    assert replacement.authorized
    assert broken.disconnect_calls == 1


def test_revoked_session_requires_browser_authorization_without_upstream_login(services):
    client = Client(authorized=False)
    service = services(Factory(client))
    with pytest.raises(UserSessionUnavailableError):
        service.import_phones(['+79990000001'])
    assert client.requests == []
    assert service.snapshot()['state'] == 'NOT_AUTHORIZED'


def test_dispatched_failure_is_unknown_and_not_replayed(services):
    broken, replacement = Client(request_error=ConnectionError('private detail')), Client()
    factory = Factory(broken, replacement)
    service = services(factory)
    with pytest.raises(RequestOutcomeUnknownError) as caught:
        service.import_phones(['+79990000001'])
    assert 'private detail' not in str(caught.value)
    assert len(broken.requests) == 1
    assert factory.created == 1
    service.resolve_phone('+79990000001')
    assert factory.created == 2
    assert len(replacement.requests) == 1


def test_revocation_during_request_is_sanitized_unavailable(services):
    client = Client(request_error=SessionRevokedError(None))
    service = services(Factory(client))
    with pytest.raises(UserSessionUnavailableError):
        service.resolve_phone('+79990000001')
    assert service.snapshot()['state'] == 'NOT_AUTHORIZED'


def test_floodwait_is_passed_to_durable_worker_without_sleep_or_replay(services):
    flood = FloodWaitError(None, capture=73)
    client = Client(request_error=flood)
    service = services(Factory(client))
    with pytest.raises(FloodWaitError) as caught:
        service.import_phones(['+79990000001'])
    assert caught.value.seconds == 73
    assert len(client.requests) == 1
    assert client.disconnect_calls == 0


def test_authorization_failure_does_not_expose_raw_errors(services):
    client = Client(authorized=False, request_error=ValueError('12345 private-password sensitive raw error'))
    service = services(Factory(client))
    service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'CODE_SENT')
    service.submit_code('12345')
    assert wait_until(lambda: service.snapshot()['state'] == 'ERROR')
    assert 'sensitive' not in str(service.snapshot())
    assert '12345' not in str(service.snapshot())


def test_close_interrupts_blocked_operation_and_disconnects_once(services):
    client = Client(block=True)
    service = services(Factory(client))
    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(service.import_phones, ['+79990000001'])
        assert client.request_started.wait(2)
        service.close(timeout=.05)
        with pytest.raises(RequestOutcomeUnknownError):
            request.result(timeout=2)
    assert client.disconnect_calls == 1
    service.close()
    assert client.disconnect_calls == 1
    with pytest.raises(UserSessionUnavailableError):
        service.resolve_phone('+79990000001')


def test_operations_are_serialized_on_one_loop(services):
    client = Client()
    service = services(Factory(client))
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(service.resolve_phone, ['+79990000001'] * 3))
    assert len(results) == 3
    assert client.connect_calls == 1
    assert len(client.threads) == 1


def test_idle_close_does_not_wait_for_telethon_background_tasks(services):
    class BackgroundClient(Client):
        async def connect(self):
            await super().connect()
            self.background = asyncio.create_task(asyncio.Event().wait())
        async def disconnect(self):
            self.background.cancel()
            await asyncio.gather(self.background, return_exceptions=True)
            await super().disconnect()
    client = BackgroundClient()
    service = services(Factory(client))
    service.resolve_phone('+79990000001')
    started = time.monotonic()
    service.close(timeout=.5)
    assert time.monotonic() - started < .2
    assert client.disconnect_calls == 1


def test_malformed_begin_does_not_destroy_pending_authorization(services):
    client = Client(authorized=False)
    service = services(Factory(client))
    service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'CODE_SENT')
    assert not service.start_authorization('bad phone')
    assert service.snapshot()['state'] == 'CODE_SENT'
    assert service.submit_code('12345')
    assert wait_until(lambda: service.snapshot()['state'] == 'AUTHORIZED')


def test_revocation_during_connect_requires_browser_auth(services):
    client = Client(connect_error=SessionRevokedError(None))
    service = services(Factory(client))
    with pytest.raises(UserSessionUnavailableError):
        service.import_phones(['+79990000001'])
    assert client.requests == []
    assert service.snapshot()['state'] == 'NOT_AUTHORIZED'


def test_browser_authorization_respects_server_floodwait_without_replay(services, monkeypatch):
    from telegram_phone_number_checker import user_session
    clock = [100.0]
    monkeypatch.setattr(user_session, 'monotonic', lambda: clock[0], raising=False)
    class FloodClient(Client):
        async def send_code_request(self, phone):
            raise FloodWaitError(None, capture=73)
    client, later = FloodClient(authorized=False), Client(authorized=False)
    factory = Factory(client, later)
    service = services(factory)
    service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'ERROR')
    assert not service.start_authorization('+79991112233')
    assert factory.created == 1
    clock[0] += 72
    assert not service.start_authorization('+79991112233')
    clock[0] += 1
    assert service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'CODE_SENT')
    assert factory.created == 2


def test_authorization_snapshot_requires_live_connection_after_verification(services):
    class DisconnectingClient(Client):
        async def is_user_authorized(self):
            self.connected = False
            return True
    service = services(Factory(DisconnectingClient()))
    service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] != 'REQUESTING_CODE')
    assert service.snapshot()['state'] != 'AUTHORIZED'


def test_lost_connection_during_authorization_check_does_not_dispatch(services):
    class DisconnectingClient(Client):
        async def is_user_authorized(self):
            self.connected = False
            return True
    client = DisconnectingClient()
    service = services(Factory(client))
    with pytest.raises(RequestNotDispatchedError):
        service.resolve_phone('+79990000001')
    assert client.requests == []


def test_authorization_failure_disposal_is_serialized_with_validation(services):
    class SlowDisconnectClient(Client):
        def __init__(self):
            super().__init__(authorized=False, request_error=ValueError('private error'))
            self.disconnect_started = Event()
            self.release_disconnect = Event()
        async def disconnect(self):
            self.disconnect_started.set()
            while not self.release_disconnect.is_set():
                await asyncio.sleep(.005)
            await super().disconnect()
    broken = SlowDisconnectClient()
    factory = Factory(broken, Client())
    service = services(factory)
    service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'CODE_SENT')
    service.submit_code('12345')
    assert broken.disconnect_started.wait(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        later = executor.submit(service.resolve_phone, '+79990000001')
        try:
            time.sleep(.03)
            assert not later.done()
            assert factory.created == 1
        finally:
            broken.release_disconnect.set()
        assert 'blocked phone lookup' in later.result(timeout=2)['error']


@pytest.mark.parametrize('phase', ['connect', 'authorization_check'])
def test_pre_dispatch_floodwait_keeps_original_server_error(services, phase):
    flood = FloodWaitError(None, capture=137)
    class FloodClient(Client):
        async def is_user_authorized(self):
            if phase == 'authorization_check':
                raise flood
            return await super().is_user_authorized()
    client = FloodClient(connect_error=flood if phase == 'connect' else None)
    service = services(Factory(client))
    with pytest.raises(FloodWaitError) as caught:
        service.import_phones(['+79990000001'])
    assert caught.value is flood
    assert caught.value.seconds == 137
    assert client.requests == []


@pytest.mark.parametrize('phase', ['connect', 'authorization_check'])
def test_browser_pre_dispatch_floodwait_shows_server_duration(services, phase):
    flood = FloodWaitError(None, capture=137)
    class FloodClient(Client):
        async def is_user_authorized(self):
            if phase == 'authorization_check':
                raise flood
            return await super().is_user_authorized()
    client = FloodClient(connect_error=flood if phase == 'connect' else None)
    service = services(Factory(client))
    assert service.start_authorization('+79991112233')
    assert wait_until(lambda: service.snapshot()['state'] == 'ERROR')
    assert '137 сек.' in service.snapshot()['message']
    assert not service.start_authorization('+79991112233')


@pytest.mark.parametrize(('client_shields_disconnect', 'release_after'), [(False, .05), (True, .05), (True, 2.1)])
def test_close_during_error_disposal_waits_until_client_disconnects(services, client_shields_disconnect, release_after):
    class DelayedDisconnectClient(Client):
        def __init__(self):
            super().__init__(request_error=ConnectionError('private transport failure'))
            self.disconnect_started = Event()
            self.release_disconnect = Event()
            self.disconnect_completed = Event()
        async def disconnect(self):
            self.disconnect_calls += 1
            self.disconnect_started.set()
            async def finish():
                while not self.release_disconnect.is_set():
                    await asyncio.sleep(.005)
                self.connected = False
                self.disconnect_completed.set()
            if client_shields_disconnect:
                await asyncio.shield(finish())
            else:
                await finish()
    client = DelayedDisconnectClient()
    service = services(Factory(client))
    with ThreadPoolExecutor(max_workers=2) as executor:
        request = executor.submit(service.import_phones, ['+79990000001'])
        assert client.disconnect_started.wait(2)
        closing = executor.submit(service.close, timeout=.01)
        try:
            time.sleep(release_after)
            assert not closing.done(), 'close returned before disconnect completed'
            assert client.is_connected()
        finally:
            client.release_disconnect.set()
        closing.result(timeout=2)
        with pytest.raises(RequestOutcomeUnknownError):
            request.result(timeout=2)
    assert client.disconnect_completed.is_set()
    assert not client.is_connected()
    assert client.disconnect_calls == 1
    assert not service._thread.is_alive()


def test_paused_legacy_job_lifecycle_migrates_authorizes_and_resumes_without_new_client(tmp_path):
    """Catches auto-resume, new-client handoff, and overwriting review/manual evidence."""
    from telegram_phone_number_checker.durable_web import DurableValidatorApplication

    phones = ['+79990000001', '+79990000002', '+79990000003']
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps({'jobs': {'legacy': {
        'raw': '\n'.join(phones),
        'results_by_phone': {phones[0]: {'retry': True}, phones[1]: {'error': 'No response'}},
    }}}))
    source_bytes = source.read_bytes()
    client = Client(authorized=False)
    factory = Factory(client)
    service = UserSessionService(client_factory=factory)
    app = DurableValidatorApplication(tmp_path / 'validator.db', source, gateway=service, authorization=service)
    clock = [time.time()]
    app.store.now = lambda: clock[0]
    try:
        assert app.snapshot('legacy')['state'] == 'STAGED'
        assert not app.start('legacy')
        # Model persisted upgraded rows: a manually reviewed direct privacy row
        # and a previous dispatched request whose outcome is quarantined.
        with app.store._connect() as connection:
            connection.execute("UPDATE jobs SET state='PAUSED' WHERE id='legacy'")
            connection.execute("UPDATE job_items SET method='DIRECT_LOOKUP' WHERE job_id='legacy' AND ordinal=1")
            connection.execute("UPDATE job_items SET status='UNKNOWN_OUTCOME_REVIEW', api_status='UNKNOWN_OUTCOME' WHERE job_id='legacy' AND ordinal=2")
        manual_id = app.store.rows('legacy')[1]['_item_id']
        assert app.confirm_manual_chat('legacy', manual_id)
        before = app.store.rows('legacy')
        assert app.store.requeue_legacy_direct_rows('legacy') == 1
        assert app.snapshot('legacy')['state'] == 'PAUSED'
        # A queued task discovering revoked/absent credentials must wait durably.
        clock[0] += 61
        assert app.store.start('legacy')
        assert wait_until(lambda: app.snapshot('legacy')['state'] == 'WAITING_AUTHORIZATION')
        assert client.requests == []
        assert app.begin_authorization('+79991112233')
        assert wait_until(lambda: service.snapshot()['state'] == 'CODE_SENT')
        assert app.submit_authorization_code('12345')
        assert wait_until(lambda: service.snapshot()['state'] == 'AUTHORIZED')
        assert app.snapshot('legacy')['state'] == 'WAITING_AUTHORIZATION'
        assert client.requests == []
        assert app.start('legacy')
        assert wait_until(lambda: app.store.rows('legacy')[0]['status'] == 'UNRESOLVABLE_OR_PRIVACY')
        after = app.store.rows('legacy')
        assert after[1:] == before[1:]
        assert after[0]['method'] == 'IMPORT'
        assert [attempt['source'] for attempt in app.store.item_attempts('legacy', after[0]['_item_id'])] == [
            'JSON_MIGRATION', 'LEGACY_DIRECT_MIGRATION', 'TELEGRAM',
        ]
        assert source.read_bytes() == source_bytes
        assert factory.created == 1
        assert len(client.requests) == 1
        assert isinstance(client.requests[0], ImportContactsRequest)
        assert len(client.requests[0].contacts) == 1
        assert client.requests[0].contacts[0].phone == phones[0]
        assert 'Неизвестный результат — требуется решение оператора: 1' in app.render_job_page('legacy')
    finally:
        app.close()
    assert client.disconnect_calls == 1
    assert not app._worker_thread.is_alive()
