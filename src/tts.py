# tts.py
"""
🔊 TTS 디스패처
- config의 tts 섹션에서 Provider를 선택하고, 실패 시 폴백 체인을 순회한다
- 캐시(텍스트+스타일+프로바이더 키)와 합성 시간 로깅을 공통 처리
- 파이프라인은 synthesize(text, style) 하나만 호출하면 된다
- set_tts(provider)로 런타임 교체 (컨트롤 패널) — 선택은 config.json에 저장
"""

import array
import os
import hashlib
import wave
import re
import json
import threading
import time
import glob
from io import BytesIO

from config_loader import load_config
from providers.tts import get_provider

config = load_config()
_tts_cfg = config.get("tts", {})
_active = _tts_cfg.get("provider", "edge")
_switch_lock = threading.Lock()

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "config.json")

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "output_wav")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# TTS 캐시 (텍스트+스타일+프로바이더 → 파일 경로)
tts_cache = {}
CACHE_MAX_SIZE = 100

# Provider 인스턴스 (지연 생성)
_providers = {}


def _get_provider(name: str):
    if name not in _providers:
        _providers[name] = get_provider(name, _tts_cfg.get(name, {}))
    return _providers[name]


def _chain() -> list:
    return [_active] + [n for n in _tts_cfg.get("fallback", []) if n != _active]


def _label(name: str) -> str:
    return _tts_cfg.get(name, {}).get("label", name)


def current_tts() -> dict:
    return {"provider": _active, "label": _label(_active)}


def tts_choices() -> list:
    """컨트롤 패널에 보일 엔진 목록 (config tts.choices 순서)"""
    names = _tts_cfg.get("choices") or [_active]
    return [{"provider": n, "label": _label(n)} for n in dict.fromkeys(names + [_active])]


_warm = False    # 기동 워밍업이 끝났는가 (입장 인사가 이걸 기다린다)


def is_warm() -> bool:
    """기동 워밍업이 끝났는지. 실패해도 True가 된다 — 기다릴 이유가 없어졌다는 뜻이다"""
    return _warm


def warmup_current():
    """현재 엔진을 미리 준비 (서버 기동 시). GPT-SoVITS는 별도 서버 실행·모델 로딩에 20초 안팎"""
    global _warm
    provider = _get_provider(_active)
    if not hasattr(provider, "warmup"):
        _warm = True
        return
    try:
        t = time.time()
        provider.warmup()
        print(f"TTS warmup: {_active} ({time.time() - t:.1f}s)")
    except Exception as e:
        print(f"Warning: TTS warmup failed ({_active}): {e}")
    finally:
        _warm = True


def set_tts(provider: str) -> dict:
    """
    런타임 TTS 교체. 새 엔진을 먼저 준비(로컬 서버 실행·모델 로딩)한 뒤 바꿔 끼운다 —
    실패하면 기존 엔진이 그대로 남는다. 이전 엔진이 로컬 서버를 띄웠다면 내려 VRAM을 반환한다.
    """
    global _active
    with _switch_lock:
        if provider not in [c["provider"] for c in tts_choices()]:
            raise ValueError(f"선택할 수 없는 TTS 엔진: {provider}")
        new = _get_provider(provider)
        if hasattr(new, "warmup"):
            new.warmup()
        old_name, _active = _active, provider
        _tts_cfg["provider"] = provider
        if old_name != provider and hasattr(_providers.get(old_name), "release"):
            _providers[old_name].release()
        _persist_selection(provider)
        print(f"TTS switched: {old_name} → {provider}")
        return current_tts()


def _persist_selection(provider: str):
    """config.json의 tts.provider만 갱신 (기존 줄바꿈 유지 — llm._persist_selection과 같은 방식)"""
    with open(CONFIG_PATH, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    raw = json.loads(text)
    raw.setdefault("tts", {})["provider"] = provider
    eol = "\r\n" if "\r\n" in text else "\n"
    with open(CONFIG_PATH, "w", encoding="utf-8", newline=eol) as f:
        f.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


def clear_tts_cache():
    tts_cache.clear()


def cleanup_old_audio_files(keep_count=CACHE_MAX_SIZE):
    """오래된 오디오 파일 정리 — 보관 수는 캐시 크기와 일치시켜야 캐시가 동작함"""
    try:
        audio_files = glob.glob(os.path.join(OUTPUT_DIR, "*.wav"))
        if len(audio_files) <= keep_count:
            return
        audio_files.sort(key=os.path.getmtime, reverse=True)
        for file_path in audio_files[keep_count:]:
            try:
                os.remove(file_path)
            except FileNotFoundError:
                pass  # 동시 정리 중 다른 스레드가 먼저 삭제한 경우 — 정상
            except OSError as e:
                print(f"파일 삭제 실패: {file_path} - {e}")
    except Exception as e:
        print(f"오디오 파일 정리 중 오류: {e}")


# 숫자 사이의 수식 기호는 소리로 읽히지 않는다 — 모델이 "13 × 3 = 39입니다"라고 답한 적이 있다 (09-22)
_MATH_OPS = {"×": " 곱하기 ", "*": " 곱하기 ", "÷": " 나누기 ", "=": " 는 ", "+": " 더하기 ", "%": "퍼센트 "}
_MATH_RE = re.compile(r"(?<=\d)\s*([×*÷=+%])\s*(?=\d|$|\s)")


def read_aloud(text: str) -> str:
    """소리로 읽을 수 없는 기호를 말로 바꾼다"""
    return re.sub(r" {2,}", " ", _MATH_RE.sub(lambda m: _MATH_OPS[m.group(1)], text))


# 합성물 앞뒤에는 무음이 붙어 나온다 (실측 09-24: 앞 0.13초, 뒤 0.32초). 문장을 조각내 이어 붙이면
# 경계마다 0.45초가 비어 말이 끊겨 들린다 — 자연스러운 숨만 남기고 깎는다
MAX_LEAD_SEC = 0.06
MAX_TAIL_SEC = 0.10
SILENCE_LEVEL = 328          # 16bit 기준 약 1% 진폭


def trim_silence(audio_bytes: bytes) -> bytes:
    """WAV 앞뒤 무음을 잘라 낸다 (실패하면 원본 그대로)"""
    try:
        with wave.open(BytesIO(audio_bytes)) as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        if params.sampwidth != 2 or params.nchannels != 1:
            return audio_bytes
        a = array.array("h")
        a.frombytes(frames)
        n = len(a)
        first, last = 0, n - 1
        while first < n and abs(a[first]) < SILENCE_LEVEL:
            first += 1
        while last > first and abs(a[last]) < SILENCE_LEVEL:
            last -= 1
        if first >= last:
            return audio_bytes
        rate = params.framerate
        start = max(0, first - int(MAX_LEAD_SEC * rate))
        end = min(n, last + int(MAX_TAIL_SEC * rate))
        out = BytesIO()
        with wave.open(out, "wb") as w:
            w.setparams(params._replace(nframes=end - start))
            w.writeframes(a[start:end].tobytes())
        return out.getvalue()
    except Exception as e:
        print(f"Warning: 무음 다듬기 실패: {e}")
        return audio_bytes


def synthesize(text: str, style: str = "neutral") -> BytesIO:
    """
    텍스트를 음성으로 합성 (캐시 + 프로바이더 폴백 체인)
    :return: 오디오 BytesIO 또는 None
    """
    text = read_aloud(text)
    chain = _chain()
    primary = chain[0]
    cache_key = f"{primary}_{text}_{style}"

    # 캐시 확인
    if cache_key in tts_cache:
        cached_file = tts_cache[cache_key]
        if os.path.exists(cached_file):
            with open(cached_file, "rb") as f:
                return BytesIO(f.read())
        del tts_cache[cache_key]

    # 캐시 크기 관리 (FIFO)
    if len(tts_cache) >= CACHE_MAX_SIZE:
        del tts_cache[next(iter(tts_cache))]

    # 프로바이더 체인 순회
    for name in chain:
        try:
            t0 = time.time()
            audio_bytes = _get_provider(name).synthesize(text, style)
            if audio_bytes:
                audio_bytes = trim_silence(audio_bytes)
                print(f"[TTS:{name}] {time.time() - t0:.2f}s, {len(audio_bytes)} bytes (Text: {text[:20]}...)")
                # 파일 캐시 저장
                file_path = os.path.join(OUTPUT_DIR, hashlib.md5(audio_bytes).hexdigest() + ".wav")
                try:
                    with open(file_path, "wb") as f:
                        f.write(audio_bytes)
                    tts_cache[cache_key] = file_path
                    cleanup_old_audio_files()
                except OSError:
                    pass
                return BytesIO(audio_bytes)
            print(f"[TTS:{name}] 결과 없음 — 다음 프로바이더 시도")
        except Exception as e:
            print(f"[TTS:{name}] 오류: {e} — 다음 프로바이더 시도")

    print("[TTS] 모든 프로바이더 실패")
    return None
