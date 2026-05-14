import re

from telethon import TelegramClient

from telegram_phone_number_checker import search


async def validate_users(
    client: TelegramClient, phone_numbers: str, download_profile_photos: bool
) -> dict:
    if not phone_numbers or not len(phone_numbers):
        phone_numbers = input("Enter the phone numbers to check, separated by commas: ")
    result = {}
    phones = [re.sub(r"\s+", "", p, flags=re.UNICODE) for p in phone_numbers.split(",")]
    for phone in phones:
        if phone not in result:
            result[phone] = await search.get_names(client, phone, download_profile_photos)
    return result


async def validate_usernames(
    client: TelegramClient, usernames: str, download_profile_photos: bool
) -> dict:
    if not usernames or not len(usernames):
        usernames = input("Enter the usernames to check, separated by commas: ")
    result = {}
    username_list = [re.sub(r"\s+", "", u, flags=re.UNICODE) for u in usernames.split(",")]
    for username in username_list:
        if username not in result:
            result[username] = await search.get_user_by_username(client, username, download_profile_photos)
    return result
