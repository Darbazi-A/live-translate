// Small shared helpers for the speaker and listener pages (no build step, plain script).
(function () {
  const $ = (id) => document.getElementById(id);

  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
    num(k, d) { const v = parseFloat(store.get(k, "")); return Number.isFinite(v) ? v : d; },
    bool(k, d) { const v = store.get(k, ""); return v === "" ? d : v === "1"; },
    setBool(k, v) { store.set(k, v ? "1" : "0"); },
  };

  // 24px stroke icons
  const ICONS = {
    mic: '<path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z"/><path d="M19 11a7 7 0 0 1-14 0M12 18v3"/>',
    stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5" fill="currentColor" stroke="none"/>',
    sliders: '<path d="M3 6h11M18 6h3M3 12h5M12 12h9M3 18h13M20 18h1"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    volume: '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>',
    mute: '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="m16 9.5 5 5M21 9.5l-5 5"/>',
    headphones: '<path d="M3 15v-3a9 9 0 0 1 18 0v3"/><path d="M3 15h2.5A1.5 1.5 0 0 1 7 16.5v3A1.5 1.5 0 0 1 5.5 21H5a2 2 0 0 1-2-2v-4ZM21 15h-2.5a1.5 1.5 0 0 0-1.5 1.5v3a1.5 1.5 0 0 0 1.5 1.5h.5a2 2 0 0 0 2-2v-4Z"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.2a6.5 6.5 0 0 1 3.5 5.8"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    play: '<path d="M7 4.8v14.4a.8.8 0 0 0 1.2.7l11.5-7.2a.8.8 0 0 0 0-1.4L8.2 4.1A.8.8 0 0 0 7 4.8Z" fill="currentColor" stroke="none"/>',
    qr: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M14 14h3v3M21 14v.01M14 21h3M21 18v3h-1"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    trash: '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3Z"/>',
    power: '<path d="M12 3v8"/><path d="M6.3 7.2a8 8 0 1 0 11.4 0"/>',
    text: '<path d="M4 7V5h16v2M12 5v14M9 19h6"/>',
    maximize: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    send: '<path d="M4.5 12 20 4.5 13.5 20l-2.5-6.5L4.5 12Z"/>',
    book: '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5v-15Z"/><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19v-3"/>',
    waves: '<path d="M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4"/>',
    speaker: '<rect x="5" y="2.5" width="14" height="19" rx="2.5"/><circle cx="12" cy="14" r="3.5"/><path d="M12 6.5h.01"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.5M4 4v3.5h3.5M4 13a8 8 0 0 0 14.6 4.5M20 20v-3.5h-3.5"/>',
    keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M7.5 14h9"/>',
    activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  };
  function icon(name, cls) {
    return `<svg class="ico ${cls || ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  }
  function hydrateIcons(root) {
    (root || document).querySelectorAll("i[data-icon]").forEach((el) => {
      el.outerHTML = icon(el.dataset.icon, el.className);
    });
  }

  // theme: "auto" follows the OS
  const theme = {
    get() { return store.get("lt-theme", "auto"); },
    apply(mode) {
      if (mode === "dark" || mode === "light") document.documentElement.dataset.theme = mode;
      else delete document.documentElement.dataset.theme;
    },
    set(mode) { store.set("lt-theme", mode); theme.apply(mode); },
  };
  theme.apply(theme.get());

  // segmented control: buttons with data-value; returns a setter
  function seg(el, value, onChange) {
    const set = (v) => { for (const b of el.querySelectorAll("button")) b.classList.toggle("on", b.dataset.value === String(v)); };
    set(value);
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-value]");
      if (!b || !el.contains(b)) return;
      set(b.dataset.value);
      onChange(b.dataset.value);
    });
    return set;
  }

  // range input with a filled track and an optional value label
  function range(input, label, fmt) {
    const paint = () => {
      const p = ((input.value - input.min) / (input.max - input.min)) * 100;
      input.style.setProperty("--p", p + "%");
      if (label) label.textContent = fmt ? fmt(+input.value) : input.value;
    };
    input.addEventListener("input", paint);
    paint();
    return paint;
  }

  function toast(msg, kind) {
    // inside an open modal dialog, or the toast would sit under its backdrop
    const host = document.querySelector("dialog[open]") || document.body;
    let box = host.querySelector(":scope > .toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; host.appendChild(box); }
    const t = document.createElement("div");
    t.className = "toast" + (kind ? " " + kind : "");
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(() => t.remove(), kind === "err" ? 5200 : 2600);
  }

  // <dialog> sheets: close on backdrop click and on [data-close]
  function sheet(dialog) {
    dialog.addEventListener("click", (e) => {
      if (e.target === dialog || e.target.closest("[data-close]")) dialog.close();
    });
    return { open: () => dialog.showModal(), close: () => dialog.close() };
  }

  const pad = (n) => String(n).padStart(2, "0");
  function clock(ts) {
    const d = ts ? new Date(ts * 1000) : new Date();
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // Output devices (speakers / headphones). Chrome only lists them by name once the page
  // has microphone permission; Safari and iPhones choose the output in the OS instead.
  const canPickOutput = typeof HTMLMediaElement !== "undefined" && "setSinkId" in HTMLMediaElement.prototype;
  async function listDevices(kind) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter((d) => d.kind === kind);
  }
  async function unlockDeviceNames() {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
  }
  function fillSelect(select, devices, current, defaultLabel) {
    select.textContent = "";
    const def = document.createElement("option");
    def.value = "";
    def.textContent = defaultLabel;
    select.appendChild(def);
    let n = 0;
    for (const d of devices) {
      if (!d.deviceId || d.deviceId === "default" || d.deviceId === "communications") continue;
      const o = document.createElement("option");
      o.value = d.deviceId;
      o.textContent = d.label || `${d.kind === "audioinput" ? "Microphone" : "Output"} ${++n}`;
      select.appendChild(o);
    }
    select.value = [...select.options].some((o) => o.value === current) ? current : "";
    return devices.some((d) => d.label);
  }

  window.LT = { $, store, icon, hydrateIcons, theme, seg, range, toast, sheet, clock, canPickOutput, listDevices, unlockDeviceNames, fillSelect };
})();
