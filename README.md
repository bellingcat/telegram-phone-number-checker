# telegram-phone-number-checker

This script lets you check whether a specific phone number is connected to a Telegram account.

To run it, you need:

1. A Telegram account with an active phone number;
2. Telegram 'API_ID' and 'API_HASH', which you can get by creating a developers account using this link: https://my.telegram.org/. Place these values in a .env file, along with the phone number of your Telegram account:

```
API_ID=
API_HASH=
PHONE_NUMBER=
```

## Installing Dependencies

This project uses [pipenv](https://pipenv.pypa.io/en/latest/#install-pipenv-today) to manage dependencies. Install pipenv on your machine, and then this project's dependencies can be installed like so:

```sh
pipenv install
```

## Usage
```sh
pipenv run python telegram-phone-validation.py [OPTIONS...]
```

By default the script prompts the user to enter phone numbers separated by spaces. See below for additional options. 

You can expect the following possible responses:

1. If available, you will receive the Telegram Username that is connected with this number.
2. 'Response detected, but no user name returned by the API for the number: {phone_number}'. This means that it looks like the number was used to create a Telegram account but the user did not choose a Telegram Username. It is optional to create a Username on Telegram.
3. 'ERROR: there was no response for the phone number: {phone_number}': There can be several reasons for this response. Either the phone number has not been used to create a Telegram account. Or: The phone number is connected to a Telegram account but the user has restricted the option to find him/her via the phone number. Or: another error occurred.

### Options

The script supports the following command line options:

```
    -i, --input    Path to an optional telephone list .txt file to be used as input
    -o, --output   Save the output to a CSV file with the given filename
```
