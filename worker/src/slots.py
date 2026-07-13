"""Display-slot pool: MAX_SLOTS concurrent browsers, each on its own Xvfb
display mirrored by its own x11vnc. websockify (started by entrypoint.sh with
--token-plugin=TokenFile) maps token=<jobId> to the slot's VNC port via a file
in VNC_TOKEN_DIR, so the live URL is stable per job:

    https://<DOMAIN>/vnc.html?autoconnect=true&resize=scale
         &path=websockify%3Ftoken%3D<jobId>
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

import psutil  # already in the venv (browser-use dependency)

from config import (
    BASE_DISPLAY,
    BASE_VNC_PORT,
    DOMAIN,
    SCREEN_GEOMETRY,
    VNC_TOKEN_DIR,
)

# Prefixes of the temp Chromium profiles browser-use mkdtemp()s per launch.
# When a launch is cancelled at the bubus timeout, the dir is never cleaned,
# and leaked profiles eat the container's disk until every subsequent launch
# starts timing out.
PROFILE_DIR_PREFIXES = ("browser-use-user-data-dir-", "browseruse-tmp-")


def sweep_orphan_profile_dirs(min_age_secs: int = 60) -> int:
    """Delete leaked Chromium temp profiles under /tmp.

    Only removes dirs that (a) are not referenced in any live process's
    cmdline and (b) are older than min_age_secs — so profiles belonging to
    jobs still running on other slots are never touched.
    """
    in_use: set[str] = set()
    for proc in psutil.process_iter(["cmdline"]):
        try:
            for arg in proc.info["cmdline"] or []:
                if any(pfx in arg for pfx in PROFILE_DIR_PREFIXES):
                    # e.g. --user-data-dir=/tmp/browser-use-user-data-dir-xyz
                    in_use.add(arg.split("=", 1)[-1])
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue

    removed = 0
    now = time.time()
    for entry in os.listdir("/tmp"):
        if not any(entry.startswith(pfx) for pfx in PROFILE_DIR_PREFIXES):
            continue
        path = f"/tmp/{entry}"
        if any(path in used for used in in_use):
            continue
        try:
            if now - os.path.getmtime(path) < min_age_secs:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    if removed:
        print(f"[slots] Swept {removed} orphaned Chromium profile dir(s) from /tmp")
    return removed


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

    def free_count(self) -> int:
        with self._lock:
            return len(self._free)

    def try_acquire(self, job_id: str) -> Slot | None:
        with self._lock:
            if not self._free:
                return None
            slot = self._slots[self._free.pop(0)]
        try:
            slot.xvfb = subprocess.Popen(
                [
                    "Xvfb",
                    slot.display,
                    "-screen",
                    "0",
                    SCREEN_GEOMETRY,
                    "-nolisten",
                    "tcp",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.7)
            slot.vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    slot.display,
                    "-rfbport",
                    str(slot.vnc_port),
                    "-forever",
                    "-shared",
                    "-nopw",
                    "-quiet",
                    "-noxdamage",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with open(os.path.join(VNC_TOKEN_DIR, job_id), "w") as f:
                f.write(f"{job_id}: localhost:{slot.vnc_port}\n")
            print(
                f"[slots] job={job_id} -> slot {slot.index} "
                f"(display {slot.display}, vnc :{slot.vnc_port})"
            )
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
        # main.py killpg'd the job's process group and waited 0.5s before
        # releasing, so anything still holding a profile dir here is a
        # genuine cross-slot survivor the sweep must leave alone.
        sweep_orphan_profile_dirs()
        print(f"[slots] slot {slot.index} released (job={job_id})")
