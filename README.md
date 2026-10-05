# telegram-phone-number-checker

<a href="https://www.bellingcat.com"><img alt="Bellingcat logo: Discover Bellingcat" src="https://img.shields.io/badge/Discover%20Bellingcat-%20?style=for-the-badge&logo=data%3Aimage%2Fpng%3Bbase64%2CiVBORw0KGgoAAAANSUhEUgAAAA4AAAAYCAYAAADKx8xXAAABhGlDQ1BJQ0MgcHJvZmlsZQAAKJF9kT1Iw0AcxV9TS0UqDnZQEcxQneyiIo6likWwUNoKrTqYXPoFTRqSFBdHwbXg4Mdi1cHFWVcHV0EQ%2FABxdnBSdJES%2F5cUWsR4cNyPd%2Fced%2B8AoVllqtkTA1TNMtKJuJjLr4rBVwQwhhBEDEvM1JOZxSw8x9c9fHy9i%2FIs73N%2Fjn6lYDLAJxLHmG5YxBvEs5uWznmfOMzKkkJ8Tjxp0AWJH7kuu%2FzGueSwwDPDRjY9TxwmFktdLHcxKxsq8QxxRFE1yhdyLiuctzir1Tpr35O%2FMFTQVjJcpzmKBJaQRIo6klFHBVVYiNKqkWIiTftxD%2F%2BI40%2BRSyZXBYwcC6hBheT4wf%2Fgd7dmcXrKTQrFgcCLbX%2BMA8FdoNWw7e9j226dAP5n4Err%2BGtNYO6T9EZHixwBA9vAxXVHk%2FeAyx1g6EmXDMmR%2FDSFYhF4P6NvygODt0Dfmttbex%2BnD0CWulq%2BAQ4OgYkSZa97vLu3u7d%2Fz7T7%2BwHEU3LHAa%2FQ6gAAAAZiS0dEAAAAAAAA%2BUO7fwAAAAlwSFlzAAAuIwAALiMBeKU%2FdgAAAAd0SU1FB%2BgFHwwiMH4odB4AAAAZdEVYdENvbW1lbnQAQ3JlYXRlZCB3aXRoIEdJTVBXgQ4XAAAA50lEQVQ4y82SvWpCQRCFz25ERSJiCNqlUiS1b5AuEEiZIq1NOsGXCKms0wXSp9T6dskDiFikyiPc%2FrMZyf3FXSGQ0%2BzuzPl2ZoeVKgQ0gQ2wBVpVHlcDkjM5V%2FJ5nag6sJ%2FZX%2Bh%2FC7gEhqeAFKf7p1M9aB3b5oN1OomB7g1axUBPBr3GQHODHmOgqUF3MZAzKI2d4LWBV4H%2BMXDuJd1a7Cew1k7SwksaHC4LqNaw7aeX9GWHXkC1G1sTAS17Y3Kk2lnp4wNLiz0DrgLq8qt2MfmSSabAO%2FBBXp26dtrADPjOmN%2BAUdG7B3cE61l5hOZiAAAAAElFTkSuQmCC&logoColor=%23fff&color=%23000"></a><!--
--><a href="https://discord.gg/bellingcat"><img alt="Discord logo: Join our community" src="https://img.shields.io/badge/Join%20our%20community-%20?style=for-the-badge&logo=discord&logoColor=%23fff&color=%235865F2"></a><!--
--><a href="https://colab.research.google.com/github/bellingcat/open-source-research-notebooks/blob/main/notebooks/bellingcat/telegram-phone-number-checker.ipynb"><img alt="Colab icon: Try it on Colab" src="https://img.shields.io/badge/Try%20it%20on%20Colab-%20?style=for-the-badge&logo=googlecolab&logoColor=fff&logoSize=auto&color=e8710a"></a>

Python tool/script to check if phone numbers are connected to Telegram accounts. Retrieving username, name, and IDs where available.

## Local web interface

The project also includes a small local web interface for checking a pasted batch of phone numbers. It has no external web dependencies.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill `API_ID`, `API_HASH`, and `PHONE_NUMBER` in `.env`, then start it:

```bash
TELEGRAM_VALIDATOR_PORT=8010 .venv/bin/python -m telegram_phone_number_checker.web
```

Open [http://127.0.0.1:8010](http://127.0.0.1:8010) and authorize the single Telegram user account in the browser: enter the account phone number, the Telegram login code, and the 2FA password if requested. The web worker reuses this persistent user session; it does not use a bot account. Login itself never starts or resumes a validation job.

Paste companies and phone numbers, then click **Поставить в очередь**. The list remains staged until **Запустить обработку** is pressed. The page shows the current outcome summary, phase, row statuses, waiting reason, next scheduled import attempt and CSV export; set `TELEGRAM_VALIDATOR_PORT` to another free port if needed.

The first launch migrates a previous `.validator-jobs.json` queue into `.validator.db` without changing the JSON source. Migrated JSON jobs remain `STAGED` until you explicitly start them. The SQLite database contains phone numbers and is intentionally ignored by Git.

### Reading results and resuming work

- **Подтверждено API** counts `FOUND` and `FOUND_NO_USERNAME`: Telegram returned an account. Manual confirmation does not increase this count.
- **API не определил из-за приватности** counts `UNRESOLVABLE_OR_PRIVACY`: the API did not establish a phone match. Privacy restrictions may cause this result; it does not prove the account is absent. Open the phone profile link yourself and use **Подтвердить вручную** only after verifying the chat. This records `MANUAL_CHAT_CONFIRMED` separately while preserving the API result.
- **Повторный импорт ожидают** counts `PENDING_RETRY`. Telegram's deferred imports stay queued for another import with a stored retry schedule. **Ожидают импорта** counts other pending or active import rows. Retries, unknown outcomes, authorization waits and FloodWait suppress the completion ETA; the page shows the saved reason and available next-attempt schedule instead.
- **Требуется повторная авторизация** shows unfinished rows in a `WAITING_AUTHORIZATION` job. This is a wait indicator and can overlap the pending and unknown counts. Authorize again in the browser, then explicitly press **Возобновить после авторизации** on the job page. The control becomes available only when the session is `AUTHORIZED`; entering a code or password never resumes the queue.
- `UNKNOWN_OUTCOME_REVIEW` means a dispatched request may have succeeded but its response was lost, including after a restart. Pause the job and wait until active requests finish. After checking the saved evidence, **Разрешить повтор проверок с неизвестным результатом** explicitly returns the affected phone rows to delayed import. A repeated request may repeat Telegram-side effects. The job remains paused and requires a separate resume. Unknown cleanup requests remain quarantined; this phone requeue action does not retry them.

For old direct-lookup rows, pause the job and wait until active requests finish, then use **Вернуть старые прямые проверки в очередь импорта**. It migrates eligible unconfirmed rows, preserves prior attempts and confirmed results, and keeps the job paused. Resume only after reviewing the results. Every row's current API status and manual confirmation remain available in the CSV.

## Personal Docker container

To run the service only when you need it, use the `tg-validatorForSelf` container. Docker stores the Telegram session and validator queue in its named volume, so they remain available after the container is stopped.

```bash
docker compose up --build
```

Then open [http://127.0.0.1:8010](http://127.0.0.1:8010), enter the Telegram account number, then the Telegram login code, and, if enabled, the two-factor password. The login phone, code, and password are used only for the active authorization flow and are not saved in the queue or CSV. Stop and resume the container with:

```bash
docker compose stop
docker compose start
```

Keep the named volume to preserve the user session, queued rows and audit history. On restart the same authorized session is reused. Paused, staged and authorization-waiting jobs still require their explicit start/resume control. A request whose outcome became unknown is retained for operator review rather than retried automatically.

Use `TG_VALIDATOR_PORT=8011 docker compose up` if local port 8010 is already occupied.

The legacy CLI status `NOT_FOUND_OR_PRIVACY_RESTRICTED` likewise means Telegram did not return a contact match. It does not prove account absence.

> ⚠️ NB: We advise you **not to use your personal account** for automations as telegram may block it. A fresh account works best from residential IPs rather than known VPN IPs.
 
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

### Search by phone number
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

### Search by Username
You can also search by Telegram username(s):

```bash
# single username
telegram-phone-number-checker --usernames john

# multiple usernames
telegram-phone-number-checker --usernames john,jane

# combine phone numbers and usernames
telegram-phone-number-checker --phone-numbers +1234567890 --usernames johndoe

# with profile photo download
telegram-phone-number-checker --usernames johndoe --download-profile-photos
```

The result will be written to the console but also written as JSON to a `results.json` file, you can write it to another file by adding `--output your_filename.json` to the command.

### Connecting through a proxy

If you need to connect through a SOCKS5/SOCKS4/HTTP proxy (for example, to avoid
using a known VPN/datacenter IP - see the warning above), install the optional
`proxy` extra and pass `--proxy`:

```bash
pip install telegram-phone-number-checker[proxy]

telegram-phone-number-checker --phone-numbers +1234567890 --proxy socks5://user:pass@127.0.0.1:1080
```

The proxy can also be set via the `PROXY` environment variable or in your `.env` file.

For each phone number or username, you can expect the following possible responses:

1. If available, you will receive the Telegram Username, Name, and ID that are connected with this number.
2. 'no username detected'. This means that it looks like the number was used to create a Telegram account but the user did not choose a Telegram Username. It is optional to create a Username on Telegram.
3. 'ERROR: no response, the user does not exist or has blocked contact adding.': There can be several reasons for this response. Either the phone number has not been used to create a Telegram account. Or: The phone number is connected to a Telegram account but the user has restricted the option to find him/her via the phone number.
4. For username searches: The username may not exist, or it may belong to a channel/group rather than a user account. In the latter case, an output such as '@group_name is a channel or supergroup ('Group'), not a user account. This tool is for searching user accounts only'
5. Or: another error occurred.


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
