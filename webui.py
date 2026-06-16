#!/usr/bin/env python3
"""
opm web UI - detect Orange Pi SD cards and copy them from a browser.

It reuses opm.py's engine (detect / mount / copy / verify / delete), so there is
one source of truth. The server runs a copy as a background job and the page
polls /api/status for live per-card progress.

Run:
    sudo python3 webui.py            # root needed to MOUNT cards; copy/delete work
    python3 webui.py                 # non-root: detection/preview only

Then open  http://127.0.0.1:8765  (bound to localhost only, on purpose).
"""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import opm  # the CLI engine

HOST = "127.0.0.1"
PORT = int(os.environ.get("OPM_PORT", "8765"))
SCRIPT_DIR = Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR / "web"
THUMB_DIR = Path(tempfile.gettempdir()) / "opm-thumbs"

CFG = opm.load_config(str(SCRIPT_DIR / "config.yaml"))  # UI edits this copy in memory

_SAFE_SEG = re.compile(r"^[A-Za-z0-9._-]+$")             # a single safe path segment
_VID_EXT = (".mp4", ".mov", ".mkv", ".avi")


def _session_dir(slot, session):
    """Validated absolute session directory STRICTLY inside the card mount, or None
    (segments validated; resolved realpath must stay under the card mountpoint)."""
    if slot is None or not session or not _SAFE_SEG.match(session) or session in (".", ".."):
        return None
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return None
    card = next((c for c in opm.detect_cards(CFG) if c["slot"] == slot), None)
    if not card or not card.get("mounted_at"):
        return None
    mnt = os.path.realpath(card["mounted_at"])
    for p in CFG["copy_paths"]:
        sess = os.path.realpath(os.path.join(mnt, p.lstrip("/"), session))
        if (sess == mnt or sess.startswith(mnt + os.sep)) and os.path.isdir(sess):
            return sess
    return None


def session_videos(slot, session):
    """All video files in a session (sorted), as [{file, bytes}, …]."""
    sess = _session_dir(slot, session)
    if not sess:
        return []
    try:
        names = sorted(os.listdir(sess))
    except OSError:
        return []
    out = []
    for f in names:
        full = os.path.join(sess, f)
        if f.lower().endswith(_VID_EXT) and os.path.isfile(full):
            try:
                out.append({"file": f, "bytes": os.path.getsize(full)})
            except OSError:
                pass
    return out


def session_video(slot, session, fname=None):
    """Resolve one video for (slot, session): the named `fname` if given (validated),
    else the first non-empty video. Absolute path or None."""
    sess = _session_dir(slot, session)
    if not sess:
        return None
    if fname:
        if not _SAFE_SEG.match(fname) or fname in (".", ".."):
            return None
        cand = os.path.join(sess, fname)
        return cand if (os.path.isfile(cand) and cand.lower().endswith(_VID_EXT)) else None
    try:
        names = sorted(os.listdir(sess))
    except OSError:
        return None
    vids = [f for f in names
            if f.lower().endswith(_VID_EXT) and os.path.getsize(os.path.join(sess, f)) > 0]
    return os.path.join(sess, vids[0]) if vids else None


def ensure_thumb(video):
    """Cached 320px-wide JPEG thumbnail for a video; returns its path or None."""
    try:
        st = os.stat(video)
    except OSError:
        return None
    key = hashlib.sha1(f"{video}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()
    out = THUMB_DIR / f"{key}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video),
                    "-vf", "thumbnail,scale=320:-1", "-frames:v", "1", "-y", str(out)],
                   capture_output=True)
    return out if (out.exists() and out.stat().st_size > 0) else None


def ensure_preview(video):
    """Cached browser-playable H.264 preview for a video the browser can't decode
    natively (these recordings are HEVC). Downsampled per the `preview` config —
    by default the FULL recording (preview.seconds=0), scaled down + low fps so the
    transcode stays fast and the clip is seekable once cached."""
    try:
        st = os.stat(video)
    except OSError:
        return None
    pv = CFG.get("preview", {})
    secs = int(pv.get("seconds", 0) or 0)
    width = int(pv.get("width", 640))
    fps = int(pv.get("fps", 15))
    crf = int(pv.get("crf", 32))
    key = hashlib.sha1(
        f"prev:{video}:{st.st_mtime_ns}:{st.st_size}:{secs}:{width}:{fps}:{crf}".encode()
    ).hexdigest()
    out = THUMB_DIR / f"{key}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")                       # atomic: don't serve a half file
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if secs > 0:
        cmd += ["-t", str(secs)]                         # INPUT-side: read only the first
    cmd += ["-i", str(video),                            # `secs` seconds -> genuinely fast
            "-vf", f"scale='min({width},iw)':-2", "-r", str(fps),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            "-f", "mp4", "-y", str(tmp)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        os.replace(tmp, out)
        return out
    if tmp.exists():
        tmp.unlink()
    return None


def rescan_usb():
    """Force USB card readers to re-read their media so a newly-inserted card is
    detected WITHOUT opening it in a file manager (these readers don't reliably
    fire media-change events). Root only; skipped while a job is running."""
    if os.geteuid() != 0 or JOB.running:
        return
    try:
        data = json.loads(subprocess.run(["lsblk", "-J", "-o", "NAME,TRAN,TYPE"],
                                          text=True, capture_output=True).stdout)
    except Exception:                                  # noqa: BLE001
        return
    rescanned = False
    for d in data.get("blockdevices", []):
        if d.get("type") == "disk" and d.get("tran") == "usb":
            try:
                with open(f"/sys/block/{d['name']}/device/rescan", "w") as fh:
                    fh.write("1\n")
                rescanned = True
            except OSError:
                pass
    if rescanned:
        subprocess.run(["udevadm", "settle", "--timeout=3"], capture_output=True)


def browse_mount(cards):
    """Read-only mount eligible-but-unmounted cards so the UI can read their sizes,
    sessions and video thumbnails without opening them in a file manager. Root only;
    skipped during a job. Mounts under cfg.mount.base so process_card recognises and
    manages them (and can switch to read-write for a verified delete)."""
    if os.geteuid() != 0 or JOB.running:
        return
    for c in cards:
        if not c.get("eligible") or c.get("mounted_at") or not c.get("rootpart"):
            continue
        mnt = os.path.join(CFG["mount"]["base"], f"slot{c['slot']}")
        try:
            os.makedirs(mnt, exist_ok=True)
            subprocess.run(["mount", "-o", "ro,noload", c["rootpart"], mnt],
                           text=True, capture_output=True)
            m = opm.existing_mount(c["rootpart"])
            if m:
                c["mounted_at"] = m
        except Exception:                              # noqa: BLE001
            pass


def detect_ready():
    """opm.detect_cards + media rescan + read-only browse-mount (root), so cards are
    visible and readable without first opening them in the desktop file manager."""
    rescan_usb()
    cards = opm.detect_cards(CFG)
    browse_mount(cards)
    return cards


# --------------------------------------------------------------------------- #
# shared job state (one job at a time)
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.mode = None
        self.do_delete = False
        self.started_at = None
        self.finished_at = None
        self.cards = {}          # slot -> live dict
        self.log = []
        self.error = None
        self.io = {"read_mbps": 0.0, "write_mbps": 0.0}
        self.overall = {"eta": "", "rate_mbps": 0.0, "remaining": 0}

    def append_log(self, msg):
        self.log.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "mode": self.mode,
                "do_delete": self.do_delete,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "io": dict(self.io),
                "overall": dict(self.overall),
                "cards": {str(k): copy.deepcopy(v) for k, v in self.cards.items()},
                "log": self.log[-300:],
            }


JOB = Job()


SECTOR = 512


def _diskstats():
    """device name -> (sectors_read, sectors_written)."""
    out = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) >= 14:
                    out[p[2]] = (int(p[5]), int(p[9]))
    except OSError:
        pass
    return out


def dev_for_path(path):
    """Base block-device name of the filesystem holding `path` (e.g. nvme0n1p6)."""
    p = os.path.abspath(path)
    while not os.path.exists(p) and p != "/":
        p = os.path.dirname(p)
    src = subprocess.run(["findmnt", "-nro", "SOURCE", "--target", p],
                         text=True, capture_output=True).stdout.strip()
    return os.path.basename(src) if src.startswith("/dev/") else ""


def _fmt_eta(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def sampler_loop(src_devs, dest_dev, cfg, stop_evt):
    """Twice a second: refresh read/write MB/s from /proc/diskstats, copy progress,
    a smoothed per-card copy rate + ETA, an overall job ETA, and (during delete) how
    much of the source has been removed."""
    flatten = cfg["copy"].get("flatten", True)
    rate = {}                       # slot -> {"bytes": last_done, "ema": bytes/sec}
    prev, prev_t = _diskstats(), time.monotonic()
    while not stop_evt.is_set():
        time.sleep(0.5)
        cur, now = _diskstats(), time.monotonic()
        dt = max(1e-3, now - prev_t)
        rd = sum(max(0, cur.get(d, (0, 0))[0] - prev.get(d, (0, 0))[0]) for d in src_devs)
        wr = max(0, cur.get(dest_dev, (0, 0))[1] - prev.get(dest_dev, (0, 0))[1])
        read_mbps = rd * SECTOR / 1e6 / dt
        write_mbps = wr * SECTOR / 1e6 / dt
        prev, prev_t = cur, now

        with JOB.lock:
            JOB.io = {"read_mbps": read_mbps, "write_mbps": write_mbps}
            active = [(v["slot"], v.get("dest"), v.get("src"), v["bytes_total"],
                       v["sessions"], v["stage"])
                      for v in JOB.cards.values()
                      if v["stage"] in ("copying", "verifying", "deleting") and v.get("dest")]

        tot_remaining = tot_rate = 0.0
        for slot, dest, src, bytot, sessions, stage in active:
            done = opm.dir_stats(dest)[0]
            sdone = 0
            for s in sessions:
                sp = os.path.join(opm.dest_path_for(dest, s["path"], flatten), s["name"])
                cb = opm.dir_stats(sp)[0] if os.path.isdir(sp) else 0
                sd = s["bytes"] > 0 and cb >= s["bytes"]
                if sd:
                    sdone += 1
                with JOB.lock:
                    s["copied"], s["done"] = cb, sd

            # smoothed per-card copy rate (bytes/sec) from how fast `done` grows
            st = rate.get(slot)
            inst = max(0.0, (done - st["bytes"]) / dt) if st else 0.0
            ema = (0.4 * st["ema"] + 0.6 * inst) if st else inst
            rate[slot] = {"bytes": done, "ema": ema}

            pct = min(99, int(done * 100 / bytot)) if bytot else 0
            remaining = max(0, bytot - done)
            eta = ""
            if stage == "copying" and ema > 1e5 and remaining > 0:    # >0.1 MB/s
                eta = _fmt_eta(remaining / ema)
            if stage == "copying":
                tot_remaining += remaining
                tot_rate += ema

            del_pct = 0
            if stage == "deleting" and src and bytot:
                src_now = sum(opm.dir_stats(os.path.join(src, p.lstrip("/")))[0]
                              for p in cfg["copy_paths"])
                del_pct = max(0, min(100, int((bytot - src_now) * 100 / bytot)))
            with JOB.lock:
                if slot in JOB.cards:
                    JOB.cards[slot]["progress"].update(
                        percent=pct, bytes_done=done, sessions_done=sdone, eta=eta,
                        del_percent=del_pct, rate_mbps=round(ema / 1e6, 1))

        overall_eta = ""
        if tot_rate > 1e5 and tot_remaining > 0:
            overall_eta = _fmt_eta(tot_remaining / tot_rate)
        with JOB.lock:
            JOB.overall = {"eta": overall_eta, "rate_mbps": round(tot_rate / 1e6, 1),
                           "remaining": int(tot_remaining)}


def run_job(dry, no_delete, slots):
    cfg = CFG
    cards = detect_ready()
    usable = [c for c in cards if c.get("eligible")]
    if slots:
        usable = [c for c in usable if c["slot"] in slots]
    # Web UI: deletion is driven solely by the red "Copy + delete source" button
    # (which sends no_delete=false after a confirm dialog) — there is no delete toggle.
    do_delete = not no_delete and not dry
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    assigned, lock = set(), threading.Lock()

    # pre-scan each card's source: byte/file totals + session date/time/type breakdown
    precomp = {}
    for c in usable:
        mnt = c.get("mounted_at")
        tb = tf = 0
        sessions = []
        for p in cfg["copy_paths"]:
            full = os.path.join(mnt, p.lstrip("/")) if mnt else ""
            if full and os.path.isdir(full):
                b, f, _ = opm.dir_stats(full)
                tb += b
                tf += f
                for s in opm.list_sessions(full):
                    s["path"] = p
                    s["copied"] = 0
                    s["done"] = False
                    sessions.append(s)
        precomp[c["slot"]] = (tb, tf, sessions)

    with JOB.lock:
        JOB.running = True
        JOB.mode = "dry-run" if dry else "live"
        JOB.do_delete = do_delete
        JOB.started_at = ts
        JOB.finished_at = None
        JOB.error = None
        JOB.log = []
        JOB.io = {"read_mbps": 0.0, "write_mbps": 0.0}
        JOB.overall = {"eta": "", "rate_mbps": 0.0, "remaining": 0}
        JOB.cards = {}
        for c in usable:
            tb, tf, sessions = precomp[c["slot"]]
            JOB.cards[c["slot"]] = {
                "slot": c["slot"], "stage": "pending", "status": "pending",
                "card_id": f"slot{c['slot']}", "disk": c["disk"],
                "label": c.get("label"), "size_h": opm._fmt_size(c.get("size")),
                "mounted_at": c.get("mounted_at"),
                "bytes_total": tb, "files_total": tf, "dirs_total": len(sessions),
                "sessions": sessions, "dest": None, "src": c.get("mounted_at"),
                "progress": {"percent": 0, "bytes_done": 0, "sessions_done": 0,
                             "eta": "", "del_percent": 0, "rate_mbps": 0.0},
                "copied": [], "missing": [], "deleted": False, "error": None,
            }
        JOB.append_log(f"Starting {JOB.mode.upper()} on {len(usable)} card(s); "
                       f"delete={'yes' if do_delete else 'no'}")

    stop_evt = threading.Event()
    src_devs = {os.path.basename(c["disk"]) for c in usable}
    dest_dev = dev_for_path(cfg["destination"])
    threading.Thread(target=sampler_loop, args=(src_devs, dest_dev, cfg, stop_evt),
                     daemon=True).start()

    def on_stage(slot, s, info=None):
        with JOB.lock:
            if slot in JOB.cards:
                JOB.cards[slot]["stage"] = s
                if info and info.get("dest"):
                    JOB.cards[slot]["dest"] = info["dest"]
                if info and info.get("src"):
                    JOB.cards[slot]["src"] = info["src"]
                JOB.append_log(f"[slot {slot}] {s}")

    def do_one(card):
        res = opm.process_card(card, cfg, ts, dry, do_delete, assigned, lock,
                               on_stage=on_stage)
        with JOB.lock:
            cur = JOB.cards.get(res["slot"])
            if cur is not None:
                cur.update(status=res["status"], card_id=res["card_id"],
                           copied=res["copied"], missing=res["missing"],
                           deleted=res["deleted"], error=res["error"], stage="done")
                if res["status"] == "ok" and not dry:
                    cur["progress"].update(percent=100, sessions_done=cur["dirs_total"],
                                           bytes_done=cur["bytes_total"], eta="")
                    for s in cur["sessions"]:
                        s["done"], s["copied"] = True, s["bytes"]
                if res["deleted"]:
                    cur["progress"]["del_percent"] = 100
            JOB.append_log(f"[slot {res['slot']}] {res['status']} - "
                           f"copied {len(res['copied'])}, missing {len(res['missing'])}"
                           f"{', deleted source' if res['deleted'] else ''}")
            if res["error"]:
                JOB.append_log(f"[slot {res['slot']}] !! {res['error']}")
        return res

    try:
        if cfg["copy"].get("parallel", True) and len(usable) > 1:
            with ThreadPoolExecutor(max_workers=cfg["copy"].get("max_workers", 4)) as ex:
                list(ex.map(do_one, usable))
        else:
            for c in usable:
                do_one(c)
    except Exception as e:                                # noqa: BLE001
        with JOB.lock:
            JOB.error = str(e)
            JOB.append_log(f"!! job error: {e}")
    finally:
        stop_evt.set()
        with JOB.lock:
            JOB.running = False
            JOB.finished_at = datetime.now().strftime("%Y%m%d-%H%M%S")
            JOB.io = {"read_mbps": 0.0, "write_mbps": 0.0}
            JOB.overall = {"eta": "", "rate_mbps": 0.0, "remaining": 0}
            JOB.append_log("Job finished.")


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep the console quiet
        pass

    # -- helpers --
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, path, ctype):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return self._json({"error": f"missing file: {path}"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file_range(self, path, ctype):
        """Serve a (possibly large) file, honouring a Range request so <video> can
        stream/seek without downloading the whole thing."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({"error": "not found"}, 404)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].partition("-")
            start = int(s) if s.isdigit() else 0
            end = int(e) if e.isdigit() else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _preview(self, want):
        """Resolve ?slot=&session=&file= to a video; serve its thumbnail or a
        browser-playable H.264 preview clip (the originals are HEVC)."""
        q = parse_qs(urlparse(self.path).query)
        v = session_video(q.get("slot", [None])[0], q.get("session", [None])[0],
                          q.get("file", [None])[0])
        if not v:
            return self._json({"error": "no video"}, 404)
        if want == "thumb":
            t = ensure_thumb(v)
            return self._serve_file_range(str(t), "image/jpeg") if t \
                else self._json({"error": "no preview"}, 404)
        clip = ensure_preview(v)
        return self._serve_file_range(str(clip), "video/mp4") if clip \
            else self._json({"error": "transcode failed"}, 500)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                  # noqa: BLE001
            return {}

    # -- routes --
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/detect":
            return self._json(api_detect())
        if path == "/api/config":
            return self._json(api_config_get())
        if path == "/api/status":
            return self._json(JOB.snapshot())
        if path == "/api/thumb":
            return self._preview("thumb")
        if path == "/api/video":
            return self._preview("video")
        if path == "/api/videos":
            q = parse_qs(urlparse(self.path).query)
            return self._json({"videos": session_videos(q.get("slot", [None])[0],
                                                         q.get("session", [None])[0])})
        if path == "/api/listdir":
            q = parse_qs(urlparse(self.path).query)
            return self._json(api_listdir(q.get("path", [None])[0]))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            return self._json(api_config_set(self._body()))
        if path == "/api/precheck":
            return self._json(api_precheck(self._body()))
        if path == "/api/run":
            return self._json(api_run(self._body()))
        if path == "/api/eject":
            return self._json(api_eject(self._body()))
        return self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
# API implementations
# --------------------------------------------------------------------------- #
def api_detect():
    try:
        cards = detect_ready()
    except Exception as e:                                 # noqa: BLE001
        return {"error": str(e), "cards": [], "count": 0, "eligible": 0}
    for c in cards:
        c["size_h"] = opm._fmt_size(c.get("size"))
    return {"cards": cards, "count": len(cards),
            "eligible": sum(1 for c in cards if c.get("eligible")),
            "is_root": os.geteuid() == 0}


def api_config_get():
    placeholder = (not CFG["copy_paths"]
                   or any(opm.PLACEHOLDER in p for p in CFG["copy_paths"]))
    return {
        "destination": CFG["destination"],
        "copy_paths": CFG["copy_paths"],
        "exclude": CFG.get("exclude", []),
        "delete_source": CFG["after_copy"].get("delete_source", False),
        "verify": CFG["after_copy"].get("verify", True),
        "require_label": (CFG.get("safety") or {}).get("require_label"),
        "identify_mode": CFG["identify"].get("mode"),
        "parallel": CFG["copy"].get("parallel", True),
        "is_root": os.geteuid() == 0,
        "placeholder": placeholder,
    }


def api_config_set(body):
    if "copy_paths" in body:
        CFG["copy_paths"] = [str(p).strip() for p in body["copy_paths"] if str(p).strip()]
    if body.get("destination"):
        CFG["destination"] = str(body["destination"]).strip()
    if "delete_source" in body:
        CFG["after_copy"]["delete_source"] = bool(body["delete_source"])
    return api_config_get()


def list_mounts():
    """Mounted, real filesystems (drives) for the destination picker's quick-jump."""
    out, seen = [], set()
    try:
        data = json.loads(subprocess.run(
            ["lsblk", "-J", "-e", "7", "-o", "NAME,MOUNTPOINT,LABEL,TRAN,FSTYPE"],
            text=True, capture_output=True).stdout)
    except Exception:                                  # noqa: BLE001
        return out

    def walk(nodes):
        for n in nodes:
            mp = n.get("mountpoint")
            if (mp and mp.startswith("/") and n.get("fstype")
                    and mp not in seen and not mp.startswith(("/boot", "/snap"))):
                seen.add(mp)
                free, total = opm.disk_free(mp)
                out.append({"path": mp, "label": n.get("label") or n.get("name"),
                            "tran": n.get("tran") or "", "free": free, "total": total})
            walk(n.get("children") or [])
    walk(data.get("blockdevices", []))
    return out


def api_listdir(path):
    """Read-only listing of a directory's sub-folders for the destination picker,
    with free space and quick-jump mounts."""
    user = os.environ.get("SUDO_USER")
    home = os.path.expanduser(f"~{user}") if user else os.path.expanduser("~")
    if not os.path.isdir(home):
        home = "/home"
    path = os.path.realpath(path or home)
    if not os.path.isdir(path):
        path = home if os.path.isdir(home) else "/"
    dirs, err = [], None
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if not name.startswith(".") and os.path.isdir(full):
                dirs.append({"name": name, "path": full})
    except OSError as e:
        err = str(e)
    free, total = opm.disk_free(path)
    return {"path": path, "parent": (os.path.dirname(path) if path != "/" else None),
            "dirs": dirs, "free": free, "total": total, "home": home, "error": err,
            "writable": os.access(path, os.W_OK), "mounts": list_mounts()}


def api_precheck(body):
    """Per-card data size + free space on the card (Orange Pi) and on the
    destination, plus a fits/doesn't-fit verdict and the session breakdown."""
    slots = {int(s) for s in (body.get("slots") or [])}
    cards = detect_ready()
    usable = [c for c in cards
              if c.get("eligible") and (not slots or c["slot"] in slots)]
    dest_free, dest_total = opm.disk_free(CFG["destination"])
    flatten = CFG["copy"].get("flatten", True)
    per, need = [], 0
    for c in usable:
        mnt = c.get("mounted_at")
        cid = opm.card_id_for(mnt, CFG, c["slot"]) if mnt else f"slot{c['slot']}"
        dest_card = os.path.join(CFG["destination"], cid)
        cb = cf = newb = present = 0
        sessions = []
        for p in CFG["copy_paths"]:
            full = os.path.join(mnt, p.lstrip("/")) if mnt else ""
            if full and os.path.isdir(full):
                b, f, _ = opm.dir_stats(full)
                cb += b
                cf += f
                for s in opm.list_sessions(full):
                    s["path"] = p
                    # already transferred? dest session present with matching size + file count
                    dsp = os.path.join(opm.dest_path_for(dest_card, p, flatten), s["name"])
                    ds = opm.dir_stats(dsp) if os.path.isdir(dsp) else (0, 0, 0)
                    s["present"] = (s["bytes"] > 0 and ds[0] >= s["bytes"]
                                    and ds[1] >= s["files"])
                    if s["present"]:
                        present += 1
                    else:
                        newb += s["bytes"]
                    sessions.append(s)
        need += newb
        sfree, stotal = opm.disk_free(mnt) if mnt else (0, 0)
        per.append({"slot": c["slot"], "card_id": cid, "label": c.get("label"),
                    "disk": c["disk"], "mounted_at": mnt, "dest_card": dest_card,
                    "bytes": cb, "new_bytes": newb, "files": cf,
                    "present_count": present, "new_count": len(sessions) - present,
                    "sessions": sessions, "src_free": sfree, "src_total": stotal})
    dest_fs = opm._fs_type(CFG["destination"])
    fat32 = dest_fs.lower() in {"vfat", "fat", "fat12", "fat16", "fat32", "msdos"}
    oversize = []
    if fat32:                                          # FAT32 can't store a file >= 4 GiB
        for c in usable:
            mnt = c.get("mounted_at")
            for p in CFG["copy_paths"]:
                full = os.path.join(mnt, p.lstrip("/")) if mnt else ""
                if full and os.path.isdir(full):
                    for rel, sz in opm.oversized_files(full, 4 * 1024 ** 3):
                        oversize.append({"slot": c["slot"], "file": rel, "bytes": sz})
    return {"destination": CFG["destination"],
            "dest_free": dest_free, "dest_total": dest_total, "dest_fs": dest_fs,
            "dest_is_fat32": fat32, "fat_oversize": oversize,
            "total_new": sum(c["new_count"] for c in per),
            "total_present": sum(c["present_count"] for c in per),
            "need": need, "fits": need <= dest_free, "after_free": dest_free - need,
            "tight": need <= dest_free and (dest_free - need) < 5 * (1024 ** 3),
            "cards": per, "is_root": os.geteuid() == 0,
            "placeholder": (not CFG["copy_paths"]
                            or any(opm.PLACEHOLDER in p for p in CFG["copy_paths"]))}


def api_run(body):
    if os.geteuid() != 0:
        return {"error": "Server is not root, so it cannot mount cards. "
                         "Restart it with:  sudo python3 webui.py"}
    with JOB.lock:
        if JOB.running:
            return {"error": "A job is already running."}
    if not CFG["copy_paths"] or any(opm.PLACEHOLDER in p for p in CFG["copy_paths"]):
        return {"error": f"Set real copy_paths first (still contains {opm.PLACEHOLDER})."}
    cards = detect_ready()
    if not any(c.get("eligible") for c in cards):
        return {"error": "No eligible Orange Pi cards detected."}
    dry = bool(body.get("dry_run"))
    no_delete = bool(body.get("no_delete"))
    slots = body.get("slots") or None
    if slots:
        slots = {int(s) for s in slots}
    if not dry and not body.get("force"):
        pre = api_precheck({"slots": list(slots) if slots else []})
        if not pre["fits"]:
            return {"error": f"Not enough space: need {opm._fmt_size(pre['need'])}, only "
                             f"{opm._fmt_size(pre['dest_free'])} free at {CFG['destination']}. "
                             f"Free space or change the destination.",
                    "precheck": pre}
    threading.Thread(target=run_job, args=(dry, no_delete, slots), daemon=True).start()
    return {"started": True, "dry_run": dry, "no_delete": no_delete}


def api_eject(body):
    """Sync and unmount every filesystem on the card's reader so it's safe to pull."""
    if os.geteuid() != 0:
        return {"error": "Server needs root to unmount. Restart with: sudo python3 webui.py"}
    slot = body.get("slot")
    if slot is None:
        return {"error": "no slot given"}
    card = next((c for c in opm.detect_cards(CFG) if c["slot"] == int(slot)), None)
    if not card:
        return {"error": f"slot {slot} not found"}
    disk = card["disk"]
    subprocess.run(["sync"])
    out = subprocess.run(["lsblk", "-nro", "MOUNTPOINT", disk],
                         text=True, capture_output=True).stdout
    mps = [m.strip() for m in out.splitlines() if m.strip().startswith("/")]
    errors, lazy = [], []
    for m in mps:
        if subprocess.run(["umount", m], text=True, capture_output=True).returncode == 0:
            continue
        # busy (e.g. a file manager has the folder open) -> lazy-detach; already synced
        r2 = subprocess.run(["umount", "-l", m], text=True, capture_output=True)
        if r2.returncode == 0:
            lazy.append(m)
        else:
            errors.append(f"{m}: {(r2.stderr or '').strip() or 'busy'}")
    if errors:
        return {"error": "unmount failed - " + "; ".join(errors)}
    label = card.get("label") or disk
    if lazy:
        msg = (f"slot {slot} ({label}) unmounted (a program still had it open — "
               f"finish any transfers before pulling the card).")
    elif mps:
        msg = f"slot {slot} ({label}) unmounted - safe to remove."
    else:
        msg = f"slot {slot} ({label}) already unmounted - safe to remove."
    return {"ok": True, "slot": int(slot), "unmounted": mps, "lazy": lazy, "message": msg}


def main():
    if not (WEB_DIR / "index.html").exists():
        sys.exit(f"missing web asset: {WEB_DIR / 'index.html'}")
    role = "ROOT (copy + delete enabled)" if os.geteuid() == 0 \
        else "NON-ROOT (detect/preview only; run needs sudo)"
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"opm web UI -> http://{HOST}:{PORT}   [{role}]")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
