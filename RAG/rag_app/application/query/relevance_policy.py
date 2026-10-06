"""検索スコアのしきい値判定（根拠不足時にLLMを呼ばず「分からない」と答えるための基準）"""

from __future__ import annotations

from rag_app.domain.models import RetrievedChunk


# 関連度しきい値を一元管理するポリシー
class RelevancePolicy:
    def __init__(self, minimum_score: float):
        self.minimum_score = minimum_score

    def filter(self, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        # しきい値以上の候補だけを残す（Hybrid検索で決めた順位は崩さない）
        return [chunk for chunk in chunks if chunk.score >= self.minimum_score]

    def accept(self, chunks: list[RetrievedChunk]) -> bool:
        # 1件でもしきい値以上の候補があれば回答を試みる
        return bool(self.filter(chunks))
