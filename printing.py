"""Talks to the Brother QL-1100.

Print jobs go through the Windows spooler (RAW datatype). Printer status - which roll is loaded,
lid open, out of labels... - is read straight from the printer's USB interface, because the
Brother driver doesn't pass those replies back through the spooler.
"""
import ctypes
import itertools
import json
import logging
import os
import threading
import time
import winreg
from ctypes import wintypes

import win32print
from brother_ql.raster import BrotherQLRaster
from PIL import Image, ImageOps

import media as M

PRINTER = "Brother QL-1100"
MODEL = "QL-1100"
BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA_FILE = os.path.join(BASE, "media.json")

log = logging.getLogger("labelprinter")


def build_raster(images, media, cut=True):
    """Brother raster data for `images` (already sized for `media`). Every label is cut unless cut=False."""
    qlr = BrotherQLRaster(MODEL)
    qlr.exception_on_warning = True
    qlr.add_switch_mode()
    qlr.add_invalidate()
    qlr.add_initialize()
    qlr.add_switch_mode()
    last = len(images) - 1
    for i, im in enumerate(images):
        assert im.width == media.print_w, (im.size, media.id)
        assert media.continuous or im.height == media.print_l, (im.size, media.id)
        full = Image.new("L", (M.HEAD_PINS, im.height), 255)
        full.paste(im, (M.HEAD_PINS - media.print_w - media.right, 0))
        bits = ImageOps.invert(full).point(lambda v: 255 if v >= 128 else 0, mode="1")
        qlr.add_status_information()
        qlr.mtype = media.status_type
        qlr.mwidth = media.status_w
        qlr.mlength = 0 if media.continuous else media.status_l
        qlr.pquality = 1
        qlr.add_media_and_quality(im.height)
        qlr.add_autocut(cut)
        if cut:
            qlr.add_cut_every(1)
        qlr.dpi_600 = False
        qlr.cut_at_end = cut
        qlr.two_color_printing = False
        qlr.add_expanded_mode()
        qlr.add_margins(M.FEED if media.continuous else 0)
        qlr.add_compression(True)
        qlr.add_raster_data(bits)
        qlr.add_print(last_page=(i == last))
    return qlr.data


def send_raw(data, title):
    h = win32print.OpenPrinter(PRINTER)
    try:
        job_id = win32print.StartDocPrinter(h, 1, (title, None, "RAW"))
        try:
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, data)
            win32print.EndPagePrinter(h)
        finally:
            win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)
    return job_id


def spooler_jobs():
    h = win32print.OpenPrinter(PRINTER)
    try:
        return win32print.GetPrinter(h, 2)["cJobs"]
    finally:
        win32print.ClosePrinter(h)


# --------------------------------------------------------------------------- direct USB status

USBPRINT_CLASS = r"SYSTEM\CurrentControlSet\Control\DeviceClasses\{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"
ERROR_IO_PENDING = 997
NOT_CONNECTED = {2, 3, 21, 1167}            # file/path not found, not ready, device not connected

ERR1 = {0x01: ("no_media", "No labels loaded"), 0x04: ("cutter_jam", "Cutter jam - check the cutter"),
        0x20: ("off", "Printer is turned off")}
ERR2 = {0x10: ("cover_open", "Lid is open"), 0x40: ("media_end", "Out of labels"),
        0x01: ("replace_media", "Wrong roll for that print"), 0x80: ("system", "Printer error - turn it off and on again"),
        0x04: ("comm", "Printer communication error")}
# problems that make printing pointless until they're fixed
BLOCKING = {"offline", "off", "no_media", "cover_open", "media_end", "cutter_jam", "system", "unsupported", "missing"}


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
for _fn in (_k32.WriteFile, _k32.ReadFile):
    _fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(_Overlapped)]
_k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.CancelIo.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
INVALID_HANDLE = wintypes.HANDLE(-1).value


def parse_status(data):
    """Decodes the QL-1100's 32-byte status reply (Raster Command Reference, ESC i S)."""
    if len(data) < 32 or data[0] != 0x80 or data[2] != 0x42:
        return None
    problems = [p for bit, p in ERR2.items() if data[9] & bit] + [p for bit, p in ERR1.items() if data[8] & bit]
    order = ["cover_open", "no_media", "media_end", "cutter_jam", "replace_media", "system", "off", "comm"]
    problems.sort(key=lambda p: order.index(p[0]))
    return {"media_type": data[11], "width": data[10], "length": data[17], "problems": problems,
            "media": M.from_status(data[11], data[10], data[17]) if data[11] else None}


class PrinterLink:
    """Reads status straight from the printer over its USB printer interface. The lock is shared
    with print jobs so a status request never lands in the middle of a job."""

    REQUEST = b"\x00" * 200 + b"\x1b\x40" + b"\x1b\x69\x53"     # invalidate, initialise, status request

    def __init__(self):
        self.lock = threading.Lock()

    def _paths(self):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, USBPRINT_CLASS) as key:
                names = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
        except OSError:
            return []
        names = [n for n in names if "vid_04f9" in n.lower()]
        names.sort(key=lambda n: "pid_20a7" not in n.lower())        # the QL-1100 first
        return ["\\\\?\\" + n[4:] for n in names if n.startswith("##?#")]

    def _io(self, handle, fn, buf, n, timeout_ms):
        ov = _Overlapped()
        ov.hEvent = _k32.CreateEventW(None, True, False, None)
        done = wintypes.DWORD(0)
        try:
            if not fn(handle, buf, n, ctypes.byref(done), ctypes.byref(ov)):
                if ctypes.get_last_error() != ERROR_IO_PENDING:
                    return -1
                if _k32.WaitForSingleObject(ov.hEvent, timeout_ms) != 0:
                    _k32.CancelIo(handle)
                    _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(done), True)
                    return 0
                _k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(done), False)
            return done.value
        finally:
            _k32.CloseHandle(ov.hEvent)

    def query(self):
        """Returns a parsed status dict, "offline" if the printer isn't connected, or None if unknown."""
        with self.lock:
            try:
                if spooler_jobs():
                    return None                      # a job is being sent: never interrupt it
            except Exception:
                return None
            paths = self._paths()
            if not paths:
                return "offline"
            handle, gone = None, True
            for path in paths:
                h = _k32.CreateFileW(path, 0x80000000 | 0x40000000, 1 | 2, None, 3, 0x40000000, None)
                if h not in (None, INVALID_HANDLE):
                    handle = h
                    break
                gone = gone and ctypes.get_last_error() in NOT_CONNECTED
            if handle is None:
                return "offline" if gone else None
            try:
                buf = ctypes.create_string_buffer(64)
                for _ in range(4):                   # drain leftover status messages
                    if self._io(handle, _k32.ReadFile, buf, 32, 50) <= 0:
                        break
                req = ctypes.create_string_buffer(self.REQUEST, len(self.REQUEST))
                if self._io(handle, _k32.WriteFile, req, len(self.REQUEST), 2000) != len(self.REQUEST):
                    return None
                data, deadline = b"", time.monotonic() + 2.0
                while time.monotonic() < deadline and len(data) < 32:
                    n = self._io(handle, _k32.ReadFile, buf, 32, 300)
                    if n > 0:
                        data += buf.raw[:n]
                    else:
                        time.sleep(0.08)
                return parse_status(data[-32:]) if len(data) >= 32 else None
            finally:
                _k32.CloseHandle(handle)


LINK = PrinterLink()


# --------------------------------------------------------------------------- loaded media (remembered)

class MediaState:
    def __init__(self):
        self.media, self.detected = M.DEFAULT, False
        try:
            with open(MEDIA_FILE, encoding="utf-8") as f:
                self.media = M.MEDIA.get(json.load(f).get("id"), M.DEFAULT)
        except Exception:
            pass

    def set(self, media):
        self.detected = True
        if media is not self.media:
            log.info("loaded media changed: %s -> %s", self.media.id, media.id)
            self.media = media
            try:
                with open(MEDIA_FILE, "w", encoding="utf-8") as f:
                    json.dump({"id": media.id}, f)
            except OSError:
                pass


MEDIA = MediaState()


def current_media():
    return MEDIA.media


# --------------------------------------------------------------------------- combined status

PS = {0x1: "Printer is paused", 0x2: "Printer error", 0x8: "Paper jam", 0x10: "Out of labels",
      0x40: "Label roll problem", 0x80: "Printer is offline", 0x1000: "Printer not available",
      0x100000: "Printer needs attention", 0x400000: "Printer cover is open"}
JS_ERR = {0x2: "Printer error", 0x20: "Printer is offline", 0x40: "Out of labels",
          0x200: "Printer error - check the printer", 0x400: "Printer needs attention", 0x1: "Job paused"}

_status_cache = {"t": 0, "v": None}
_status_lock = threading.Lock()


def printer_status():
    with _status_lock:
        now = time.monotonic()
        if _status_cache["v"] and now - _status_cache["t"] < 2:
            return _status_cache["v"]
        v = _read_status()
        _status_cache.update(t=time.monotonic(), v=v)
        return v


def _read_status():
    try:
        h = win32print.OpenPrinter(PRINTER)
        try:
            info = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
    except Exception as e:
        log.warning("status failed: %s", e)
        return _with_media({"state": "error", "message": f"Printer '{PRINTER}' isn't installed", "problem": "missing", "jobs": 0})
    st, attrs, jobs = info["Status"], info["Attributes"], info["cJobs"]
    if jobs or st & 0x400:
        return _with_media({"state": "printing", "message": f"Printing ({jobs} in queue)" if jobs > 1 else "Printing",
                            "problem": None, "jobs": jobs})
    r = LINK.query()
    if r == "offline":
        return _with_media({"state": "error", "message": "Printer is off or unplugged", "problem": "offline", "jobs": 0})
    if isinstance(r, dict):
        if r["media"]:
            MEDIA.set(r["media"])
        if r["problems"]:
            code, msg = r["problems"][0]
        elif not r["media_type"] or not r["width"]:
            code, msg = "no_media", "No labels loaded"
        elif not r["media"]:
            code, msg = "unsupported", f"Unsupported roll loaded ({r['width']} mm)"
        else:
            return _with_media({"state": "ready", "message": "Ready", "problem": None, "jobs": 0})
        return _with_media({"state": "error", "message": msg, "problem": code, "jobs": 0})
    # status unknown (device busy or no reply): fall back to what Windows knows
    problems = [msg for bit, msg in PS.items() if st & bit]
    if attrs & 0x400 and "Printer is offline" not in problems:
        problems.append("Printer is offline")
    if problems:
        return _with_media({"state": "error", "message": problems[0], "problem": None, "jobs": 0})
    return _with_media({"state": "ready", "message": "Ready", "problem": None, "jobs": 0})


def _with_media(v):
    v["media"] = MEDIA.media.to_json()
    v["detected"] = MEDIA.detected
    v["blocking"] = v.get("problem") in BLOCKING
    return v


def invalidate_status():
    _status_cache["t"] = 0


# --------------------------------------------------------------------------- jobs

class Jobs:
    def __init__(self):
        self._jobs = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def submit(self, images, media, title, cut=True):
        data = build_raster(images, media, cut)
        jid = next(self._ids)
        job = {"id": jid, "state": "sending", "message": "Sending to printer...",
               "labels": len(images), "created": time.time(), "spool_id": None}
        with self._lock:
            self._jobs[jid] = job
            for k in [k for k, j in self._jobs.items() if time.time() - j["created"] > 3600]:
                del self._jobs[k]
        invalidate_status()
        try:
            with LINK.lock:                          # never overlap a direct status request
                job["spool_id"] = send_raw(data, title)
        except Exception as e:
            log.exception("print failed")
            job.update(state="error", message=f"Could not send to printer: {e}")
            return job
        job.update(state="printing", message="Printing...")
        threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        log.info("job %s: %d label(s) on %s, %d bytes", jid, len(images), media.id, len(data))
        return job

    def _watch(self, job):
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                h = win32print.OpenPrinter(PRINTER)
                try:
                    info = win32print.GetJob(h, job["spool_id"], 1)
                finally:
                    win32print.ClosePrinter(h)
            except Exception:       # job left the queue -> finished
                job.update(state="done", message="Printed")
                invalidate_status()
                return
            st = info["Status"]
            err = next((m for b, m in JS_ERR.items() if st & b), None)
            if err:
                job.update(state="error", message=err)
            elif st & 0x4:
                job.update(state="error", message="Cancelled")
            else:
                job.update(state="printing", message="Printing..." if st & 0x10 else "Waiting in queue...")
            time.sleep(0.5)
        job.update(state="error", message="Still waiting - check the printer")

    def get(self, jid):
        return self._jobs.get(jid)

    def cancel(self, jid):
        job = self._jobs.get(jid)
        if not job or not job["spool_id"]:
            return False
        try:
            h = win32print.OpenPrinter(PRINTER)
            try:
                win32print.SetJob(h, job["spool_id"], 0, None, win32print.JOB_CONTROL_DELETE)
            finally:
                win32print.ClosePrinter(h)
            job.update(state="error", message="Cancelled")
            return True
        except Exception as e:
            log.warning("cancel failed: %s", e)
            return False
