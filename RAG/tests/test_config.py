import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_app.config import ConfigurationError, load_settings


class SettingsTests(unittest.TestCase):
    # カレントディレクトリと独立した.env読込と既定パス
    def test_dotenv_is_loaded_from_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("APP_PORT=5012\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(root)

        self.assertEqual(settings.app_port, 5012)
        self.assertEqual(settings.documents_dir, (root / "documents").resolve())
        self.assertEqual(settings.qdrant_path, (root / "data" / "qdrant").resolve())

    # 長時間推論用タイムアウトの既定値
    def test_ollama_timeout_defaults_to_360_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(Path(directory))

        self.assertEqual(settings.ollama_timeout_seconds, 360.0)

    # オーバーラップをチャンクサイズ未満に制限する契約
    def test_chunk_overlap_must_be_smaller_than_chunk_size(self):
        with patch.dict(os.environ, {"CHUNK_SIZE": "100", "CHUNK_OVERLAP": "100"}, clear=True):
            with self.assertRaises(ConfigurationError):
                load_settings(Path.cwd())

    # Webサーバーをlocalhost以外へ公開しない制約
    def test_app_host_cannot_bind_to_a_public_interface(self):
        with patch.dict(os.environ, {"APP_HOST": "0.0.0.0"}, clear=True):
            with self.assertRaises(ConfigurationError):
                load_settings(Path.cwd())

    # 無限値・非正値の推論タイムアウト拒否
    def test_ollama_timeout_must_be_finite_and_positive(self):
        for value in ("0", "nan", "inf"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"OLLAMA_TIMEOUT_SECONDS": value}, clear=True):
                    with self.assertRaises(ConfigurationError):
                        load_settings(Path.cwd())


if __name__ == "__main__":
    unittest.main()