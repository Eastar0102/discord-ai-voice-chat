"""
Ollama 로컬 LLM Provider (네이티브 /api/chat).
- OpenAI 호환 엔드포인트(/v1) 대신 네이티브 API를 쓰는 이유: think=false(추론 모델의 사고 출력
  끄기), keep_alive(VRAM 상주 시간), options(num_predict/num_ctx)를 직접 제어해야
  음성 응답의 첫 토큰 지연이 안정적이다
- 설치 모델 조회·로드·해제도 여기 둔다 — 컨트롤 패널의 모델 교체가 사용
- 오류는 RuntimeError로 raise — ERROR_* 문자열 매핑은 호출자(llm.py)가 유지
"""

import json
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx

DEFAULT_URL = "http://127.0.0.1:11434"


def is_running(base_url: str = DEFAULT_URL) -> bool:
    try:
        return httpx.get(f"{base_url.rstrip('/')}/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_server(base_url: str = DEFAULT_URL, wait_sec: float = 30) -> None:
    """
    Ollama가 꺼져 있으면 실행하고 응답할 때까지 기다린다 (로컬 주소일 때만).
    Windows에서는 로그인 자동 실행과 같은 트레이 앱(ollama app.exe)을 우선 띄운다.
    cmd의 start로 띄우는 이유: 부모(cmd)가 즉시 종료돼 프로세스 트리가 끊기므로,
    컨트롤 앱이 서버를 taskkill /T로 내릴 때 Ollama까지 같이 죽지 않는다.
    """
    if is_running(base_url):
        return
    host = urlparse(base_url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(f"Ollama({base_url})에 연결할 수 없습니다 — 원격 주소라 자동 실행하지 않습니다")

    if sys.platform == "win32":
        tray = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama app.exe")
        if os.path.exists(tray):
            cmd = f'start "" "{tray}"'
        elif shutil.which("ollama"):
            cmd = 'start "" /min ollama serve'
        else:
            raise RuntimeError("Ollama가 설치돼 있지 않습니다 (https://ollama.com)")
        subprocess.run(cmd, shell=True, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        if not shutil.which("ollama"):
            raise RuntimeError("Ollama가 설치돼 있지 않습니다 (https://ollama.com)")
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    print("Ollama가 꺼져 있어 실행합니다...")
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if is_running(base_url):
            print("Ollama 실행 완료")
            return
        time.sleep(0.5)
    raise RuntimeError(f"Ollama를 실행했지만 {wait_sec:.0f}초 안에 응답하지 않습니다")


class OllamaLLM:
    def __init__(self, options: dict):
        self.base_url = options.get("base_url", DEFAULT_URL).rstrip("/")
        self.model = options.get("model")
        if not self.model:
            raise ValueError("ollama provider requires 'model'")
        # 기본 5분이면 대화가 뜸할 때 언로드돼 다음 응답이 모델 로딩(수 초)을 기다린다
        self.keep_alive = options.get("keep_alive", "30m")
        self.think = options.get("think", False)
        self.timeout = httpx.Timeout(options.get("timeout_sec", 120), connect=5)
        self.gen_options = {
            "temperature": options.get("temperature", 0.6),
            "num_predict": options.get("max_tokens", 200),
            "top_p": options.get("top_p", 1.0),
        }
        if options.get("num_ctx"):
            self.gen_options["num_ctx"] = options["num_ctx"]
        # Ollama 고유 옵션(repeat_penalty 등)은 options 키로 그대로 전달
        self.gen_options.update(options.get("options", {}))

    def _payload(self, messages: list, stream: bool) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": self.gen_options,
        }

    async def prefetch(self, messages: list) -> None:
        """프롬프트만 먹여 KV 캐시를 데운다 — 사용자가 아직 말하는 중에 부분 인식으로 부른다.
        토큰은 한 개만 받고 버린다. 실측(09-23): 첫 조각 1.17s → 0.84s, 데우는 비용 0.44s"""
        payload = {**self._payload(messages, stream=False),
                   "options": {**self.gen_options, "num_predict": 1}}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as client:
                await client.post(f"{self.base_url}/api/chat", json=payload)
        except Exception:
            pass  # 데우기 실패는 대화에 영향이 없다

    def chat(self, messages: list) -> str:
        """메시지 목록 → 완성된 응답 텍스트 (동기)"""
        res = httpx.post(f"{self.base_url}/api/chat",
                         json=self._payload(messages, stream=False), timeout=self.timeout)
        data = res.json()
        if res.status_code != 200 or "error" in data:
            raise RuntimeError(f"Ollama error ({res.status_code}): {data.get('error', data)}")
        return data.get("message", {}).get("content", "").strip()

    async def stream_chat(self, messages: list, options: dict = None):
        """메시지 목록 → 텍스트 델타 스트림 (비동기 제너레이터).
        제너레이터가 중간에 닫히면 HTTP 스트림도 닫혀 Ollama가 생성을 멈춘다 (barge-in).
        options: 이번 호출에만 얹을 생성 옵션 (지난 대답을 베껴 다시 뽑을 때 온도를 올린다)"""
        payload = self._payload(messages, stream=True)
        if options:
            payload["options"] = {**self.gen_options, **options}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as res:
                if res.status_code != 200:
                    body = (await res.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"Ollama error ({res.status_code}): {body}")
                async for line in res.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise RuntimeError(f"Ollama error: {data['error']}")
                    content = data.get("message", {}).get("content")
                    if content:
                        yield content
                    if data.get("done"):
                        break

    def warmup(self):
        """모델을 VRAM에 올려둔다 — 교체 직후 첫 응답이 로딩을 기다리지 않게.
        빈 messages는 생성 없이 로드만 한다. Ollama가 꺼져 있으면 먼저 실행한다."""
        ensure_server(self.base_url)
        res = httpx.post(f"{self.base_url}/api/chat",
                         json={"model": self.model, "messages": [], "keep_alive": self.keep_alive},
                         timeout=httpx.Timeout(300, connect=5))
        if res.status_code != 200:
            raise RuntimeError(f"Ollama load failed ({res.status_code}): {res.text}")

    def release(self):
        """모델을 VRAM에서 내린다 (keep_alive=0). 실패해도 교체는 진행."""
        try:
            httpx.post(f"{self.base_url}/api/chat",
                       json={"model": self.model, "messages": [], "keep_alive": 0},
                       timeout=httpx.Timeout(30, connect=5))
        except Exception as e:
            print(f"Warning: Ollama unload failed for {self.model}: {e}")


def list_models(base_url: str = DEFAULT_URL) -> list[dict]:
    """설치된 채팅 가능 모델 + 현재 VRAM에 올라간 상태.
    Ollama가 꺼져 있으면 httpx 예외가 그대로 올라간다."""
    base_url = base_url.rstrip("/")
    tags = httpx.get(f"{base_url}/api/tags", timeout=3).json().get("models", [])
    loaded = {m["name"]: m.get("size_vram", 0)
              for m in httpx.get(f"{base_url}/api/ps", timeout=3).json().get("models", [])}
    models = []
    for m in tags:
        caps = m.get("capabilities")
        if caps is not None and "completion" not in caps:
            continue  # 임베딩 전용 모델 등
        details = m.get("details", {})
        models.append({
            "model": m["name"],
            "size": m.get("size", 0),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "thinking": "thinking" in (caps or []),
            "loaded": m["name"] in loaded,
            "size_vram": loaded.get(m["name"], 0),
        })
    return models
