# telegram-phone-number-checker

<a href="https://www.bellingcat.com"><img alt="Bellingcat logo: Discover Bellingcat" src="https://img.shields.io/badge/Discover%20Bellingcat-%20?style=for-the-badge&logo=data%3Aimage%2Fpng%3Bbase64%2CiVBORw0KGgoAAAANSUhEUgAAAA4AAAAYCAYAAADKx8xXAAABhGlDQ1BJQ0MgcHJvZmlsZQAAKJF9kT1Iw0AcxV9TS0UqDnZQEcxQneyiIo6likWwUNoKrTqYXPoFTRqSFBdHwbXg4Mdi1cHFWVcHV0EQ%2FABxdnBSdJES%2F5cUWsR4cNyPd%2Fced%2B8AoVllqtkTA1TNMtKJuJjLr4rBVwQwhhBEDEvM1JOZxSw8x9c9fHy9i%2FIs73N%2Fjn6lYDLAJxLHmG5YxBvEs5uWznmfOMzKkkJ8Tjxp0AWJH7kuu%2FzGueSwwDPDRjY9TxwmFktdLHcxKxsq8QxxRFE1yhdyLiuctzir1Tpr35O%2FMFTQVjJcpzmKBJaQRIo6klFHBVVYiNKqkWIiTftxD%2F%2BI40%2BRSyZXBYwcC6hBheT4wf%2Fgd7dmcXrKTQrFgcCLbX%2BMA8FdoNWw7e9j226dAP5n4Err%2BGtNYO6T9EZHixwBA9vAxXVHk%2FeAyx1g6EmXDMmR%2FDSFYhF4P6NvygODt0Dfmttbex%2BnD0CWulq%2BAQ4OgYkSZa97vLu3u7d%2Fz7T7%2BwHEU3LHAa%2FQ6gAAAAZiS0dEAAAAAAAA%2BUO7fwAAAAlwSFlzAAAuIwAALiMBeKU%2FdgAAAAd0SU1FB%2BgFHwwiMH4odB4AAAAZdEVYdENvbW1lbnQAQ3JlYXRlZCB3aXRoIEdJTVBXgQ4XAAAA50lEQVQ4y82SvWpCQRCFz25ERSJiCNqlUiS1b5AuEEiZIq1NOsGXCKms0wXSp9T6dskDiFikyiPc%2FrMZyf3FXSGQ0%2BzuzPl2ZoeVKgQ0gQ2wBVpVHlcDkjM5V%2FJ5nag6sJ%2FZX%2Bh%2FC7gEhqeAFKf7p1M9aB3b5oN1OomB7g1axUBPBr3GQHODHmOgqUF3MZAzKI2d4LWBV4H%2BMXDuJd1a7Cew1k7SwksaHC4LqNaw7aeX9GWHXkC1G1sTAS17Y3Kk2lnp4wNLiz0DrgLq8qt2MfmSSabAO%2FBBXp26dtrADPjOmN%2BAUdG7B3cE61l5hOZiAAAAAElFTkSuQmCC&logoColor=%23fff&color=%23000"></a><!--
--><a href="https://discord.gg/bellingcat"><img alt="Discord logo: Join our community" src="https://img.shields.io/badge/Join%20our%20community-%20?style=for-the-badge&logo=discord&logoColor=%23fff&color=%235865F2"></a><!--
--><a href="https://colab.research.google.com/github/bellingcat/open-source-research-notebooks/blob/main/notebooks/bellingcat/telegram-phone-number-checker.ipynb"><img alt="Colab icon: Try it on Colab" src="https://img.shields.io/badge/Try%20it%20on%20Colab-%20?style=for-the-badge&logo=googlecolab&logoColor=fff&logoSize=auto&color=e8710a"></a>

Python tool/script to check if phone numbers are connected to Telegram accounts. Retrieving username, name, and IDs where available.

## Installation

[![PyPI - Version](https://img.shields.io/pypi/v/telegram-phone-number-checker)
](https://pypi.org/project/telegram-phone-number-checker/)

You can install this tool directly from the [official pypi release](https://pypi.org/project/telegram-phone-number-checker/).

```bash
pip install telegram-phone-number-checker
```

You can also install it and run it directly from GitHub as a script.
```bash
git clone https://github.com/bellingcat/telegram-phone-number-checker
cd telegram-phone-number-checker
pip install -r requirements.txt
python telegram-phone-number-checker/main.py
```

## Requirements
To run it, you need:

1. A Telegram account with an active phone number;
2. Telegram `API_ID` and `API_HASH`, which you can get by creating a developers account at https://my.telegram.org/. Place these values in a `.env` file, along with the phone number of your Telegram account:

```
API_ID=
API_HASH=
PHONE_NUMBER=
```
If you don't create this file, you can also provide these 3 values when calling the tool, or even be prompted for them interactively.

## Usage
The tool accepts a comma-separated list of phone numbers to check, you can pass this when you call the tool, or interactively.

See the examples below:

```bash
# single phone number
telegram-phone-number-checker --phone-numbers +1234567890

# single phone number, download profile photo
telegram-phone-number-checker --phone-numbers +1234567890 --download-profile-photos

# multiple phone numbers
telegram-phone-number-checker --phone-numbers +1234567890,+9876543210,+111111111

# interactive version, you will be prompted for the phone-numbers
telegram-phone-number-checker

# overwrite the telegram API keys in .env (or if no .env is found)
telegram-phone-number-checker --api-id YOUR_API_KEY --api-hash YOUR_API_HASH --api-phone-number YOUR_PHONE_NUMBER --phone-numbers +1234567890
```

The result will be written to the console but also written as JSON to a `results.json` file, you can write it to another file by adding `--output your_filename.json` to the command.

For each phone number, you can expect the following possible responses:

1. If available, you will receive the Telegram Username, Name, and ID that are connected with this number.
2. 'no username detected'. This means that it looks like the number was used to create a Telegram account but the user did not choose a Telegram Username. It is optional to create a Username on Telegram.
3. 'ERROR: no response, the user does not exist or has blocked contact adding.': There can be several reasons for this response. Either the phone number has not been used to create a Telegram account. Or: The phone number is connected to a Telegram account but the user has restricted the option to find him/her via the phone number.
4. Or: another error occurred.


## Development 
This section describes how to install the project in order to run it locally, for example if you want to build new features.

```bash
# clone the code
git clone https://github.com/bellingcat/telegram-phone-number-checker

# move into the project's folder
cd telegram-phone-number-checker
```

This project uses [poetry](https://python-poetry.org/) to manage dependencies. You can install dependencies via poetry, or use the up-to-date [requirements.txt](requirements.txt) file.

```bash
# install poetry if you haven't already
pip install poetry

# with poetry
poetry install

# with pip
pip install -r requirements.txt
```

You can then run it with any of these:
```bash
# with poetry
poetry run telegram-phone-number-checker

# with pip installation
python3 telegram_phone_number_checker/main.py
```

### Generating `requirements.txt` & `requirements-dev.txt`

Poetry is used to generate both of these files. `requirements.txt` contains only those dependencies necessary for
running the CLI. `requirements-dev.txt` contains all dependencies including those used for running tests, linters, etc.

To generate `requirements.txt`:

```shell
poetry export --output=requirements.txt --without-urls
```

To generate `requirements-dev.txt`:

```shell
poetry export --output=requirements-dev.txt --without-urls --with=dev
```

💡 `--without-urls` is for users who install from their own private package repository
instead of pypi.org
