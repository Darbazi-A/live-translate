// Test harness: replays speech the way desktop Chrome's Web Speech API reports it,
// into the real speaker page, and records what listeners receive.
// Chrome behaviours reproduced:
//  - interim text split into a stable result + a volatile tail result (both non-final)
//  - the newest word first arrives half-recognized, then corrected
//  - a pause (~1s) merges them into ONE final result; results.length shrinks
//  - mid-sentence pauses shorter than that keep the result open
//  - after long silence Chrome ends the session (onend) and the page restarts it
window.chromeSim = async function (script, opts = {}) {
  const wordMs = opts.wordMs || 380;
  const finalizeAfterMs = opts.finalizeAfterMs || 900;
  const endAfterMs = opts.endAfterMs || 3500;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const received = [];
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "chunk") received.push({ t: Math.round(performance.now()), text: m.text[m.dst] });
  };
  await sleep(300);

  const mkRes = (transcript, isFinal) => Object.assign([{ transcript, confidence: 0.9 }], { isFinal });
  let rec = null;
  let results = [];
  const fire = (resultIndex) => rec.onresult({ resultIndex, results: results.slice() });

  // capture the recognizer the page creates
  const RealSR = window.webkitSpeechRecognition;
  window.__fakeStarts = 0;
  RealSR.prototype.start = function () {
    rec = this; results = []; window.__fakeStarts++;
    setTimeout(() => this.onstart && this.onstart(), 0);
  };
  RealSR.prototype.stop = RealSR.prototype.abort = function () { setTimeout(() => this.onend && this.onend(), 0); };
  window.__ltNoMic = true;   // no real microphone pipeline under test
  document.getElementById("mic").click();
  await sleep(100);

  const t0 = performance.now();
  const spokenAt = [];
  for (const { text, pause } of script) {
    const words = text.split(" ");
    const base = results.length;           // index of this utterance's first result
    for (let i = 0; i < words.length; i++) {
      for (const part of [words[i].slice(0, Math.ceil(words[i].length / 2)), words[i]]) {
        const cur = [...words.slice(0, i), part];
        const stable = cur.slice(0, Math.max(0, cur.length - 2)).join(" ");
        const tail = cur.slice(Math.max(0, cur.length - 2)).join(" ");
        results = results.slice(0, base);
        if (stable) results.push(mkRes(stable, false));
        results.push(mkRes((stable ? " " : "") + tail, false));
        fire(base);
        await sleep(wordMs / 2);
      }
      spokenAt.push([words[i], Math.round(performance.now() - t0)]);
    }
    if (pause * 1000 >= finalizeAfterMs) {
      await sleep(finalizeAfterMs);
      results = results.slice(0, base);
      let finalText = text;
      if (opts.reviseFinal) {   // Chrome often corrects a word when it finalizes
        const w = text.split(" ");
        const k = Math.floor(w.length / 2);
        w[k] = w[k].length > 4 ? w[k].slice(0, -1) : w[k] + "ც";
        finalText = w.join(" ");
      }
      results.push(mkRes((base ? " " : "") + finalText, true));   // merged final result
      fire(base);
      const rest = pause * 1000 - finalizeAfterMs;
      if (rest >= endAfterMs) { await sleep(endAfterMs); rec.onend(); await sleep(rest - endAfterMs); }
      else await sleep(rest);
    } else {
      await sleep(pause * 1000);
    }
  }
  await sleep(4000);
  document.getElementById("mic").click();
  await sleep(500);
  es.close();
  return { spokenAt, received: received.map((r) => ({ ...r, t: r.t - Math.round(t0) })) };
};

window.analyzeRepeats = (text) => {
  const stop = new Set("a an the and or but so to of in on at for with from by about as it its this that is are was were be will would can has have had do we you they he she i our your their my his her there here then very not".split(" "));
  const w = text.toLowerCase().replace(/[^\w\s']/g, " ").split(/\s+/).filter(Boolean);
  const reps = [];
  for (let i = 0; i < w.length; i++)
    for (let j = Math.max(0, i - 8); j < i; j++)
      if (w[i] === w[j] && !stop.has(w[i]) && w[i].length > 3) { reps.push(w.slice(Math.max(0, j - 2), i + 2).join(" ")); break; }
  return reps;
};

window.TEST_SCRIPTS = {
  intro: [
    { text: "დღეს ჩვენ ვისაუბრებთ ხელოვნურ ინტელექტზე", pause: 0.6 },
    { text: "და იმაზე თუ როგორ შეცვლის ის ჩვენს ცხოვრებას", pause: 2.0 },
    { text: "ჩვენი კომპანია უკვე ხუთი წელია", pause: 1.3 },
    { text: "მუშაობს ამ მიმართულებით", pause: 3.0 },
    { text: "და ბევრი საინტერესო პროექტი გვაქვს განხორციელებული", pause: 1.5 },
    { text: "მინდა მადლობა გადავუხადო ყველას ვინც დღეს აქ მობრძანდა", pause: 5.0 },
    { text: "ახლა გადავიდეთ პირველ თემაზე", pause: 0.7 },
    { text: "რომელიც ეხება განათლებას", pause: 2.0 },
  ],
  conference: [
    { text: "ძვირფასო კოლეგებო მოგესალმებით ჩვენს ყოველწლიურ კონფერენციაზე", pause: 1.6 },
    { text: "წელს ჩვენ", pause: 0.6 },
    { text: "განსაკუთრებულ ყურადღებას დავუთმობთ ციფრულ ტრანსფორმაციას", pause: 2.2 },
    { text: "ბოლო ორი წლის განმავლობაში", pause: 0.8 },
    { text: "ჩვენმა გუნდმა შექმნა ახალი პლატფორმა", pause: 1.4 },
    { text: "რომელიც უკვე ათასზე მეტ მომხმარებელს ემსახურება", pause: 3.5 },
    { text: "ეს პლატფორმა საშუალებას აძლევს მასწავლებლებს", pause: 0.7 },
    { text: "მარტივად შექმნან ინტერაქტიული გაკვეთილები", pause: 6.0 },
    { text: "კითხვები თუ გაქვთ შეგიძლიათ მოგვწეროთ", pause: 2.0 },
  ],
};

// Runs tests one after another in the background; results land in window.__suite.
window.runSuite = (cases) => {
  window.__suite = { done: false, results: [] };
  (async () => {
    for (const [name, mode, opts] of cases) {
      await fetch("/api/clear", { method: "POST", body: "{}" });
      document.querySelector(`#modeSeg [data-mode="${mode}"]`).click();
      const r = await window.chromeSim(window.TEST_SCRIPTS[name], opts || {});
      const joined = r.received.map((x) => x.text).join(" ");
      window.__suite.results.push({ name, mode, opts, out: r.received.map((x) => x.text).join(" | "), repeats: window.analyzeRepeats(joined) });
    }
    window.__suite.done = true;
  })();
  return "started";
};
