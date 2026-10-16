"""
캐릭터 팩의 lore.txt → lore_<캐릭터> 컬렉션 재적재 (config.json의 character).

빈 줄로 구분된 블록을 하나씩 임베딩한다. '#'로 시작하는 줄은 주석으로 무시.
lore.txt를 수정할 때마다 다시 실행해야 검색에 반영된다.

실행:  python scripts/ingest_lore.py
"""

import os
import sys

# src/ 모듈(vector_db) import 경로 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import character  # noqa: E402
from vector_db import VectorDB  # noqa: E402
from lore import LORE_COLLECTION  # noqa: E402

LORE_PATH = character.path("lore.txt")
# 런타임(memory.py)과 같은 저장소를 쓰도록 절대경로로 고정
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "chroma_db"))


def parse_blocks(text: str):
    """빈 줄로 블록 분리, 각 블록에서 주석(#) 줄 제거, 빈 블록 폐기"""
    blocks = []
    for raw in text.split("\n\n"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("#")]
        block = "\n".join(lines).strip()
        if block:
            blocks.append(block)
    return blocks


def main():
    if not os.path.exists(LORE_PATH):
        print(f"No lore file: {LORE_PATH}")
        return
    with open(LORE_PATH, "r", encoding="utf-8") as f:
        blocks = parse_blocks(f.read())

    db = VectorDB(collection_name=LORE_COLLECTION, persist_directory=DATA_DIR)
    db.reset()  # 전체 재적재 — 삭제된 블록이 남지 않게
    for block in blocks:
        # ChromaDB는 비어있지 않은 metadata를 요구 — 블록 제목을 함께 보관
        title = block.splitlines()[0].strip("[] ")
        db.add_memory(block, {"kind": "lore", "title": title})

    print(f"Ingested {len(blocks)} lore blocks into '{LORE_COLLECTION}' (count={db.count()})")
    for i, b in enumerate(blocks, 1):
        head = b.splitlines()[0]
        print(f"  {i}. {head}")


if __name__ == "__main__":
    main()
