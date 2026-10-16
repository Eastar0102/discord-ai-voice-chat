"""
OpenAI Chat Completions LLM Provider.
- 순수 엔진 호출부만 담당: 메시지 목록을 받아 응답 텍스트(또는 델타 스트림)를 낸다
- RAG·프롬프트 조립·히스토리·태그 파싱은 LLM-비종속 오케스트레이션이라 llm.py가 소유
- API 예외는 그대로 raise — ERROR_* 문자열 매핑은 호출자(llm.py)가 유지
"""

import openai


class OpenAIChatLLM:
    def __init__(self, options: dict):
        self.api_key = options.get("api_key")
        self.model = options.get("model", "gpt-4o-mini")
        self.gen_params = {
            "temperature": options.get("temperature", 0.6),
            "max_tokens": options.get("max_tokens", 200),
            "top_p": options.get("top_p", 1.0),
            "frequency_penalty": options.get("frequency_penalty", 0.0),
            "presence_penalty": options.get("presence_penalty", 0.0),
        }
        self._client = None
        self._async_client = None

    def _get_client(self):
        """동기 클라이언트 지연 초기화 (proxies 인자 호환 처리 포함)"""
        if self._client is None:
            print(f"Initializing OpenAI client with key: {str(self.api_key)[:4]}***")
            try:
                self._client = openai.OpenAI(api_key=self.api_key)
                print("Success: OpenAI client initialized")
            except TypeError as e:
                # proxies 인자 오류인 경우 httpx 클라이언트를 명시적으로 주입
                print(f"TypeError: {e}, trying alternative initialization")
                import httpx
                self._client = openai.OpenAI(api_key=self.api_key, http_client=httpx.Client())
                print("Success: OpenAI client initialized with explicit httpx client")
        return self._client

    def _get_async_client(self):
        """비동기 클라이언트 지연 초기화 (httpx 명시적 주입으로 호환성 확보)"""
        if self._async_client is None:
            print(f"Initializing AsyncOpenAI client with key: {str(self.api_key)[:4]}***")
            import httpx
            self._async_client = openai.AsyncOpenAI(api_key=self.api_key, http_client=httpx.AsyncClient())
            print("Success: AsyncOpenAI client initialized with explicit httpx client")
        return self._async_client

    def chat(self, messages: list) -> str:
        """메시지 목록 → 완성된 응답 텍스트 (동기, 스트리밍 수집)"""
        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **self.gen_params,
        )
        reply = ""
        for chunk in response:
            if chunk.choices[0].delta.content:
                reply += chunk.choices[0].delta.content
        return reply.strip()

    async def stream_chat(self, messages: list):
        """메시지 목록 → 텍스트 델타 스트림 (비동기 제너레이터)"""
        response = await self._get_async_client().chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            **self.gen_params,
        )
        async for chunk in response:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
