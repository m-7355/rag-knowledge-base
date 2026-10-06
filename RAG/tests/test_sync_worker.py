"""同期Workerの状態遷移と認可要求をテスト"""

import unittest

from rag_app.application.sync.sync_worker import SyncWorker
from rag_app.domain.errors import GoogleAuthorizationRequired
from rag_app.domain.models import IngestionItemResult, IngestionReport


class FakeState:
    def __init__(self, ready=0):
        self.ready = ready

    def count_ready(self):
        return self.ready

    def list_all(self):
        return []


class FakeSource:
    name = "ローカルフォルダー"

    def owns(self, source_id):
        return True


class FakeIngestion:
    def __init__(self):
        self.calls = []

    def sync_source(self, source, force=False, on_progress=None):
        self.calls.append((source, force))
        if on_progress:
            on_progress(0, 1, "documents/a.pdf")
            on_progress(1, 1, "")
        return IngestionReport([IngestionItemResult("documents/a.pdf", "indexed", 2)])


class SyncWorkerTests(unittest.TestCase):
    def build_worker(self, ready=0, drive_factory=None):
        self.ingestion = FakeIngestion()
        self.state = FakeState(ready)
        return SyncWorker(
            self.ingestion,
            self.state,
            FakeSource(),
            drive_factory,
            interval_seconds=300,
        )

    def test_first_sync_is_initial_setup_until_a_document_is_ready(self):
        worker = self.build_worker()

        self.assertTrue(worker.status().initial_sync)
        self.assertFalse(worker.status().searchable)

        worker.run_once(force=True)

        status = worker.status()
        self.assertEqual(status.state, "idle")
        self.assertFalse(status.initial_sync)
        self.assertFalse(status.searchable)
        self.assertEqual(status.last_summary.indexed, 1)

    def test_drive_authorization_is_reported_without_blocking_local_sync(self):
        def require_authorization(interactive):
            raise GoogleAuthorizationRequired("許可が必要です")

        worker = self.build_worker(ready=2, drive_factory=require_authorization)
        worker.run_once(force=True)

        status = worker.status()
        self.assertEqual(status.drive_state, "auth_required")
        self.assertEqual(len(self.ingestion.calls), 1)
        self.assertEqual(status.state, "idle")
        self.assertTrue(status.searchable)

    def test_status_summary_counts_file_failures(self):
        class FailingIngestion(FakeIngestion):
            def sync_source(self, source, force=False, on_progress=None):
                return IngestionReport(
                    [IngestionItemResult("documents/a.pdf", "failed", message="解析エラー")]
                )

        worker = self.build_worker(ready=1)
        worker._ingestion = FailingIngestion()
        worker.run_once(force=True)

        status = worker.status()
        self.assertEqual(status.last_summary.failed, 1)
        self.assertEqual(status.failures[0].path, "documents/a.pdf")
        self.assertTrue(status.searchable)


if __name__ == "__main__":
    unittest.main()
