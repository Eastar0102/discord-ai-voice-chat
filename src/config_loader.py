"""
환경변수와 설정 파일을 로드하는 모듈
"""
import os
import json
from typing import Dict, Any
from dotenv import load_dotenv

# .env 파일 로드 (프로젝트 루트의 .env 파일을 명시적으로 로드)
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(env_path)

def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if k.startswith("_"):
            continue            # 설명용 키는 합치지 않는다
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config() -> Dict[str, Any]:
    """
    환경변수와 config.json을 결합하여 설정을 로드
    """
    # config.json 로드 (절대 경로 사용)
    import os
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    # config/private.json(버전 관리 제외)을 위에 겹친다 — 이름·문서 제목·구글 폴더 ID처럼
    # 저장소에 남기면 안 되는 값을 둔다. 딕셔너리는 안쪽까지 합치고, 나머지는 덮어쓴다
    private_path = os.path.join(os.path.dirname(config_path), "private.json")
    if os.path.exists(private_path):
        with open(private_path, "r", encoding="utf-8") as f:
            _merge(config, json.load(f))

    # 환경변수에서 API 키 로드
    # config["openai"]는 공유 OpenAI 자격증명/요약 모델용 (memory.py가 사용)
    config.setdefault("openai", {})["api_key"] = os.getenv("OPENAI_API_KEY")
    # Claude(Anthropic) — 키가 없으면 SDK가 ant 로그인 프로필 등 다른 경로로 찾는다
    config.setdefault("llm", {}).setdefault("claude", {})["api_key"] = os.getenv("ANTHROPIC_API_KEY")

    # LLM provider가 자급자족하도록 선택된 OpenAI 계열 provider 옵션에도 키 주입
    llm_cfg = config.get("llm", {})
    for _name, _opts in llm_cfg.items():
        if isinstance(_opts, dict) and _name.startswith("openai"):
            _opts.setdefault("api_key", config["openai"]["api_key"])
    
    # 환경변수에서 오디오 장치 설정 로드 (선택사항)
    if os.getenv("AUDIO_INPUT_DEVICE"):
        config.setdefault("audio", {})["device"] = int(os.getenv("AUDIO_INPUT_DEVICE"))
    if os.getenv("AUDIO_OUTPUT_DEVICE"):
        config["audio"]["output_device"] = int(os.getenv("AUDIO_OUTPUT_DEVICE"))
    if os.getenv("AUDIO_CABLE_OUTPUT"):
        config["audio"]["cable_output"] = int(os.getenv("AUDIO_CABLE_OUTPUT"))
    
    # API 키는 고른 provider가 필요로 할 때만 필요하다 (로컬 Ollama·llama.cpp만 쓰면 없어도 된다)
    return config

def load_discord_config() -> Dict[str, str]:
    """
    Discord 봇 설정 로드
    """
    # Discord 봇 토큰을 환경변수에서 로드
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        # 환경변수가 없으면 config.json에서 로드 (하위 호환성)
        with open("discord_bot/config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        token = config.get("token")
    
    if not token or token == "your_discord_token_here":
        raise ValueError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")
    
    return {"token": token}
