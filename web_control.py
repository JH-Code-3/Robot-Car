from flask import Flask, request, jsonify, render_template_string, Response
from picarx import Picarx
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

# ── Camera capture thread ────────────────────────────────────────────────────
_cap = cv2.VideoCapture(0)
_cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
_frame_lock = threading.Lock()
_latest_frame = None

def _capture_loop():
    global _latest_frame
    while True:
        ok, frame = _cap.read()
        if ok:
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
            with _frame_lock:
                _latest_frame = buf.tobytes()

threading.Thread(target=_capture_loop, daemon=True).start()

def _gen_frames():
    while True:
        with _frame_lock:
            frame = _latest_frame
        if frame:
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
        time.sleep(0.04)  # ~25 fps

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

    /* Aim dot — follows finger while dragging, transitions back to center on release */
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
      opacity: 0.35;
      transition: left 0.18s ease, top 0.18s ease, opacity 0.2s;
    }
    #aimDot.dragging {
      opacity: 1;
      transition: opacity 0.08s;
    }

    /* D-pad — fixed bottom-left, frosted over the video */
    .dpad-wrap {
      position: fixed;
      bottom: 20px; left: 20px;
      z-index: 2;
      background: rgba(0, 0, 0, 0.5);
      border-radius: 18px;
      padding: 10px;
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
    }
    .dpad {
      display: grid;
      grid-template-columns: repeat(3, 72px);
      grid-template-rows: repeat(3, 72px);
      gap: 6px;
    }
    .dpad-btn {
      background: rgba(255, 255, 255, 0.1);
      border: 1.5px solid rgba(255, 255, 255, 0.18);
      border-radius: 12px;
      color: #ddd;
      font-size: 1.6rem;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
      touch-action: none;
      transition: background 0.07s, border-color 0.07s;
    }
    .dpad-btn.active {
      background: rgba(0, 170, 255, 0.75);
      border-color: #0af;
      color: #fff;
    }
    .dpad-stop { font-size: 1rem; color: rgba(255,255,255,0.35); }
    .dpad-stop.active { background: rgba(255, 55, 55, 0.75); border-color: #f44; }
    .dpad-empty { visibility: hidden; }
  </style>
</head>
<body>

  <img id="feed" src="/video_feed" alt="">
  <div id="aimDot"></div>
  <div id="camDrag"></div>

  <div class="dpad-wrap">
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
  </div>

  <script>
    // ── Movement ─────────────────────────────────────────────────
    function sendMove(dir) {
      fetch('/move/' + dir, { method: 'POST' }).catch(() => {});
    }

    document.querySelectorAll('.dpad-btn').forEach(btn => {
      const dir = btn.dataset.dir;
      const start = () => { btn.classList.add('active');    sendMove(dir); };
      const end   = () => { btn.classList.remove('active'); if (dir !== 'stop') sendMove('stop'); };

      btn.addEventListener('touchstart', e => { e.stopPropagation(); e.preventDefault(); start(); }, { passive: false });
      btn.addEventListener('touchend',   e => { e.stopPropagation(); e.preventDefault(); end();   }, { passive: false });
      btn.addEventListener('mousedown',  e => { e.stopPropagation(); start(); });
      btn.addEventListener('mouseup',    e => { e.stopPropagation(); end();   });
      btn.addEventListener('mouseleave', () => { if (btn.classList.contains('active')) end(); });
    });

    // ── Camera drag (full screen) ─────────────────────────────────
    const drag    = document.getElementById('camDrag');
    const aimDot  = document.getElementById('aimDot');
    const PAN_MAX  = 35;
    const TILT_MAX = 35;
    let isDragging = false;
    let lastPan = 0, lastTilt = 0;

    function srcPos(e) {
      const s = e.touches ? e.touches[0] : e;
      return { x: s.clientX, y: s.clientY };
    }

    function applyCamera(e) {
      const { x, y } = srcPos(e);
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

    function dragStart(e) {
      e.preventDefault();
      isDragging = true;
      aimDot.classList.add('dragging');
      applyCamera(e);
    }
    function dragMove(e) {
      e.preventDefault();
      if (isDragging) applyCamera(e);
    }
    function dragEnd(e) {
      e.preventDefault();
      isDragging = false;
      aimDot.classList.remove('dragging');
      aimDot.style.left = '50%';
      aimDot.style.top  = '50%';
      if (lastPan !== 0 || lastTilt !== 0) {
        lastPan = 0; lastTilt = 0;
        fetch('/camera', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pan: 0, tilt: 0 }),
        }).catch(() => {});
      }
    }

    drag.addEventListener('touchstart',  dragStart, { passive: false });
    drag.addEventListener('touchmove',   dragMove,  { passive: false });
    drag.addEventListener('touchend',    dragEnd,   { passive: false });
    drag.addEventListener('touchcancel', dragEnd,   { passive: false });
    drag.addEventListener('mousedown',   dragStart);
    drag.addEventListener('mousemove',   dragMove);
    drag.addEventListener('mouseup',     dragEnd);
    drag.addEventListener('mouseleave',  e => { if (isDragging) dragEnd(e); });
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
        _cap.release()
        px.set_cam_pan_angle(0)
        px.set_cam_tilt_angle(0)
        px.set_dir_servo_angle(0)
        px.stop()
