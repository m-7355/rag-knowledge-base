import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from app import main
from rag_app.domain.errors import DependencyUnavailable


class AppStartupTests(unittest.TestCase):
    # Qdrantロック失敗時の案内表示と内部トレース非公開
    def test_local_qdrant_lock_prints_guidance_without_traceback(self):
        error_output = io.StringIO()
        with patch("app.load_settings", return_value=object()), patch(
            "app.build_container",
            side_effect=DependencyUnavailable(
                "Qdrant Localは別のプロセスで使用中です。起動中のWebアプリを終了してください。"
            ),
        ), redirect_stderr(error_output):
            exit_code = main()

        self.assertEqual(exit_code, 2)
        self.assertIn("別のプロセスで使用中", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()