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
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import simul

try:
    import aiohttp
    import edge_tts
except ImportError:
    edge_tts = None

FROZEN = getattr(sys, "frozen", False)   # running from the installer build (PyInstaller)

# The installed app and python.org's macOS Python have no CA certificates of their own
# ("CERTIFICATE_VERIFY_FAILED"); certifi's bundle (an edge-tts dependency) fixes both.
if (FROZEN or sys.platform == "darwin") and not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass

VERSION = "1.1.0"
APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(APP_DIR, "public")


def _data_dir():
    """Where settings and logs go: next to the code when run from source, in the
    user's profile when installed (Program Files / the .app bundle are read-only)."""
    if os.environ.get("LT_DATA_DIR"):
        return os.environ["LT_DATA_DIR"]
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LiveTranslate")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/LiveTranslate")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "live-translate")


DATA_DIR = _data_dir()
LOG_DIR = os.path.join(DATA_DIR, "logs")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

PORT = int(os.environ.get("PORT", "8000"))
HISTORY_LIMIT = 100       # utterances kept for listeners who join late
CHUNK_LIMIT = 1000
TTS_CACHE_LIMIT = 400
STALE_UTTERANCE = 8.0     # seconds without updates before an unfinished utterance is closed
SPEAKER_TIMEOUT = 12.0    # seconds without a heartbeat before the speaker page counts as closed
# Control endpoints (sending speech, clearing, settings) only accept requests from this
# computer, so nobody on the Wi-Fi can push text to every listener.
ALLOW_REMOTE_SPEAKER = os.environ.get("LT_ALLOW_REMOTE_SPEAKER") == "1"

VOICES = {
    "ka": {"f": "ka-GE-EkaNeural", "m": "ka-GE-GiorgiNeural"},
    "en": {"f": "en-US-AriaNeural", "m": "en-US-GuyNeural"},
}
OTHER = {"ka": "en", "en": "ka"}
TTS_RATES = ("+0%", "+10%", "+15%", "+25%")

# ---------------------------------------------------------------- state

clients = set()           # one queue.Queue per connected SSE listener
clients_lock = threading.Lock()
utterances = OrderedDict()  # utterance id -> {utt, src, dst, source, final, chunks, ts}
streams = {}                # utterance id -> simul.Utterance while it is still being spoken
chunks = OrderedDict()      # chunk id -> {id, utt, src, dst, text: {dst: ...}}
stream_lock = threading.RLock()
next_id = int(time.time() * 1000)  # unique across restarts, so browsers never reuse stale audio

tts_cache = {}            # (segment_id, lang, gender, rate) -> TtsJob
tts_lock = threading.Lock()
tts_pool = ThreadPoolExecutor(max_workers=8)

speaker = {"live": False, "lang": "ka", "seen": 0.0}   # what the speaker page last reported


def broadcast(event):
    data = json.dumps(event, ensure_ascii=False)
    with clients_lock:
        for q in list(clients):
            q.put(data)


def listener_count():
    with clients_lock:
        return len(clients)


def speaker_state():
    live = speaker["live"] and time.time() - speaker["seen"] < SPEAKER_TIMEOUT
    return {"type": "speaker", "live": live, "lang": speaker["lang"]}


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    # one rule per line, "what is heard = what it should be"; see apply_glossary
    "glossary": "",
    "tts_rate": os.environ.get("TTS_RATE", "+15%"),  # slightly faster speech keeps up with the speaker
    "clean_fillers": True,
}
settings = dict(DEFAULT_SETTINGS)
settings_lock = threading.Lock()
glossary_rules = []


def compile_glossary(text):
    """Rules apply to whichever text they match: a Georgian rule fixes what Chrome heard
    (or how a name is written before it is translated), an English rule fixes the English
    side. Georgian rules match at the start of a word and keep case endings
    ("დარბაზი = Darbazi" also turns "დარბაზში" into "Darbazში")."""
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=>", "→", "="):
            if sep in line:
                a, b = (x.strip() for x in line.split(sep, 1))
                break
        else:
            continue
        if not a:
            continue
        latin = bool(re.search(r"[A-Za-z]", a))
        if latin:
            rx = re.compile(r"(?<!\w)" + re.escape(a) + r"(?!\w)", re.IGNORECASE)
            rules.append((len(a), rx, b, b))
        elif a.endswith("ი") and len(a) > 2:
            # nominative -ი drops before case endings: დარბაზი, დარბაზში, დარბაზის
            rx = re.compile(r"(?<!\w)" + re.escape(a[:-1]) + r"(?:(ი)(?!\w)|(?=[ა-ჰ]))")
            if re.search(r"[A-Za-z]", b):
                stem = b + "-"   # Latin name keeps its form: Darbazi-ში, as Georgian writes foreign names
            else:
                stem = b[:-1] if b.endswith("ი") else b
            rules.append((len(a), rx, b, stem))
        else:
            rules.append((len(a), re.compile(r"(?<!\w)" + re.escape(a)), b, b))
    rules.sort(key=lambda r: -r[0])   # longest first, so phrases win over single words
    return [r[1:] for r in rules]


def apply_glossary(text):
    for rx, full, stem in glossary_rules:
        text = rx.sub(lambda m, f=full, s=stem: f if m.re.groups == 0 or m.group(1) else s, text)
    return text


def load_settings():
    global glossary_rules
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for k in DEFAULT_SETTINGS:
            if k in saved and type(saved[k]) is type(DEFAULT_SETTINGS[k]):
                settings[k] = saved[k]
    except (OSError, ValueError):
        pass
    if settings["tts_rate"] not in TTS_RATES:
        settings["tts_rate"] = DEFAULT_SETTINGS["tts_rate"] if DEFAULT_SETTINGS["tts_rate"] in TTS_RATES else "+15%"
    glossary_rules = compile_glossary(settings["glossary"])


def save_settings(changes):
    global glossary_rules
    with settings_lock:
        if isinstance(changes.get("glossary"), str):
            settings["glossary"] = changes["glossary"][:20000]
            glossary_rules = compile_glossary(settings["glossary"])
        if changes.get("tts_rate") in TTS_RATES:
            settings["tts_rate"] = changes["tts_rate"]
        if isinstance(changes.get("clean_fillers"), bool):
            settings["clean_fillers"] = changes["clean_fillers"]
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SETTINGS_FILE)
        except OSError as e:
            print(f"[settings] could not save: {e}", file=sys.stderr)
        return dict(settings)


# ---------------------------------------------------------------- source clean-up

# Hesitation sounds that recognizers sometimes write out ("ეეე", "ამმ", "um"). They
# only confuse the translator. Real words are never touched.
FILLERS = re.compile(r"(?<!\w)(?:ე{2,}|ა{2,}|ამ{2,}|მ{2,}|ჰ?მმ+|ხმ+|u+m+|u+h+|u+hm+|e+rm+|h+m+)(?!\w)", re.IGNORECASE)


def clean_source(text):
    """What the translator gets: fillers and stuttered repeats ("და და") removed,
    glossary applied. Deterministic, so every update of a growing utterance is cleaned
    the same way and the translations stay comparable."""
    if settings["clean_fillers"]:
        text = FILLERS.sub(" ", text)
        out = []
        for w in text.split():
            if out and simul.norm(w) and simul.norm(w) == simul.norm(out[-1]):
                continue
            out.append(w)
        text = " ".join(out)
    else:
        text = " ".join(text.split())
    return apply_glossary(text)


# ---------------------------------------------------------------- translation

# Google Translate's public web endpoints, the ones Chrome extensions use. Google rate
# limits per client id (not per host): after a few minutes at ~4 requests/s "gtx" may
# answer 429 everywhere while "dict-chrome-ex" keeps working, so several are kept in a
# pool. The fastest healthy one is asked first; if it hasn't answered after HEDGE_AFTER
# the next one is asked too; endpoints that fail or are rate limited sit out a cooldown.
# Fine for a demo; use the official Cloud Translation API (or DeepL / an LLM) for production.
HEDGE_AFTER = 0.35   # seconds
MAX_PARALLEL = 3


class Endpoint:
    def __init__(self, host, client):
        self.host, self.client = host, client
        self.conns = queue.LifoQueue()
        self.ms = 250.0             # smoothed response time
        self.cool_until = 0.0
        self.cooldown = 0.0
        self.lock = threading.Lock()

    def healthy(self, now):
        return now >= self.cool_until

    def succeeded(self, ms):
        with self.lock:
            self.cooldown = 0.0
            self.ms = self.ms * 0.7 + ms * 0.3

    def failed(self, limited):
        # rate limited: 1 min, doubling up to 15; other errors (timeouts, resets): a few seconds
        with self.lock:
            if limited:
                self.cooldown = min(900.0, max(60.0, self.cooldown * 2))
            else:
                self.cooldown = min(60.0, max(3.0, self.cooldown * 2))
            self.cool_until = time.monotonic() + self.cooldown


TRANSLATE_ENDPOINTS = [
    Endpoint("translate.googleapis.com", "dict-chrome-ex"),
    Endpoint("clients1.google.com", "dict-chrome-ex"),
    Endpoint("translate.googleapis.com", "gtx"),
    Endpoint("clients5.google.com", "dict-chrome-ex"),
]
translate_pool = ThreadPoolExecutor(max_workers=12)
TRANSLATE_CACHE_LIMIT = 3000
translate_cache = OrderedDict()   # (src, dst, text) -> translation
translate_cache_lock = threading.Lock()
# health shown on the speaker page
tstats = {"ok": 0, "fail": 0, "ms": 0.0, "last_error": "", "last_error_at": 0.0}


class HttpStatus(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def _translate_once(ep, text, src, dst):
    path = "/translate_a/t?" + urllib.parse.urlencode(
        {"client": ep.client, "sl": src, "tl": dst, "q": text}
    )
    t0 = time.monotonic()
    for attempt in range(2):
        try:
            conn = ep.conns.get_nowait()
        except queue.Empty:
            conn = http.client.HTTPSConnection(ep.host, timeout=8)
        try:
            conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0"})
            r = conn.getresponse()
            body = r.read()
            if r.status != 200:
                conn.close()
                ep.failed(limited=r.status in (403, 429))
                raise HttpStatus(r.status)
            ep.conns.put(conn)
            data = json.loads(body)
            first = data[0]
            result = first if isinstance(first, str) else first[0]
            ep.succeeded((time.monotonic() - t0) * 1000)
            return result
        except HttpStatus:
            raise
        except Exception:
            conn.close()
            if attempt == 1:   # (the first try may just have hit a connection the server closed)
                ep.failed(limited=False)
                raise


def translate(text, src, dst):
    """Translation of `text`, or None when no endpoint answered (callers keep what they
    had instead of treating the failure as an empty sentence). Identical text always
    gets the identical translation, which keeps the agreement in simul.py stable."""
    key = (src, dst, text)
    with translate_cache_lock:
        hit = translate_cache.get(key)
        if hit is not None:
            translate_cache.move_to_end(key)
            return hit
    t0 = now = time.monotonic()
    # healthy endpoints, fastest first; then benched ones, soonest back first
    ranked = sorted(TRANSLATE_ENDPOINTS, key=lambda e: (0, e.ms) if e.healthy(now) else (1, e.cool_until))
    futures = {}
    errors = []
    nxt = 0

    def launch():
        nonlocal nxt
        ep = ranked[nxt]
        nxt += 1
        futures[translate_pool.submit(_translate_once, ep, text, src, dst)] = ep

    launch()
    pending = set(futures)
    while pending:
        can_hedge = nxt < len(ranked) and len(futures) < MAX_PARALLEL
        budget = 8 - (time.monotonic() - t0)
        if budget <= 0:
            break
        done, pending = wait(pending, timeout=min(HEDGE_AFTER, budget) if can_hedge else budget,
                             return_when=FIRST_COMPLETED)
        for f in done:
            if f.exception() is None:
                result = f.result()
                ms = (time.monotonic() - t0) * 1000
                tstats["ok"] += 1
                tstats["ms"] = ms if tstats["ms"] == 0 else tstats["ms"] * 0.8 + ms * 0.2
                with translate_cache_lock:
                    translate_cache[key] = result
                    while len(translate_cache) > TRANSLATE_CACHE_LIMIT:
                        translate_cache.popitem(last=False)
                return result
            errors.append(f"{futures[f].host}/{futures[f].client}: {f.exception()}")
        # nothing yet (slow) or something failed: bring in the next endpoint
        if can_hedge:
            launch()
            pending = {f for f in futures if not f.done()}
    tstats["fail"] += 1
    tstats["last_error"] = "; ".join(errors) or "timeout"
    tstats["last_error_at"] = time.time()
    print(f"[translate] {src}->{dst} failed: {errors or 'timeout'}", file=sys.stderr)
    return None


def translation_health():
    recent_error = time.time() - tstats["last_error_at"] < 20
    state = "error" if recent_error and tstats["fail"] else ("slow" if tstats["ms"] > 900 else "ok")
    return {"state": state, "ms": round(tstats["ms"]), "ok": tstats["ok"], "fail": tstats["fail"],
            "error": tstats["last_error"] if recent_error else ""}


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

    async def _turn(self, ws, text, voice, rate, on_audio):
        C = self.C
        await ws.send_str(
            f"X-Timestamp:{C.date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        cfg = C.TTSConfig(voice, rate, "+0%", "+0Hz", "SentenceBoundary")
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

    async def _synth(self, text, voice, rate, on_audio):
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
                await self._turn(slot["ws"], text, voice, rate, count)
                slot["used"] = time.time()
                self.slots.put_nowait(slot)
                return
            except Exception:
                if slot:
                    await slot["ws"].close()
                self.slots.put_nowait(None)
                if attempt == 1 or sent:  # don't retry once audio went out (it would repeat)
                    raise

    def synthesize(self, text, voice, rate, on_audio):
        """Blocking call from any thread; on_audio(bytes) is called as audio arrives."""
        asyncio.run_coroutine_threadsafe(self._synth(text, voice, rate, on_audio), self.loop).result(timeout=30)


warm_tts = None


class TtsJob:
    """mp3 audio being synthesized; listeners can stream it while it is still growing."""

    def __init__(self, text, voice, rate):
        self.buf = bytearray()
        self.done = False
        self.failed = False
        self.cond = threading.Condition()
        tts_pool.submit(self._run, text, voice, rate)

    def _append(self, data):
        with self.cond:
            self.buf.extend(data)
            self.cond.notify_all()

    def _run(self, text, voice, rate):
        async def plain():
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    self._append(chunk["data"])

        try:
            if warm_tts:
                try:
                    warm_tts.synthesize(text, voice, rate, self._append)
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
    """Synthesized once per (segment, language, voice, speed) and shared by every listener."""
    rate = segment.get("rate") or settings["tts_rate"]
    key = (segment["id"], lang, gender, rate)
    with tts_lock:
        job = tts_cache.get(key)
        if job is None:
            job = TtsJob(segment["text"][lang], VOICES[lang][gender], rate)
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
          "text": {rec["dst"]: text}, "rate": settings["tts_rate"], "ts": time.time()}
    next_id += 1
    chunks[ch["id"]] = ch
    while len(chunks) > CHUNK_LIMIT:
        chunks.popitem(last=False)
    rec["chunks"].append({"id": ch["id"], "text": text})
    if edge_tts:
        get_tts(ch, rec["dst"], "f")
        get_tts(ch, rec["dst"], "m")
    broadcast({"type": "chunk", **ch})


def stream_update(uid, src, raw, final, mode):
    """One recognizer update. The translation request runs outside stream_lock, so
    a slow answer from Google never holds up listeners fetching audio."""
    text = clean_source(raw)
    with stream_lock:
        rec = utterances.get(uid)
        if rec is None:
            rec = {"utt": uid, "src": src, "dst": OTHER[src], "source": "", "raw": "", "final": False,
                   "chunks": [], "ts": time.time(), "seq": 0}
            utterances[uid] = rec
            while len(utterances) > HISTORY_LIMIT:
                old, _ = utterances.popitem(last=False)
                streams.pop(old, None)
        if rec["final"]:
            return rec, ""
        u = streams.get(uid)
        if u is None:
            u = streams[uid] = simul.Utterance()
        rec["seq"] += 1
        rec["raw"] = raw
        seq = rec["seq"]
        u.last_update = time.monotonic()   # keeps the janitor away while we translate
        if not final:
            speaker.update(live=True, seen=time.time())

    hyp = translate(text, src, rec["dst"]) if text else ""
    if hyp is None and final:
        hyp = translate(text, src, rec["dst"])   # the end of a sentence is worth one retry

    with stream_lock:
        if rec["final"] or (not final and rec["seq"] != seq):
            return rec, u.preview()   # a newer update (or the final) already got through
        if hyp is None:
            if not final:
                return rec, u.preview()   # keep what we have; the next update tries again
            # translation is down: close the sentence with the last translation we got
            hyp = " ".join(u.prev_hyps[-1]) if u.prev_hyps else ""
        else:
            hyp = apply_glossary(hyp)
        out, preview = u.update(text, final, mode, translated=hyp)
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
    """Emit words that waited too long, close utterances the speaker abandoned, and
    tell listeners when the speaker page goes quiet."""
    last_speaker = None
    while True:
        time.sleep(0.15)
        try:
            stale = []
            with stream_lock:
                now = time.monotonic()
                for uid, u in list(streams.items()):
                    rec = utterances.get(uid)
                    if rec is None:
                        streams.pop(uid, None)
                    elif now - u.last_update > STALE_UTTERANCE:
                        stale.append((uid, rec["src"], rec["raw"]))
                    else:
                        out = u.flush_if_stale(now)
                        if out:
                            log_stream({"utt": uid, "flush": out})
                        for piece in out:
                            emit_chunk(rec, piece)
                        if out:
                            broadcast({"type": "preview", "utt": uid, "text": u.preview()})
            for uid, src, raw in stale:
                stream_update(uid, src, raw, True, None)
            state = speaker_state()
            if state != last_speaker:
                last_speaker = state
                broadcast(state)
        except Exception as e:
            print(f"[stream] janitor error: {e}", file=sys.stderr)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if any(p in self.path for p in ("/events", "/api/tts", "/api/info", "/api/stream")):
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

    def is_local(self):
        host = self.client_address[0]
        return ALLOW_REMOTE_SPEAKER or host in ("127.0.0.1", "::1") or host.startswith("::ffff:127.")

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
            if qs.get("role") == ["speaker"] and self.is_local():
                lang = qs.get("lang", ["ka"])[0]
                speaker.update(live=qs.get("live") == ["1"], seen=time.time(),
                               lang=lang if lang in OTHER else "ka")
            return self.send_json({
                "version": VERSION,
                "urls": lan_urls(),
                "listeners": listener_count(),
                "tts": edge_tts is not None,
                "translation": translation_health(),
                "local": self.is_local(),
            })
        if path == "/api/settings":
            return self.send_json({**settings, "tts_rates": TTS_RATES})

        pages = {"/": "index.html", "/speaker": "speaker.html", "/listen": "listen.html"}
        rel = pages.get(path, path.lstrip("/"))
        full = os.path.normpath(os.path.join(PUBLIC_DIR, rel))
        if not full.startswith(PUBLIC_DIR) or not os.path.isfile(full):
            return self.send_error_json(404, "not found")
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript") or ctype.endswith("json"):
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
        if not self.is_local():
            return self.send_error_json(403, "the speaker controls only work on the server computer")
        if self.path == "/api/stream":
            return self.handle_stream()
        if self.path == "/api/clear":
            with stream_lock:
                utterances.clear()
                streams.clear()
                chunks.clear()
            broadcast({"type": "clear"})
            return self.send_json({"ok": True})
        if self.path == "/api/settings":
            try:
                changes = self.read_json()
            except Exception:
                return self.send_error_json(400, "bad json")
            return self.send_json({**save_settings(changes), "tts_rates": TTS_RATES})
        if self.path == "/api/shutdown":
            self.send_json({"ok": True})
            broadcast({"type": "speaker", "live": False, "lang": speaker["lang"]})
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
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
            "source": rec["source"],
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
            snapshot = json.dumps({"type": "history", "utterances": list(utterances.values()),
                                   "speaker": speaker_state()}, ensure_ascii=False)
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


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # browsers drop kept-alive connections on reload; that's not worth a traceback
        # in the window the organizer is looking at
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class DualStackServer(QuietServer):
    """Listen on IPv6 and IPv4 at once. Windows resolves "localhost" to ::1 first,
    and an IPv4-only server makes every such request wait ~2s for the fallback."""

    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class Ipv4Server(QuietServer):
    pass


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1.5) as r:
            return "version" in json.loads(r.read())
    except Exception:
        return False


def bind(port):
    """The first free port from `port` on. Returns (server, port), or (None, port) when
    Live Translate itself is already running there."""
    for p in range(port, port + 10):
        for cls, addr in ((DualStackServer, "::"), (Ipv4Server, "")):
            try:
                return cls((addr, p), Handler), p
            except OSError:
                continue
        if already_running(p):
            return None, p
    raise SystemExit(f"Ports {port}-{port + 9} are all busy. Set another one with PORT=...")


def open_in_chrome(url):
    """The speaker page needs Chrome (speech recognition); open it there if we can."""
    if os.environ.get("LT_NO_BROWSER") == "1":
        return
    try:
        if sys.platform == "darwin" and os.path.isdir("/Applications/Google Chrome.app"):
            subprocess.Popen(["open", "-a", "Google Chrome", url])
            return
        if sys.platform == "win32":
            for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                         os.environ.get("LOCALAPPDATA")):
                exe = base and os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                if exe and os.path.isfile(exe):
                    subprocess.Popen([exe, url])
                    return
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            exe = shutil.which(name)
            if exe:
                subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    except OSError:
        pass
    webbrowser.open(url)


def main():
    global warm_tts, PORT
    for stream in (sys.stdout, sys.stderr):   # (None in the windowed macOS app)
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)
    load_settings()
    server, PORT = bind(PORT)
    speaker_url = f"http://localhost:{PORT}/speaker"
    if server is None:
        print(f"\n  Live Translate is already running: {speaker_url}\n")
        open_in_chrome(speaker_url)
        return
    if edge_tts is None:
        print("! edge-tts not installed - listeners will fall back to browser voices "
              "(no Georgian voice in Chrome). Run: pip install edge-tts")
    else:
        try:
            warm_tts = WarmTts()
        except Exception as e:
            print(f"! warm TTS connections unavailable ({e}); using one connection per phrase")
    threading.Thread(target=stream_janitor, daemon=True).start()
    print(f"\n  Live Translate {VERSION} is running\n")
    print(f"  Speaker (this computer, Chrome):  {speaker_url}")
    print(f"  Listener (this computer):         http://localhost:{PORT}/listen")
    for u in lan_urls():
        print(f"  Listener (phones on same Wi-Fi):  {u}")
    print("\n  Keep this window open during the event. Ctrl+C to stop.\n")
    threading.Timer(0.5, open_in_chrome, args=(speaker_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
