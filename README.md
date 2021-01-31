# Telegram_phone_numbers

This script lets you check whether a specific phone number is connected to a Telegram account.

To run it, you need:

1. A Telegram account with an active phone number;
2. Telegram 'API_ID' and 'API_HASH', which you can get by creating a developers account using this link: https://my.telegram.org/.

Insert your PHONE_NUMBER, the API_ID and the API_HASH in the script.
Then, run the script: python3 Telegram_phone_validation.py

You can expect the following possible responses:

1. If available, you will receive the Telegram Username that is connected with this number.
2. 'Response detected, but no user name returned by the API for the number: {phone_number}'. This means that it looks like the number was used to create a Telegram account but the user did not choose a Telegram Username. It is optional to create a Username on Telegram.
3. 'ERROR: there was no response for the phone number: {phone_number}': There can be several reasons for this response. Either the phone number has not been used to create a Telegram account. Or: The phone number is connected to a Telegram account but the user has restricted the option to find him/her via the phone number. Or: another error occurred.
 


