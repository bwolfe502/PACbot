"""Tests for startup.get_relay_config() auto-configuration."""

import hashlib
from unittest.mock import patch

from startup import get_relay_config


class TestAutoDerive:
    """When relay is auto-derived from license key."""

    @patch("license.get_license_key", return_value="test-key-123")
    def test_returns_tuple_when_licensed(self, _mock):
        cfg = get_relay_config({"remote_access": True})
        assert cfg is not None
        url, secret, bot = cfg
        assert url
        assert secret
        assert bot

    @patch("license.get_license_key", return_value="test-key-123")
    def test_bot_name_is_sha256_prefix(self, _mock):
        cfg = get_relay_config({})
        expected = hashlib.sha256(b"test-key-123").hexdigest()[:10]
        assert cfg[2] == expected

    @patch("license.get_license_key", return_value="test-key-123")
    def test_bot_name_is_10_hex_chars(self, _mock):
        _, _, bot = get_relay_config({})
        assert len(bot) == 10
        assert all(c in "0123456789abcdef" for c in bot)

    @patch("license.get_license_key", return_value="stable-key")
    def test_same_key_same_bot(self, _mock):
        cfg1 = get_relay_config({})
        cfg2 = get_relay_config({})
        assert cfg1[2] == cfg2[2]

    @patch("license.get_license_key", return_value="key-a")
    def test_different_keys_different_bots(self, _mock):
        bot_a = get_relay_config({})[2]
        with patch("license.get_license_key", return_value="key-b"):
            bot_b = get_relay_config({})[2]
        assert bot_a != bot_b


class TestDisabled:
    """When relay should be disabled."""

    @patch("license.get_license_key", return_value=None)
    def test_no_license_key(self, _mock):
        assert get_relay_config({}) is None

    @patch("license.get_license_key", side_effect=ImportError)
    def test_import_error(self, _mock):
        assert get_relay_config({}) is None

    @patch("license.get_license_key", return_value="valid-key")
    def test_remote_access_false(self, _mock):
        assert get_relay_config({"remote_access": False}) is None


class TestDefaults:
    """Integration with settings DEFAULTS."""

    @patch("license.get_license_key", return_value="fresh-install")
    def test_fresh_defaults_auto_configure(self, _mock):
        from settings import DEFAULTS
        cfg = get_relay_config(dict(DEFAULTS))
        assert cfg is not None
        assert cfg[2] == hashlib.sha256(b"fresh-install").hexdigest()[:10]
