#!/usr/bin/env python3
"""Phone push notifications for Rosey's alerts, via ntfy (https://ntfy.sh).

Caregivers install the free ntfy app (iPhone / Android), tap "+", and subscribe to
Rosey's topic. Falls and help requests then arrive as urgent notifications with
the camera snapshot attached. The robot sends them itself, so they work even if
the control website is asleep or down.

The topic name is the only secret: anyone who knows it can read the alerts, so it
is long and random (generated on first run, saved in agent.env as ROSEY_NTFY_TOPIC).

Settings (agent.env):
    ROSEY_NTFY_TOPIC      topic name ("0" turns notifications off)
    ROSEY_NTFY_SERVER     default https://ntfy.sh (or your own ntfy server)
    ROSEY_NTFY_IMAGES     "0" = don't attach camera snapshots (text only)
    ROSEY_SITE_URL        opened when the notification is tapped

CLI:  python3 rosey_notify.py test     # send a test notification
      python3 rosey_notify.py info     # print the topic / subscribe link
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request

ROSEY_HOME = os.environ.get("ROSEY_HOME") or (
    os.path.expanduser("~/Desktop/rosey") if os.path.isdir(os.path.expanduser("~/Desktop/rosey/voices"))
    else os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROSEY_HOME, "agent.env")
SERVER = os.environ.get("ROSEY_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
SITE = os.environ.get("ROSEY_SITE_URL", "https://rosey-control.onrender.com")

ALERTS = {
    "fall_detected": ("Rosey: possible fall", "rotating_light", 5),
    "help_requested": ("Rosey: asked for help", "sos", 5),
}


def topic(create: bool = True) -> str:
    """The ntfy topic; creates one and saves it to agent.env the first time."""
    t = os.environ.get("ROSEY_NTFY_TOPIC", "").strip()
    if t or not create:
        return t
    t = "rosey-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:22]
    try:
        with open(ENV_FILE, "a") as f:
            f.write("\n# Phone notifications (ntfy app: subscribe to this topic). Keep it private.\nROSEY_NTFY_TOPIC=%s\n" % t)
        os.environ["ROSEY_NTFY_TOPIC"] = t
    except OSError:
        pass
    return t


def subscribe_link(t: str) -> str:
    return "%s/%s" % (SERVER, t)


def _ascii(text: str, limit: int = 400) -> str:
    """HTTP headers must be ASCII: swap common characters, drop the rest."""
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"') \
        .replace("–", "-").replace("—", "-").replace("°", " deg")
    return text.encode("ascii", "ignore").decode()[:limit].replace("\n", " ")


def send(title: str, message: str, priority: int = 3, tags: str = "", image: bytes = None,
         timeout: float = 15.0) -> int:
    t = topic()
    if not t or t == "0":
        return 0
    headers = {"Title": _ascii(title, 120), "Priority": str(priority), "Click": SITE, "User-Agent": "rosey-robot/1.0"}
    if tags:
        headers["Tags"] = tags
    url = subscribe_link(t)
    if image and os.environ.get("ROSEY_NTFY_IMAGES", "1") != "0":
        # Snapshot as an attachment; the text goes in the Message header.
        headers.update({"Message": _ascii(message), "Filename": "rosey.jpg"})
        req = urllib.request.Request(url, data=image, headers=headers, method="PUT")
    else:
        req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def notify_event(event: dict) -> bool:
    """Send a robot event (as given to agent.send_event) if it's an alert. Retries a few times."""
    name = event.get("event")
    if name not in ALERTS:
        return False
    title, tags, prio = ALERTS[name]
    desc = event.get("description") or event.get("text") or ""
    src = event.get("source")
    msg = (desc or title) + (" (from the %s)" % ("camera" if src == "camera" else src) if src else "")
    if event.get("unconfirmed"):
        msg += " Not double-checked."
    if name == "help_requested" and event.get("text"):
        msg = 'Heard: "%s"' % event["text"]
    msg += " " + time.strftime("%-I:%M %p")
    image = None
    img = event.get("image")
    if isinstance(img, str) and img.startswith("data:image/jpeg;base64,"):
        image = base64.b64decode(img.split(",", 1)[1])
    for attempt in range(3):
        try:
            return bool(send(title, msg, prio, tags, image))
        except Exception as exc:
            print("rosey_notify: send failed (%s), attempt %d" % (exc, attempt + 1), file=sys.stderr, flush=True)
            time.sleep(2 * (attempt + 1))
    return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    t = topic()
    print("ntfy topic: %s\nsubscribe: %s" % (t, subscribe_link(t)))
    if cmd == "test":
        print("sent:", send("Rosey: test notification", "Phone alerts are working. Tap to open the control page.",
                            3, "white_check_mark"))
