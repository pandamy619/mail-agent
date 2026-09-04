# -*- coding: utf-8 -*-
"""Тесты клиента Ollama без сети: параметры запроса и выбор модели."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config, llm  # noqa: E402

CFG = """\
llm:
  base_url: "http://localhost:11434/v1"
  model: "qwen3:14b"
  num_ctx: 8192
  keep_alive: "1h"
proactive:
  enabled: true
  model: "qwen3:4b"
  think: true
"""


class ChatPayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "config.yaml"
        path.write_text(CFG, encoding="utf-8")
        self._load = config.load
        config.load = lambda p=None: self._load(path)
        self._post = llm._post
        self.sent = []

        def fake_post(url, payload, timeout=600):
            self.sent.append((url, payload))
            return {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}
        llm._post = fake_post

    def tearDown(self):
        config.load = self._load
        llm._post = self._post
        self.tmp.cleanup()

    def test_default_model_and_options(self):
        llm.chat([{"role": "user", "content": "hi"}])
        url, payload = self.sent[0]
        self.assertEqual(url, "http://localhost:11434/api/chat")
        self.assertEqual(payload["model"], "qwen3:14b")
        self.assertEqual(payload["options"], {"num_ctx": 8192})
        self.assertEqual(payload["keep_alive"], "1h")
        self.assertIs(payload["think"], False)

    def test_model_override_and_think(self):
        llm.chat([{"role": "user", "content": "hi"}], model=llm.proactive_model(),
                 think=llm.proactive_think())
        self.assertEqual(self.sent[0][1]["model"], "qwen3:4b")
        self.assertIs(self.sent[0][1]["think"], True)

    def test_proactive_model_empty_means_chat_model(self):
        path = Path(self.tmp.name) / "config.yaml"
        path.write_text(CFG.replace('model: "qwen3:4b"', 'model: ""'), encoding="utf-8")
        self.assertIsNone(llm.proactive_model())


if __name__ == "__main__":
    unittest.main()
