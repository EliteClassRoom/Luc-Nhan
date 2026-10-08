"""Luc Nhan error hierarchy."""

from __future__ import annotations


class LucNhanError(Exception):
    """Base exception for all Luc Nhan errors."""


class ConfigError(LucNhanError):
    """Configuration-related errors."""


class ProviderError(LucNhanError):
    """LLM provider errors."""

    def __init__(
        self,
        message: str,
        provider: str = "",
        status_code: int = 0,
        retryable: bool = False,
        retry_after: float = 0,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


# Provider-specific guidance for missing API keys.
# These are appended to AuthenticationError messages so users get
# actionable instructions for the specific provider they're using.
_AUTH_GUIDANCE: dict[str, str] = {
    "anthropic": (
        "Set the ANTHROPIC_API_KEY environment variable, "
        "configure a key in Luc Nhan settings, "
        "or run `claude setup-token` for OAuth."
    ),
    "openai": ("Set the OPENAI_API_KEY environment variable or configure a key in Luc Nhan settings."),
    "gemini": (
        "Set the GOOGLE_API_KEY or GEMINI_API_KEY environment variable or configure a key in Luc Nhan settings."
    ),
    "minimax": ("Set the MINIMAX_API_KEY environment variable or configure a key in Luc Nhan settings."),
    "ollama": (
        "Ollama does not require an API key. "
        "Ensure the Ollama service is running (default: http://localhost:11434). "
        "You can set OLLAMA_BASE_URL to override the default address."
    ),
    "openai_compat": (
        "OpenAI-compatible providers require a configured API key "
        "and base URL in Luc Nhan settings. "
        "Use --api-base to set the endpoint URL."
    ),
}


def _auth_guidance_for(provider: str) -> str:
    """Return provider-specific guidance, or a generic fallback."""
    return _AUTH_GUIDANCE.get(
        provider, "Set the API key via the provider's environment variable or in Luc Nhan settings."
    )


class AuthenticationError(ProviderError):
    """Invalid or missing API key."""

    def __init__(
        self,
        message: str = "Invalid or missing API key",
        provider: str = "",
        guidance: str | None = None,
    ):
        if guidance is None and provider:
            guidance = _auth_guidance_for(provider)
        if guidance:
            message = f"{message} — {guidance}"
        super().__init__(message, provider=provider, status_code=401, retryable=False)
        self.guidance = guidance


class RateLimitError(ProviderError):
    """Rate limit exceeded."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        provider: str = "",
        retry_after: float = 0,
    ):
        super().__init__(message, provider=provider, status_code=429, retryable=True)
        self.retry_after = retry_after


class ContextLengthError(ProviderError):
    """Context window exceeded."""

    def __init__(self, message: str = "Context length exceeded", provider: str = ""):
        super().__init__(message, provider=provider, status_code=400, retryable=False)


class ToolError(LucNhanError):
    """Tool execution errors."""

    def __init__(self, message: str, tool_name: str = ""):
        super().__init__(message)
        self.tool_name = tool_name


class ToolNotFoundError(ToolError):
    """Requested tool does not exist."""


class ToolValidationError(ToolError):
    """Tool arguments failed validation."""


class AgentError(LucNhanError):
    """Agent loop errors."""


class CancellationError(AgentError):
    """Agent run was cancelled."""


class SessionError(LucNhanError):
    """Session/checkpoint errors."""


class UIError(LucNhanError):
    """UI-related errors."""


class SkillError(LucNhanError):
    """Skill loading or execution errors."""


class MCPError(LucNhanError):
    """MCP protocol errors."""


class MCPConnectionError(MCPError):
    """Failed to connect to an MCP server."""


class MCPTimeoutError(MCPError):
    """MCP request timed out."""
