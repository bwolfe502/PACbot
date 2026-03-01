"""Tests for _classify_quest_text (actions/quests.py)."""

from actions.quests import _classify_quest_text
from config import QuestType


class TestClassifyQuestText:
    def test_titan(self):
        assert _classify_quest_text("Defeat Titans") == QuestType.TITAN
        assert _classify_quest_text("defeat titans") == QuestType.TITAN
        assert _classify_quest_text("TITAN rally") == QuestType.TITAN

    def test_eg(self):
        assert _classify_quest_text("Evil Guard") == QuestType.EVIL_GUARD
        assert _classify_quest_text("evil guard rally") == QuestType.EVIL_GUARD
        assert _classify_quest_text("Guard") == QuestType.EVIL_GUARD

    def test_pvp(self):
        assert _classify_quest_text("PvP Battle") == QuestType.PVP
        assert _classify_quest_text("Attack enemies") == QuestType.PVP
        assert _classify_quest_text("Defeat the Enemy") == QuestType.PVP
        assert _classify_quest_text("Defeat the Enemv") == QuestType.PVP  # OCR 'y'->'v'

    def test_gather(self):
        assert _classify_quest_text("Gather Resources") == QuestType.GATHER

    def test_fortress(self):
        assert _classify_quest_text("Occupy Fortress") == QuestType.FORTRESS
        assert _classify_quest_text("fortress defense") == QuestType.FORTRESS

    def test_tower(self):
        assert _classify_quest_text("Tower defense") == QuestType.TOWER

    def test_unknown(self):
        assert _classify_quest_text("something else entirely") is None
        assert _classify_quest_text("") is None
