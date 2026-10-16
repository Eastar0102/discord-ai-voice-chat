# vad.py
import webrtcvad
import numpy as np
import sounddevice as sd
import collections
import threading
import time
import queue

class VADAudio:
    def __init__(self, device=None, sample_rate=16000, frame_duration=30, audio_player=None):  # 30ms (기본값)
        """
        :param device: 마이크 장치 번호 (None이면 기본 입력)
        :param sample_rate: 샘플레이트 (16kHz 권장)
        :param frame_duration: 프레임 단위(ms)
        :param audio_player: 현재 TTS 재생 객체 (TTS 중단용)
        """
        self.sample_rate = sample_rate
        self.frame_duration = frame_duration
        self.frame_size = int(sample_rate * frame_duration / 1000)
        self.vad = webrtcvad.Vad(2)  # 감도: 2 (적당한 감도)
        self.device = device
        self.audio_player = audio_player

        self.buffer = collections.deque(maxlen=30)  # 버퍼 크기 조정 (균형)
        self.frames = []
        self.recording = False
        self.q = queue.Queue()

    def callback(self, indata, frames, time_info, status):
      audio = indata[:, 0]
      # float32 → int16 변환
      audio_int16 = (audio * 32767).astype(np.int16)
      raw_bytes = audio_int16.tobytes()

      if len(raw_bytes) != self.frame_size * 2:
          print(f"⚠️ 프레임 크기 불일치: {len(raw_bytes)} bytes")
          return

      is_speech = self.vad.is_speech(raw_bytes, self.sample_rate)
      self.buffer.append((audio_int16, is_speech))

      if is_speech and not self.recording:
          self.recording = True
          self.frames = []

      if self.recording:
          self.frames.append(audio_int16)

          if all(not speech for _, speech in list(self.buffer)[-30:]):  # 적당한 침묵 허용
              self.recording = False
              audio_np = np.concatenate(self.frames)
              self.q.put(audio_np)

    def start(self):
        threading.Thread(target=self._stream, daemon=True).start()

    def _stream(self):
        with sd.InputStream(
          device=self.device,
          samplerate=self.sample_rate,
          channels=1,
          dtype='float32',
          blocksize=self.frame_size,  # 추가!
          callback=self.callback
        ):
            # 조용히 시작 (디버그 정보 제거)
            while True:
                time.sleep(0.1)

    def read_audio(self):
        return self.q.get()


class StreamingVAD:
    """
    WebSocket 스트리밍용 VAD (sounddevice 의존성 제거)
    """
    def __init__(self, sample_rate=16000, frame_duration=30):
        self.sample_rate = sample_rate
        self.frame_duration = frame_duration
        self.frame_size = int(sample_rate * frame_duration / 1000) # 샘플 수 (예: 480)
        self.frame_bytes = self.frame_size * 2 # 바이트 수 (예: 960)
        
        self.vad = webrtcvad.Vad(3) # 감도: 3 (가장 둔감함 - 소음에 강함)

        self.buffer = collections.deque(maxlen=30) # 링 버퍼 (약 1초)
        self.frames = [] # 현재 발화 프레임들
        self.recording = False # 발화 중 여부

        self.raw_buffer = b"" # 들어오는 청크를 프레임 크기로 맞추기 위한 버퍼

        # 롤링 부분 전사(websocket_handler)용 상태
        self.utterance_id = 0       # 발화마다 증가 — 부분 전사 캐시가 이전 발화와 섞이지 않게 함
        self.silence_run = 0        # 녹음 중 연속 침묵 프레임 수
        self.speech_end_frame = 0   # frames 내에서 마지막 음성 프레임의 끝 인덱스
        self.last_utterance_id = 0      # 직전에 확정된 발화의 id
        self.last_speech_end_frame = 0  # 직전에 확정된 발화의 음성 끝 인덱스
        self.last_endpoint = "silence"  # 직전 발화의 확정 방식 (silence|max|early|stall)
        self.last_speech_wall = time.time()  # 마지막 음성 프레임의 벽시계 시각 (지연 계측용)

    def reset(self):
        """
        VAD 상태 초기화 (버퍼 비우기)
        """
        self.buffer.clear()
        self.frames = []
        self.recording = False
        self.raw_buffer = b""
        self.silence_run = 0
        self.speech_end_frame = 0
        print("[VAD] Reset")

    def snapshot(self):
        """
        녹음 중인 발화의 현재까지 오디오 스냅샷 (롤링 부분 전사용)
        :return: (utterance_id, audio_np, 커버한 프레임 수, 음성 끝 프레임) 또는 None
        """
        if not self.recording or not self.frames:
            return None
        return self.utterance_id, np.concatenate(self.frames), len(self.frames), self.speech_end_frame

    def _finish_utterance(self, kind):
        """녹음 종료 공통 처리 — 세그먼트를 확정하고 발화 메타데이터를 기록"""
        self.recording = False
        self.last_utterance_id = self.utterance_id
        self.last_speech_end_frame = self.speech_end_frame
        self.last_endpoint = kind
        result = np.concatenate(self.frames)
        self.frames = []
        return result

    def force_endpoint(self, kind="forced"):
        """
        발화를 즉시 확정 (조기 확정·stall 워치독용 — 침묵 600ms를 기다리지 않음)
        :return: 발화 오디오 numpy array, 너무 짧으면 None
        """
        if not self.recording or not self.frames:
            return None
        total_duration_ms = len(self.frames) * self.frame_duration
        if total_duration_ms < 300:
            # 너무 짧은 발화는 폐기 — recording을 유지하면 stall 워치독이 무한 재시도함
            print(f"[VAD] Ignored short speech ({total_duration_ms}ms, {kind})")
            self.recording = False
            self.frames = []
            return None
        return self._finish_utterance(kind)

    def process_chunk(self, chunk: bytes):
        """
        오디오 청크(16kHz, 16bit mono)를 처리하고, 발화가 완료되면 오디오 데이터를 반환
        :return: 발화 완료 시 numpy array, 아니면 None
        """
        self.raw_buffer += chunk
        
        result = None
        
        while len(self.raw_buffer) >= self.frame_bytes:
            frame = self.raw_buffer[:self.frame_bytes]
            self.raw_buffer = self.raw_buffer[self.frame_bytes:]
            
            # RMS 계산 (소리 크기)
            audio_float = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
            rms = np.sqrt(np.mean(audio_float**2))
            
            # VAD 판별
            try:
                # RMS 게이트: 소리가 너무 작으면 무조건 침묵 처리
                # (0.008 ≈ -42dB — 실발화 RMS는 0.03~0.10 수준, 잡음 세그먼트 생성 억제)
                if rms < 0.008:
                    is_speech = False
                else:
                    is_speech = self.vad.is_speech(frame, self.sample_rate)
            except Exception as e:
                print(f"[VAD] Error: {e}")
                is_speech = False

            # 디버깅: 주기적으로 오디오 레벨 출력 (100프레임마다)
            if len(self.buffer) % 100 == 0:
                 audio_float = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                 rms = np.sqrt(np.mean(audio_float**2))
                 if rms > 0.01: # 의미있는 소리만 로그
                     print(f"[VAD] RMS: {rms:.4f}, Speech: {is_speech}")
            
            # numpy 변환
            audio_int16 = np.frombuffer(frame, dtype=np.int16)
            
            self.buffer.append((audio_int16, is_speech))
            
            if is_speech and not self.recording:
                self.recording = True
                self.utterance_id += 1
                self.silence_run = 0
                print("[VAD] Speech started")
                self.frames = []
                # 버퍼에 있던 이전 프레임들도 일부 포함 (앞부분 잘림 방지)
                # 최근 5개 프레임(150ms) 정도 포함
                pre_speech_frames = [f for f, s in list(self.buffer)[-5:]]
                self.frames.extend(pre_speech_frames)

            if self.recording:
                self.frames.append(audio_int16)
                if is_speech:
                    self.silence_run = 0
                    self.speech_end_frame = len(self.frames)
                    self.last_speech_wall = time.time()
                else:
                    self.silence_run += 1

                # 침묵 감지 (최근 20프레임 = 600ms 동안 침묵이면 종료)
                # (480ms는 문장 중간 숨고르기에도 발화를 잘라 한 문장이 여러 턴으로 파편화됐음)
                if len(self.frames) > 20 and self.silence_run >= 20:
                    # 너무 짧은 발화는 무시 (예: 0.3초 미만)
                    total_duration_ms = len(self.frames) * self.frame_duration
                    if total_duration_ms < 300:
                        print(f"[VAD] Ignored short speech ({total_duration_ms}ms)")
                        self.recording = False
                        self.frames = []
                        return None

                    return self._finish_utterance("silence")

            # 최대 발화 길이 제한 - 소음으로 인한 무한 녹음 방지
            # (5초는 일반 발화도 중간에 잘라 맥락을 깨뜨려서 15초로 상향)
            if self.recording and len(self.frames) * self.frame_duration > 15000:
                 print("[VAD] Max duration reached, forcing flush")
                 return self._finish_utterance("max")
                    
        return result
