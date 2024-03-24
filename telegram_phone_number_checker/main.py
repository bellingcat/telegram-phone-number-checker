import asyncio
import os
import json
import re
from telethon.sync import TelegramClient, errors, functions
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPhoneContact, TypeUserStatus, UserStatusLastWeek, UserStatusEmpty,UserStatusRecently, UserStatusOnline, UserStatusLastMonth, UserStatusOffline
from dotenv import load_dotenv
from getpass import getpass
import click


load_dotenv()

def get_human_readable_status(status: TypeUserStatus):
    match status:
        case UserStatusEmpty():
            return "Unknown"
        case UserStatusOnline():
            return "Currently online"
        case UserStatusOffline():
            return status.was_online.strftime("%Y-%m-%d %H:%M:%S %Z")
        case UserStatusRecently():
            return "Last seen recently"
        case UserStatusLastWeek():
            return "Last seen last week"
        case UserStatusLastMonth():
            return "Last seen last month"
        case _:
            return "Unknown status returned"


async def get_user_info(client: TelegramClient, phone_number: str) -> dict:
    """Take in a phone number and returns the associated user information if the user exists."""
    result = {}
    print(f"Checking: {phone_number=} ...", end="", flush=True)
    peer_id = await client.get_peer_id(phone_number)
    user = (await client(GetFullUserRequest(peer_id))).users[0]
    return {
        "id": peer_id,
        "username": user.username,
        "usernames": user.usernames,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "fake": user.fake,
        "verified": user.verified,
        "premium": user.premium,
        "mutual_contact": user.mutual_contact,
        "bot": user.bot,
        "bot_chat_history": user.bot_chat_history,
        "restricted": user.restricted,
        "restriction_reason": user.restriction_reason,
        "user_was_online": get_human_readable_status(user.status),
        "deleted": user.deleted,
    }


async def validate_users(client: TelegramClient, phone_numbers: str) -> dict:
    """
    Take in a string of comma separated phone numbers and try to get the user information associated with each phone number.
    """
    if not phone_numbers or not len(phone_numbers):
        phone_numbers = input("Enter the phone numbers to check, separated by commas: ")
    phones = {re.sub(r"\s+", "", p, flags=re.UNICODE) for p in phone_numbers.split(",")}
    return {phone: await get_user_info(client, phone) for phone in phones}


def show_results(output: str, res: dict) -> None:
    print(json.dumps(res, indent=4))
    with open(output, "w") as f:
        json.dump(res, f, indent=4)
        print(f"Results saved to {output}")


@click.command(
    epilog="Check out the docs at github.com/bellingcat/telegram-phone-number-checker for more information."
)
@click.option(
    "--phone-numbers",
    "-p",
    help="List of phone numbers to check, separated by commas",
    type=str,
)
@click.option(
    "--api-id",
    help="Your Telegram app api_id",
    type=int,
    prompt="Enter your Telegram App app_id",
    envvar="API_ID",
    show_envvar=True,
)
@click.option(
    "--api-hash",
    help="Your Telegram app api_hash",
    type=str,
    prompt="Enter your Telegram App api_hash",
    hide_input=True,
    envvar="API_HASH",
    show_envvar=True,
)
@click.option(
    "--api-phone-number",
    help="Your phone number",
    type=str,
    prompt="Enter the number associated with your Telegram account",
    envvar="PHONE_NUMBER",
    show_envvar=True,
)
@click.option(
    "--api-phone-password",
    help="The password for your Telegram account",
    type=str,
    prompt="Enter the password associated with your Telegram account",
    hide_input=True,
    envvar="PASSWORD",
    show_envvar=True,
)
@click.option(
    "--output",
    help="Filename to store results",
    default="results.json",
    show_default=True,
    type=str,
)
def main_entrypoint(
    phone_numbers: str, api_id: int, api_hash: str, api_phone_number: str, api_phone_password: str, output: str
) -> None:
    """
    Check to see if one or more phone numbers belong to a valid Telegram account.

    \b
    Prerequisites:
    1. A Telegram account with an active phone number
    2. A Telegram App api_id and App api_hash, which you can get by creating
       a Telegram App @ https://my.telegram.org/apps

    \b
    Note:
    If you do not want to enter the API ID, API hash, phone number, or password
    associated with your Telegram account on the command line, you can store these
    values in a `.env` file located within the same directory you run this command from.

    \b
    // .env file example:
    API_ID=12345678
    API_HASH=1234abcd5678efgh1234abcd567
    PHONE_NUMBER=+15555555555
    PASSWORD=mmyy_+ppaasssswwoorrdd$

    See the official Telegram docs at https://core.telegram.org/api/obtaining_api_id
    for more information on obtaining an API ID.

    \b
    Recommendations:
    Telegram recommends entering phone numbers in international format
    +(country code)(city or carrier code)(your number)
    i.e. +491234567891

    """
    asyncio.run(run_program(api_phone_number, api_phone_password, api_id, api_hash, phone_numbers, output))


async def run_program(api_phone_number, api_phone_password, api_id, api_hash, phone_numbers, output):
    async with TelegramClient(api_phone_number, api_id, api_hash) as client:
        await client.start(phone=api_phone_number, password=api_phone_password)
        res = await validate_users(client, phone_numbers)
        show_results(output, res)


if __name__ == "__main__":
    main_entrypoint()
