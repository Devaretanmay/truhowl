"""BYOK LLM Client supporting Anthropic, OpenAI, and OpenAI-compatible endpoints."""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from koyote.credentials import load_credentials
from koyote.redact import redact_secrets


@dataclass
class LLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str | None = None
    timeout_seconds: int = 60


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def resolve_llm_config(
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMConfig | None:
    """Resolve LLM configuration from parameters, environment variables, or stored credentials."""
    key = api_key or os.environ.get("KOYOTE_LLM_KEY")
    url = base_url or os.environ.get("OPENAI_BASE_URL")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")

    # If no explicit args or env vars, check ~/.koyote/credentials.json
    if not key and not anthropic_key and not openai_key and not groq_key and not url:
        try:
            stored = load_credentials()
            if stored:
                stored_prov = stored.get("provider", "openai").lower()
                stored_key = stored.get("api_key")
                stored_model = model or stored.get("model")
                stored_url = url or stored.get("base_url")

                if stored_prov == "anthropic" or (stored_key and stored_key.startswith("sk-ant-")):
                    return LLMConfig(
                        provider="anthropic",
                        api_key=stored_key,
                        model=stored_model or "claude-3-5-sonnet-20241022",
                        base_url=stored_url,
                    )
                elif stored_prov == "groq" or (stored_key and stored_key.startswith("gsk_")):
                    return LLMConfig(
                        provider="openai_compatible",
                        api_key=stored_key,
                        model=stored_model or "openai/gpt-oss-120b",
                        base_url=stored_url or "https://api.groq.com/openai/v1",
                    )
                elif stored_prov in ("ollama", "local", "openai_compatible"):
                    return LLMConfig(
                        provider="openai_compatible",
                        api_key=stored_key or "ollama_or_local",
                        model=stored_model or "deepseek-coder",
                        base_url=stored_url or "http://localhost:11434/v1",
                    )
                else:
                    return LLMConfig(
                        provider="openai",
                        api_key=stored_key,
                        model=stored_model or "gpt-4o",
                        base_url=stored_url,
                    )
        except Exception:
            pass

    if key:
        if key.startswith("sk-ant-") or (model and "claude" in model.lower()):
            return LLMConfig(
                provider="anthropic",
                api_key=key,
                model=model or "claude-3-5-sonnet-20241022",
                base_url=url,
            )
        if key.startswith("gsk_"):
            return LLMConfig(
                provider="openai_compatible",
                api_key=key,
                model=model or "openai/gpt-oss-120b",
                base_url=url or "https://api.groq.com/openai/v1",
            )
        return LLMConfig(
            provider="openai",
            api_key=key,
            model=model or "gpt-4o",
            base_url=url,
        )

    if groq_key:
        return LLMConfig(
            provider="openai_compatible",
            api_key=groq_key,
            model=model or "openai/gpt-oss-120b",
            base_url=url or "https://api.groq.com/openai/v1",
        )

    if anthropic_key:
        return LLMConfig(
            provider="anthropic",
            api_key=anthropic_key,
            model=model or "claude-3-5-sonnet-20241022",
            base_url=url,
        )

    if openai_key:
        return LLMConfig(
            provider="openai",
            api_key=openai_key,
            model=model or "gpt-4o",
            base_url=url,
        )

    if url:
        return LLMConfig(
            provider="openai_compatible",
            api_key="ollama_or_local",
            model=model or "deepseek-coder",
            base_url=url,
        )

    return None


class LLMClient:
    """Zero-dependency HTTP client for LLM API calls."""

    def __init__(self, config: LLMConfig):
        self.config = config

    def complete(self, messages: list[dict[str, str]], system_prompt: str | None = None) -> LLMResponse:
        """Send completion request to configured provider.

        Message contents are secret-scrubbed first: repository file
        contents routinely contain hardcoded keys, and nothing secret
        may leave the machine toward the provider. The auth credential
        itself travels only in request headers, never in the body.
        """
        scrubbed = []
        for message in messages:
            content = message.get("content", "")
            clean, _ = redact_secrets(content) if isinstance(content, str) else (content, 0)
            scrubbed.append({**message, "content": clean})
        if system_prompt:
            system_prompt, _ = redact_secrets(system_prompt)
        if self.config.provider == "anthropic":
            return self._call_anthropic(scrubbed, system_prompt)
        return self._call_openai(scrubbed, system_prompt)

    def _call_anthropic(self, messages: list[dict[str, str]], system_prompt: str | None) -> LLMResponse:
        url = self.config.base_url or "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "Koyote/1.1.0",
        }

        payload: dict[str, object] = {
            "model": self.config.model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if system_prompt:
            payload["system"] = system_prompt

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        data = {}
        for attempt_i in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as err:
                if err.code == 429 and attempt_i < 3:
                    time.sleep(3.0 * (attempt_i + 1))
                    continue
                raise

        content = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")

        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=data.get("model", self.config.model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
        )

    def _call_openai(self, messages: list[dict[str, str]], system_prompt: str | None) -> LLMResponse:
        base = self.config.base_url or "https://api.openai.com/v1"
        url = f"{base.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "User-Agent": "Koyote/1.1.0",
        }

        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        formatted_messages.extend(messages)

        payload = {
            "model": self.config.model,
            "messages": formatted_messages,
            "temperature": 0.0,
            "max_tokens": 4096,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        data = {}
        for attempt_i in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as err:
                if err.code == 429 and attempt_i < 3:
                    time.sleep(3.0 * (attempt_i + 1))
                    continue
                raise

        content = ""
        choices = data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")

        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=data.get("model", self.config.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
