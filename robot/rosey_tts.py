#!/usr/bin/env python3
"""Loud, simple text-to-speech for Rosey (no extra installs).

Synthesizes with libespeak-ng (preinstalled with speech-dispatcher), boosts the
result to near full scale and compresses it (espeak's raw output is much quieter
than music; set ROSEY_TTS_DRIVE=0..5 to change how hard), adds
a little leading silence so a Bluetooth speaker waking up doesn't clip the first
word, and plays it with paplay on the default PulseAudio output.

CLI:  python3 rosey_tts.py "Hello, I am Rosey"
"""
from __future__ import annotations

import array
import ctypes
import math
import os
import subprocess
import sys
import tempfile
import threading
import wave

ESPEAK_VOLUME = 2          # espeak_PARAMETER enum: espeakVOLUME
ESPEAK_RATE = 1            # words per minute
TARGET_PEAK = 0.95         # fraction of full scale after boosting
MAX_GAIN = 12.0            # never amplify more than this (avoids blowing up noise)
LEAD_SILENCE_S = 0.35
# Soft-clip compression: raises the quiet parts of each word so speech sounds
# louder at the same peak level. 0 = off, 3 = about +6 dB louder, 5 = harsher.
DRIVE = float(os.environ.get("ROSEY_TTS_DRIVE", 3))
# Speech loudness, 0-100 (% of full). This is the speech stream's own volume:
# the JLab speaker pins its hardware volume and overrides the system slider.
VOLUME = float(os.environ.get("ROSEY_TTS_VOLUME", 50))

_lock = threading.Lock()   # libespeak-ng is not thread-safe
_lib = None
_rate = 0
_samples = bytearray()

_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)


@_CB
def _collect(wav, n, _events):
    if n > 0:
        _samples.extend(ctypes.string_at(wav, n * 2))
    return 0


def _init():
    global _lib, _rate
    if _lib is None:
        lib = ctypes.CDLL("libespeak-ng.so.1")
        rate = lib.espeak_Initialize(1, 0, None, 0)    # 1 = AUDIO_OUTPUT_RETRIEVAL
        if rate <= 0:
            raise RuntimeError("espeak-ng failed to initialise")
        lib.espeak_SetSynthCallback(_collect)
        lib.espeak_SetParameter(ESPEAK_VOLUME, 200, 0)  # 0-200, default 100
        lib.espeak_SetParameter(ESPEAK_RATE, 160, 0)   # a bit slower than default 175: clearer
        _lib, _rate = lib, rate


def _espeak_pcm(text: str) -> "tuple[array.array, int]":
    with _lock:
        _init()
        _samples.clear()
        data = text.encode("utf-8")
        _lib.espeak_Synth(ctypes.c_char_p(data), len(data) + 1, 0, 1, 0, 0, None, None)
        _lib.espeak_Synchronize()
        pcm = array.array("h", bytes(_samples))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm, _rate


# -- Piper: natural neural voice, used when installed (pip3 install --user piper-tts)
# and a voice model (.onnx + .onnx.json) is in VOICES_DIR. Falls back to espeak.
VOICES_DIR = os.environ.get("ROSEY_VOICES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices"))
VOICE = os.environ.get("ROSEY_VOICE", "en_US-lessac-medium")
_piper = None
_piper_failed = False


def _piper_voice():
    global _piper, _piper_failed
    if _piper is None and not _piper_failed:
        model = os.path.join(VOICES_DIR, VOICE + ".onnx")
        try:
            from piper import PiperVoice
            if not os.path.exists(model):
                raise FileNotFoundError(model)
            _piper = PiperVoice.load(model)
        except Exception as exc:   # not installed / no model: use espeak instead
            _piper_failed = True
            print("rosey_tts: Piper unavailable (%s), using espeak" % exc, file=sys.stderr)
    return _piper


def _piper_pcm(voice, text: str) -> "tuple[array.array, int]":
    import io
    buf = io.BytesIO()
    with _lock:
        with wave.open(buf, "wb") as wf:
            if hasattr(voice, "synthesize_wav"):     # piper-tts >= 1.3
                voice.synthesize_wav(text, wf)
            else:                                    # piper-tts 1.2
                voice.synthesize(text, wf)
    buf.seek(0)
    with wave.open(buf, "rb") as wf:
        rate = wf.getframerate()
        pcm = array.array("h", wf.readframes(wf.getnframes()))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm, rate


def engine() -> str:
    return "piper:" + VOICE if _piper_voice() is not None else "espeak"


def synth(text: str, lead: bool = True) -> "tuple[array.array, int]":
    """Return (16-bit mono samples, sample rate), boosted, with lead-in silence if lead."""
    voice = _piper_voice()
    if voice is not None:
        pcm, rate = _piper_pcm(voice, text)
        drive = 0.0          # natural voice: just normalise, don't compress
    else:
        pcm, rate = _espeak_pcm(text)
        drive = DRIVE
    peak = max((abs(s) for s in pcm), default=0)
    if peak:
        gain = min(MAX_GAIN / 32767, 1.0 / peak)  # normalise to -1..1 (at most MAX_GAIN x)
        DRIVE_ = drive
        if DRIVE_ > 0:
            norm = TARGET_PEAK / math.tanh(DRIVE_)
            shape = lambda v: math.tanh(DRIVE_ * v) * norm
        else:
            shape = lambda v: v * TARGET_PEAK
        pcm = array.array("h", (int(32767 * max(-1.0, min(1.0, shape(s * gain)))) for s in pcm))
    silence = array.array("h", [0]) * int((LEAD_SILENCE_S if lead else 0.05) * rate)
    return silence + pcm, rate


# While Rosey is talking this file exists, so the microphone listener (a separate
# process) can ignore her own voice. It lives in /run/user/<uid> (cleared at boot).
SPEAKING_FLAG = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(),
                             "rosey-speaking")


def is_speaking(tail_s: float = 0.6) -> bool:
    """True while speaking, and for tail_s after (room echo, Bluetooth delay)."""
    try:
        st = os.stat(SPEAKING_FLAG)
    except OSError:
        return False
    until, pid = _read_flag()
    import time
    now = time.time()
    if until == 0.0:                       # "speaking now" -- unless its process died mid-sentence
        return _alive(pid) and now - st.st_mtime < 120
    return now < until + tail_s


def _read_flag():
    """(until, pid) from the flag file: until 0.0 means speaking right now."""
    try:
        with open(SPEAKING_FLAG) as f:
            parts = f.read().split()
        return float(parts[0]), (int(parts[1]) if len(parts) > 1 else 0)
    except (OSError, ValueError, IndexError):
        return -1.0, 0


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _set_flag(until: float) -> None:
    try:
        with open(SPEAKING_FLAG, "w") as f:
            f.write("%.3f %d" % (until, os.getpid()))
    except OSError:
        pass


def _speaker_awake(within_s: float = 4.0) -> bool:
    """PulseAudio suspends an idle Bluetooth speaker after ~5 s. If anything played in
    the last few seconds it's still awake, so the wake-up pause can be skipped."""
    import time
    until, pid = _read_flag()
    if until < 0:
        return False
    if until == 0.0:
        return _alive(pid)
    return time.time() - until < within_s


def _play(pcm, rate: int, timeout: float = 60) -> None:
    import time
    fd, path = tempfile.mkstemp(prefix="rosey-say-", suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        vol = int(65536 * max(0.0, min(100.0, VOLUME)) / 100)
        _set_flag(0.0)                      # 0 = "speaking now"
        try:
            res = subprocess.run(["paplay", "--volume=%d" % vol, path], capture_output=True, timeout=timeout)
        finally:
            _set_flag(time.time())          # finished at this time
        if res.returncode != 0:
            raise RuntimeError("paplay failed: %s" % res.stderr.decode(errors="replace").strip())
    finally:
        os.unlink(path)


def chime() -> None:
    """A short, soft two-note "I heard you" sound (about 0.3 s)."""
    rate = 22050
    lead = LEAD_SILENCE_S if not _speaker_awake() else 0.03
    out = array.array("h", [0]) * int(lead * rate)
    for freq, dur in ((660.0, 0.11), (880.0, 0.16)):
        n = int(dur * rate)
        for i in range(n):
            env = min(1.0, i / (0.01 * rate), (n - i) / (0.06 * rate))   # soft attack/release
            out.append(int(0.5 * 32767 * env * math.sin(2 * math.pi * freq * i / rate)))
    _play(out, rate, timeout=5)


def speak(text: str, timeout: float = 60, lead: bool = True) -> None:
    """Speak text on the default speaker; blocks until finished.

    lead=False skips the Bluetooth wake-up pause (for the 2nd+ sentence of a reply)."""
    import time
    text = text.strip()[:500]
    if not text:
        raise ValueError("no text")
    pcm, rate = synth(text, lead=lead and not _speaker_awake())
    _play(pcm, rate, timeout=timeout)


if __name__ == "__main__":
    speak(" ".join(sys.argv[1:]) or "Hello, I am Rosey.")
