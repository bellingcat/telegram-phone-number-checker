#!/usr/local/bin/python3
from telethon import TelegramClient, events, sync
from telethon.tl.types import InputPhoneContact
from telethon import functions, types

result = {}

PHONE_NUMBER = # Enter the telegram account phone number here
API_ID = # Enter the api id 
API_HASH = #Enter the api hash

def get_names(phone_number):
    try:
        contact = InputPhoneContact(client_id = 0, phone = phone_number, first_name="__test__", last_name="__last_test__")
        contacts = client(functions.contacts.ImportContactsRequest([contact]))
        #first_name = contacts.to_dict()['users'][0]['first_name']
        #last_name = contacts.to_dict()['users'][0]['last_name']
        username = contacts.to_dict()['users'][0]['username']
        del_usr = client(functions.contacts.DeleteContactsRequest(id=[username]))
        if not username:
            return f'No user name returned by the API for the number: {phone_number}'
        else:
            return username
    except IndexError as e:
        #err = "ERROR - maybe the user does not exist or something else went wrong."
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
            #if type(api_res) == tuple:
            #    result[phone] = { 'first name' : api_res[0], 'last name' : api_res[1] }
            #else: 
            #    result[phone] = api_res
    except:
        raise
            

if __name__ == '__main__':
    client = TelegramClient(PHONE_NUMBER, API_ID, API_HASH)
    client.connect()
    if not client.is_user_authorized():
        client.send_code_request(PHONE_NUMBER)
        client.sign_in(PHONE_NUMBER, input('Enter the code (sent on telegram): '))
    user_validator()
    print(result)
