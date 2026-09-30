import os
import unittest
from unittest.mock import MagicMock, patch

from truhowl.llm import LLMClient, LLMConfig, resolve_llm_config


class TestLLMClient(unittest.TestCase):
    def test_resolve_anthropic_explicit_key(self):
        cfg = resolve_llm_config(api_key="sk-ant-test12345")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.provider, "anthropic")
        self.assertEqual(cfg.api_key, "sk-ant-test12345")
        self.assertIn("claude", cfg.model)

    def test_resolve_openai_explicit_key(self):
        cfg = resolve_llm_config(api_key="sk-proj-test12345")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.provider, "openai")
        self.assertEqual(cfg.model, "gpt-4o")

    def test_resolve_env_anthropic(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-fromenv"}, clear=True):
            cfg = resolve_llm_config()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.provider, "anthropic")
            self.assertEqual(cfg.api_key, "sk-ant-fromenv")

    def test_resolve_env_openai(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fromenv"}, clear=True):
            cfg = resolve_llm_config()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.provider, "openai")
            self.assertEqual(cfg.api_key, "sk-fromenv")

    def test_resolve_custom_base_url(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://localhost:11434/v1"}, clear=True):
            cfg = resolve_llm_config()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.provider, "openai_compatible")
            self.assertEqual(cfg.base_url, "http://localhost:11434/v1")

    def test_resolve_groq_explicit_key(self):
        cfg = resolve_llm_config(api_key="gsk_test1234567890")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.provider, "openai_compatible")
        self.assertEqual(cfg.base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(cfg.model, "openai/gpt-oss-120b")

    def test_resolve_env_groq(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fromenv123"}, clear=True):
            cfg = resolve_llm_config()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.provider, "openai_compatible")
            self.assertEqual(cfg.api_key, "gsk_fromenv123")
            self.assertEqual(cfg.base_url, "https://api.groq.com/openai/v1")

    @patch("truhowl.llm.load_credentials", return_value=None)
    def test_resolve_none_when_empty(self, _mock_creds):
        with patch.dict(os.environ, {}, clear=True):
            cfg = resolve_llm_config()
            self.assertIsNone(cfg)

    @patch("urllib.request.urlopen")
    def test_call_openai_mock(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"choices": [{"message": {"content": "hello world"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}'
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        cfg = LLMConfig(provider="openai", api_key="sk-test", model="gpt-4o")
        client = LLMClient(cfg)
        resp = client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "hello world")
        self.assertEqual(resp.prompt_tokens, 10)

    @patch("urllib.request.urlopen")
    def test_call_anthropic_mock(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"content": [{"type": "text", "text": "claude response"}], "usage": {"input_tokens": 15, "output_tokens": 8}}'
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        cfg = LLMConfig(provider="anthropic", api_key="sk-ant-test", model="claude-3-5-sonnet")
        client = LLMClient(cfg)
        resp = client.complete([{"role": "user", "content": "hi"}], system_prompt="system")
        self.assertEqual(resp.content, "claude response")
        self.assertEqual(resp.prompt_tokens, 15)


if __name__ == "__main__":
    unittest.main()
