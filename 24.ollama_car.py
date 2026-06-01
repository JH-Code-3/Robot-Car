#!/usr/bin/env python3
"""
Ollama-powered robot car — local LLM, no internet required.

Voice:    say "hey buddy" then give your command
Keyboard: just type and press Enter (VoiceAssistant handles it)
"""

from picarx.llm import Ollama as LLM
from picarx.preset_actions import actions_dict, sounds_dict
from voice_active_car import VoiceActiveCar

import collections
import re
import time
import urllib.request
import urllib.error
import sys

# ─── Settings ────────────────────────────────────────────────────────────────
OLLAMA_IP    = "localhost"
OLLAMA_MODEL = "llama3.2:1b"   # "llama3.2:3b" for smarter replies

NAME         = "Buddy"
TOO_CLOSE    = 12       # cm — obstacle trigger distance
DRIVE_SPEED  = 40
TURN_SPEED   = 35
MOVE_TIME    = 0.8      # seconds the car drives per movement command

TTS_MODEL    = "en_US-ryan-low"
STT_LANGUAGE = "en-us"

WAKE_ENABLE    = True
WAKE_WORD      = ["hey buddy"]
ANSWER_ON_WAKE = "Hi there!"
WELCOME        = "Hi, I'm Buddy. Say hey buddy to wake me up!"

MAX_SAME_TURN  = 3

# These are handled by driving the motors directly, not action_flow
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
Stopping now!
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

    def parse_response(self, text: str) -> str:
        print(f"[LLM] {repr(text[:200])}")

        # Case-insensitive ACTIONS: parsing
        match = re.search(r'actions:\s*(.+)', text, re.IGNORECASE)
        if match:
            actions = [a.strip().lower() for a in match.group(1).split(",") if a.strip()]
        else:
            print("[parse] no ACTIONS line found")
            actions = ["stop"]

        # Strip everything from ACTIONS: onward to get the reply text
        response_text = re.split(r'actions:', text, flags=re.IGNORECASE)[0].strip()
        # Remove literal template text if the model copied it
        response_text = re.sub(r'(?i)response_text', '', response_text).strip()

        # Anti-loop: flip a repeated turn direction
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
        """Drive the car motors directly — action_flow only handles gestures."""
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

    def is_too_close(self):
        triggered, disable_image, message = super().is_too_close()
        if triggered:
            self._obs_count += 1
            turn = "turn_right" if self._obs_count % 2 == 1 else "turn_left"
            self._drive(turn)
            print(f"[Obstacle #{self._obs_count}] avoidance: {turn}")
            if self._obs_count >= 4:
                self._drive(turn)
                self._drive(turn)
                self._obs_count = 0
                print("[Obstacle] wide escape done")
        else:
            self._obs_count = 0
        return triggered, disable_image, message


# ─── Ollama health check ──────────────────────────────────────────────────────

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
    print("\nOllama is not running. In another terminal: ollama serve\n")
    sys.exit(1)

# ─── Start ────────────────────────────────────────────────────────────────────

llm = LLM(ip=OLLAMA_IP, model=OLLAMA_MODEL)

car = SmartOllamaCar(
    llm,
    name=NAME,
    too_close=TOO_CLOSE,
    with_image=False,
    stt_language=STT_LANGUAGE,
    tts_model=TTS_MODEL,
    keyboard_enable=True,   # lets you type commands if voice isn't working
    wake_enable=WAKE_ENABLE,
    wake_word=WAKE_WORD,
    answer_on_wake=ANSWER_ON_WAKE,
    welcome=WELCOME,
    instructions=INSTRUCTIONS,
)

if __name__ == "__main__":
    car.run()
