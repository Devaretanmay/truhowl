# Zero-Trust Credential Proxy Guide

Truhowl includes a built-in Credential Proxy that allows AI agents to make outbound API requests without ever storing raw API keys in agent-readable configuration: keys live in host environment variables and are injected into proxied requests in memory.

---

## 1. Overview

Agent applications often need to issue HTTP calls to LLM providers (e.g. OpenAI, Anthropic, Hugging Face) or external microservices. Storing raw API keys in environment variables inside an untrusted agent environment risks prompt injection leaks or secret theft.

The Truhowl Credential Proxy operates as a local HTTP proxy server that intercepts requests matching predefined route patterns and injects authentication headers in memory before forwarding requests upstream.

---

## 2. Configuring Proxy Routes (`RouteConfig`)

```python
from truhowl.sandbox.proxy import CredentialProxy, RouteConfig

proxy = CredentialProxy(routes=[
    RouteConfig(
        prefix="/openai",
        upstream="https://api.openai.com",
        credential_source="env:OPENAI_API_KEY",
        header="Authorization",
        format="Bearer {credential}"
    ),
    RouteConfig(
        prefix="/anthropic",
        upstream="https://api.anthropic.com",
        credential_source="env:ANTHROPIC_API_KEY",
        header="x-api-key",
        format="{credential}"
    )
])
```

---

## 3. Proxy Lifecycle & Environment Injection

```python
# Start the local HTTP proxy server
proxy.start()

# Automatically sets HTTP_PROXY and HTTPS_PROXY in the environment
proxy.set_env()

# Agent code executes requests through the proxy
# e.g., requests to http://localhost:<port>/openai/v1/chat/completions
# are rewritten to https://api.openai.com/v1/chat/completions with API key attached.

# Clean up environment variables and shutdown proxy
proxy.restore_env()
proxy.stop()
```

---

## 4. Origin-Form vs Absolute-Form Request Handling

The Credential Proxy handles both HTTP request forms transparently:

- **Origin-Form**: `GET /openai/v1/models`
- **Absolute-Form**: `GET http://api.openai.com/openai/v1/models`

Query string parameters are preserved during path rewriting, and unmatched requests pass through untouched according to the compartment's network access policy.
