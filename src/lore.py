"""
🏰 캐릭터 세계관(자체 설정) 검색 — $MEMORY(사용자 사실)와 분리된 별도 채널.

캐릭터 팩의 lore.txt를 lore_<캐릭터> 컬렉션에 임베딩해두고, 매 턴 사용자 발화와 의미적으로
관련된 조각만 끌어와 프롬프트의 $LORE에 주입한다. lore.txt가 바뀌었으면(또는 컬렉션이 비었으면)
서버가 뜰 때 저절로 다시 넣는다 — 캐릭터를 바꾸거나 파일을 고친 뒤 스크립트를 잊어도 된다.
임베딩 모델은 memory_manager 것을 공유해 중복 로드를 피한다.
"""

import os
from collections import deque
from datetime import datetime

import character
from vector_db import VectorDB

LORE_COLLECTION = f"lore_{character.CHARACTER_ID}"
LORE_PATH = character.path("lore.txt")
_SYNC_MARK = character.data_path("lore_mtime.txt")   # 마지막으로 넣은 lore.txt의 수정 시각

# 관련도 게이트 — 이 L2 거리보다 멀면 "무관"으로 보고 주입하지 않는다.
# (무관한 잡담에 세계관이 끼어드는 것 방지) ko-sroberta + ChromaDB L2 기준 실측:
# 관련 발화 톱매칭 ~127~157, 명백히 무관한 발화 ~228+. lore.txt를 키우면 조정.
LORE_MAX_DISTANCE = 170.0


def parse_blocks(text: str) -> list:
    """빈 줄로 블록 분리, 각 블록에서 주석(#) 줄 제거, 빈 블록 폐기"""
    blocks = []
    for raw in text.split("\n\n"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("#")]
        block = "\n".join(lines).strip()
        if block:
            blocks.append(block)
    return blocks


def ingest(db: VectorDB) -> int:
    """lore.txt 전체를 다시 넣는다 (지운 블록이 남지 않게 비우고 시작). 넣은 블록 수"""
    db.reset()
    if not os.path.exists(LORE_PATH):
        return 0
    with open(LORE_PATH, "r", encoding="utf-8") as f:
        blocks = parse_blocks(f.read())
    for block in blocks:
        # ChromaDB는 비어있지 않은 metadata를 요구 — 블록 제목을 함께 보관
        db.add_memory(block, {"kind": "lore", "title": block.splitlines()[0].strip("[] ")})
    try:
        with open(_SYNC_MARK, "w", encoding="utf-8") as f:
            f.write(str(os.path.getmtime(LORE_PATH)))
    except OSError:
        pass
    return len(blocks)


def _stale(db: VectorDB) -> bool:
    if not os.path.exists(LORE_PATH):
        return db.count() > 0                # 파일을 지웠으면 컬렉션도 비운다
    try:
        with open(_SYNC_MARK, "r", encoding="utf-8") as f:
            synced = f.read().strip()
    except OSError:
        synced = ""
    return db.count() == 0 or synced != str(os.path.getmtime(LORE_PATH))


class LoreManager:
    def __init__(self):
        # 사용자 기억과 같은 임베딩 모델을 재사용 (모델 중복 로드 방지)
        from memory import memory_manager
        self.vector_db = VectorDB(
            collection_name=LORE_COLLECTION,
            embedding_model=memory_manager.vector_db.embedding_model,
        )
        if _stale(self.vector_db):
            n = ingest(self.vector_db)
            print(f"[Lore] lore.txt를 다시 넣었습니다 ({n}블록 → {LORE_COLLECTION})")
        # 최근 검색 로그 (앱 '상태' 탭에서 "지금 무슨 세계관을 참조하는지" 표시용)
        self.recent_retrievals = deque(maxlen=20)

    def retrieve_relevant_lore(self, query: str, n_results: int = 2,
                               max_distance: float = LORE_MAX_DISTANCE) -> str:
        """사용자 발화와 관련된 세계관 조각을 검색해 텍스트로 반환 (없으면 빈 문자열).
        관련도 게이트(max_distance)로 무관한 발화에는 아무것도 주입하지 않는다."""
        if self.vector_db.count() == 0:
            return ""
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
