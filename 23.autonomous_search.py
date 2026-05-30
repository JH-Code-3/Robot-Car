#!/usr/bin/env python3
"""
Autonomous search robot: user names a target, GPT-4o navigates the car to find it.
"""

from picarx import Picarx
from picarx.tts import Piper
from picamera2 import Picamera2
from secret import OPENAI_API_KEY
import openai
import base64
import time
import threading
import sys
import termios

# === Settings ===
DRIVE_SPEED    = 40
TURN_SPEED     = 35
SAFE_DISTANCE  = 25   # cm — never move forward below this
MOVE_DURATION  = 0.5  # seconds per movement step
LOOP_DELAY     = 1.2  # seconds between GPT decisions
IMAGE_PATH     = "/tmp/search_frame.jpg"

# === Hardware ===
px  = Picarx()
tts = Piper()
tts.set_model("en_US-ryan-low")

camera = Picamera2()
camera.configure(camera.create_still_configuration(main={"size": (640, 480)}))
camera.start()
time.sleep(2)  # let camera warm up

# === OpenAI client ===
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Global flag so Ctrl+C during a GPT call still stops the search
_stop_search = threading.Event()

SYSTEM_PROMPT = """
You are the navigation brain of an autonomous PiCar-X robot car.
Your job is to find a target object by driving around and looking through the camera.

Each turn you receive:
- A camera image of what the robot currently sees
- The ultrasonic distance reading (cm) to the nearest obstacle in front
- The target to find

Respond ONLY in this exact format — no extra text, no explanation:
MOVE: <forward|backward|turn_left|turn_right|stop>
FOUND: <yes|no>
SAY: <short message, max 8 words>

Navigation rules:
- If distance < 25cm, never choose forward — turn instead
- If the target is clearly visible and takes up a large portion of the image, respond FOUND: yes
- Explore methodically — scan left and right, move forward when clear
- Vary your search pattern to cover new ground
- Keep SAY messages natural and brief
"""

# ── helpers ──────────────────────────────────────────────────────────────────

def flush_stdin():
    """Discard any keystrokes buffered before the prompt appears."""
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass

def get_distance() -> float:
    d = round(px.ultrasonic.read(), 1)
    return d if d > 0 else 999.0

def capture() -> str:
    camera.capture_file(IMAGE_PATH)
    return IMAGE_PATH

def image_to_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

def ask_gpt(target: str, distance: float, history: list) -> str:
    img_b64 = image_to_b64(capture())

    user_message = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"Target: {target}\nUltrasonic distance: {distance}cm\nWhat should I do next?"
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
            }
        ]
    }

    history.append(user_message)

    # Run the blocking API call in a thread so Ctrl+C can interrupt
    result = [None]
    def call():
        result[0] = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
            max_tokens=80,
        )

    t = threading.Thread(target=call, daemon=True)
    t.start()
    while t.is_alive():
        if _stop_search.is_set():
            return ""
        t.join(timeout=0.2)

    if result[0] is None:
        return ""

    reply = result[0].choices[0].message.content.strip()
    history.append({"role": "assistant", "content": reply})

    if len(history) > 16:
        history[:] = history[-16:]

    return reply

def parse(response: str) -> tuple[str, bool, str]:
    move, found, say = "stop", False, ""
    for line in response.splitlines():
        if line.startswith("MOVE:"):
            move = line.split(":", 1)[1].strip().lower()
        elif line.startswith("FOUND:"):
            found = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("SAY:"):
            say = line.split(":", 1)[1].strip()
    return move, found, say

def execute(move: str):
    if move == "forward":
        px.set_dir_servo_angle(0)
        px.forward(DRIVE_SPEED)
        time.sleep(MOVE_DURATION)
        px.stop()
    elif move == "backward":
        px.set_dir_servo_angle(0)
        px.backward(DRIVE_SPEED)
        time.sleep(MOVE_DURATION)
        px.stop()
    elif move == "turn_left":
        px.set_dir_servo_angle(-30)
        px.forward(TURN_SPEED)
        time.sleep(MOVE_DURATION)
        px.stop()
        px.set_dir_servo_angle(0)
    elif move == "turn_right":
        px.set_dir_servo_angle(30)
        px.forward(TURN_SPEED)
        time.sleep(MOVE_DURATION)
        px.stop()
        px.set_dir_servo_angle(0)
    else:
        px.stop()

def say_async(text: str):
    threading.Thread(target=tts.say, args=(text,), daemon=True).start()

# ── main search loop ─────────────────────────────────────────────────────────

def search(target: str):
    _stop_search.clear()
    print(f"\nSearching for: {target}")
    print("(Press Ctrl+C to cancel and pick a new target)\n")
    tts.say(f"Starting search for {target}.")

    history: list = []

    try:
        while not _stop_search.is_set():
            distance = get_distance()

            print(f"Distance: {distance}cm — asking GPT...")
            response = ask_gpt(target, distance, history)

            if _stop_search.is_set() or not response:
                break

            print(f"GPT: {response}\n")
            move, found, say = parse(response)

            if say:
                say_async(say)

            if found:
                px.stop()
                time.sleep(0.3)
                tts.say(f"I found it! I found the {target}!")
                print(f"Target found: {target}")
                return

            if move == "forward" and distance < SAFE_DISTANCE:
                print("Safety override: obstacle too close, turning instead")
                move = "turn_left"

            execute(move)
            time.sleep(LOOP_DELAY)

    except KeyboardInterrupt:
        _stop_search.set()

    px.stop()
    print("\nSearch stopped.")
    tts.say("Search cancelled.")

# ── entry point ──────────────────────────────────────────────────────────────

def main():
    # Print a clear banner after all hardware noise has settled
    print("\n" + "=" * 40)
    print("   AUTONOMOUS SEARCH ROBOT")
    print("=" * 40)
    print("Type a target to search for.")
    print("Type 'quit' to exit.\n")

    flush_stdin()  # throw away any keystrokes typed before this point

    while True:
        try:
            sys.stdout.write("What should I find? >>> ")
            sys.stdout.flush()
            target = sys.stdin.readline().strip()
        except KeyboardInterrupt:
            print("\nType 'quit' to exit.")
            flush_stdin()
            continue

        if not target:
            continue
        if target.lower() in ("quit", "exit", "q"):
            tts.say("Goodbye!")
            break
        search(target)
        flush_stdin()  # clear any keys pressed during the search
        print("\nEnter a new target or type 'quit' to exit.\n")

    px.stop()
    px.set_dir_servo_angle(0)
    camera.stop()

if __name__ == "__main__":
    main()
