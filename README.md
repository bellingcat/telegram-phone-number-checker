# Telegram_phone_numbers

This script lets you check whether a specific phone number is connected to a Username on Telegram.

To run it, you need:
1. A Telegram account with an active phone number;
2. Telegram 'API_ID' and 'API_HASH', which you can get by creating a developers account using this link: https://my.telegram.org/.

Insert your PHONE_NUMBER, the API_ID and the API_HASH in the script.
Then, run the script: python3 Telegram_phone_validation.py

If available, you will now receive the Telegram Username (starting with @) that is connected with this number.

If you get an error message this might be due to the following reasons:
1. The phone number has not been used to create a Telegram account;
2. The phone number is connected to a Telegram account but the user did not choose a Telegram Username (this is optional on Telegram);
3. The phone number is connected to a Telegram account but the user has restricted the option to find him/her via the phone number.
4. Other error

