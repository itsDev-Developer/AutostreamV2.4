"""Stream targets: build a correct RTMP(S) URL from what the platforms show you, check it, explain failures.

Kick shows a "Stream URL" (rtmps://<id>.global-contribute.live-video.net) and a separate "Stream Key" (sk_...).
ffmpeg needs ONE url:  rtmps://<id>.global-contribute.live-video.net:443/app/<key>
YouTube:               rtmp://a.rtmp.youtube.com/live2/<key>
"""
from __future__ import annotations

import re
import socket
import ssl
import subprocess
from urllib.parse import urlsplit

YOUTUBE_URL = "rtmp://a.rtmp.youtube.com/live2"


def mask_text(s):
    """Hide stream keys in any text that contains rtmp(s) URLs."""
    s = re.sub(r"(rtmps?://[^/\s'\"]+/[^/\s'\"]+/)[^\s'\"|\]]+", r"\1****", s)
    return re.sub(r"\bsk_[A-Za-z0-9_\-]{6,}", "sk_****", s)


def mask(url):
    return mask_text(url)


def _clean(s):
    return re.sub(r"\s+", "", (s or "").strip().strip("'\""))


def platform_of(url):
    host = (urlsplit(url).hostname or "").lower()
    if "live-video.net" in host or "kick.com" in host:
        return "kick"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "twitch.tv" in host:
        return "twitch"
    return "custom"


def build_url(url, key=""):
    """Return (full_url, warnings). Accepts a base URL + key, or a complete URL."""
    warn = []
    url, key = _clean(url), _clean(key)
    if not url:
        return "", ["empty URL"]
    if "://" not in url:
        url = "rtmps://" + url if "live-video.net" in url or "kick" in url else "rtmp://" + url
        warn.append("no rtmp:// or rtmps:// prefix - added one")
    sp = urlsplit(url)
    host = sp.hostname or ""
    plat = platform_of(url)
    scheme, port = sp.scheme.lower(), sp.port
    path = sp.path.rstrip("/")
    if scheme not in ("rtmp", "rtmps"):
        return url, [f"unsupported scheme '{scheme}' (use rtmps:// for Kick, rtmp:// for YouTube)"]
    if plat == "kick":
        if scheme != "rtmps":
            scheme = "rtmps"
            warn.append("Kick only accepts rtmps:// - switched")
        port = port or 443
        if not path:
            path = "/app"
        elif re.fullmatch(r"/sk_[^/]+", path):                  # base url + key pasted together without /app
            path = "/app" + path
            warn.append("added the missing /app/ before the key")
    elif plat == "youtube":
        if not path:
            path = "/live2"
    netloc = host + (f":{port}" if port else "")
    base = f"{scheme}://{netloc}{path}"
    if key:
        tail = key.lstrip("/")
        if not base.endswith("/" + tail):
            base = base + "/" + tail
    full = base
    parts = [p for p in urlsplit(full).path.split("/") if p]
    expect = 2
    if len(parts) < expect:
        warn.append("this URL has no stream key - paste the key from the platform too")
    return full, warn


def host_port(url):
    sp = urlsplit(url)
    port = sp.port or (443 if sp.scheme == "rtmps" else 1935)
    return sp.hostname or "", port, sp.scheme == "rtmps"


def check_network(url, timeout=6.0):
    """DNS -> TCP -> TLS. Returns a list of (step, ok, detail)."""
    out = []
    host, port, tls = host_port(url)
    if not host:
        return [("url", False, "no host name in the URL")]
    try:
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
        out.append(("dns", True, f"{host} -> {addr}"))
    except OSError as e:
        return out + [("dns", False, f"cannot resolve {host}: {e}. Check the spelling of the URL and your internet / DNS.")]
    try:
        sock = socket.create_connection((host, port), timeout)
        out.append(("tcp", True, f"connected to port {port}"))
    except OSError as e:
        return out + [("tcp", False, f"cannot connect to {host}:{port}: {e}. A firewall / mobile network may block port {port}.")]
    try:
        if tls:
            try:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as s:
                    out.append(("tls", True, f"{s.version()} handshake ok"))
            except ssl.SSLError as e:
                out.append(("tls", False, f"TLS handshake failed: {e}. Wrong date/time on this device or an old CA store can cause this."))
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return out


def ffmpeg_supports_rtmps():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-protocols"], capture_output=True, text=True, timeout=10)
        return bool(re.search(r"^\s*rtmps\s*$", r.stdout.split("Output:")[-1], re.M))
    except Exception:
        return True


# (regex, hint) - first match per kind is reported once per session
HINTS = [
    (r"Unknown protocol|Protocol not found|not compiled with (?:openssl|gnutls|tls)|requires TLS",
     "ffmpeg here has no TLS, so rtmps:// (Kick) cannot work. Termux: pkg upgrade ffmpeg (or pkg install ffmpeg-tls). Debian: apt install ffmpeg."),
    (r"Failed to resolve hostname|Name or service not known|Temporary failure in name resolution",
     "The host name in the stream URL cannot be found. Re-copy the Stream URL from the platform; check internet/DNS."),
    (r"Connection timed out|Network is unreachable|No route to host",
     "Cannot reach the ingest server. A firewall or mobile network may block outgoing port 443 (Kick) / 1935 (YouTube)."),
    (r"Connection refused",
     "The server refused the connection - the URL/port is wrong (Kick needs rtmps://...:443/app/KEY)."),
    (r"Connection reset by peer|Broken pipe|RTMP_Connect|Handshake|handshake|Server error|Input/output error|Cannot open connection|Error writing trailer",
     "The server dropped the connection. Usual causes: stream key wrong/expired (regenerate it in the platform's dashboard), "
     "URL missing /app/ (Kick), another encoder already using the same key, or YouTube not 'ready' to receive."),
    (r"Slave '.*' failed|Slave muxer #\d+ failed|All tee slaves failed|Error muxing",
     "One output stopped. See the line above it for the reason; the stream to the other platforms continues."),
    (r"Conversion failed", "ffmpeg stopped (see the lines above for the cause)."),
]


def hint_for(line):
    for rx, hint in HINTS:
        if re.search(rx, line):
            return hint
    return None
