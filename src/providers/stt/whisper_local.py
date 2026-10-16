"""
로컬 Whisper STT Provider (faster-whisper).
- numpy 오디오 배열을 한국어 텍스트로 변환
- CUDA 우선, 실패 시 CPU(int8) 폴백
- 기동 시 워밍업으로 첫 실호출의 CUDA 커널 초기화 지연을 흡수

옵션 기본값은 어시스턴트 경로(짧은 명령·잡음 환경)에 맞춰져 있다. 전사 모드처럼
"들린 건 최대한 다 남긴다"가 목표인 호출자는 options로 게이트를 풀 수 있다
(rms_gate / no_speech_threshold / sanitize / min_chars).
"""

import re

import numpy as np
from faster_whisper import WhisperModel


class WhisperLocalSTT:
    def __init__(self, options: dict):
        self.model_name = options.get("model", "large-v3")
        self.device = options.get("device", "cuda")
        self.compute_type = options.get("compute_type", "float16")
        self.beam_size = options.get("beam_size", 2)
        self.last_stats = {}                                  # 마지막 전사의 확신도
        self.base_hotwords = options.get("hotwords") or ""   # 설정값 (예: 캐릭터 이름)
        self.hotwords = self.base_hotwords or None            # 실제로 쓰는 값 (채널 사람 이름이 더해진다)
        self.language = options.get("language", "ko")

        # 정확도 우선 디코딩 옵션 (전사 모드의 무거운 프리셋용 — 기본은 끔)
        self.best_of = options.get("best_of")
        self.patience = options.get("patience")
        self.temperature = options.get("temperature")  # 숫자 또는 폴백 리스트
        self.condition_on_previous_text = options.get("condition_on_previous_text", False)

        # 입출력 게이트 (전사 모드에서 완화하기 위해 옵션화 — 기본값은 기존 동작 유지)
        self.rms_gate = options.get("rms_gate", 0.005)
        self.vad_filter = options.get("vad_filter", True)
        self.min_silence_ms = options.get("min_silence_duration_ms", 500)
        self.no_speech_threshold = options.get("no_speech_threshold", 0.6)
        self.logprob_threshold = options.get("logprob_threshold", -1.0)
        self.sanitize = options.get("sanitize", True)
        self.min_chars = options.get("min_chars", 2)
        self.warmup = options.get("warmup", True)

        # 로컬 Whisper 모델 로드 (한 번만)
        print(f"로컬 Whisper 모델(faster-whisper {self.model_name}) 로딩 중...")
        try:
            self.model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
            print(f"로컬 Whisper 모델 로딩 완료 (Device: {self.device}, Type: {self.compute_type})")
        except Exception as e:
            print(f"모델 로딩 실패 (CUDA 오류일 수 있음), CPU로 전환: {e}")
            self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            self.device = "cpu"
            print("로컬 Whisper 모델 로딩 완료 (Device: cpu, Type: int8)")

        # 워밍업: 첫 실호출의 CUDA 커널 초기화 지연(~1초)을 서버 기동 시점으로 이동
        if self.warmup:
            try:
                _dummy = (np.random.randn(16000) * 0.02).astype(np.float32)
                _segments, _ = self.model.transcribe(_dummy, language=self.language, beam_size=1)
                list(_segments)  # 제너레이터 소비 시점에 실제 연산 발생
                print("Whisper 워밍업 완료")
            except Exception as _e:
                print(f"Whisper 워밍업 실패 (무시): {_e}")

    def set_names(self, names):
        """지금 채널에 있는 사람들의 이름을 hotwords에 얹는다 (voice_event마다 갱신)"""
        words = [w.strip() for w in (self.base_hotwords or "").replace(",", " ").split() if w.strip()]
        for n in names or []:
            n = (n or "").strip()
            if n and n not in words:
                words.append(n)
        self.hotwords = " ".join(words) or None
        return self.hotwords

    def transcribe(self, audio_np: np.ndarray) -> str:
        """
        numpy 오디오 배열 → 텍스트 문자열 반환
        :param audio_np: int16 numpy 배열 (16000Hz, 단일 채널)
        :return: 인식된 텍스트
        """
        try:
            # 볼륨 체크
            audio_fp = audio_np.astype(np.float32) / 32768.0
            rms = np.sqrt(np.mean(audio_fp**2)) if len(audio_fp) else 0.0

            if np.isnan(rms) or rms < self.rms_gate:
                return ""

            # faster-whisper는 float32 입력을 기대
            kwargs = {}
            if self.hotwords:
                kwargs["hotwords"] = self.hotwords  # 고유명사 인식 유도 (캐릭터 이름)
            if self.best_of:
                kwargs["best_of"] = self.best_of
            if self.patience:
                kwargs["patience"] = self.patience
            if self.temperature is not None:
                # 리스트를 주면 faster-whisper가 실패 시 순서대로 온도를 올려 재시도
                kwargs["temperature"] = self.temperature

            # initial_prompt는 쓰지 않는다 — 잡음/침묵 입력에서 프롬프트 문장을
            # 그대로 환각 출력하는 부작용이 있음. 이름 유도는 hotwords로 충분.
            segments, info = self.model.transcribe(
                audio_fp,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,  # 내장 VAD 사용 가능
                vad_parameters=dict(min_silence_duration_ms=self.min_silence_ms),
                condition_on_previous_text=self.condition_on_previous_text,  # 기본 False = 환각 방지
                **kwargs
            )

            texts = []
            probs = []      # 마지막 전사의 확신도 — 핫워드가 소음을 이름으로 바꿨는지 가려내는 데 쓴다
            for segment in segments:
                # Whisper 환각 필터: 무음일 확률이 높거나 신뢰도가 낮은 세그먼트 폐기
                # (기침·잡음에서 "다음 영상에서 만나요", "고맙습니다" 류가 생성되는 문제)
                if segment.no_speech_prob > self.no_speech_threshold:
                    continue
                if segment.avg_logprob < self.logprob_threshold:
                    continue
                texts.append(segment.text)
                probs.append((segment.avg_logprob, segment.no_speech_prob))
            text = " ".join(texts).strip()
            self.last_stats = {
                "avg_logprob": round(min(p[0] for p in probs), 3) if probs else None,
                "no_speech_prob": round(max(p[1] for p in probs), 3) if probs else None,
            }

            # 후처리
            if self.sanitize:
                text = re.sub(r'[^\w\s가-힣.,!?]', '', text)
            text = re.sub(r'\s+', ' ', text)
            text = text.strip()

            if len(text) < self.min_chars:
                return ""

            return text

        except Exception as e:
            print(f"[STT Error] {e}")
            import traceback
            traceback.print_exc()
            return ""

    def release(self):
        """모델을 내려 VRAM을 반환한다 (전사 전용 인스턴스의 세션 종료용)."""
        model, self.model = getattr(self, "model", None), None
        del model
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        print(f"Whisper 모델 해제 ({self.model_name})")
