import unittest
from pathlib import Path


class FrontendSafetyTests(unittest.TestCase):
    # 回答と出典をHTMLとして解釈せず文字列で描画する安全性
    def test_dynamic_answer_and_source_rendering_uses_text_content(self):
        script = Path(__file__).parents[1] / "rag_app" / "static" / "chat.js"
        source = script.read_text(encoding="utf-8")

        self.assertIn("answerText.textContent", source)
        self.assertIn("excerpt.textContent", source)
        self.assertNotIn("innerHTML", source)


if __name__ == "__main__":
    unittest.main()