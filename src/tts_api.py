"""
TTS 엔진 교체 엔드포인트 (컨트롤 패널용).

- GET  /tts/engines : 현재 엔진 + 고를 수 있는 목록 (config tts.choices)
- POST /tts/select  : {provider}로 교체. GPT-SoVITS는 서버 실행·모델 로딩까지 끝난 뒤 응답한다
"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import tts

router = APIRouter(prefix="/tts", tags=["tts"])


class SelectRequest(BaseModel):
    provider: str


@router.get("/engines")
async def tts_engines():
    return {"current": tts.current_tts(), "engines": tts.tts_choices()}


@router.post("/select")
async def tts_select(req: SelectRequest):
    loop = asyncio.get_event_loop()
    try:
        current = await loop.run_in_executor(None, tts.set_tts, req.provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS 교체 실패: {e}")
    return {"current": current}
