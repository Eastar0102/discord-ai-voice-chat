"""
LLM Provider 레지스트리 — config의 llm.provider 이름으로 구현체를 선택한다.
모든 Provider는 chat(messages) -> str 과 stream_chat(messages) -> AsyncIterator[str] 를
구현한다 (providers/base.py: LLMProvider).
"""


def get_provider(name: str, options: dict = None):
    if name == "openai_chat":
        from providers.llm.openai_chat import OpenAIChatLLM
        return OpenAIChatLLM(options or {})
    if name == "claude":
        from providers.llm.claude import ClaudeLLM
        return ClaudeLLM(options or {})
    if name == "ollama":
        from providers.llm.ollama import OllamaLLM
        return OllamaLLM(options or {})
    if name == "llamacpp":
        from providers.llm.llamacpp import LlamaCppLLM
        return LlamaCppLLM(options or {})
    raise ValueError(f"Unknown LLM provider: {name}")
