"""Tests for auth utilities — password hashing, HMAC signing, password strength."""

from __future__ import annotations

from pathlib import Path

import pytest

from songmaker_cli.auth import (
    BCRYPT_ROUNDS,
    MIN_PASSWORD_LENGTH,
    RATE_LIMIT_WINDOW_SECONDS,
    ROLE_ADMIN,
    TrustedProxies,
    check_password_strength,
    ensure_session_secret,
    generate_csrf_token,
    get_client_ip,
    hash_password,
    parse_trusted_proxies,
    sign_session_id,
    verify_csrf_token,
    verify_password,
    verify_session_cookie,
)
from songmaker_cli.settings import get_settings

_TEST_SECRET = b"a" * 64


def test_hash_and_verify_password() -> None:
    hashed = hash_password("testpassword123")
    assert hashed != "testpassword123"
    assert verify_password("testpassword123", hashed)


def test_verify_wrong_password() -> None:
    hashed = hash_password("correct-password")
    assert not verify_password("wrong-password", hashed)


def test_hash_produces_different_hashes() -> None:
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2


def test_constants() -> None:
    assert BCRYPT_ROUNDS == 12
    assert ROLE_ADMIN == "admin"
    assert MIN_PASSWORD_LENGTH == 8
    assert RATE_LIMIT_WINDOW_SECONDS == 3600


def test_default_settings_values() -> None:
    settings = get_settings()
    assert settings.login_rate_limit == 5
    assert settings.session_max_age_seconds == 60 * 60 * 24 * 30
    assert settings.session_absolute_max_age_seconds == 60 * 60 * 24 * 90
    assert settings.generation_rate_limit_user == 3
    assert settings.scoring_rate_limit_user == 10
    assert settings.max_queue_depth == 100
    assert settings.max_user_active_jobs == 10
    assert settings.login_lockout_threshold == 15
    assert settings.login_lockout_window_seconds == 3600
    assert settings.max_concurrent_sessions_per_user == 10


# -- HMAC session signing ---------------------------------------------------


def test_sign_and_verify_session() -> None:
    signed = sign_session_id("my-session-token", _TEST_SECRET)
    assert "." in signed
    assert verify_session_cookie(signed, _TEST_SECRET) == "my-session-token"


def test_verify_rejects_tampered_signature() -> None:
    signed = sign_session_id("my-session-token", _TEST_SECRET)
    tampered = signed[:-4] + "XXXX"
    assert verify_session_cookie(tampered, _TEST_SECRET) is None


def test_verify_rejects_no_dot() -> None:
    assert verify_session_cookie("no-dot-here", _TEST_SECRET) is None


def test_verify_rejects_empty_parts() -> None:
    assert verify_session_cookie(".abc", _TEST_SECRET) is None
    assert verify_session_cookie("abc.", _TEST_SECRET) is None


def test_ensure_session_secret_returns_settings_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_SECRET", "c" * 64)
    result = ensure_session_secret(tmp_path)
    assert result == "c" * 64


def test_ensure_session_secret_rejects_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="too short"):
        ensure_session_secret(tmp_path)


# -- Password strength -------------------------------------------------------


def test_common_password_rejected() -> None:
    with pytest.raises(ValueError, match="too common"):
        check_password_strength("password")


def test_low_entropy_rejected() -> None:
    with pytest.raises(ValueError, match="unique characters"):
        check_password_strength("aaaaaaaa")


def test_strong_password_accepted() -> None:
    assert check_password_strength("s3cur3P@ss!") == "s3cur3P@ss!"


# -- CSRF token binding ------------------------------------------------------


def test_generate_csrf_token_deterministic() -> None:
    t1 = generate_csrf_token("session-abc", _TEST_SECRET)
    t2 = generate_csrf_token("session-abc", _TEST_SECRET)
    assert t1 == t2


def test_generate_csrf_token_differs_per_session() -> None:
    t1 = generate_csrf_token("session-1", _TEST_SECRET)
    t2 = generate_csrf_token("session-2", _TEST_SECRET)
    assert t1 != t2


def test_verify_csrf_token_valid() -> None:
    token = generate_csrf_token("my-session", _TEST_SECRET)
    assert verify_csrf_token(token, "my-session", _TEST_SECRET) is True


def test_verify_csrf_token_wrong_session() -> None:
    token = generate_csrf_token("session-a", _TEST_SECRET)
    assert verify_csrf_token(token, "session-b", _TEST_SECRET) is False


def test_verify_csrf_token_forged() -> None:
    assert verify_csrf_token("forged-token", "session-a", _TEST_SECRET) is False


def test_none_password_passes() -> None:
    assert check_password_strength(None) is None


# -- Trusted proxies ---------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "peer", "trusted"),
    [
        ("172.16.0.0/12", "172.18.0.1", True),
        ("172.16.0.0/12", "172.31.255.254", True),
        ("172.16.0.0/12", "10.0.0.1", False),
        ("10.0.0.1", "10.0.0.1", True),
        ("10.0.0.1", "10.0.0.2", False),
        ("10.0.0.1, 172.16.0.0/12", "172.18.0.1", True),
        ("10.0.0.1, 172.16.0.0/12", "10.0.0.1", True),
        ("10.0.0.1, 172.16.0.0/12", "203.0.113.9", False),
        ("172.16.0.0/12", "::ffff:172.18.0.1", True),
        ("2001:db8::/32", "2001:db8::5", True),
        ("2001:db8::/32", "172.18.0.1", False),
        ("172.16.0.0/12", "testclient", False),
        ("", "172.18.0.1", False),
    ],
)
def test_trusted_proxies_membership(configured: str, peer: str, trusted: bool) -> None:
    assert (peer in TrustedProxies.parse(configured)) is trusted


def test_parse_trusted_proxies_reads_configured_networks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.1, 172.16.0.0/12")
    proxies = parse_trusted_proxies()
    assert "10.0.0.1" in proxies
    assert "172.20.3.4" in proxies
    assert "203.0.113.9" not in proxies


def test_parse_trusted_proxies_empty_default_trusts_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    proxies = parse_trusted_proxies()
    assert not proxies
    assert "172.18.0.1" not in proxies


@pytest.mark.parametrize("entry", ["not-an-ip", "10.0.0.0/33", "10.0.0.1/24"])
def test_parse_trusted_proxies_rejects_unparsable_entry(
    monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", entry)
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        parse_trusted_proxies()


# -- get_client_ip -----------------------------------------------------------


def test_get_client_ip_no_trusted_proxies() -> None:
    assert get_client_ip("1.2.3.4", "5.6.7.8, 9.10.11.12", TrustedProxies()) == "1.2.3.4"


def test_get_client_ip_ignores_forwarded_for_from_untrusted_peer() -> None:
    proxies = TrustedProxies.parse("172.16.0.0/12")
    assert get_client_ip("203.0.113.9", "1.2.3.4", proxies) == "203.0.113.9"


def test_get_client_ip_rightmost_untrusted() -> None:
    proxies = TrustedProxies.parse("10.0.0.1")
    result = get_client_ip("10.0.0.1", "1.2.3.4, 5.6.7.8, 10.0.0.1", proxies)
    assert result == "5.6.7.8"


def test_get_client_ip_rightmost_untrusted_behind_proxy_network() -> None:
    proxies = TrustedProxies.parse("172.16.0.0/12")
    result = get_client_ip("172.18.0.1", "203.0.113.7, 172.18.0.9", proxies)
    assert result == "203.0.113.7"


def test_get_client_ip_all_trusted_falls_back() -> None:
    proxies = TrustedProxies.parse("10.0.0.1, 10.0.0.2")
    result = get_client_ip("10.0.0.1", "10.0.0.2, 10.0.0.1", proxies)
    assert result == "10.0.0.1"


def test_get_client_ip_no_xff() -> None:
    proxies = TrustedProxies.parse("10.0.0.1")
    result = get_client_ip("10.0.0.1", None, proxies)
    assert result == "10.0.0.1"
