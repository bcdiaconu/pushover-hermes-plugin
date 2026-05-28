"""Pushover test tool — registered via the pushover-hermes-plugin.

Sends a test push notification to verify that credentials and connectivity
are working. Callable from the Hermes prompt as:

    /pushover_test "Test message"
    or
    pushover_test(message="Test message")
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


def _check_pushover_test_available() -> bool:
    """Check if aiohttp is available and Pushover credentials exist."""
    try:
        import aiohttp  # noqa: F401
        return bool(
            os.getenv("PUSHOVER_APP_TOKEN", "").strip()
            and os.getenv("PUSHOVER_USER_KEY", "").strip()
        )
    except ImportError:
        return False


PUSHOVER_TEST_SCHEMA = {
    "name": "pushover_test",
    "description": (
        "Send a test push notification via Pushover to verify credentials and connectivity. "
        "Returns success/failure with the API response. Use this to test Pushover integration "
        "before sending real notifications."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The test message to send. Defaults to 'Pushover test successful.'",
            },
            "title": {
                "type": "string",
                "description": "Optional notification title. Defaults to 'Hermes Pushover Test'.",
            },
        },
        "required": [],
    },
}


async def _handle_pushover_test(args: Dict[str, Any]) -> str:
    """Send a test push notification via Pushover.

    Returns a JSON string with the result:
    - {"success": true, "request_id": "..."} on success
    - {"success": false, "error": "..."} on failure
    """
    try:
        import aiohttp
    except ImportError:
        return json.dumps({"success": False, "error": "aiohttp not installed. Run: pip install aiohttp"})

    app_token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    user_key = os.getenv("PUSHOVER_USER_KEY", "").strip()

    if not app_token or not user_key:
        return json.dumps({
            "success": False,
            "error": "Pushover credentials not configured. Set PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY env vars.",
        })

    message = args.get("message", "Pushover test successful.")
    title = args.get("title", "Hermes Pushover Test")

    # Truncate to Pushover's limit
    if len(message) > 1024:
        message = message[:1021] + "..."

    payload = {
        "token": app_token,
        "user": user_key,
        "message": message,
        "title": title,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(PUSHOVER_API_URL, data=payload) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("status") == 1:
                    return json.dumps({
                        "success": True,
                        "request_id": result.get("request"),
                        "message": "Test notification sent successfully.",
                    })
                errors = result.get("errors", [result.get("message", "Unknown error")])
                return json.dumps({
                    "success": False,
                    "error": str(errors[0] if isinstance(errors, list) else errors),
                })
    except aiohttp.ClientError as e:
        return json.dumps({"success": False, "error": f"HTTP error: {e}"})
    except Exception as e:
        return json.dumps({"success": False, "error": f"Unexpected error: {type(e).__name__}: {e}"})
