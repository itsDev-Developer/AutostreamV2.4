"""Streaming runtime: render loop -> ffmpeg (video+audio pipes) -> RTMP, with supervision and monitoring."""
from __future__ import annotations

import io
import json
import logging
import os
import queue
import random
import re
import secrets
import subprocess
import threading
import time

from . import targets as T
from .admin import LogRing
from .audio import SR, Audio
from .director import Director
from .player import VIDEO_EXT, Player, is_url, safe_name
from .games import Ctx
from .gfx import Canvas, Particles
from .state import Clock, Stats

log = logging.getLogger("engine")
UPS = 60


mask = T.mask
PROGRESS = re.compile(r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|out_time|dup_frames|drop_frames|speed|progress)=(.*)$")


class Telemetry:
    def __init__(self):
        self.started = time.time()
        self.frames = 0
        self.last_frame = 0.0
        self.restarts = 0
        self.dropped = 0
        self.render_ms = 0.0
        self.status = {}
        self.connected = False
        self.last_error = ""
        self.last_error_t = 0.0
        self.hints = []
        self.session_t = 0.0
        self.enc_speed = None
        self.enc_bitrate = None
        self.enc_t = 0.0
        self.tuned = 0
        self.target_err = {}


class Streamer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.tele = Telemetry()
        self.clock = Clock()
        self.cv = Canvas(cfg.width, cfg.height, cfg.fx)
        self.rng = random.Random(cfg.seed)
        self.stats = Stats(cfg.state_file, self.clock)
        if cfg.reset_campaign:
            self.stats.new_campaign()
        self.audio = Audio(cfg, cfg.games)
        self.ctx = Ctx(cfg, self.cv, self.audio, self.stats, self.rng, Particles(self.rng), self.clock)
        self.director = Director(self.ctx, cfg.games)
        self._acc = 0.0        # fractional audio samples carried between frames
        self.cmds = queue.Queue()      # admin actions, executed on the render thread
        self.restart_req = False
        self.admin = None
        self.logring = LogRing()
        logging.getLogger().addHandler(self.logring)
        self.last_raw = None
        self._jpg = (0.0, None)
        self.streaming = cfg.autostart
        self.mode = "games"
        self.video_dir = cfg.video_dir or os.path.join(os.path.dirname(os.path.abspath(cfg.state_file)), "videos")
        self.player = Player(cfg.width, cfg.height, cfg.fps, self.video_dir)
        self.player.set_sources(cfg.video, cfg.video_loop)
        self.want_mode = cfg.mode
        self._slow = 0.0
        self.settings_path = os.path.join(os.path.dirname(os.path.abspath(cfg.state_file)), "admin.json")
        self.targets = [{"id": secrets.token_hex(3), "name": T.platform_of(u).title(), "url": u} for u in cfg.urls]
        self._load_settings()

    # ------------------------------------------------------------------ admin-editable settings
    def _load_settings(self):
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        from .config import Goal
        if isinstance(d.get("targets"), list):
            self.targets = [{"id": t.get("id") or secrets.token_hex(3), "name": str(t.get("name", "stream")), "url": t["url"]}
                            for t in d["targets"] if isinstance(t, dict) and t.get("url")]
            self.cfg.urls = [t["url"] for t in self.targets]
        if isinstance(d.get("video_sources"), list):
            self.cfg.video = [str(x) for x in d["video_sources"]]
        if "video_loop" in d:
            self.cfg.video_loop = bool(d["video_loop"])
        self.player.set_sources(self.cfg.video, self.cfg.video_loop)
        if "streaming" in d:
            self.streaming = bool(d["streaming"])
        if d.get("mode") in ("games", "video"):
            self.want_mode = d["mode"]
        if "minutes" in d:
            self.cfg.minutes = self.director.minutes = float(d["minutes"])
        if "skill" in d:
            self.cfg.skill = float(d["skill"])
            for g in self.director.games:
                g.h.skill = self.cfg.skill
        if isinstance(d.get("goals"), dict):
            self.cfg.goals = {k: Goal(int(v["target"]), v.get("mode", "best")) for k, v in d["goals"].items()
                              if k in self.director.by_key and int(v.get("target", 0)) > 0}
        log.info("loaded admin settings from %s (%d target(s))", self.settings_path, len(self.targets))

    def save_settings(self):
        d = {"targets": self.targets, "streaming": self.streaming, "mode": self.mode, "video_sources": self.cfg.video,
             "video_loop": self.cfg.video_loop, "minutes": self.director.minutes, "skill": self.cfg.skill,
             "goals": {k: {"target": g.target, "mode": g.mode} for k, g in self.cfg.goals.items()}}
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            tmp = self.settings_path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # holds stream keys: owner only
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.settings_path)
        except OSError as e:
            log.error("cannot save admin settings: %s", e)

    def submit(self, fn):
        self.cmds.put(fn)

    def _drain(self):
        while True:
            try:
                fn = self.cmds.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log.exception("admin action failed")

    def add_target(self, name, url):
        self.targets = [t for t in self.targets if t["url"] != url] + [{"id": secrets.token_hex(3), "name": name, "url": url}]
        self.cfg.urls = [t["url"] for t in self.targets]
        self.save_settings()
        self.restart_req = True
        log.info("target added: %s %s", name, mask(url))

    def remove_target(self, tid):
        self.targets = [t for t in self.targets if t["id"] != tid]
        self.cfg.urls = [t["url"] for t in self.targets]
        self.save_settings()
        self.restart_req = True
        log.info("target removed")

    def do_stream(self, on):
        self.streaming = on
        self.tele.hints, self.tele.last_error = [], ""
        if not on:
            self.tele.connected = False
        self.save_settings()
        log.info("admin: stream %s", "STARTED" if on else "ENDED (program keeps running)")

    def do_mode(self, mode):
        if mode == "video":
            self.player.set_sources(self.cfg.video, self.cfg.video_loop)
            if not self.player.start():
                log.warning("video mode: %s", self.player.error)
                return
        elif self.mode == "video":
            self.player.stop()
        self.mode = self.want_mode = mode
        self.save_settings()
        log.info("admin: now streaming %s", "the video player" if mode == "video" else "the games")

    def video_files(self):
        try:
            return sorted((f, os.path.getsize(os.path.join(self.video_dir, f))) for f in os.listdir(self.video_dir)
                          if f.lower().endswith(VIDEO_EXT))
        except OSError:
            return []

    def add_video(self, src):
        src = src.strip()
        if not (is_url(src) or os.path.exists(os.path.join(self.video_dir, safe_name(src)))):
            return False
        src = src if is_url(src) else safe_name(src)
        if src not in self.cfg.video:
            self.cfg.video = self.cfg.video + [src]
        self.player.set_sources(self.cfg.video, self.cfg.video_loop)
        self.save_settings()
        return True

    def remove_video(self, src):
        self.cfg.video = [v for v in self.cfg.video if v != src]
        self.player.set_sources(self.cfg.video, self.cfg.video_loop)
        self.save_settings()

    def delete_file(self, name):
        name = safe_name(name)
        try:
            os.remove(os.path.join(self.video_dir, name))
        except OSError:
            return False
        self.remove_video(name)
        return True

    def do_skip(self):
        if self.mode == "video":
            self.player.next()
            return
        d = self.director
        if d.state in ("intro", "play"):
            d._end_segment("skip")
            log.info("admin: skipped to next game")

    def do_play(self, key):
        d = self.director
        d.finished = False
        d._begin(d.games.index(d.by_key[key]))
        log.info("admin: switched to %s", key)

    def do_goal(self, key, target, mode):
        from .config import Goal
        if target <= 0:
            self.cfg.goals.pop(key, None)
        else:
            self.cfg.goals[key] = Goal(target, mode)
            self.stats.c["done"].pop(key, None)
            self.stats.dirty = True
            self.director._check_feasibility()
        self.save_settings()

    def do_settings(self, minutes=None, skill=None):
        if minutes is not None:
            self.cfg.minutes = self.director.minutes = minutes
        if skill is not None:
            self.cfg.skill = skill
            for g in self.director.games:
                g.h.skill = skill
        self.save_settings()

    def do_reset(self):
        d = self.director
        self.stats.new_campaign()
        d.finished = False
        d._begin(next((i for i, g in enumerate(d.games) if d._active(g)), 0))
        log.info("admin: new challenge started")

    def frame_jpeg(self):
        raw = self.last_raw
        if raw is None:
            return None
        ts, jpg = self._jpg
        if jpg is not None and time.time() - ts < 1.5:
            return jpg
        from PIL import Image
        im = Image.frombytes("RGB", (self.cfg.width, self.cfg.height), raw)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        self._jpg = (time.time(), buf.getvalue())
        return self._jpg[1]

    # ------------------------------------------------------------------ one simulation + render step
    def step(self, draw=True):
        steps = max(1, round(UPS / self.cfg.fps))
        for _ in range(steps):
            self.director.update()
            self.ctx.parts.update()
            if self.clock.sim is not None:
                self.clock.sim += 1.0 / UPS
            for t in self.ctx.toasts:
                t[2] -= 1.0 / UPS
            self.ctx.toasts[:] = [t for t in self.ctx.toasts if t[2] > 0]
        if draw:
            self.director.draw(self.cv)

    def audio_bytes(self):
        self._acc += SR / self.cfg.fps
        n = int(self._acc)
        self._acc -= n
        if self.mode == "video":
            return self.player.audio(n * 2)
        return self.audio.chunk(n)

    # ------------------------------------------------------------------ ffmpeg
    def ffmpeg_cmd(self, audio_fd, out_file=None):
        c = self.cfg
        kbps = int(c.bitrate.lower().rstrip("k"))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{c.width}x{c.height}", "-framerate", str(c.fps),
               "-probesize", "32", "-analyzeduration", "0", "-thread_queue_size", "256", "-i", "pipe:0",
               "-f", "s16le", "-ar", str(SR), "-ac", "1", "-probesize", "32", "-analyzeduration", "0",
               "-thread_queue_size", "512", "-i", f"pipe:{audio_fd}",
               "-map", "0:v", "-map", "1:a",
               "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
               "-c:v", "libx264", "-preset", c.preset, "-profile:v", "high", "-b:v", c.bitrate,
               "-maxrate", c.bitrate, "-bufsize", f"{kbps * 2}k", "-g", str(c.fps * 2), "-keyint_min", str(c.fps * 2),
               "-sc_threshold", "0", "-nostats", "-progress", "pipe:2", "-stats_period", "2", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
        if out_file:
            return cmd + ["-tune", "animation", "-movflags", "+faststart", out_file]
        if not c.urls:                                         # nothing configured yet: keep playing, send nowhere
            return cmd + ["-tune", "zerolatency", "-f", "null", "-"]
        # every output has its own fifo queue + automatic reconnect, so a slow or failing platform cannot stall the others
        opt = ("[f=flv:flvflags=no_duration_filesize:onfail=ignore:use_fifo=1:"
               "fifo_options=attempt_recovery=1\\\\:drop_pkts_on_overflow=1\\\\:recover_any_error=1\\\\:restart_with_keyframe=1"
               "\\\\:recovery_wait_time=3\\\\:queue_size=600]")
        tee = "|".join(f"{opt}{u}" for u in c.urls)
        return cmd + ["-tune", "zerolatency", "-f", "tee", tee]

    def _spawn(self, out_file=None):
        ar, aw = os.pipe()
        proc = subprocess.Popen(self.ffmpeg_cmd(ar, out_file), stdin=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(ar,))
        os.close(ar)
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        return proc, os.fdopen(aw, "wb", buffering=0)

    def _read_stderr(self, proc):
        """Show ffmpeg's messages (keys masked) with a plain-language hint for the usual Kick/YouTube problems."""
        seen = set()
        for raw in iter(proc.stderr.readline, b""):
            line = T.mask_text(raw.decode("utf-8", "replace").strip())
            if not line or "Guessed Channel Layout" in line:
                continue
            m = PROGRESS.match(line)
            if m:
                k, v = m.groups()
                if k == "speed":
                    try:
                        self.tele.enc_speed, self.tele.enc_t = float(v.strip().rstrip("x")), time.time()
                    except ValueError:
                        pass
                elif k == "bitrate":
                    try:
                        self.tele.enc_bitrate = float(v.strip().replace("kbits/s", ""))
                    except ValueError:
                        pass
                continue
            log.warning("ffmpeg: %s", line)
            m = re.search(r"Error opening (rtmps?://\S+?):? ", line + " ")
            if m:
                self.tele.target_err[m.group(1).rstrip(":")] = time.time()
            if "message queue blocking" not in line:
                self.tele.last_error, self.tele.last_error_t = line[:300], time.time()
            h = T.hint_for(line)
            if h and h not in seen:
                seen.add(h)
                log.error("HINT: %s", h)
                if h not in self.tele.hints:
                    self.tele.hints = (self.tele.hints + [h])[-4:]

    # ------------------------------------------------------------------ live loop
    def run_live(self):
        cfg, backoff = self.cfg, 5
        log.info("outputs: %s", ", ".join(mask(u) for u in cfg.urls) or "none yet - add a target in the admin panel (/admin)")
        while not self.stop.is_set() and not self.director.finished:
            if not self.streaming:                                 # stream ended from the admin panel: program stays up
                self.tele.connected = False
                self._idle()
                continue
            began = time.time()
            self._session()
            if self.stop.is_set() or self.director.finished:
                break
            self.tele.connected = False
            if self.restart_req:                                   # requested from the admin panel: reconnect at once
                self.restart_req = False
                self.tele.hints = []
                log.info("reconnecting with the updated stream targets")
                continue
            self.tele.restarts += 1
            backoff = 5 if time.time() - began > 90 else min(backoff * 2, 60)
            log.warning("ffmpeg stopped - restarting in %ds (restart #%d)", backoff, self.tele.restarts)
            self._wait(backoff)
        self.stats.save(force=True)
        log.info("stream ended")

    def _idle(self):
        while not self.streaming and not self.stop.is_set():
            self._drain()
            time.sleep(0.2)

    def _tune(self):
        """If the encoder cannot keep up (speed < 0.9x for 25 s) lower the cost once, then tell the user what to change."""
        t = self.tele
        if not self.cfg.auto_tune or t.enc_speed is None or time.time() - t.enc_t > 6 or time.time() - t.session_t < 20:
            self._slow = 0.0
            return
        if t.enc_speed >= 0.9:
            self._slow = 0.0
            return
        self._slow = self._slow or time.time()
        if time.time() - self._slow < 25:
            return
        self._slow = 0.0
        if t.tuned == 0:
            t.tuned = 1
            self.cfg.preset, self.cv.fx = "ultrafast", 0
            msg = f"The host is too slow for this quality (encoder at {t.enc_speed:.2f}x). Switched to the lightest settings (ultrafast, no effects)."
            log.warning(msg)
            t.hints = (t.hints + [msg])[-4:]
            self.restart_req = True
        elif t.tuned == 1:
            t.tuned = 2
            msg = ("Still too slow even on the lightest settings. Restart with --profile low (360p) or use a bigger plan; "
                   "free hosting tiers (about 0.1-0.5 CPU) usually cannot encode live video.")
            log.error(msg)
            t.hints = (t.hints + [msg])[-4:]

    def _wait(self, seconds):
        """Sleep, but wake up for admin actions (they also run while the stream is reconnecting)."""
        end = time.time() + seconds
        while time.time() < end and not self.stop.is_set() and not self.restart_req and self.streaming:
            self._drain()
            time.sleep(0.2)
        if self.restart_req:
            self.restart_req = False
            self.tele.hints = []

    def _session(self):
        proc, aud = self._spawn()
        dt, nxt, last_bytes, last_save, win = 1.0 / self.cfg.fps, time.time(), None, time.time(), []
        self.tele.connected = True
        self.tele.session_t, self.tele.enc_speed, self.tele.enc_bitrate = time.time(), None, None
        self._slow = 0.0
        black = bytes(self.cfg.width * self.cfg.height * 3)
        try:
            while (not self.stop.is_set() and not self.director.finished and proc.poll() is None and not self.restart_req
                   and self.streaming):
                self._drain()
                if self.want_mode != self.mode:
                    self.do_mode(self.want_mode) if self.want_mode == "video" else self.do_mode("games")
                    self.want_mode = self.mode
                now = time.time()
                late = now - nxt
                t0 = time.time()
                if self.mode == "video":
                    fr = self.player.frame()
                    frame = last_bytes = self.last_raw = fr if fr is not None else (last_bytes or black)
                    if self.player.ended:
                        log.info("video finished - back to the games (%s)", self.player.error or "end of playlist")
                        self.do_mode("games")
                elif late > dt * 1.5 and last_bytes is not None:   # behind schedule: keep clock, repeat last frame
                    self.step(draw=False)
                    frame = last_bytes
                    self.tele.dropped += 1
                else:
                    self.step()
                    frame = last_bytes = self.last_raw = self.cv.img.tobytes()
                self._tune()
                aud.write(self.audio_bytes())
                proc.stdin.write(frame)
                self.tele.frames += 1
                self.tele.last_frame = time.time()
                win = (win + [(time.time() - t0) * 1000])[-60:]
                self.tele.render_ms = sum(win) / len(win)
                nxt += dt
                if nxt - time.time() > 0:
                    time.sleep(nxt - time.time())
                elif late > 2:
                    nxt = time.time()                              # way behind (e.g. network stall): resync
                if time.time() - last_save > 15:
                    self.stats.save()
                    last_save = time.time()
        except (BrokenPipeError, OSError):
            pass
        finally:
            for f in (aud, proc.stdin):
                try:
                    f.close()
                except Exception:
                    pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ------------------------------------------------------------------ offline test clip
    def run_test(self, seconds):
        self.clock.sim = time.time()
        self.cfg.minutes = min(self.cfg.minutes, max(0.1, seconds / len(self.cfg.games) / 60 - 0.2))
        self.director.minutes = self.cfg.minutes
        proc, aud = self._spawn("test.mp4")
        t0 = time.time()
        try:
            for _ in range(int(seconds * self.cfg.fps)):
                self.step()
                aud.write(self.audio_bytes())
                proc.stdin.write(self.cv.img.tobytes())
        finally:
            aud.close()
            proc.stdin.close()
            proc.wait()
        log.info("wrote test.mp4 (%.0fs video in %.1fs; render speed x%.1f realtime)", seconds, time.time() - t0, seconds / (time.time() - t0))

    # ------------------------------------------------------------------ preview PNGs
    def run_shots(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.clock.sim = time.time()
        d, cv = self.director, self.cv

        def save(name):
            self.ctx.toasts.clear() if name.endswith("intro.png") else None
            d.draw(cv)
            cv.img.save(os.path.join(folder, name))
            log.info("saved %s", name)

        for i, g in enumerate(d.games):
            d._begin(i)
            for _ in range(60 * 70):                       # play ~70 s of game time
                g.update()
                self.ctx.parts.update()
            d.state, d.timer = "intro", 1.6
            save(f"{g.key}_1_intro.png")
            d.state = "play"
            d.seg_ticks = 60 * 40
            save(f"{g.key}_2_gameplay.png")
        d.state, d.timer, d.reason = "outro", 1.2, "goal"
        self.ctx.cfg.goals.setdefault(d.game.key, __import__("streamer.config", fromlist=["Goal"]).Goal(100))
        self.ctx.parts.confetti(100, ["#ffd93d", "#35e0a1", "#35c4ff", "#ff6b9d"])
        for _ in range(40):
            self.ctx.parts.update()
        save("outro_goal_reached.png")
        d.state, d.timer, d.reason = "outro", 1.2, "time"
        save("outro_round_complete.png")
        d.state = "final"
        save("final_results.png")

    # ------------------------------------------------------------------ monitoring + admin panel
    def start_http(self):
        from .admin import start_http
        return start_http(self)
