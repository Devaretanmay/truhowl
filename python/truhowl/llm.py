"""BYOK LLM Client supporting Anthropic, OpenAI, and OpenAI-compatible endpoints."""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from truhowl.credentials import load_credentials
from truhowl.redact import redact_secrets


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


class LLMTransientError(RuntimeError):
    """Provider said "try again later" and every retry was exhausted.

    Distinct from a hard failure so the audit can classify a rate-limited run
    as MODEL_RATE_LIMIT instead of the misleading "author produced no patch".
    """

    def __init__(self, code: int, message: str, attempts: int):
        super().__init__(f"provider unavailable after {attempts} attempt(s): HTTP {code} {message}")
        self.code = code
        self.attempts = attempts


# Retry policy for rate limits and transient provider failures. A repair run
# issues several calls (reason, author per file, interpret); one throttled call
# must not silently become "the model authored nothing".
_RETRYABLE_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 5
_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 20.0)
_MAX_RETRY_AFTER = 45.0


def _retry_delay(err: "urllib.error.HTTPError", attempt_index: int) -> float:
    """Backoff for one retryable failure, honoring Retry-After when sane."""
    header = ""
    try:
        header = (err.headers.get("Retry-After") or "") if err.headers else ""
    except Exception:
        header = ""
    if header:
        try:
            return max(0.0, min(float(header.strip()), _MAX_RETRY_AFTER))
        except ValueError:
            pass
    return _BACKOFF_SECONDS[min(attempt_index, len(_BACKOFF_SECONDS) - 1)]


def _post_json(url: str, headers: dict[str, str], payload: dict, timeout: int) -> dict:
    """POST JSON with bounded retries on throttling and transient 5xx."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    last_err: urllib.error.HTTPError | None = None
    for attempt_i in range(_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            last_err = err
            if err.code not in _RETRYABLE_CODES or attempt_i >= _MAX_ATTEMPTS - 1:
                break
            time.sleep(_retry_delay(err, attempt_i))
        except (urllib.error.URLError, TimeoutError):
            if attempt_i >= _MAX_ATTEMPTS - 1:
                raise
            time.sleep(_BACKOFF_SECONDS[min(attempt_i, len(_BACKOFF_SECONDS) - 1)])
    if last_err is not None:
        if last_err.code in _RETRYABLE_CODES:
            raise LLMTransientError(last_err.code, str(getattr(last_err, "reason", "")),
                                    _MAX_ATTEMPTS) from last_err
        raise last_err
    raise RuntimeError("LLM request failed without a recorded HTTP error")


def resolve_llm_config(
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMConfig | None:
    """Resolve LLM configuration from parameters, environment variables, or stored credentials."""
    key = api_key or os.environ.get("TRUHOWL_LLM_KEY")
    url = base_url or os.environ.get("OPENAI_BASE_URL")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")

    # If no explicit args or env vars, check ~/.truhowl/credentials.json
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
            "User-Agent": "Truhowl/1.1.0",
        }

        payload: dict[str, object] = {
            "model": self.config.model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if system_prompt:
            payload["system"] = system_prompt

        data = _post_json(url, headers, payload, self.config.timeout_seconds)

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
            "User-Agent": "Truhowl/1.1.0",
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

        data = _post_json(url, headers, payload, self.config.timeout_seconds)

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
