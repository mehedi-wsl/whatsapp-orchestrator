"""Routing: apply rules.json to one message.

Two stages, split on whether a model is allowed to have an opinion.

  Stage 1 -- GUARDED RULES, evaluated in code.
      The safety rules and the opt-out rule key on structural facts: account
      age, domain mismatch, verification, an explicit opt-out. None of them
      need to read the message, so none of them are shown to a model. This is
      deliberate. A rule that can suppress a scam must not be reachable by
      argument from the scam itself.

  Stage 2 -- JUDGED RULES, selected by the model.
      Everything below the safety block asks a question about what the message
      IS -- does it state a deadline, does it want a reply, is it advertising.
      The model is given the remaining rules in priority order, with the facts
      it cannot read off the text, and asked which one applies.

The model chooses a rule. It does not choose an action: the selected rule's
`then` clause decides that, resolved against structural facts. So the policy
file remains the only place routing is defined, every row records the rule id
that produced it, and a model failure shows up as citing the wrong rule rather
than as an unexplainable output.

Reproducibility: selections are cached per message_id in `route_store.json`
alongside the rule text version they were made against. With the store present
the router runs with no model, no network and no API key.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import engine
import llm
from schema import ACTIONS, MESSAGE_TYPES

_HERE = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.environ.get("ROUTE_STORE", os.path.join(_HERE, "route_store.json"))

# Rules the model is never shown. Everything else in the table is fair game.
GUARDED = ("S1_injection", "S2_impersonation", "S3_credential_request",
           "S4_chain_forward", "U1_opted_out")

GUARDED_RULES = [r for r in engine.ACTION_RULES if r["id"] in GUARDED]
JUDGED_RULES = [r for r in engine.ACTION_RULES if r["id"] not in GUARDED]
JUDGED_IDS = [r["id"] for r in JUDGED_RULES]


class _Store:
    """message_id -> {"rule_id":..., "message_type":...}, readable JSON."""

    def __init__(self, path: str):
        self.path, self.rows, self.dirty = path, {}, False
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    blob = json.load(fh)
                if blob.get("policy_version") == engine.POLICY_VERSION:
                    self.rows = blob.get("routes", {})
            except (json.JSONDecodeError, OSError):
                pass

    def get(self, mid: str) -> Optional[dict]:
        return self.rows.get(mid)

    def put(self, mid: str, row: dict) -> None:
        self.rows[mid] = row
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"policy_version": engine.POLICY_VERSION,
                       "routes": self.rows}, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = False


STORE = _Store(STORE_PATH)


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

def _rule_block() -> str:
    """The judged rules, in priority order, as the model sees them.

    Each rule's `why` is included: the rationale is what lets the model apply
    the rule to a message the condition list does not literally describe, which
    is the entire reason for asking a model instead of matching strings.
    """
    out = []
    for i, r in enumerate(JUDGED_RULES, 1):
        out.append(f'{i}. {r["id"]}\n   {r["why"]}')
    return "\n\n".join(out)


_RULE_TEXT = _rule_block()


def _facts_block(labels: Dict[str, Any]) -> str:
    """Structural facts the model cannot read off the message text."""
    bits = []
    pl, pn = labels.get("precedent_label"), labels.get("precedent_n") or 0
    if pl and pn:
        agree = "every time" if labels.get("precedent_unanimous") else "not consistently"
        gloss = {"notify": "opened and replied to quickly",
                 "digest": "read later without acting",
                 "mute": "dismissed without reading"}[pl]
        bits.append(f"Across {pn} earlier messages from this sender the user "
                    f"{gloss} -- {agree}. (precedent_label = {pl}, "
                    f"precedent_unanimous = {str(labels.get('precedent_unanimous')).lower()})")
    else:
        bits.append("The user has no earlier history with this sender. "
                    "(precedent_label = null)")
    bits.append("The sender is a business account."
                if labels.get("is_business") else
                "The sender is an individual, not a business.")
    if labels.get("group_muted"):
        bits.append("The user has muted the group this arrived in. (group_muted = true)")
    if labels.get("media_precedent_label"):
        bits.append("The same attachment was received before. "
                    f"(media_precedent_label = {labels['media_precedent_label']})")
    return "\n".join("- " + b for b in bits)


def _grammar() -> str:
    rid = " | ".join(f'"{i}"' for i in JUDGED_IDS)
    typ = " | ".join(f'"{t}"' for t in MESSAGE_TYPES)
    return ('root ::= "{" ws "\\"rule_id\\"" ws ":" ws "\\"" rid "\\"" ws ","'
            ' ws "\\"message_type\\"" ws ":" ws "\\"" typ "\\"" ws "}"\n'
            f"rid ::= {rid}\n"
            f"typ ::= {typ}\n"
            'ws ::= [ \\t\\n]*\n')


_PREFIX = (
    "You route WhatsApp messages. Below is the routing policy, in priority "
    "order: the FIRST rule that applies wins.\n\n"
    "POLICY\n" + _RULE_TEXT +
    "\n\nPick the first rule that applies to the message, and also say what "
    "kind of message it is.\n"
    "Reply with one JSON object: the rule_id you picked and the message_type. "
    "Nothing else.\n\n"
)


def select_rule(msg, feats, labels: Dict[str, Any]) -> Optional[dict]:
    """Ask the model which judged rule applies. Cached, deterministic."""
    hit = STORE.get(msg.message_id)
    if hit is not None:
        return hit

    text = ((msg.message_text or "") + " " + (feats.caption or "")).strip()
    if not text:
        # Nothing to judge. Structural rules still apply; the caller falls
        # through to evaluating the judged table in code instead of inventing
        # an opinion about a message with no readable content.
        return None

    prompt = (_PREFIX + "FACTS ABOUT THIS MESSAGE\n" + _facts_block(labels)
              + "\n\nMESSAGE\n" + llm._fence(text) + "\nJSON:")
    raw = llm.complete(prompt, grammar=_grammar(), max_tokens=64)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if parsed.get("rule_id") not in JUDGED_IDS:
        return None
    if parsed.get("message_type") not in MESSAGE_TYPES:
        return None

    row = {"rule_id": parsed["rule_id"], "message_type": parsed["message_type"]}
    STORE.put(msg.message_id, row)
    STORE.flush()
    return row


# --------------------------------------------------------------------------
# The two stages
# --------------------------------------------------------------------------

def route(labels: Dict[str, Any], msg=None, feats=None,
          use_llm: bool = False) -> dict:
    """Apply the policy to one message.

    Returns the selected rule's outcome plus the id that produced it. Guarded
    rules are always evaluated in code and always win.
    """
    # -- Stage 1: guarded rules, in code, first ---------------------------
    for rule in GUARDED_RULES:
        if engine._matches(rule, labels):
            out = dict(rule["then"])
            out["action"] = engine._resolve(out.get("action"), labels)
            out["reason"] = engine._resolve(out.get("reason"), labels)
            out["rule_id"] = rule["id"]
            out["decided_by"] = "guarded"
            return out

    # -- Stage 2: judged rules ---------------------------------------------
    chosen = select_rule(msg, feats, labels) if (use_llm and msg is not None) else None

    if chosen is not None:
        rule = next(r for r in JUDGED_RULES if r["id"] == chosen["rule_id"])
        # The model picked the rule; the rule -- not the model -- says what to
        # do. `@precedent_label` resolves from structural facts either way.
        if engine._matches(rule, labels) or _selectable(rule, labels):
            out = dict(rule["then"])
            out["action"] = engine._resolve(out.get("action"), labels)
            out["reason"] = engine._resolve(out.get("reason"), labels)
            out["rule_id"] = rule["id"]
            out["message_type"] = chosen["message_type"]
            out["decided_by"] = "judged"
            if out["action"] in ACTIONS:
                return out

    # -- Fallback: evaluate the judged table in code -----------------------
    # Reached when there is no model, no cached selection, no readable text, or
    # the model picked a rule whose structural preconditions do not hold.
    for rule in JUDGED_RULES:
        if engine._matches(rule, labels):
            out = dict(rule["then"])
            out["action"] = engine._resolve(out.get("action"), labels)
            out["reason"] = engine._resolve(out.get("reason"), labels)
            out["rule_id"] = rule["id"]
            out["decided_by"] = "table"
            return out

    raise ValueError("no rule matched; rules.json must end in a default rule")


def _selectable(rule: dict, labels: Dict[str, Any]) -> bool:
    """May the model pick this rule even though its conditions do not match?

    Only for conditions the model is entitled to judge. A rule resting on a
    structural fact -- precedent, group_muted -- is not selectable when that
    fact is absent, because the model cannot see the engagement history and
    must not be able to assert one.
    """
    structural = {"precedent_label", "precedent_unanimous", "precedent_n",
                  "media_precedent_label", "group_muted", "is_business",
                  "user_opted_out", "sender_verified", "sender_domain_mismatch",
                  "sender_account_age_days", "sender_structurally_trusted"}
    for cond in rule["when"]:
        if cond[0] in structural and not engine._test(cond, labels):
            return False
    return True


# --------------------------------------------------------------------------
# Producing an output row
# --------------------------------------------------------------------------

_REASON_TEXT = {
    "injection": "The message contains instructions aimed at the routing system rather than the user.",
    "impersonation": "The sender impersonates a known brand from an unverified, recently created account on a lookalike domain.",
    "credential_request": "The message asks for OTP, card or wallet credentials through a suspicious verification flow.",
    "chain": "The message is a chain forward asking the user to propagate it further.",
    "opt_out": "The user has opted out of or repeatedly dismissed messages from this sender.",
    "deadline": "The message carries a time-sensitive instruction the user is likely to need now.",
    "precedent": "How the user has consistently handled this sender is the best guide available.",
    "precedent_notify": "The user consistently opens and replies to this sender, so this message is worth an interruption.",
    "precedent_digest": "The user reads this sender's messages but rarely acts on them immediately, so it can wait.",
    "precedent_mute": "The user has consistently dismissed or muted messages from this sender.",
    "media_precedent": "The same media was received before and the user's handling of it is a reliable guide.",
    "promotion": "The message is promotional and does not require immediate attention.",
    "greeting": "A routine greeting with no actionable content.",
    "default": "Safe but low priority; no evidence that it needs to interrupt the user.",
}


def build_decision(msg, feats, labels, cands=(), use_llm: bool = False):
    """One output row, produced entirely by the policy in rules.json.

    The rule that fired sets the action and the confidence band position; the
    type table sets message_type; `reason` is the rule's stated rationale,
    optionally rewritten by a model to describe this specific message.
    """
    import engine
    from schema import CONFIDENCE_BANDS, Decision

    outcome = route(labels, msg, feats, use_llm=use_llm)
    action = outcome["action"]
    _, type_out = engine.evaluate(labels)
    mtype = outcome.get("message_type") or type_out["type"]

    kind = outcome.get("reason") or "default"
    if kind == "precedent":
        kind = "precedent_" + action
    reason = _REASON_TEXT.get(kind, _REASON_TEXT["default"])

    lo, hi = CONFIDENCE_BANDS[action]
    strength = float(outcome.get("strength", 0.5))
    confidence = round(lo + (hi - lo) * max(0.0, min(1.0, strength)), 2)

    # A model may rewrite the prose, never the routing. Guarded rows keep their
    # fixed wording: those are the rows whose text may be adversarial.
    #
    # This is attempted on every run, not just under --llm, because the written
    # reasons are already in the committed cache. Without --llm the model layer
    # is in cache-only mode, so this replays what is on disk and asks for
    # nothing new; a row with no cached prose simply keeps the rule's own
    # wording. That is what lets a keyless run reproduce the submitted
    # output.csv byte for byte instead of a blander variant of it.
    if outcome.get("decided_by") != "guarded":
        try:
            import llm
            written = llm.write_reason(msg, feats, action, kind, reason)
            if written:
                reason = written
        except ImportError:
            pass

    return Decision(
        message_id=msg.message_id,
        action=action,
        message_type=mtype,
        reason=reason,
        confidence=confidence,
        evidence_message_ids=[c.message.message_id for c in cands][:2],
        decided_by=outcome["rule_id"],
    )
