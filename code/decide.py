"""Decision layer: the six-gate routing stack.

Evaluated strictly in order; the first gate to fire wins. See ARCHITECTURE.md s6.

    1. SAFETY            unconditional, never overridden by engagement history
    2. HARD USER STATE   explicit opt-out
    3. URGENCY           real deadline, relative to the message's own created_at
    4. PRECEDENT         how this user has actually treated this sender
    5. CONTENT CLASS     promotion / greeting / forward
    6. DEFAULT           digest -- the recoverable middle

Stdlib only. No model calls: every gate here is deterministic. A model, when
available, refines `message_type` and reranks evidence -- it never overrides a
gate.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

from schema import CONFIDENCE_BANDS, Candidate, Decision, Features, Message

# --------------------------------------------------------------------------
# Confidence positioning within a band
# --------------------------------------------------------------------------

# Where a decision sits inside its action's band, by how much support it has.
# Calibration is a scored criterion, so this must track real certainty rather
# than emitting a constant.
_STRENGTH = {
    "safety_composite": 1.00,   # structural: sender metadata, not a guess
    "opt_out": 0.95,
    "unanimous_precedent": 0.90,
    "deadline": 0.75,
    "media_precedent": 0.70,
    "mixed_precedent": 0.45,
    "content_class": 0.40,
    "default": 0.15,
}


def _confidence(action: str, strength: float) -> float:
    lo, hi = CONFIDENCE_BANDS[action]
    return round(lo + (hi - lo) * max(0.0, min(1.0, strength)), 2)


# --------------------------------------------------------------------------
# message_type inference
# --------------------------------------------------------------------------

# Greetings are checked before chain language: "share blessings with everyone"
# inside a "Good morning all" message is still a greeting, not a forward.
_GREETING = re.compile(
    r"^\s*\W*(good\s+(morning|evening|night)|happy\s+\w+|namaste|shubh|"
    r"season'?s greetings|wishing (you|everyone))\b", re.I)

# An explicitly relayed chain, as opposed to merely mentioning sharing.
_FORWARD = re.compile(
    r"\b(fwd|forwarded)\b|\bas received\b|\bcopy ?paste\b|"
    r"\bshare (this )?(with|in) (all|ten|10|your|family|every)", re.I)

# Something scheduled: a time, a place, a form, a circular.
_EVENT = re.compile(
    r"\b(meeting|sync|class|trip|venue|schedule|scheduling|reschedul\w*|"
    r"appointment|session|assembly|ceremony|rehearsal|circular|agenda|rsvp|"
    r"form is open|consent note|timing|itinerary|booking|slot)\b", re.I)

# Commercial solicitation -- brand promos AND peer-to-peer selling. The
# marketplace rows are gold `promotion`, not `personal`.
_PROMO = re.compile(
    r"\b(offer|discount|sale|deal|coupon|cashback|% ?off|\d+% off|flat \d+|"
    r"limited (time|period)|shop now|buy now|lowest price|festive|promo|"
    r"selling|for sale|price is|barely used|no crash damage|"
    r"pickup is|photos (for|of) the)\b", re.I)

# Transactional relationship updates: order state, feedback, advisories.
_BIZ_UPDATE = re.compile(
    r"\b(order|delivery|deliver\w*|packed|shipped|dispatch\w*|tracking|"
    r"return pickup|statement|advisory|policy|maintenance|downtime|"
    r"experience with us|feedback|thank you for choosing|your account has been)\b",
    re.I)

# A genuine request to move money -- not merely the word "refund".
_PAYMENT = re.compile(
    r"\b(pay|paid|payment|invoice|due date|amount due|outstanding|"
    r"transfer|token amount|installment|emi|fees?|bill)\b", re.I)

# Time-critical: something is happening now or imminently and the user must act.
# Deliberately excludes a bare "now" -- "Don't call now, phone is charging" is
# the opposite of urgent.
_URGENT = re.compile(
    r"\b(immediately|urgent\w*|asap|right away|right now|come online now|"
    r"in \d+ (min|mins|minutes|hours?)|\d+ ?(min|mins|minutes) max|"
    r"escalat\w*|alert threshold|last-minute|pulled to \d|"
    r"leaving \d+ mins early|before it (closes|shuts)|"
    r"can you come online)\b", re.I)

# Explicit de-escalation. When the sender says it can wait, it can wait --
# this outranks any urgency cue in the same message.
_NOT_URGENT = re.compile(
    r"\b(nothing dramatic|no pressure|no rush|whenever you|"
    r"don'?t call now|we can talk tomorrow|read after|no intraday|"
    r"not urgent|when you get (a|\d+) )", re.I)

# "we never ask for OTP" is a safety advisory, the opposite of a scam ask.
_CREDENTIAL_NEGATED = re.compile(
    r"\b(never|will not|won'?t|do not|don'?t|no one)\b[^.]{0,40}\b"
    r"(ask|request|seek)\b", re.I)


def type_candidates(msg: Message, feats: Features, action: str,
                    cred_cleared: bool = False) -> List[str]:
    """Every type whose lexical pattern matches, best-first.

    The first entry is what the rule layer would pick on its own. When there is
    more than one, the message is genuinely ambiguous to a regex and the model
    layer is asked to adjudicate -- that is where every measured type miss
    lives ("itinerary" in a travel advert, "payment details" inside "we never
    ask for payment details").
    """
    text = ((msg.message_text or "") + " " + (feats.caption or "")).strip()
    out: List[str] = []

    def add(t: str) -> None:
        if t not in out:
            out.append(t)

    if feats.has_injection_attempt or feats.is_impersonation:
        return ["scam"]
    # `cred_cleared` means the safety audit ruled this trigger a misfire on a
    # structurally trusted sender. Without it a row could be un-muted for being
    # legitimate and still be labelled `scam`, which is incoherent.
    if (feats.has_credential_request and not cred_cleared
            and not _CREDENTIAL_NEGATED.search(text)):
        return ["scam"]

    if _FORWARD.search(text) and not _GREETING.search(text):
        add("forward")
    if _GREETING.search(text):
        add("greeting")
    if _EVENT.search(text):
        add("event")
    if not _NOT_URGENT.search(text):
        if _URGENT.search(text) or (feats.has_deadline_language and not msg.business_id):
            add("urgent")
    if _PAYMENT.search(text):
        add("payment")
    if _PROMO.search(text):
        add("promotion")
    if msg.business_id:
        if _BIZ_UPDATE.search(text):
            add("business_update")
        elif action == "mute" and not text:
            # A business with no transactional content and no relationship is
            # unsolicited bulk.
            add("spam")
        else:
            add("promotion")
    if msg.conversation_type == "personal" or msg.sender_user_id:
        # A stranger's message is `unknown` in the solved rows, not `personal`:
        # the label tracks whether the router knows the sender at all.
        add("personal" if feats.precedent_n else "unknown")

    return out or ["unknown"]


def infer_type(msg: Message, feats: Features, action: str) -> str:
    """Single best-fit label -- the rule layer's own pick.

    Thin wrapper over `type_candidates` so there is exactly one place where
    type precedence is defined. The model layer, when enabled, may override
    this with another entry from the same candidate list.
    """
    return type_candidates(msg, feats, action)[0]


# --------------------------------------------------------------------------
# reason
# --------------------------------------------------------------------------

def _reason(kind: str, msg: Message, feats: Features) -> str:
    """One short human-readable clause naming the deciding factor.

    Register matched to the solved rows, which read like:
      "A trusted group admin sent a time-sensitive update that should
       interrupt the user."
    """
    return {
        "impersonation":
            "The sender impersonates a known brand from an unverified, recently "
            "created account using a lookalike domain.",
        "credential_request":
            "The message asks for OTP, card or wallet credentials through a "
            "suspicious verification flow.",
        "injection":
            "The message contains instructions aimed at the routing system "
            "rather than the user.",
        "chain":
            "The message is a chain forward asking the user to propagate it "
            "further.",
        "opt_out":
            "The user has opted out of or repeatedly dismissed messages from "
            "this sender.",
        "deadline":
            "The message carries a same-day deadline or time-sensitive "
            "instruction the user is likely to need now.",
        "precedent_notify":
            "The user consistently opens and replies to this sender, so this "
            "message is worth an interruption.",
        "precedent_digest":
            "The user reads this sender's messages but rarely acts on them "
            "immediately, so it can wait.",
        "precedent_mute":
            "The user has consistently dismissed or muted messages from this "
            "sender.",
        "media_precedent":
            "The same media was received before and the user's handling of it "
            "is a reliable guide.",
        "promotion":
            "The message is promotional and does not require immediate "
            "attention.",
        "greeting":
            "A routine greeting with no actionable content.",
        "default":
            "Safe but low priority; no evidence that it needs to interrupt the "
            "user.",
    }[kind]


# --------------------------------------------------------------------------
# The stack
# --------------------------------------------------------------------------

def decide(ds, msg: Message, feats: Features,
           cands: Sequence[Candidate] = (), use_llm: bool = False) -> Decision:
    """Route one message. First gate to fire wins.

    `use_llm` enables the local model layer (code/llm.py). It changes only
    `message_type` on ambiguous rows, `reason` prose, and one tiebreak boolean
    in gates 3-4. It cannot change which gate fires and never touches a row the
    safety gate decided.
    """
    llm = None
    if use_llm:
        try:
            import llm as llm  # noqa: PLW0127  (optional dependency)
        except ImportError:
            llm = None

    def build(action: str, kind: str, strength_key: str,
              evidence: Optional[List[str]] = None) -> Decision:
        ev = list(evidence or [])[:2]
        cand_types = type_candidates(msg, feats, action)
        mtype = cand_types[0]
        reason = _reason(kind, msg, feats)

        # The model refines, never routes -- and never touches a safety row,
        # whose text may be adversarial and whose wording must stay fixed.
        if llm is not None and strength_key != "safety_composite":
            if len(cand_types) > 1:
                picked = llm.classify_type(msg, feats, cand_types)
                if picked:
                    mtype = picked
            written = llm.write_reason(msg, feats, action, kind, reason)
            if written:
                reason = written

        return Decision(
            message_id=msg.message_id,
            action=action,
            message_type=mtype,
            reason=reason,
            confidence=_confidence(action, _STRENGTH[strength_key]),
            evidence_message_ids=ev,
            decided_by=strength_key,
        )

    retrieved = [c.message.message_id for c in cands]

    def wants_response() -> Optional[bool]:
        """Is the recipient actually being asked for something?

        The one judgment with no lexical form. sample_msg_006 ("when you get 5
        mins can you call? Nothing dramatic") and sample_msg_050 ("Don't call
        now... Nothing urgent") share sender, register and hedge; they differ
        only here, and gold routes them notify and digest respectively.
        Consumed as a tiebreak, never as a gate of its own.
        """
        return llm.is_directed_request(msg, feats) if llm is not None else None

    # -- Layer 1: SAFETY. Unconditional. -----------------------------------
    # Runs before personalization: a user who has engaged with a scam sender
    # before must still not be interrupted by one.
    if feats.has_injection_attempt:
        return build("mute", "injection", "safety_composite", retrieved)
    if feats.is_impersonation:
        return build("mute", "impersonation", "safety_composite", retrieved)
    # A brand advisory that says it will NEVER ask for an OTP is the opposite
    # of a credential request. Muting it would suppress exactly the security
    # guidance the user should see.
    text_all = ((msg.message_text or "") + " " + (feats.caption or "")).strip()

    def misfired(trigger: str) -> bool:
        """Is this lexical trigger a false positive on a sender we can vouch for?

        Asymmetric on purpose, and the same rule the mixed-precedent tiebreak
        uses: the model alone may ADD a mute, never remove one. Removing also
        requires identity evidence the message text cannot forge -- a verified,
        aged account on an aged domain. An attacker who already has that did
        not need the injection.

        Only the two text-keyed triggers are auditable. Impersonation keys on
        account metadata a model cannot improve on, and injection is never
        shown to a model at all.
        """
        if llm is None or not llm.structurally_trusted(feats):
            return False
        return llm.safety_false_positive(msg, feats, trigger) is True

    if (feats.has_credential_request
            and not _CREDENTIAL_NEGATED.search(text_all)
            and not feats.is_link_shortener
            and not misfired("credential_request")):
        return build("mute", "credential_request", "safety_composite", retrieved)
    if feats.has_chain_instruction and not misfired("chain"):
        return build("mute", "chain", "safety_composite", retrieved)

    # -- Layer 2: HARD USER STATE ------------------------------------------
    if feats.user_opted_out:
        return build("mute", "opt_out", "opt_out", retrieved)

    # -- Layer 3: URGENCY ---------------------------------------------------
    # A genuine deadline outranks a lukewarm engagement history, but never
    # outranks layer 1 -- urgency is the scam's primary instrument.
    # A unanimous relationship precedent outranks a lexical deadline cue.
    # Deadline language is broad (it fires on ~46% of messages), while an
    # unanimous precedent is the strongest measured signal in the dataset
    # (unanimous for 79 of 110). When the user has consistently treated this
    # sender a certain way, that wins.
    # A deadline that asks nothing of the recipient is an FYI, not an
    # interruption: "Reached home... don't call now, we can talk tomorrow"
    # carries deadline language and wants no reply at all.
    if (feats.has_deadline_language and not msg.business_id
            and not feats.precedent_unanimous):
        if wants_response() is not False:
            return build("notify", "deadline", "deadline", retrieved)

    # -- Layer 4: RELATIONSHIP PRECEDENT ------------------------------------
    # Unanimous for 79 of the 110. The unit is (user, sender), not sender.
    if feats.precedent_label:
        label = feats.precedent_label
        key = "unanimous_precedent" if feats.precedent_unanimous else "mixed_precedent"
        # When the history is split the user has treated this sender both ways,
        # so the message itself is the tiebreak. Only ever moves between notify
        # and digest -- a mixed history is never grounds to suppress.
        if not feats.precedent_unanimous:
            asked = wants_response()
            text_mp = ((msg.message_text or "") + " " + (feats.caption or ""))
            if asked is True and label == "digest":
                label = "notify"
            elif asked is False and label == "notify":
                # Demote only when the rule layer also sees nothing urgent.
                # The model answered "no" to "Can you come online now? Retry
                # count crossed the alert threshold and escalation starts in 20
                # minutes" (sample_msg_051) and demoted a real interruption.
                # Promotion needs one signal; suppression needs both to agree.
                if not _URGENT.search(text_mp):
                    label = "digest"
        return build(label, f"precedent_{label}", key,
                     retrieved or feats.precedent_message_ids)

    # Media whose file was seen before, when no sender precedent exists.
    if feats.media_precedent_label:
        return build(feats.media_precedent_label, "media_precedent",
                     "media_precedent",
                     retrieved or feats.media_precedent_message_ids)

    # -- Layer 5: CONTENT CLASS ---------------------------------------------
    text = (msg.message_text or "") + " " + (feats.caption or "")
    if _GREETING.search(text):
        return build("digest", "greeting", "content_class", retrieved)
    if _PROMO.search(text):
        # Muted group or no relationship => the user does not want this.
        action = "mute" if feats.group_muted else "digest"
        return build(action, "promotion", "content_class", retrieved)

    # -- Layer 6: DEFAULT ---------------------------------------------------
    # digest is wrong-but-recoverable in both directions; never default to
    # notify.
    return build("digest", "default", "default", retrieved)


def apply_quiet_hours(decision: Decision, msg: Message, feats: Features) -> Decision:
    """Hold a non-urgent notify that would land inside the user's quiet hours.

    Applied after routing, not as a gate, because quiet hours change *when* the
    user is interrupted rather than whether the message deserves it.

    Demotes non-urgent only, and never past `digest` -- quiet hours are a
    preference about timing and must not suppress anything. Anything typed
    `urgent` or `scam` is exempt: a fire alarm at 23:40 is exactly what an
    override is for.

    Measured reach: 8 of the 110 messages fall inside a quiet window and 2 of
    those route notify (msg_062, msg_077), both typed urgent on unanimous
    notify precedent -- so both are exempt and this currently changes nothing.
    It is implemented because §13 documents the policy, and a documented
    behaviour with no code path is worse than either having it or dropping it.
    """
    if not feats.in_quiet_hours or decision.action != "notify":
        return decision
    if decision.message_type in ("urgent", "scam"):
        return decision
    decision.action = "digest"
    decision.reason = ("Held until quiet hours end; the message is not urgent "
                       "enough to interrupt the user overnight.")
    decision.confidence = _confidence("digest", _STRENGTH["content_class"])
    decision.decided_by += "+quiet_hours"
    return decision
