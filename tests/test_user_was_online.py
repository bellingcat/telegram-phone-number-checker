from datetime import datetime, timezone

import pytest
from telethon.tl import types

from telegram_phone_number_checker import main


@pytest.mark.parametrize(
    "user_status, readable_string",
    [
        (types.UserStatusEmpty(), "Unknown"),
        (types.UserStatusOnline(expires=None), "Currently online"),
        (
            types.UserStatusOffline(
                was_online=datetime(2024, 4, 6, 12, 30, 1, tzinfo=timezone.utc)
            ),
            "2024-04-06 12:30:01 UTC",
        ),
        (types.UserStatusRecently(), "Last seen recently"),
        (types.UserStatusLastWeek(), "Last seen last week"),
        (types.UserStatusLastMonth(), "Last seen last month"),
    ],
)
def test_should_return_correct_status_string(user_status, readable_string):
    assert main.get_human_readable_user_status(user_status) == readable_string
