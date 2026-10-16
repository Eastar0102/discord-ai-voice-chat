"""
🏰 캐릭터 세계관(자체 설정) 검색 — $MEMORY(사용자 사실)와 분리된 별도 채널.

캐릭터 팩의 lore.txt를 lore_<캐릭터> 컬렉션에 임베딩해두고(scripts/ingest_lore.py),
매 턴 사용자 발화와 의미적으로 관련된 조각만 끌어와 프롬프트의 $LORE에 주입한다.
임베딩 모델은 memory_manager 것을 공유해 중복 로드를 피한다.
"""

from collections import deque
from datetime import datetime

import character
from vector_db import VectorDB

LORE_COLLECTION = f"lore_{character.CHARACTER_ID}"

# 관련도 게이트 — 이 L2 거리보다 멀면 "무관"으로 보고 주입하지 않는다.
# (무관한 잡담에 세계관이 끼어드는 것 방지) ko-sroberta + ChromaDB L2 기준 실측:
# 관련 발화 톱매칭 ~127~157, 명백히 무관한 발화 ~228+. lore.txt를 키우면 조정.
LORE_MAX_DISTANCE = 170.0


class LoreManager:
    def __init__(self):
        # 사용자 기억과 같은 임베딩 모델을 재사용 (모델 중복 로드 방지)
        from memory import memory_manager
        self.vector_db = VectorDB(
            collection_name=LORE_COLLECTION,
            embedding_model=memory_manager.vector_db.embedding_model,
        )
        # 최근 검색 로그 (앱 '상태' 탭에서 "지금 무슨 세계관을 참조하는지" 표시용)
        self.recent_retrievals = deque(maxlen=20)

    def retrieve_relevant_lore(self, query: str, n_results: int = 2,
                               max_distance: float = LORE_MAX_DISTANCE) -> str:
        """사용자 발화와 관련된 세계관 조각을 검색해 텍스트로 반환 (없으면 빈 문자열).
        관련도 게이트(max_distance)로 무관한 발화에는 아무것도 주입하지 않는다."""
        results = self.vector_db.query_memory(query, n_results)
        relevant = [r for r in results if r.get("distance", 1e9) <= max_distance]

        # 후보 전체를 로그에 남김(kept 표시) — 앱 상태 탭에서 게이트 동작을 볼 수 있게
        self.recent_retrievals.appendleft({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "query": query,
            "hits": [{
                "title": r["metadata"].get("title", ""),
                "distance": round(r.get("distance", 0), 1),
                "kept": r.get("distance", 1e9) <= max_distance,
            } for r in results],
        })

        if not relevant:
            return ""
        return "\n\n".join(r["text"].strip() for r in relevant)


# 지연 싱글톤 — 최초 retrieve 시점(memory_manager 생성 이후)에 만든다
_lore_manager = None


def get_lore_manager():
    global _lore_manager
    if _lore_manager is None:
        _lore_manager = LoreManager()
    return _lore_manager
