from flask import Flask, request, jsonify, render_template_string, Response
from picarx import Picarx
from picamera2 import Picamera2
import threading
import socket
import time
import cv2

app = Flask(__name__)
px = Picarx()

SPEED = 60
DIR_ANGLE = 30
PAN_MAX = 35
TILT_MAX = 35

cam_lock = threading.Lock()
_lights_on = False

# ── Camera via picamera2 ──────────────────────────────────────────────────────
_picam = Picamera2()
_picam.configure(_picam.create_video_configuration(
    main={"size": (640, 480), "format": "BGR888"}
))
_picam.start()
time.sleep(0.5)

# Color correction: proper white balance, exposure, saturation, and sharpness
try:
    _picam.set_controls({
        "AwbEnable": True,
        "AeEnable": True,
        "Saturation": 1.3,
        "Sharpness": 1.5,
        "Contrast": 1.1,
    })
except Exception:
    pass

_frame_lock = threading.Lock()
_latest_frame = None

def _capture_loop():
    global _latest_frame
    while True:
        frame = _picam.capture_array()
        if frame is not None:
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            with _frame_lock:
                _latest_frame = buf.tobytes()
        time.sleep(0.033)

threading.Thread(target=_capture_loop, daemon=True).start()

def _gen_frames():
    while True:
        with _frame_lock:
            frame = _latest_frame
        if frame:
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
        time.sleep(0.033)

# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>PiCar-X</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      width: 100%; height: 100%;
      overflow: hidden;
      background: #000;
      touch-action: none;
      font-family: system-ui, sans-serif;
    }

    /* Live feed — full-screen background */
    #feed {
      position: fixed;
      inset: 0;
      width: 100%; height: 100%;
      object-fit: cover;
      z-index: 0;
    }

    /* Camera drag layer — covers entire screen, sits above video */
    #camDrag {
      position: fixed;
      inset: 0;
      z-index: 1;
      touch-action: none;
    }

    /* Aim dot — shows where camera is pointing */
    #aimDot {
      position: fixed;
      width: 26px; height: 26px;
      background: rgba(0, 170, 255, 0.9);
      border-radius: 50%;
      box-shadow: 0 0 14px 5px rgba(0, 170, 255, 0.45);
      transform: translate(-50%, -50%);
      pointer-events: none;
      z-index: 3;
      left: 50%; top: 50%;
      opacity: 0.5;
      transition: opacity 0.2s;
    }
    #aimDot.dragging { opacity: 1; }

    /* Joystick — fixed bottom-left */
    .joy-wrap {
      position: fixed;
      bottom: 24px; left: 24px;
      z-index: 2;
      touch-action: none;
    }
    .joy-base {
      width: 150px; height: 150px;
      border-radius: 50%;
      background: rgba(0, 0, 0, 0.5);
      border: 2px solid rgba(255, 255, 255, 0.15);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      position: relative;
    }
    .joy-knob {
      width: 60px; height: 60px;
      border-radius: 50%;
      background: rgba(0, 170, 255, 0.7);
      border: 2px solid rgba(0, 170, 255, 0.9);
      box-shadow: 0 0 12px rgba(0, 170, 255, 0.4);
      position: absolute;
      top: 50%; left: 50%;
      transform: translate(-50%, -50%);
      transition: transform 0.12s ease;
    }
    .joy-knob.active { transition: none; }

    /* Speed control — above joystick */
    .speed-ctrl {
      position: fixed;
      bottom: 188px; left: 24px;
      z-index: 2;
      width: 150px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      touch-action: pan-x;
    }
    .speed-label {
      color: rgba(255, 255, 255, 0.6);
      font-size: 0.68rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }
    #speedSlider {
      width: 100%;
      accent-color: rgba(0, 170, 255, 0.9);
      cursor: pointer;
      touch-action: pan-x;
    }

    /* Right button column */
    .btn-col {
      position: fixed;
      bottom: 24px; right: 24px;
      z-index: 2;
      display: flex;
      flex-direction: column;
      gap: 10px;
      align-items: center;
    }
    .icon-btn {
      width: 56px; height: 56px;
      border-radius: 50%;
      background: rgba(0, 0, 0, 0.5);
      border: 1.5px solid rgba(255, 255, 255, 0.2);
      color: rgba(255, 255, 255, 0.6);
      font-size: 1.3rem;
      line-height: 1;
      cursor: pointer;
      touch-action: manipulation;
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      -webkit-tap-highlight-color: transparent;
      display: flex;
      align-items: center;
      justify-content: center;
      user-select: none;
      -webkit-user-select: none;
    }
    .icon-btn.active {
      background: rgba(0, 170, 255, 0.35);
      border-color: rgba(0, 170, 255, 0.7);
      color: rgba(255, 255, 255, 0.9);
    }
    #hornBtn.active {
      background: rgba(255, 80, 80, 0.4);
      border-color: rgba(255, 80, 80, 0.8);
      color: rgba(255, 200, 200, 0.95);
    }
  </style>
</head>
<body>

  <img id="feed" src="/video_feed" alt="">
  <div id="aimDot"></div>
  <div id="camDrag"></div>

  <div class="joy-wrap" id="joyWrap">
    <div class="joy-base" id="joyBase">
      <div class="joy-knob" id="joyKnob"></div>
    </div>
  </div>

  <div class="speed-ctrl">
    <span class="speed-label">SPD <span id="speedVal">60</span></span>
    <input type="range" id="speedSlider" min="10" max="100" value="60" step="5">
  </div>

  <!-- Horn=&#9836; Lights=&#10022; Camera=&#8982; -->
  <div class="btn-col">
    <button id="hornBtn"   class="icon-btn" title="Hold to beep (Space)">&#9836;</button>
    <button id="lightsBtn" class="icon-btn" title="Toggle lights (L)">&#10022;</button>
    <button id="camCenter" class="icon-btn" title="Center camera (C)">&#8982;</button>
  </div>

  <script>
    // ── Joystick ──────────────────────────────────────────────────
    const joyWrap  = document.getElementById('joyWrap');
    const joyBase  = document.getElementById('joyBase');
    const knob     = document.getElementById('joyKnob');
    const JOY_R    = 45;
    let   MAX_SPEED  = 60;
    const MAX_ANGLE  = 30;
    let joyActive = false;

    function joyDelta(e) {
      const r   = joyBase.getBoundingClientRect();
      const cx  = r.left + r.width  / 2;
      const cy  = r.top  + r.height / 2;
      const src = e.touches ? e.touches[0] : e;
      let dx = src.clientX - cx;
      let dy = src.clientY - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist > JOY_R) { dx = dx / dist * JOY_R; dy = dy / dist * JOY_R; }
      return { dx, dy };
    }

    function joyStart(e) {
      e.preventDefault(); e.stopPropagation();
      joyActive = true;
      knob.classList.add('active');
      joyMove(e);
    }
    function joyMove(e) {
      e.preventDefault(); e.stopPropagation();
      if (!joyActive) return;
      const { dx, dy } = joyDelta(e);
      knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
      const angle = Math.round((dx / JOY_R) * MAX_ANGLE);
      const speed = Math.round((-dy / JOY_R) * MAX_SPEED);
      sendDrive(angle, speed);
    }
    function joyEnd(e) {
      e.preventDefault(); e.stopPropagation();
      joyActive = false;
      knob.classList.remove('active');
      knob.style.transform = 'translate(-50%, -50%)';
      sendDrive(0, 0);
    }

    function sendDrive(angle, speed) {
      fetch('/drive', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ angle, speed }),
      }).catch(() => {});
    }

    joyWrap.addEventListener('touchstart',  joyStart, { passive: false });
    joyWrap.addEventListener('touchmove',   joyMove,  { passive: false });
    joyWrap.addEventListener('touchend',    joyEnd,   { passive: false });
    joyWrap.addEventListener('touchcancel', joyEnd,   { passive: false });
    joyWrap.addEventListener('mousedown',   joyStart);
    document.addEventListener('mousemove',  e => { if (joyActive) joyMove(e); });
    document.addEventListener('mouseup',    e => { if (joyActive) joyEnd(e); });

    // ── Speed slider ──────────────────────────────────────────────
    const speedSlider = document.getElementById('speedSlider');
    const speedVal    = document.getElementById('speedVal');

    speedSlider.addEventListener('input', () => {
      MAX_SPEED = parseInt(speedSlider.value, 10);
      speedVal.textContent = MAX_SPEED;
    });
    speedSlider.addEventListener('touchstart', e => e.stopPropagation(), { passive: true });
    speedSlider.addEventListener('touchmove',  e => e.stopPropagation(), { passive: true });

    // ── Camera drag (full screen, stays on release) ───────────────
    const drag    = document.getElementById('camDrag');
    const aimDot  = document.getElementById('aimDot');
    const PAN_MAX  = 35;
    const TILT_MAX = 35;
    let isDragging = false;
    let lastPan = 0, lastTilt = 0;

    function applyCamera(e) {
      const src = e.touches ? e.touches[0] : e;
      const x = src.clientX, y = src.clientY;
      const pan  = Math.round(((x / window.innerWidth)  - 0.5) *  2 * PAN_MAX);
      const tilt = Math.round(((y / window.innerHeight) - 0.5) * -2 * TILT_MAX);
      aimDot.style.left = x + 'px';
      aimDot.style.top  = y + 'px';
      if (pan !== lastPan || tilt !== lastTilt) {
        lastPan = pan; lastTilt = tilt;
        fetch('/camera', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pan, tilt }),
        }).catch(() => {});
      }
    }

    function dragStart(e) { e.preventDefault(); isDragging = true;  aimDot.classList.add('dragging');    applyCamera(e); }
    function dragMove(e)  { e.preventDefault(); if (isDragging) applyCamera(e); }
    function dragEnd(e)   { e.preventDefault(); isDragging = false; aimDot.classList.remove('dragging'); }

    drag.addEventListener('touchstart',  dragStart, { passive: false });
    drag.addEventListener('touchmove',   dragMove,  { passive: false });
    drag.addEventListener('touchend',    dragEnd,   { passive: false });
    drag.addEventListener('touchcancel', dragEnd,   { passive: false });
    drag.addEventListener('mousedown',   dragStart);
    drag.addEventListener('mousemove',   dragMove);
    drag.addEventListener('mouseup',     dragEnd);
    drag.addEventListener('mouseleave',  e => { if (isDragging) dragEnd(e); });

    // ── Center camera button ──────────────────────────────────────
    document.getElementById('camCenter').addEventListener('click', e => {
      e.stopPropagation();
      lastPan = 0; lastTilt = 0;
      aimDot.style.left = '50%';
      aimDot.style.top  = '50%';
      fetch('/camera', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pan: 0, tilt: 0 }),
      }).catch(() => {});
    });

    // ── Horn button (hold to beep) ────────────────────────────────
    const hornBtn = document.getElementById('hornBtn');
    let hornActive = false;

    function startHorn() {
      if (hornActive) return;
      hornActive = true;
      hornBtn.classList.add('active');
      fetch('/horn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: true }),
      }).catch(() => {});
    }
    function stopHorn() {
      if (!hornActive) return;
      hornActive = false;
      hornBtn.classList.remove('active');
      fetch('/horn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: false }),
      }).catch(() => {});
    }

    hornBtn.addEventListener('touchstart',  e => { e.preventDefault(); e.stopPropagation(); startHorn(); }, { passive: false });
    hornBtn.addEventListener('touchend',    e => { e.preventDefault(); e.stopPropagation(); stopHorn();  }, { passive: false });
    hornBtn.addEventListener('touchcancel', e => { e.preventDefault(); e.stopPropagation(); stopHorn();  }, { passive: false });
    hornBtn.addEventListener('mousedown',   e => { e.stopPropagation(); startHorn(); });
    document.addEventListener('mouseup',    () => { if (hornActive) stopHorn(); });

    // ── Lights toggle ─────────────────────────────────────────────
    const lightsBtn = document.getElementById('lightsBtn');
    let lightsOn = false;

    function toggleLights() {
      lightsOn = !lightsOn;
      lightsBtn.classList.toggle('active', lightsOn);
      fetch('/lights', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: lightsOn }),
      }).catch(() => {});
    }

    lightsBtn.addEventListener('click', e => { e.stopPropagation(); toggleLights(); });

    // ── Keyboard controls — WASD/arrows + Space=horn, L=lights, C=center ──
    const keys = {};

    function sendKeyDrive() {
      let speed = 0, angle = 0;
      if (keys['KeyW'] || keys['ArrowUp'])    speed =  MAX_SPEED;
      if (keys['KeyS'] || keys['ArrowDown'])  speed = -MAX_SPEED;
      if (keys['KeyA'] || keys['ArrowLeft'])  angle = -MAX_ANGLE;
      if (keys['KeyD'] || keys['ArrowRight']) angle =  MAX_ANGLE;
      sendDrive(angle, speed);
    }

    const DRIVE_KEYS = new Set(['KeyW','KeyS','KeyA','KeyD','ArrowUp','ArrowDown','ArrowLeft','ArrowRight']);

    document.addEventListener('keydown', e => {
      if (e.repeat) return;
      if (DRIVE_KEYS.has(e.code)) {
        e.preventDefault();
        if (!joyActive) { keys[e.code] = true; sendKeyDrive(); }
      } else if (e.code === 'Space') {
        e.preventDefault();
        startHorn();
      } else if (e.code === 'KeyL') {
        e.preventDefault();
        toggleLights();
      } else if (e.code === 'KeyC') {
        e.preventDefault();
        document.getElementById('camCenter').click();
      }
    });

    document.addEventListener('keyup', e => {
      if (DRIVE_KEYS.has(e.code) && keys[e.code]) {
        delete keys[e.code];
        if (!joyActive) sendKeyDrive();
      }
      if (e.code === 'Space') stopHorn();
    });
  </script>
</body>
</html>"""


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/video_feed')
def video_feed():
    return Response(_gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/drive', methods=['POST'])
def drive():
    data  = request.get_json(silent=True) or {}
    angle = max(-DIR_ANGLE, min(DIR_ANGLE, int(data.get('angle', 0))))
    speed = max(-SPEED,     min(SPEED,     int(data.get('speed', 0))))
    px.set_dir_servo_angle(angle)
    if speed > 0:
        px.forward(speed)
    elif speed < 0:
        px.backward(abs(speed))
    else:
        px.stop()
    return jsonify(ok=True)

@app.route('/camera', methods=['POST'])
def camera():
    data = request.get_json(silent=True) or {}
    pan  = max(-PAN_MAX,  min(PAN_MAX,  int(data.get('pan',  0))))
    tilt = max(-TILT_MAX, min(TILT_MAX, int(data.get('tilt', 0))))
    with cam_lock:
        px.set_cam_pan_angle(pan)
        px.set_cam_tilt_angle(tilt)
    return jsonify(ok=True)

@app.route('/horn', methods=['POST'])
def horn():
    data = request.get_json(silent=True) or {}
    on = bool(data.get('on', False))
    try:
        if on:
            px.buzzer.on()
        else:
            px.buzzer.off()
    except Exception:
        pass
    return jsonify(ok=True)

@app.route('/lights', methods=['POST'])
def lights():
    global _lights_on
    data = request.get_json(silent=True) or {}
    _lights_on = bool(data.get('on', False))
    try:
        for led in px.leds:
            led.on() if _lights_on else led.off()
    except Exception:
        pass
    return jsonify(ok=True, on=_lights_on)


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '0.0.0.0'
    finally:
        s.close()


if __name__ == '__main__':
    ip = _local_ip()
    print(f"\n  PiCar-X Web Controller")
    print(f"  Open on your phone: http://{ip}:5000\n")
    print("  Keyboard: WASD/arrows=drive  Space=horn  L=lights  C=center-cam\n")
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    finally:
        _picam.stop()
        px.set_cam_pan_angle(0)
        px.set_cam_tilt_angle(0)
        px.set_dir_servo_angle(0)
        px.stop()
        try:
            px.buzzer.off()
        except Exception:
            pass
        try:
            for led in px.leds:
                led.off()
        except Exception:
            pass
