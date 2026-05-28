"""Pushover platform adapter — outbound notifications only.

Plugin-based gateway adapter that sends push notifications via the
Pushover API (https://pushover.net). Outbound-only — no inbound
message handling.

Also registers agent lifecycle hooks to send Pushover notifications
when the agent finishes processing, needs approval, or has questions.

Configuration in config.yaml::

    gateway:
      platforms:
        pushover:
          enabled: true
          api_key: <PUSHOVER_APP_TOKEN>   # app token — PlatformConfig.api_key
          token: <PUSHOVER_USER_KEY>      # user key  — PlatformConfig.token
          extra:
            device: ""   # optional device filter

Lifecycle notification env vars:
    PUSHOVER_NOTIFY_ENABLED    — "true" to enable agent lifecycle notifications
    PUSHOVER_NOTIFY_QUESTION   — "full" (default), "summary", or "minimal"
                                controls what question text is included
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
MAX_MESSAGE_LENGTH = 1024


def check_requirements() -> bool:
    """Check that aiohttp is available.

    This is a package dependency check only — NOT a credential check.
    check_fn() is called by platform_registry.create_adapter(); returning
    False prevents adapter creation and shows install_hint. Credential
    validation belongs in is_connected() so the platform stays registered
    even when env vars aren't set yet.
    """
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False




def validate_config(config) -> bool:
    """Check whether Pushover is configured via env vars or config.yaml.

    Returns True if _any_ credential source is available. Adapter creation
    should not be blocked for an outbound-only platform — missing credentials
    are caught gracefully in send().
    """
    # Env vars always take precedence.
    if os.getenv("PUSHOVER_APP_TOKEN", "").strip() and os.getenv("PUSHOVER_USER_KEY", "").strip():
        return True
    # Fall back to config.yaml fields.
    token = getattr(config, "token", "") or ""
    api_key = getattr(config, "api_key", "") or ""
    return bool(token and api_key)


def is_connected(config) -> bool:
    """True if credentials are available from env or config.yaml."""
    if os.getenv("PUSHOVER_APP_TOKEN", "").strip() and os.getenv("PUSHOVER_USER_KEY", "").strip():
        return True
    return validate_config(config)


def interactive_setup() -> None:
    """Prompt for Pushover credentials and write them to ~/.hermes/.env.

    Called by ``hermes gateway setup`` when the user picks Pushover.
    Inlines prompts rather than importing _prompt_env_var from
    hermes_cli.gateway (private helper, fragile across upstream updates).
    Writes to ~/.hermes/.env — standard hermes env file location.
    """
    print()
    print("  \u2500\u2500\u2500 \U0001f514 Pushover Setup \u2500\u2500\u2500")
    print("  1. Log in at https://pushover.net")
    print("  2. Your User Key is on the front page \u2014 copy it")
    print("  3. Create an app at https://pushover.net/apps \u2192 Create New Application")
    print("  4. Copy the API Token for your new app")
    print()

    env_path = os.path.expanduser("~/.hermes/.env")
    updates: Dict[str, str] = {}

    for var, prompt in [
        ("PUSHOVER_APP_TOKEN", "Pushover App Token (from pushover.net/apps)"),
        ("PUSHOVER_USER_KEY",  "Pushover User Key (from pushover.net front page)"),
        ("PUSHOVER_ALLOWED_USERS", "Allowed user keys, comma-separated (leave empty = allow all)"),
    ]:
        current = os.getenv(var, "")
        display = f" [{current}]" if current else ""
        value = input(f"  {prompt}{display}: ").strip()
        if value:
            updates[var] = value

    if not updates:
        print("  No changes.")
        return

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    for var, value in updates.items():
        key_prefix = f"{var}="
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(key_prefix):
                lines[i] = f"{var}={value}\n"
                replaced = True
                break
        if not replaced:
            lines.append(f"{var}={value}\n")

    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    with open(env_path, "w") as f:
        f.writelines(lines)
    print(f"  Saved to {env_path}")


class PushoverAdapter(BasePlatformAdapter):
    """Outbound-only Pushover adapter.

    Sends push notifications to the Pushover API.
    No persistent connection — each send() opens a short-lived aiohttp session.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("pushover"))
        # Field mapping: PlatformConfig.api_key = PUSHOVER_APP_TOKEN (app token)
        #                PlatformConfig.token    = PUSHOVER_USER_KEY  (user key)
        # Confirmed from _apply_env_overrides(): api_key=pushover_app, token=pushover_user
        # and send_message_tool.py: _send_pushover(pconfig.api_key, pconfig.token, chunk)
        self._app_token: str = os.getenv("PUSHOVER_APP_TOKEN", "") or getattr(config, "api_key", "") or ""
        self._user_key: str = os.getenv("PUSHOVER_USER_KEY", "") or getattr(config, "token", "") or ""
        extra = getattr(config, "extra", {}) or {}
        self._device: str = extra.get("device", "") if isinstance(extra, dict) else ""

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # Pushover sends to a user key. For home-channel cron delivery,
        # chat_id may be empty — fall back to the configured user key.
        effective_user = chat_id or self._user_key

        if not self._app_token or not effective_user:
            return SendResult(
                success=False,
                error="Pushover credentials not configured (PUSHOVER_APP_TOKEN / PUSHOVER_USER_KEY)",
            )

        # Truncation is handled by send_message_tool before this is called,
        # but apply a hard cap as a safety net.
        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[: MAX_MESSAGE_LENGTH - 3] + "..."

        payload: Dict[str, str] = {
            "token": self._app_token,
            "user": effective_user,
            "message": content,
        }
        if self._device:
            payload["device"] = self._device
        if metadata and "title" in metadata:
            payload["title"] = metadata["title"]

        try:
            import aiohttp
        except ImportError:
            return SendResult(success=False, error="aiohttp not installed")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(PUSHOVER_API_URL, data=payload) as resp:
                    result = await resp.json()
                    if resp.status == 200 and result.get("status") == 1:
                        return SendResult(success=True, message_id=result.get("request"))
                    errors = result.get("errors", [result.get("message", "Unknown error")])
                    logger.error("Pushover send failed: %s", errors[0])
                    return SendResult(success=False, error=str(errors[0]))
        except aiohttp.ClientError as e:
            logger.error("Pushover HTTP error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_image(self, chat_id: str, image_url: str, caption: str = "") -> SendResult:
        content = f"{caption}\n\n{image_url}" if caption else image_url
        return await self.send(chat_id, content)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        # BasePlatformAdapter.get_chat_info is abstract AND async — must match signature.
        return {"name": "Pushover", "type": "user", "chat_id": self._user_key}


# =============================================================================
# Agent lifecycle notifications
# =============================================================================
# Sends Pushover notifications when the agent yields control back to the user:
#   - post_llm_call     → agent finished processing (idle, question, or error)
#   - pre_approval_request → agent needs command approval
#
# Enabled by PUSHOVER_NOTIFY_ENABLED=true.
# Question detail controlled by PUSHOVER_NOTIFY_QUESTION:
#   "full" (default) — include the actual question text
#   "summary"        — include only the first line of the question
#   "minimal"        — just "I have a question" (privacy mode)
# =============================================================================

# Env var toggles
_NOTIFY_ENABLED = os.getenv("PUSHOVER_NOTIFY_ENABLED", "").lower() in {"true", "1", "yes"}
_NOTIFY_QUESTION = os.getenv("PUSHOVER_NOTIFY_QUESTION", "full").lower()  # full|summary|minimal
_NOTIFY_DEVICE = os.getenv("PUSHOVER_NOTIFY_DEVICE", "")


def _send_pushover_sync(title: str, message: str) -> None:
    """Send a Pushover notification synchronously (for sync hook handlers).

    Uses ``requests`` (stdlib fallback via urllib) so hook handlers
    don't need an async event loop.  Fires and forgets — errors are
    silently swallowed so notifications never block the main flow.
    """
    app_token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user_key = os.getenv("PUSHOVER_USER_KEY", "").strip()
    if not app_token or not user_key:
        return

    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - 3] + "..."

    payload = {
        "token": app_token,
        "user": user_key,
        "message": message,
        "title": title,
    }
    if _NOTIFY_DEVICE:
        payload["device"] = _NOTIFY_DEVICE

    try:
        import requests as _req
        _req.post(PUSHOVER_API_URL, data=payload, timeout=10)
    except ImportError:
        # Fallback to urllib if requests is not available
        try:
            import urllib.parse as _urp
            import urllib.request as _ur
            data = _urp.urlencode(payload).encode("utf-8")
            _ur.urlopen(PUSHOVER_API_URL, data=data, timeout=10)
        except Exception:
            pass
    except Exception:
        pass  # Fire-and-forget — never block the hook


def _is_question(response: str) -> bool:
    """Heuristic: does the response contain a clarifying question?"""
    if "?" in response:
        return True
    lower = response.lower()
    return any(phrase in lower for phrase in [
        "i have a question",
        "could you clarify",
        "can you clarify",
        "please clarify",
        "need clarification",
        "which of these",
        "please confirm",
    ])


def _extract_question(response: str) -> str:
    """Extract the question text from the response."""
    for line in response.split("\n"):
        line = line.strip()
        if "?" in line and len(line) > 5:
            return line
    return response.split("\n")[0].strip() if response.strip() else response


def _has_error(response: str) -> bool:
    """Heuristic: does the response indicate an error?"""
    lower = response.lower()
    error_indicators = [
        "error:", "failed to", "failed:", "unable to", "cannot ",
        "⚠️", "error occurred", "execution error",
    ]
    return any(ind in lower for ind in error_indicators)


def _extract_error(response: str) -> str:
    """Extract the error message from the response."""
    for line in response.split("\n"):
        line = line.strip()
        lower = line.lower()
        if any(ind in lower for ind in ["error:", "failed:", "unable to", "cannot "]):
            return line
    return response.split("\n")[0].strip()


def _on_post_llm_call(**kwargs: Any) -> None:
    """Hook handler: fires after each agent turn.

    Sends a Pushover notification when the agent yields control back
    to the user.  Detects: finished/idle, clarifying questions, errors.
    """
    if not _NOTIFY_ENABLED:
        return

    response = str(kwargs.get("assistant_response") or "")
    if not response.strip():
        return

    # Detect notification type
    if _is_question(response):
        question = _extract_question(response)
        if _NOTIFY_QUESTION == "minimal":
            title = "Hermes — Question"
            message = "I have a question"
        elif _NOTIFY_QUESTION == "summary":
            title = "Hermes — Question"
            message = question[:200]
        else:  # full
            title = "Hermes — Question"
            message = question
    elif _has_error(response):
        error = _extract_error(response)
        title = "Hermes — Error"
        message = error[:500]
    else:
        title = "Hermes"
        message = "finished"

    _send_pushover_sync(title, message)


def _on_pre_approval_request(**kwargs: Any) -> None:
    """Hook handler: fires when a dangerous command needs user approval."""
    if not _NOTIFY_ENABLED:
        return

    command = str(kwargs.get("command") or "")
    description = str(kwargs.get("description") or "")
    pattern_keys = kwargs.get("pattern_keys", [])

    parts = []
    if description:
        parts.append(description)
    if command:
        cmd_short = command[:200]
        parts.append(f"Command: {cmd_short}")
    if pattern_keys:
        parts.append(f"Patterns: {', '.join(pattern_keys[:3])}")

    message = "; ".join(parts) if parts else "A command needs your approval"

    _send_pushover_sync("Hermes — Approval Needed", message)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    ctx.register_platform(
        name="pushover",
        label="Pushover",
        adapter_factory=PushoverAdapter,
        check_fn=check_requirements,
        # No validate_config — this is outbound-only. The adapter checks
        # credentials in send() and returns a descriptive SendResult error
        # if they're missing. Blocking adapter creation here is unnecessary.
        is_connected=is_connected,
        required_env=["PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY"],
        install_hint="pip install aiohttp",  # shown only if aiohttp is missing
        setup_fn=interactive_setup,
        allowed_users_env="PUSHOVER_ALLOWED_USERS",
        allow_all_env="PUSHOVER_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="\U0001f514",
        pii_safe=True,
        allow_update_command=False,
        cron_deliver_env_var="PUSHOVER_HOME_CHANNEL",
        platform_hint=(
            "You are sending a Pushover push notification. Keep messages very short \u2014 "
            "Pushover notifications are brief alerts, max 1024 characters. No markdown "
            "rendering. Use plain text. Pushover is fire-and-forget; do not expect a reply."
        ),
    )

    # Register slash command /pushover-test
    ctx.register_command(
        "pushover-test",
        handler=_handle_pushover_test_slash,
        description="Send a test push notification via Pushover.",
        args_hint="<message>",
    )

    # Register agent lifecycle notification hooks
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_approval_request", _on_pre_approval_request)


async def _handle_pushover_test_slash(raw_args: str) -> Optional[str]:
    """Handle /pushover-test <message> — send a test push notification.

    Usage:
        /pushover-test                    — sends default test message
        /pushover-test "Custom message"   — sends custom message
    """
    import json
    import aiohttp

    app_token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user_key = os.getenv("PUSHOVER_USER_KEY", "").strip()

    if not app_token or not user_key:
        return "Pushover credentials not configured. Set PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY env vars."

    message = raw_args.strip() or "Pushover test successful."
    if len(message) > 1024:
        message = message[:1021] + "..."

    payload = {
        "token": app_token,
        "user": user_key,
        "message": message,
        "title": "Hermes Pushover Test",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(PUSHOVER_API_URL, data=payload) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("status") == 1:
                    return f"Pushover test sent successfully (request: {result.get('request')})"
                errors = result.get("errors", [result.get("message", "Unknown error")])
                return f"Pushover send failed: {errors[0]}"
    except aiohttp.ClientError as e:
        return f"Pushover HTTP error: {e}"
    except Exception as e:
        return f"Pushover error: {type(e).__name__}: {e}"
