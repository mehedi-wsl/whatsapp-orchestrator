"""Shared contract for the Message Notification Router.

Every module builds against the types and signatures here. Do not change this
file without updating ARCHITECTURE.md -- other modules are written against it
in parallel.

Stdlib + numpy only. No network, no API calls at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# --------------------------------------------------------------------------
# Allowed values (problem_statement.md)
# --------------------------------------------------------------------------

ACTIONS = ("notify", "digest", "mute")

MESSAGE_TYPES = (
    "personal", "urgent", "event", "payment", "business_update",
    "promotion", "greeting", "forward", "spam", "scam", "unknown",
)

OUTPUT_COLUMNS = (
    "message_id", "action", "message_type", "reason",
    "confidence", "evidence_message_ids",
)

# Confidence bands, calibrated to the 30 solved rows (ARCHITECTURE.md s10).
CONFIDENCE_BANDS = {
    "notify": (0.85, 0.91),
    "mute":   (0.81, 0.87),
    "digest": (0.78, 0.84),
}

# The five engagement signatures in message_events.csv and the routing label
# each one encodes. Key order:
#   (opened, replied, reaction_time_minutes, dismissed, muted_after, reported)
# See ARCHITECTURE.md s2.
ENGAGEMENT_SIGNATURES = {
    ("1", "1", "2",   "0", "0", "0"): "notify",
    ("1", "0", "120", "0", "0", "0"): "digest",
    ("0", "0", "",    "1", "1", "0"): "mute",
    ("0", "0", "",    "1", "1", "1"): "mute",
    ("1", "0", "9",   "0", "0", "0"): "mute",
}

DATASET_DIR = os.environ.get("DATASET_DIR", "dataset")


# --------------------------------------------------------------------------
# Core records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """One row of messages.csv / sample_messages.csv / message_history.csv."""
    message_id: str
    user_id: str
    conversation_type: str          # personal | group | business
    group_id: str
    business_id: str
    sender_user_id: str
    created_at: str                 # "YYYY-MM-DD HH:MM"
    message_text: str
    media_type: str                 # "" | image | voice
    media_id: str
    forwarded_count: str

    @property
    def source(self) -> Optional[str]:
        """Sender identity: user id, or 'BIZ:<business_id>'. None if neither.

        This is the key for relationship precedent. See ARCHITECTURE.md s3.
        """
        if self.sender_user_id:
            return self.sender_user_id
        if self.business_id:
            return "BIZ:" + self.business_id
        return None


@dataclass
class Dataset:
    """Everything loaded from dataset/. Built once by features.load_dataset()."""
    messages: List[Message]
    history: List[Message]
    samples: List[dict]                       # solved rows: Message fields + labels
    events: Dict[tuple, dict]                 # (user_id, message_id) -> event row
    users: Dict[str, dict]
    groups: Dict[str, dict]
    group_members: Dict[tuple, dict]          # (user_id, group_id) -> row
    businesses: Dict[str, dict]
    user_business: Dict[tuple, dict]          # (user_id, business_id) -> row
    daily_load: Dict[tuple, dict]             # (user_id, date) -> row
    images: Dict[str, str]                    # image_id -> file_path
    voice_notes: Dict[str, str]               # voice_note_id -> file_path


@dataclass
class Features:
    """Per-message signals produced by the feature layer.

    Everything here is derived only from dataset/ files -- no media decoding,
    no model calls. Safe to compute for all 110 messages offline.
    """
    message_id: str

    # --- safety (ARCHITECTURE.md s5) ---
    is_impersonation: bool = False       # mismatch + unverified + age<=60d
    is_link_shortener: bool = False      # verified + old account + old domain
    domain_mismatch: bool = False
    sender_domain_age_days: Optional[int] = None
    business_verified: Optional[bool] = None
    business_age_days: Optional[int] = None
    business_reports_30d: Optional[int] = None

    # --- relationship precedent (ARCHITECTURE.md s3) ---
    precedent_label: Optional[str] = None      # notify | digest | mute | None
    precedent_unanimous: bool = False
    precedent_n: int = 0
    precedent_message_ids: List[str] = field(default_factory=list)

    # --- user / group state ---
    group_muted: bool = False
    group_type: str = ""
    user_opted_out: bool = False
    in_quiet_hours: bool = False
    notifications_today: Optional[int] = None

    # --- content shape (cheap lexical flags, no model) ---
    has_deadline_language: bool = False
    has_payment_language: bool = False
    has_credential_request: bool = False       # OTP / PIN / card / wallet verify
    has_chain_instruction: bool = False        # "forward to ten people"
    has_injection_attempt: bool = False        # instructions aimed at the router
    has_url_shortener: bool = False

    # --- media (ARCHITECTURE.md s8) ---
    media_kind: str = ""                       # "" | image | voice
    caption: str = ""                          # image caption; "" for voice
    media_precedent_label: Optional[str] = None    # from same media_id in history
    media_precedent_message_ids: List[str] = field(default_factory=list)


@dataclass
class Decision:
    """Final routed result for one message. One per row of messages.csv."""
    message_id: str
    action: str                                # must be in ACTIONS
    message_type: str                          # must be in MESSAGE_TYPES
    reason: str
    confidence: float
    evidence_message_ids: List[str]            # [] serialises to "none"
    decided_by: str = ""                       # layer name, for debugging only

    def to_row(self) -> dict:
        assert self.action in ACTIONS, f"bad action {self.action!r}"
        assert self.message_type in MESSAGE_TYPES, f"bad type {self.message_type!r}"
        lo, hi = CONFIDENCE_BANDS[self.action]
        conf = min(hi, max(lo, round(float(self.confidence), 2)))
        return {
            "message_id": self.message_id,
            "action": self.action,
            "message_type": self.message_type,
            "reason": self.reason,
            "confidence": f"{conf:.2f}",
            "evidence_message_ids": ";".join(self.evidence_message_ids) or "none",
        }


@dataclass
class Candidate:
    """A retrieved historical message, with its recovered label."""
    message: Message
    score: float
    label: Optional[str]           # notify | digest | mute, from engagement signature
    same_source: bool
    same_group: bool


# --------------------------------------------------------------------------
# Module interfaces -- implemented in the files named below
# --------------------------------------------------------------------------

# code/features.py
#   def load_dataset(dataset_dir: str = DATASET_DIR) -> Dataset
#   def label_for_event(event: dict) -> Optional[str]
#   def build_features(ds: Dataset, msg: Message) -> Features
#
# code/retrieval.py
#   def candidates(ds: Dataset, msg: Message, k: int = 5) -> List[Candidate]
#       Same-user filter is mandatory and lossless (28/28 on the dev set).
#       Rank lexically; return top k, best first.
#   def select_evidence(cands: Sequence[Candidate], limit: int = 1) -> List[str]
#       Apply a similarity floor; return [] when nothing clears it.
#
# code/decide.py
#   def decide(ds: Dataset, msg: Message, feats: Features,
#              cands: Sequence[Candidate]) -> Decision
#
# code/evaluate.py
#   def backtest(dataset_dir: str = DATASET_DIR) -> dict
#       Scores action accuracy, message_type accuracy, and evidence top-1
#       against sample_messages.csv. Prints a per-row diff table.
#
# code/main.py
#   Wires the above and writes output.csv with OUTPUT_COLUMNS, one row per
#   message in messages.csv, in input order.
