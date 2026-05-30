#!/usr/bin/env python3
"""
Autonomous search robot.
- Colors/faces: real-time Vilib detection (no internet)
- Objects: GPT-4o with persistent session memory
- Camera pan/tilt scans while driving (stare_at_you style)
- Realistic movement: forward + deliberate coverage turns
- Live hint input during search + robot asks when stuck
- Session memory: remembers ALL objects seen, not just target
- Victory music on find, shutdown music on quit
- Cliff detection (grayscale sensors) — highest priority safety
- Obstacle avoidance (ultrasonic) — turns toward the clearer side
"""

from picarx import Picarx
from vilib import Vilib
from picarx.tts import Piper
from picarx.music import Music
from pathlib import Path
from secret import OPENAI_API_KEY
import openai
import base64
import time
import threading
import sys
import termios
import os
import wave
import struct
import math
import tempfile
from enum import Enum
from collections import deque

# ── Settings ──────────────────────────────────────────────────────────────────
DRIVE_SPEED      = 40
TURN_SPEED       = 35
SAFE_DISTANCE    = 25     # cm
FRAME_W, FRAME_H = 640, 480
FRAME_CX         = FRAME_W // 2
FRAME_CY         = FRAME_H // 2
FOUND_WIDTH      = 170    # px blob width = close enough
CONTROL_HZ       = 0.05   # 20 Hz movement loop
GPT_INTERVAL     = 2.5    # s between GPT vision calls
STRUGGLE_TIMEOUT = 20.0   # s without progress before asking for help
COVERAGE_TIMEOUT = 4.0    # s driving forward before a deliberate turn
PLANNED_TURN_SEC = 0.7    # s per coverage turn

COLOR_NAMES = {"red", "orange", "yellow", "green", "blue", "purple"}

class Mode(Enum):
    COLOR  = "color"
    FACE   = "face"
    OBJECT = "object"

# ── Hardware ──────────────────────────────────────────────────────────────────
px    = Picarx()
tts   = Piper()
music = Music()
tts.set_model("en_US-ryan-low")
# Cliff and servo calibration values are loaded automatically from the
# picarx config file — run 1.cali_grayscale.py and 1.cali_servo_motor.py
# on the Pi to set them correctly for your surface and hardware.

Vilib.camera_start(vflip=False, hflip=False)
Vilib.display(local=False, web=True)
time.sleep(1)

client    = openai.OpenAI(api_key=OPENAI_API_KEY)
SONG_PATH = Path(__file__).parent / "music" / "song.mp3"

# ── Shared state ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_s = {
    "mode":          Mode.COLOR,
    "detected":      False,
    "obj_x":         FRAME_CX,
    "obj_y":         FRAME_CY,
    "obj_w":         0,
    "cam_pan":       0.0,
    "cam_tilt":      0.0,
    "distance":      999.0,
    "gpt_move":      "forward",
    "last_progress": time.time(),
    "cliff":         False,   # True when grayscale sensors detect a drop
}

def sg(key):
    with _lock: return _s[key]

def ss(key, val):
    with _lock: _s[key] = val

def clamp(v, lo, hi): return max(lo, min(hi, v))

# ── Stdin — single reader thread ──────────────────────────────────────────────
# _searching=True  → lines go to _hint_queue (used by GPT thread)
# _searching=False → lines go to _main_queue (used by main thread)
_searching   = False
_hint_queue  = deque()
_main_queue  = deque()
_stdin_stop  = threading.Event()

def _stdin_reader():
    while not _stdin_stop.is_set():
        try:
            line = sys.stdin.readline().strip()
            if line:
                if _searching:
                    _hint_queue.append(line)
                    print(f"\n  [hint received] {line}\n", flush=True)
                else:
                    _main_queue.append(line)
        except Exception:
            break

def _get_input(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    while not _main_queue and not _stdin_stop.is_set():
        time.sleep(0.05)
    return _main_queue.popleft() if _main_queue else ""

def _flush():
    _hint_queue.clear()
    _main_queue.clear()
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass

# ── Music ─────────────────────────────────────────────────────────────────────

def _gen_tune(freqs, dur=0.25, gap=0.04):
    rate, samples = 44100, []
    for f in freqs:
        n = int(rate * dur)
        for i in range(n):
            env = math.sin(math.pi * i / n)
            samples.append(int(32767 * env * math.sin(2 * math.pi * f * i / rate)))
        samples.extend([0] * int(rate * gap))
    tmp = tempfile.mktemp(suffix=".wav")
    with wave.open(tmp, 'w') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    return tmp

def play_victory():
    if SONG_PATH.exists():
        music.music_set_volume(70)
        music.music_play(str(SONG_PATH))
        time.sleep(5)
        music.music_stop()
    else:
        tmp = _gen_tune([523, 659, 784, 1047, 1047], dur=0.2)
        music.music_set_volume(80)
        music.sound_play(tmp)
        time.sleep(1.6)
        os.remove(tmp)

def play_shutdown():
    tmp = _gen_tune([784, 659, 523, 392], dur=0.3)
    music.music_set_volume(70)
    music.sound_play(tmp)
    time.sleep(2.0)
    os.remove(tmp)

# ── Helpers ───────────────────────────────────────────────────────────────────

def say(text: str):
    print(f"\n  [robot] {text}", flush=True)
    threading.Thread(target=tts.say, args=(text,), daemon=True).start()

def parse_target(raw: str) -> tuple[Mode, str, str]:
    """Returns (mode, target_word, full_prompt_as_initial_hint)."""
    t = raw.lower()
    for c in COLOR_NAMES:
        if c in t:
            return Mode.COLOR, c, raw
    if any(w in t for w in ["face", "person", "people", "human", "someone"]):
        return Mode.FACE, "person", raw
    return Mode.OBJECT, raw, raw

# ── Threads ───────────────────────────────────────────────────────────────────

def distance_thread(stop):
    while not stop.is_set():
        d = px.ultrasonic.read()
        ss("distance", round(d, 1) if d and d > 0 else 999.0)
        time.sleep(0.08)

def cliff_thread(stop):
    """
    Reads the three grayscale sensors at high frequency.
    Sets cliff=True immediately when a drop is detected and stops motors.
    Highest-priority safety — overrides everything in movement_thread.
    """
    while not stop.is_set():
        try:
            gm_vals  = px.get_grayscale_data()
            is_cliff = px.get_cliff_status(gm_vals)
            ss("cliff", bool(is_cliff))
            if is_cliff:
                px.stop()   # immediate stop — don't wait for movement_thread
        except Exception:
            pass
        time.sleep(0.04)   # ~25Hz — fast enough to catch a cliff before rolling off

def camera_scan_thread(stop):
    """Sweeps pan/tilt to cover the space; locks on target when detected."""
    pan_seq = [0,8,16,24,32,35,32,24,16,8,0,-8,-16,-24,-32,-35,-32,-24,-16,-8]
    i = 0
    while not stop.is_set():
        pan  = sg("cam_pan")
        tilt = sg("cam_tilt")
        if sg("detected"):
            pan  += (sg("obj_x") * 10 / FRAME_W) - 5
            tilt -= (sg("obj_y") * 10 / FRAME_H) - 5
            pan  = clamp(pan,  -35, 35)
            tilt = clamp(tilt, -20, 15)
        else:
            pan  = pan_seq[i % len(pan_seq)]
            tilt = -5.0
            i   += 1
        ss("cam_pan",  pan)
        ss("cam_tilt", tilt)
        px.set_cam_pan_angle(pan)
        px.set_cam_tilt_angle(tilt)
        time.sleep(0.12)
    px.set_cam_pan_angle(0)
    px.set_cam_tilt_angle(0)

def vision_thread(stop):
    """High-frequency Vilib reads for color/face modes."""
    while not stop.is_set():
        mode = sg("mode")
        if mode == Mode.COLOR:
            n = Vilib.detect_obj_parameter.get("color_n", 0)
            x = Vilib.detect_obj_parameter.get("color_x", FRAME_CX)
            y = Vilib.detect_obj_parameter.get("color_y", FRAME_CY)
            w = Vilib.detect_obj_parameter.get("color_w", 0)
        elif mode == Mode.FACE:
            n = Vilib.detect_obj_parameter.get("human_n", 0)
            x = Vilib.detect_obj_parameter.get("human_x", FRAME_CX)
            y = Vilib.detect_obj_parameter.get("human_y", FRAME_CY)
            w = Vilib.detect_obj_parameter.get("human_w", 0)
        else:
            time.sleep(0.05)
            continue
        ss("detected", n > 0)
        if n > 0:
            ss("obj_x", x); ss("obj_y", y); ss("obj_w", w)
            ss("last_progress", time.time())
        time.sleep(0.04)

GPT_SYSTEM = """
You are the vision and memory brain of an autonomous robot car searching for a target.
You build a persistent session memory of EVERYTHING you observe — not just the target.

Each query gives you: a camera image, obstacle distance, target, user hints, and your memory so far.

Reply ONLY in this exact format:
DETECTED: yes|no
MOVE: forward|turn_left|turn_right|backward|stop
SAY: <spoken update, 10 words max, or blank>
MEMORY: <one sentence updating what you've seen and where — include ALL objects noticed>
QUESTION: <a question to ask the user if genuinely stuck, or blank>

Rules:
- DETECTED yes only when target is clearly and fully visible in the frame
- Never pick forward if distance < 25cm
- Always update MEMORY with objects seen and their rough position (left/right/ahead/behind)
- Only use QUESTION when stuck for a long time — keep it short and specific
- Act on user hints immediately — they know the space
- The user may later ask about non-target objects you saw, so remember them
"""

def gpt_vision_thread(stop, target, found, session, initial_context):
    last_call     = 0.0
    last_question = 0.0
    username      = os.getlogin()
    snap_dir      = f"/home/{username}/Pictures/"
    snap_name     = "search_snap"

    if initial_context:
        session.append(f"Original prompt: {initial_context}")

    while not stop.is_set() and not found.is_set():
        now = time.time()
        if now - last_call < GPT_INTERVAL:
            time.sleep(0.1)
            continue
        last_call = now

        # Pull in any user hints
        hints = []
        while _hint_queue:
            hints.append(_hint_queue.popleft())
        if hints:
            for h in hints:
                session.append(f"User hint: {h}")
            ss("last_progress", now)

        # Capture frame via Vilib
        try:
            Vilib.take_photo(snap_name, snap_dir)
            time.sleep(0.15)
            with open(f"{snap_dir}{snap_name}.jpg", "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
        except Exception as e:
            print(f"  [gpt] snapshot error: {e}", flush=True)
            continue

        memory_str = "\n".join(session[-6:]) if session else "Just started."
        dist = sg("distance")

        result = [None]
        def call():
            result[0] = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": GPT_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": (
                            f"Target: {target}\n"
                            f"Obstacle distance: {dist:.0f}cm\n"
                            f"Session memory:\n{memory_str}"
                        )},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                    ]}
                ],
                max_tokens=120,
            )
        t = threading.Thread(target=call, daemon=True)
        t.start()
        while t.is_alive():
            if stop.is_set(): return
            t.join(timeout=0.1)

        if not result[0]: continue

        reply        = result[0].choices[0].message.content.strip()
        detected_gpt = False
        move         = "forward"
        say_text = memory_upd = question = ""

        for line in reply.splitlines():
            if   line.startswith("DETECTED:"): detected_gpt = line.split(":",1)[1].strip().lower() == "yes"
            elif line.startswith("MOVE:"):     move         = line.split(":",1)[1].strip().lower()
            elif line.startswith("SAY:"):      say_text     = line.split(":",1)[1].strip()
            elif line.startswith("MEMORY:"):   memory_upd   = line.split(":",1)[1].strip()
            elif line.startswith("QUESTION:"): question     = line.split(":",1)[1].strip()

        if memory_upd:
            session.append(memory_upd)
            if len(session) > 14: session[:] = session[-14:]

        if say_text: say(say_text)

        ss("gpt_move", move)

        # Ask for help if stuck
        if question and now - last_question > STRUGGLE_TIMEOUT:
            last_question = now
            say(question)
            print("  [type your answer and press Enter]\n", flush=True)

        if detected_gpt:
            ss("detected", True)
            ss("obj_x", FRAME_CX)
            ss("obj_w", FOUND_WIDTH + 10)
            ss("last_progress", now)
            found.set()

def movement_thread(stop, found):
    """
    20Hz movement control.
    - Drives forward while camera scans the space
    - Makes deliberate turns every COVERAGE_TIMEOUT seconds to explore new areas
    - Obstacle avoidance is non-blocking (state machine with timestamps)
    - Stops only when target is reached
    """
    CLIFF_BACK_SEC = 0.5    # back up longer for cliff — need more clearance
    CLIFF_TURN_SEC = 0.8
    AVOID_BACK_SEC = 0.35
    AVOID_TURN_SEC = 0.60

    # Separate state machines for cliff and obstacle so they don't interfere
    cliff_phase    = None
    cliff_t        = 0.0
    avoid_phase    = None
    avoid_t        = 0.0
    turn_dir       = 35
    coverage_t     = time.time()
    planned_turn   = False
    planned_turn_t = 0.0

    _la = [None]; _ls = [None]; _lf = [None]

    def drive(angle: int, speed: int, forward: bool = True):
        """Only send commands when values change — prevents servo jitter."""
        if angle != _la[0]:
            px.set_dir_servo_angle(angle); _la[0] = angle
        if speed != _ls[0] or forward != _lf[0]:
            if speed == 0: px.stop()
            elif forward:  px.forward(speed)
            else:          px.backward(speed)
            _ls[0] = speed; _lf[0] = forward

    while not stop.is_set() and not found.is_set():
        now      = time.time()
        cliff    = sg("cliff")
        dist     = sg("distance")
        detected = sg("detected")
        obj_x    = sg("obj_x")
        obj_w    = sg("obj_w")
        mode     = sg("mode")

        # ── CLIFF — highest priority, overrides everything ──
        if cliff and cliff_phase is None:
            cliff_phase = "back"
            cliff_t     = now
            avoid_phase = None   # cancel any obstacle manoeuvre
            drive(0, DRIVE_SPEED, forward=False)

        if cliff_phase == "back":
            if now - cliff_t >= CLIFF_BACK_SEC:
                cliff_phase = "turn"; cliff_t = now
                # Turn away from the cliff — use camera pan to guess direction
                turn_dir = -35 if sg("cam_pan") >= 0 else 35
                drive(turn_dir, TURN_SPEED)
            time.sleep(CONTROL_HZ); continue

        if cliff_phase == "turn":
            if now - cliff_t >= CLIFF_TURN_SEC:
                cliff_phase = None; coverage_t = now
            else:
                time.sleep(CONTROL_HZ); continue

        # ── OBSTACLE — second priority ──
        if dist < SAFE_DISTANCE and avoid_phase is None and not planned_turn:
            avoid_phase = "back"
            avoid_t     = now
            # Turn toward whichever side the camera isn't currently facing
            # — likely the clearer side
            turn_dir = 35 if sg("cam_pan") <= 0 else -35
            drive(0, DRIVE_SPEED, forward=False)

        if avoid_phase == "back":
            if now - avoid_t >= AVOID_BACK_SEC:
                avoid_phase = "turn"; avoid_t = now
                drive(turn_dir, TURN_SPEED)
            time.sleep(CONTROL_HZ); continue

        if avoid_phase == "turn":
            if now - avoid_t >= AVOID_TURN_SEC:
                avoid_phase = None; coverage_t = now
            else:
                time.sleep(CONTROL_HZ); continue

        # ── Planned coverage turn (non-blocking) ──
        if planned_turn:
            if now - planned_turn_t >= PLANNED_TURN_SEC:
                planned_turn = False; coverage_t = now
            else:
                time.sleep(CONTROL_HZ); continue

        # ── Target tracking ──
        if detected:
            error = obj_x - FRAME_CX
            if obj_w >= FOUND_WIDTH:
                drive(0, 0)
                found.set(); break
            angle = clamp(int(error / FRAME_CX * 35), -35, 35)
            drive(angle, DRIVE_SPEED)
            coverage_t = now

        # ── Searching ──
        else:
            if mode == Mode.OBJECT:
                gpt_cmd = sg("gpt_move")
                if   gpt_cmd == "turn_left":  drive(-35, TURN_SPEED)
                elif gpt_cmd == "turn_right":  drive(35,  TURN_SPEED)
                elif gpt_cmd == "backward":    drive(0, DRIVE_SPEED, forward=False)
                elif gpt_cmd == "stop":        drive(0, 0)
                else:                          drive(0, DRIVE_SPEED)
            else:
                if now - coverage_t >= COVERAGE_TIMEOUT:
                    planned_turn   = True
                    planned_turn_t = now
                    turn_dir = 35 if sg("cam_pan") >= 0 else -35
                    drive(turn_dir, TURN_SPEED)
                else:
                    drive(0, DRIVE_SPEED)

        time.sleep(CONTROL_HZ)

# ── Search session ─────────────────────────────────────────────────────────────

def _reset_vilib(mode):
    if mode == Mode.COLOR: Vilib.color_detect("close")
    elif mode == Mode.FACE: Vilib.face_detect_switch(False)

def search(raw: str):
    global _searching
    mode, target, initial_context = parse_target(raw)

    ss("mode", mode); ss("detected", False)
    ss("obj_w", 0);   ss("obj_x", FRAME_CX)
    ss("gpt_move", "forward")
    ss("last_progress", time.time())
    _flush()

    stop  = threading.Event()
    found = threading.Event()
    session: list = []

    if mode == Mode.COLOR:
        Vilib.color_detect(target)
        say(f"Looking for something {target}. Searching now!")
    elif mode == Mode.FACE:
        Vilib.face_detect_switch(True)
        say("Looking for a person. Searching now!")
    else:
        say(f"Searching for {target}. Type hints anytime!")

    threads = [
        threading.Thread(target=distance_thread,    args=(stop,),       daemon=True),
        threading.Thread(target=cliff_thread,       args=(stop,),       daemon=True),
        threading.Thread(target=camera_scan_thread, args=(stop,),       daemon=True),
        threading.Thread(target=vision_thread,      args=(stop,),       daemon=True),
        threading.Thread(target=movement_thread,    args=(stop, found), daemon=True),
    ]
    if mode == Mode.OBJECT:
        threads.append(threading.Thread(
            target=gpt_vision_thread,
            args=(stop, target, found, session, initial_context),
            daemon=True))

    for t in threads: t.start()

    print(f"\n  Target: {target}")
    print(  "  Type a hint and press Enter at any time to help.")
    print(  "  Ctrl+C to cancel.\n", flush=True)

    _searching = True
    try:
        while not found.is_set():
            print(f"  dist={sg('distance'):.0f}cm | "
                  f"detected={sg('detected')} | "
                  f"w={sg('obj_w')}px",
                  end="\r", flush=True)
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n  Search cancelled.")
        say("Search cancelled.")

    _searching = False
    stop.set()
    for t in threads: t.join(timeout=1.5)

    px.stop()
    px.set_dir_servo_angle(0)
    _reset_vilib(mode)
    _flush()

    if found.is_set():
        print(f"\n  Found: {target}!", flush=True)
        say(f"I found it! I found the {target}!")
        play_victory()

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    global _searching

    # Start single long-lived stdin reader
    threading.Thread(target=_stdin_reader, daemon=True).start()

    print("\n" + "="*48)
    print("   AUTONOMOUS SEARCH ROBOT")
    print("   Color/face → real-time (no internet)")
    print("   Any object → GPT-4o session memory")
    print("="*48)
    print("\nYou can include hints in your prompt:")
    print('  e.g. "find the apple, try the kitchen counter"\n')
    print("Hardware ready. Press ENTER to start.\n")
    sys.stdout.flush()
    _flush()
    _get_input("")   # gate — discards buffered input, waits for clean Enter

    try:
        while True:
            raw = _get_input("\nWhat should I find? >>> ")
            if not raw: continue
            if raw.lower() in ("quit", "exit", "q"): break
            search(raw)
    except KeyboardInterrupt:
        pass

    print("\n  Shutting down...")
    say("Goodbye!")
    play_shutdown()
    _stdin_stop.set()
    px.stop()
    px.set_dir_servo_angle(0)
    px.set_cam_pan_angle(0)
    px.set_cam_tilt_angle(0)
    try: Vilib.camera_close()
    except Exception: pass

if __name__ == "__main__":
    main()
