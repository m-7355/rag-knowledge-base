import unittest

from rag_app.domain.errors import DependencyUnavailable, QuestionValidationError
from rag_app.application.sync.sync_worker import SyncProgress, SyncStatus, SyncSummary
from rag_app.domain.models import AnswerResult, AnswerSource
from rag_app.web.app_factory import create_app


class FakeAnswerService:
    def answer(self, question):
        if question == "invalid":
            raise QuestionValidationError("質問を確認してください。")
        if question == "dependency":
            raise DependencyUnavailable("C:\\private\\local\\error")
        if question == "unexpected":
            raise RuntimeError("C:\\private\\stack trace")
        return AnswerResult(
            answer="30日以内です。[S1]",
            sources=[AnswerSource("S1", "返品規約.pdf", "documents/返品規約.pdf", 4, 0.8, "申請期限は30日です。")],
            abstained=False,
        )


class FakeHealth:
    def __init__(self, status="ok"):
        self.status = status

    def health(self):
        return self.status


class FakeSyncWorker:
    def __init__(self):
        self.value = SyncStatus(
            state="idle",
            drive_state="auth_required",
            drive_message="許可が必要です",
            initial_sync=False,
            searchable=True,
            ready_documents=2,
            progress=SyncProgress(),
            last_synced_at=None,
            last_summary=SyncSummary(),
        )
        self.sync_requests = 0
        self.authorization_requests = 0

    def status(self):
        return self.value

    def request_sync(self):
        self.sync_requests += 1

    def request_drive_authorization(self):
        self.authorization_requests += 1


class WebRouteTests(unittest.TestCase):
    def setUp(self):
        self.sync_worker = FakeSyncWorker()
        self.app = create_app(
            FakeAnswerService(), FakeHealth(), FakeHealth(), "pdf_knowledge_v1", 10,
            sync_worker=self.sync_worker,
        )
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    # 画面表示・質問上限・内部パス非公開の確認
    def test_home_and_health_routes(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('maxlength="10"', home.get_data(as_text=True))
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["collection"], "pdf_knowledge_v1")
        self.assertNotIn("path", str(response.json).lower())

    # 依存サービス停止時のヘルス応答と503状態
    def test_health_reports_unavailable_dependencies(self):
        app = create_app(
            FakeAnswerService(), FakeHealth("unavailable"), FakeHealth(), "collection", 10
        )
        response = app.test_client().get("/api/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["status"], "unavailable")

    # 回答と根拠出典のAPI応答
    def test_chat_returns_grounded_sources(self):
        response = self.client.post("/api/chat", json={"question": "質問"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["sources"][0]["file_name"], "返品規約.pdf")
        self.assertEqual(response.json["sources"][0]["page_number"], 4)

    # 入力・依存エラーのHTTP状態と内部情報保護
    def test_invalid_input_and_dependency_errors_have_safe_statuses(self):
        for payload in ({}, {"question": ""}, {"question": "あ" * 11}):
            response = self.client.post("/api/chat", json=payload)
            self.assertEqual(response.status_code, 400)
        invalid = self.client.post("/api/chat", json={"question": "invalid"})
        self.assertEqual(invalid.status_code, 400)
        dependency = self.client.post("/api/chat", json={"question": "dependency"})
        self.assertEqual(dependency.status_code, 503)
        self.assertNotIn("private", dependency.get_data(as_text=True))

    # 初回同期中は「根拠なし」と誤回答せず、検索APIを一時停止する
    def test_chat_is_blocked_until_initial_index_is_searchable(self):
        self.sync_worker.value = SyncStatus(
            state="syncing",
            drive_state="connected",
            drive_message="",
            initial_sync=True,
            searchable=False,
            ready_documents=0,
            progress=SyncProgress("Google Drive", 4, 10, "manual.pdf"),
            last_synced_at=None,
            last_summary=None,
        )

        response = self.client.post("/api/chat", json={"question": "質問"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("初回同期中", response.json["error"])

    # 同期状態取得・手動同期・Drive認可をWorkerへ委譲する
    def test_sync_routes_report_status_and_enqueue_operations(self):
        status = self.client.get("/api/sync/status")
        sync = self.client.post("/api/sync", json={})
        connect = self.client.post("/api/drive/connect", json={})

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["ready_documents"], 2)
        self.assertEqual(sync.status_code, 202)
        self.assertEqual(connect.status_code, 202)
        self.assertEqual(self.sync_worker.sync_requests, 1)
        self.assertEqual(self.sync_worker.authorization_requests, 1)

    # 出典URLを含む画面で同期状態パネルを一度だけ描画する
    def test_home_includes_one_sync_panel(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(html.count('id="sync-panel"'), 1)
        self.assertIn('id="drive-connect"', html)

    # 予期しない例外の詳細を伏せる安全な応答
    def test_unexpected_errors_do_not_leak_internal_details(self):
        response = self.client.post("/api/chat", json={"question": "unexpected"})

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertIn("request_id", response.json)


if __name__ == "__main__":
    unittest.main()
