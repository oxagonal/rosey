#!/usr/bin/env python3
"""Talk with Rosey: USB mic -> Whisper (offline) -> Claude -> Piper voice.

Say "Rosey, ..." to start. After she answers you can reply without her name for
FOLLOW_UP_S seconds. While she is speaking the mic is ignored (no talking to herself).

Runs as a child of run_agent.py (so it shares the agent's restarts and logs), but is
a separate process so a crash here can never affect motor control. It prints one
JSON line per event on stdout; run_agent.py forwards "help" lines to the website.

Settings (environment, e.g. in agent.env):
    ANTHROPIC_API_KEY    needed for replies (without it: --echo test mode only)
    ROSEY_CLAUDE_MODEL   default claude-opus-5-5
    ROSEY_MIC            PulseAudio source name (default: the system default mic)
    ROSEY_WAKE_WORD      1 (default) = must say "Rosey"; 0 = answer everything heard

CLI:
    python3 rosey_converse.py           # full conversation
    python3 rosey_converse.py --echo    # test mic + speech-to-text: repeats what it heard
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np

import rosey_tts

HERE = os.path.dirname(os.path.abspath(__file__))
# Data that isn't in git (voices/, models/whisper/, agent.env) lives in the deployment
# folder, ~/Desktop/rosey, whose code files are symlinks into the repo. Python resolves
# symlinks for the script's folder, so don't rely on __file__ for data.
ROSEY_HOME = os.environ.get("ROSEY_HOME") or (
    os.path.expanduser("~/Desktop/rosey") if os.path.isdir(os.path.expanduser("~/Desktop/rosey/voices"))
    else os.path.dirname(os.path.abspath(__file__)))
RATE = 16000
FRAME = 480                       # 30 ms at 16 kHz
FRAME_S = FRAME / RATE

# Voice activity detection (energy based, adaptive to the room's background noise)
START_DB = 9.0                    # speech = this many dB above the noise floor...
START_FRAMES = 4                  # ...for this many frames in a row (120 ms)
END_SILENCE_S = 0.7               # utterance ends after this much quiet
PRE_ROLL_S = 0.4                  # keep audio from just before speech started
MAX_UTTERANCE_S = 10.0
MIN_UTTERANCE_S = 0.4
# "Nearest voice" end detection: the person talking to Rosey is close to the mic and
# loud; other voices in the room are quieter. Anything this many dB below the current
# speaker's level counts as silence, so background chatter can't keep an utterance open.
NEAR_VOICE_DROP_DB = 15.0

FOLLOW_UP_S = 10.0                # reply without the wake word for this long
HISTORY_TURNS = 20                # remembered exchanges
FORGET_AFTER_S = 30 * 60          # start a fresh conversation after 30 min quiet
WAKE_RE = re.compile(r"\b(rosey|rosie|rosy|rozy|rosi|roseanne|rose)\b", re.I)
HELP_RE = re.compile(r"\b(help me|help|i fell|i've fallen|i have fallen|fallen|can't get up|cannot get up|"
                     r"emergency|chest pain|can't breathe|cannot breathe|call 911|ambulance|stroke)\b", re.I)

MODEL = os.environ.get("ROSEY_CLAUDE_MODEL", "claude-opus-5-5")
SYSTEM = """You are Rosey, a friendly home robot that keeps an older adult who lives alone company and helps look after them. You are talking out loud: everything you write is spoken by a text-to-speech voice through a small speaker.

How to talk:
- Reply in one to three short, natural sentences, the way a kind person talks. Never use lists, headings, markdown, emoji, or symbols that can't be spoken.
- Be warm, patient and respectful; never talk down to the person. If you didn't catch something, ask them to say it again.
- The speech-to-text can mishear words. If a request seems odd, gently check what they meant.
- Write numbers and times the way you would say them, like "three thirty in the afternoon".
- You are given the current local time, your approximate location (town) and the live local weather in a system note. Only bring them up when the person asks or it clearly matters, for example suggesting a coat when it's cold out. The location comes from the internet connection and can be off by a town or two, so if asked where you are, say roughly where.

What you are and can do:
- You are a small three-wheeled robot with a speaker, a microphone and a camera. A caregiver can drive you, watch your camera and turn on a "follow" mode from a website.
- When your camera is working, the person's message comes with a photo your camera took a moment ago, plus a note on whether you are following someone. The camera sits low, near the floor, and looks forward and slightly up, so you often see legs, furniture and ceilings, and people from below. Use the photo when it helps: if they ask what you see, how they look, what they're holding, whether something is in the way, or if what you see suggests they may be hurt (for example someone lying on the floor). Don't describe the photo when they didn't ask and nothing is wrong. If the picture is too dark, blurry or doesn't show what they ask about, say so honestly rather than guessing.
- You can talk, listen, look, keep company, remind, and explain. You cannot make phone calls or control other devices. You cannot start or stop following by yourself; a caregiver does that from the website. Don't pretend otherwise.
- You are not a doctor. For health questions give simple, sensible information and suggest asking their doctor or pharmacist.

Safety comes first:
- If the person may be hurt, has fallen, has chest pain, trouble breathing, signs of a stroke, or says it's an emergency: tell them clearly to call 911 now, or to press their medical alert button if they have one, and say you have sent an alert to their caregiver. Keep it short and calm.
- If they seem confused, very upset, or talk about harming themselves, stay calm and kind, and encourage them to call someone they trust or 988 (the Suicide and Crisis Lifeline) right away."""

FALLBACK_REPLY = "I'm sorry, I couldn't think of an answer just now. Could you ask me again?"
OFFLINE_REPLY = "I'm sorry, I can't reach my brain on the internet right now. Please try again in a moment."


def emit(kind: str, **data) -> None:
    """One JSON line per event on stdout, for run_agent.py (and the log)."""
    print(json.dumps(dict(type=kind, time=round(time.time(), 3), **data)), flush=True)


def log(msg: str) -> None:
    print("rosey_converse: " + msg, file=sys.stderr, flush=True)


# -- microphone + voice activity detection --------------------------------------
class Mic:
    """Yields whole utterances (float32 arrays at 16 kHz) from PulseAudio."""

    def __init__(self, source: str | None = None):
        self.source = source or os.environ.get("ROSEY_MIC") or None
        self.noise_db = -50.0

    def _open(self):
        cmd = ["parec", "--raw", "--format=s16le", "--channels=1", "--rate=%d" % RATE,
               "--latency-msec=30"]
        if self.source:
            cmd.append("--device=" + self.source)
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def start(self, paused=lambda: False) -> "queue.Queue":
        """Read the mic continuously in a thread (so audio is judged as it arrives,
        never from a stale buffer) and put finished utterances on the returned queue."""
        q: "queue.Queue" = queue.Queue()

        def run():
            while True:
                try:
                    for utt in self.utterances(paused):
                        q.put(utt)
                except Exception as exc:
                    log("microphone problem: %s; retrying in 3 s" % exc)
                    time.sleep(3)

        threading.Thread(target=run, daemon=True).start()
        return q

    def utterances(self, paused=lambda: False):
        proc = self._open()
        pre = deque(maxlen=int(PRE_ROLL_S / FRAME_S))
        speech: list = []
        levels: list = []                 # dB of each frame in the current utterance
        loud_run = 0
        quiet_s = 0.0
        try:
            while True:
                # A pipe read can return part of a frame; keep reading until it's whole.
                raw = b""
                while len(raw) < FRAME * 2:
                    chunk = proc.stdout.read(FRAME * 2 - len(raw))
                    if not chunk:
                        raise RuntimeError("microphone stream ended (unplugged?)")
                    raw += chunk
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                db = 20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-6))
                if paused():
                    speech, loud_run, quiet_s = [], 0, 0.0
                    pre.clear()
                    continue
                if not speech:
                    # Track background noise slowly while nobody talks.
                    self.noise_db += (db - self.noise_db) * (0.05 if db > self.noise_db else 0.2)
                    self.noise_db = max(self.noise_db, -70.0)
                    pre.append(x)
                    loud_run = loud_run + 1 if db > self.noise_db + START_DB else 0
                    if loud_run >= START_FRAMES:
                        speech = list(pre)
                        levels = [db]
                        quiet_s = 0.0
                    continue
                speech.append(x)
                levels.append(db)
                speaker_db = float(np.percentile(levels, 80))     # how loud the talker is
                quiet_below = max(self.noise_db + START_DB * 0.6, speaker_db - NEAR_VOICE_DROP_DB)
                quiet_s = quiet_s + FRAME_S if db < quiet_below else 0.0
                dur = len(speech) * FRAME_S
                if quiet_s >= END_SILENCE_S or dur >= MAX_UTTERANCE_S:
                    if dur - quiet_s >= MIN_UTTERANCE_S:
                        yield np.concatenate(speech)
                    speech, levels, loud_run, quiet_s = [], [], 0, 0.0
                    pre.clear()
        finally:
            proc.kill()


# -- speech to text ------------------------------------------------------------
class Ears:
    def __init__(self):
        from faster_whisper import WhisperModel
        t = time.time()
        self.model = WhisperModel(os.environ.get("ROSEY_WHISPER", "base.en"), device="cpu",
                                  compute_type="int8", cpu_threads=4,     # 4 measured faster than 6
                                  download_root=os.path.join(ROSEY_HOME, "models", "whisper"))
        log("speech-to-text ready (%.1f s)" % (time.time() - t))

    def transcribe(self, audio) -> str:
        # No vad_filter: our own VAD already cut the audio (saves ~0.1-0.3 s).
        segs, _info = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                            condition_on_previous_text=False, without_timestamps=True)
        text = " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.6).strip()
        # Whisper's classic hallucinations on near-silence
        if text.lower().strip(" .!?") in {"", "you", "thank you", "thanks for watching", "bye"}:
            return ""
        return text


# -- talking: speak sentences as they stream in ----------------------------------
class Mouth:
    def __init__(self):
        self.q: "queue.Queue" = queue.Queue()
        self.busy = threading.Event()     # a reply is being spoken (or is about to be)
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, sentence: str, first: bool) -> None:
        self.busy.set()
        self.q.put((sentence, first))

    def chime(self) -> None:
        """Queue the "I heard you" chime; it plays while Claude is still thinking."""
        self.busy.set()
        self.q.put(("", "chime"))

    def done(self) -> None:
        self.q.put(None)                  # marks the end of one reply

    def wait(self) -> None:
        self.q.join()

    def _run(self):
        while True:
            item = self.q.get()
            try:
                if item is None:
                    self.busy.clear()
                    continue
                sentence, first = item
                try:
                    if first == "chime":
                        rosey_tts.chime()
                        continue
                    rosey_tts.speak(sentence, lead=first)
                except Exception as exc:
                    log("speech failed: %s" % exc)
            finally:
                self.q.task_done()


SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+")


def split_sentences(buf: str):
    """Split streamed text into complete sentences (end punctuation + whitespace)
    and the unfinished rest, so each sentence can be spoken as soon as it's done."""
    out = []
    while True:
        m = SENTENCE_END.search(buf)
        if not m:
            return out, buf
        sentence = buf[:m.end()].strip()
        if sentence:
            out.append(sentence)
        buf = buf[m.end():]


# -- what the camera sees (written by rosey_vision.py once a second) -------------
CAMERA_DIR = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
CAMERA_JPG = os.path.join(CAMERA_DIR, "rosey-camera.jpg")
CAMERA_JSON = os.path.join(CAMERA_DIR, "rosey-camera.json")
CAMERA_MAX_AGE_S = 5.0
FOLLOW_WORDS = {"searching": "looking for a person to follow", "following": "following a person",
                "arrived": "next to the person you follow, waiting", "lost": "lost the person you were following",
                "blocked": "following, but stopped because something is in the way",
                "stuck": "following, but paused because the wheels seemed blocked"}


def camera_content(heard: str):
    """The user turn: the camera's latest photo (if fresh) + a status line + what they said.
    Only the current turn carries the photo; history keeps plain text."""
    if os.environ.get("ROSEY_VISION_CONTEXT", "1") == "0":
        return heard
    try:
        with open(CAMERA_JSON) as f:
            info = json.load(f)
        if time.time() - float(info.get("time", 0)) > CAMERA_MAX_AGE_S:
            raise ValueError("stale")
        with open(CAMERA_JPG, "rb") as f:
            jpg = f.read()
    except (OSError, ValueError):
        return [{"type": "text", "text": "(Your camera isn't available right now.)"},
                {"type": "text", "text": heard}]
    fol = info.get("follow") or {}
    state = FOLLOW_WORDS.get(fol.get("state"), "not following anyone") if fol.get("enabled") else "not following anyone"
    import base64
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(jpg).decode()}},
        {"type": "text", "text": "(Photo from your camera, taken %.0f s ago. Right now you are %s.)"
                                 % (max(0.0, time.time() - info["time"]), state)},
        {"type": "text", "text": heard},
    ]


# -- the brain -----------------------------------------------------------------
class Brain:
    def __init__(self):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=2, timeout=60.0)
        self.history: list = []
        self.last_turn = 0.0
        from rosey_context import LiveContext
        self.context = LiveContext().start()     # location + weather, refreshed in the background

    def reply(self, heard: str, on_sentence) -> str:
        if time.time() - self.last_turn > FORGET_AFTER_S:
            self.history = []
        self.last_turn = time.time()
        now = datetime.now().strftime("%A, %B %-d, %Y, %-I:%M %p")
        # The clock goes in a mid-conversation system note (operator channel) so Claude
        # can answer "what time is it?" without bringing the time up unprompted.
        messages = self.history + [
            {"role": "user", "content": camera_content(heard)},
            {"role": "system", "content": ("Local time: %s.\n%s\nDon't mention the time, date, place or "
                                           "weather unless the person asks or it clearly matters."
                                           % (now, self.context.note())).replace("\n\n", "\n")},
        ]
        text_out = ""
        buf = ""
        try:
            with self.client.beta.messages.stream(
                model=MODEL,
                max_tokens=4000,                      # replies are a few spoken sentences
                system=SYSTEM,
                messages=messages,
                output_config={"effort": "low"},      # conversation: fast answers
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",                  # if declined, retry on a fallback model
            ) as stream:
                for delta in stream.text_stream:
                    buf += delta
                    sentences, buf = split_sentences(buf)
                    for s in sentences:
                        on_sentence(s)
                        text_out += s + " "
                final = stream.get_final_message()
            if buf.strip():
                on_sentence(buf.strip())
                text_out += buf.strip()
            if final.stop_reason == "refusal" and not text_out.strip():
                on_sentence(FALLBACK_REPLY)
                return FALLBACK_REPLY
            # Keep history as plain text turns (short and model-independent).
            self.history += [{"role": "user", "content": heard},
                             {"role": "assistant", "content": text_out.strip() or "(no answer)"}]
            self.history = self.history[-2 * HISTORY_TURNS:]
            return text_out.strip()
        except self.anthropic.AuthenticationError:
            log("Claude rejected the API key (check ANTHROPIC_API_KEY)")
            on_sentence(OFFLINE_REPLY)
            return OFFLINE_REPLY
        except (self.anthropic.APIConnectionError, self.anthropic.RateLimitError,
                self.anthropic.APIStatusError) as exc:
            log("Claude request failed: %s" % exc)
            if not text_out:
                on_sentence(OFFLINE_REPLY)
            return text_out.strip() or OFFLINE_REPLY


# -- main loop -----------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--echo", action="store_true", help="test mode: repeat what was heard, no Claude")
    args = ap.parse_args()

    wake_needed = os.environ.get("ROSEY_WAKE_WORD", "1") != "0"
    if not args.echo and not os.environ.get("ANTHROPIC_API_KEY"):
        log("ANTHROPIC_API_KEY is not set; conversation is off (use --echo to test the mic)")
        return 2

    rosey_tts.engine()                    # load the voice now
    ears = Ears()
    brain = None if args.echo else Brain()
    mouth = Mouth()
    mic = Mic()
    follow_up_until = 0.0
    log("listening%s (model %s)" % (' for "Rosey"' if wake_needed else "", "echo" if args.echo else MODEL))
    emit("ready", echo=args.echo, wake_word=wake_needed)

    thinking = threading.Event()
    paused = lambda: thinking.is_set() or mouth.busy.is_set() or rosey_tts.is_speaking()
    heard_q = mic.start(paused=paused)
    while True:
        audio = heard_q.get()
        t0 = time.time()
        heard = ears.transcribe(audio)
        if not heard:
            continue
        addressed = (not wake_needed) or WAKE_RE.search(heard) or time.time() < follow_up_until
        if not addressed:
            # Privacy: never log what people say when they aren't talking to Rosey.
            log("ignored %.1f s of speech (no wake word)" % (len(audio) / RATE))
            continue
        log("heard (%.1f s audio, %.1f s to text): %s" % (len(audio) / RATE, time.time() - t0, heard))
        emit("heard", text=heard)
        thinking.set()                    # don't listen while answering
        mouth.chime()                     # instant "I heard you"; doesn't wait
        if HELP_RE.search(heard):
            emit("help", text=heard)      # run_agent.py turns this into a website alert

        first = [True]

        def speak_sentence(s: str) -> None:
            if first[0]:
                log("first sentence ready %.1f s after speech ended" % (time.time() - t0 + END_SILENCE_S))
            mouth.say(s, first=first[0])
            first[0] = False

        if args.echo:
            speak_sentence("I heard: " + heard)
            said = "I heard: " + heard
        else:
            said = brain.reply(heard, speak_sentence)
            log("replied in %.1f s: %s" % (time.time() - t0, said))
        mouth.done()
        mouth.wait()
        thinking.clear()
        while not heard_q.empty():        # drop anything captured mid-reply
            heard_q.get_nowait()
        emit("said", text=said)
        follow_up_until = time.time() + FOLLOW_UP_S


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
