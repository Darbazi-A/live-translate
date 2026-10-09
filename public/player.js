// Plays the voiced translation pieces back to back. Each piece is downloaded, decoded,
// trimmed of leading/trailing silence and played; when pieces pile up, playback speeds up
// (pitch preserved) to stay live, and pieces older than maxLag are skipped.
// Used by the listener page and by the speaker page's "play on this computer" monitor.
(function () {
  const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";

  // iPhones ignore element volume (only the hardware buttons change it)
  const volumeWorks = (() => {
    try { const a = new Audio(); a.volume = 0.5; return a.volume === 0.5; } catch { return false; }
  })();

  class Player {
    constructor() {
      this.audio = new Audio();
      this.audio.preservesPitch = true;
      this.clips = [];
      this.pumping = false;
      this.decodeCtx = null;
      this.stopCurrent = null;
      this.enabled = true;     // false = captions only
      this.lang = "en";        // language this player voices
      this.voice = "f";
      this.speed = "auto";     // "auto" or a fixed playback rate
      this.maxLag = 8;         // seconds behind the speaker before pieces get skipped
      this.sinkId = "";
      this.onstate = null;     // ({lag, queued, playing}) => void
      this.playing = false;
    }

    static get volumeWorks() { return volumeWorks; }

    /** Call from a click/tap: phones only allow audio that a user gesture started. */
    unlock() {
      this.audio.src = SILENT_WAV;
      this.audio.play().catch(() => {});
      if (navigator.audioSession) navigator.audioSession.type = "playback";
      if (window.speechSynthesis) speechSynthesis.getVoices();
    }

    setVolume(v) { this.audio.volume = Math.max(0, Math.min(1, v)); }

    async setSink(id) {
      this.sinkId = id || "";
      if (this.audio.setSinkId) await this.audio.setSinkId(this.sinkId);
    }

    setEnabled(on) {
      this.enabled = on;
      if (!on) this.clear();
    }

    enqueue(seg) {
      if (!this.enabled || seg.dst !== this.lang) return;
      const item = { seg, lang: this.lang, at: performance.now(), p: this._prepare(seg, this.lang, this.voice) };
      item.p.catch(() => {});
      this.clips.push(item);
      this._report();
      this._pump();
    }

    clear() {
      for (const c of this.clips.splice(0)) c.p.then((u) => u && URL.revokeObjectURL(u), () => {});
      this.audio.pause();
      if (this.stopCurrent) this.stopCurrent();
      if (window.speechSynthesis) speechSynthesis.cancel();
      this._report();
    }

    _report(lag) {
      if (this.onstate) this.onstate({ lag: lag ?? this.lastLag ?? 0, queued: this.clips.length, playing: this.playing });
    }

    _decode(buf) {
      if (!this.decodeCtx) {
        const Ctx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
        this.decodeCtx = new Ctx(1, 1, 24000);
      }
      return new Promise((resolve, reject) => {
        const p = this.decodeCtx.decodeAudioData(buf, resolve, reject);
        if (p && p.then) p.then(resolve, reject);
      });
    }

    _trimmedWavUrl(ab) {
      const sr = ab.sampleRate, d = ab.getChannelData(0), thr = 0.015;
      let a = 0, b = d.length - 1;
      while (a < d.length && Math.abs(d[a]) < thr) a++;
      while (b > a && Math.abs(d[b]) < thr) b--;
      if (a >= b) return null;
      a = Math.max(0, a - Math.round(0.02 * sr));
      b = Math.min(d.length, b + Math.round(0.07 * sr));
      const n = b - a, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
      const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
      w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt ");
      v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
      v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true);
      v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
      for (let i = 0; i < n; i++) {
        const s = Math.max(-1, Math.min(1, d[a + i]));
        v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      }
      return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
    }

    async _prepare(seg, lang, voice) {
      const r = await fetch(`/api/tts?id=${seg.id}&lang=${lang}&voice=${voice}`);
      if (!r.ok) throw new Error("tts " + r.status);
      return this._trimmedWavUrl(await this._decode(await r.arrayBuffer()));
    }

    _play(url, rate) {
      return new Promise((resolve) => {
        // (not "pause": changing src fires one, which would cut the previous piece short)
        const done = () => { this.stopCurrent = null; resolve(); };
        this.stopCurrent = done;
        this.audio.onended = this.audio.onerror = done;
        this.audio.defaultPlaybackRate = rate;
        this.audio.src = url;
        this.audio.playbackRate = rate;
        this.audio.play().catch(done);
      });
    }

    async _pump() {
      if (this.pumping) return;
      this.pumping = true;
      try {
        while (this.clips.length) {
          const item = this.clips.shift();
          let url = null, failed = false;
          try { url = await item.p; } catch { failed = true; }
          const lag = (performance.now() - item.at) / 1000;
          const skip = !this.enabled || item.lang !== this.lang || (lag > this.maxLag && this.clips.length);
          if (skip) { if (url) URL.revokeObjectURL(url); continue; }
          this.lastLag = lag;
          this.playing = true;
          this._report(lag);
          if (failed) { await this._speakWithBrowser(item.seg.text[item.lang], item.lang); continue; }
          if (!url) continue;
          // auto: 1.0x when keeping up, up to 1.35x when pieces are waiting or we're behind
          const auto = Math.min(1.35, 1 + 0.1 * this.clips.length + (lag > 3 ? 0.1 : 0));
          const rate = this.speed === "auto" ? auto : Math.max(+this.speed, lag > 4 ? auto : 0);
          await this._play(url, rate);
          URL.revokeObjectURL(url);
        }
      } finally {
        this.pumping = false;
        this.playing = false;
        this._report();
      }
    }

    // Fallback when the server has no TTS: use a browser voice if one exists for the language.
    _speakWithBrowser(text, lang) {
      return new Promise((resolve) => {
        if (!window.speechSynthesis) return resolve();
        const v = speechSynthesis.getVoices().find((x) => x.lang.toLowerCase().startsWith(lang));
        if (!v || !text) return resolve();
        const u = new SpeechSynthesisUtterance(text);
        u.voice = v; u.lang = v.lang; u.volume = this.audio.volume; u.onend = u.onerror = resolve;
        speechSynthesis.speak(u);
      });
    }
  }

  window.LT = window.LT || {};
  window.LT.Player = Player;
})();
