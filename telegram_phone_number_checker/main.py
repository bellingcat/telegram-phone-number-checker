import asyncio
import json
import os
from pathlib import Path
import re
from getpass import getpass
import logging
import csv

import click
from dotenv import load_dotenv
from telethon.sync import TelegramClient, errors, functions
from telethon.tl import types
from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
load_dotenv()

def load_usernames_from_csv(filepath: str, column: str | None = None) -> str:
    """Load usernames from a CSV file. 
    
    If column is specified, reads from that column name.
    Otherwise uses the first column.
    """
    usernames = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV file appears to be empty.")
        
        col = column or reader.fieldnames[0]
        if col not in reader.fieldnames:
            raise ValueError(f"Column '{col}' not found in CSV. Available: {list(reader.fieldnames)}")
        
        seen = set()
        for row in reader:
            value = row[col].strip()
            if value and value not in seen:
                seen.add(value)
                usernames.append(value)
    
    if not usernames:
        raise ValueError("No usernames found in the CSV file.")
    
    logging.info(f"Loaded {len(usernames)} username(s) from '{filepath}' (column: '{col}')")
    return ",".join(usernames)

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


async def get_names(
    client: TelegramClient, phone_number: str, download_profile_photos: bool = False
) -> dict:
    """Take in a phone number and returns the associated user information if the user exists.

    It does so by first adding the user's phones to the contact list, retrieving the
    information, and then deleting the user from the contact list.
    ---
    client, TelegramClient : Telegram client used to generate API call(s)
    phone_number, str : Phone number associated with a given Telegram account (including country code, example format '+11232223333')
    download_profile_photos, bool : Flag for whether to download a profile's associated account photo; defaults to False.
    """
    result = {}
    logging.info(f"Checking: {phone_number=} ...")
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
                    "usernames": [u.username for u in (user.usernames or [])] if user.usernames else None,
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
            if download_profile_photos is True:
                try:
                    photo_output_path = Path("{}_{}_photo.jpeg".format(user.id, phone_number))
                    logging.info(
                        "Attempting to download profile photo for %s (%s)",
                        str(user.id),
                        str(phone_number),
                    )
                    photo = await client.download_profile_photo(
                        user, file=photo_output_path, download_big=True
                    )
                    if photo is not None:
                        logging.info("Downloaded photo at '%s'", photo)
                    else:
                        logging.info(
                            "No photo found for %s (%s)", str(user.id), str(phone_number)
                        )
                # We don't want the script to fail if download I/O fails locally, file format error, etc.
                # TODO : Add handling for ind. exceptions
                except Exception as e:
                    logging.exception(
                        "---\nUnable to download profile photo for %s. Exception provided below.\n---\n%s\n---\n",
                        str(phone_number),
                        str(e),
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
    logging.info("Done.")
    return result


async def get_user_by_username(
    client: TelegramClient, username: str, download_profile_photos: bool = False
) -> dict:
    """Take in a username and returns the associated user information if the user exists.

    Uses Telegram's get_entity to look up users by their username.
    ---
    client, TelegramClient : Telegram client used to generate API call(s)
    username, str : Username to search (with or without @ symbol, e.g. 'username' or '@username')
    download_profile_photos, bool : Flag for whether to download a profile's associated account photo; defaults to False.
    """
    result = {}
    # Remove @ symbol if present
    clean_username = username.lstrip('@')
    logging.info(f"Checking username: @{clean_username} ...")
    
    try:
        # Get entity by username
        entity = await client.get_entity(clean_username)
        
        # Check if it's a User (not a Channel or Chat)
        if isinstance(entity, types.User):
            result.update(
                {
                    "id": entity.id,
                    "username": entity.username,
                    "usernames": [u.username for u in (entity.usernames or [])] if entity.usernames else None,
                    "first_name": entity.first_name,
                    "last_name": entity.last_name,
                    "fake": entity.fake,
                    "verified": entity.verified,
                    "premium": entity.premium,
                    "mutual_contact": entity.mutual_contact,
                    "bot": entity.bot,
                    "bot_chat_history": entity.bot_chat_history,
                    "restricted": entity.restricted,
                    "restriction_reason": entity.restriction_reason,
                    "user_was_online": get_human_readable_user_status(entity.status),
                    "phone": entity.phone,
                }
            )
            
            if download_profile_photos is True:
                try:
                    photo_output_path = Path(f"{entity.id}_{clean_username}_photo.jpeg")
                    logging.info(
                        "Attempting to download profile photo for @%s (%s)",
                        clean_username,
                        str(entity.id),
                    )
                    photo = await client.download_profile_photo(
                        entity, file=photo_output_path, download_big=True
                    )
                    if photo is not None:
                        logging.info("Downloaded photo at '%s'", photo)
                    else:
                        logging.info(
                            "No photo found for @%s (%s)", clean_username, str(entity.id)
                        )
                except Exception as e:
                    logging.exception(
                        "---\nUnable to download profile photo for @%s. Exception provided below.\n---\n%s\n---\n",
                        clean_username,
                        str(e),
                    )
        elif isinstance(entity, types.Channel):
            result.update(
                {
                    "error": f"@{clean_username} is a channel or supergroup ('{entity.title}'), not a user account. This tool is for searching user accounts only."
                }
            )
        elif isinstance(entity, types.Chat):
            result.update(
                {
                    "error": f"@{clean_username} is a group chat ('{entity.title}'), not a user account. This tool is for searching user accounts only."
                }
            )
        else:
            result.update(
                {
                    "error": f"@{clean_username} returned an unexpected entity type: {type(entity).__name__}"
                }
            )
    except FloodWaitError as e:
        logging.warning("FloodWait: ждём %d секунд...", e.seconds)
        await asyncio.sleep(e.seconds)
        result.update({"error": f"FloodWait {e.seconds}s, попробуйте позже."})
    except errors.UsernameNotOccupiedError:
        result.update({"error": f"Username @{clean_username} does not exist on Telegram."})
    except errors.UsernameInvalidError:
        result.update({"error": f"Username @{clean_username} is invalid."})
    except ValueError as e:
        result.update({"error": f"Could not find username @{clean_username}: {e}"})
    except Exception as e:
        result.update({"error": f"Unexpected error while searching for @{clean_username}: {e}."})
        raise
    
    logging.info("Done.")
    return result


async def validate_users(
    client: TelegramClient, phone_numbers: str, download_profile_photos: bool
) -> dict:
    """
    Take in a string of comma separated phone numbers and try to get the user information associated with each phone number.
    """
    if not phone_numbers or not len(phone_numbers):
        phone_numbers = input("Enter the phone numbers to check, separated by commas: ")
    result = {}
    phones = [re.sub(r"\s+", "", p, flags=re.UNICODE) for p in phone_numbers.split(",")]
    try:
        for phone in phones:
            if phone not in result:
                result[phone] = await get_names(client, phone, download_profile_photos)
    except Exception as e:
        logging.error(e)
        raise
    return result


async def validate_usernames(
    client: TelegramClient, usernames: str, download_profile_photos: bool
) -> dict:
    if not usernames or not len(usernames):
        usernames = input("Enter the usernames to check, separated by commas: ")
    result = {}
    username_list = [re.sub(r"\s+", "", u, flags=re.UNICODE) for u in usernames.split(",")]
    try:
        for username in username_list:
            if username not in result:
                result[username] = await get_user_by_username(client, username, download_profile_photos)
                await asyncio.sleep(2)  # пауза 2 секунды между запросами
    except Exception as e:
        logging.error(e)
        raise
    return result


async def login(
    api_id: str | None, api_hash: str | None, phone_number: str | None
) -> TelegramClient:
    """Create a telethon session or reuse existing one"""
    logging.info("Logging in...")
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
    logging.info("Done.")
    return client


def show_results(output: str, res: dict) -> None:
    logging.info(json.dumps(res, indent=4))
    with open(output, "w") as f:
        json.dump(res, f, indent=4)
        logging.info(f"Results saved to {output}")


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
    "--usernames",
    "-u",
    help="List of usernames to check, separated by commas (e.g. 'username' or '@username')",
    type=str,
)
@click.option(
    "--usernames-file",
    "-f",
    help="Path to a CSV file containing usernames to check",
    type=click.Path(exists=True, readable=True),
    default=None,
)
@click.option(
    "--usernames-column",
    help="Column name in the CSV file to read usernames from (defaults to first column)",
    type=str,
    default=None,
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
    "--download-profile-photos",
    help="Download the user profile photo associated with requested Telegram account",
    is_flag=True,
    default=False,
    show_default=True,
)
def main_entrypoint(
    phone_numbers: str,
    usernames: str,
    usernames_file: str,
    usernames_column: str,
    api_id: str,
    api_hash: str,
    api_phone_number: str,
    output: str,
    download_profile_photos: bool,
) -> None:
    """
    Check to see if one or more phone numbers or usernames belong to a valid Telegram account.

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
            usernames,
            usernames_file,     # <-- новый
            usernames_column,   # <-- новый
            api_id,
            api_hash,
            api_phone_number,
            output,
            download_profile_photos,
        )
    )


async def run_program(
    phone_numbers: str,
    usernames: str,
    usernames_file: str,
    usernames_column: str,
    api_id: str,
    api_hash: str,
    api_phone_number: str,
    output: str,
    download_profile_photos: bool = False,
):
    """
    Get all args passed from Click parser, pass them into the script.
    """
    client = await login(api_id, api_hash, api_phone_number)
    
    results = {}
    
    # Search by phone numbers if provided
    if phone_numbers:
        phone_results = await validate_users(client, phone_numbers, download_profile_photos)
        results.update(phone_results)
    
    # Search by usernames if provided
    all_usernames_parts = []
    if usernames:
        all_usernames_parts.append(usernames)
    if usernames_file:
        all_usernames_parts.append(load_usernames_from_csv(usernames_file, usernames_column))
    combined_usernames = ",".join(all_usernames_parts) or None

    if combined_usernames:
        username_list = [u.strip() for u in combined_usernames.split(",")]
        for username in username_list:
            if username not in results:
                results[username] = await get_user_by_username(client, username, download_profile_photos)
                await asyncio.sleep(2)
                show_results(output, results)  # сохраняем после каждого

    if not phone_numbers and not combined_usernames:
        choice = input("Search by (p)hone numbers or (u)sernames? [p/u]: ").lower()
        if choice == 'u':
            usernames = input("Enter the usernames to check, separated by commas: ")
            results.update(await validate_usernames(client, usernames, download_profile_photos))
        else:
            phone_numbers = input("Enter the phone numbers to check, separated by commas: ")
            results.update(await validate_users(client, phone_numbers, download_profile_photos))
    
    show_results(output, results)
    client.disconnect()


if __name__ == "__main__":
    main_entrypoint()