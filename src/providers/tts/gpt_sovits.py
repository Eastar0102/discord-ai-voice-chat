"""
GPT-SoVITS TTS Provider — 직접 학습한 캐릭터 목소리 (로컬, v2Pro)

- GPT-SoVITS는 자체 런타임(Python 3.9·torch 2.0)이라 이 프로젝트 venv에 import할 수 없다 →
  통합 패키지의 api_v2.py를 별도 프로세스로 띄우고 HTTP(/tts)로 호출한다
- 감정 태그 → voice preset(voice.json)의 감정별 참조 음성·대사. 평소 음성(neutral.wav)을
  보조 참조(aux)로 섞어 감정이 바뀌어도 음색을 붙잡는다
- GPT-SoVITS는 주·보조 참조의 스타일 벡터를 단순 평균한다 → 감정 참조를 보조에 반복해 넣어
  감정:평소 비율(emotion_weight)을 정한다. 1:1은 감정 음높이 차가 절반 넘게 사라져(화남 +72→+23Hz)
  청취 비교에서 3:1을 선택
- 서버가 없으면 warmup()이 띄운다 (로컬 주소일 때만). API 서버의 자식 프로세스로 띄워
  컨트롤 앱이 서버를 내리면(taskkill /T) 함께 내려가고 VRAM(~1.6GB)도 반환된다
"""
import atexit
import json
import os
import subprocess
import time
from urllib.parse import urlparse

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_URL = "http://127.0.0.1:9880"
STYLES = ("neutral", "happy", "sad", "curious", "admiring", "angry", "sleepy")


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


class GptSovitsTTS:
    def __init__(self, options: dict):
        self.base_url = options.get("base_url", DEFAULT_URL).rstrip("/")
        self.install_dir = options.get("install_dir", "")
        self.gpt_weights = options.get("gpt_weights", "")
        self.sovits_weights = options.get("sovits_weights", "")
        self.aux_neutral = options.get("aux_neutral", True)
        self.emotion_weight = max(1, int(options.get("emotion_weight", 3)))
        self.params = {"top_k": options.get("top_k", 15), "top_p": options.get("top_p", 1.0),
                       "temperature": options.get("temperature", 1.0), "speed_factor": options.get("speed", 1.0),
                       "seed": options.get("seed", 8), "repetition_penalty": options.get("repetition_penalty", 1.35)}
        # parallel_infer는 짧은 문장에서 손해다 — 소리는 바이트까지 같은데 시간만 든다.
        # 실측(v2Pro, 09-23): 10~16자 1.19→1.08s, 25~35자 2.76→2.11s, 50~70자 5.18→4.15s
        self.parallel_infer = bool(options.get("parallel_infer", False))
        self.startup_timeout = options.get("startup_timeout_sec", 120)
        self.label = options.get("label", "GPT-SoVITS")

        preset_dir = _abs(options.get("voice_preset", "characters/example/voice"))
        spec = json.load(open(os.path.join(preset_dir, "voice.json"), encoding="utf-8"))
        self.refs = {e: {"wav": os.path.join(preset_dir, v["reference_wav"]), "text": v["line"]}
                     for e, v in spec["emotions"].items()}
        self.neutral_wav = self.refs["neutral"]["wav"]
        self._proc = None
        self._client = httpx.Client(timeout=httpx.Timeout(60.0, connect=2.0))

    # ── 서버 수명 ───────────────────────────────────────────
    def is_running(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/docs", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    def _launch(self):
        host = urlparse(self.base_url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(f"GPT-SoVITS({self.base_url})에 연결할 수 없습니다 — 원격 주소라 자동 실행하지 않습니다")
        pkg = self.install_dir
        py = os.path.join(pkg, "runtime", "python.exe")
        if not os.path.exists(py):
            raise RuntimeError(f"GPT-SoVITS 패키지를 찾을 수 없습니다: {pkg} (config tts.gpt_sovits.install_dir)")
        for w in (self.gpt_weights, self.sovits_weights):
            if not os.path.exists(os.path.join(pkg, w)):
                raise RuntimeError(f"학습 가중치가 없습니다: {w}")

        # api_v2는 -c의 yaml을 읽고 가중치 교체 때 그 파일에 다시 쓴다 — 패키지 기본 설정을 건드리지 않게 전용 파일
        cfg_path = os.path.join(pkg, "GPT_SoVITS", "configs", "companion_infer.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("custom:\n"
                    "  bert_base_path: GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large\n"
                    "  cnhuhbert_base_path: GPT_SoVITS/pretrained_models/chinese-hubert-base\n"
                    "  device: cuda\n  is_half: true\n  version: v2Pro\n"
                    f"  t2s_weights_path: {self.gpt_weights}\n  vits_weights_path: {self.sovits_weights}\n")
        port = str(urlparse(self.base_url).port or 9880)
        log_dir = os.path.join(ROOT, "data", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log = open(os.path.join(log_dir, "gpt_sovits.log"), "w", encoding="utf-8")
        # PYTHONUTF8: 자식 런타임이 cp949로 파일을 쓰다 한자가 섞인 문장에서 합성이 실패했다
        #   ("cp949 codec can't encode character '试'") — UTF-8 모드로 띄운다
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PATH=os.path.join(pkg, "runtime") + os.pathsep + os.environ.get("PATH", ""))
        print(f"[TTS:gpt_sovits] 서버 실행 중 ({pkg}, 포트 {port})...")
        self._proc = subprocess.Popen([py, "-I", "api_v2.py", "-a", "127.0.0.1", "-p", port, "-c", cfg_path],
                                      cwd=pkg, env=env, stdout=log, stderr=subprocess.STDOUT)
        atexit.register(self.release)

    def warmup(self):
        """서버가 없으면 띄우고, 모델 로딩까지 끝나도록 짧은 문장을 한 번 합성한다"""
        if not self.is_running():
            self._launch()
            deadline = time.time() + self.startup_timeout
            while not self.is_running():
                if self._proc.poll() is not None:
                    raise RuntimeError("GPT-SoVITS 서버가 시작 직후 종료됐습니다 — data/logs/gpt_sovits.log 확인")
                if time.time() > deadline:
                    raise RuntimeError(f"GPT-SoVITS 서버가 {self.startup_timeout}초 안에 응답하지 않습니다")
                time.sleep(1)
        # 첫 합성은 CUDA 커널 준비로 느리다 — 첫 대답에서 기다리지 않게 미리
        self.synthesize("안녕하세요.", "neutral")

    def release(self):
        """직접 띄운 서버만 내린다 (VRAM 반환). 외부에서 띄운 서버는 건드리지 않음"""
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            subprocess.run(["taskkill", "/pid", str(p.pid), "/T", "/F"], capture_output=True)
            print("[TTS:gpt_sovits] 서버 종료 (VRAM 반환)")

    # ── 합성 ────────────────────────────────────────────────
    def synthesize(self, text: str, style: str = "neutral"):
        ref = self.refs.get(style) or self.refs["neutral"]
        aux = ([ref["wav"]] * (self.emotion_weight - 1) + [self.neutral_wav]
               if self.aux_neutral and ref["wav"] != self.neutral_wav else [])
        payload = {"text": text, "text_lang": "ko", "ref_audio_path": ref["wav"], "prompt_text": ref["text"],
                   "prompt_lang": "ko", "aux_ref_audio_paths": aux, "text_split_method": "cut5",
                   "batch_size": 1, "media_type": "wav", "streaming_mode": False,
                   "parallel_infer": self.parallel_infer,
                   **self.params}
        try:
            res = self._client.post(f"{self.base_url}/tts", json=payload)
        except httpx.HTTPError as e:
            print(f"[TTS:gpt_sovits] 서버 연결 실패: {e}")
            return None
        if res.status_code != 200:
            print(f"[TTS:gpt_sovits] 합성 실패 {res.status_code}: {res.text[:200]}")
            return None
        return res.content or None
