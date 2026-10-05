import re


PHONE_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
COMPANY_HEADER_PATTERN = re.compile(
    r"^\[(?P<company_name>.+)\]\((?P<company_link>https?://[^\s]+)\)$"
)


def normalize_phone_line(line: str) -> str:
    """Normalize punctuation in one pasted phone number without guessing a prefix."""
    stripped = line.strip()
    if (
        len(stripped) >= 2
        and stripped[0] == stripped[-1]
        and stripped[0] in {"\"", "'"}
    ):
        stripped = stripped[1:-1].strip()
    if any(character.isalpha() for character in stripped):
        return stripped
    digits = "".join(character for character in stripped if character.isdigit())
    if stripped.startswith("+") or (len(digits) == 11 and digits.startswith("7")):
        return f"+{digits}"
    return stripped


def parse_company_phone_batch(raw: str, deduplicate: bool = True) -> list[dict[str, str]]:
    """Parse optional Markdown company headers and their following phone numbers."""
    rows: list[dict[str, str]] = []
    company_name = ""
    company_link = ""
    seen: set[tuple[str, str, str]] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        header = COMPANY_HEADER_PATTERN.fullmatch(stripped)
        if header:
            company_name = header.group("company_name")
            company_link = header.group("company_link")
            continue
        if not stripped.startswith("+") and any(character.isalpha() for character in stripped):
            company_name = stripped
            company_link = ""
            continue
        phone = normalize_phone_line(stripped)
        key = (company_name, company_link, phone)
        if not deduplicate or key not in seen:
            rows.append(
                {
                    "company_name": company_name,
                    "company_link": company_link,
                    "phone": phone,
                }
            )
            seen.add(key)
    return rows


def parse_phone_batch(raw: str) -> list[str]:
    """Return unique, lightly formatted international phone numbers in input order."""
    phones: list[str] = []
    for row in parse_company_phone_batch(raw):
        phone = row["phone"]
        if phone not in phones:
            phones.append(phone)
    return phones


def is_valid_phone(phone: str) -> bool:
    return bool(PHONE_PATTERN.fullmatch(phone))


def map_lookup_result(phone: str, result: dict) -> dict[str, str]:
    """Convert the upstream response into a stable row for display and CSV export."""
    error = result.get("error")
    if error:
        status = (
            "DEFERRED_NOT_CHECKED"
            if result.get("skipped")
            else (
                "NOT_FOUND_OR_PRIVACY_RESTRICTED"
                if error.startswith("No response")
                else "ERROR"
            )
        )
        return {
            "phone": phone,
            "status": status,
            "telegram_id": "",
            "username": "",
            "name": "",
            "telegram_link": "",
            "detail": error,
        }

    username = result.get("username") or ""
    name = " ".join(
        value.strip()
        for value in (result.get("first_name") or "", result.get("last_name") or "")
        if value.strip()
    )
    return {
        "phone": phone,
        "status": "FOUND" if username else "FOUND_NO_USERNAME",
        "telegram_id": str(result.get("id") or ""),
        "username": username,
        "name": name,
        "telegram_link": (
            f"https://t.me/{username}"
            if username
            else f"https://t.me/{phone}?profile"
        ),
        "detail": "",
    }
