"""
STT Provider 레지스트리 — config의 stt.provider 이름으로 구현체를 선택한다.
모든 Provider는 transcribe(audio_np) -> str 을 구현한다 (providers/base.py: STTProvider).
"""


def get_provider(name: str, options: dict = None):
    if name == "whisper_local":
        from providers.stt.whisper_local import WhisperLocalSTT
        return WhisperLocalSTT(options or {})
    raise ValueError(f"Unknown STT provider: {name}")
