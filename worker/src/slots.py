# worker/src/slots.py
"""Display-slot pool for concurrent, individually-viewable runs.

Each concurrent job gets a slot: an Xvfb virtual display, an x11vnc server on a
per-slot port, and a websockify token (the jobId) that maps to that port. The
app's live-view URL is then:

    https://<DOMAIN>/vnc.html?path=websockify%3Ftoken%3D<jobId>&autoconnect=true

websockify reads token->host:port from a directory of token files (the standard
token-plugin layout), so attaching a job to a slot is just writing one file.

Slot i (0-based):
    display  = ':{BASE_DISPLAY + i}'
    vnc port = BASE_VNC_PORT + i
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass

from config import BASE_DISPLAY, BASE_VNC_PORT, MAX_SLOTS, SCREEN_GEOMETRY

TOKEN_DIR = os.environ.get("VNC_TOKEN_DIR", "/tmp/vnc-tokens")


@dataclass
class Slot:
    index: int
    display: str
    vnc_port: int
    _procs: list[subprocess.Popen]


class SlotPool:
    def __init__(self, size: int = MAX_SLOTS):
        self.size = size
        self._free: list[int] = list(range(size))
        self._slots: dict[int, Slot] = {}
        self._lock = threading.Lock()
        os.makedirs(TOKEN_DIR, exist_ok=True)

    def acquire(self, job_id: str) -> Slot | None:
        with self._lock:
            if not self._free:
                return None
            i = self._free.pop(0)

        display = f":{BASE_DISPLAY + i}"
        vnc_port = BASE_VNC_PORT + i
        procs: list[subprocess.Popen] = []

        # Xvfb virtual display.
        procs.append(subprocess.Popen(
            ["Xvfb", display, "-screen", "0", SCREEN_GEOMETRY, "-ac", "+extension", "GLX"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        # x11vnc bound to that display.
        procs.append(subprocess.Popen(
            ["x11vnc", "-display", display, "-rfbport", str(vnc_port),
             "-forever", "-shared", "-nopw", "-quiet", "-bg", "-noxdamage"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))

        # Token file: <jobId>: localhost:<vnc_port>
        with open(os.path.join(TOKEN_DIR, job_id), "w") as f:
            f.write(f"{job_id}: localhost:{vnc_port}\n")

        slot = Slot(index=i, display=display, vnc_port=vnc_port, _procs=procs)
        with self._lock:
            self._slots[i] = slot
        print(f"[slots] acquired slot {i} display={display} vnc={vnc_port} for job={job_id}")
        return slot

    def release(self, slot: Slot, job_id: str) -> None:
        for p in slot._procs:
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:
                pass
        try:
            os.remove(os.path.join(TOKEN_DIR, job_id))
        except FileNotFoundError:
            pass
        with self._lock:
            self._slots.pop(slot.index, None)
            self._free.append(slot.index)
            self._free.sort()
        print(f"[slots] released slot {slot.index} for job={job_id}")
