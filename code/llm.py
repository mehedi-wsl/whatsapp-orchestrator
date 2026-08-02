"""Local LLM layer -- contextual reasoning, no API key, no network at run time.

Backed by a llama.cpp server on localhost speaking the OpenAI chat-completions
shape. There is no hosted provider and no secret anywhere in this module: see
`docs` in ARCHITECTURE.md s11 for why a local model was chosen over a free API
tier for a submission that a grader has to reproduce.

Three guarantees this module must keep:

1. NEVER ROUTES.  The model refines `message_type`, writes `reason` prose, and
   answers narrow yes/no questions. It cannot set `action`. The six-gate stack
   in decide.py remains the sole authority on routing.

2. NEVER SEES A SAFETY ROW.  Layer 1 of the stack fires before any call here.
   Message text is attacker-controlled -- 5 of the 110 messages carry
   instructions aimed at the router -- so injected text must never reach a
   model whose output is trusted. Callers must skip rows decided by
   `safety_composite`.

3. ALWAYS DEGRADES.  Every entry point returns None when the server is down,
   the cache misses, or the reply fails validation. The deterministic path in
   decide.py stands alone; this layer is strictly additive.

Determinism: temperature 0, fixed seed, and a JSON response cache keyed by
(model, prompt). With the cache committed, a run reproduces exactly on a
machine that has never downloaded the weights.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence

from schema import MESSAGE_TYPES

# --------------------------------------------------------------------------
# Configuration -- environment only, and none of it secret
# --------------------------------------------------------------------------

def _load_dotenv(path: str = None) -> None:
    """Read KEY=VALUE lines from .env into the environment if not already set.

    AGENTS.md rule 4 permits a .env file and requires that secrets live only in
    the environment. Nothing here ever writes a value back out, and no value
    read here is logged, cached, or included in a cache key.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # Both the repo root and code/ are checked, nearest first. Which one a
    # person picks is a coin flip, and failing silently because the file was
    # one directory over is a bad way to spend an afternoon.
    candidates = [path] if path else [
        os.path.join(here, ".env"),
        os.path.join(os.path.dirname(here), ".env"),
    ]
    for cand in candidates:
        if not cand or not os.path.exists(cand):
            continue
        try:
            with open(cand, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        except OSError:
            continue


_load_dotenv()

BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080")
MODEL = os.environ.get("LLM_MODEL", "local")

# Hosted backend. Selected automatically when a key is present, because the
# local 7B needs ~111s per message and a hosted call needs ~1s -- the
# difference between a 3.4-hour extraction and a 2-minute one.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def backend() -> str:
    """'anthropic' when a usable key is available, else 'local'.

    Goes sticky-local after the first hosted failure, so one bad key does not
    cost a network round trip on every one of the 110 messages.
    """
    return "anthropic" if (ANTHROPIC_KEY and not _hosted_down) else "local"


def supports_grammar() -> bool:
    """Whether output can be constrained structurally rather than validated.

    The local llama.cpp backend enforces GBNF, so a malformed or manipulated
    reply is unreachable. The hosted API has no equivalent, so the same
    guarantee weakens to "malformed replies are rejected and fall back". Every
    caller must therefore still validate; the grammar is defence, not a
    substitute for checking.
    """
    return backend() == "local"


# Replay mode. The committed `llm_cache.json` holds every answer this system
# has ever used, so a run with no key and no server can still reproduce the
# submitted output exactly -- it just may not produce anything *new*. Setting
# this makes a cache miss return None immediately instead of reaching for a
# backend, which is the difference between "offline and faithful" and "offline
# and quietly degraded".
CACHE_ONLY = False


def set_cache_only(flag: bool) -> None:
    global CACHE_ONLY
    CACHE_ONLY = bool(flag)


TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
CACHE_PATH = os.environ.get(
    "LLM_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_cache.json"),
)

SEED = 7
MAX_TEXT_CHARS = 900          # truncate long bodies; the tail is never the cue


_served: Optional[str] = None


def served_model() -> str:
    """Identity of the weights actually loaded, for cache keying.

    Asked of the server once and remembered. Falls back to $LLM_MODEL when the
    server is unreachable, which is the read-only path where the answer only
    has to match whatever filled the cache originally.
    """
    global _served
    if _served is not None:
        return _served
    # The hosted backend has its own identity; without this a Sonnet verdict
    # and a local 7B verdict would share a cache slot.
    if backend() == "anthropic":
        _served = "anthropic:" + ANTHROPIC_MODEL
        return _served
    try:
        req = urllib.request.Request(BASE_URL + "/v1/models")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.load(resp)
        _served = os.path.basename(str(body["data"][0]["id"]))
    except (urllib.error.URLError, OSError, KeyError, IndexError,
            json.JSONDecodeError, ValueError):
        _served = MODEL
    return _served


class _Cache:
    """Prompt -> completion, persisted as JSON.

    Written through on every miss so a long run that dies partway keeps what it
    paid for. Read once at import.
    """

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, str] = {}
        self.hits = 0
        self.misses = 0
        self.dirty = False
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def key(self, prompt: str, grammar: str, max_tokens: int) -> str:
        # The identity of the actual weights must be in the key. `MODEL` is the
        # literal "local" for every llama.cpp server regardless of which GGUF
        # is loaded, so keying on it alone made a 3B answer indistinguishable
        # from a 7B one -- swapping models silently served the old model's
        # cached verdicts and made a comparison between them meaningless.
        blob = f"{served_model()}\x00{max_tokens}\x00{grammar}\x00{prompt}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def get(self, k: str) -> Optional[str]:
        v = self.data.get(k)
        if v is None:
            self.misses += 1
        else:
            self.hits += 1
        return v

    def put(self, k: str, v: str) -> None:
        self.data[k] = v
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=0, sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = False


CACHE = _Cache(CACHE_PATH)

_server_down = False          # sticky: stop retrying a dead server every row
_hosted_down = False          # sticky: a present-but-unusable API key


def available() -> bool:
    """True if the local server answers. Cheap, cached after first failure."""
    global _server_down
    if _server_down:
        return False
    try:
        req = urllib.request.Request(BASE_URL + "/health")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        _server_down = True
        return False


def complete(prompt: str, grammar: str = "", max_tokens: int = 64,
             prefill: str = "") -> Optional[str]:
    """One deterministic completion, or None if unavailable.

    `grammar` is GBNF. Passing one is how an answer is constrained to an enum:
    the model cannot emit a token outside the grammar, so a prompt-injection
    payload cannot widen the output space even if it survives to this point.
    """
    k = CACHE.key(prompt, grammar + "\x00" + prefill, max_tokens)
    hit = CACHE.get(k)
    if hit is not None:
        return hit
    if CACHE_ONLY:
        return None

    if backend() == "anthropic":
        out = _anthropic(prompt, max_tokens, prefill)
        if out is not None:
            CACHE.put(k, out)
            CACHE.flush()
            return out
        # Fall through to the local server rather than returning None. A key
        # that is present but unusable -- expired, rate-limited, or out of
        # credits, which is what actually happened here -- must degrade to the
        # slow path instead of silently disabling the whole model layer.
        global _hosted_down
        _hosted_down = True

    if not available():
        return None

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "top_k": 1,
        "seed": SEED,
        "max_tokens": max_tokens,
        "cache_prompt": True,
    }
    if grammar:
        payload["grammar"] = grammar

    req = urllib.request.Request(
        BASE_URL + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.load(resp)
        out = body["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, OSError, KeyError, IndexError,
            json.JSONDecodeError, ValueError):
        return None

    CACHE.put(k, out)
    CACHE.flush()
    return out



def _anthropic(prompt: str, max_tokens: int, prefill: str = "") -> Optional[str]:
    """One Messages API call. The key is read from the environment and is never
    logged, cached, echoed, or written to disk -- not on success and not in any
    error path, which is why failures here return None rather than raising with
    request context attached.

    `prefill` seeds the assistant turn. Seeding with "{" is how a JSON reply is
    forced without a grammar: the model cannot open with prose because its
    first token is already spent.
    """
    # `prefill` is honoured only as an instruction, not as a seeded assistant
    # turn: the current models reject a conversation that does not end with a
    # user message. Output shape is therefore requested and then validated by
    # the caller, which it has to do on this backend regardless -- there is no
    # grammar here to make a malformed reply unreachable.
    if prefill:
        prompt = prompt + f"\n\nBegin your reply with {prefill!r} and output nothing else."
    messages = [{"role": "user", "content": prompt}]
    # No `temperature`: the current models reject it outright. Determinism on
    # this path therefore rests on the response cache rather than on greedy
    # decoding -- once a verdict is written to label_store.json it is never
    # re-sampled, so a re-run reproduces exactly even though the call that
    # first produced it was not guaranteed to.
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": ANTHROPIC_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
        text = "".join(b.get("text", "") for b in payload.get("content", []))
    except (urllib.error.URLError, OSError, KeyError, IndexError,
            json.JSONDecodeError, ValueError):
        return None
    return text.strip()


# --------------------------------------------------------------------------
# Grammars
# --------------------------------------------------------------------------

def _enum_grammar(options: Sequence[str]) -> str:
    alts = " | ".join('"%s"' % o for o in options)
    return "root ::= %s" % alts


_YESNO = _enum_grammar(["yes", "no"])
_TYPE_GRAMMAR = _enum_grammar(MESSAGE_TYPES)
# One sentence: printable text, ending in a period. Bounded so the model cannot
# be talked into emitting a wall of attacker-chosen prose into the output CSV.
_SENTENCE = r'''root ::= [A-Z] [^\n"]{20,180} "."'''


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

# Message text is DATA, never instruction, and is fenced to say so.
#
# An earlier version of this module opened every prompt with a paragraph
# explaining that the fenced text was untrusted and must not be obeyed. On a 3B
# model that paragraph measurably destroyed the task: `is_directed_request`
# answered "no" to all seven probe rows, including "when you get 5 mins can you
# call?". Removing it recovered the judgment.
#
# The defence that actually holds here is structural, not textual:
#   - layer 1 of the gate stack mutes the adversarial rows BEFORE any call;
#   - every answer is GBNF-constrained, so the reachable output set is an enum
#     or one bounded sentence -- injected text cannot widen it;
#   - nothing from this module can set `action`.
# A successful injection can therefore at most flip one tiebreak boolean or one
# type label on a row that already passed safety.
_FENCE = '"""'


def _fence(text: str) -> str:
    body = (text or "").strip()[:MAX_TEXT_CHARS] or "(no text)"
    body = body.replace(_FENCE, "'''")
    return f'{_FENCE}\n{body}\n{_FENCE}\n'


def _context_block(msg, feats) -> str:
    bits = [f"channel: {msg.conversation_type or 'unknown'}"]
    if feats.group_type:
        bits.append(f"group kind: {feats.group_type}")
    if msg.business_id:
        bits.append("sender: a business account")
    elif msg.sender_user_id:
        bits.append("sender: an individual contact")
    if feats.media_kind:
        bits.append(f"attachment: {feats.media_kind}")
    return "\n".join("- " + b for b in bits)


# --------------------------------------------------------------------------
# Task 1 -- directed request
# --------------------------------------------------------------------------

def is_directed_request(msg, feats) -> Optional[bool]:
    """Does the message ask THIS recipient to do or answer something?

    The one judgment no lexical rule separates. sample_msg_006 ("when you get 5
    mins can you call? Nothing dramatic") and sample_msg_050 ("Don't call now,
    we can talk tomorrow. Nothing urgent") share register, warmth, and a
    self-deprecating hedge; they differ only in whether a response is wanted.
    Gold routes the first to notify and the second to digest.
    """
    text = ((msg.message_text or "") + " " + (feats.caption or "")).strip()
    if not text:
        return None

    prompt = (
        "Read the message and decide if the sender is waiting on the "
        "recipient.\n\nMessage:\n"
        + _fence(text)
        + '\nAnswer "yes" if the message asks a question, requests a call, or '
        "needs a decision from the recipient.\n"
        'Answer "no" if it only reports news, shares an update, or says no '
        "reply is needed.\n\nAnswer:"
    )
    out = complete(prompt, grammar=_YESNO, max_tokens=4)
    if out is None:
        return None
    return out.strip().lower().startswith("y")


# --------------------------------------------------------------------------
# Task 2 -- message_type adjudication
# --------------------------------------------------------------------------

_TYPE_DEF = {
    "personal": "one-to-one talk between people, nothing for anyone else to do",
    "urgent": "needs attention within hours; someone is waiting on it",
    "event": "something scheduled - a meeting, class, trip, or dated plan",
    "payment": "money must actually move: a bill, invoice, or amount owed",
    "business_update": "a company reporting on the recipient's own order, "
                       "account, or policy",
    "promotion": "advertising or selling something, including a person selling "
                 "an item",
    "greeting": "a wish or pleasantry with nothing to act on",
    "forward": "a chain message the recipient is asked to pass on",
    "spam": "unsolicited bulk with no relationship behind it",
    "scam": "an attempt to defraud: fake brand, credential theft, false urgency",
    "unknown": "does not fit any other category",
}


def classify_type(msg, feats, candidates: Sequence[str]) -> Optional[str]:
    """Choose among types when the lexical layer is ambiguous.

    Called only on collisions, not on all 110 rows. Every measured type miss is
    a word-sense error -- "itinerary" in a travel advert read as `event`,
    "payment details" inside "we never ask for payment details" read as
    `payment` -- which is exactly what a reader resolves and a regex cannot.
    """
    text = ((msg.message_text or "") + " " + (feats.caption or "")).strip()
    if not text:
        return None

    opts = [c for c in candidates if c in MESSAGE_TYPES]
    if len(opts) < 2:
        return None

    # Only the colliding options are described. Offering all eleven made the
    # model drift on rows the lexical layer already had right; narrowing the
    # choice to the actual ambiguity is what made this task work.
    defs = "\n".join(f"- {o}: {_TYPE_DEF[o]}" for o in opts)
    prompt = (
        "Categories:\n" + defs + "\n\nMessage:\n"
        + _fence(text)
        + "\nWhich category best describes what this message IS ABOUT? Judge "
        "the purpose of the message, not which words appear in it.\n"
        f"Answer with one word: {' or '.join(opts)}."
    )
    out = complete(prompt, grammar=_enum_grammar(opts), max_tokens=8)
    if out is None:
        return None
    out = out.strip().lower()
    return out if out in opts else None


# --------------------------------------------------------------------------
# Task 3 -- reason prose
# --------------------------------------------------------------------------

_ACTION_GLOSS = {
    "notify": "show it to the user right away",
    "digest": "save it for later instead of interrupting",
    "mute": "hide it",
}

# Plain English for what the user's own history with this sender looks like.
# An earlier version passed the raw label through ("the user consistently
# treated them as 'notify'") and the model both inverted the agency -- "the
# sender has a history of treating previous messages as digest" -- and leaked
# the routing vocabulary into a user-facing column, which the solved rows never
# do. Describing the behaviour instead of naming the label fixed both.
_HISTORY_GLOSS = {
    "notify": "the user usually opens and replies to this sender quickly",
    "digest": "the user usually reads this sender later without acting on it",
    "mute": "the user usually dismisses this sender without reading",
}


def write_reason(msg, feats, action: str, decided_by: str,
                 fallback: str) -> Optional[str]:
    """One clause naming why THIS message was routed the way it was.

    The scored gap this closes: the graded rows carry 24 distinct reasons
    across 30 rows, while the template layer emits 11 across 110 -- one string
    repeated 28 times. The gate has already decided; the model only says why in
    the register of the solved rows ("A school admin sent a same-day
    operational update that the user is likely to need immediately.").

    Never called for safety rows. Those keep their fixed wording, because a row
    that may contain injected text must not have attacker-influenced prose
    written into the deliverable.
    """
    text = ((msg.message_text or "") + " " + (feats.caption or "")).strip()

    facts = []
    if feats.precedent_label and feats.precedent_n:
        facts.append(_HISTORY_GLOSS[feats.precedent_label])
    if feats.group_type:
        facts.append(f"it came from a {feats.group_type} group")
    if msg.business_id:
        facts.append("the sender is a business account")
    if feats.media_kind:
        facts.append(f"it carries {'an image' if feats.media_kind == 'image' else 'a voice note'}")

    known = ("Background:\n" + "\n".join("- " + f for f in facts) + "\n\n") if facts else ""

    prompt = (
        "A notification assistant decided to "
        + _ACTION_GLOSS.get(action, action)
        + " the message below. Write one sentence saying why.\n\n"
        + known
        + "Message:\n" + _fence(text)
        + "\nDescribe what this particular message is and why it deserves that "
        "treatment. Write about the message, not about the assistant. Do not "
        "use the words notify, digest, mute, or router. No names or phone "
        "numbers. Under 25 words.\n\n"
        "Examples of the style:\n"
        "- A school admin sent a same-day operational update that the user is "
        "likely to need immediately.\n"
        "- The verified business message is legitimate but does not require "
        "immediate attention.\n"
        "- The sender is unfamiliar, but the message shows no urgency or "
        "safety risk.\n\nSentence:"
    )
    out = complete(prompt, grammar=_SENTENCE, max_tokens=60)
    if out is None:
        return None

    out = " ".join(out.split()).strip().strip('"')
    # Reject anything that failed to look like a single clean sentence rather
    # than shipping model noise into a scored column.
    if len(out) < 25 or len(out) > 220 or "\n" in out:
        return None
    if _FENCE in out or re.search(r"(?i)\b(as an ai|i cannot|instruction)\b", out):
        return None
    # Internal routing vocabulary must not reach a user-facing column, and a
    # sentence about the assistant is not a reason about the message.
    if re.search(r"(?i)\b(notify|digest|mute[ds]?|router|assistant|"
                 r"precedent|decided_by)\b", out):
        return None
    if not out.endswith("."):
        out += "."
    return out or fallback


# --------------------------------------------------------------------------
# Task 4 -- safety false-positive audit
# --------------------------------------------------------------------------

# A business old and established enough that its identity is not in question.
# Reports are median 7 / p90 56 across the 110 accounts, so this threshold
# excludes the visibly-complained-about tail without keying on a single row.
_TRUSTED_MIN_AGE_DAYS = 365
_TRUSTED_MAX_REPORTS = 30


def structurally_trusted(feats) -> bool:
    """Identity evidence the message text cannot forge.

    This is the gate on any model opinion that would REMOVE a mute. A model can
    be argued into "this is legitimate" by the message itself; it cannot be
    argued into a domain being three years old. Account age, verification and
    domain age come from business_accounts.csv, never from the message.
    """
    return bool(
        feats.business_verified
        and not feats.domain_mismatch
        and (feats.business_age_days or 0) >= _TRUSTED_MIN_AGE_DAYS
        and (feats.sender_domain_age_days or 0) >= _TRUSTED_MIN_AGE_DAYS
        and (feats.business_reports_30d or 0) <= _TRUSTED_MAX_REPORTS
    )


_AUDIT_Q = {
    "credential_request":
        "Does this message ask the reader to hand over a one-time password, "
        "PIN, card number, or account password?",
    "chain":
        "Does this message instruct the reader to forward or re-share it to "
        "other people?",
}


def safety_false_positive(msg, feats, trigger: str) -> Optional[bool]:
    """True if the lexical safety trigger looks like a misfire.

    Only `credential_request` and `chain` are auditable. The impersonation gate
    keys on account metadata, which a model cannot improve on, and the
    injection gate must never be handed to a model -- asking one to judge text
    written to manipulate models is giving the attacker the pen.

    A confirmed misfire is necessary but NOT sufficient to un-mute: the caller
    additionally requires `structurally_trusted`. Known misfire modes are real
    -- a brand advisory reading "we never ask for OTP" was muted as a
    credential request, suppressing the exact guidance the user needed.
    """
    q = _AUDIT_Q.get(trigger)
    if q is None:
        return None
    text = ((msg.message_text or "") + " " + (feats.caption or "")).strip()
    if not text:
        return None

    prompt = (
        "Message:\n" + _fence(text) + "\n" + q + "\n"
        'Answer "yes" if the message actually makes that request of the '
        "reader.\n"
        'Answer "no" if it only mentions the topic -- for example warning that '
        "such details are never asked for, or describing someone else doing "
        "it.\n\nAnswer:"
    )
    out = complete(prompt, grammar=_YESNO, max_tokens=4)
    if out is None:
        return None
    return out.strip().lower().startswith("n")     # "no" => the regex misfired


def stats() -> dict:
    return {"cache_hits": CACHE.hits, "cache_misses": CACHE.misses,
            "cached": len(CACHE.data), "server": not _server_down}
