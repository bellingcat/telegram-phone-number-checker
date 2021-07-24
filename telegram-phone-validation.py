#!/usr/local/bin/python3
from telethon import TelegramClient, errors, events, sync
from telethon.tl.types import InputPhoneContact
from telethon import functions, types
from dotenv import load_dotenv
import argparse
import os
from getpass import getpass


load_dotenv()

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
PHONE_NUMBER = os.getenv('PHONE_NUMBER')

def get_names(phone_number):    
    try:
        contact = InputPhoneContact(client_id = 0, phone = phone_number, first_name="", last_name="")
        contacts = client(functions.contacts.ImportContactsRequest([contact]))
        username = contacts.to_dict()['users'][0]['username']
        if not username:
            print("*"*5 + f' Response detected, but no user name returned by the API for the number: {phone_number} ' + "*"*5)
            del_usr = client(functions.contacts.DeleteContactsRequest(id=[username]))
            return
        else:
            del_usr = client(functions.contacts.DeleteContactsRequest(id=[username]))
            return username
    except IndexError as e:
        return f'ERROR: there was no response for the phone number: {phone_number}'
    except TypeError as e:
        return f"TypeError: {e}. --> The error might have occured due to the inability to delete the {phone_number} from the contact list."
    except:
        raise

def user_validator(phone_numbers: list):
    '''
    The function uses the get_api_response function to first check if the user exists and if it does, then it returns the first user name and the last user name.
    '''
    result = {}
    for phone in phones:
        api_res = get_names(phone)
        result[phone] = api_res

    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check to see if a phone number is a valid Telegram account')
    parser.add_argument('-i', dest='input_file_path', help='Path to an optional telephone list .txt file to be used as input')
    parser.add_argument('--tocsv', action="store_true", 
                        help='If present, the output will be parsed as a csv. Useful for piping the output into a file [$script -i path --tocsv > output.txt]')

    args = parser.parse_args()

    client = TelegramClient(PHONE_NUMBER, API_ID, API_HASH)
    client.connect()
    if not client.is_user_authorized():
        client.send_code_request(PHONE_NUMBER)
        try:
            client.sign_in(PHONE_NUMBER, input('Enter the code (sent on telegram): '))
        except errors.SessionPasswordNeededError:
            pw = getpass('Two-Step Verification enabled. Please enter your account password: ')
            client.sign_in(password=pw)

    # get input from file if the argument was used, if not, get them from the user input
    input_phones = []
    if not args.input_file_path:
        input_phones = input("Phone numbers: ")
        phones = input_phones.split()

    else:
        with open(args.input_file_path, 'r') as input_file:
            input_phones = input_file.readlines()
        
        # remove spaces and newlines in the phones
        phones = [tlf.strip('\n').replace(' ','') for tlf in input_phones]

    result = user_validator(phones)
    if not args.tocsv:
        print(result)

    else:
        csv = "telephone,username\n"
        for phone, username in result.items():
            csv += f"{phone},{username}\n"

        print(csv)
