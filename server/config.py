"""Settings, read from environment variables (set them in the Render dashboard)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    # Shared secret the Jetson presents when it connects to /ws/robot.
    robot_token: str
    # Shared secret for people/apps controlling the robot (HTTP API, /ws/control, web page).
    operator_token: str
    # Every drive command expires on the robot after this long unless renewed
    # (dead-man switch). Clients may ask for up to drive_ttl_max_ms.
    drive_ttl_default_ms: int = 500
    drive_ttl_max_ms: int = 2000
    # How long HTTP callers wait for the robot to acknowledge a command.
    command_timeout_s: float = 5.0
    # How long a WebSocket client has to send its auth message.
    auth_timeout_s: float = 5.0
    # Optional: POST every robot event (e.g. fall_detected) here as JSON.
    alert_webhook_url: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Settings":
        robot = os.environ.get("ROBOT_TOKEN", "").strip()
        operator = os.environ.get("OPERATOR_TOKEN", "").strip()
        missing = [name for name, val in (("ROBOT_TOKEN", robot), ("OPERATOR_TOKEN", operator)) if not val]
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): %s. On Render these are generated "
                "by render.yaml; locally, export them before starting the server." % ", ".join(missing)
            )
        for name, val in (("ROBOT_TOKEN", robot), ("OPERATOR_TOKEN", operator)):
            if len(val) < 16:
                raise RuntimeError("%s must be at least 16 characters" % name)
        if robot == operator:
            raise RuntimeError("ROBOT_TOKEN and OPERATOR_TOKEN must be different")
        return cls(
            robot_token=robot,
            operator_token=operator,
            drive_ttl_default_ms=int(os.environ.get("DRIVE_TTL_DEFAULT_MS", 500)),
            drive_ttl_max_ms=int(os.environ.get("DRIVE_TTL_MAX_MS", 2000)),
            command_timeout_s=float(os.environ.get("COMMAND_TIMEOUT_S", 5.0)),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL") or None,
        )
