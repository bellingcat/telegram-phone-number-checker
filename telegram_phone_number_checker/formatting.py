from telethon.tl import types

def get_human_readable_user_status(status: types.TypeUserStatus):
    match status:
        case types.UserStatusOnline():
            return "Currently online"
        case types.UserStatusOffline():
            return status.was_online.strftime("%Y-%m-%d %H:%M:%S %Z")
        case types.UserStatusRecently():
            return "Last seen recently"
        case types.UserStatusLastWeek():
            return "Last seen last week"
        case types.UserStatusLastMonth():
            return "Last seen last month"
        case _:
            return "Unknown"