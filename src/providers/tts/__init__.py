"""
TTS Provider 레지스트리 — config의 tts.provider 이름으로 구현체를 선택한다.
모든 Provider는 synthesize(text, style) -> bytes | None 을 구현한다.
"""


def get_provider(name: str, options: dict = None):
    if name == "edge":
        from providers.tts.edge import EdgeTTS
        return EdgeTTS(options or {})
    if name == "gpt_sovits":
        from providers.tts.gpt_sovits import GptSovitsTTS
        return GptSovitsTTS(options or {})
    raise ValueError(f"Unknown TTS provider: {name}")
