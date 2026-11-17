"""Video player source: plays files / playlists / live URLs (mp4, mov, mkv, webm, m3u8 HLS, rtmp, rtsp, srt, ...) into the
same stream the games use. ffmpeg decodes (any codec ffmpeg knows), scales to the stream size and hands raw frames + audio
to the main encoder, so switching between games and videos never drops the connection to YouTube / Kick.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections import deque

from .audio import SR

log = logging.getLogger("player")

VIDEO_EXT = (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv", ".3gp",
             ".ogv", ".m3u8", ".m3u", ".mp3", ".aac", ".wav", ".ogg", ".flac", ".m4a")
URL_RX = re.compile(r"^(https?|rtmps?|rtsp|srt|udp|tcp)://", re.I)
WHITELIST = "file,http,https,tcp,tls,crypto,rtmp,rtmps,rtsp,rtp,udp,srt,hls"


def is_url(s):
    return bool(URL_RX.match(s or ""))


def safe_name(name):
    name = os.path.basename(str(name).replace("\\", "/")).strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._ \-()]", "_", name)[:120].lstrip(".")
    return name


def expand(src, video_dir):
    """Turn one configured source into playable items: URL, file, or every media file of a folder."""
    if is_url(src):
        return [src]
    path = src if os.path.isabs(src) else os.path.join(video_dir, src)
    if os.path.isdir(path):
        return [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.lower().endswith(VIDEO_EXT)]
    return [path] if os.path.exists(path) else []


def has_audio(src):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", WHITELIST, "-select_streams", "a:0",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", src],
                           capture_output=True, text=True, timeout=15)
        return "audio" in r.stdout
    except Exception:
        return True


class Player:
    def __init__(self, width, height, fps, video_dir):
        self.w, self.h, self.fps, self.video_dir = width, height, fps, video_dir
        self.fsize = width * height * 3
        self.sources, self.loop = [], True
        self.items, self.idx = [], 0
        self.proc = None
        self.vq, self.abuf = deque(maxlen=12), bytearray()
        self.lock = threading.Lock()
        self.current, self.started, self.ended = "", 0.0, False
        self.last_frame = None
        self.error = ""
        self.gen = 0
        self.fails = 0

    # ---- playlist
    def set_sources(self, sources, loop=True):
        self.sources, self.loop = [s for s in sources if s], loop

    def playlist(self):
        items = []
        for s in self.sources:
            items += expand(s, self.video_dir)
        return items

    def start(self, index=0):
        self.items = self.playlist()
        self.ended = False
        if not self.items:
            self.error = "nothing to play - add a video file or a URL first"
            self.ended = True
            return False
        self.error, self.fails = "", 0
        self._open(index % len(self.items))
        return True

    def stop(self):
        self.gen += 1
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()
        if p:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        with self.lock:
            self.vq.clear()
            self.abuf.clear()

    def next(self):
        if not self.items:
            return
        self.idx += 1
        if self.idx >= len(self.items):
            if not self.loop:
                self.stop()
                self.ended = True
                return
            self.items = self.playlist() or self.items            # pick up newly uploaded files
            self.idx = 0
        self._open(self.idx)

    # ---- decoding
    def _open(self, i):
        self.stop()
        self.idx, src = i, self.items[i]
        self.current = src if is_url(src) else os.path.basename(src)
        w, h = self.w, self.h
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={self.fps},format=rgb24"
        ar, aw = os.pipe()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", WHITELIST]
        if src.lower().startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        cmd += ["-re", "-i", src, "-map", "0:v:0?", "-vf", vf, "-f", "rawvideo", "pipe:1"]
        if has_audio(src):                      # sources without sound simply play silent (the output below would fail)
            cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", f"pipe:{aw}"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, pass_fds=(aw,))
        except OSError as e:
            os.close(ar)
            os.close(aw)
            self.error = f"cannot start ffmpeg: {e}"
            return
        os.close(aw)
        self.started, self.gen = time.time(), self.gen + 1
        g, p = self.gen, self.proc
        threading.Thread(target=self._read_video, args=(p, g), daemon=True).start()
        threading.Thread(target=self._read_audio, args=(ar, g), daemon=True).start()
        threading.Thread(target=self._read_err, args=(p, g), daemon=True).start()
        log.info("playing %s (%d/%d)", self.current, i + 1, len(self.items))

    def _read_video(self, p, g):
        f, n = p.stdout, self.fsize
        while g == self.gen:
            buf = f.read(n)
            if len(buf) < n:
                break
            with self.lock:
                self.vq.append(buf)

    def _read_audio(self, fd, g):
        with os.fdopen(fd, "rb", buffering=0) as f:
            while g == self.gen:
                b = f.read(8192)
                if not b:
                    break
                with self.lock:
                    self.abuf += b
                    if len(self.abuf) > SR * 2 * 6:                    # never lag more than 6 s behind
                        del self.abuf[: len(self.abuf) - SR * 2 * 3]

    def _read_err(self, p, g):
        lines = []
        for raw in iter(p.stderr.readline, b""):
            t = raw.decode("utf-8", "replace").strip()
            if t:
                lines.append(t)
        if g == self.gen and lines:
            self.error = lines[-1][:200]
            log.warning("player: %s", " | ".join(lines[-3:]))

    # ---- called by the render loop once per output frame
    def frame(self):
        """Newest decoded frame (bytes) or the previous one. Moves to the next item when the current one is over."""
        with self.lock:
            fr = self.vq.popleft() if self.vq else None
        if fr is not None:
            self.last_frame = fr
            return fr
        p = self.proc
        if p is not None and p.poll() is not None and not self.vq and time.time() - self.started > 0.5:
            if p.returncode != 0 and time.time() - self.started < 5:
                self.fails += 1
                log.warning("cannot play %s: %s", self.current, self.error)
                if self.fails >= len(self.items):                  # every item fails: stop instead of spinning
                    self.stop()
                    self.ended = True
                    return self.last_frame
            else:
                self.fails = 0
            self.next()
        return self.last_frame

    def audio(self, nbytes):
        with self.lock:
            if len(self.abuf) >= nbytes:
                out = bytes(self.abuf[:nbytes])
                del self.abuf[:nbytes]
                return out
            out = bytes(self.abuf) + b"\x00" * (nbytes - len(self.abuf))
            self.abuf.clear()
            return out
