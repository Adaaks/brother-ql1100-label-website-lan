"""labelprinter - LAN label printing server for a Brother QL-1100 (detects the loaded DK roll).

Serves http://labelprinter.local/ (mDNS) and http://<this-pc-ip>/ on port 80.
"""
import base64
import glob
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from PIL import Image

import media as M
import render
from printing import Jobs, current_media, printer_status
from render import EmptyDesign, RenderError

BASE = os.path.dirname(os.path.abspath(__file__))
HOSTNAME = "labelprinter"
PORTS = [80, 8080]
MAX_COPIES = 100
MAX_PREVIEW_PAGES = 12
HIST_DIR = os.path.join(BASE, "history")
HIST_MAX = 30

os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
log = logging.getLogger("labelprinter")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(BASE, "logs", "server.log"), maxBytes=1_000_000, backupCount=2)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)
logging.getLogger("waitress").addHandler(_h)

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
jobs = Jobs()

# ---------------------------------------------------------------- uploads (kept in memory, 1 h)
uploads = {}
uploads_lock = threading.Lock()


def _prune_uploads():
    with uploads_lock:
        now = time.time()
        for k in [k for k, u in uploads.items() if now - u["t"] > 3600]:
            del uploads[k]
        while len(uploads) > 20:
            del uploads[min(uploads, key=lambda k: uploads[k]["t"])]


# ---------------------------------------------------------------- print history (on disk)

def primary_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))     # no packet is sent; picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class History:
    def __init__(self):
        os.makedirs(HIST_DIR, exist_ok=True)
        self.lock = threading.Lock()
        self.path = os.path.join(HIST_DIR, "index.json")
        self.names = {}          # ip -> device name (reverse DNS), cached
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = json.load(f)
        except Exception:
            self.items = []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f)
        os.replace(tmp, self.path)

    def _remove_files(self, hid):
        for p in glob.glob(os.path.join(HIST_DIR, f"{hid}_*.png")):
            try:
                os.remove(p)
            except OSError:
                pass

    def add(self, kind, title, images, rotated, copies, spec, ip, media):
        hid = uuid.uuid4().hex[:12]
        for n, im in enumerate(images):
            im.convert("1").save(os.path.join(HIST_DIR, f"{hid}_{n}.png"), optimize=True)
        local = ip in ("127.0.0.1", "::1") or ip == primary_ip()
        geom = [media.geometry(im.height) for im in images]
        entry = {"id": hid, "t": time.time(), "kind": kind, "title": (title or "Label")[:80],
                 "count": len(images), "copies": copies, "media": media.id, "media_name": media.name,
                 "geom": geom, "lengths": [g["length_mm"] for g in geom],
                 "rotated": list(rotated), "spec": spec, "who": "This PC" if local else self.names.get(ip, ip)}
        with self.lock:
            self.items.insert(0, entry)
            for old in self.items[HIST_MAX:]:
                self._remove_files(old["id"])
            self.items = self.items[:HIST_MAX]
            self._save()
        if not local and ip not in self.names:
            threading.Thread(target=self._resolve, args=(hid, ip), daemon=True).start()
        return entry

    def _resolve(self, hid, ip):
        try:
            name = socket.gethostbyaddr(ip)[0].split(".")[0]
        except Exception:
            return
        self.names[ip] = name
        with self.lock:
            for e in self.items:
                if e["id"] == hid:
                    e["who"] = name
            self._save()

    def get(self, hid):
        return next((e for e in self.items if e["id"] == hid), None)

    def images(self, hid):
        e = self.get(hid)
        return [Image.open(os.path.join(HIST_DIR, f"{hid}_{n}.png")).convert("L") for n in range(e["count"])]

    def bump(self, hid):
        with self.lock:
            e = self.get(hid)
            if e:
                self.items.remove(e)
                e["t"] = time.time()
                self.items.insert(0, e)
                self._save()

    def delete(self, hid):
        with self.lock:
            e = self.get(hid)
            if e:
                self.items.remove(e)
                self._save()
                self._remove_files(hid)
        return bool(e)


history = History()


# ---------------------------------------------------------------- rendering helpers

def _png(img):
    buf = io.BytesIO()
    img.convert("1").save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _upload(uid):
    up = uploads.get(uid)
    if not up:
        raise RenderError("Upload expired - please upload the file again.")
    up["t"] = time.time()
    return up


def _render(payload, media, limit=None):
    """Returns (list of (design_canvas, rotated), warnings, total_pages) for the given roll."""
    kind = payload.get("kind")
    if kind == "text":
        canvas, warnings, rotated = render.render_text(payload.get("text") or {}, media)
        return [(canvas, rotated)], warnings, 1
    if kind == "file":
        spec = payload.get("file") or {}
        pages = _upload(spec.get("id"))["pages"]
        sel = spec.get("pages")
        idx = [i for i in sel if 0 <= i < len(pages)] if isinstance(sel, list) else list(range(len(pages)))
        if not idx:
            raise RenderError("No pages selected.")
        out, warnings = [], []
        for i in idx[:limit] if limit else idx:
            canvas, w = render.render_image(pages[i], spec, media)
            out.append((canvas, False))
            warnings += [x for x in w if x not in warnings]
        return out, warnings, len(idx)
    raise RenderError("Nothing to print.")


def _title(payload):
    if payload.get("kind") == "file":
        up = uploads.get((payload.get("file") or {}).get("id")) or {}
        return up.get("name") or "Uploaded label"
    for b in (payload.get("text") or {}).get("blocks", []):
        line = next((l.strip() for l in str(b.get("text", "")).splitlines() if l.strip()), "")
        if line:
            return line
    return "Label"


def _err(msg, code=400, **extra):
    return jsonify({"error": msg, **extra}), code


def _public(job):
    return {k: v for k, v in job.items() if k != "created"}


def _printer_problem():
    """An error response when the printer can't print right now (no labels, lid open, off...)."""
    st = printer_status()
    return _err(st["message"], 409, problem=st["problem"]) if st.get("blocking") else None


@app.errorhandler(RenderError)
def _render_error(e):
    return _err(str(e), empty=isinstance(e, EmptyDesign))


@app.errorhandler(413)
def _too_big(e):
    return _err("File is too large (max 40 MB).", 413)


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/info")
def info():
    popular, rest = render.FONTS.names()
    return jsonify({
        "media": current_media().to_json(),
        "dpi": render.DPI,
        "fonts_popular": popular,
        "fonts_other": rest,
    })


@app.get("/api/status")
def status():
    return jsonify(printer_status())


@app.get("/api/font-sample")
def font_sample():
    png = render.font_sample(request.args.get("f", ""))
    if not png:
        return _err("Unknown font", 404)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f:
        return _err("No file received.")
    data = f.read()
    if not data:
        return _err("The file is empty.")
    pages = render.load_upload(data, f.filename or "")
    _prune_uploads()
    uid = uuid.uuid4().hex
    with uploads_lock:
        uploads[uid] = {"pages": pages, "name": f.filename, "t": time.time()}
    auto = "vinted" if any(p.get("auto") == "vinted" for p in pages) else None
    log.info("upload %s (%d page(s)%s) from %s", f.filename, len(pages), ", vinted crop" if auto else "", request.remote_addr)
    return jsonify({"id": uid, "name": f.filename, "pages": len(pages), "auto": auto})


@app.get("/api/upload/<uid>/page/<int:n>")
def upload_page(uid, n):
    pages = _upload(uid)["pages"]
    if not 0 <= n < len(pages):
        return _err("No such page", 404)
    resp = Response(render.page_preview(pages[n]), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.post("/api/preview")
def preview():
    payload = request.get_json(force=True, silent=True) or {}
    media = current_media()
    items, warnings, total = _render(payload, media, MAX_PREVIEW_PAGES)
    pages = []
    for c, r in items:
        img = render.to_print(c, r)
        pages.append({"png": _png(img), "rotated": r, "at_min": bool(c.info.get("at_min")), **media.geometry(img.height)})
    return jsonify({"pages": pages, "total": total, "media": media.to_json(), "warnings": warnings})


@app.post("/api/print")
def do_print():
    payload = request.get_json(force=True, silent=True) or {}
    problem = _printer_problem()
    if problem:
        return problem
    media = current_media()
    items, _, _ = _render(payload, media)
    copies = min(MAX_COPIES, max(1, int(payload.get("copies", 1))))
    labels = [render.to_print(c, r) for c, r in items]
    if len(labels) * copies > 300:
        return _err("That's too many labels for one job (max 300).")
    kind = payload.get("kind")
    job = jobs.submit(labels * copies, media, f"labelprinter {kind}")
    log.info("print request from %s: %d label(s)", request.remote_addr, len(labels) * copies)
    if job["state"] != "error":
        try:
            spec = payload.get("text") if kind == "text" else None
            history.add(kind, _title(payload), labels, [r for _, r in items], copies, spec, request.remote_addr, media)
        except Exception:
            log.exception("could not save history")
    return jsonify(_public(job))


@app.get("/api/jobs/<int:jid>")
def job_status(jid):
    job = jobs.get(jid)
    if not job:
        return _err("Unknown job", 404)
    return jsonify(_public(job))


@app.post("/api/jobs/<int:jid>/cancel")
def job_cancel(jid):
    return jsonify({"ok": jobs.cancel(jid)})


HID = re.compile(r"[0-9a-f]{12}")


@app.get("/api/history")
def history_list():
    return jsonify(history.items)


@app.get("/api/history/<hid>/<name>.png")
def history_image(hid, name):
    if not HID.fullmatch(hid) or not (name == "thumb" or name.isdigit()):
        return _err("Not found", 404)
    path = os.path.join(HIST_DIR, f"{hid}_{name}.png")
    if not os.path.exists(path):
        return _err("Not found", 404)
    return send_file(path, mimetype="image/png", max_age=31536000)


@app.post("/api/history/<hid>/reprint")
def history_reprint(hid):
    e = history.get(hid) if HID.fullmatch(hid) else None
    if not e:
        return _err("That label is no longer in the history.", 404)
    problem = _printer_problem()
    if problem:
        return problem
    media = current_media()
    made_for = M.MEDIA.get(e.get("media", M.DEFAULT.id), M.DEFAULT)
    if made_for is media:
        images = history.images(hid)
    elif e.get("kind") == "text" and e.get("spec"):          # re-make text labels for the loaded roll
        canvas, _, rotated = render.render_text(e["spec"], media)
        images = [render.to_print(canvas, rotated)]
    else:
        return _err(f"This label was made for {made_for.name}. Load that roll to print it again.", 409, problem="other_roll")
    job = jobs.submit(images * e["copies"], media, "labelprinter reprint")
    log.info("reprint %s from %s: %d label(s)", hid, request.remote_addr, len(images) * e["copies"])
    if job["state"] != "error":
        history.bump(hid)
    return jsonify(_public(job))


@app.delete("/api/history/<hid>")
def history_delete(hid):
    return jsonify({"ok": HID.fullmatch(hid) is not None and history.delete(hid)})


# ---------------------------------------------------------------- mDNS (labelprinter.local)

def mdns_loop(port):
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
    zc, info, current = None, None, None
    while True:
        ip = primary_ip()
        if ip != current:
            try:
                if zc:
                    zc.unregister_all_services()
                    zc.close()
                    zc = None
                if ip:
                    info = ServiceInfo(
                        "_http._tcp.local.", "labelprinter._http._tcp.local.",
                        addresses=[socket.inet_aton(ip)], port=port,
                        properties={"path": "/"}, server=f"{HOSTNAME}.local.")
                    zc = Zeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
                    zc.register_service(info, allow_name_change=True)
                    log.info("mDNS: %s.local -> %s:%d", HOSTNAME, ip, port)
                current = ip
            except Exception:
                log.exception("mDNS registration failed")
                current = None
        time.sleep(30)


# ---------------------------------------------------------------- main

def main():
    import win32api, win32event, winerror
    from waitress import create_server
    mutex = win32event.CreateMutex(None, False, "labelprinter-server")  # noqa: F841 (held for process life)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        log.info("already running, exiting")
        return
    render.FONTS.load()
    server = None
    for port in PORTS:
        try:
            server = create_server(app, host="0.0.0.0", port=port, threads=6, ident="labelprinter")
            break
        except OSError as e:
            log.warning("port %d unavailable: %s", port, e)
    if not server:
        log.error("no port available, exiting")
        sys.exit(1)
    threading.Thread(target=mdns_loop, args=(port,), daemon=True).start()
    threading.Thread(target=printer_status, daemon=True).start()      # learn which roll is loaded
    log.info("labelprinter running on port %d (%d font families)", port, len(render.FONTS.families))
    print(f"labelprinter running: http://{HOSTNAME}.local{'' if port == 80 else ':%d' % port}/")
    server.run()


if __name__ == "__main__":
    main()
