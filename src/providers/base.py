"""
Provider 인터페이스 정의 (Protocol).

엔진(faster-whisper / openai SDK / Edge TTS 등)은 전부 이 인터페이스 뒤에 둔다.
엔진 교체는 config 수정만으로 가능해야 하고, providers/ 밖의 어떤 모듈도
faster_whisper·openai를 직접 import하지 않는다.
새 엔진은 이 시그니처를 구현하고 providers/<종류>/__init__.py의 get_provider에 이름을 등록하면 된다.

지금은 최소 시그니처다. 확장할 만한 것:
- STTProvider.stream(): 스트리밍 STT 엔진(Deepgram 등)용 부분 결과
- LLMProvider: 네이티브 tool calling, 구조화 출력(선두 스타일 태그)
- 풍부한 타입(AudioSegment/Transcript/VoiceStyle)으로의 교체
"""

from typing import AsyncIterator, Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class STTProvider(Protocol):
    def transcribe(self, audio_np: np.ndarray) -> str:
        """16kHz 단일 채널 numpy 배열 → 인식 텍스트. 무음/저신뢰는 빈 문자열."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    def chat(self, messages: list[dict]) -> str:
        """메시지 목록 → 완성된 응답 텍스트 (동기)."""
        ...

    def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """메시지 목록 → 텍스트 델타 스트림 (비동기 제너레이터)."""
        ...


@runtime_checkable
class TTSProvider(Protocol):
    def synthesize(self, text: str, style: str = "neutral") -> bytes | None:
        """텍스트 + 감정 스타일 → 오디오 바이트. 실패 시 None."""
        ...
