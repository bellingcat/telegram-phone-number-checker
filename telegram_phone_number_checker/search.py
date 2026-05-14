import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.tl import errors, functions, types

from telegram_phone_number_checker.formatting import get_human_readable_user_status

async def get_names(
    client: TelegramClient, phone_number: str, download_profile_photos: bool = False
) -> dict:
    result = {}
    logging.info(f"Checking: {phone_number=} ...")
    try:
        contact = types.InputPhoneContact(
            client_id=0, phone=phone_number, first_name="", last_name=""
        )
        contacts = await client(functions.contacts.ImportContactsRequest([contact]))
        users = contacts.to_dict().get("users", [])
        number_of_matches = len(users)

        if number_of_matches == 0:
            result.update(
                {
                    "error": "No response, the phone number is not on Telegram or has blocked contact adding."
                }
            )
        elif number_of_matches == 1:
            updates_response: types.Updates = await client(
                functions.contacts.DeleteContactsRequest(id=[users[0].get("id")])
            )
            user = updates_response.users[0]
            result.update(
                {
                    "id": user.id,
                    "username": user.username,
                    "usernames": [u.username for u in (user.usernames or [])]
                    if user.usernames
                    else None,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "fake": user.fake,
                    "verified": user.verified,
                    "premium": user.premium,
                    "mutual_contact": user.mutual_contact,
                    "bot": user.bot,
                    "bot_chat_history": user.bot_chat_history,
                    "restricted": user.restricted,
                    "restriction_reason": user.restriction_reason,
                    "user_was_online": get_human_readable_user_status(user.status),
                    "phone": user.phone,
                }
            )
            if download_profile_photos is True:
                try:
                    photo_output_path = Path("{}_{}_photo.jpeg".format(user.id, phone_number))
                    logging.info(
                        "Attempting to download profile photo for %s (%s)",
                        str(user.id),
                        str(phone_number),
                    )
                    photo = await client.download_profile_photo(
                        user, file=photo_output_path, download_big=True
                    )
                    if photo is not None:
                        logging.info("Downloaded photo at '%s'", photo)
                    else:
                        logging.info(
                            "No photo found for %s (%s)", str(user.id), str(phone_number)
                        )
                except Exception as e:
                    logging.exception(
                        "---\nUnable to download profile photo for %s. Exception provided below.\n---\n%s\n---\n",
                        str(phone_number),
                        str(e),
                    )
        else:
            result.update(
                {
                    "error": """This phone number matched multiple Telegram accounts, 
            which is unexpected. Please contact the developer: contact-tech@bellingcat.com"""
                }
            )
    except TypeError as e:
        result.update(
            {
                "error": f"TypeError: {e}. --> The error might have occurred due to the inability to delete the {phone_number=} from the contact list."
            }
        )
    except Exception as e:
        result.update({"error": f"Unexpected error: {e}."})
        raise
    logging.info("Done.")
    return result


async def get_user_by_username(
    client: TelegramClient, username: str, download_profile_photos: bool = False
) -> dict:
    result = {}
    clean_username = username.lstrip("@")
    logging.info(f"Checking username: @{clean_username} ...")

    try:
        entity = await client.get_entity(clean_username)

        if isinstance(entity, types.User):
            result.update(
                {
                    "id": entity.id,
                    "username": entity.username,
                    "usernames": [u.username for u in (entity.usernames or [])]
                    if entity.usernames
                    else None,
                    "first_name": entity.first_name,
                    "last_name": entity.last_name,
                    "fake": entity.fake,
                    "verified": entity.verified,
                    "premium": entity.premium,
                    "mutual_contact": entity.mutual_contact,
                    "bot": entity.bot,
                    "bot_chat_history": entity.bot_chat_history,
                    "restricted": entity.restricted,
                    "restriction_reason": entity.restriction_reason,
                    "user_was_online": get_human_readable_user_status(entity.status),
                    "phone": entity.phone,
                }
            )

            if download_profile_photos is True:
                try:
                    photo_output_path = Path(f"{entity.id}_{clean_username}_photo.jpeg")
                    logging.info(
                        "Attempting to download profile photo for @%s (%s)",
                        clean_username,
                        str(entity.id),
                    )
                    photo = await client.download_profile_photo(
                        entity, file=photo_output_path, download_big=True
                    )
                    if photo is not None:
                        logging.info("Downloaded photo at '%s'", photo)
                    else:
                        logging.info(
                            "No photo found for @%s (%s)", clean_username, str(entity.id)
                        )
                except Exception as e:
                    logging.exception(
                        "---\nUnable to download profile photo for @%s. Exception provided below.\n---\n%s\n---\n",
                        clean_username,
                        str(e),
                    )
        elif isinstance(entity, types.Channel):
            result.update(
                {
                    "error": f"@{clean_username} is a channel or supergroup ('{entity.title}'), not a user account. This tool is for searching user accounts only."
                }
            )
        elif isinstance(entity, types.Chat):
            result.update(
                {
                    "error": f"@{clean_username} is a group chat ('{entity.title}'), not a user account. This tool is for searching user accounts only."
                }
            )
        else:
            result.update(
                {
                    "error": f"@{clean_username} returned an unexpected entity type: {type(entity).__name__}"
                }
            )
    except errors.UsernameNotOccupiedError:
        result.update({"error": f"Username @{clean_username} does not exist on Telegram."})
    except errors.UsernameInvalidError:
        result.update({"error": f"Username @{clean_username} is invalid."})
    except ValueError as e:
        result.update({"error": f"Could not find username @{clean_username}: {e}"})
    except Exception as e:
        result.update({"error": f"Unexpected error while searching for @{clean_username}: {e}."})
        raise

    logging.info("Done.")
    return result
