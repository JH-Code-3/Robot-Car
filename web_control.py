from flask import Flask, request, jsonify, render_template_string
from picarx import Picarx
import threading
import socket

app = Flask(__name__)
px = Picarx()

SPEED = 60
DIR_ANGLE = 30
PAN_MAX = 35
TILT_MAX = 35

cam_lock = threading.Lock()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>PiCar-X</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d0d0d;
      color: #eee;
      font-family: system-ui, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 24px 16px;
      gap: 32px;
      min-height: 100dvh;
      overscroll-behavior: none;
    }
    h1 {
      font-size: 0.85rem;
      letter-spacing: 3px;
      text-transform: uppercase;
      opacity: 0.4;
    }

    /* ── D-PAD ── */
    .dpad {
      display: grid;
      grid-template-columns: repeat(3, 90px);
      grid-template-rows: repeat(3, 90px);
      gap: 8px;
    }
    .dpad-btn {
      background: #1c1c1c;
      border: 2px solid #2e2e2e;
      border-radius: 14px;
      color: #ccc;
      font-size: 2rem;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
      touch-action: none;
      transition: background 0.08s, border-color 0.08s, color 0.08s;
    }
    .dpad-btn.active {
      background: #0af;
      border-color: #0af;
      color: #000;
    }
    .dpad-stop { font-size: 1.1rem; color: #555; }
    .dpad-stop.active { background: #f44; border-color: #f44; color: #fff; }
    .dpad-empty { visibility: hidden; }

    /* ── CAMERA PAD ── */
    .cam-section { display: flex; flex-direction: column; align-items: center; gap: 10px; }
    .cam-label { font-size: 0.75rem; letter-spacing: 2px; opacity: 0.35; }
    .cam-pad {
      width: 260px;
      height: 260px;
      background: #1c1c1c;
      border: 2px solid #2e2e2e;
      border-radius: 20px;
      position: relative;
      touch-action: none;
      cursor: crosshair;
      overflow: hidden;
    }
    /* crosshair lines */
    .cam-pad::before, .cam-pad::after {
      content: '';
      position: absolute;
      background: #2e2e2e;
      pointer-events: none;
    }
    .cam-pad::before { left: 50%; top: 12%; bottom: 12%; width: 1px; transform: translateX(-50%); }
    .cam-pad::after  { top: 50%; left: 12%; right: 12%; height: 1px; transform: translateY(-50%); }
    .cam-dot {
      width: 22px;
      height: 22px;
      background: #0af;
      border-radius: 50%;
      position: absolute;
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%);
      pointer-events: none;
      box-shadow: 0 0 10px #0af8;
    }
    .cam-reset-hint {
      font-size: 0.7rem;
      opacity: 0.25;
      letter-spacing: 1px;
    }
  </style>
</head>
<body>
  <h1>PiCar-X Control</h1>

  <!-- D-PAD -->
  <div class="dpad">
    <div class="dpad-empty"></div>
    <div class="dpad-btn" data-dir="forward">&#9650;</div>
    <div class="dpad-empty"></div>
    <div class="dpad-btn" data-dir="left">&#9664;</div>
    <div class="dpad-btn dpad-stop" data-dir="stop">&#9632;</div>
    <div class="dpad-btn" data-dir="right">&#9654;</div>
    <div class="dpad-empty"></div>
    <div class="dpad-btn" data-dir="backward">&#9660;</div>
    <div class="dpad-empty"></div>
  </div>

  <!-- CAMERA TOUCH PAD -->
  <div class="cam-section">
    <div class="cam-label">CAMERA &mdash; DRAG TO LOOK</div>
    <div class="cam-pad" id="camPad">
      <div class="cam-dot" id="camDot"></div>
    </div>
    <div class="cam-reset-hint">Release to center</div>
  </div>

  <script>
    // ── Movement ────────────────────────────────────────────────
    const dpadBtns = document.querySelectorAll('.dpad-btn');

    function sendMove(dir) {
      fetch('/move/' + dir, { method: 'POST' }).catch(() => {});
    }

    dpadBtns.forEach(btn => {
      const dir = btn.dataset.dir;

      btn.addEventListener('touchstart', e => {
        e.preventDefault();
        btn.classList.add('active');
        sendMove(dir);
      }, { passive: false });

      btn.addEventListener('touchend', e => {
        e.preventDefault();
        btn.classList.remove('active');
        if (dir !== 'stop') sendMove('stop');
      }, { passive: false });

      // Mouse fallback (desktop testing)
      btn.addEventListener('mousedown', () => { btn.classList.add('active'); sendMove(dir); });
      btn.addEventListener('mouseup',   () => { btn.classList.remove('active'); if (dir !== 'stop') sendMove('stop'); });
      btn.addEventListener('mouseleave',() => { if (btn.classList.contains('active')) { btn.classList.remove('active'); if (dir !== 'stop') sendMove('stop'); } });
    });

    // ── Camera ──────────────────────────────────────────────────
    const pad    = document.getElementById('camPad');
    const dot    = document.getElementById('camDot');
    const PAN_MAX  = 35;
    const TILT_MAX = 35;
    let camActive = false;
    let lastPan = 0, lastTilt = 0;

    function pointerPos(e) {
      const rect = pad.getBoundingClientRect();
      const src  = e.touches ? e.touches[0] : e;
      return {
        x: Math.max(0, Math.min(rect.width,  src.clientX - rect.left)),
        y: Math.max(0, Math.min(rect.height, src.clientY - rect.top)),
        w: rect.width,
        h: rect.height,
      };
    }

    function updateCamera(e) {
      const { x, y, w, h } = pointerPos(e);
      const pan  = Math.round(((x / w) - 0.5) *  2 * PAN_MAX);
      const tilt = Math.round(((y / h) - 0.5) * -2 * TILT_MAX); // drag up = look up

      dot.style.left = x + 'px';
      dot.style.top  = y + 'px';

      if (pan !== lastPan || tilt !== lastTilt) {
        lastPan = pan; lastTilt = tilt;
        fetch('/camera', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pan, tilt }),
        }).catch(() => {});
      }
    }

    function centerCamera() {
      camActive = false;
      dot.style.left = '50%';
      dot.style.top  = '50%';
      dot.style.transform = 'translate(-50%, -50%)';
      if (lastPan !== 0 || lastTilt !== 0) {
        lastPan = 0; lastTilt = 0;
        fetch('/camera', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pan: 0, tilt: 0 }),
        }).catch(() => {});
      }
    }

    pad.addEventListener('touchstart', e => {
      e.preventDefault();
      camActive = true;
      dot.style.transform = 'translate(-50%, -50%)';
      updateCamera(e);
    }, { passive: false });

    pad.addEventListener('touchmove', e => {
      e.preventDefault();
      if (camActive) updateCamera(e);
    }, { passive: false });

    pad.addEventListener('touchend',   e => { e.preventDefault(); centerCamera(); }, { passive: false });
    pad.addEventListener('touchcancel',e => { e.preventDefault(); centerCamera(); }, { passive: false });

    // Mouse fallback
    pad.addEventListener('mousedown', e => { camActive = true; dot.style.transform = 'translate(-50%, -50%)'; updateCamera(e); });
    pad.addEventListener('mousemove', e => { if (camActive) updateCamera(e); });
    pad.addEventListener('mouseup',   () => centerCamera());
    pad.addEventListener('mouseleave',() => { if (camActive) centerCamera(); });
  </script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/move/<direction>', methods=['POST'])
def move(direction):
    if direction == 'forward':
        px.set_dir_servo_angle(0)
        px.forward(SPEED)
    elif direction == 'backward':
        px.set_dir_servo_angle(0)
        px.backward(SPEED)
    elif direction == 'left':
        px.set_dir_servo_angle(-DIR_ANGLE)
        px.forward(SPEED)
    elif direction == 'right':
        px.set_dir_servo_angle(DIR_ANGLE)
        px.forward(SPEED)
    elif direction == 'stop':
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
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    finally:
        px.set_cam_pan_angle(0)
        px.set_cam_tilt_angle(0)
        px.set_dir_servo_angle(0)
        px.stop()
