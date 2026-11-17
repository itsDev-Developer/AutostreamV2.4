"""HTTP server: /health /metrics / (public status) and the password-protected admin panel at /admin."""
from __future__ import annotations

import base64
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import targets as T
from .player import VIDEO_EXT, safe_name

log = logging.getLogger("admin")


class LogRing(logging.Handler):
    """Keeps the last log lines (keys masked) for the admin panel."""

    def __init__(self, n=400):
        super().__init__()
        self.lines, self.n = [], n
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.lines.append(T.mask_text(self.format(record)))
            if len(self.lines) > self.n:
                del self.lines[: len(self.lines) - self.n]
        except Exception:
            pass

    def add(self, text):
        self.lines.append(T.mask_text(text))
        if len(self.lines) > self.n:
            del self.lines[: len(self.lines) - self.n]


def proc_stats():
    """CPU seconds and RSS (MB) of this process and its children (ffmpeg) from /proc (Linux/Android)."""
    try:
        tick = os.sysconf("SC_CLK_TCK")
        me, cpu, rss = os.getpid(), 0.0, 0.0
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    p = f.read().rsplit(")", 1)[1].split()
                if int(d) != me and int(p[1]) != me:
                    continue
                cpu += (int(p[11]) + int(p[12])) / tick
                with open(f"/proc/{d}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS"):
                            rss += int(line.split()[1]) / 1024
            except (OSError, ValueError, IndexError):
                continue
        return cpu, rss
    except Exception:
        return None, None


class Admin:
    """Auth + actions. Every action runs on the render thread via Streamer.submit()."""

    def __init__(self, streamer, password, user="admin"):
        self.s, self.user = streamer, user
        self.generated = not password
        self.password = password or secrets.token_urlsafe(9)
        self.fail_times = []
        self.cpu_prev = (time.time(), *(proc_stats()[:1] or [None]))
        self.cpu_pct = None

    # ---- auth
    def locked(self):
        now = time.time()
        self.fail_times = [t for t in self.fail_times if now - t < 300]
        return len(self.fail_times) >= 12

    def check(self, header):
        if self.locked():
            return None
        ok = False
        try:
            kind, _, b64 = (header or "").partition(" ")
            if kind.lower() == "basic":
                u, _, p = base64.b64decode(b64).decode("utf-8", "replace").partition(":")
                ok = hmac.compare_digest(u.encode(), self.user.encode()) & hmac.compare_digest(p.encode(), self.password.encode())
        except Exception:
            ok = False
        if not ok:
            self.fail_times.append(time.time())
        return ok

    # ---- data
    def status(self):
        s, d = self.s, self.s.director
        t = s.tele
        now = time.time()
        cpu, rss = proc_stats()
        if cpu is not None and now - self.cpu_prev[0] >= 2 and self.cpu_prev[1] is not None:
            self.cpu_pct = round((cpu - self.cpu_prev[1]) / (now - self.cpu_prev[0]) * 100)
            self.cpu_prev = (now, cpu)
        g = d.game
        games = []
        for x in d.games:
            goal = s.cfg.goals.get(x.key)
            games.append({"key": x.key, "title": x.title, "record": s.stats.record(x.key),
                          "done": s.stats.is_done(x.key),
                          "goal": ({"target": goal.target, "mode": goal.mode, "value": s.stats.goal_value(x.key, goal)} if goal else None)})
        age = now - t.session_t
        if not s.streaming:
            label = "off"
        elif not s.targets:
            label = "no_target"
        elif not t.connected or age < 8:
            label = "connecting"
        else:
            bad = [x for x in s.targets if now - t.target_err.get(T.mask(x["url"]), 0) < 15]
            if bad and len(bad) == len(s.targets):
                label = "errors"
            elif bad:
                label = "partial"
            elif now - t.last_error_t < 15:
                label = "errors"
            else:
                label = "live"
        return {
            "live": label == "live", "label": label, "streaming": s.streaming, "mode": s.mode,
            "enc_speed": t.enc_speed, "enc_bitrate": t.enc_bitrate,
            "video": {"sources": list(s.cfg.video), "loop": s.cfg.video_loop, "current": s.player.current if s.mode == "video" else "",
                      "error": s.player.error, "files": [{"name": n, "size": z} for n, z in s.video_files()]},
            "connected": t.connected, "uptime": round(now - t.started), "frames": t.frames, "dropped": t.dropped,
            "restarts": t.restarts, "render_ms": round(t.render_ms, 1), "fps": s.cfg.fps,
            "profile": f"{s.cfg.width}x{s.cfg.height}@{s.cfg.fps} {s.cfg.bitrate}",
            "cpu": self.cpu_pct, "rss": round(rss) if rss else None,
            "state": d.state, "game": g.key, "title": g.title, "score": g.score,
            "elapsed": round(s.stats.elapsed()), "games": games, "minutes": d.minutes, "skill": s.cfg.skill,
            "targets": [{"id": x["id"], "name": x["name"], "platform": T.platform_of(x["url"]), "url": T.mask(x["url"]),
                         "failing": now - t.target_err.get(T.mask(x["url"]), 0) < 15} for x in s.targets],
            "last_error": t.last_error, "hints": list(t.hints),
            "generated_password": self.generated,
        }

    def act(self, req):
        """Run an action; returns (http_code, json-able)."""
        s, a = self.s, str(req.get("action", ""))
        try:
            if a == "add_target":
                url, warn = T.build_url(req.get("url", ""), req.get("key", ""))
                if not url or not url.startswith(("rtmp://", "rtmps://")) or any("no stream key" in w for w in warn):
                    return 400, {"ok": False, "error": "; ".join(warn) or "URL needed"}
                name = str(req.get("name") or T.platform_of(url)).strip()[:30] or "stream"
                net = T.check_network(url)
                s.add_target(name, url)
                return 200, {"ok": True, "warnings": warn, "network": net,
                             "message": "Saved. The stream restarts now and connects to the new target."}
            if a == "remove_target":
                s.remove_target(str(req.get("id", "")))
                return 200, {"ok": True, "message": "Removed."}
            if a == "check_targets":
                return 200, {"ok": True, "results": [{"name": x["name"], "url": T.mask(x["url"]), "steps": T.check_network(x["url"])}
                                                       for x in s.targets]}
            if a == "start_stream":
                s.submit(lambda: s.do_stream(True))
                return 200, {"ok": True, "message": "Starting the stream..."}
            if a == "end_stream":
                s.submit(lambda: s.do_stream(False))
                return 200, {"ok": True, "message": "Stream ended. The program keeps running - press Start stream to go live again."}
            if a == "set_mode":
                mode = "video" if req.get("mode") == "video" else "games"
                if mode == "video" and not s.cfg.video:
                    return 400, {"ok": False, "error": "add a video file or URL to the player first"}
                s.want_mode = mode
                if not s.streaming:
                    s.submit(lambda: s.do_mode(mode))
                return 200, {"ok": True, "message": "Switching to the video player." if mode == "video" else "Switching to the games."}
            if a == "add_video":
                if not s.add_video(str(req.get("src", ""))):
                    return 400, {"ok": False, "error": "use a full URL (http/https/rtmp/rtsp/srt, e.g. .m3u8) or the name of an uploaded file"}
                return 200, {"ok": True, "message": "Added to the playlist."}
            if a == "remove_video":
                s.remove_video(str(req.get("src", "")))
                return 200, {"ok": True, "message": "Removed from the playlist."}
            if a == "delete_file":
                ok = s.delete_file(str(req.get("name", "")))
                return (200, {"ok": True, "message": "File deleted."}) if ok else (400, {"ok": False, "error": "file not found"})
            if a == "video_loop":
                s.cfg.video_loop = bool(req.get("loop"))
                s.player.set_sources(s.cfg.video, s.cfg.video_loop)
                s.save_settings()
                return 200, {"ok": True, "message": "Playlist will loop." if s.cfg.video_loop else "Playlist plays once, then back to the games."}
            if a == "restart_stream":
                s.streaming = True
                s.restart_req = True
                return 200, {"ok": True, "message": "Restarting the stream."}
            if a == "skip":
                s.submit(s.do_skip)
                return 200, {"ok": True, "message": "Skipping to the next game."}
            if a == "play":
                key = str(req.get("game", ""))
                if key not in s.director.by_key:
                    return 400, {"ok": False, "error": "unknown game"}
                s.submit(lambda: s.do_play(key))
                return 200, {"ok": True, "message": f"Switching to {key}."}
            if a == "set_goal":
                key, tgt = str(req.get("game", "")), int(req.get("target", 0))
                mode = "total" if req.get("mode") == "total" else "best"
                if key not in s.director.by_key:
                    return 400, {"ok": False, "error": "unknown game"}
                s.submit(lambda: s.do_goal(key, tgt, mode))
                return 200, {"ok": True, "message": "Goal removed." if tgt <= 0 else f"Goal for {key}: {tgt:,} ({mode})."}
            if a == "set_minutes":
                m = max(0.5, min(600.0, float(req.get("minutes", 10))))
                s.submit(lambda: s.do_settings(minutes=m))
                return 200, {"ok": True, "message": f"{m:g} minutes per game."}
            if a == "set_skill":
                k = max(0.0, min(1.0, float(req.get("skill", 0.85))))
                s.submit(lambda: s.do_settings(skill=k))
                return 200, {"ok": True, "message": f"Player skill {k:g}."}
            if a == "reset_campaign":
                s.submit(s.do_reset)
                return 200, {"ok": True, "message": "New challenge started (all-time records kept)."}
            if a == "stop":
                s.stop.set()
                return 200, {"ok": True, "message": "Stopping."}
        except (ValueError, TypeError) as e:
            return 400, {"ok": False, "error": f"bad value: {e}"}
        return 400, {"ok": False, "error": "unknown action"}


def start_http(streamer):
    s, port = streamer, streamer.cfg.port
    adm = Admin(s, s.cfg.admin_password, s.cfg.admin_user) if s.cfg.admin else None
    s.admin = adm

    class H(BaseHTTPRequestHandler):
        server_version = "AutoStream"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _auth(self):
            if adm is None:
                self._send(404, "admin panel is disabled", "text/plain")
                return False
            ok = adm.check(self.headers.get("Authorization"))
            if ok:
                return True
            if ok is None:
                self._send(429, "too many failed logins - wait a few minutes", "text/plain")
            else:
                self._send(401, "login required", "text/plain", {"WWW-Authenticate": 'Basic realm="AutoStream admin", charset="UTF-8"'})
            return False

        def do_GET(self):
            path = urlsplit(self.path).path
            t = s.tele
            healthy = s.stop.is_set() or time.time() - t.last_frame < 15 or time.time() - t.started < 60
            if path.startswith("/admin"):
                if not self._auth():
                    return
                if path in ("/admin", "/admin/"):
                    return self._send(200, PAGE, "text/html; charset=utf-8")
                if path == "/admin/api/status":
                    return self._send(200, json.dumps(adm.status()))
                if path == "/admin/api/logs":
                    return self._send(200, "\n".join(s.logring.lines[-250:]), "text/plain; charset=utf-8")
                if path == "/admin/frame.jpg":
                    jpg = s.frame_jpeg()
                    return self._send(200, jpg, "image/jpeg") if jpg else self._send(404, "no frame yet", "text/plain")
                return self._send(404, "not found", "text/plain")
            if path.startswith("/health"):
                self._send(200 if healthy else 503, "ok" if healthy else "stalled", "text/plain")
            elif path.startswith("/metrics"):
                ex = s.director.snapshot()
                lines = [f"autostream_frames_total {t.frames}", f"autostream_frames_dropped_total {t.dropped}",
                         f"autostream_ffmpeg_restarts_total {t.restarts}", f"autostream_render_ms {t.render_ms:.2f}",
                         f"autostream_uptime_seconds {time.time() - t.started:.0f}",
                         f"autostream_campaign_seconds {ex['elapsed']}"]
                lines += [f'autostream_record{{game="{k}"}} {v}' for k, v in ex["records"].items()]
                self._send(200, "\n".join(lines) + "\n", "text/plain")
            else:
                ex = s.director.snapshot()
                ex.update(frames=t.frames, restarts=t.restarts, dropped=t.dropped, render_ms=round(t.render_ms, 2),
                          connected=t.connected, healthy=healthy)
                self._send(200, json.dumps(ex))

        def _upload(self):
            q = parse_qs(urlsplit(self.path).query)
            name = safe_name((q.get("name") or [""])[0])
            size = int(self.headers.get("Content-Length") or 0)
            limit = s.cfg.max_upload_mb * 1024 * 1024
            if not name or not name.lower().endswith(VIDEO_EXT):
                return self._send(400, json.dumps({"ok": False, "error": "file type not supported (" + ", ".join(VIDEO_EXT[:8]) + " ...)"}))
            if size <= 0 or size > limit:
                return self._send(413, json.dumps({"ok": False, "error": f"file too large (limit {s.cfg.max_upload_mb} MB; set MAX_UPLOAD_MB)"}))
            os.makedirs(s.video_dir, exist_ok=True)
            dest, tmp = os.path.join(s.video_dir, name), os.path.join(s.video_dir, name + ".part")
            got = 0
            try:
                with open(tmp, "wb") as f:
                    while got < size:
                        b = self.rfile.read(min(1 << 20, size - got))
                        if not b:
                            break
                        f.write(b)
                        got += len(b)
                if got != size:
                    raise OSError("upload interrupted")
                os.replace(tmp, dest)
            except OSError as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
            s.add_video(name)
            log.info("uploaded %s (%.1f MB)", name, size / 1e6)
            self._send(200, json.dumps({"ok": True, "message": f"Uploaded {name} and added it to the playlist."}))

        def do_POST(self):
            path = urlsplit(self.path).path
            if not path.startswith("/admin/api/") or not self._auth():
                if not path.startswith("/admin/api/"):
                    self._send(404, "not found", "text/plain")
                return
            if self.headers.get("X-Admin") != "1":                  # blocks cross-site form posts (CSRF)
                return self._send(403, json.dumps({"ok": False, "error": "missing X-Admin header"}))
            if path == "/admin/api/upload":
                return self._upload()
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 20000)
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad JSON"}))
            code, out = adm.act(req)
            self._send(code, json.dumps(out))

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    except OSError as e:
        raise SystemExit(f"cannot open port {port} for the status/admin server ({e}). Is another copy already running? "
                         f"Use --port to pick another port.")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("status endpoint on :%d  (/health /metrics /)", port)
    if adm:
        log.info("admin panel: http://localhost:%d/admin   user: %s   password: %s%s", port, adm.user,
                 adm.password if adm.generated else "(from ADMIN_PASSWORD)",
                 "   (generated - set ADMIN_PASSWORD to choose your own)" if adm.generated else "")
    return srv


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stream admin</title>
<style>
:root{--bg:#0f1320;--card:#181e33;--line:#2a3354;--tx:#e8ecff;--mut:#97a1c9;--ok:#3ddc84;--bad:#ff5a5f;--acc:#4f8cff;--warn:#ffb02e}
@media (prefers-color-scheme:light){:root{--bg:#f2f4fb;--card:#fff;--line:#d9deef;--tx:#151a30;--mut:#5a6488}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
h1{font-size:18px;margin:0;flex:1}h2{font-size:14px;margin:0 0 10px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}
.wide{grid-column:1/-1}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:13px;background:var(--line)}
.pill.ok{background:var(--ok);color:#06210f}.pill.bad{background:var(--bad);color:#fff}.pill.warn{background:var(--warn);color:#2a1a00}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px}.kv b{color:var(--mut);font-weight:500}
button,input,select{font:inherit;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--tx);padding:8px 12px}
button{background:var(--acc);border-color:var(--acc);color:#fff;cursor:pointer;font-weight:600}button.g{background:transparent;color:var(--tx);border-color:var(--line)}
button.d{background:var(--bad);border-color:var(--bad)}button:disabled{opacity:.5}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}.row>input,.row>select{flex:1;min-width:120px}
table{width:100%;border-collapse:collapse}td,th{padding:6px 4px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}th{color:var(--mut);font-weight:500;font-size:13px}
img{width:100%;border-radius:8px;display:block;background:#000;aspect-ratio:16/9}
pre{margin:0;max-height:300px;overflow:auto;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-all;background:var(--bg);padding:10px;border-radius:8px}
.msg{min-height:20px;font-size:14px;margin-top:6px}.err{color:var(--bad)}.good{color:var(--ok)}.hint{background:#ffb02e22;border:1px solid var(--warn);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:14px}
small{color:var(--mut)}
</style></head><body>
<header><h1>Stream admin</h1><span id="live" class="pill">...</span></header>
<main>
<section class="card wide"><h2>Stream control</h2>
<div class="row"><button id="bstart" onclick="act({action:'start_stream'})">&#9654; Start stream</button><button id="bend" class="d" onclick="if(confirm('End the stream? Viewers will see it go offline. The program keeps running.'))act({action:'end_stream'})">&#9632; End stream</button>
<button class="g" onclick="act({action:'restart_stream'})">&#8635; Restart stream</button>
<span style="flex:1"></span><small>Now sending:</small><select id="mode" onchange="act({action:'set_mode',mode:this.value})" style="flex:0 0 auto"><option value="games">Games</option><option value="video">Video player</option></select></div>
<div class="msg" id="cmsg"></div><div id="diag"></div></section>
<section class="card"><h2>Live preview</h2><img id="frame" alt="preview"><div class="row"><small>Updates every 3 s.</small></div></section>
<section class="card"><h2>Status</h2><div class="kv" id="st"></div></section>

<section class="card wide"><h2>Stream targets (Kick, YouTube, ...)</h2>
<div id="hints"></div>
<table><thead><tr><th>Name</th><th>URL (key hidden)</th><th></th></tr></thead><tbody id="tg"></tbody></table>
<div class="row"><select id="plat"><option value="kick">Kick</option><option value="youtube">YouTube</option><option value="custom">Other</option></select>
<input id="turl" placeholder="Stream URL (e.g. rtmps://....global-contribute.live-video.net)" autocomplete="off">
<input id="tkey" placeholder="Stream key" type="password" autocomplete="off"></div>
<div class="row"><button onclick="addT()">Add / save target</button><button class="g" onclick="act({action:'check_targets'},showCheck)">Check connection</button>
<button class="g" onclick="act({action:'restart_stream'})">Reconnect now</button></div>
<small>Kick: copy "Stream URL" and "Stream Key" from Kick &rarr; Dashboard &rarr; Settings &rarr; Stream. The panel adds <code>:443/app/</code> for you. Leave the URL blank on YouTube to use the default server.</small>
<div class="msg" id="tmsg"></div><pre id="chk" hidden></pre></section>

<section class="card wide"><h2>Video player (mp4, mov, mkv, webm, m3u8 / HLS, rtmp, rtsp, srt ...)</h2>
<small>Plays videos instead of the games on the same stream. Any codec ffmpeg can decode works; it is re-encoded to H.264/AAC for YouTube and Kick.</small>
<div class="row"><input id="vurl" placeholder="Video URL, e.g. https://example.com/live/playlist.m3u8 or https://.../video.mp4" autocomplete="off"><button class="g" onclick="addV($('vurl').value);$('vurl').value=''">Add URL</button></div>
<div class="row"><input id="vfile" type="file" accept="video/*,audio/*,.mkv,.m3u8,.ts,.mov,.flv"><button class="g" onclick="upload()">Upload file</button><small id="vprog"></small></div>
<table><thead><tr><th>Playlist (plays in this order)</th><th></th></tr></thead><tbody id="vl"></tbody></table>
<table><thead><tr><th>Uploaded files</th><th>Size</th><th></th></tr></thead><tbody id="vf"></tbody></table>
<div class="row"><label><input id="vloop" type="checkbox" onchange="act({action:'video_loop',loop:this.checked},null,'vmsg')" style="width:auto"> Loop the playlist</label>
<span style="flex:1"></span><button onclick="act({action:'set_mode',mode:'video'},null,'vmsg')">Play videos</button><button class="g" onclick="act({action:'set_mode',mode:'games'},null,'vmsg')">Back to games</button>
<button class="g" onclick="act({action:'skip'},null,'vmsg')">Next video</button></div>
<div class="msg" id="vmsg"></div></section>
<section class="card"><h2>Games</h2>
<table><thead><tr><th>Game</th><th>Record</th><th>Goal</th><th></th></tr></thead><tbody id="gm"></tbody></table>
<div class="row"><button onclick="act({action:'skip'})">Skip to next game</button></div></section>

<section class="card"><h2>Settings</h2>
<div class="row"><label>Minutes per game (no goal)</label><input id="min" type="number" min="0.5" step="0.5" style="max-width:110px"><button class="g" onclick="act({action:'set_minutes',minutes:+min.value})">Set</button></div>
<div class="row"><label>Player skill 0-1</label><input id="skill" type="number" min="0" max="1" step="0.05" style="max-width:110px"><button class="g" onclick="act({action:'set_skill',skill:+skill.value})">Set</button></div>
<div class="row"><button class="g" onclick="if(confirm('Start a new challenge? Progress toward goals resets; all-time records stay.'))act({action:'reset_campaign'})">New challenge</button>
<button class="d" onclick="if(confirm('Quit the whole program? On Render/Koyeb it will restart by itself; elsewhere you must start it again.'))act({action:'stop'})">Quit program</button></div>
</section>

<section class="card wide"><h2>Log</h2><pre id="log"></pre></section>
</main>
<script>
const $=id=>document.getElementById(id);const H={'Content-Type':'application/json','X-Admin':'1'};
const f2=n=>n==null?'-':n.toLocaleString();const dur=s=>{s=+s;return Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m'};
function say(t,bad,el){el=$(el||'cmsg');el.textContent=t;el.className='msg '+(bad?'err':'good')}
async function act(body,cb,el){try{const r=await fetch('/admin/api/action',{method:'POST',headers:H,body:JSON.stringify(body)});const j=await r.json();
 if(cb)cb(j);else say(j.message||j.error||'',!j.ok,el);refresh();return j}catch(e){say('request failed: '+e,true,el)}}
async function addT(){const j=await act({action:'add_target',name:$('plat').selectedOptions[0].text,url:$('turl').value||($('plat').value=='youtube'?'rtmp://a.rtmp.youtube.com/live2':''),key:$('tkey').value},null,'tmsg');
 if(j&&j.ok){$('tkey').value='';$('turl').value='';let t=j.message;if(j.warnings&&j.warnings.length)t+=' Note: '+j.warnings.join('; ');say(t,false,'tmsg');showCheck({results:[{name:'new target',steps:j.network}]})}}
function showCheck(j){const p=$('chk');p.hidden=false;p.textContent=(j.results||[]).map(r=>r.name+'  '+(r.url||'')+'\n'+r.steps.map(s=>(s[1]?'  OK   ':'  FAIL ')+s[0]+': '+s[2]).join('\n')).join('\n\n')}
function addV(src){if(src)act({action:'add_video',src},null,'vmsg')}
function upload(){const f=$('vfile').files[0];if(!f)return say('choose a file first',true,'vmsg');const x=new XMLHttpRequest();x.open('POST','/admin/api/upload?name='+encodeURIComponent(f.name));x.setRequestHeader('X-Admin','1');
 x.upload.onprogress=e=>{if(e.lengthComputable)$('vprog').textContent=Math.round(e.loaded/e.total*100)+'%'};x.onload=()=>{$('vprog').textContent='';try{const j=JSON.parse(x.responseText);say(j.message||j.error,!j.ok,'vmsg')}catch(e){say('upload failed ('+x.status+')',true,'vmsg')}refresh()};x.onerror=()=>say('upload failed',true,'vmsg');x.send(f)}
function pick(k){return document.querySelector('[data-g="'+k+'"]')}
async function refresh(){try{const r=await fetch('/admin/api/status',{headers:H});if(!r.ok)return;const s=await r.json();
 const L={live:['LIVE','ok'],connecting:['CONNECTING...','warn'],errors:['ERRORS - see below','bad'],partial:['PARTLY LIVE - see below','warn'],off:['STREAM ENDED','warn'],no_target:['NO TARGET','warn']}[s.label];const live=$('live');live.textContent=L[0];live.className='pill '+L[1];
 $('bstart').disabled=s.streaming;$('bend').disabled=!s.streaming;if(document.activeElement!==$('mode'))$('mode').value=s.mode;
 let dg='';if(s.label=='live')dg='<div class="hint">Sending to '+s.targets.map(t=>t.name).join(', ')+(s.enc_speed?' - encoder '+s.enc_speed+'x speed':'')+(s.enc_bitrate?', '+Math.round(s.enc_bitrate)+' kbit/s':'')+'. If a platform still shows offline: YouTube - open YouTube Studio &rarr; Go live &rarr; Stream and press <b>Go live</b> if it says "Ready"/"No data" (check you used THIS channel\'s key); Kick - check Dashboard &rarr; Stream for the same key, give it ~15 s.</div>';
 if(s.enc_speed&&s.enc_speed<0.9&&s.streaming)dg+='<div class="hint">Encoder is running at '+s.enc_speed+'x - this host is too slow, so the platforms see a stalling stream and will not go live. Use <code>--profile low</code> or a stronger server.</div>';$('diag').innerHTML=dg;
 const v=s.video;$('vl').innerHTML=v.sources.length?v.sources.map(x=>'<tr><td style="word-break:break-all">'+x.replace(/</g,'&lt;')+(v.current&&x.endsWith(v.current)?' <span class="pill ok">playing</span>':'')+'</td><td><button class="g" onclick="act({action:\'remove_video\',src:'+JSON.stringify(x).replace(/"/g,'&quot;')+'},null,\'vmsg\')">Remove</button></td></tr>').join(''):'<tr><td colspan=2><small>Playlist is empty - add a URL or upload a file.</small></td></tr>';
 $('vf').innerHTML=v.files.length?v.files.map(f=>'<tr><td style="word-break:break-all">'+f.name.replace(/</g,'&lt;')+'</td><td>'+(f.size/1048576).toFixed(1)+' MB</td><td><button class="g" onclick="addV('+JSON.stringify(f.name).replace(/"/g,'&quot;')+')">Add</button> <button class="g" onclick="if(confirm(\'Delete this file?\'))act({action:\'delete_file\',name:'+JSON.stringify(f.name).replace(/"/g,'&quot;')+'},null,\'vmsg\')">Delete</button></td></tr>').join(''):'<tr><td colspan=3><small>No uploaded files.</small></td></tr>';
 if(document.activeElement!==$('vloop'))$('vloop').checked=v.loop;if(v.error)say(v.error,true,'vmsg');
 $('st').innerHTML=[['Now playing',s.title+' ('+s.state+')'],['Score',f2(s.score)],['Challenge time',dur(s.elapsed)],['Video',s.profile],['Frame time',s.render_ms+' ms of '+Math.round(1000/s.fps)+' ms'],
  ['Dropped frames',f2(s.dropped)],['Reconnects',s.restarts],['CPU / RAM',(s.cpu==null?'-':s.cpu+'%')+' / '+(s.rss==null?'-':s.rss+' MB')],['Program uptime',dur(s.uptime)]].map(a=>'<b>'+a[0]+'</b><span>'+a[1]+'</span>').join('');
 $('tg').innerHTML=s.targets.length?s.targets.map(t=>'<tr><td>'+t.name+(t.failing?' <span class="pill bad">failing</span>':'')+'</td><td style="word-break:break-all"><small>'+t.url+'</small></td><td><button class="g" onclick="if(confirm(\'Remove this target?\'))act({action:\'remove_target\',id:\''+t.id+'\'},null,\'tmsg\')">Remove</button></td></tr>').join(''):'<tr><td colspan=3><small>No target yet - add Kick or YouTube below. Until then the game runs but nothing is sent.</small></td></tr>';
 let h='';if(s.hints.length)h+=s.hints.map(x=>'<div class="hint">'+x+'</div>').join('');if(s.last_error)h+='<div class="hint">Last ffmpeg message: <code>'+s.last_error.replace(/</g,'&lt;')+'</code></div>';$('hints').innerHTML=h;
 const gm=$('gm');if(!gm.contains(document.activeElement)){gm.innerHTML=s.games.map(g=>'<tr><td>'+g.title+(g.key==s.game?' <span class="pill ok">on</span>':'')+(g.done?' <span class="pill">done</span>':'')+'</td><td>'+f2(g.record)+'</td><td>'+
  '<input data-g="'+g.key+'" type="number" min="0" placeholder="none" value="'+(g.goal?g.goal.target:'')+'" style="width:90px"> <select data-m="'+g.key+'"><option value="best"'+(g.goal&&g.goal.mode=='best'?' selected':'')+'>single run</option><option value="total"'+(g.goal&&g.goal.mode=='total'?' selected':'')+'>total</option></select>'+
  (g.goal?'<br><small>'+f2(g.goal.value)+' / '+f2(g.goal.target)+'</small>':'')+'</td><td><button class="g" onclick="setG(\''+g.key+'\')">Set goal</button> <button class="g" onclick="act({action:\'play\',game:\''+g.key+'\'})">Play</button></td></tr>').join('')}
 if(document.activeElement!==$('min'))$('min').value=s.minutes;if(document.activeElement!==$('skill'))$('skill').value=s.skill;
 $('frame').src='/admin/frame.jpg?'+Date.now();
 const lg=await fetch('/admin/api/logs',{headers:H});const p=$('log');const bottom=p.scrollTop+p.clientHeight>=p.scrollHeight-20;p.textContent=await lg.text();if(bottom)p.scrollTop=p.scrollHeight}catch(e){}}
function setG(k){act({action:'set_goal',game:k,target:+(pick(k).value||0),mode:document.querySelector('[data-m="'+k+'"]').value})}
refresh();setInterval(refresh,3000);
</script></body></html>
"""
