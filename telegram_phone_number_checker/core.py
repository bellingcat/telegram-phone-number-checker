import logging
from getpass import getpass

from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions

from telegram_phone_number_checker.output_file import show_results
from telegram_phone_number_checker.validation import validate_usernames, validate_users

load_dotenv()

logger = logging.getLogger(__name__)

async def login(
    api_id: str | None, api_hash: str | None, phone_number: str | None
) -> TelegramClient:
    logging.info("Logging in...")
    assert api_id is not None
    assert api_hash is not None
    assert phone_number is not None
    client = TelegramClient(phone_number, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(phone_number)
        try:
            await client.sign_in(phone_number, input("Enter the code (sent on telegram): "))
        except errors.SessionPasswordNeededError:
            pw = getpass("Two-Step Verification enabled. Please enter your account password: ")
            await client.sign_in(password=pw)
    logging.info("Done.")
    return client

async def run_program(
    phone_numbers: str,
    usernames: str,
    api_id: str,
    api_hash: str,
    api_phone_number: str,
    output: str,
    download_profile_photos: bool = False,
):
    client = await login(api_id, api_hash, api_phone_number)
    results = {}

    if phone_numbers:
        phone_results = await validate_users(
            client, phone_numbers, download_profile_photos
        )
        results.update(phone_results)

    if usernames:
        username_results = await validate_usernames(
            client, usernames, download_profile_photos
        )
        results.update(username_results)

    if not phone_numbers and not usernames:
        choice = input("Search by (p)hone numbers or (u)sernames? [p/u]: ").lower()
        if choice == "u":
            usernames = input("Enter the usernames to check, separated by commas: ")
            username_results = await validate_usernames(
                client, usernames, download_profile_photos
            )
            results.update(username_results)
        else:
            phone_numbers = input("Enter the phone numbers to check, separated by commas: ")
            phone_results = await validate_users(
                client, phone_numbers, download_profile_photos
            )
            results.update(phone_results)

    show_results(output, results)
    await client.disconnect()
