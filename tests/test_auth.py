"""Tests for Qmemory API token helpers."""
from qmemory.auth import generate_api_token, hash_token, get_token_prefix, verify_token_format


def test_generate_api_token_format():
    token = generate_api_token()
    assert token.startswith("qm_ak_")
    assert len(token) == 38  # "qm_ak_" (6) + 32 hex chars


def test_generate_api_token_unique():
    t1 = generate_api_token()
    t2 = generate_api_token()
    assert t1 != t2


def test_hash_token_deterministic():
    token = "qm_ak_abcdef1234567890abcdef1234567890ab"
    assert hash_token(token) == hash_token(token)


def test_hash_token_not_plaintext():
    token = "qm_ak_abcdef1234567890abcdef1234567890ab"
    assert hash_token(token) != token


def test_get_token_prefix():
    token = "qm_ak_abcdef1234567890abcdef1234567890ab"
    prefix = get_token_prefix(token)
    assert prefix == "qm_ak_abcd"
    assert len(prefix) == 10


def test_verify_token_format_valid():
    token = generate_api_token()
    assert verify_token_format(token) is True


def test_verify_token_format_invalid():
    assert verify_token_format("invalid") is False
    assert verify_token_format("") is False
    assert verify_token_format("qm_ak_short") is False
    assert verify_token_format(None) is False
