import io
import unittest
from contextlib import redirect_stderr

from rag_app.cli import main


class CliTests(unittest.TestCase):
    # 既存索引の削除前に明示確認を必須とする契約
    def test_rebuild_requires_explicit_confirmation_before_startup(self):
        error_output = io.StringIO()
        with redirect_stderr(error_output):
            with self.assertRaises(SystemExit) as raised:
                main(["rebuild-index"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--confirm", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
