"""
전사(스크립트) 모드 전용 STT 엔드포인트.

어시스턴트 경로(WebSocket /ws)와 완전히 분리된 두 번째 Whisper 인스턴스를 쓴다.
- 목적: 여러 사람이 동시에 말하는 회의/잡담을 화자별로 받아 적는 것 (Scripty류)
- 분리 이유: 어시스턴트용 모델 락을 공유하면 전사 요청이 몰릴 때 캐릭터 응답이
  같이 밀린다. 대신 VRAM을 추가로 쓰므로 세션 시작 시 로딩하고,
  세션이 끝나거나 유휴 시간이 지나면 내려서 반환한다.

전사는 실시간 응답이 아니라 기록이므로 무게를 고를 수 있다 — config의
transcribe.presets(fast / balanced / accurate / max / cpu)에서 선택하고,
세션마다 /transcribe/select로 덮어쓴다. 프리셋을 바꾸면 기존 인스턴스를
내리고 새로 로딩한다 (VRAM에 두 모델을 동시에 두지 않는다).

Node(전사 핸들러)는 발화가 끝난 덩어리를 그대로 POST 한다. 48kHz 스테레오 PCM을
보내면 여기서 16kHz 모노로 변환한다 — Node 쪽에 리샘플러를 두지 않기 위함.
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request

from audio_utils import duration_sec, pcm_to_16k_mono
from config_loader import load_config

router = APIRouter(prefix="/transcribe", tags=["transcribe"])

# 프리셋 정의에만 쓰이고 STT Provider에는 넘기지 않는 메타 키
_META_KEYS = {"label", "vram_mb", "provider", "timeout_sec", "_comment"}


def _cfg() -> dict:
    return (load_config() or {}).get("transcribe", {}) or {}


def _presets(cfg: dict = None) -> dict:
    return (cfg or _cfg()).get("presets", {}) or {}


def resolve_preset(name: str = None) -> tuple:
    """
    프리셋 이름 → (이름, provider, provider에 넘길 options, 이 프리셋의 메타).
    defaults(공통 게이트) 위에 프리셋 값을 덮어쓴다.
    """
    cfg = _cfg()
    presets = _presets(cfg)
    name = name or cfg.get("preset") or next(iter(presets), None)

    if not presets:
        raise HTTPException(status_code=500, detail="config에 transcribe.presets가 없습니다")
    if name not in presets:
        raise HTTPException(
            status_code=404,
            detail=f"알 수 없는 프리셋: {name} (사용 가능: {', '.join(presets)})")

    preset = presets[name] or {}
    options = {k: v for k, v in (cfg.get("defaults") or {}).items()
               if not k.startswith("_")}
    options.update({k: v for k, v in preset.items() if k not in _META_KEYS})

    provider = preset.get("provider") or cfg.get("provider", "whisper_local")
    meta = {
        "preset": name,
        "label": preset.get("label", name),
        "model": preset.get("model"),
        "device": preset.get("device"),
        "compute_type": preset.get("compute_type"),
        "vram_mb": preset.get("vram_mb"),
        # 무거운 프리셋은 자기 타임아웃을 가질 수 있다 (CPU/max)
        "timeout_sec": preset.get("timeout_sec", cfg.get("timeout_sec", 120)),
    }
    return name, provider, options, meta


class TranscribeEngine:
    """전사 전용 STT 인스턴스 — 프리셋 선택 + 지연 로딩 + 직렬화 + 유휴 해제."""

    def __init__(self):
        self._provider = None
        self._load_lock = asyncio.Lock()
        self._run_sem = None
        self._pending = 0
        self._last_used = 0.0
        self._idle_task = None
        self._selected = None   # 활성 프리셋 이름 (None이면 config 기본값)
        self._meta = {}         # 로딩된 인스턴스의 메타 (모델/디바이스/타임아웃)
        self._load_sec = None

    @property
    def loaded(self) -> bool:
        return self._provider is not None

    async def select(self, preset: str = None, preload: bool = True) -> dict:
        """
        사용할 프리셋 지정. 이미 다른 프리셋이 로딩돼 있으면 내리고 교체한다.
        preload=True면 지금 로딩해서 첫 발화가 모델 로딩을 기다리지 않게 한다.
        """
        name, _provider, _options, meta = resolve_preset(preset)

        if self.loaded and self._meta.get("preset") != name:
            print(f"[Transcribe] 프리셋 교체: {self._meta.get('preset')} → {name}")
            await self.release()

        self._selected = name
        if preload:
            _free_assistant_vram()
            await self._ensure_loaded()
        return self.status()

    async def _ensure_loaded(self):  # noqa: E301 (아래 헬퍼와 분리)
        if self._provider is not None:
            return self._provider

        async with self._load_lock:
            if self._provider is not None:
                return self._provider

            name, provider_name, options, meta = resolve_preset(self._selected)

            from providers.stt import get_provider
            print(f"[Transcribe] 전용 STT 로딩: [{name}] {meta['label']} "
                  f"— {meta['model']} ({meta['device']}/{meta['compute_type']})")
            started = time.time()
            loop = asyncio.get_event_loop()
            # 모델 로딩은 수 초~수십 초 블로킹 — 이벤트 루프를 막지 않도록 스레드에서
            self._provider = await loop.run_in_executor(
                None, lambda: get_provider(provider_name, options))
            self._load_sec = round(time.time() - started, 1)
            self._meta = meta
            self._run_sem = asyncio.Semaphore(int(_cfg().get("max_concurrency", 1)))
            self._last_used = time.time()
            print(f"[Transcribe] 로딩 완료 ({self._load_sec}초)")

            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._idle_watchdog())

            return self._provider

    async def _idle_watchdog(self):
        """유휴 시간이 지나면 모델을 내려 VRAM 반환 (게임 중 VRAM 경합 완화)."""
        while True:
            await asyncio.sleep(30)
            idle_limit = int(_cfg().get("idle_unload_sec", 600))
            if idle_limit <= 0 or self._provider is None:
                continue
            if self._pending == 0 and time.time() - self._last_used > idle_limit:
                print(f"[Transcribe] {idle_limit}초 유휴 — 전용 STT 인스턴스 해제")
                await self.release()
                return

    async def transcribe(self, audio_16k) -> str:
        cfg = _cfg()
        max_pending = int(cfg.get("max_pending", 60))
        if self._pending >= max_pending:
            # 백로그가 쌓이면 늦게 도착한 전사는 가치가 없다 — 거절하고 흘려보낸다
            raise HTTPException(status_code=503, detail="transcribe backlog full")

        provider = await self._ensure_loaded()
        self._pending += 1
        try:
            async with self._run_sem:
                loop = asyncio.get_event_loop()
                timeout = float(self._meta.get("timeout_sec") or cfg.get("timeout_sec", 120))
                try:
                    return await asyncio.wait_for(
                        loop.run_in_executor(None, provider.transcribe, audio_16k),
                        timeout=timeout)
                except asyncio.TimeoutError:
                    print(f"[Transcribe] STT 타임아웃 ({timeout}초) — 건너뜀")
                    return ""
        finally:
            self._pending -= 1
            self._last_used = time.time()

    async def release(self):
        async with self._load_lock:
            provider, self._provider = self._provider, None
            self._meta = {}
            self._load_sec = None
            if provider is None:
                return False
            loop = asyncio.get_event_loop()
            release = getattr(provider, "release", None)
            if release:
                await loop.run_in_executor(None, release)
            return True

    def status(self) -> dict:
        cfg = _cfg()
        # 로딩 전이면 "지금 선택된 프리셋"의 정의를 보여준다
        active = self._meta
        if not active:
            try:
                _n, _p, _o, active = resolve_preset(self._selected)
            except HTTPException:
                active = {}
        return {
            "loaded": self.loaded,
            "preset": active.get("preset"),
            "label": active.get("label"),
            "model": active.get("model"),
            "device": active.get("device"),
            "compute_type": active.get("compute_type"),
            "vram_mb": active.get("vram_mb"),
            "timeout_sec": active.get("timeout_sec"),
            "load_sec": self._load_sec,
            "pending": self._pending,
            "default_preset": cfg.get("preset"),
            "idle_sec": round(time.time() - self._last_used, 1) if self._last_used else None,
        }


engine = TranscribeEngine()


def _free_assistant_vram():
    """전사가 쓰지 않는 LLM·TTS를 내려 VRAM을 돌려준다 (transcribe.free_assistant_vram).

    전사 세션은 어시스턴트 듣기와 배타적이라 그동안 캐릭터는 말하지 않는다. 둘 다 다음
    요청에서 스스로 다시 올라오므로 되돌리는 코드는 없다 — 전사가 끝난 뒤 첫 대답만 느리다.
    한쪽이 실패해도 다른 쪽은 계속 내린다: 목적은 자리를 만드는 것이고, 못 내려도 전사는 돈다.
    """
    if not _cfg().get("free_assistant_vram", True):
        return
    freed = []
    try:
        import llm  # 순환 import 방지 — 호출 시점에만
        prov = getattr(llm, "_provider", None)
        if prov is not None and hasattr(prov, "release"):
            prov.release()
            freed.append(f"LLM({getattr(prov, 'model', '?')})")
    except Exception as e:
        print(f"[Transcribe] LLM 반환 실패(무시): {e}")
    try:
        import tts
        for name, prov in list(getattr(tts, "_providers", {}).items()):
            if hasattr(prov, "release"):
                prov.release()
                freed.append(f"TTS({name})")
    except Exception as e:
        print(f"[Transcribe] TTS 반환 실패(무시): {e}")
    if freed:
        print(f"[Transcribe] 전사는 STT만 씁니다 — {', '.join(freed)} 내려 VRAM 반환 "
              f"(다음 대답 때 다시 올라옵니다)")


@router.get("/presets")
async def transcribe_presets():
    """고를 수 있는 전사 프리셋 목록 (무게 순서는 config 정의 순서)."""
    cfg = _cfg()
    items = []
    for name, preset in _presets(cfg).items():
        items.append({
            "name": name,
            "label": (preset or {}).get("label", name),
            "model": (preset or {}).get("model"),
            "device": (preset or {}).get("device"),
            "compute_type": (preset or {}).get("compute_type"),
            "beam_size": (preset or {}).get("beam_size"),
            "vram_mb": (preset or {}).get("vram_mb"),
            "is_default": name == cfg.get("preset"),
        })
    return {"default": cfg.get("preset"), "active": engine.status()["preset"],
            "presets": items}


@router.post("/select")
async def transcribe_select(preset: str = None, preload: bool = True):
    """
    사용할 프리셋 선택 (Node가 세션 시작 시 호출).
    :param preset: 프리셋 이름 (생략 시 config의 transcribe.preset)
    :param preload: 즉시 로딩 — 첫 발화가 모델 로딩 시간을 기다리지 않게 한다
    """
    return await engine.select(preset, preload)


@router.post("/utterance")
async def transcribe_utterance(request: Request,
                               sr: int = 48000,
                               ch: int = 2,
                               speaker: str = ""):
    """
    발화 한 덩어리(raw int16 PCM) → 텍스트.
    :param sr: 입력 샘플레이트 (디스코드 기본 48000)
    :param ch: 입력 채널 수 (디스코드 기본 2)
    :param speaker: 로그용 화자 이름 (인식에는 영향 없음)
    :return: {"text": "...", "duration": 초, "pending": 대기 중인 전사 수}
    """
    raw = await request.body()
    audio = pcm_to_16k_mono(raw, sample_rate=sr, channels=ch)

    seconds = duration_sec(audio)
    min_sec = float(_cfg().get("min_utterance_sec", 0.4))
    if seconds < min_sec:
        return {"text": "", "duration": round(seconds, 2), "skipped": "too_short"}

    max_sec = float(_cfg().get("max_utterance_sec", 60))
    if seconds > max_sec:
        # 지나치게 긴 덩어리는 뒤쪽만 남기지 않고 앞부터 잘라 전사 (드문 케이스)
        audio = audio[: int(max_sec * 16000)]
        seconds = duration_sec(audio)

    started = time.time()
    text = await engine.transcribe(audio)
    took = round(time.time() - started, 1)
    if text:
        print(f"[전사] {speaker or '?'}: {text}  ({seconds:.1f}초 발화 / {took}초 소요)")
    return {"text": text, "duration": round(seconds, 2),
            "took": took, "pending": engine._pending}


@router.post("/release")
async def transcribe_release():
    """전용 STT 인스턴스를 내려 VRAM 반환 (세션 종료 시 Node가 호출)."""
    released = await engine.release()
    return {"released": released}


@router.get("/status")
async def transcribe_status():
    """활성 프리셋, 로딩 여부, 대기열 상태."""
    return engine.status()
