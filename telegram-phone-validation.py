#!/usr/local/bin/python3
from telethon import TelegramClient, events, sync
from telethon.tl.types import InputPhoneContact
from telethon import functions, types
from dotenv import load_dotenv
import argparse
import os

load_dotenv()

result = {}

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')

def get_names(phone_number):
    try:
        contact = InputPhoneContact(client_id = 0, phone = phone_number, first_name="__test__", last_name="__last_test__")
        contacts = client(functions.contacts.ImportContactsRequest([contact]))
        username = contacts.to_dict()['users'][0]['username']
        del_usr = client(functions.contacts.DeleteContactsRequest(id=[username]))
        if not username:
            return f'Response detected, but no user name returned by the API for the number: {phone_number}'
        else:
            return username
    except IndexError as e:
        return f'ERROR: there was no response for the phone number: {phone_number}'
    except:
        raise


def user_validator():
    '''
    The function uses the get_api_response function to first check if the user exists and if it does, then it returns the first user name and the last user name.
    '''
    input_phones = input("Phone numbers: ")
    phones = input_phones.split()
    try:
        for phone in phones:
            api_res = get_names(phone)
            result[phone] = api_res
    except:
        raise
            

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check to see if a phone number is a valid Telegram account')
    parser.add_argument('--phone_number', dest='phone_number', action='store', help='Enter the phone number to check')

    args = parser.parse_args()

    client = TelegramClient(args.phone_number, API_ID, API_HASH)
    client.connect()
    if not client.is_user_authorized():
        client.send_code_request(args.phone_number)
        client.sign_in(args.phone_number, input('Enter the code (sent on telegram): '))
    user_validator()
    print(result)
