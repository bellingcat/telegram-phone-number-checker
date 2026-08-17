import socks
import pytest

from telegram_phone_number_checker import main


def test_parses_socks5_proxy_with_auth():
    assert main.parse_proxy("socks5://user:pass@127.0.0.1:1080") == (
        socks.SOCKS5,
        "127.0.0.1",
        1080,
        True,
        "user",
        "pass",
    )


def test_parses_socks4_proxy_without_auth():
    assert main.parse_proxy("socks4://proxy.example.com:9050") == (
        socks.SOCKS4,
        "proxy.example.com",
        9050,
        True,
        None,
        None,
    )


def test_parses_http_proxy():
    assert main.parse_proxy("http://proxy.example.com:8080") == (
        socks.HTTP,
        "proxy.example.com",
        8080,
        True,
        None,
        None,
    )


def test_rejects_unsupported_scheme():
    with pytest.raises(Exception, match="Unsupported proxy scheme"):
        main.parse_proxy("ftp://proxy.example.com:21")


def test_rejects_missing_port():
    with pytest.raises(Exception, match="host and port"):
        main.parse_proxy("socks5://proxy.example.com")
