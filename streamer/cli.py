from __future__ import annotations

import logging
import shutil
import signal
import sys

from .config import load_config


def check_targets(cfg):
    """--check: DNS/TLS test and a 5 s test push to every target. NOTE: this briefly goes live on that key."""
    import subprocess
    from . import targets as T
    if not cfg.urls:
        sys.exit("nothing to check: pass --kick-url/--kick-key, --youtube-key or --url")
    print("ffmpeg rtmps support:", "yes" if T.ffmpeg_supports_rtmps() else "NO - Kick cannot work with this ffmpeg")
    bad = 0
    for u in cfg.urls:
        print(f"\n== {T.mask(u)}")
        _, warn = T.build_url(u)
        for w in warn:
            print("  note:", w)
        steps = T.check_network(u)
        for name, ok, detail in steps:
            print(f"  {'OK  ' if ok else 'FAIL'} {name}: {detail}")
        if not all(ok for _, ok, _ in steps):
            bad += 1
            continue
        print("  test push (5 s, briefly appears on that channel's dashboard)...")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-re", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=20",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", "-b:v", "800k", "-g", "40", "-c:a", "aac", "-b:a", "128k", "-f", "flv", u]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            err = T.mask_text(r.stderr.strip())
        except subprocess.TimeoutExpired:
            r, err = None, "timed out"
        if r is not None and r.returncode == 0:
            print("  OK   the server accepted the stream")
        else:
            bad += 1
            print("  FAIL", err[-600:])
            h = next((x for x in map(T.hint_for, err.splitlines()) if x), None)
            if h:
                print("  HINT:", h)
    print("\nAll good." if not bad else f"\n{bad} target(s) failed.")
    return 0 if not bad else 1


def main(argv=None):
    cfg = load_config(argv)
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("main")
    from .engine import Streamer

    if not cfg.shots and not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found.  Termux: pkg install ffmpeg   |   Debian/Ubuntu: apt install ffmpeg")
    if cfg.check:
        return check_targets(cfg)
    live = not cfg.shots and not cfg.test
    if live and not cfg.urls and not (cfg.admin and cfg.port):
        sys.exit("No stream target.  Examples:\n"
                 "  python stream.py --kick-url rtmps://XXXX.global-contribute.live-video.net --kick-key sk_XXXX\n"
                 "  python stream.py --youtube-key YOUR_KEY\n"
                 "  python stream.py            (then open http://localhost:8080/admin and add a target there)")
    if live and not cfg.urls:
        log.warning("no stream target yet - open /admin on port %d to add Kick / YouTube", cfg.port)
    if live and any(u.startswith("rtmps://") for u in cfg.urls):
        from .targets import ffmpeg_supports_rtmps
        if not ffmpeg_supports_rtmps():
            log.error("this ffmpeg has no rtmps (TLS) support, Kick will not connect. Termux: pkg upgrade ffmpeg. Debian: apt install ffmpeg")

    s = Streamer(cfg)
    if cfg.shots:
        return s.run_shots(cfg.shots)
    if cfg.test:
        return s.run_test(cfg.test)

    def stop(*_):
        log.info("shutdown requested")
        s.stop.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if cfg.port:
        s.start_http()
    for g, goal in cfg.goals.items():
        log.info("goal: %s -> %s (%s)", g, f"{goal.target:,}", goal.mode)
    s.run_live()


if __name__ == "__main__":
    main()
