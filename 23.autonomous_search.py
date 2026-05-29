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

# === Settings ===
DRIVE_SPEED    = 40    # 0-100
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

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
        max_tokens=80,
    )

    reply = response.choices[0].message.content.strip()
    history.append({"role": "assistant", "content": reply})

    # keep history short to control cost and latency
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
    print(f"\nSearching for: {target}")
    tts.say(f"Starting search for {target}. I'll let you know when I find it.")

    history: list = []

    try:
        while True:
            distance = get_distance()

            print(f"\nDistance: {distance}cm — asking GPT...")
            response = ask_gpt(target, distance, history)
            print(f"GPT: {response}")

            move, found, say = parse(response)

            if say:
                say_async(say)

            if found:
                px.stop()
                time.sleep(0.3)
                tts.say(f"I found it! I found the {target}!")
                print(f"\nTarget found: {target}")
                return

            # Safety: never drive forward into an obstacle
            if move == "forward" and distance < SAFE_DISTANCE:
                print("Safety override: too close, turning left instead")
                move = "turn_left"

            execute(move)
            time.sleep(LOOP_DELAY)

    except KeyboardInterrupt:
        px.stop()
        tts.say("Search cancelled.")
        print("\nSearch stopped.")

# ── entry point ──────────────────────────────────────────────────────────────

def main():
    print("=== Autonomous Search Robot ===")
    print("Type a target and the robot will go find it.")
    print("Press Ctrl+C during a search to cancel.\n")

    try:
        while True:
            target = input("What should I find? >>> ").strip()
            if not target:
                continue
            if target.lower() in ("quit", "exit", "q"):
                tts.say("Goodbye!")
                break
            search(target)
            print("\nSearch complete. Enter a new target or type 'quit' to exit.")
    finally:
        px.stop()
        px.set_dir_servo_angle(0)
        camera.stop()

if __name__ == "__main__":
    main()
