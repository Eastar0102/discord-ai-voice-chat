"""
PCM 변환 유틸 — 디스코드 원본(48kHz 스테레오 int16)을 STT 입력(16kHz 모노)으로.

websocket_handler는 청크 간 필터 상태를 유지하는 스트리밍 전용 경로를 따로 갖고 있다.
여기 함수들은 "발화 한 덩어리"를 한 번에 변환하는 무상태 경로용이다 (전사 모드).
"""

from math import gcd

import numpy as np
import scipy.signal

TARGET_RATE = 16000


def pcm_to_16k_mono(raw: bytes, sample_rate: int = 48000, channels: int = 2) -> np.ndarray:
    """raw int16 PCM → 16kHz 단일 채널 int16 numpy 배열. 데이터가 없으면 빈 배열."""
    if not raw or len(raw) < 2:
        return np.zeros(0, dtype=np.int16)

    samples = np.frombuffer(raw, dtype=np.int16)

    # 채널 수로 나누어떨어지게 잘라낸 뒤 다운믹스
    if channels > 1:
        usable = (len(samples) // channels) * channels
        if usable == 0:
            return np.zeros(0, dtype=np.int16)
        mono = samples[:usable].reshape(-1, channels).mean(axis=1)
    else:
        mono = samples.astype(np.float32)

    if sample_rate != TARGET_RATE:
        # resample_poly는 내부에 안티앨리어싱 FIR을 포함한다 (배수가 아닌 비율도 안전)
        g = gcd(sample_rate, TARGET_RATE)
        mono = scipy.signal.resample_poly(mono, TARGET_RATE // g, sample_rate // g)

    return np.clip(mono, -32768, 32767).astype(np.int16)


def duration_sec(audio_16k: np.ndarray) -> float:
    """16kHz 배열의 길이를 초로."""
    return len(audio_16k) / float(TARGET_RATE)
