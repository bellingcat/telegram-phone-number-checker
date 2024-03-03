import os, json, re
from telethon.sync import TelegramClient, errors, functions
from telethon.tl.types import InputPhoneContact
from dotenv import load_dotenv
from getpass import getpass
import click

def get_names(client: TelegramClient, phone_number: str) -> dict:
    """
    Takes in a phone number and returns the associated user information if the user exists. It does so by first adding the user's phones to the contact list, retrieving the information, and then deleting the user from the contact list.
    """
    result = {}
    print(f'Checking: {phone_number=} ...', end="", flush=True)
    try:
        # Create a contact
        contact = InputPhoneContact(client_id = 0, phone = phone_number, first_name="", last_name="")
        # Attempt to add the contact from the address book
        contacts = client(functions.contacts.ImportContactsRequest([contact]))

        users = contacts.to_dict().get('users', [])
        number_of_matches = len(users)

        if number_of_matches == 0:
            result.update({"error": f'No response, the phone number is not on Telegram or has blocked contact adding.'})
        elif number_of_matches == 1:
            # Attempt to remove the contact from the address book
            # The response from DeleteContactsRequest contains more information than from ImportContactsRequest
            del_user = client(functions.contacts.DeleteContactsRequest(id=[users[0].get('id')]))
            user  = del_user.to_dict().get('users')[0]
            # getting more information about the user
            result.update({
                "id": user.get('id'),
                "username": user.get('username'),
                "first_name": user.get('first_name'),
                "last_name": user.get('last_name'),
                "fake" : user.get('fake'),
                "verified" : user.get('verified'),
                "premium" : user.get('premium'),
                "mutual_contact" : user.get('mutual_contact'),
                "bot" : user.get('bot'),
                "bot_chat_history" : user.get('bot_chat_history'),
                "restricted" : user.get('restricted'),
                "restriction_reason" : user.get('restriction_reason'),
                "Was online": user.get('status', {}).get('was_online').strftime("%Y-%m-%d %H:%M:%S %Z") if user.get('status') and user.get('status', {}).get('was_online') else None

                })
        else:
            result.update({"error": f'This phone number matched multiple Telegram accounts, which is unexpected. Please contact the developer: contact-tech@bellingcat.com'})

    except TypeError as e:
        result.update({"error": f"TypeError: {e}. --> The error might have occurred due to the inability to delete the {phone_number=} from the contact list."})
    except Exception as e:
        result.update({"error": f"Unexpected error: {e}."})
        raise
    print("Done.")
    return result


def validate_users(client: TelegramClient, phone_numbers: str) -> dict:
    """
    Takes in a string of comma separated phone numbers and tries to get the user information associated with each phone number. 
    """
    if not phone_numbers or not len(phone_numbers):
        phone_numbers = input('Enter the phone numbers to check, separated by commas: ')
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


def login(api_id: str, api_hash: str, phone_number: str) -> TelegramClient:
    """Create a telethon session or reuse existing one"""
    print('Logging in...', end="", flush=True)
    API_ID = api_id or os.getenv('API_ID') or input('Enter your API ID: ')
    API_HASH = api_hash or os.getenv('API_HASH') or input('Enter your API HASH: ')
    PHONE_NUMBER = phone_number or os.getenv('PHONE_NUMBER') or input('Enter your phone number: ')
    client = TelegramClient(PHONE_NUMBER, API_ID, API_HASH)
    client.connect()
    if not client.is_user_authorized():
        client.send_code_request(PHONE_NUMBER)
        try:
            client.sign_in(PHONE_NUMBER, input('Enter the code (sent on telegram): '))
        except errors.SessionPasswordNeededError:
            pw = getpass('Two-Step Verification enabled. Please enter your account password: ')
            client.sign_in(password=pw)
    print("Done.")
    return client

def show_results(output: str, res: dict) -> None:
    print(json.dumps(res, indent=4))
    with open(output, 'w') as f:
        json.dump(res, f, indent=4)
        print(f"Results saved to {output}")

@click.command()
@click.option('--phone-numbers', '-p', help='List of phone numbers to check, separated by commas', type=str)
@click.option('--api-id', help='Your API_ID', type=str)
@click.option('--api-hash', help='Your API_HASH', type=str)
@click.option('--api-phone-number', help='Your phone_number', type=str)
@click.option('--output', help='results filename, default to results.json', default="results.json", type=str)
def main_entrypoint(phone_numbers: str, api_id: str, api_hash: str, api_phone_number: str, output: str) -> None:
    """Check to see if one or more phone numbers belong to a valid Telegram account"""
    load_dotenv(".env")
    client = login(api_id, api_hash, api_phone_number)
    res =  validate_users(client, phone_numbers)
    show_results(output, res)


if __name__ == '__main__':
    main_entrypoint()