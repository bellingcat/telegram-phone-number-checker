import asyncio
import json
import os
import re
import time
from getpass import getpass

import click
from dotenv import load_dotenv
from telethon.sync import TelegramClient, errors, functions
from telethon.tl import types

load_dotenv()

BATCH_SIZE = 50  # Number of phone numbers processed each time
DELAY_SECONDS = 10  # Delay time after each batch is processed

def get_human_readable_user_status(status: types.TypeUserStatus):
    match status:
        case types.UserStatusOnline():
            return "Currently online"
        case types.UserStatusOffline():
            return status.was_online.strftime("%Y-%m-%d %H:%M:%S %Z")
        case types.UserStatusRecently():
            return "Last seen recently"
        case types.UserStatusLastWeek():
            return "Last seen last week"
        case types.UserStatusLastMonth():
            return "Last seen last month"
        case _:
            return "Unknown"

async def get_names(client: TelegramClient, phone_number: str) -> dict:
    """Take in a phone number and returns the associated user information if the user exists.

    It does so by first adding the user's phones to the contact list, retrieving the
    information, and then deleting the user from the contact list.
    """
    result = {}
    print(f"Checking: {phone_number=} ...", end="", flush=True)
    try:
        # Create a contact
        contact = types.InputPhoneContact(
            client_id=0, phone=phone_number, first_name="", last_name=""
        )
        # Attempt to add the contact from the address book
        contacts = await client(functions.contacts.ImportContactsRequest([contact]))

        users = contacts.to_dict().get("users", [])
        number_of_matches = len(users)

        if number_of_matches == 0:
            result.update(
                {
                    "error": "No response, the phone number is not on Telegram or has blocked contact adding."
                }
            )
        elif number_of_matches == 1:
            # Attempt to remove the contact from the address book.
            # The response from DeleteContactsRequest contains more information than from ImportContactsRequest
            updates_response: types.Updates = await client(
                functions.contacts.DeleteContactsRequest(id=[users[0].get("id")])
            )
            user = updates_response.users[0]
            # getting more information about the user
            result.update(
                {
                    "id": user.id,
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
                    "user_was_online": get_human_readable_user_status(user.status),
                    "phone": user.phone,
                }
            )
        else:
            result.update(
                {
                    "error": """This phone number matched multiple Telegram accounts, 
            which is unexpected. Please contact the developer: contact-tech@bellingcat.com"""
                }
            )

    except TypeError as e:
        result.update(
            {
                "error": f"TypeError: {e}. --> The error might have occurred due to the inability to delete the {phone_number=} from the contact list."
            }
        )
    except Exception as e:
        result.update({"error": f"Unexpected error: {e}."})
        raise
    print("Done.")
    return result

async def validate_users(client: TelegramClient, phone_numbers: list) -> dict:
    """
    Take in a list of phone numbers and try to get the user information associated with each phone number.
    """
    result = {}
    phones = [re.sub(r"\s+", "", p, flags=re.UNICODE) for p in phone_numbers]
    try:
        for phone in phones:
            if phone not in result:
                result[phone] = await get_names(client, phone)
    except Exception as e:
        print(e)
        raise
    return result

async def login(
    api_id: str | None, api_hash: str | None, phone_number: str | None
) -> TelegramClient:
    """Create a telethon session or reuse existing one"""
    print("Logging in...", end="", flush=True)
    API_ID = api_id or os.getenv("API_ID") or input("Enter your API ID: ")
    API_HASH = api_hash or os.getenv("API_HASH") or input("Enter your API HASH: ")
    PHONE_NUMBER = (
        phone_number or os.getenv("PHONE_NUMBER") or input("Enter your phone number: ")
    )
    client = TelegramClient(PHONE_NUMBER, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(PHONE_NUMBER)
        try:
            await client.sign_in(
                PHONE_NUMBER, input("Enter the code (sent on telegram): ")
            )
        except errors.SessionPasswordNeededError:
            pw = getpass(
                "Two-Step Verification enabled. Please enter your account password: "
            )
            await client.sign_in(password=pw)
    print("Done.")
    return client

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
    type=str,
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
    "--output",
    help="Filename to store results",
    default="results.json",
    show_default=True,
    type=str,
)
@click.option(
    "--input-file",
    help="Input file containing phone numbers",
    default="phone-numbers.txt",
    show_default=True,
    type=str,
)
def main_entrypoint(
    phone_numbers: str, api_id: str, api_hash: str, api_phone_number: str, output: str, input_file: str
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
    If you do not want to enter the API ID, API hash, or phone number associated with
    your Telegram account on the command line, you can store these values in a `.env`
    file located within the same directory you run this command from.

    \b
    // .env file example:
    API_ID=12345678
    API_HASH=1234abcd5678efgh1234abcd567
    PHONE_NUMBER=+15555555555

    See the official Telegram docs at https://core.telegram.org/api/obtaining_api_id
    for more information on obtaining an API ID.

    \b
    Recommendations:
    Telegram recommends entering phone numbers in international format
    +(country code)(city or carrier code)(your number)
    i.e. +491234567891

    """
    asyncio.run(
        run_program(
            phone_numbers,
            api_id,
            api_hash,
            api_phone_number,
            output,
            input_file
        )
    )

async def run_program(
    phone_numbers: str, api_id: str, api_hash: str, api_phone_number: str, output: str, input_file: str
):
    # Read phone numbers from file if phone_numbers is not provided
    if not phone_numbers:
        with open(input_file, 'r') as file:
            phone_numbers = file.read().strip().split('\n')
    else:
        phone_numbers = phone_numbers.split(',')

    client = await login(api_id, api_hash, api_phone_number)
    result = {}

    for i in range(0, len(phone_numbers), BATCH_SIZE):
        batch = phone_numbers[i:i + BATCH_SIZE]
        batch_result = await validate_users(client, batch)
        result.update(batch_result)
        time.sleep(DELAY_SECONDS)  # Delay to avoid triggering rate limits

    show_results(output, result)
    await client.disconnect()

if __name__ == "__main__":
    main_entrypoint()
