"""
Edge TTS Provider — Microsoft Edge 신경망 보이스 (무료, API 키 불필요)
- 베이스 피치를 올려 시그니처 톤을 만든다 (Neuro-sama의 "상용 TTS + 피치 시프트" 패턴)
- 감정 태그를 rate/pitch 변화로 표현
- 합성이 빨라(문장당 수백 ms) 첫 음성까지의 지연(TTFA)이 짧다
"""
import asyncio
import io

import edge_tts


class EdgeTTS:
    # 감정별 rate(%)·pitch(Hz) 변화량 — 베이스 값에 가산
    STYLE_MAP = {
        "sleepy":   {"rate": -20, "pitch": -10},
        "neutral":  {"rate": 0,   "pitch": 0},
        "curious":  {"rate": 5,   "pitch": 15},
        "admiring": {"rate": 0,   "pitch": 10},
        "happy":    {"rate": 10,  "pitch": 20},
        "sad":      {"rate": -15, "pitch": -15},
        "angry":    {"rate": 15,  "pitch": 5},
    }

    def __init__(self, options: dict):
        self.voice = options.get("voice", "ko-KR-SunHiNeural")
        self.base_rate = options.get("base_rate", 5)    # %
        self.base_pitch = options.get("base_pitch", 15)  # Hz (캐릭터 톤업)

    def synthesize(self, text: str, style: str = "neutral"):
        delta = self.STYLE_MAP.get(style, self.STYLE_MAP["neutral"])
        rate = self.base_rate + delta["rate"]
        pitch = self.base_pitch + delta["pitch"]
        rate_str = f"{'+' if rate >= 0 else ''}{rate}%"
        pitch_str = f"{'+' if pitch >= 0 else ''}{pitch}Hz"

        async def run():
            communicate = edge_tts.Communicate(text, self.voice, rate=rate_str, pitch=pitch_str)
            buf = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()

        try:
            # executor 스레드에서 호출되므로 전용 이벤트 루프 사용
            audio = asyncio.run(run())
            return audio if audio else None
        except Exception as e:
            print(f"[TTS:edge] 합성 오류: {e}")
            return None
