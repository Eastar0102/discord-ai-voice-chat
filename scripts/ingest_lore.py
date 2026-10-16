"""
캐릭터 팩의 lore.txt → lore_<캐릭터> 컬렉션 재적재 (config.json의 character).

서버가 뜰 때 lore.txt가 바뀌었으면 저절로 다시 넣으므로 보통은 필요 없다. 서버를 끈 채로
바로 확인하고 싶을 때 쓴다 — 서버가 켜져 있으면 같은 저장소를 두 프로세스가 열게 되니 끄고 실행한다.

빈 줄로 구분된 블록을 하나씩 임베딩한다. '#'로 시작하는 줄은 주석으로 무시.

실행:  python scripts/ingest_lore.py
"""

import os
import sys

# src/ 모듈 import 경로 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from vector_db import VectorDB  # noqa: E402
from lore import LORE_COLLECTION, LORE_PATH, ingest  # noqa: E402

# 런타임(memory.py)과 같은 저장소를 쓰도록 절대경로로 고정
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "chroma_db"))


def main():
    db = VectorDB(collection_name=LORE_COLLECTION, persist_directory=DATA_DIR)
    n = ingest(db)
    if not os.path.exists(LORE_PATH):
        print(f"No lore file: {LORE_PATH} (collection cleared)")
        return
    print(f"Ingested {n} lore blocks into '{LORE_COLLECTION}' (count={db.count()})")


if __name__ == "__main__":
    main()
