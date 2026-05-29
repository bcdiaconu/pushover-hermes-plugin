"""Pushover platform adapter — outbound notifications only.

Plugin-based gateway adapter that sends push notifications via the
Pushover API (https://pushover.net). Outbound-only — no inbound
message handling.

Also registers agent lifecycle hooks to send Pushover notifications
when the agent finishes processing, needs approval, asks questions,
blocks a kanban task, or needs a secret/API key.

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
    PUSHOVER_NOTIFY_STATES     — space-separated: finished, questions, errors,
                                approvals, blockers, all (default: "all")
    PUSHOVER_NOTIFY_DEVICE     — optional device filter for notifications
    PUSHOVER_LOG_LEVEL         — logging level override: DEBUG, INFO, WARNING,
                                ERROR. Defaults to logging.level from config.yaml.
"""

import atexit
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

# Dedicated plugin logger — writes to ~/.hermes/logs/pushover_hermes_plugin.log
_PLUGIN_LOG_DIR = Path.home() / ".hermes" / "logs"
_PLUGIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
_plugin_logger = logging.getLogger("pushover_plugin")

# Resolve log level: env var override > config.yaml logging.level > INFO
_plugin_log_level = os.getenv("PUSHOVER_LOG_LEVEL", "").upper()
if not _plugin_log_level:
    try:
        from hermes_cli.config import load_config as _load_hermes_config
        _cfg = _load_hermes_config()
        _lvl = (_cfg.get("logging") or {}).get("level", "")
        if _lvl:
            _plugin_log_level = str(_lvl).upper()
    except Exception:
        pass
if not _plugin_log_level:
    _plugin_log_level = "INFO"
_plugin_logger.setLevel(getattr(logging, _plugin_log_level, logging.INFO))
_plugin_handler = logging.FileHandler(_PLUGIN_LOG_DIR / "pushover_hermes_plugin.log", mode='a')
_plugin_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_plugin_logger.addHandler(_plugin_handler)

# Log module load — to stderr for visibility and to file
sys.stderr.write(f"[PUSHOVER_PLUGIN] Module loaded, home={Path.home()}, log_dir={_PLUGIN_LOG_DIR}\n")
sys.stderr.flush()
_plugin_logger.info("=== MODULE LOADED — home=%s, log_level=%s ===", Path.home(), _plugin_log_level)

# Track tool call start times for timing analysis
_TOOL_CALL_TIMES: Dict[str, float] = {}

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


def _load_env(env_path: str) -> Dict[str, str]:
    """Read key=value pairs from ~/.hermes/.env."""
    result: Dict[str, str] = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    return result


def _save_env(env_path: str, updates: Dict[str, str]) -> None:
    """Update key=value pairs in ~/.hermes/.env (upsert semantics)."""
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


def _resolve_current(var: str, env: Dict[str, str]) -> str:
    """Get current value: env var first, then .env file, then empty."""
    return os.getenv(var, env.get(var, ""))


def _load_current_config(env_path: str) -> Dict[str, Any]:
    """Load current Pushover config from env vars + .env file into a flat dict."""
    env = _load_env(env_path)
    cur_states = _resolve_current("PUSHOVER_NOTIFY_STATES", env) or "usual"
    usual_states = _USUAL_STATE_SET
    all_states = usual_states | {"post-approval"}
    if cur_states == "usual":
        cur_state_set = usual_states
    elif cur_states == "all":
        cur_state_set = all_states
    else:
        cur_state_set = set(cur_states.split())

    return {
        "env_path": env_path,
        "env": env,
        "app_token": _resolve_current("PUSHOVER_APP_TOKEN", env),
        "user_key": _resolve_current("PUSHOVER_USER_KEY", env),
        "allowed": _resolve_current("PUSHOVER_ALLOWED_USERS", env),
        "enabled": _resolve_current("PUSHOVER_NOTIFY_ENABLED", env).lower() in {"true", "1", "yes"},
        "question": _resolve_current("PUSHOVER_NOTIFY_QUESTION", env) or "full",
        "states": cur_states,
        "state_set": cur_state_set,
        "device": _resolve_current("PUSHOVER_NOTIFY_DEVICE", env),
        "usual_states": usual_states,
        "all_states": all_states,
    }


def _save_config(cfg: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Save wizard results to .env, writing only changed values.

    Both _setup_inquirer() and _setup_legacy() call this with their
    collected results.  The function computes the diff against the
    current config and performs the write.

    Args:
        cfg: Current config dict from _load_current_config().
        result: Wizard-collected values (same keys as cfg except
                without env/state_set/usual_states/all_states).
    """
    env_path = cfg["env_path"]
    updates: Dict[str, str] = {}

    # Credentials
    if result.get("app_token") != cfg["app_token"]:
        updates["PUSHOVER_APP_TOKEN"] = result["app_token"]
    if result.get("user_key") != cfg["user_key"]:
        updates["PUSHOVER_USER_KEY"] = result["user_key"]
    if result.get("allowed") != cfg["allowed"]:
        updates["PUSHOVER_ALLOWED_USERS"] = result["allowed"]

    # Notifications
    if result.get("enabled") != cfg["enabled"]:
        updates["PUSHOVER_NOTIFY_ENABLED"] = str(result["enabled"]).lower()

    if result.get("enabled"):
        if result.get("question") != cfg["question"]:
            updates["PUSHOVER_NOTIFY_QUESTION"] = result["question"]
        if result.get("states") != cfg["states"]:
            updates["PUSHOVER_NOTIFY_STATES"] = result["states"]
        if result.get("device") != cfg["device"]:
            updates["PUSHOVER_NOTIFY_DEVICE"] = result["device"]

    if not updates:
        print("  No changes.")
        return
    _save_env(env_path, updates)
    print(f"  Saved to {env_path}")


def interactive_setup() -> None:
    """Dispatch to the prompt_toolkit wizard or legacy fallback."""
    try:
        from prompt_toolkit.shortcuts import radiolist_dialog  # noqa: F401
        _plugin_logger.info("[SETUP] Starting prompt_toolkit wizard")
        _setup_prompt()
    except Exception as e:
        _plugin_logger.warning("[SETUP] prompt_toolkit unavailable (%s) — using legacy wizard", e)
        _plugin_logger.debug("[SETUP] prompt_toolkit error details:", exc_info=True)
        _setup_legacy()


def _setup_prompt() -> None:
    """Interactive setup wizard using prompt_toolkit."""
    from prompt_toolkit.shortcuts import input_dialog, radiolist_dialog, checkboxlist_dialog
    from prompt_toolkit.styles import Style

    # Use `default` so prompt_toolkit inherits terminal colors.
    # `reverse` on focus/checked uses the terminal's own highlight scheme.
    style = Style.from_dict({
        "dialog":                "bg:default fg:default",
        "dialog frame":          "fg:default",
        "dialog frame.label":    "fg:default bold",
        "dialog.body":           "bg:default fg:default",
        "dialog shadow":         "bg:default",
        "dialog border":         "fg:default",
        "text-input":            "bg:default fg:default",
        "text-input.text":       "bg:default fg:default",
        "text-input.placeholder":"fg:default",
        "searchfield":           "bg:default fg:default",
        "searchfield text":      "noinherit",
        "radio-selected":        "fg:default reverse bold",
        "button.focused":        "bg:default fg:default bold",
        "radio on":              "fg:default reverse bold",
        "radio off":             "noinherit",
        "checkbox on":           "fg:default reverse bold",
        "checkbox off":          "noinherit",
        "button":                "bg:default fg:default",
        "button.focused":        "fg:default reverse bold",
    })

    cfg = _load_current_config(os.path.expanduser("~/.hermes/.env"))

    print("\n─── 🔔 Pushover Setup ───")
    print("1. Log in at https://pushover.net")
    print("2. Copy your User Key (front page)")
    print("3. Create an app at https://pushover.net/apps → Create New Application")
    print("4. Copy the API Token\n")

    # Credentials
    app_token = input_dialog(
        title="Pushover Setup",
        text="App Token (from pushover.net/apps):",
        default=cfg["app_token"],
        style=style,
    ).run() or cfg["app_token"]

    user_key = input_dialog(
        title="Pushover Setup",
        text="User Key (from pushover.net front page):",
        default=cfg["user_key"],
        style=style,
    ).run() or cfg["user_key"]

    allowed = input_dialog(
        title="Pushover Setup",
        text="Allowed user keys (comma-separated, empty = allow all):",
        default=cfg["allowed"],
        style=style,
    ).run() or ""

    # Notifications
    print("\n─── Agent Lifecycle Notifications ───")
    print("Get Pushover alerts when Hermes finishes work, asks questions,")
    print("hits errors, or needs command approval.\n")

    enabled_choice = radiolist_dialog(
        title="Pushover Setup",
        text="Enable pushover notifications?",
        values=[
            ("yes", "Yes"),
            ("no", "No"),
        ],
        default="yes" if cfg["enabled"] else "no",
        style=style,
    ).run()
    notify_enabled = enabled_choice == "yes"

    question = cfg["question"]
    states_val = cfg["states"]
    device = cfg["device"]

    if notify_enabled:
        question = radiolist_dialog(
            title="Pushover Setup",
            text="Message detail level:",
            values=[
                ("full", "Full — include the actual question text"),
                ("summary", "Summary — include only the first line"),
                ("minimal", "Minimal — just 'I have a question' (privacy)"),
            ],
            default=cfg["question"],
            style=style,
        ).run()

        # States checkbox
        states_values = [
            ("finished", "Task finished"),
            ("questions", "Questions"),
            ("errors", "Errors"),
            ("pre-approval", "Pre-approval (command needs approval)"),
            ("blockers", "Blockers (task blocked)"),
            ("post-approval", "Post-approval (response recorded) [noisy]"),
        ]
        states_defaults = [v for v, _ in states_values if v in cfg["state_set"]]
        states_choice = checkboxlist_dialog(
            title="Pushover Setup",
            text="Which events should trigger notifications?",
            values=states_values,
            default_values=states_defaults,
            style=style,
        ).run()

        if set(states_choice) == cfg["usual_states"]:
            states_val = "usual"
        elif set(states_choice) == cfg["all_states"]:
            states_val = "all"
        else:
            states_val = " ".join(sorted(states_choice))

        device = input_dialog(
            title="Pushover Setup",
            text="Device filter (empty = all devices):",
            default=cfg["device"],
            style=style,
        ).run() or ""

    _save_config(cfg, {
        "app_token": app_token,
        "user_key": user_key,
        "allowed": allowed,
        "enabled": notify_enabled,
        "question": question,
        "states": states_val,
        "device": device,
    })


def _setup_legacy() -> None:
    """Interactive setup wizard using raw input() \u2014 no InquirerPy required."""
    cfg = _load_current_config(os.path.expanduser("~/.hermes/.env"))

    print()
    _print_panel(
        "\U0001f514 Pushover Setup",
        "1. Log in at https://pushover.net\n"
        "2. Your User Key is on the front page \u2014 copy it\n"
        "3. Create an app at https://pushover.net/apps \u2192 Create New Application\n"
        "4. Copy the API Token for your new app",
    )

    app_token = input(f"  App Token [{cfg['app_token']}]: ").strip() or cfg["app_token"]
    user_key = input(f"  User Key [{cfg['user_key']}]: ").strip() or cfg["user_key"]
    allowed = input(f"  Allowed user keys (empty = all) [{cfg['allowed']}]: ").strip() or cfg["allowed"]

    print()
    _print_panel(
        "\U0001f514 Agent Lifecycle Notifications",
        "Get Pushover alerts when Hermes finishes work, asks questions, "
        "hits errors, or needs command approval.",
    )

    ans = input(f"  Enable notifications [{'true' if cfg['enabled'] else 'false'}]: ").strip().lower()
    notify_enabled = ans in {"true", "yes", "1"} if ans else cfg["enabled"]

    question = cfg["question"]
    states_val = cfg["states"]
    device = cfg["device"]

    if notify_enabled:
        # Message detail
        print()
        print(f"  Message detail level [{cfg['question']}]:")
        print("    full     \u2014 include the actual question text")
        print("    summary  \u2014 include only the first line")
        print("    minimal  \u2014 just 'I have a question' (privacy)")
        q = input("  > ").strip().lower()
        question = q if q in {"full", "summary", "minimal"} else cfg["question"]

        # States (interactive checkbox fallback)
        state_choices = [
            {"name": "Task finished", "value": "finished", "selected": "finished" in cfg["state_set"]},
            {"name": "Questions", "value": "questions", "selected": "questions" in cfg["state_set"]},
            {"name": "Errors", "value": "errors", "selected": "errors" in cfg["state_set"]},
            {"name": "Pre-approval (command needs approval)", "value": "pre-approval", "selected": "pre-approval" in cfg["state_set"]},
            {"name": "Blockers (task blocked)", "value": "blockers", "selected": "blockers" in cfg["state_set"]},
            {"name": "Post-approval (response recorded) [noisy]", "value": "post-approval", "selected": "post-approval" in cfg["state_set"]},
        ]
        selected = _fallback_checkbox("Which events should trigger notifications?", state_choices)

        if set(selected) == cfg["usual_states"]:
            states_val = "usual"
        elif set(selected) == cfg["all_states"]:
            states_val = "all"
        else:
            states_val = " ".join(sorted(selected))

        device = input(f"  Device filter (empty = all) [{cfg['device']}]: ").strip() or cfg["device"]

    _save_config(cfg, {
        "app_token": app_token,
        "user_key": user_key,
        "allowed": allowed,
        "enabled": notify_enabled,
        "question": question,
        "states": states_val,
        "device": device,
    })


def _fallback_checkbox(message: str, choices: list) -> list:
    """Interactive checkbox fallback when InquirerPy is not available.

    Args:
        message: Prompt text.
        choices: List of dicts with 'name', 'value', 'selected' keys.

    Returns:
        List of selected values.
    """
    print()
    print(f"  {message}")
    print("  (Type number to toggle, 'd' when done, 'a' = all, 'u' = usual)")
    print()
    while True:
        for i, sc in enumerate(choices, 1):
            mark = " [x]" if sc["selected"] else " [ ]"
            print(f"    {mark} {i}. {sc['name']}")
        print()
        raw = input("  > ").strip().lower()
        if raw == "d":
            break
        elif raw == "a":
            for sc in choices:
                sc["selected"] = True
            break
        elif raw == "u":
            usual_values = _USUAL_STATE_SET
            for sc in choices:
                sc["selected"] = sc["value"] in usual_values
            break
        elif raw:
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(choices):
                    choices[idx]["selected"] = not choices[idx]["selected"]
                else:
                    print("  Invalid number.")
            except ValueError:
                print("  Invalid input.")
        print()
    return [sc["value"] for sc in choices if sc["selected"]]


def _print_panel(title: str, text: str) -> None:
    """Print a styled panel (fallback for InquirerPy panels)."""
    try:
        from wcwidth import wcswidth
    except ImportError:
        wcswidth = len

    lines = text.split("\n")
    inner_w = max(wcswidth(title), *(wcswidth(line) for line in lines))
    box_w = inner_w + 2  # 1 space padding on each side
    h, v, tl, tr, bl, br, ml, mr = "\u2550", "\u2551", "\u2554", "\u2557", "\u255a", "\u255d", "\u2560", "\u2563"

    def _pad(line: str, target: int) -> str:
        return line + " " * max(0, target - wcswidth(line))

    print(f" {tl}{h * box_w}{tr}")
    print(f" {v} {_pad(title, inner_w)} {v}")
    print(f" {ml}{h * box_w}{mr}")
    for line in lines:
        print(f" {v} {_pad(line, inner_w)} {v}")
    print(f" {bl}{h * box_w}{br}")


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
                    _plugin_logger.error("Pushover send failed: %s", errors[0])
                    return SendResult(success=False, error=str(errors[0]))
        except aiohttp.ClientError as e:
            _plugin_logger.error("Pushover HTTP error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_image(self, chat_id: str, image_url: str, caption: str = "") -> SendResult:
        content = f"{caption}\n\n{image_url}" if caption else image_url
        return await self.send(chat_id, content)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a clarify prompt to the user.

        Override to send Pushover notification BEFORE the tool blocks.
        """
        _plugin_logger.info("send_clarify called: question=%s, choices=%s, notify_enabled=%s",
                    question[:50], choices, _NOTIFY_ENABLED)
        _plugin_logger.debug("send_clarify full args: clarify_id=%s, session_key=%s", clarify_id, session_key)

        # Send notification for clarify questions
        if _NOTIFY_ENABLED and "questions" in _NOTIFY_STATE_SET:
            _plugin_logger.info("send_clarify: building notification (minimal=%s)", _NOTIFY_QUESTION)
            title, message = _build_clarify_notification({"question": question, "choices": choices or []})
            _plugin_logger.info("send_clarify: sending pushover notification: title=%s", title)
            _send_pushover_notification(title, message)
            _plugin_logger.info("send_clarify: pushover notification sent")
        else:
            _plugin_logger.info("send_clarify: skipping notification (enabled=%s, questions_in_set=%s)",
                        _NOTIFY_ENABLED, "questions" in _NOTIFY_STATE_SET)

        # Call parent to actually send the clarify prompt
        _plugin_logger.info("send_clarify: calling parent send_clarify")
        return await super().send_clarify(
            chat_id=chat_id,
            question=question,
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            metadata=metadata,
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        # BasePlatformAdapter.get_chat_info is abstract AND async — must match signature.
        return {"name": "Pushover", "type": "user", "chat_id": self._user_key}


# =============================================================================
# Agent lifecycle notifications
# =============================================================================
# Sends Pushover notifications when the agent yields control back to the user:
#   - post_llm_call             → agent finished processing (idle, question, or error)
#   - pre_approval_request      → agent needs command approval
#   - post_approval_response    → user responded to an approval request
#   - post_tool_call (clarify)  → agent asked a clarifying question with choices
#   - post_tool_call (kanban_block) → kanban task blocked awaiting user input
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
_NOTIFY_STATES = os.getenv("PUSHOVER_NOTIFY_STATES", "all").lower()  # space-separated states or "all" or "usual"
# "usual" preset: all common states except post-approval (exceptional, noisy)
_USUAL_STATE_SET = {"finished", "questions", "errors", "pre-approval", "blockers"}
_NOTIFY_STATE_SET = (
    set(_NOTIFY_STATES.split())
    if _NOTIFY_STATES not in {"all", "usual"}
    else (
        _USUAL_STATE_SET
        if _NOTIFY_STATES == "usual"
        else {"finished", "questions", "errors", "pre-approval", "post-approval", "blockers"}
    )
)


# Log initialization state to dedicated file (will appear once module is loaded)
def _log_notify_init():
    _plugin_logger.info(
        "[INIT] _NOTIFY_ENABLED=%s, _NOTIFY_QUESTION=%s, _NOTIFY_DEVICE=%s, _NOTIFY_STATES=%s, _NOTIFY_STATE_SET=%s",
        _NOTIFY_ENABLED, _NOTIFY_QUESTION, _NOTIFY_DEVICE, _NOTIFY_STATES, _NOTIFY_STATE_SET
    )
    _plugin_logger.debug("[INIT] env vars present: PUSHOVER_APP_TOKEN=%s, PUSHOVER_USER_KEY=%s, SUDO_PASSWORD=%s",
                        bool(os.getenv("PUSHOVER_APP_TOKEN")), bool(os.getenv("PUSHOVER_USER_KEY")),
                        "SUDO_PASSWORD" in os.environ)


atexit.register(_log_notify_init)


def _send_pushover_notification(title: str, message: str) -> bool:
    """Send a Pushover notification synchronously (for sync hook handlers).

    Uses ``requests`` (stdlib fallback via urllib) so hook handlers
    don't need an async event loop.  Hook callers ignore the return
    value — notifications never block the main flow.  The test command
    relies on the return value for round-trip verification.

    Returns:
        True if the Pushover API responded with status 200, False otherwise.
    """
    _plugin_logger.info("[PUSHOVER_SEND] ENTRY: title=%s, msg_len=%d", title, len(message))

    if not _NOTIFY_ENABLED:
        _plugin_logger.warning("[PUSHOVER_SEND] ABORT: _NOTIFY_ENABLED is False")
        return False

    app_token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user_key = os.getenv("PUSHOVER_USER_KEY", "").strip()
    _plugin_logger.info("[PUSHOVER_SEND] creds: app_token=%s, user_key=%s",
                        app_token[:6] + "..." if app_token else "(empty)",
                        user_key[:6] + "..." if user_key else "(empty)")
    if not app_token or not user_key:
        _plugin_logger.error("[PUSHOVER_SEND] ABORT: missing credentials")
        return False

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
        _plugin_logger.info("[PUSHOVER_SEND] using requests library")
        resp = _req.post(PUSHOVER_API_URL, data=payload, timeout=10)
        _plugin_logger.info("[PUSHOVER_SEND] HTTP status=%s, body=%s", resp.status_code, resp.text[:200])
        return resp.status_code == 200
    except ImportError:
        _plugin_logger.info("[PUSHOVER_SEND] requests not found, falling back to urllib")
        try:
            import urllib.parse as _urp
            import urllib.request as _ur
            data = _urp.urlencode(payload).encode("utf-8")
            resp = _ur.urlopen(PUSHOVER_API_URL, data=data, timeout=10)
            _plugin_logger.info("[PUSHOVER_SEND] urllib success: status=%s", resp.status)
            return resp.status == 200
        except Exception as e:
            _plugin_logger.error("[PUSHOVER_SEND] urllib FAILED: %s", e)
            return False
    except Exception as e:
        _plugin_logger.error("[PUSHOVER_SEND] requests FAILED: %s", e)
        return False


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


_SUDO_PLAIN_RE = re.compile(
    r"(?:^|[;&|`\n]|&&|\|\|)\s*sudo\b(?!\s+-(?:S|s|--stdin|--askpass))"
)


def _is_sudo_password_prompt(command: str) -> bool:
    """Detect `sudo` commands that will prompt for password."""
    result = bool(_SUDO_PLAIN_RE.search(command))
    _plugin_logger.info("[SUDO_CHECK] command=%s, regex_match=%s", command[:100], result)
    return result


def _notify_message(minimal: str, summary: str, full: str) -> str:
    """Select message text based on verbosity setting.

    Ensures consistent behaviour across all notification types —
    prevents bugs where a notification type forgets to respect
    PUSHOVER_NOTIFY_QUESTION.

    Args:
        minimal: Shorthand / privacy-safe message (no sensitive content).
        summary: Abbreviated message (truncated to ~200 chars).
        full: Complete message.

    Returns:
        The message appropriate for the current verbosity level.
    """
    if _NOTIFY_QUESTION == "minimal":
        return minimal
    if _NOTIFY_QUESTION == "summary":
        return summary
    return full


def _on_post_llm_call(**kwargs: Any) -> None:
    """Hook handler: fires after each agent LLM call.

    Sends a Pushover notification when the agent yields control back
    to the user.  Detects: finished/idle, clarifying questions, errors.
    Respects PUSHOVER_NOTIFY_STATES for per-state filtering.
    """
    response = str(kwargs.get("assistant_response") or "")

    # Diagnostic: log ALL kwargs keys and types to understand hook interface
    _plugin_logger.debug("POST_LLM kwargs=%s", list(kwargs.keys()))
    for k, v in kwargs.items():
        if k == "assistant_response":
            continue  # already logged separately
        v_repr = repr(v) if len(repr(v)) < 200 else repr(v)[:200] + "..."
        _plugin_logger.debug("  kwargs[%s] = %s (type=%s)", k, v_repr, type(v).__name__)

    _plugin_logger.debug("POST_LLM response_len=%d notify=%s preview=%s",
                        len(response), _NOTIFY_ENABLED, response[:300])

    if not _NOTIFY_ENABLED:
        return

    if not response.strip():
        return

    # Detect notification type and check state filter
    if _is_question(response):
        _plugin_logger.debug("  -> detected: question")
        if "questions" not in _NOTIFY_STATE_SET:
            _plugin_logger.warning("  -> skipped: questions not in state set")
            return
        question = _extract_question(response)
        title = "Hermes — Question"
        message = _notify_message(
            minimal="I have a question",
            summary=question[:200],
            full=question,
        )
    elif _has_error(response):
        if "errors" not in _NOTIFY_STATE_SET:
            _plugin_logger.warning("  -> skipped: errors not in state set")
            return
        error = _extract_error(response)
        title = "Hermes — Error"
        message = _notify_message(
            minimal="An error occurred",
            summary=error[:200],
            full=error[:500],
        )
    else:
        if "finished" not in _NOTIFY_STATE_SET:
            _plugin_logger.warning("  -> skipped: finished not in state set")
            return
        title = "Hermes"
        message = _notify_message(
            minimal="Finished",
            summary="Finished",
            full="Finished",
        )

    _send_pushover_notification(title, message)


def _on_pre_approval_request(**kwargs: Any) -> None:
    """Hook handler: fires when a dangerous command needs user approval.

    Respects PUSHOVER_NOTIFY_STATES for per-state filtering.
    """
    _plugin_logger.debug(
        "PRE_APPROVAL pattern=%s command=%s notify=%s pre_approval_in_set=%s",
        kwargs.get("pattern_key"),
        (kwargs.get("command") or "")[:80],
        _NOTIFY_ENABLED,
        "pre-approval" in _NOTIFY_STATE_SET,
    )
    if not _NOTIFY_ENABLED:
        return
    if "pre-approval" not in _NOTIFY_STATE_SET:
        return

    pattern_key = str(kwargs.get("pattern_key") or "")
    command = str(kwargs.get("command") or "")
    description = str(kwargs.get("description") or "")
    pattern_keys = kwargs.get("pattern_keys", [])

    # Build detailed message for summary/full
    parts = []
    if description:
        parts.append(description)
    if command:
        cmd_short = command[:200]
        parts.append(f"Command: {cmd_short}")
    if pattern_keys:
        parts.append(f"Patterns: {', '.join(pattern_keys[:3])}")
    detailed = "; ".join(parts) if parts else "A command needs your approval"

    message = _notify_message(
        minimal=f"Approval required for {pattern_key}" if pattern_key else "Approval required",
        summary=detailed,
        full=detailed,
    )

    _send_pushover_notification("Hermes — Approval Needed", message)


def _on_post_approval_response(**kwargs: Any) -> None:
    """Hook handler: fires after user responds to an approval request.

    Respects PUSHOVER_NOTIFY_STATES for per-state filtering.
    Post-approval is NOT included in the "usual" preset — only enabled
    explicitly when the user wants notification on every approval response.
    """
    if not _NOTIFY_ENABLED:
        return
    if "post-approval" not in _NOTIFY_STATE_SET:
        return

    choice = str(kwargs.get("choice") or "")
    if not choice:
        return

    # Only notify on non-trivial choices (allow/deny, not timeout/session)
    if choice in {"once", "always", "deny"}:
        pattern_key = str(kwargs.get("pattern_key") or "")
        command = str(kwargs.get("command") or "")
        cmd_short = command[:80] if command else ""

        verb = {"deny": "Denied", "always": "Always allow", "once": "Allowed once"}[choice]
        message = _notify_message(
            minimal=f"{verb}: {pattern_key}" if pattern_key else verb,
            summary=f"{verb}: {cmd_short}",
            full=f"{verb}: {cmd_short}",
        )
        _send_pushover_notification("Hermes — Approval Response", message)


def _build_clarify_notification(args: Dict[str, Any]) -> tuple[str, str]:
    """Build (title, message) for a clarify notification.

    Returns the formatted title and message for a Pushover notification
    based on the clarify tool arguments.
    """
    question = str(args.get("question") or "")
    choices = args.get("choices") or []
    has_choices = bool(choices)

    # Build detailed message with choices
    parts = [question]
    if has_choices:
        for i, choice in enumerate(choices, 1):
            parts.append(f"  {i}. {choice[:120]}")
        parts.append("  Other (type your answer)")
        title = f"Hermes — Clarification ({len(choices) + 1} options)"
    else:
        title = "Hermes — Clarification"
    detailed = "\n".join(parts)

    # Truncate for Pushover
    if len(detailed) > MAX_MESSAGE_LENGTH:
        detailed = detailed[: MAX_MESSAGE_LENGTH - 3] + "..."

    message = _notify_message(
        minimal="I need clarification",
        summary=question[:200],
        full=detailed,
    )

    return title, message


def _on_pre_tool_call(**kwargs: Any) -> None:
    """Hook handler: fires BEFORE every tool execution.

    Expected kwargs: tool_name, args, task_id, session_id, tool_call_id
    """
    _plugin_logger.debug("[PRE_TOOL] ENTRY - kwargs keys: %s", list(kwargs.keys()))

    tool_name = str(kwargs.get("tool_name") or "unknown")
    args = kwargs.get("args") or {}
    session_id = str(kwargs.get("session_id") or "")
    task_id = str(kwargs.get("task_id") or "")
    tool_call_id = str(kwargs.get("tool_call_id") or "")

    # Log every tool call
    _plugin_logger.debug(
        "[PRE_TOOL] tool=%s session=%s task=%s call=%s notify=%s | args_keys=%s",
        tool_name, session_id, task_id, tool_call_id, _NOTIFY_ENABLED, list(args.keys())
    )

    # Redact sensitive values in args
    args_redacted = {}
    for k, v in args.items():
        if k in ("api_key", "token", "password", "secret", "content"):
            args_redacted[k] = f"<{type(v).__name__}:{len(str(v))}>"
        else:
            args_redacted[k] = v
    _plugin_logger.debug("  args=%s", args_redacted)

    # Track start time
    if session_id:
        _TOOL_CALL_TIMES[f"{session_id}:{tool_call_id}:{tool_name}"] = time.time()

    # --- Early return: notifications disabled ---
    if not _NOTIFY_ENABLED:
        _plugin_logger.warning("[PRE_TOOL] ABORT: _NOTIFY_ENABLED is False")
        return

    _plugin_logger.info("[PRE_TOOL] Notifications ENABLED - continuing")

    # --- Clarify: notify BEFORE the tool blocks ---
    # Must come BEFORE the session_id guard — in CLI mode, session_id
    # may not be populated yet, but we still want to notify.
    if tool_name == "clarify" and "questions" in _NOTIFY_STATE_SET:
        _plugin_logger.info("[PRE_TOOL] sending clarify notification")
        title, message = _build_clarify_notification(args)
        _plugin_logger.debug("[PRE_TOOL] pushover: title=%s", title)
        _send_pushover_notification(title, message)
        _plugin_logger.info("[PRE_TOOL] pushover SENT for clarify")

    # --- Terminal sudo: notify if command will prompt for password ---
    if tool_name == "terminal":
        _plugin_logger.info("[PRE_TOOL] tool=terminal detected")
        command = str(args.get("command") or "")
        _plugin_logger.debug("[PRE_TOOL] terminal command=%s", command[:200])
        _plugin_logger.info(
            "[PRE_TOOL] terminal check: blockers_in_set=%s, sudo_password_in_env=%s, _NOTIFY_ENABLED=%s",
            "blockers" in _NOTIFY_STATE_SET,
            "SUDO_PASSWORD" in os.environ,
            _NOTIFY_ENABLED,
        )
        if "blockers" in _NOTIFY_STATE_SET:
            _plugin_logger.info("[PRE_TOOL] blockers IN state set - checking for sudo")
            is_sudo = _is_sudo_password_prompt(command)
            _plugin_logger.debug("[PRE_TOOL] _is_sudo_password_prompt returned: %s", is_sudo)
            if is_sudo:
                _plugin_logger.debug("[PRE_TOOL] SUDO PASSWORD PROMPT detected: %s", command[:80])
                msg = _notify_message(
                    minimal="Sudo password needed — the command will timeout without input",
                    summary=f"Sudo command requires password: {command[:120]}",
                    full=f"Sudo command requires password: {command[:300]}",
                )
                _plugin_logger.info("[PRE_TOOL] calling _send_pushover_notification for sudo")
                _send_pushover_notification(
                    "Hermes — Sudo Password Needed",
                    msg,
                )
                _plugin_logger.debug("[PRE_TOOL] _send_pushover_notification returned")
        else:
            _plugin_logger.debug("[PRE_TOOL] blockers NOT in state set - skipping sudo check")

    if not session_id:
        _plugin_logger.info("[PRE_TOOL] no session_id - returning early")
        return


def _on_post_tool_call(**kwargs: Any) -> None:
    """Hook handler: fires after every tool execution.

    Logs ALL tool completions to plugin log with timing analysis.
    Detects blocking tools that wait for user input:
      - kanban_block: kanban task blocked awaiting user input

    Note: clarify notifications are handled by _on_pre_tool_call instead,
    so the user gets notified instantly rather than after the tool times out.

    Respects PUSHOVER_NOTIFY_STATES for per-state filtering.

    Expected kwargs: tool_name, args, result, task_id, session_id, tool_call_id, duration_ms
    """
    tool_name = str(kwargs.get("tool_name") or "unknown")
    session_id = str(kwargs.get("session_id") or "")
    tool_call_id = str(kwargs.get("tool_call_id") or "")
    duration_ms = kwargs.get("duration_ms")

    # Log completion with timing
    if session_id:
        key = f"{session_id}:{tool_call_id}:{tool_name}"
        start = _TOOL_CALL_TIMES.pop(key, None)
        if start:
            elapsed = time.time() - start
            _plugin_logger.debug(
                "POST_TOOL [%s] tool=%s call=%s duration_ms=%s elapsed=%.2fs",
                session_id, tool_name, tool_call_id, duration_ms, elapsed
            )
        else:
            _plugin_logger.debug(
                "POST_TOOL [%s] tool=%s call=%s duration_ms=%s (no pre-timing)",
                session_id, tool_name, tool_call_id, duration_ms
            )
    else:
        _plugin_logger.warning("POST_TOOL tool=%s (no session_id)", tool_name)

    if not _NOTIFY_ENABLED:
        return

    args = kwargs.get("args") or {}

    # --- kanban_block ---
    if tool_name == "kanban_block":
        if "blockers" not in _NOTIFY_STATE_SET:
            return

        reason = str(args.get("reason") or "")
        task_id = str(args.get("task_id") or "")
        parts = []
        if task_id:
            parts.append(f"Task: {task_id}")
        if reason:
            parts.append(reason[:400])
        message = " | ".join(parts) if parts else "Kanban task blocked — needs your input"
        _send_pushover_notification("Hermes — Kanban Blocked", message)


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
    ctx.register_hook("post_approval_response", _on_post_approval_response)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)

    # Log registration confirmation
    _plugin_logger.info("=== Plugin hooks registered successfully ===")
    _plugin_logger.info("  post_llm_call: %s", _on_post_llm_call.__name__)
    _plugin_logger.info("  pre_approval_request: %s", _on_pre_approval_request.__name__)
    _plugin_logger.info("  post_approval_response: %s", _on_post_approval_response.__name__)
    _plugin_logger.info("  pre_tool_call: %s", _on_pre_tool_call.__name__)
    _plugin_logger.info("  post_tool_call: %s", _on_post_tool_call.__name__)


async def _handle_pushover_test_slash(raw_args: str) -> Optional[str]:
    """Handle /pushover-test <message> — send a test push notification.

    Uses the same code path as hook notifications so the result
    exercises the exact logic (_NOTIFY_ENABLED, credential checks,
    truncation, device targeting) that the plugin relies on.

    Usage:
        /pushover-test                    — sends default test message
        /pushover-test "Custom message"   — sends custom message
    """
    message = raw_args.strip() or "Pushover test successful."
    _plugin_logger.info("[SLASH /pushover-test] message=%s", message[:100])

    success = _send_pushover_notification("Hermes Pushover Test", message)
    if success:
        _plugin_logger.info("[SLASH /pushover-test] sent successfully")
        return "Pushover test notification sent successfully."
    # Give actionable reason instead of generic failure
    if not _NOTIFY_ENABLED:
        _plugin_logger.warning("[SLASH /pushover-test] aborted: notifications disabled")
        return "Notifications are disabled (PUSHOVER_NOTIFY=False). Enable them and try again."
    app_token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user_key = os.getenv("PUSHOVER_USER_KEY", "").strip()
    if not app_token or not user_key:
        _plugin_logger.warning("[SLASH /pushover-test] aborted: missing credentials")
        return "Pushover credentials not configured. Set PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY env vars."
    _plugin_logger.warning("[SLASH /pushover-test] failed — check plugin logs for details")
    return "Pushover send failed — check plugin logs for details."
