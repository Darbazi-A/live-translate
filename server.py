"""
Live Translate demo server (Georgian <-> English).

Speaker page (Chrome) recognizes speech with the Web Speech API and keeps posting the
growing utterance here. simul.py re-translates it and decides which words are stable
enough to say; those are broadcast to listeners over Server-Sent Events and voiced
with Microsoft neural voices (edge-tts).

Run:  python server.py   (then open http://localhost:8000)
"""
import asyncio
import http.client
import json
import mimetypes
import os
import queue
import socket
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import simul

try:
    import aiohttp
    import edge_tts
except ImportError:
    edge_tts = None

PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
HISTORY_LIMIT = 100       # utterances kept for listeners who join late
CHUNK_LIMIT = 1000
TTS_CACHE_LIMIT = 400
STALE_UTTERANCE = 8.0     # seconds without updates before an unfinished utterance is closed
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
TTS_RATE = os.environ.get("TTS_RATE", "+15%")  # slightly faster speech helps keep up with the speaker

VOICES = {
    "ka": {"f": "ka-GE-EkaNeural", "m": "ka-GE-GiorgiNeural"},
    "en": {"f": "en-US-AriaNeural", "m": "en-US-GuyNeural"},
}
OTHER = {"ka": "en", "en": "ka"}

# ---------------------------------------------------------------- state

clients = set()           # one queue.Queue per connected SSE listener
clients_lock = threading.Lock()
utterances = OrderedDict()  # utterance id -> {utt, src, dst, source, final, chunks, ts}
streams = {}                # utterance id -> simul.Utterance while it is still being spoken
chunks = OrderedDict()      # chunk id -> {id, utt, src, dst, text: {dst: ...}}
stream_lock = threading.RLock()
next_id = int(time.time() * 1000)  # unique across restarts, so browsers never reuse stale audio

tts_cache = {}            # (segment_id, lang, gender) -> TtsJob
tts_lock = threading.Lock()
tts_pool = ThreadPoolExecutor(max_workers=8)


def broadcast(event):
    data = json.dumps(event, ensure_ascii=False)
    with clients_lock:
        for q in list(clients):
            q.put(data)


def listener_count():
    with clients_lock:
        return len(clients)


# ---------------------------------------------------------------- translation

# Two of Google Translate's public web endpoints. Both answer in ~0.1s on a kept-alive
# connection (the older /translate_a/single endpoint often takes 0.5-2s). Fine for a
# demo; use the official Cloud Translation API (or DeepL / an LLM) for production.
TRANSLATE_ENDPOINTS = [
    ("translate.googleapis.com", "gtx"),
    ("clients5.google.com", "dict-chrome-ex"),
]
HEDGE_AFTER = 0.3   # seconds; then ask the second endpoint too and take whichever answers first
translate_conns = {host: queue.LifoQueue() for host, _ in TRANSLATE_ENDPOINTS}
translate_pool = ThreadPoolExecutor(max_workers=8)


def _translate_once(host, client, text, src, dst):
    path = "/translate_a/t?" + urllib.parse.urlencode(
        {"client": client, "sl": src, "tl": dst, "q": text}
    )
    for attempt in range(2):
        try:
            conn = translate_conns[host].get_nowait()
        except queue.Empty:
            conn = http.client.HTTPSConnection(host, timeout=8)
        try:
            conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0"})
            r = conn.getresponse()
            body = r.read()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            translate_conns[host].put(conn)
            data = json.loads(body)
            first = data[0]
            return first if isinstance(first, str) else first[0]
        except Exception:
            conn.close()
            if attempt == 1:
                raise


def translate(text, src, dst):
    futures = [translate_pool.submit(_translate_once, *TRANSLATE_ENDPOINTS[0], text, src, dst)]
    done, _ = wait(futures, timeout=HEDGE_AFTER)
    if not done or futures[0].exception():
        futures.append(translate_pool.submit(_translate_once, *TRANSLATE_ENDPOINTS[1], text, src, dst))
    pending = set(futures)
    while pending:
        done, pending = wait(pending, timeout=8, return_when=FIRST_COMPLETED)
        if not done:
            break
        for f in done:
            if not f.exception():
                return f.result()
    errors = [str(f.exception()) for f in futures if f.done() and f.exception()]
    print(f"[translate] {src}->{dst} failed: {errors or 'timeout'}", file=sys.stderr)
    return ""


# ---------------------------------------------------------------- text-to-speech

class WarmTts:
    """Keeps a few websocket connections to the Edge TTS service open and reuses
    them, so a phrase costs ~0.3s to first audio instead of ~0.7s with a fresh
    connection per phrase. Built on edge-tts internals; if those ever change,
    TtsJob falls back to the public edge_tts.Communicate API."""

    SIZE = 4
    MAX_IDLE = 45  # seconds; reconnect idle sockets before the service drops them

    def __init__(self):
        from edge_tts import communicate as C
        from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
        from edge_tts.drm import DRM

        self.C, self.DRM = C, DRM
        self.url = WSS_URL
        self.headers = WSS_HEADERS
        self.gec_version = SEC_MS_GEC_VERSION
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=5)

    async def _start(self):
        self.session = aiohttp.ClientSession(trust_env=True)
        self.slots = asyncio.Queue()
        for _ in range(self.SIZE):
            self.slots.put_nowait(None)  # None = not connected yet
        self.loop.create_task(self._keep_warm())

    async def _connect(self):
        C, DRM = self.C, self.DRM
        ws = await self.session.ws_connect(
            f"{self.url}&ConnectionId={C.connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={self.gec_version}",
            compress=15,
            headers=DRM.headers_with_muid(self.headers),
            ssl=C._SSL_CTX,
        )
        return {"ws": ws, "used": time.time()}

    async def _keep_warm(self):
        while True:
            for _ in range(self.SIZE):
                slot = await self.slots.get()
                try:
                    stale = slot is None or slot["ws"].closed or time.time() - slot["used"] > self.MAX_IDLE
                    if stale:
                        if slot and not slot["ws"].closed:
                            await slot["ws"].close()
                        slot = await self._connect()
                except Exception as e:
                    print(f"[tts] warm-up failed: {e}", file=sys.stderr)
                    slot = None
                self.slots.put_nowait(slot)
            await asyncio.sleep(20)

    async def _turn(self, ws, text, voice, on_audio):
        C = self.C
        await ws.send_str(
            f"X-Timestamp:{C.date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        cfg = C.TTSConfig(voice, TTS_RATE, "+0%", "+0Hz", "SentenceBoundary")
        ssml = C.mkssml(cfg, C.escape(C.remove_incompatible_characters(text)))
        await ws.send_str(C.ssml_headers_plus_data(C.connect_id(), C.date_to_string(), ssml))
        while True:
            msg = await ws.receive(timeout=10)
            if msg.type == aiohttp.WSMsgType.TEXT:
                if "Path:turn.end" in msg.data:
                    return
            elif msg.type == aiohttp.WSMsgType.BINARY:
                header_len = int.from_bytes(msg.data[:2], "big")
                audio = msg.data[2 + header_len:]
                if audio:
                    on_audio(audio)
            else:
                raise ConnectionError(f"websocket {msg.type.name}")

    async def _synth(self, text, voice, on_audio):
        sent = 0

        def count(data):
            nonlocal sent
            sent += len(data)
            on_audio(data)

        for attempt in range(2):
            slot = await self.slots.get()
            try:
                if slot is None or slot["ws"].closed:
                    slot = await self._connect()
                await self._turn(slot["ws"], text, voice, count)
                slot["used"] = time.time()
                self.slots.put_nowait(slot)
                return
            except Exception:
                if slot:
                    await slot["ws"].close()
                self.slots.put_nowait(None)
                if attempt == 1 or sent:  # don't retry once audio went out (it would repeat)
                    raise

    def synthesize(self, text, voice, on_audio):
        """Blocking call from any thread; on_audio(bytes) is called as audio arrives."""
        asyncio.run_coroutine_threadsafe(self._synth(text, voice, on_audio), self.loop).result(timeout=30)


warm_tts = None


class TtsJob:
    """mp3 audio being synthesized; listeners can stream it while it is still growing."""

    def __init__(self, text, voice):
        self.buf = bytearray()
        self.done = False
        self.failed = False
        self.cond = threading.Condition()
        tts_pool.submit(self._run, text, voice)

    def _append(self, data):
        with self.cond:
            self.buf.extend(data)
            self.cond.notify_all()

    def _run(self, text, voice):
        async def plain():
            communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    self._append(chunk["data"])

        try:
            if warm_tts:
                try:
                    warm_tts.synthesize(text, voice, self._append)
                except Exception as e:
                    if self.buf:
                        raise
                    print(f"[tts] warm connection failed ({e}), using a fresh one", file=sys.stderr)
                    asyncio.run(plain())
            else:
                asyncio.run(plain())
        except Exception as e:
            print(f"[tts] failed: {e}", file=sys.stderr)
            self.failed = True
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def chunks(self, timeout=20):
        """Yield audio bytes as they arrive."""
        pos = 0
        while True:
            with self.cond:
                if pos >= len(self.buf) and not self.done:
                    if not self.cond.wait(timeout):
                        return
                data = bytes(self.buf[pos:])
                finished = self.done
            pos += len(data)
            if data:
                yield data
            if finished and pos >= len(self.buf):
                return


def get_tts(segment, lang, gender):
    """Synthesized once per (segment, language, voice) and shared by every listener."""
    key = (segment["id"], lang, gender)
    with tts_lock:
        job = tts_cache.get(key)
        if job is None:
            job = TtsJob(segment["text"][lang], VOICES[lang][gender])
            tts_cache[key] = job
            # keep the cache bounded
            if len(tts_cache) > TTS_CACHE_LIMIT:
                for old in list(tts_cache)[: len(tts_cache) - TTS_CACHE_LIMIT]:
                    tts_cache.pop(old, None)
        return job


# ---------------------------------------------------------------- live stream

_log_lock = threading.Lock()


def log_stream(entry):
    """Every recognizer update and every spoken piece, one JSON line each, so a real
    session can be replayed and debugged afterwards (logs/stream-YYYY-MM-DD.jsonl)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        line = json.dumps({"t": round(time.time(), 3), **entry}, ensure_ascii=False)
        with _log_lock, open(os.path.join(LOG_DIR, time.strftime("stream-%Y-%m-%d.jsonl")), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def emit_chunk(rec, text):
    """A piece of translation is final: store it, start its voice, tell listeners."""
    global next_id
    ch = {"id": next_id, "utt": rec["utt"], "src": rec["src"], "dst": rec["dst"],
          "text": {rec["dst"]: text}, "ts": time.time()}
    next_id += 1
    chunks[ch["id"]] = ch
    while len(chunks) > CHUNK_LIMIT:
        chunks.popitem(last=False)
    rec["chunks"].append({"id": ch["id"], "text": text})
    if edge_tts:
        get_tts(ch, rec["dst"], "f")
        get_tts(ch, rec["dst"], "m")
    broadcast({"type": "chunk", **ch})


def stream_update(uid, src, text, final, mode):
    with stream_lock:
        rec = utterances.get(uid)
        if rec is None:
            rec = {"utt": uid, "src": src, "dst": OTHER[src], "source": "", "final": False,
                   "chunks": [], "ts": time.time()}
            utterances[uid] = rec
            while len(utterances) > HISTORY_LIMIT:
                old, _ = utterances.popitem(last=False)
                streams.pop(old, None)
        if rec["final"]:
            return rec, ""
        u = streams.get(uid)
        if u is None:
            u = streams[uid] = simul.Utterance(lambda t: translate(t, src, rec["dst"]))
        out, preview = u.update(text, final, mode)
        log_stream({"utt": uid, "src": src, "mode": mode, "final": final, "text": text,
                    "emit": out, "preview": preview})
        rec["source"] = text
        rec["final"] = final
        broadcast({"type": "source", "utt": uid, "src": src, "dst": rec["dst"], "text": text,
                   "final": final, "ts": rec["ts"]})
        for piece in out:
            emit_chunk(rec, piece)
        broadcast({"type": "preview", "utt": uid, "text": preview})
        if final:
            streams.pop(uid, None)
        return rec, preview


def stream_janitor():
    """Emit words that waited too long, and close utterances the speaker abandoned."""
    while True:
        time.sleep(0.15)
        try:
            with stream_lock:
                now = time.monotonic()
                for uid, u in list(streams.items()):
                    rec = utterances.get(uid)
                    if rec is None:
                        streams.pop(uid, None)
                    elif now - u.last_update > STALE_UTTERANCE:
                        stream_update(uid, rec["src"], u.text, True, None)
                    else:
                        out = u.flush_if_stale(now)
                        if out:
                            log_stream({"utt": uid, "flush": out})
                        for piece in out:
                            emit_chunk(rec, piece)
                        if out:
                            broadcast({"type": "preview", "utt": uid, "text": u.preview()})
        except Exception as e:
            print(f"[stream] janitor error: {e}", file=sys.stderr)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/events" in self.path or "/api/tts" in self.path:
            return
        sys.stderr.write("[http] " + fmt % args + "\n")

    # helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, msg):
        self.send_json({"error": msg}, status)

    def read_json(self):
        return json.loads(self.body or b"{}")

    # routes
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        qs = urllib.parse.parse_qs(url.query)

        if path == "/events":
            return self.handle_events()
        if path == "/api/tts":
            return self.handle_tts(qs)
        if path == "/api/info":
            return self.send_json({
                "urls": lan_urls(),
                "listeners": listener_count(),
                "tts": edge_tts is not None,
            })

        pages = {"/": "index.html", "/speaker": "speaker.html", "/listen": "listen.html"}
        rel = pages.get(path, path.lstrip("/"))
        full = os.path.normpath(os.path.join(PUBLIC_DIR, rel))
        if not full.startswith(PUBLIC_DIR) or not os.path.isfile(full):
            return self.send_error_json(404, "not found")
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Always consume the body: on a kept-alive connection unread bytes would be
        # glued to the front of the next request ("{}POST ..." -> 501 HTML page).
        self.body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/stream":
            return self.handle_stream()
        if self.path == "/api/clear":
            with stream_lock:
                utterances.clear()
                streams.clear()
                chunks.clear()
            broadcast({"type": "clear"})
            return self.send_json({"ok": True})
        self.send_error_json(404, "not found")

    def handle_stream(self):
        try:
            body = self.read_json()
        except Exception:
            return self.send_error_json(400, "bad json")
        src = body.get("lang")
        uid = str(body.get("utt") or "")
        text = (body.get("text") or "").strip()
        final = bool(body.get("final"))
        if src not in OTHER or not uid:
            return self.send_error_json(400, "lang must be ka|en, utt required")
        rec, preview = stream_update(uid, src, text, final, body.get("mode"))
        self.send_json({
            "ok": True,
            "translation": " ".join(c["text"] for c in rec["chunks"]),
            "preview": preview,
            "listeners": listener_count(),
        })

    def handle_tts(self, qs):
        if edge_tts is None:
            return self.send_error_json(503, "edge-tts not installed (pip install edge-tts)")
        try:
            seg_id = int(qs.get("id", ["0"])[0])
        except ValueError:
            return self.send_error_json(400, "bad id")
        lang = qs.get("lang", ["en"])[0]
        gender = qs.get("voice", ["f"])[0]
        if lang not in VOICES or gender not in ("f", "m"):
            return self.send_error_json(400, "bad lang/voice")
        with stream_lock:
            seg = chunks.get(seg_id)
        if not seg or not seg["text"].get(lang):
            return self.send_error_json(404, "segment not found")
        job = get_tts(seg, lang, gender)
        if job.failed:
            return self.send_error_json(502, "tts failed")
        if job.done:
            audio = bytes(job.buf)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(audio)
            return
        # still generating: stream it so playback starts with the first chunk
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for data in job.chunks():
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    def handle_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = queue.Queue()
        with stream_lock:
            snapshot = json.dumps({"type": "history", "utterances": list(utterances.values())}, ensure_ascii=False)
        q.put(snapshot)
        with clients_lock:
            clients.add(q)
        broadcast({"type": "listeners", "count": listener_count()})
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with clients_lock:
                clients.discard(q)
            broadcast({"type": "listeners", "count": listener_count()})


def lan_urls():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except OSError:
        pass
    return [f"http://{ip}:{PORT}/listen" for ip in sorted(ips)]


class DualStackServer(ThreadingHTTPServer):
    """Listen on IPv6 and IPv4 at once. Windows resolves "localhost" to ::1 first,
    and an IPv4-only server makes every such request wait ~2s for the fallback."""

    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main():
    global warm_tts
    if edge_tts is None:
        print("! edge-tts not installed - listeners will fall back to browser voices "
              "(no Georgian voice in Chrome). Run: pip install edge-tts")
    else:
        try:
            warm_tts = WarmTts()
        except Exception as e:
            print(f"! warm TTS connections unavailable ({e}); using one connection per phrase")
    threading.Thread(target=stream_janitor, daemon=True).start()
    server = DualStackServer(("::", PORT), Handler)
    print(f"\n  Live Translate is running\n")
    print(f"  Speaker (this computer, Chrome):  http://localhost:{PORT}/speaker")
    print(f"  Listener (this computer):         http://localhost:{PORT}/listen")
    for u in lan_urls():
        print(f"  Listener (phones on same Wi-Fi):  {u}")
    print("\n  Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
