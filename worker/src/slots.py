"""Display-slot pool: MAX_SLOTS concurrent browsers, each on its own Xvfb
display mirrored by its own x11vnc. websockify (started by entrypoint.sh with
--token-plugin=TokenFile) maps token=<jobId> to the slot's VNC port via a file
in VNC_TOKEN_DIR, so the live URL is stable per job:

    https://<DOMAIN>/vnc.html?autoconnect=true&resize=scale
         &path=websockify%3Ftoken%3D<jobId>
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from config import (
    BASE_DISPLAY, BASE_VNC_PORT, DOMAIN, SCREEN_GEOMETRY, VNC_TOKEN_DIR,
)


def live_url(job_id: str) -> str:
    return (
        f"https://{DOMAIN}/vnc.html?autoconnect=true&resize=scale"
        f"&path=websockify%3Ftoken%3D{job_id}"
    )


class Slot:
    def __init__(self, index: int):
        self.index = index
        self.display = f":{BASE_DISPLAY + index}"
        self.vnc_port = BASE_VNC_PORT + index
        self.xvfb: subprocess.Popen | None = None
        self.vnc: subprocess.Popen | None = None


class SlotPool:
    def __init__(self, size: int):
        self._slots = [Slot(i) for i in range(size)]
        self._free = list(range(size))
        self._lock = threading.Lock()
        os.makedirs(VNC_TOKEN_DIR, exist_ok=True)

    def try_acquire(self, job_id: str) -> Slot | None:
        with self._lock:
            if not self._free:
                return None
            slot = self._slots[self._free.pop(0)]
        try:
            slot.xvfb = subprocess.Popen(
                ["Xvfb", slot.display, "-screen", "0", SCREEN_GEOMETRY,
                 "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.7)
            slot.vnc = subprocess.Popen(
                ["x11vnc", "-display", slot.display, "-rfbport",
                 str(slot.vnc_port), "-forever", "-shared", "-nopw", "-quiet",
                 "-noxdamage"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with open(os.path.join(VNC_TOKEN_DIR, job_id), "w") as f:
                f.write(f"{job_id}: localhost:{slot.vnc_port}\n")
            print(f"[slots] job={job_id} -> slot {slot.index} "
                  f"(display {slot.display}, vnc :{slot.vnc_port})")
            return slot
        except Exception as e:
            print(f"[slots] failed to start slot {slot.index}: {e}")
            self.release(slot, job_id)
            return None

    def release(self, slot: Slot, job_id: str) -> None:
        for proc in (slot.vnc, slot.xvfb):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        slot.vnc = slot.xvfb = None
        try:
            os.remove(os.path.join(VNC_TOKEN_DIR, job_id))
        except FileNotFoundError:
            pass
        with self._lock:
            if slot.index not in self._free:
                self._free.append(slot.index)
        print(f"[slots] slot {slot.index} released (job={job_id})")
