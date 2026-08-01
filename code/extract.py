"""Label extraction: message text -> the labels defined in labels.json.

The call shape depends on the backend, and that turned out to matter more than
model size. Qwen2.5 3B and 7B BOTH collapse when asked for all eleven labels in
one reply -- the 7B returned no labels at all for a bus-schedule change that it
labelled correctly when the same questions were asked one at a time. So the
local path spends eleven cheap calls per message, and a hosted frontier model
takes the batch in one.

Reproducibility is the point of this module:

  * answers are written to `label_store.json`, keyed by message_id and readable
    as plain JSON -- you can diff it, review it, and correct it by hand;
  * the store records the labels.json `version`, and a version bump invalidates
    it rather than silently mixing answers to different questions;
  * with the store present the router runs to completion with no model, no
    network, and no API key, producing byte-identical output.

The store is the artifact. The model is just how it gets filled in the first
time.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import llm

_HERE = os.path.dirname(os.path.abspath(__file__))
LABELS_PATH = os.path.join(_HERE, "labels.json")
STORE_PATH = os.environ.get("LABEL_STORE", os.path.join(_HERE, "label_store.json"))

with open(LABELS_PATH, "r", encoding="utf-8") as _fh:
    SPEC = json.load(_fh)

SEMANTIC = SPEC["semantic"]
SEMANTIC_IDS = [d["id"] for d in SEMANTIC]
SPEC_VERSION = SPEC["version"]


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

class LabelStore:
    """message_id -> {label_id: bool}, persisted as readable JSON."""

    def __init__(self, path: str = STORE_PATH):
        self.path = path
        self.rows: Dict[str, Dict[str, bool]] = {}
        self.dirty = False
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    blob = json.load(fh)
                if blob.get("spec_version") == SPEC_VERSION:
                    self.rows = blob.get("labels", {})
            except (json.JSONDecodeError, OSError):
                pass

    def get(self, message_id: str) -> Optional[Dict[str, bool]]:
        row = self.rows.get(message_id)
        # A row written against a shorter label list is incomplete, not usable.
        if row is not None and all(k in row for k in SEMANTIC_IDS):
            return row
        return None

    def put(self, message_id: str, labels: Dict[str, bool]) -> None:
        self.rows[message_id] = labels
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"spec_version": SPEC_VERSION, "labels": self.rows},
                      fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = False


STORE = LabelStore()


# --------------------------------------------------------------------------
# Prompt and grammar
# --------------------------------------------------------------------------

def _grammar() -> str:
    """GBNF pinning the reply to one verdict per label, in spec order.

    The question is re-stated in the forced output immediately before its own
    answer. That placement is not cosmetic: with all eleven questions stated
    once at the top and a bare JSON object at the end, a 3B model stopped
    reading them and returned near-uniform `false` with one arbitrary `true`
    (sample_msg_001, an urgent group update, came back as `is_greeting` alone).
    Re-stating each question at its decision point fixed it.

    The model still cannot omit a label, invent one, reorder them, or emit
    anything but yes/no -- a malformed reply is unreachable rather than
    something to validate afterwards.
    """
    parts = []
    for i, d in enumerate(SEMANTIC):
        q = d["question"].replace('"', "'")
        parts.append(f'"{"" if i == 0 else chr(92) + "n"}{q} " verdict')
    return ("root ::= " + " ".join(parts) + "\n"
            'verdict ::= "yes" | "no"\n')


_VERDICT_ORDER = SEMANTIC_IDS


_GUIDE = "\n".join(
    f'- {d["question"]} Say yes when {d["yes_when"]}; no when {d["no_when"]}.'
    for d in SEMANTIC
)

_PREFIX = (
    "Read the message, then answer each question about it with yes or no.\n\n"
    "Guidance:\n" + _GUIDE + "\n\nMessage:\n"
)


def extract(msg, feats, allow_model: bool = True) -> Optional[Dict[str, bool]]:
    """Semantic labels for one message. None if unavailable and uncached."""
    cached = STORE.get(msg.message_id)
    if cached is not None:
        return cached
    if not allow_model:
        return None

    text = ((msg.message_text or "") + " " + (feats.caption or "")).strip()
    if not text:
        # No text is not the same as false labels: an empty voice note is
        # unjudged, and the rules must fall through to structural evidence
        # rather than act on eleven confident-looking negatives.
        return None

    # Batched extraction, deliberately, despite the coupling it introduces.
    #
    # Asking all eleven questions in one prompt means editing one definition
    # perturbs the others -- measured: changing `requests_response` moved
    # `is_scheduled_event` on unrelated rows. Isolating each label into its own
    # call removes that, and was tried: action fell 96.7% -> 86.7% and type
    # 83.3% -> 80.0%. Seeing the questions together is doing real work, because
    # judging urgency alongside "is a reply wanted" calibrates both.
    #
    # So the coupling is a known, documented cost rather than a bug: labels.json
    # is the specification, but a definition's answers can shift when a
    # neighbouring definition changes, and any edit needs a full re-extract and
    # re-score rather than a local check.
    if llm.backend() == "anthropic":
        return _extract_batched(msg, text)

    labels = {}
    for spec in SEMANTIC:
        verdict = _ask_one(spec, text)
        if verdict is None:
            # One transient failure voided all eleven labels in an earlier
            # version, which is why an isolated run produced 87 usable rows
            # against the batched path's 102. Retry once, then give up on that
            # label alone rather than on the message.
            verdict = _ask_one(spec, text)
        if verdict is not None:
            labels[spec["id"]] = verdict
    if len(labels) < len(SEMANTIC_IDS):
        return None
    STORE.put(msg.message_id, labels)
    STORE.flush()
    return labels


def _ask_one(spec: dict, text: str) -> Optional[bool]:
    """One label, one call. The reliable shape for a small local model."""
    prompt = (
        "Read the message and answer the question about it.\n\nMessage:\n"
        + llm._fence(text) + "\n" + spec["question"] + "\n"
        f'Answer "yes" when {spec["yes_when"]}.\n'
        f'Answer "no" when {spec["no_when"]}.\n\nAnswer:'
    )
    out = llm.complete(prompt, grammar='root ::= "yes" | "no"', max_tokens=4)
    if out is None:
        return None
    return out.strip().lower().startswith("y")


def _extract_batched(msg, text: str) -> Optional[Dict[str, bool]]:
    """All labels in one call, as JSON. Used when the backend can handle it."""
    keys = ", ".join(f'"{k}"' for k in SEMANTIC_IDS)
    prompt = (
        _PREFIX + llm._fence(text)
        + f"\nReply with one JSON object with exactly these keys: {keys}. "
          "Each value must be true or false. No other text."
    )
    raw = llm.complete(prompt, max_tokens=48 * len(SEMANTIC_IDS), prefill="{")
    if raw is None:
        return None
    # Without a grammar the reply may arrive wrapped in prose or a code fence,
    # so take the outermost brace span rather than trusting the whole string.
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not all(isinstance(parsed.get(k), bool) for k in SEMANTIC_IDS):
        return None
    labels = {k: bool(parsed[k]) for k in SEMANTIC_IDS}
    STORE.put(msg.message_id, labels)
    STORE.flush()
    return labels


def structural(msg, feats) -> Dict[str, Any]:
    """Labels read straight off the dataset. No judgement, no model."""
    return {
        "is_business": bool(msg.business_id),
        "conversation_type": msg.conversation_type or "",
        "media_kind": feats.media_kind or "",
        "group_muted": bool(feats.group_muted),
        "group_type": feats.group_type or "",
        "user_opted_out": bool(feats.user_opted_out),
        "sender_verified": bool(feats.business_verified),
        "sender_domain_mismatch": bool(feats.domain_mismatch),
        "sender_account_age_days": feats.business_age_days,
        "sender_domain_age_days": feats.sender_domain_age_days,
        "sender_reports_30d": feats.business_reports_30d,
        "sender_structurally_trusted": llm.structurally_trusted(feats),
        "precedent_label": feats.precedent_label,
        "precedent_unanimous": bool(feats.precedent_unanimous),
        "precedent_n": feats.precedent_n,
        "media_precedent_label": feats.media_precedent_label,

        # Lexical detectors. These are structural on purpose: they are
        # deterministic functions of the text, and unlike the semantic labels
        # they cannot be talked out of firing. The policy combines each with
        # its model-judged twin so that either one is enough to suppress -- a
        # model may add a mute, never remove one.
        "text_matches_injection_pattern": bool(feats.has_injection_attempt),
        "text_matches_credential_pattern": bool(feats.has_credential_request),
        "text_matches_chain_pattern": bool(feats.has_chain_instruction),
    }


def labels_for(msg, feats, allow_model: bool = True) -> Dict[str, Any]:
    """The complete label view a rule is evaluated against.

    Unextracted semantic labels are supplied as None rather than omitted, so a
    rule referencing one simply fails to match instead of raising. That makes
    an absent model degrade toward the default rule -- digest -- rather than
    toward a confident wrong answer.
    """
    view: Dict[str, Any] = dict.fromkeys(SEMANTIC_IDS)
    sem = extract(msg, feats, allow_model=allow_model)
    if sem:
        view.update(sem)
    view.update(structural(msg, feats))
    return view


def coverage() -> dict:
    return {"spec_version": SPEC_VERSION, "stored": len(STORE.rows),
            "labels_per_row": len(SEMANTIC_IDS)}
