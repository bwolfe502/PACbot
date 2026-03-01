"""Tests for device token generation and validation (startup.py)."""

import hashlib
from unittest.mock import patch

from startup import (device_hash, generate_device_token,
                     generate_device_ro_token, validate_device_token)


class TestDeviceHash:
    """device_hash() produces stable, short, URL-safe hashes."""

    def test_returns_8_hex_chars(self):
        h = device_hash("127.0.0.1:5555")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        assert device_hash("127.0.0.1:5555") == device_hash("127.0.0.1:5555")

    def test_different_devices_different_hashes(self):
        assert device_hash("127.0.0.1:5555") != device_hash("127.0.0.1:5556")

    def test_matches_raw_sha256(self):
        device_id = "127.0.0.1:9999"
        expected = hashlib.sha256(device_id.encode()).hexdigest()[:8]
        assert device_hash(device_id) == expected


class TestGenerateDeviceToken:
    """generate_device_token() produces deterministic per-device tokens."""

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_returns_16_hex_chars(self, _mock):
        token = generate_device_token("127.0.0.1:5555")
        assert len(token) == 16
        assert all(c in "0123456789abcdef" for c in token)

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_deterministic(self, _mock):
        t1 = generate_device_token("127.0.0.1:5555")
        t2 = generate_device_token("127.0.0.1:5555")
        assert t1 == t2

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_different_devices_different_tokens(self, _mock):
        t1 = generate_device_token("127.0.0.1:5555")
        t2 = generate_device_token("127.0.0.1:5556")
        assert t1 != t2

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_matches_raw_sha256(self, _mock):
        device_id = "127.0.0.1:9999"
        expected = hashlib.sha256(f"test-key-abc:{device_id}".encode()).hexdigest()[:16]
        assert generate_device_token(device_id) == expected

    @patch("license.get_license_key", return_value=None)
    def test_returns_none_without_license(self, _mock):
        assert generate_device_token("127.0.0.1:5555") is None

    @patch("license.get_license_key", side_effect=Exception("no license module"))
    def test_returns_none_on_exception(self, _mock):
        assert generate_device_token("127.0.0.1:5555") is None


class TestGenerateDeviceRoToken:
    """generate_device_ro_token() produces deterministic read-only tokens."""

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_returns_16_hex_chars(self, _mock):
        token = generate_device_ro_token("127.0.0.1:5555")
        assert len(token) == 16
        assert all(c in "0123456789abcdef" for c in token)

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_different_from_full_token(self, _mock):
        full = generate_device_token("127.0.0.1:5555")
        ro = generate_device_ro_token("127.0.0.1:5555")
        assert full != ro

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_deterministic(self, _mock):
        t1 = generate_device_ro_token("127.0.0.1:5555")
        t2 = generate_device_ro_token("127.0.0.1:5555")
        assert t1 == t2

    @patch("license.get_license_key", return_value=None)
    def test_returns_none_without_license(self, _mock):
        assert generate_device_ro_token("127.0.0.1:5555") is None


class TestValidateDeviceToken:
    """validate_device_token() returns access level or None."""

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_full_token_returns_full(self, _mock):
        device_id = "127.0.0.1:5555"
        token = generate_device_token(device_id)
        assert validate_device_token(device_id, token) == "full"

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_ro_token_returns_readonly(self, _mock):
        device_id = "127.0.0.1:5555"
        token = generate_device_ro_token(device_id)
        assert validate_device_token(device_id, token) == "readonly"

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_wrong_token_returns_none(self, _mock):
        assert validate_device_token("127.0.0.1:5555", "0000000000000000") is None

    @patch("license.get_license_key", return_value="test-key-abc")
    def test_wrong_device_returns_none(self, _mock):
        token = generate_device_token("127.0.0.1:5555")
        assert validate_device_token("127.0.0.1:5556", token) is None

    @patch("license.get_license_key", return_value=None)
    def test_no_license_returns_none(self, _mock):
        assert validate_device_token("127.0.0.1:5555", "anything") is None
