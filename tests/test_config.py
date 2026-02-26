"""Tests for config setters with non-trivial logic (config.py).

Only tests for functions that do more than simple assignment — e.g.
clamping, multi-field updates. Trivial enable/disable setters are
not tested here (they're just assignment).
"""

import pytest
import config
from config import set_ap_restore_options


class TestSetApRestoreOptions:
    """Tests for AP restore options — the only setter with clamping logic."""

    def test_all_fields_set(self):
        set_ap_restore_options(True, True, True, True, 1000)
        assert config.AP_USE_FREE is True
        assert config.AP_USE_POTIONS is True
        assert config.AP_ALLOW_LARGE_POTIONS is True
        assert config.AP_USE_GEMS is True
        assert config.AP_GEM_LIMIT == 1000

    @pytest.mark.parametrize("input_limit,expected", [
        (9999, 3500),   # clamped high
        (-5, 0),        # clamped negative
        (0, 0),         # zero passthrough
        (1500, 1500),   # normal passthrough
    ])
    def test_gem_limit_clamping(self, input_limit, expected):
        set_ap_restore_options(False, False, False, False, input_limit)
        assert config.AP_GEM_LIMIT == expected
