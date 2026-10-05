"""One connected browser-authorized Telethon user session and event loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError
import os
from threading import Event, Lock, Thread
from time import monotonic

from telethon.errors import FloodWaitError, RPCError
from telegram_phone_number_checker import main as upstream
from telegram_phone_number_checker.web_service import is_valid_phone, normalize_phone_line


class UserSessionUnavailableError(Exception):
    """Browser authorization is required, or the application is shutting down."""


class RequestNotDispatchedError(Exception):
    """The transport failed before the operation reached Telegram."""


class RequestOutcomeUnknownError(Exception):
    """The dispatched operation may have reached Telegram; never replay here."""


class UserSessionService:
    def __init__(self, client_factory=None):
        self._client_factory = client_factory or upstream.TelegramClient
        self._client = None
        self._disposal_task = None
        self._state_lock = Lock()
        self._submission_lock = Lock()
        self._close_lock = Lock()
        self._closing = Event()
        self._ready = Event()
        self._state = 'NOT_AUTHORIZED'
        self._message = 'Подключите Telegram, чтобы получить код авторизации.'
        self._phone = ''
        self._authorization_retry_at = 0.0
        self._thread = Thread(target=self._run_loop, name='telegram-user-session', daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._operation_lock = asyncio.Lock()
        self._tasks = set()
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def snapshot(self) -> dict[str, str]:
        with self._state_lock:
            return {'state': self._state, 'message': self._message}

    def _set_state(self, state, message):
        with self._state_lock:
            self._state, self._message = state, message

    def _submit(self, coroutine):
        with self._submission_lock:
            if self._closing.is_set():
                coroutine.close()
                raise UserSessionUnavailableError('Telegram session is closed.')
            return asyncio.run_coroutine_threadsafe(self._run_owned(coroutine), self._loop)

    async def _run_owned(self, coroutine):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            return await coroutine
        finally:
            self._tasks.discard(task)

    def start_authorization(self, phone: str) -> bool:
        phone = normalize_phone_line(phone)
        with self._state_lock:
            if self._closing.is_set() or monotonic() < self._authorization_retry_at or self._state in {
                'REQUESTING_CODE', 'CODE_SENT', 'VERIFYING_CODE',
                'PASSWORD_REQUIRED', 'VERIFYING_PASSWORD', 'AUTHORIZED',
            }:
                return False
            if not is_valid_phone(phone):
                self._state = 'ERROR'
                self._message = 'Введите номер Telegram в международном формате, например +79991234567.'
                return False
            self._phone = phone
            self._state = 'REQUESTING_CODE'
            self._message = 'Запрашиваем код у Telegram…'
        return self._submit_authorization('begin', phone)

    def submit_code(self, code: str) -> bool:
        code = ''.join(character for character in code if character.isdigit())
        return self._accept_secret('CODE_SENT', 'VERIFYING_CODE', 'Проверяем код Telegram…', 'code', code)

    def submit_password(self, password: str) -> bool:
        return self._accept_secret('PASSWORD_REQUIRED', 'VERIFYING_PASSWORD', 'Проверяем пароль 2FA…', 'password', password)

    def _accept_secret(self, expected, state, message, action, secret):
        with self._state_lock:
            if not secret or self._closing.is_set() or self._state != expected:
                return False
            self._state, self._message = state, message
        return self._submit_authorization(action, secret)

    def _submit_authorization(self, action, value):
        try:
            self._submit(self._authorize(action, value))
        except UserSessionUnavailableError:
            return False
        return True

    async def _authorize(self, action, value):
        async with self._operation_lock:
            await self._authorize_locked(action, value)

    async def _authorize_locked(self, action, value):
        try:
            if self._closing.is_set():
                return
            client = await self._connect()
            if action == 'begin':
                authorized = await client.is_user_authorized()
                if not client.is_connected():
                    raise RequestNotDispatchedError()
                if authorized:
                    self._set_state('AUTHORIZED', 'Telegram подключён.')
                else:
                    await client.send_code_request(value)
                    self._set_state('CODE_SENT', 'Введите код из Telegram.')
                return
            try:
                if action == 'code':
                    await client.sign_in(self._phone, value)
                else:
                    await client.sign_in(password=value)
            except upstream.errors.SessionPasswordNeededError:
                self._set_state('PASSWORD_REQUIRED', 'Введите пароль двухэтапной аутентификации.')
                return
            authorized = await client.is_user_authorized()
            if not authorized or not client.is_connected():
                raise UserSessionUnavailableError()
            self._set_state('AUTHORIZED', 'Telegram подключён.')
        except asyncio.CancelledError:
            return
        except FloodWaitError as error:
            await self._dispose_client()
            with self._state_lock:
                self._authorization_retry_at = monotonic() + error.seconds
                self._state = 'ERROR'
                self._message = f'Telegram ограничил авторизацию. Повторите подключение через {error.seconds} сек.'
        except Exception:
            # Neither submitted secrets nor Telegram error text leave this loop.
            await self._dispose_client()
            self._set_state('ERROR', 'Не удалось авторизовать Telegram. Повторите подключение в браузере.')

    async def _connect(self):
        if self._disposal_task is not None:
            await self._dispose_client()
        if self._client is not None and self._client.is_connected():
            return self._client
        try:
            if self._client is not None:
                await self._dispose_client()
            api_id, api_hash = os.getenv('API_ID'), os.getenv('API_HASH')
            if not api_id or not api_hash:
                raise UserSessionUnavailableError('Configure Telegram API credentials.')
            proxy = os.getenv('PROXY')
            self._client = self._client_factory(
                os.getenv('TELEGRAM_SESSION_PATH', '/data/telegram-session'), api_id, api_hash,
                proxy=upstream.parse_proxy(proxy) if proxy else None,
                flood_sleep_threshold=0,
                # The durable worker owns retries. Telethon must surface loss
                # after dispatch instead of reconnecting and replaying an RPC.
                auto_reconnect=False,
                request_retries=0,
                connection_retries=0,
                raise_last_call_error=True,
            )
            await self._client.connect()
            if not self._client.is_connected():
                raise ConnectionError()
            return self._client
        except FloodWaitError:
            await self._dispose_client()
            raise
        except UserSessionUnavailableError:
            raise
        except Exception as error:
            await self._dispose_client()
            if self._is_revoked(error):
                self._set_state('NOT_AUTHORIZED', 'Требуется авторизация Telegram в браузере.')
                raise UserSessionUnavailableError('Browser Telegram authorization is required.') from None
            self._set_state('NOT_AUTHORIZED', 'Подключение Telegram прервано; ожидается повторная попытка.')
            raise RequestNotDispatchedError('Telegram request was not dispatched.') from None

    @staticmethod
    def _is_revoked(error):
        return isinstance(error, RPCError) and error.code == 401

    async def _request(self, operation):
        dispatched = False
        try:
            async with self._operation_lock:
                if self._closing.is_set():
                    raise UserSessionUnavailableError('Telegram session is closed.')
                client = await self._connect()
                try:
                    authorized = await client.is_user_authorized()
                except FloodWaitError:
                    raise
                except Exception as error:
                    if self._is_revoked(error):
                        authorized = False
                    else:
                        await self._dispose_client()
                        self._set_state('NOT_AUTHORIZED', 'Подключение Telegram прервано; ожидается повторная попытка.')
                        raise RequestNotDispatchedError('Telegram request was not dispatched.') from None
                if not client.is_connected():
                    await self._dispose_client()
                    self._set_state('NOT_AUTHORIZED', 'Подключение Telegram прервано; ожидается повторная попытка.')
                    raise RequestNotDispatchedError('Telegram request was not dispatched.')
                if not authorized:
                    self._set_state('NOT_AUTHORIZED', 'Требуется авторизация Telegram в браузере.')
                    raise UserSessionUnavailableError('Browser Telegram authorization is required.')
                self._set_state('AUTHORIZED', 'Telegram подключён.')
                dispatched = True
                try:
                    return await operation(client)
                except FloodWaitError:
                    raise  # The durable worker owns server-directed waiting.
                except Exception as error:
                    await self._dispose_client()
                    self._set_state('NOT_AUTHORIZED', 'Требуется проверить подключение Telegram в браузере.')
                    if self._is_revoked(error):
                        raise UserSessionUnavailableError('Browser Telegram authorization is required.') from None
                    raise RequestOutcomeUnknownError('Telegram request outcome is unknown.') from None
        except asyncio.CancelledError:
            exception = RequestOutcomeUnknownError if dispatched else RequestNotDispatchedError
            raise exception('Telegram request interrupted during shutdown.') from None

    def _call(self, operation):
        try:
            return self._submit(self._request(operation)).result()
        except CancelledError:
            raise RequestNotDispatchedError('Telegram request interrupted during shutdown.') from None

    def import_phones(self, phones: list[str]) -> dict[str, dict]:
        return self._call(lambda client: upstream.get_names_batch(client, phones, cleanup_contacts=False))

    def resolve_phone(self, phone: str) -> dict:
        return self._call(lambda client: upstream.resolve_phone(client, phone))

    def cleanup_contacts(self, user_ids: list[int]) -> None:
        self._call(lambda client: upstream.cleanup_imported_contacts(client, user_ids))

    async def _dispose_client(self):
        if self._disposal_task is None:
            if self._client is None:
                return
            self._disposal_task = asyncio.create_task(self._disconnect_owned_client(self._client))
        disposal = self._disposal_task
        try:
            # Request cancellation must not interrupt transport cleanup. A later
            # shutdown awaits this same task, rather than disconnecting twice.
            await asyncio.shield(disposal)
        finally:
            if disposal.done() and self._disposal_task is disposal:
                self._disposal_task = None

    async def _disconnect_owned_client(self, client):
        try:
            await client.disconnect()
        except Exception:
            pass
        finally:
            if self._client is client:
                self._client = None

    async def _shutdown(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._dispose_client()
        self._set_state('NOT_AUTHORIZED', 'Telegram отключён.')

    def close(self, timeout: float = 5.0) -> None:
        """Allow active work its grace period, then finish cleanup before stopping."""
        with self._close_lock:
            if self._closing.is_set():
                return
            with self._submission_lock:
                self._closing.set()
            async def drain():
                # Already running work may finish; new submissions are rejected.
                tasks = list(self._tasks)
                if tasks:
                    await asyncio.wait(tasks, timeout=max(0, timeout))
                await self._shutdown()
            shutdown = asyncio.run_coroutine_threadsafe(drain(), self._loop)
            try:
                # The timeout bounds active work, not disconnect. Stopping the
                # loop before cleanup completes would strand the owned session.
                shutdown.result()
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()
