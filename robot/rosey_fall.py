"""Camera fall detection for Rosey.

Two stages, so it is cheap and doesn't cry wolf:

1. Watch (every detector pass, free): a person box that is wide and short, i.e.
   someone lying down rather than standing or sitting, seen in most checks over
   a few seconds.
2. Confirm (rare, one Claude call): the frame is shown to Claude, which says
   whether a person really appears to have fallen / be lying on the floor.

    fw = FallWatch()
    if fw.observe(img, detections, now):          # stage 1 says "check this"
        verdict = fw.confirm(jpeg_bytes)          # stage 2 (blocking, ~2 s)
        if verdict["fallen"]: ...alert...

Limits (camera only): it can't see falls out of view or behind furniture, a
person lying under a blanket on a couch may be flagged for confirmation, and
the detector misses some lying poses. The Myo armband is the backup for that.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from typing import Deque, Optional, Tuple

PERSON = 0
WINDOW_S = 3.0           # look at the last few seconds
MIN_HITS = 4             # ... and need this many "lying" sightings
MIN_RATIO = 0.6          # ... making up this share of the checks
RECHECK_AFTER_NO_S = 30  # Claude said "no fall": don't ask again this soon
REALERT_AFTER_S = 120    # after an alert, stay quiet this long

PROMPT = """You are the fall-detection check of a home-care robot for an older adult who lives alone. This photo is from the robot's camera, which sits low near the floor and looks forward and slightly up.

Does the photo show a person who has fallen or is lying on the floor (not on a bed or couch)? Sitting, crouching, kneeling on purpose, bending over, or a person only partly in view while standing is NOT a fall.

Answer with only one line of JSON: {"fallen": true or false, "confidence": 0.0-1.0, "description": "one short sentence for a caregiver"}"""


def lying_box(box: Tuple[float, float, float, float], w: int, h: int) -> bool:
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return False
    if x1 <= 2 and x2 >= w - 2:          # fills the whole width: a close-up torso, not a body on the floor
        return False
    return bw > 1.25 * bh and bh < 0.55 * h and y2 > 0.45 * h and bw * bh > 0.01 * w * h


class FallWatch:
    def __init__(self, model: Optional[str] = None):
        self.model = model or os.environ.get("ROSEY_CLAUDE_MODEL", "claude-opus-5-5")
        self.seen: Deque[Tuple[float, bool]] = deque()
        self.quiet_until = 0.0
        self.last_box = None
        self._client = None

    def observe(self, img, dets, now: Optional[float] = None) -> bool:
        """Feed one detector pass. True = looks like a fall, time to confirm."""
        now = time.monotonic() if now is None else now
        h, w = img.shape[:2]
        lying = [d.box for d in dets if d.cls == PERSON and d.score >= 0.4 and lying_box(d.box, w, h)]
        self.seen.append((now, bool(lying)))
        while self.seen and now - self.seen[0][0] > WINDOW_S:
            self.seen.popleft()
        if lying:
            self.last_box = max(lying, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        if now < self.quiet_until:
            return False
        hits = sum(1 for _, x in self.seen if x)
        return hits >= MIN_HITS and hits / len(self.seen) >= MIN_RATIO

    def confirm(self, jpeg: bytes, now: Optional[float] = None) -> dict:
        """Ask Claude about the frame. Never raises: on error it errs on the side of alerting."""
        now = time.monotonic() if now is None else now
        self.seen.clear()
        try:
            verdict = self._ask(jpeg)
        except Exception as exc:
            # Can't reach Claude: a strong, persistent "lying" signal is still worth an alert.
            import sys
            print("rosey_fall: double-check failed: %s: %s" % (type(exc).__name__, str(exc)[:300]),
                  file=sys.stderr, flush=True)
            verdict = {"fallen": True, "confidence": 0.5, "unconfirmed": True,
                       "description": "A person may be lying on the floor (couldn't double-check: %s)." % type(exc).__name__}
        fallen = bool(verdict.get("fallen")) and float(verdict.get("confidence", 0)) >= 0.5
        verdict["fallen"] = fallen
        self.quiet_until = now + (REALERT_AFTER_S if fallen else RECHECK_AFTER_NO_S)
        return verdict

    def _ask(self, jpeg: bytes) -> dict:
        import base64
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=1, timeout=20.0)
        msg = self._client.messages.create(
            model=self.model, max_tokens=500,   # room for thinking (120-200 tokens seen) before the JSON; 300 once left no answer
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(jpeg).decode()}},
                {"type": "text", "text": PROMPT}]}],
            output_config={"effort": "low"},
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("no JSON in answer (stop_reason=%s): %r" % (getattr(msg, "stop_reason", "?"), text[:200]))
        return json.loads(m.group(0))
