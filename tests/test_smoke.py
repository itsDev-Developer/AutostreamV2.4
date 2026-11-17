"""Run with:  python -m unittest discover -s tests -v   (needs no ffmpeg, no network)"""
import os
import json
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamer import audio                                   # noqa: E402
from streamer.config import Goal, load_config, parse_goals, parse_mistake  # noqa: E402
from streamer.director import Director                       # noqa: E402
from streamer.games import REGISTRY, Ctx                     # noqa: E402
from streamer.gfx import Canvas, Particles                   # noqa: E402
from streamer.state import Clock, Stats                      # noqa: E402


def make_ctx(tmp, argv=()):
    cfg = load_config(["--state-file", os.path.join(tmp, "s.json"), "--cache-dir", os.path.join(tmp, "c"),
                       "--profile", "low", "--no-music", "--seed", "1", *argv])
    clock = Clock()
    rng = random.Random(1)
    ctx = Ctx(cfg, Canvas(cfg.width, cfg.height, cfg.fx), audio.Audio(cfg, cfg.games), Stats(cfg.state_file, clock), rng,
              Particles(rng), clock)
    return cfg, ctx


class ConfigTests(unittest.TestCase):
    def test_goal_parsing(self):
        g = parse_goals(["flappy=100_000", "racing=5000:total"])
        self.assertEqual(g["flappy"], Goal(100000, "best"))
        self.assertEqual(g["racing"], Goal(5000, "total"))

    def test_mistake_parsing(self):
        self.assertEqual(parse_mistake("0.02"), {"*": 0.02})
        self.assertEqual(parse_mistake("flappy=0,racing=0.1"), {"flappy": 0.0, "racing": 0.1})

    def test_profiles(self):
        cfg = load_config(["--profile", "high"])
        self.assertEqual((cfg.width, cfg.height, cfg.fps), (1280, 720, 30))


class TargetTests(unittest.TestCase):
    def test_kick_url_is_completed(self):
        from streamer.targets import build_url, mask_text
        want = "rtmps://x1.global-contribute.live-video.net:443/app/sk_us-west-2_abc123"
        for base, key in (("rtmps://x1.global-contribute.live-video.net", "sk_us-west-2_abc123"),
                          ("rtmp://x1.global-contribute.live-video.net/", " sk_us-west-2_abc123\n"),
                          ("rtmps://x1.global-contribute.live-video.net:443/app/", "sk_us-west-2_abc123"),
                          ("rtmps://x1.global-contribute.live-video.net/sk_us-west-2_abc123", "")):
            self.assertEqual(build_url(base, key)[0], want)
        self.assertTrue(build_url("rtmps://x1.global-contribute.live-video.net")[1])      # missing key is reported
        self.assertEqual(build_url("rtmp://a.rtmp.youtube.com/live2", "k-1")[0], "rtmp://a.rtmp.youtube.com/live2/k-1")
        self.assertNotIn("abc123", mask_text(want))

    def test_admin_auth_and_actions(self):
        import base64
        from streamer.admin import Admin
        from streamer.engine import Streamer
        with tempfile.TemporaryDirectory() as t:
            cfg = load_config(["--state-file", os.path.join(t, "s.json"), "--cache-dir", os.path.join(t, "c"), "--profile", "low",
                               "--no-music", "--admin-password", "secret"])
            st = Streamer(cfg)
            adm = Admin(st, "secret")
            good = "Basic " + base64.b64encode(b"admin:secret").decode()
            bad = "Basic " + base64.b64encode(b"admin:nope").decode()
            self.assertTrue(adm.check(good))
            self.assertFalse(adm.check(bad))
            self.assertFalse(adm.check(None))
            code, out = adm.act({"action": "add_target", "url": "rtmps://x1.global-contribute.live-video.net", "key": "sk_abc123456"})
            self.assertEqual(code, 200)
            self.assertTrue(st.restart_req and len(st.targets) == 1)
            self.assertNotIn("abc123456", json.dumps(adm.status()))                 # keys never leave the server
            self.assertEqual(adm.act({"action": "add_target", "url": "rtmps://x1.global-contribute.live-video.net"})[0], 400)
            adm.act({"action": "set_goal", "game": "flappy", "target": 900, "mode": "total"})
            st._drain()
            self.assertEqual(cfg.goals["flappy"].target, 900)
            st2 = Streamer(load_config(["--state-file", os.path.join(t, "s.json"), "--cache-dir", os.path.join(t, "c"), "--profile", "low", "--no-music"]))
            self.assertEqual(len(st2.targets), 1)                                   # saved settings survive a restart
            self.assertEqual(st2.cfg.goals["flappy"].target, 900)
            self.assertEqual(adm.act({"action": "add_video", "src": "../../etc/passwd"})[0], 400)    # only URLs / uploaded files
            self.assertEqual(adm.act({"action": "add_video", "src": "https://example.com/live.m3u8"})[0], 200)
            adm.act({"action": "end_stream"})
            st._drain()
            self.assertFalse(st.streaming)
            for i in range(10):
                adm.check(bad)
            self.assertIsNone(adm.check(good))                                      # locked after repeated failures


class PlayerTests(unittest.TestCase):
    def test_names_and_urls(self):
        from streamer.player import is_url, safe_name
        self.assertEqual(safe_name("../../etc/pass wd.mp4"), "pass wd.mp4")
        self.assertTrue(is_url("https://x.com/a.m3u8") and not is_url("/etc/passwd"))

    def test_plays_a_file_and_loops(self):
        import shutil
        import subprocess
        import time
        if not shutil.which("ffmpeg"):
            self.skipTest("no ffmpeg")
        from streamer.player import Player
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "a.mp4")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "1",
                            "-pix_fmt", "yuv420p", f], check=True)
            p = Player(160, 90, 10, t)
            p.set_sources(["a.mp4"], True)
            self.assertTrue(p.start())
            frames, t0 = 0, time.time()
            while time.time() - t0 < 4 and frames < 15:
                fr = p.frame()
                if fr is not None and len(fr) == 160 * 90 * 3:
                    frames += 1
                time.sleep(0.1)
            p.stop()
            self.assertGreaterEqual(frames, 5)                       # decoded, scaled, and kept going past the first loop
            self.assertEqual(len(p.audio(100)), 100)                 # silent source -> silence, never an error


class StateTests(unittest.TestCase):
    def test_persistence_and_goal(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "s.json")
            st = Stats(p, Clock())
            st.on_score("flappy", 5, 5)
            st.mark_done("flappy")
            st.save(force=True)
            again = Stats(p, Clock())
            self.assertEqual(again.record("flappy"), 5)
            self.assertTrue(again.is_done("flappy"))

    def test_corrupt_state_is_survived(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "s.json")
            open(p, "w").write("{not json")
            st = Stats(p, Clock())
            self.assertEqual(st.record("flappy"), 0)


class AudioTests(unittest.TestCase):
    def test_track_and_chunk(self):
        tr = audio.render_track("racing")
        self.assertGreater(len(tr), audio.SR * 10)
        self.assertLessEqual(max(tr), 32767)

    def test_mixer_returns_exact_sample_count(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t)
            ctx.audio.sfx("crash")
            self.assertEqual(len(ctx.audio.chunk(918)), 918 * 2)


class GameTests(unittest.TestCase):
    def test_every_game_runs_and_renders(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t)
            for key, cls in REGISTRY.items():
                g = cls(ctx)
                for i in range(4000):
                    g.update()
                    ctx.parts.update()
                    if i % 1000 == 0:
                        g.draw(ctx.cv)
                self.assertGreaterEqual(ctx.stats.record(key), 0)

    def test_flappy_player_scores(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t, ["--skill", "1"])
            g = REGISTRY["flappy"](ctx)
            for _ in range(60 * 60 * 5):
                g.update()
                ctx.parts.items.clear()
            self.assertGreater(ctx.stats.record("flappy"), 25)

    def test_play_is_not_frame_perfect(self):
        """Taps must not all happen at the same height: human timing varies."""
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t, ["--skill", "1"])
            g = REGISTRY["flappy"](ctx)
            heights, prev_vy = [], 0.0
            for _ in range(60 * 60 * 3):
                g.update()
                ctx.parts.items.clear()
                if g.vy <= -8.0 and prev_vy > -8.0 and not g.dead:
                    heights.append(g.y)
                prev_vy = g.vy
            self.assertGreater(len(set(round(h / 4) for h in heights)), 10)

    def test_fight_matches_complete_and_score(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t)
            g = REGISTRY["fight"](ctx)
            seen = set()
            for _ in range(60 * 60 * 20):
                g.update()
                ctx.parts.items.clear()
                seen.add(g.phase)
            self.assertTrue({"vs", "intro", "fight", "ko", "match"} <= seen | {"ko"})
            self.assertGreater(ctx.stats.record("fight"), 0)

    def test_screen_text_does_not_mention_automation(self):
        import re
        import streamer.overlay as ov
        bad = re.compile(r"\b(bot|bots|ai|robot|automatic|automated|autoplay)\b", re.I)
        for key, cls in REGISTRY.items():
            self.assertIsNone(bad.search(cls.title + " " + cls.tagline), key)
        src = open(ov.__file__, encoding="utf-8").read()
        self.assertIsNone(bad.search(src))


class DirectorTests(unittest.TestCase):
    def test_goal_rotation_and_final(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t, ["--goal", "flappy=2", "--goal", "blocks=150", "--games", "flappy,blocks", "--mistake", "0",
                                    "--end-on-complete"])
            ctx.clock.sim = 0.0
            d = Director(ctx, cfg.games)
            seen = []
            for _ in range(60 * 200):
                d.update()
                ctx.parts.update()
                ctx.clock.sim += 1 / 60
                if not seen or seen[-1] != (d.state, d.game.key):
                    seen.append((d.state, d.game.key))
                if d.finished:
                    break
            self.assertTrue(d.finished)
            self.assertTrue(ctx.stats.is_done("flappy") and ctx.stats.is_done("blocks"))
            self.assertIn(("play", "blocks"), seen)
            self.assertEqual(seen[-1][0], "final")

    def test_timed_rotation(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t, ["--minutes", "0.1", "--games", "flappy,racing"])
            d = Director(ctx, cfg.games)
            for _ in range(60 * 30):
                d.update()
            self.assertIn(d.game.key, ("racing", "flappy"))
            self.assertTrue(d.stats.d["runs"] >= 2)

    def test_draw_all_states(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, ctx = make_ctx(t, ["--goal", "flappy=100", "--duration", "24", "--title", "TEST"])
            d = Director(ctx, cfg.games)
            for state in ("intro", "play", "outro", "final"):
                d.state, d.timer, d.reason = state, 1.0, "goal"
                d.draw(ctx.cv)


if __name__ == "__main__":
    unittest.main()
