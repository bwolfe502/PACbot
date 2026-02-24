"""Tests for _classify_quest_text (actions.py)."""

from actions import _classify_quest_text


class TestClassifyQuestText:
    def test_titan(self):
        assert _classify_quest_text("Defeat Titans") == "titan"
        assert _classify_quest_text("defeat titans") == "titan"
        assert _classify_quest_text("TITAN rally") == "titan"

    def test_eg(self):
        assert _classify_quest_text("Evil Guard") == "eg"
        assert _classify_quest_text("evil guard rally") == "eg"
        assert _classify_quest_text("Guard") == "eg"

    def test_pvp(self):
        assert _classify_quest_text("PvP Battle") == "pvp"
        assert _classify_quest_text("Attack enemies") == "pvp"

    def test_gather(self):
        assert _classify_quest_text("Gather Resources") == "gather"

    def test_fortress(self):
        assert _classify_quest_text("Occupy Fortress") == "fortress"
        assert _classify_quest_text("fortress defense") == "fortress"

    def test_tower(self):
        assert _classify_quest_text("Tower defense") == "tower"

    def test_unknown(self):
        assert _classify_quest_text("something else entirely") is None
        assert _classify_quest_text("") is None
