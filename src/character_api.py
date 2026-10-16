"""
캐릭터 팩 고르기 (컨트롤 앱 제어 탭의 '캐릭터' 패널)

GET  /characters         → {"current": id, "characters": [{id, name, lore, voice}]}
POST /characters/select  → config의 character를 바꿔 저장한다. **서버를 다시 시작해야 적용된다** —
                           호출어·프롬프트·핫워드·기억 컬렉션이 모두 서버가 뜰 때 만들어지기 때문이다.
                           컨트롤 앱은 자기가 띄운 서버면 바로 다시 시작한다.
"""
import json
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import character

router = APIRouter()
CONFIG_DIR = os.path.join(character.BASE_DIR, "config")


class SelectRequest(BaseModel):
    id: str


@router.get("/characters")
async def characters():
    return {"current": character.CHARACTER_ID, "name": character.NAME, "characters": character.list_packs()}


def _write_character(path: str, cid: str):
    """그 파일의 character만 바꾼다 — 줄바꿈 형식을 지켜 git diff가 파일 전체가 되지 않게"""
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    raw = json.loads(text)
    raw["character"] = cid
    eol = "\r\n" if "\r\n" in text else "\n"
    with open(path, "w", encoding="utf-8", newline=eol) as f:
        f.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


@router.post("/characters/select")
async def select(inp: SelectRequest):
    ids = {p["id"] for p in character.list_packs()}
    if inp.id not in ids:
        raise HTTPException(status_code=400, detail=f"캐릭터 팩이 없습니다: {inp.id}")
    # private.json이 character를 덮고 있으면 거기를 고친다 — config.json만 고치면 적용되지 않는다
    private = os.path.join(CONFIG_DIR, "private.json")
    target = os.path.join(CONFIG_DIR, "config.json")
    try:
        with open(private, "r", encoding="utf-8") as f:
            if "character" in json.load(f):
                target = private
    except (OSError, json.JSONDecodeError):
        pass
    _write_character(target, inp.id)
    return {"ok": True, "selected": inp.id, "current": character.CHARACTER_ID,
            "restart_required": inp.id != character.CHARACTER_ID}
