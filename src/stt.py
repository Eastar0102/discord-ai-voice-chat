"""
🧠 STT 진입점 (얇은 래퍼)
- config의 stt 섹션에서 Provider를 선택해 위임한다
- 엔진 교체는 config/config.json의 stt.provider 수정만으로 가능
- 실제 구현은 providers/stt/ 아래에 있다 (현재: whisper_local)
"""

import numpy as np

from config_loader import load_config
from providers.stt import get_provider

_cfg = load_config().get("stt", {})
_name = _cfg.get("provider", "whisper_local")
_opts = dict(_cfg.get(_name, {}))
if not _opts.get("hotwords"):
    import character
    _opts["hotwords"] = character.HOTWORDS      # 비어 있으면 캐릭터 이름을 인식 힌트로
_provider = get_provider(_name, _opts)


def transcribe(audio_np: np.ndarray) -> str:
    """numpy 오디오 배열(16kHz 단일 채널) → 인식 텍스트. 무음/저신뢰는 빈 문자열."""
    return _provider.transcribe(audio_np)


def set_hotword_names(names):
    """채널에 있는 사람 이름을 STT에 알려 준다 — 이름이 뭉개져 들리는 것을 줄인다.
    provider가 지원하지 않으면 조용히 무시한다"""
    prov = _provider
    if hasattr(prov, "set_names"):
        try:
            return prov.set_names(names)
        except Exception as e:
            print(f"[STT] hotwords 갱신 실패: {e}")
    return None


def last_stats() -> dict:
    """마지막 전사의 확신도 (avg_logprob, no_speech_prob). 없으면 빈 dict"""
    return dict(getattr(_provider, "last_stats", {}) or {})
