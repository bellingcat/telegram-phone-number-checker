import asyncio
from telethon.tl import types

from telegram_phone_number_checker import main


def test_uses_persisted_session_path_from_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", "/data/telegram-session")

    assert main.get_session_name("+79991234567") == "/data/telegram-session"


class ImportedContact:
    def __init__(self, client_id, user_id):
        self.client_id = client_id
        self.user_id = user_id


class ImportedContactsResponse:
    def __init__(self):
        self.imported = [ImportedContact(1, 101)]
        self.retry_contacts = []
        self.users = [
            types.User(
                id=101,
                access_hash=1,
                first_name="Тест",
                last_name="Пользователь",
                username="test_user",
                phone="79991234567",
                status=types.UserStatusRecently(),
            )
        ]


class ResolvedPeerResponse:
    def __init__(self):
        self.users = [
            types.User(
                id=202,
                access_hash=2,
                first_name="Проверенный",
                last_name="Контакт",
                username=None,
                phone="79991234567",
                status=types.UserStatusRecently(),
            )
        ]


class FakeClient:
    def __init__(self):
        self.imported_contacts = None
        self.deleted_contact_ids = None

    async def __call__(self, request):
        if isinstance(request, main.functions.contacts.ImportContactsRequest):
            self.imported_contacts = request.contacts
            return ImportedContactsResponse()
        if isinstance(request, main.functions.contacts.DeleteContactsRequest):
            self.deleted_contact_ids = request.id
            return None
        raise AssertionError(f"Unexpected request: {request}")


class ResolvePhoneClient:
    async def __call__(self, request):
        if isinstance(request, main.functions.contacts.ResolvePhoneRequest):
            assert request.phone == "+79991234567"
            return ResolvedPeerResponse()
        raise AssertionError(f"Unexpected request: {request}")


def test_batch_lookup_imports_many_phones_once_and_cleans_found_contacts():
    client = FakeClient()

    results = asyncio.run(
        main.get_names_batch(client, ["+79991234567", "+79990000000"])
    )

    assert [contact.phone for contact in client.imported_contacts] == [
        "+79991234567",
        "+79990000000",
    ]
    assert results["+79991234567"]["username"] == "test_user"
    assert results["+79990000000"]["error"].startswith("No response")
    assert client.deleted_contact_ids == [101]


def test_resolve_phone_returns_found_account_without_importing_contact():
    result = asyncio.run(main.resolve_phone(ResolvePhoneClient(), "+79991234567"))

    assert result == {
        "id": 202,
        "username": None,
        "usernames": None,
        "first_name": "Проверенный",
        "last_name": "Контакт",
        "fake": None,
        "verified": None,
        "premium": None,
        "mutual_contact": None,
        "bot": None,
        "bot_chat_history": None,
    }
