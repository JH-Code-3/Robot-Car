#!/usr/bin/env python3
"""
Ollama-powered robot car — local LLM, no internet required.

Controls:
  m + Enter  — toggle microphone on/off  (starts MUTED to avoid noise)
  anything + Enter — send a typed command directly to the LLM
"""

from picarx.llm import Ollama as LLM
from picarx.preset_actions import actions_dict, sounds_dict
from voice_active_car import VoiceActiveCar

import collections
import threading
import select
import sys
import re
import urllib.request
import urllib.error
import time

# ─── Ollama ───────────────────────────────────────────────────────────────────
OLLAMA_IP    = "localhost"
OLLAMA_MODEL = "llama3.2:1b"   # use "llama3.2:3b" for smarter replies

# ─── Robot ───────────────────────────────────────────────────────────────────
NAME         = "Buddy"
TOO_CLOSE    = 12          # cm — obstacle avoidance trigger distance
DRIVE_SPEED  = 40
TURN_SPEED   = 35
MOVE_TIME    = 0.8         # seconds the car drives per movement command

TTS_MODEL    = "en_US-ryan-low"
STT_LANGUAGE = "en-us"

WAKE_ENABLE    = True
WAKE_WORD      = ["hey buddy"]
ANSWER_ON_WAKE = "Hi there!"
WELCOME        = "Hi, I'm Buddy. Press m then Enter to unmute the mic!"

MAX_SAME_TURN = 3

# Movement commands the car handles directly (bypasses action_flow)
MOVE_COMMANDS = {"forward", "backward", "turn_left", "turn_right", "stop"}

INSTRUCTIONS = """You are Buddy, a PiCar-X robot car. Short replies only.

Movement: forward, backward, turn_left, turn_right, stop
Gestures: shake head, nod, wave hands, resist, act cute, rub hands, think, twist body, celebrate, depressed
Sounds: honking, start engine

Reply in EXACTLY 2 lines:
<your short reply>
ACTIONS: <action1>, <action2>

Examples:
User: go forward
Zooming ahead!
ACTIONS: forward

User: turn right
Turning right!
ACTIONS: turn_right

User: hello
Hey there human!
ACTIONS: nod

User: stop
Stopping!
ACTIONS: stop

User: dance
Let me bust a move!
ACTIONS: twist body, celebrate

User: beep
BEEP BEEP!
ACTIONS: honking"""


class SmartOllamaCar(VoiceActiveCar):

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
        self._mic_on      = False   # starts MUTED — press m to enable
        self._alive       = True

        self.add_trigger(self._hint_trigger)
        threading.Thread(target=self._kbd_reader, daemon=True, name="kbd").start()

    # ── keyboard reader ───────────────────────────────────────────────────────

    def _kbd_reader(self):
        self._print_mic_state()
        while self._alive:
            try:
                if select.select([sys.stdin], [], [], 0.3)[0]:
                    line = sys.stdin.readline().strip()
                    if line.lower() == "m":
                        self._mic_on = not self._mic_on
                        self._print_mic_state()
                    elif line:
                        with self._hint_lock:
                            self._hint = line
                        print(f"[Command queued: '{line}']\n")
            except Exception:
                break

    def _print_mic_state(self):
        if self._mic_on:
            print("\n[MIC ON  — listening | press m + Enter to mute]\n")
        else:
            print("\n[MIC OFF — press m + Enter to unmute, or type a command]\n")

    def _hint_trigger(self):
        with self._hint_lock:
            if not self._hint:
                return False, False, ""
            msg, self._hint = f"[User says: {self._hint}]", None
        return True, False, msg

    # ── mute gate — drop voice input when mic is off ──────────────────────────

    def parse_response(self, text: str) -> str:
        # When mic is muted and this came from voice (not a typed hint),
        # the hint_trigger would have already fired for typed input.
        # We can't distinguish here perfectly, so we always process — the
        # mute mainly prevents accidental wake-word triggers from noise.

        print(f"[LLM] {repr(text[:120])}")

        match = re.search(r'actions:\s*(.+)', text, re.IGNORECASE)
        if match:
            actions = [a.strip().lower() for a in match.group(1).split(",") if a.strip()]
        else:
            actions = ["stop"]

        response_text = re.split(r'actions:', text, flags=re.IGNORECASE)[0].strip()
        # Strip any literal template text the small model might copy
        response_text = re.sub(r'(?i)response_text', '', response_text).strip()

        # Anti-loop: flip if the same turn repeats too many times
        if actions and actions[0] in self._FLIP:
            streak = 0
            for a in reversed(self._action_hist):
                if a == actions[0]:
                    streak += 1
                else:
                    break
            if streak >= MAX_SAME_TURN:
                flipped = self._FLIP[actions[0]]
                print(f"[Anti-loop] {streak}x '{actions[0]}' -> '{flipped}'")
                actions[0] = flipped

        self._action_hist.extend(actions)
        print(f"[Actions] {actions}")

        for action in actions:
            if action in MOVE_COMMANDS:
                self._drive(action)
            elif action:
                self.action_flow.add_action(action)

        return response_text

    def _drive(self, command: str):
        """Execute a movement command directly on the car motors."""
        if command == "forward":
            self.car.set_dir_servo_angle(0)
            self.car.forward(DRIVE_SPEED)
            time.sleep(MOVE_TIME)
            self.car.stop()
        elif command == "backward":
            self.car.set_dir_servo_angle(0)
            self.car.backward(DRIVE_SPEED)
            time.sleep(MOVE_TIME)
            self.car.stop()
        elif command == "turn_left":
            self.car.set_dir_servo_angle(-30)
            self.car.forward(TURN_SPEED)
            time.sleep(MOVE_TIME)
            self.car.stop()
            self.car.set_dir_servo_angle(0)
        elif command == "turn_right":
            self.car.set_dir_servo_angle(30)
            self.car.forward(TURN_SPEED)
            time.sleep(MOVE_TIME)
            self.car.stop()
            self.car.set_dir_servo_angle(0)
        elif command == "stop":
            self.car.stop()

    # ── smart obstacle avoidance ──────────────────────────────────────────────

    def is_too_close(self):
        triggered, disable_image, message = super().is_too_close()
        if triggered:
            self._obs_count += 1
            turn = "turn_right" if self._obs_count % 2 == 1 else "turn_left"
            self._drive(turn)
            print(f"[Obstacle #{self._obs_count}] {turn}")
            if self._obs_count >= 4:
                self._drive(turn)
                self._drive(turn)
                self._obs_count = 0
                print("[Obstacle] Stuck — wide escape done")
        else:
            self._obs_count = 0
        return triggered, disable_image, message

    # ── mute: suppress voice-triggered LLM calls when mic is off ─────────────

    def before_think(self, text):
        self.led.blink(delay=0.1)
        if not self._mic_on and "[User says:" not in text:
            # Noise fired the wake word while muted — swallow it
            print("[MIC OFF] ignoring voice input (press m to unmute)")
            # Return a canned non-response so the LLM isn't called
            # This relies on VoiceAssistant using before_think's return value;
            # if it doesn't, the mute is best-effort only.
            return "<<MUTED>>"
        return text

    def on_stop(self):
        self._alive = False
        super().on_stop()


# ─── Startup health check ─────────────────────────────────────────────────────

def _wait_for_ollama(ip: str, retries: int = 3, delay: float = 2.0) -> bool:
    url = f"http://{ip}:11434/api/tags"
    for attempt in range(1, retries + 1):
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.URLError:
            if attempt < retries:
                print(f"[Ollama] not ready, retry {attempt}/{retries}...")
                time.sleep(delay)
    return False


if not _wait_for_ollama(OLLAMA_IP):
    print(
        "\nOllama is not running.\n"
        "  Start it with: ollama serve\n"
        f"  Pull model with: ollama pull {OLLAMA_MODEL}\n"
    )
    sys.exit(1)

llm = LLM(ip=OLLAMA_IP, model=OLLAMA_MODEL)

car = SmartOllamaCar(
    llm,
    name=NAME,
    too_close=TOO_CLOSE,
    with_image=False,
    stt_language=STT_LANGUAGE,
    tts_model=TTS_MODEL,
    keyboard_enable=False,   # our kbd thread handles all stdin
    wake_enable=WAKE_ENABLE,
    wake_word=WAKE_WORD,
    answer_on_wake=ANSWER_ON_WAKE,
    welcome=WELCOME,
    instructions=INSTRUCTIONS,
)

if __name__ == "__main__":
    car.run()
