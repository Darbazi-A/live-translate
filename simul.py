"""
Simultaneous translation of a growing utterance ("re-translate + local agreement").

The speaker's recognizer keeps sending the whole current utterance as it grows.
Every update re-translates the full text, so the translator always sees the full
context (Georgian puts the verb last; translating fragments on their own made
English repeat subjects: "Today we" + "We will talk").

An English word is committed (sent to listeners and voiced) only once two
consecutive translations agree on it. Committed words are remembered, so later
translations are aligned against them and only the genuinely new tail is spoken:
nothing is said twice even when Google reshuffles the sentence.
"""
import re
import time

# How eagerly words are committed. agree: how many consecutive translations must
# agree on a word; hold: newest words of a translation that are always held back
# (they change most); force: seconds after which words are committed even without
# agreement; min_emit / max_age: when the committed words are handed to the voice
# (fewer words = lower delay, choppier speech).
MODES = {
    "fast":     {"agree": 2, "hold": 0, "force": 1.8, "min_emit": 2, "max_age": 0.5},
    "balanced": {"agree": 3, "hold": 1, "force": 3.0, "min_emit": 3, "max_age": 0.9},
    "quality":  {"agree": 3, "hold": 3, "force": 6.0, "min_emit": 6, "max_age": 2.0},
}
DEFAULT_MODE = "balanced"

STOPWORDS = set(
    "a an the and or but so to of in on at for with from by about as it its this that these "
    "those is are was were be been being will would can could should shall may might must has "
    "have had do does did not no we you they he she i me us him her them our your their my his "
    "there here then than also very".split()
)
_CLAUSE_END = re.compile(r"[.,;:!?]$")


def norm(word):
    return re.sub(r"[^\w']+", "", word.lower())


def align(spoken, hyp):
    """Index in `hyp` right after the part that corresponds to the words already
    spoken. Uses the longest common subsequence: each matched word scores +2 and
    every hypothesis word consumed costs 1, so revised words are skipped rather
    than repeated, while a lone far-away match can't swallow new words."""
    if not spoken:
        return 0
    s = [norm(w) for w in spoken]
    t = [norm(w) for w in hyp]
    prev = [0] * (len(t) + 1)
    for sw in s:
        cur = [0] * (len(t) + 1)
        for j, tw in enumerate(t):
            cur[j + 1] = prev[j] + 1 if sw and sw == tw else max(prev[j + 1], cur[j])
        prev = cur
    best_k, best = 0, 0
    for k in range(len(t) + 1):
        score = 2 * prev[k] - k
        if score >= best:  # ties: prefer consuming more (skip rather than repeat)
            best, best_k = score, k
    return best_k


def common_prefix(a, b):
    n = 0
    while n < len(a) and n < len(b) and norm(a[n]) == norm(b[n]):
        n += 1
    return n


def _contains(seq, gram):
    n = len(gram)
    return any(seq[i:i + n] == gram for i in range(len(seq) - n + 1))


def _is_content(w):
    return len(w) > 3 and w not in STOPWORDS


def drop_repeats(words, spoken):
    """Remove words that would audibly repeat what was just said: an immediate echo
    of the last word, a content word said within the last 5 words, or a 2-4 word
    phrase (with a content word) said in the last dozen words. Typical when Google
    moves a word or phrase to another spot as the sentence grows."""
    recent = [norm(w) for w in spoken[-12:]]
    kept, i = [], 0
    while i < len(words):
        w = norm(words[i])
        if w and recent and w == recent[-1]:
            i += 1
            continue
        if _is_content(w) and w in recent[-5:]:
            i += 1
            continue
        for n in (4, 3, 2):
            gram = [norm(x) for x in words[i:i + n]]
            if len(gram) == n and any(_is_content(g) for g in gram) and _contains(recent, gram):
                i += n
                break
        else:
            kept.append(words[i])
            recent.append(w)
            i += 1
    return kept


class Utterance:
    def __init__(self, translate):
        self.translate = translate      # text -> translated text
        self.accounted = []             # hypothesis words already consumed (spoken or dropped)
        self.spoken = []                # words actually handed to listeners
        self.buffer = []                # committed, waiting to be emitted as one chunk
        self.buffer_since = None
        self.prev_hyps = []             # latest translations, newest last
        self.cand_since = None          # when the current uncommitted tail first appeared
        self.tail = []                  # uncommitted words of the latest translation
        self.mode = MODES[DEFAULT_MODE]
        self.last_update = time.monotonic()
        self.text = ""

    def update(self, text, final, mode=DEFAULT_MODE, now=None):
        """Feed the full current utterance text. Returns (chunks_to_emit, preview)."""
        now = time.monotonic() if now is None else now
        m = self.mode = MODES.get(mode, MODES[DEFAULT_MODE])
        self.last_update = now
        self.text = text
        hyp = self.translate(text).split() if text.strip() else []
        k = align(self.accounted, hyp)
        cand = hyp[k:]

        if final:
            n = len(cand)
        else:
            n = 0
            older = self.prev_hyps[-(m["agree"] - 1):]
            if len(older) == m["agree"] - 1:
                n = len(cand)
                for prev in older:
                    n = min(n, common_prefix(cand, prev[align(self.accounted, prev):]))
            n = min(n, max(0, len(cand) - m["hold"]))
            if cand and self.cand_since is None:
                self.cand_since = now
            if cand and now - self.cand_since >= m["force"]:
                n = max(n, len(cand) - max(1, m["hold"]))
        self.prev_hyps = (self.prev_hyps + [hyp])[-3:]

        commit = cand[:n]
        if commit:
            self.accounted += commit
            kept = drop_repeats(commit, self.spoken)
            if kept and not self.buffer:
                self.buffer_since = now
            self.spoken += kept
            self.buffer += kept
        self.tail = cand[n:]
        if not self.tail:
            self.cand_since = None
        elif commit:
            self.cand_since = now

        chunks = self._emit(now, force=final)
        return chunks, self.preview()

    def flush_if_stale(self, now=None):
        """Emit buffered words that waited too long (speaker paused mid-sentence)."""
        now = time.monotonic() if now is None else now
        return self._emit(now, force=False)

    def preview(self):
        return " ".join(self.buffer + self.tail)

    def _emit(self, now, force):
        if not self.buffer:
            return []
        m = self.mode
        if (force or len(self.buffer) >= m["min_emit"] or _CLAUSE_END.search(self.buffer[-1])
                or now - self.buffer_since >= m["max_age"]):
            chunk = " ".join(self.buffer)
            self.buffer = []
            return [chunk]
        return []
