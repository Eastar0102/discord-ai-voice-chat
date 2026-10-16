"""
캐릭터 팩 — 이름·성격·세계관·호출어는 코드가 아니라 characters/<id>/ 에 있다.

config.json의 "character"가 팩 이름이다 (기본 "example"). 팩 구성:
    characters/<id>/character.json   이름, 호출어, 핫워드
    characters/<id>/prompt.txt       성격 프롬프트 ($MEMORY · $LORE · $TOPIC 자리 포함)
    characters/<id>/topic.txt        말투 예시 (선택)
    characters/<id>/lore.txt         세계관 — 서버가 뜰 때 바뀌었으면 저절로 다시 임베딩 (선택)

코드 안의 지시문은 NAME(캐릭터 이름)과 OWNER(만든 사람·주인 이름)만 끼워 넣는다.
OWNER는 config의 identity.owner_names 첫 항목이고, 비어 있으면 "너를 만든 사람"이다.

대화 기록·기억은 캐릭터마다 따로 둔다 (data/characters/<id>/, 기억 컬렉션 memory_<id>) —
같이 쓰면 새 캐릭터가 앞 캐릭터의 기억과 말투를 그대로 물려받는다. 사람 목록(people.json)과
지표(metrics.jsonl)는 사람·시스템에 대한 것이라 함께 쓴다.

캐릭터는 서버가 뜰 때 정해진다. 바꾸려면 config의 character를 고치고(POST /characters/select)
서버를 다시 시작한다 — 호출어·프롬프트·핫워드·기억이 모두 이 값으로 만들어진다.
"""
import json
import os

from config_loader import load_config

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config = load_config()

CHARACTER_ID = _config.get("character", "example")
PACK_DIR = os.path.join(BASE_DIR, "characters", CHARACTER_ID)

if not os.path.isdir(PACK_DIR):
    raise FileNotFoundError(f"캐릭터 팩이 없습니다: {PACK_DIR} (config.json의 character 값을 확인하세요)")

with open(os.path.join(PACK_DIR, "character.json"), "r", encoding="utf-8") as _f:
    _pack = json.load(_f)

NAME = _pack.get("name", "Companion")
WAKE_WORDS = [w for w in _pack.get("wake_words", [NAME]) if isinstance(w, str) and w.strip()]
HOTWORDS = _pack.get("hotwords", NAME)

OWNER_NAMES = [n for n in _config.get("identity", {}).get("owner_names", [])
               if isinstance(n, str) and n.strip()]
OWNER = OWNER_NAMES[0] if OWNER_NAMES else "너를 만든 사람"


DATA_DIR = os.path.join(BASE_DIR, "data", "characters", CHARACTER_ID)
os.makedirs(DATA_DIR, exist_ok=True)
MEMORY_COLLECTION = f"memory_{CHARACTER_ID}"


def data_path(name: str) -> str:
    """이 캐릭터 전용 데이터 파일 경로 (data/characters/<id>/<name>)"""
    return os.path.join(DATA_DIR, name)


def list_packs() -> list:
    """characters/ 아래의 팩 목록 [{id, name, lore, voice}]. character.json이 없거나 깨진 폴더는 뺀다"""
    root = os.path.join(BASE_DIR, "characters")
    out = []
    for cid in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, cid)
        try:
            with open(os.path.join(d, "character.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": cid, "name": meta.get("name", cid),
                    "lore": os.path.exists(os.path.join(d, "lore.txt")),
                    "voice": os.path.exists(os.path.join(d, "voice", "voice.json"))})
    return out


def path(name: str) -> str:
    """팩 안의 파일 경로"""
    return os.path.join(PACK_DIR, name)


def read(name: str, default: str = "") -> str:
    """팩 안의 텍스트 파일 (없으면 default)"""
    p = path(name)
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return f.read().strip()
