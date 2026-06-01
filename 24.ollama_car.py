#!/usr/bin/env python3
"""
Ollama-powered robot car — local LLM, no internet required.

Improvements over original:
  • Speed      — llama3.2:1b + shorter prompt = less inference wait time
  • Anti-loop  — parse_response tracks action history; flips turn direction
                 when the same turn repeats MAX_SAME_TURN times in a row
  • Smart avoid— is_too_close alternates L/R so the car doesn't spiral one
                 direction; after 4+ consecutive hits it runs a wide escape
  • Hints      — background stdin reader; type a message + Enter at any time
                 to guide the car — the robot never waits for you to type
"""

from picarx.llm import Ollama as LLM
from picarx.preset_actions import actions_dict, sounds_dict
from voice_active_car import VoiceActiveCar

import collections
import threading
import select
import sys
import urllib.request
import urllib.error
import time

# ─── Ollama ───────────────────────────────────────────────────────────────────
OLLAMA_IP    = "localhost"
# llama3.2:1b  → fastest, smallest  (good for quick reactions)
# llama3.2:3b  → smarter, a bit slower
OLLAMA_MODEL = "llama3.2:1b"

# ─── Robot ───────────────────────────────────────────────────────────────────
NAME         = "Buddy"
TOO_CLOSE    = 12          # cm — obstacle avoidance trigger distance

TTS_MODEL    = "en_US-ryan-low"
STT_LANGUAGE = "en-us"

WAKE_ENABLE    = True
WAKE_WORD      = [f"hey {NAME.lower()}"]
ANSWER_ON_WAKE = "Hi there!"
WELCOME        = f"Hi, I'm {NAME}. Running on Ollama — no internet needed!"

# After this many consecutive identical turns, flip to the opposite direction
MAX_SAME_TURN = 3

# Shorter prompt = fewer tokens Ollama has to generate = faster replies
INSTRUCTIONS = f"""You are {NAME}, a PiCar-X robot. Keep replies short and fun.

Actions you can do: {list(actions_dict.keys())}
Sounds you can make: {list(sounds_dict.keys())}

Always reply in EXACTLY this format:
YOUR_REPLY_TEXT
ACTIONS: action1, action2

No action needed → ACTIONS: stop"""


class SmartOllamaCar(VoiceActiveCar):
    """
    Extends VoiceActiveCar with:

    1. Anti-loop    — tracks recent actions; after MAX_SAME_TURN identical turns
                      in a row, automatically flips to the opposite direction.

    2. Smart avoid  — alternates L/R after each obstacle hit instead of always
                      turning the same way; runs a wide escape after 4+ hits.

    3. Hint trigger — background thread reads stdin without blocking; when you
                      type a message and press Enter it's forwarded to the LLM
                      as a trigger message on the next cycle.
    """

    # Maps each turn action to its opposite
    _FLIP = {
        "turn_right": "turn_left", "right": "left",
        "turn_left":  "turn_right", "left":  "right",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._action_hist = collections.deque(maxlen=8)
        self._obs_count   = 0
        self._hint        = None
        self._hint_lock   = threading.Lock()
        self._alive       = True

        # Register hint as a trigger so it feeds into the LLM automatically
        self.add_trigger(self._hint_trigger)

        # Non-blocking stdin reader — runs forever in the background
        threading.Thread(
            target=self._hint_reader, daemon=True, name="hint-reader"
        ).start()

    # ── hint plumbing ─────────────────────────────────────────────────────────

    def _hint_reader(self):
        """Waits for optional typed input; never blocks the main control loop."""
        sys.stdout.write(
            "\n[Hint mode: type a message + Enter at any time to guide the car]\n\n"
        )
        sys.stdout.flush()
        while self._alive:
            try:
                if select.select([sys.stdin], [], [], 0.3)[0]:
                    line = sys.stdin.readline().strip()
                    if line:
                        with self._hint_lock:
                            self._hint = line
                        print(f"[Hint queued → '{line}']\n")
            except Exception:
                break

    def _hint_trigger(self) -> tuple[bool, bool, str]:
        """Trigger: if a hint is pending, return it as an LLM message."""
        with self._hint_lock:
            if not self._hint:
                return False, False, ""
            msg, self._hint = f"[User guidance: {self._hint}]", None
        print(f"[Hint injected into LLM context]\n")
        return True, False, msg

    # ── smart obstacle avoidance ──────────────────────────────────────────────

    def is_too_close(self) -> tuple[bool, bool, str]:
        triggered, disable_image, message = super().is_too_close()
        if triggered:
            self._obs_count += 1
            # Alternate L/R on each obstacle hit — prevents corner-spiralling
            turn = "turn_right" if self._obs_count % 2 == 1 else "turn_left"
            self.action_flow.add_action(turn)
            print(f"[Obstacle #{self._obs_count}] Avoidance turn: {turn}")

            if self._obs_count >= 4:
                # Really stuck — execute a wider 3-step escape before resuming
                self.action_flow.add_action(turn, turn)
                self._obs_count = 0
                print("[Obstacle] Stuck! Running wide escape sequence")
        else:
            # Reset streak when path is clear
            self._obs_count = 0
        return triggered, disable_image, message

    # ── anti-loop response parsing ────────────────────────────────────────────

    def parse_response(self, text: str) -> str:
        parts = text.strip().split("ACTIONS:")
        response_text = parts[0].strip()

        if len(parts) > 1:
            actions = [a.strip() for a in parts[1].split(",") if a.strip()]
        else:
            actions = ["stop"]

        # Count how many consecutive times this exact turn appears at the end
        # of history; if too many, flip to the opposite direction
        if actions and actions[0] in self._FLIP:
            streak = 0
            for a in reversed(self._action_hist):
                if a == actions[0]:
                    streak += 1
                else:
                    break
            if streak >= MAX_SAME_TURN:
                flipped = self._FLIP[actions[0]]
                print(f"[Anti-loop] {streak}× '{actions[0]}' → forcing '{flipped}'")
                actions[0] = flipped

        self._action_hist.extend(actions)
        self.action_flow.add_action(*actions)
        return response_text

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def on_stop(self):
        self._alive = False
        super().on_stop()


# ─── Startup health check ─────────────────────────────────────────────────────

def _wait_for_ollama(ip: str, retries: int = 3, delay: float = 2.0) -> bool:
    """Ping Ollama's API endpoint; return True if reachable, False otherwise."""
    url = f"http://{ip}:11434/api/tags"
    for attempt in range(1, retries + 1):
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.URLError:
            if attempt < retries:
                print(f"[Ollama] Not reachable yet, retrying ({attempt}/{retries})…")
                time.sleep(delay)
    return False


# ─── Entry point ──────────────────────────────────────────────────────────────

if not _wait_for_ollama(OLLAMA_IP):
    print(
        "\n─────────────────────────────────────────────────\n"
        " Ollama is not running on this machine.\n"
        "\n"
        " Fix: open a separate terminal and run:\n"
        "   ollama serve\n"
        "\n"
        " Then make sure the model is downloaded:\n"
        f"   ollama pull {OLLAMA_MODEL}\n"
        "─────────────────────────────────────────────────\n"
    )
    sys.exit(1)

llm = LLM(ip=OLLAMA_IP, model=OLLAMA_MODEL)

car = SmartOllamaCar(
    llm,
    name=NAME,
    too_close=TOO_CLOSE,
    with_image=False,       # set True for a vision-capable model like llava
    stt_language=STT_LANGUAGE,
    tts_model=TTS_MODEL,
    keyboard_enable=False,  # disabled: hint thread manages all stdin instead
    wake_enable=WAKE_ENABLE,
    wake_word=WAKE_WORD,
    answer_on_wake=ANSWER_ON_WAKE,
    welcome=WELCOME,
    instructions=INSTRUCTIONS,
)

if __name__ == "__main__":
    car.run()
