import os
import json
import re
from telethon.sync import TelegramClient, errors, functions
from telethon.tl.types import InputPhoneContact
from dotenv import load_dotenv
from getpass import getpass
import click


load_dotenv()


def get_names(client: TelegramClient, phone_number: str) -> dict:
    """Take in a phone number and returns the associated user information if the user exists.

    It does so by first adding the user's phones to the contact list, retrieving the
    information, and then deleting the user from the contact list.
    """
    result = {}
    print(f"Checking: {phone_number=} ...", end="", flush=True)
    try:
        # Create a contact
        contact = InputPhoneContact(
            client_id=0, phone=phone_number, first_name="", last_name=""
        )
        # Attempt to add the contact from the address book
        contacts = client(functions.contacts.ImportContactsRequest([contact]))

        users = contacts.to_dict().get("users", [])
        number_of_matches = len(users)

        if not users:
            result.update(
                {
                    "error": "No response, the phone number is not on Telegram or has blocked contact adding."
                }
            )
        elif number_of_matches == 1:
            # Attempt to remove the contact from the address book.
            # The response from DeleteContactsRequest contains more information than from ImportContactsRequest
            del_user = client(
                functions.contacts.DeleteContactsRequest(id=[users[0].get("id")])
            )
            user = del_user.to_dict().get("users")[0]
            user_was_online = user.get("status", {}).get("was_online")
            # getting more information about the user
            result.update(
                {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "first_name": user.get("first_name"),
                    "last_name": user.get("last_name"),
                    "fake": user.get("fake"),
                    "verified": user.get("verified"),
                    "premium": user.get("premium"),
                    "mutual_contact": user.get("mutual_contact"),
                    "bot": user.get("bot"),
                    "bot_chat_history": user.get("bot_chat_history"),
                    "restricted": user.get("restricted"),
                    "restriction_reason": user.get("restriction_reason"),
                    "user_was_online": (
                        user_was_online.strftime("%Y-%m-%d %H:%M:%S %Z")
                        if user_was_online
                        else None
                    ),
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


def validate_users(client: TelegramClient, phone_numbers: str) -> dict:
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
                result[phone] = get_names(client, phone)
    except Exception as e:
        print(e)
        raise
    return result


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
    "--output",
    help="Filename to store results",
    default="results.json",
    show_default=True,
    type=str,
)
def main_entrypoint(
    phone_numbers: str, api_id: int, api_hash: str, api_phone_number: str, output: str
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
    client = TelegramClient(api_phone_number, api_id, api_hash)
    with client:
        res = validate_users(client, phone_numbers)
        show_results(output, res)


if __name__ == "__main__":
    main_entrypoint()
