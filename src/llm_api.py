"""
LLM 모델 교체 엔드포인트 (컨트롤 패널용).

- GET  /llm/models : 현재 모델 + 고를 수 있는 목록 (OpenAI는 config의 choices,
                     Ollama는 설치된 모델을 실시간 조회 — ollama pull만 하면 목록에 뜬다)
- POST /llm/select : {provider, model}로 교체. 로컬 모델은 VRAM 로딩까지 끝난 뒤 응답한다
"""

import asyncio

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import llm
from config_loader import load_config
from providers.llm import ollama

router = APIRouter(prefix="/llm", tags=["llm"])


class SelectRequest(BaseModel):
    provider: str
    model: str


@router.get("/models")
async def llm_models():
    llm_cfg = load_config().get("llm", {})
    openai_cfg = llm_cfg.get("openai_chat", {})
    openai_models = list(dict.fromkeys(openai_cfg.get("choices", []) + [openai_cfg.get("model", "gpt-4o-mini")]))

    ollama_cfg = llm_cfg.get("ollama", {})
    ollama_url = ollama_cfg.get("base_url", ollama.DEFAULT_URL)
    ollama_info = {"available": True, "error": None, "models": []}
    try:
        loop = asyncio.get_event_loop()
        ollama_info["models"] = await loop.run_in_executor(None, ollama.list_models, ollama_url)
    except httpx.HTTPError as e:
        ollama_info.update(available=False, error=f"Ollama에 연결할 수 없음 ({ollama_url}): {e}")
        # 꺼져 있으면 설치 목록을 못 읽는다 — 마지막으로 쓴 로컬 모델을 대신 내보내
        # 선택(→ /llm/select가 Ollama 자동 실행)할 수 있게 한다
        if ollama_cfg.get("model"):
            ollama_info["models"] = [{"model": ollama_cfg["model"], "offline": True}]

    # Claude는 설치 목록이 없으니 config의 choices를 그대로 (키가 없으면 고를 수 없게 표시)
    claude_cfg = llm_cfg.get("claude", {})
    claude_models = list(dict.fromkeys(
        claude_cfg.get("choices", ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"])
        + [claude_cfg.get("model", "claude-opus-5")]))
    has_key = bool(claude_cfg.get("api_key"))

    # llama.cpp는 설치 목록 API가 없다 — config의 models(이름 → GGUF)에 적힌 것 중 파일이 있는 것만
    lc = llm_cfg.get("llamacpp", {})
    import os
    lc_models = [{"model": name, "missing": not os.path.exists(path or "")}
                 for name, path in (lc.get("models") or {}).items()]
    lc_ok = bool(lc.get("server")) and os.path.exists(lc.get("server") or "")

    return {
        "current": llm.current_llm(),
        "llamacpp": {"available": lc_ok, "models": lc_models,
                     "error": None if lc_ok else f"llama-server가 없습니다: {lc.get('server')}"},
        "openai_chat": {"models": [{"model": m} for m in openai_models]},
        "ollama": ollama_info,
        "claude": {"available": has_key, "models": [{"model": m} for m in claude_models],
                   "error": None if has_key else ".env에 ANTHROPIC_API_KEY가 없습니다"},
    }


@router.post("/select")
async def llm_select(req: SelectRequest):
    loop = asyncio.get_event_loop()
    try:
        current = await loop.run_in_executor(None, llm.set_llm, req.provider, req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"모델 교체 실패: {e}")
    return {"current": current}
