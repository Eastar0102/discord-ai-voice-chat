"""
Claude (Anthropic API) Provider.

로컬 모델과 다른 점 두 가지를 이 파일에서 흡수한다.

1) 시스템 프롬프트가 messages가 아니라 top-level `system`으로 간다.
2) **프롬프트 캐싱** — 성격 규칙 + 말투 예시(약 7,050자, 4,400토큰)는 매 턴 글자 하나까지
   똑같다. 그 앞부분만 잘라 캐시 표시를 붙이면 입력 비용이 1/10이 된다.
   config/prompt.txt는 [규칙 … $TOPIC] 다음에 $MEMORY·$LORE(매 턴 바뀜)가 오므로,
   $MEMORY 앞까지가 그대로 안정 구간이다. 실제로 그 경계에서 잘리는지 매번 확인하고,
   안 맞으면 캐싱을 포기한다 (프롬프트를 고쳐도 조용히 틀리지 않게).

생각(thinking)은 기본으로 끄지 않고 effort를 낮춘다 — Opus 5에서 thinking을 끄면
도구 호출을 본문에 쓰거나 태그가 새는 사고가 알려져 있다. 음성 대화는 지연이 중요하므로
effort는 low가 기본이고 config로 올릴 수 있다.
"""

import os

import anthropic


class ClaudeLLM:
    def __init__(self, options: dict):
        self.model = options.get("model", "claude-opus-5")
        self.max_tokens = int(options.get("max_tokens", 300))
        self.effort = options.get("effort", "low")
        self.cache = bool(options.get("cache", True))
        self.timeout = float(options.get("timeout", 60))
        api_key = options.get("api_key") or os.getenv("ANTHROPIC_API_KEY")
        # 키가 없으면 SDK가 ant 로그인 프로필 등 다른 경로로 찾는다 — 굳이 막지 않는다
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=self.timeout) \
            if api_key else anthropic.AsyncAnthropic(timeout=self.timeout)
        self._warned = False
        print(f"Claude provider: {self.model} (effort={self.effort}, 캐싱={'on' if self.cache else 'off'})")

    # ------------------------------------------------------------------ 캐싱
    def _stable_prefix(self) -> str:
        """매 턴 똑같은 앞부분 (성격 규칙 + 말투 예시). 못 구하면 빈 문자열"""
        try:
            import llm  # 지연 임포트 — llm이 이 모듈을 먼저 부른다
            template, topic = llm.prompt_for_model(self.model)
            head = template.split("$MEMORY")[0]
            return head.replace("$TOPIC", topic) if head else ""
        except Exception:
            return ""

    def _system_blocks(self, system_text: str):
        """[안정 구간(캐시), 나머지] 로 나눈다. 경계를 못 찾으면 통째로 하나"""
        if not self.cache:
            return [{"type": "text", "text": system_text}]
        prefix = self._stable_prefix()
        if prefix and system_text.startswith(prefix) and len(prefix) > 500:
            return [
                {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system_text[len(prefix):]},
            ]
        if not self._warned:
            self._warned = True
            print("[Claude] 캐시 경계를 찾지 못해 이번 프롬프트는 통째로 보냅니다 "
                  "(prompt.txt의 $MEMORY 위치가 바뀌었는지 확인하세요)")
        return [{"type": "text", "text": system_text}]

    @staticmethod
    def _split(messages: list):
        """(system 문자열, 나머지 메시지). 연속된 같은 역할은 합친다"""
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        turns = []
        for m in messages:
            if m.get("role") == "system":
                continue
            content = m.get("content") or ""
            if turns and turns[-1]["role"] == m["role"]:
                turns[-1]["content"] += "\n" + content
            else:
                turns.append({"role": m["role"], "content": content})
        # 첫 메시지는 user여야 한다
        while turns and turns[0]["role"] != "user":
            turns.pop(0)
        return system, turns

    @staticmethod
    def _supports_effort(model: str) -> bool:
        """effort는 Opus 4.5+·Sonnet 5·Fable 계열만 받는다.
        Haiku 4.5와 Sonnet 4.5는 400 'This model does not support the effort parameter' (실측 09-26)"""
        m = (model or "").lower()
        return not ("haiku" in m or "sonnet-4-5" in m)

    def _params(self, messages: list, options: dict = None):
        system, turns = self._split(messages)
        opts = options or {}
        model = opts.get("model", self.model)
        params = {
            "model": model,
            "max_tokens": int(opts.get("num_predict") or opts.get("max_tokens") or self.max_tokens),
            "messages": turns or [{"role": "user", "content": "..."}],
        }
        if self._supports_effort(model):
            params["output_config"] = {"effort": opts.get("effort", self.effort)}
        if system:
            params["system"] = self._system_blocks(system)
        if "temperature" in opts:
            # Opus 5 계열은 temperature를 받지 않는다 — 조용히 무시한다
            pass
        return params

    # ------------------------------------------------------------------ 호출
    async def chat(self, messages: list) -> str:
        res = await self.client.messages.create(**self._params(messages))
        if res.stop_reason == "refusal":
            print(f"[Claude] 거절됨: {getattr(res.stop_details, 'category', None)}")
            return ""
        return "".join(b.text for b in res.content if b.type == "text").strip()

    async def stream_chat(self, messages: list, options: dict = None):
        params = self._params(messages, options)
        async with self.client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        u = final.usage
        cached, fresh = getattr(u, "cache_read_input_tokens", 0) or 0, u.input_tokens
        print(f"[Claude] 입력 {fresh + cached}토큰 (캐시 {cached}) · 출력 {u.output_tokens}토큰")

    def warmup(self):
        """원격이라 예열할 것이 없다 (인터페이스만 맞춘다)"""
        return None

    def release(self):
        return None
