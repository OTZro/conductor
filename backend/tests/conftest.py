"""Test isolation from the developer's real .env.

conductor.config loads `.env` at import time, and real env vars beat file values
in pydantic-settings — so pinning these BEFORE any conductor import makes the
suite deterministic regardless of what the local .env says (auth flipped on,
remote hosts configured, …). Tests that need a feature ON monkeypatch settings
explicitly.
"""

from __future__ import annotations

import os

os.environ["CONDUCTOR_AUTH_ENABLED"] = "false"
os.environ["CONDUCTOR_REMOTE_HOSTS"] = ""
os.environ["CONDUCTOR_SLACK_TOKEN"] = ""
os.environ["CONDUCTOR_NTFY_TOPIC"] = ""
