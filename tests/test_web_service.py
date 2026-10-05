from telegram_phone_number_checker.web_service import (
    is_valid_phone,
    map_lookup_result,
    parse_company_phone_batch,
    parse_phone_batch,
)


def test_parses_unique_normalized_phone_lines():
    assert parse_phone_batch(
        "+7 (999) 123-45-67\n+ +79991234567\n\n+44 7911 123456"
    ) == ["+79991234567", "+447911123456"]


def test_normalizes_phone_pasted_with_csv_quotes():
    assert parse_phone_batch('"+7 (812) 313-87-86"') == ["+78123138786"]


def test_parses_markdown_company_headers_without_creating_invalid_rows():
    assert parse_company_phone_batch(
        "[ООО ПКО \"ФИЛБЕРТ\"](https://basis.myseldon.com/company/1107847250961)\n"
        "+7 (812) 313-87-86\n"
        "+7 (800) 333-01-25\n\n"
        "[Другая компания](https://example.test/company/2)\n"
        "+7 (921) 961-21-38"
    ) == [
        {
            "company_name": 'ООО ПКО "ФИЛБЕРТ"',
            "company_link": "https://basis.myseldon.com/company/1107847250961",
            "phone": "+78123138786",
        },
        {
            "company_name": 'ООО ПКО "ФИЛБЕРТ"',
            "company_link": "https://basis.myseldon.com/company/1107847250961",
            "phone": "+78003330125",
        },
        {
            "company_name": "Другая компания",
            "company_link": "https://example.test/company/2",
            "phone": "+79219612138",
        },
    ]


def test_parses_plain_company_names_without_links():
    assert parse_company_phone_batch(
        'ААУ "ЦФОП АПК"\t\n+7 (495) 540-42-86\n\nООО ПКО "ФИЛБЕРТ"\n+7 (812) 313-87-86'
    ) == [
        {
            "company_name": 'ААУ "ЦФОП АПК"',
            "company_link": "",
            "phone": "+74955404286",
        },
        {
            "company_name": 'ООО ПКО "ФИЛБЕРТ"',
            "company_link": "",
            "phone": "+78123138786",
        },
    ]


def test_normalizes_russian_phone_without_plus_prefix():
    assert parse_phone_batch("79151054401") == ["+79151054401"]
    assert is_valid_phone("+79151054401") is True


def test_rejects_phone_with_extension_instead_of_changing_its_digits():
    phone = parse_phone_batch("+1 202 555 0123 ext 4")[0]
    assert phone == "+1 202 555 0123 ext 4"
    assert is_valid_phone(phone) is False


def test_maps_username_less_match_without_inventing_link():
    assert map_lookup_result(
        "+79991234567",
        {"id": 123, "username": None, "first_name": "Иван", "last_name": "Иванов"},
    ) == {
        "phone": "+79991234567",
        "status": "FOUND_NO_USERNAME",
        "telegram_id": "123",
        "username": "",
        "name": "Иван Иванов",
        "telegram_link": "https://t.me/+79991234567?profile",
        "detail": "",
    }


def test_maps_no_contact_response_as_privacy_limited():
    assert map_lookup_result(
        "+79991234567",
        {"error": "No response, the phone number is not on Telegram or has blocked contact adding."},
    )["status"] == "NOT_FOUND_OR_PRIVACY_RESTRICTED"


def test_maps_stopped_deferred_contact_without_claiming_a_result():
    assert map_lookup_result(
        "+79991234567", {"skipped": True, "error": "Проверка отложена пользователем."}
    )["status"] == "DEFERRED_NOT_CHECKED"
