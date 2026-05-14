import asyncio

import click

from telegram_phone_number_checker.core import run_program


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
    "--usernames",
    "-u",
    help="List of usernames to check, separated by commas (e.g. 'username' or '@username')",
    type=str,
)
@click.option(
    "--api-id",
    help="Your Telegram app api_id",
    type=str,
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
@click.option(
    "--download-profile-photos",
    help="Download the user profile photo associated with requested Telegram account",
    is_flag=True,
    default=False,
    show_default=True,
)
def main_entrypoint(
    phone_numbers: str,
    usernames: str,
    api_id: str,
    api_hash: str,
    api_phone_number: str,
    output: str,
    download_profile_photos: bool,
) -> None:
    asyncio.run(
        run_program(
            phone_numbers,
            usernames,
            api_id,
            api_hash,
            api_phone_number,
            output,
            download_profile_photos,
        )
    )
