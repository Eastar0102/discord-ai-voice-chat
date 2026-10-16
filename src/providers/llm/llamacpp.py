"""
llama.cpp 서버(llama-server) LLM Provider — Ollama로는 못 돌리는 모델을 위해.

Kanana-2-30B-A3B(MoE, 활성 3B)는 Q4로 17GB라 12GB 카드에 안 올라간다. llama-server의
`--cpu-moe`는 전문가 층만 RAM에 두고 나머지를 GPU에 올려 VRAM 2.8GB로 돌린다 — Ollama는
층 단위로만 나눠서 이게 안 된다. 실측(10-05, 실제 세션 24턴): `-ub 4096`이 없으면 첫 토큰
8.3초, 있으면 2.6초. prefill이 배치마다 전문가 층을 CPU로 한 번씩 훑기 때문이다.

- 서버는 이 프로세스의 자식으로 띄우고 release()에서 내린다 (GPT-SoVITS와 같은 방식)
- 슬롯이 둘이다: 0은 대화, 1은 부수 작업(발화 판정·기억 추출). 같은 슬롯을 쓰면 판정
  한 번이 대화의 KV 캐시를 밀어내 다음 턴이 처음부터 prefill한다
- cache_layout: llm.build_messages가 턴마다 바뀌는 칸을 기록 뒤로 보낸다 (llm.py 참고)
- 오류는 RuntimeError — ERROR_* 문자열 매핑은 호출자(llm.py)가 한다
"""

import asyncio
import atexit
import json
import os
import re
import subprocess
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
MAIN_SLOT, SIDE_SLOT = 0, 1
END_TAG_RE = re.compile(r"\[importance:\s*\d+(?:\.\d+)?\s*\]", re.IGNORECASE)


class LlamaCppLLM:
    def __init__(self, options: dict):
        self.model = options.get("model")
        if not self.model:
            raise ValueError("llamacpp provider requires 'model'")
        # 모델마다 다른 설정 (model_options.<이름>) — 공통 값을 덮는다. Qwen3.6은 생각 모드를 꺼야 하고
        # (chat_template_kwargs), 권장 샘플링이 다르다. body로 그대로 보낼 것은 "body"에 둔다
        per = dict((options.get("model_options") or {}).get(self.model) or {})
        self.extra_body = per.pop("body", {})
        options = {**options, **per}
        files = options.get("models") or {}
        self.gguf = files.get(self.model) or options.get("gguf")
        self.server_exe = options.get("server")
        self.port = int(options.get("port", 8090))
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.ctx = int(options.get("ctx", 16384))
        self.slots = int(options.get("parallel", 2))
        self.args = [str(a) for a in options.get("args", [])]
        self.cache_layout = bool(options.get("cache_layout", True))
        self.startup_timeout = float(options.get("startup_timeout_sec", 180))
        self.timeout = httpx.Timeout(options.get("timeout_sec", 120), connect=5)
        self.gen = {
            "temperature": options.get("temperature", 0.6),
            "top_p": options.get("top_p", 0.95),
            "max_tokens": options.get("max_tokens", 200),
        }
        self._proc = None

    # ── 서버 ────────────────────────────────────────────────
    def is_running(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=2)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False

    def _launch(self):
        if not self.server_exe or not os.path.exists(self.server_exe):
            raise RuntimeError(f"llama-server를 찾을 수 없습니다: {self.server_exe} (config llm.llamacpp.server)")
        if not self.gguf or not os.path.exists(self.gguf):
            raise RuntimeError(f"'{self.model}'의 GGUF가 없습니다: {self.gguf} (config llm.llamacpp.models)")
        log_dir = os.path.join(ROOT, "data", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log = open(os.path.join(log_dir, "llamacpp.log"), "w", encoding="utf-8")
        cmd = [self.server_exe, "-m", self.gguf, "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(self.ctx), "-np", str(self.slots), "-ngl", "99", "--jinja", *self.args]
        print(f"[LLM:llamacpp] 서버 실행 중 ({self.model}, 포트 {self.port})...")
        self._proc = subprocess.Popen(cmd, cwd=os.path.dirname(self.server_exe),
                                      stdout=log, stderr=subprocess.STDOUT)
        atexit.register(self.release)

    def warmup(self):
        """서버가 없으면 띄우고 모델 로딩이 끝날 때까지 기다린다 (17GB를 디스크에서 읽으면 수십 초)"""
        if not self.is_running():
            self._launch()
            deadline = time.time() + self.startup_timeout
            while not self.is_running():
                if self._proc.poll() is not None:
                    raise RuntimeError("llama-server가 시작 직후 종료됐습니다 — data/logs/llamacpp.log 확인")
                if time.time() > deadline:
                    raise RuntimeError(f"llama-server가 {self.startup_timeout:.0f}초 안에 응답하지 않습니다")
                time.sleep(1)
        # 첫 요청은 CUDA 그래프 준비로 느리다 — 첫 대답에서 기다리지 않게
        self.chat([{"role": "user", "content": "안녕"}], options={"max_tokens": 1}, slot=SIDE_SLOT)

    def release(self):
        """직접 띄운 서버만 내린다 (VRAM·RAM 반환). 밖에서 띄운 서버는 건드리지 않는다"""
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            subprocess.run(["taskkill", "/pid", str(p.pid), "/T", "/F"], capture_output=True)
            print("[LLM:llamacpp] 서버 종료")

    def _ensure_up(self):
        """내려가 있으면 다시 띄운다. 전사 모드는 시작할 때 대화 모델을 release()해 VRAM을 돌려받고
        "다음 대화에서 다시 올라온다"에 기댄다 — Ollama는 저절로 그러지만 이 서버는 아니다"""
        if not self.is_running():
            print("[LLM:llamacpp] 서버가 내려가 있어 다시 띄웁니다 (첫 대답이 느립니다)")
            self.warmup()

    # ── 요청 ────────────────────────────────────────────────
    def _body(self, messages: list, stream: bool, options: dict = None, slot: int = MAIN_SLOT) -> dict:
        opts = dict(options or {})
        # Ollama 이름으로 들어오는 옵션을 맞춰 준다 (judge·재생성이 Ollama 기준으로 쓴다)
        if "num_predict" in opts:
            opts["max_tokens"] = opts.pop("num_predict")
        json_mode = opts.pop("json", False)
        body = {"messages": messages, "stream": stream, **self.extra_body, **self.gen, **opts,
                "id_slot": slot if slot < self.slots else -1, "cache_prompt": True}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def chat(self, messages: list, options: dict = None, slot: int = SIDE_SLOT) -> str:
        """완성된 응답 (동기). 대화가 아니라 부수 작업이 쓰므로 기본 슬롯은 SIDE"""
        if self._proc is None:
            self._ensure_up()
        r = httpx.post(f"{self.base_url}/v1/chat/completions",
                       json=self._body(messages, False, options, slot), timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"llama-server error ({r.status_code}): {r.text[:300]}")
        return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()

    async def stream_chat(self, messages: list, options: dict = None, slot: int = MAIN_SLOT):
        """텍스트 델타 스트림. 제너레이터가 닫히면 HTTP 스트림이 닫혀 서버가 생성을 멈춘다 (barge-in)"""
        body = self._body(messages, True, options, slot)
        if self._proc is None:
            await asyncio.to_thread(self._ensure_up)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url}/v1/chat/completions", json=body) as res:
                if res.status_code != 200:
                    raise RuntimeError(f"llama-server error ({res.status_code}): "
                                       f"{(await res.aread()).decode('utf-8', 'replace')[:300]}")
                text, sent = "", 0
                async for line in res.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    d = json.loads(data)
                    if "error" in d:
                        raise RuntimeError(f"llama-server error: {d['error']}")
                    delta = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content")
                    if not delta:
                        continue
                    text += delta
                    # 형식의 끝은 [importance: N]이다. Kanana는 그 뒤에 줄을 바꿔 말을 더 잇는 일이 있고
                    # (10-05: "아까 그 얘기요. [importance: 1]" 뒤에 줄을 바꿔 더 말했다), 음성 경로는 받은 것을
                    # 전부 읽는다 — 태그가 닫히는 데서 끊는다
                    m = END_TAG_RE.search(text)
                    if m:
                        if m.end() > sent:
                            yield text[sent:m.end()]
                        return
                    yield text[sent:]
                    sent = len(text)

    async def prefetch(self, messages: list) -> None:
        """사용자가 말하는 중에 프롬프트를 대화 슬롯에 먹여 둔다 (토큰 하나만 받고 버린다).
        실측(10-05): 바뀌는 칸이 최종 턴과 같으면 첫 토큰 1.6s → 0.15s"""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
                await client.post(f"{self.base_url}/v1/chat/completions",
                                  json=self._body(messages, False, {"max_tokens": 1}, MAIN_SLOT))
        except Exception:
            pass   # 데우기 실패는 대화에 영향이 없다
