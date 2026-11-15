"""Request bodies and the message format shared with the Jetson agent.

Server -> robot messages (JSON over /ws/robot):
    {"type": "drive", "left": -1..1, "right": -1..1, "ttl_ms": 500}
    {"type": "stop", "reason": "..."}
    {"type": "<command>", "id": "<uuid>", ...}     any other command; robot replies with an ack

Robot -> server messages:
    {"type": "ack", "id": "<uuid>", "ok": true, "result": {...}}  /  {"ok": false, "error": "..."}
    {"type": "telemetry", ...}                     about once a second
    {"type": "event", "event": "fall_detected", ...}
"""

from __future__ import annotations

from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Command types that have their own endpoint or are part of the protocol itself.
RESERVED_TYPES = {"auth", "auth_ok", "auth_error", "ack", "telemetry", "event", "drive"}


def mix(linear: float, angular: float) -> Tuple[float, float]:
    """Differential drive: forward speed + turn rate (positive = turn left) -> wheel speeds."""
    left = linear - angular
    right = linear + angular
    scale = max(1.0, abs(left), abs(right))
    return left / scale, right / scale


class DriveRequest(BaseModel):
    """Either ``linear``+``angular`` or ``left``+``right``, all in -1..1."""

    model_config = ConfigDict(extra="forbid")

    linear: Optional[float] = Field(None, ge=-1, le=1, description="forward (+) / backward (-)")
    angular: Optional[float] = Field(None, ge=-1, le=1, description="turn left (+) / right (-)")
    left: Optional[float] = Field(None, ge=-1, le=1)
    right: Optional[float] = Field(None, ge=-1, le=1)
    ttl_ms: Optional[int] = Field(None, ge=50, description="stop automatically after this long")

    @model_validator(mode="after")
    def _one_style(self) -> "DriveRequest":
        twist = self.linear is not None or self.angular is not None
        wheels = self.left is not None or self.right is not None
        if twist == wheels:
            raise ValueError("give either linear/angular or left/right")
        return self

    def wheels(self) -> Tuple[float, float]:
        if self.left is not None or self.right is not None:
            return self.left or 0.0, self.right or 0.0
        return mix(self.linear or 0.0, self.angular or 0.0)


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


class CommandRequest(BaseModel):
    """Generic command passed through to the robot, e.g. {"type": "mode", "mode": "follow"}."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(..., min_length=1, max_length=64)

    @model_validator(mode="after")
    def _not_reserved(self) -> "CommandRequest":
        if self.type in RESERVED_TYPES:
            raise ValueError("'%s' is not allowed here (use /api/drive for driving)" % self.type)
        return self
