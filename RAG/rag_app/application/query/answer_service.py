"""検索・同期状態照合・棄却判定・再順位付けを通した根拠付き回答生成

Queryパイプライン:
    質問の検証
      ↓
    Hybrid Retrieval（ベクトル + キーワード）… 候補 RETRIEVAL_CANDIDATES 件
      ↓
    同期状態との照合 … 削除済み・更新途中・失敗した文書を根拠にしない
      ↓
    関連度しきい値 … 根拠がなければLLMを呼ばず「確認できません」と返す
      ↓
    Rerank → 上位 TOP_K 件
      ↓
    Context構築 → LLM → 参照ID検証 → 回答＋出典
"""

from __future__ import annotations

import unicodedata
from typing import Callable

from rag_app.application.query.hybrid_retriever import HybridRetriever
from rag_app.application.query.prompt_builder import PromptBuilder
from rag_app.application.query.relevance_policy import RelevancePolicy
from rag_app.domain.errors import DependencyUnavailable, QuestionValidationError
from rag_app.domain.models import AnswerResult, AnswerSource, RetrievedChunk
from rag_app.ports.interfaces import ChatModel, Reranker, SyncStateRepository


ABSTENTION_TEXT = "書類フォルダの資料から回答根拠を確認できませんでした。"
# システムプロンプトでLLMに指示している「分からない」ときの回答
MODEL_ABSTENTION_PHRASE = "資料から確認できません"


# 検索から根拠付き回答までを調整するアプリケーションサービス
class RagAnswerService:
    def __init__(
        self,
        retriever: HybridRetriever,
        sync_state: SyncStateRepository,
        relevance_policy: RelevancePolicy,
        prompt_builder: PromptBuilder,
        chat_model: ChatModel,
        embedding_model: str,
        top_k: int,
        max_context_chars: int,
        max_question_chars: int,
        reranker: Reranker | None = None,
        source_url_resolver: Callable[[str], str | None] | None = None,
    ):
        self._retriever = retriever
        self._sync_state = sync_state
        self._relevance_policy = relevance_policy
        self._prompt_builder = prompt_builder
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._top_k = top_k
        self._max_context_chars = max_context_chars
        self._max_question_chars = max_question_chars
        self._reranker = reranker
        # ソースIDから元ファイルを開くURLを求める関数（Drive文書など）
        self._source_url_resolver = source_url_resolver or (lambda source_id: None)

    def answer(self, question: str) -> AnswerResult:
        # 質問検証から検索・根拠付き回答生成までの一連の処理
        normalized_question = self._validate_question(question)
        candidates = self._retriever.retrieve(normalized_question)
        if not candidates:
            return self._abstain("no_results")

        active = []
        # ready状態・ハッシュ・モデル・パスが一致する候補だけを採用
        for candidate in candidates:
            chunk = candidate.chunk
            entry = self._sync_state.get(chunk.source_id)
            if (
                entry is not None
                and entry.status == "ready"
                and entry.file_hash == chunk.file_hash
                and entry.embedding_model == self._embedding_model
                and entry.relative_path == chunk.relative_path
            ):
                active.append(candidate)
        if not active:
            return self._abstain("all_candidates_inactive")

        relevant = self._relevance_policy.filter(active)
        if not relevant:
            return self._abstain("below_threshold")
        # 関連度の高い候補だけを並べ替え、LLMへ渡す件数を絞る
        selected = self._rerank(normalized_question, relevant)[: self._top_k]

        prompt = self._prompt_builder.build(
            normalized_question,
            selected,
            self._max_context_chars,
        )
        if not prompt.contexts:
            return self._abstain("context_too_large")

        response = self._chat_model.generate(prompt.messages).strip()
        # プロンプトに含めた参照IDだけを回答に残す検証
        allowed_ids = {context.reference_id for context in prompt.contexts}
        answer = self._prompt_builder.validate_references(response, allowed_ids)
        if not answer:
            raise DependencyUnavailable("Ollamaから空の回答が返されました。")
        # LLM自身が「資料から確認できない」と判断した場合は、無関係な出典を表示しない
        if answer.startswith(MODEL_ABSTENTION_PHRASE):
            return self._abstain("model_abstained")

        sources = [
            AnswerSource(
                reference_id=context.reference_id,
                file_name=context.retrieved.chunk.file_name,
                relative_path=context.retrieved.chunk.relative_path,
                page_number=context.retrieved.chunk.page_number,
                score=context.retrieved.score,
                excerpt=context.excerpt,
                url=self._source_url_resolver(context.retrieved.chunk.source_id),
            )
            for context in prompt.contexts
        ]
        return AnswerResult(answer=answer, sources=sources, abstained=False)

    def _rerank(self, question: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
        # Reranker未設定時はHybrid検索の順位をそのまま使う
        if self._reranker is None:
            return candidates
        return self._reranker.rerank(question, candidates)

    def _validate_question(self, question: str) -> str:
        # 質問の型・空白・長さ・制御文字に関する入力検証
        if not isinstance(question, str):
            raise QuestionValidationError("質問は文字列で指定してください。")
        normalized = question.strip()
        if not normalized:
            raise QuestionValidationError("質問を入力してください。")
        if len(normalized) > self._max_question_chars:
            raise QuestionValidationError(
                f"質問は{self._max_question_chars}文字以内で入力してください。"
            )
        if any(unicodedata.category(character) == "Cc" for character in normalized):
            raise QuestionValidationError("質問に制御文字は使用できません。")
        return normalized

    @staticmethod
    def _abstain(reason: str) -> AnswerResult:
        # 根拠不足時にモデルを呼ばず返す共通結果
        return AnswerResult(ABSTENTION_TEXT, [], True, reason)
