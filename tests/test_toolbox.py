# -*- coding: utf-8 -*-
"""Реестр инструментов: схемы совпадают с эталоном байт в байт, диспетчер."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import toolbox  # noqa: E402
from agent.conversation import Conversation  # noqa: E402

SNAPSHOT = ROOT / "tests" / "data" / "tools_schema.json"


class SchemaTest(unittest.TestCase):
    def test_schemas_match_snapshot(self):
        # промпт с инструментами кэшируется Ollama байт в байт: любое изменение
        # схем — осознанное, с обновлением эталона
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(json.dumps(toolbox.schemas(), ensure_ascii=False, sort_keys=False),
                         json.dumps(expected, ensure_ascii=False, sort_keys=False))

    def test_names_unique_and_ordered(self):
        names = toolbox.names()
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], "list_accounts")
        self.assertEqual([s["function"]["name"] for s in toolbox.schemas()], names)


class DispatchTest(unittest.TestCase):
    def test_unknown_tool(self):
        res = json.loads(toolbox.execute(Conversation(), "no_such_tool", {}))
        self.assertIn("неизвестный инструмент", res["error"])

    def test_cancel_without_pending(self):
        conv = Conversation()
        res = json.loads(toolbox.execute(conv, "cancel_action", None))
        self.assertEqual(res["cancelled"], "заявок не было")

    def test_helpers(self):
        self.assertEqual(toolbox.clamp("7", 10), 7)
        self.assertEqual(toolbox.clamp("x", 10), 10)
        self.assertEqual(toolbox.clamp(99, 10), toolbox.SHOW_CAP)
        self.assertEqual(toolbox.category({"category": "Social"}), "social")
        self.assertIsNone(toolbox.category({"category": "spam"}))
        self.assertEqual(toolbox.ids_list({"ids": "[1, 2]"}), [1, 2])


if __name__ == "__main__":
    unittest.main()
