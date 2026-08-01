"""Feature layer for the Message Notification Router.

Implements the three functions the rest of the pipeline builds against
(see ``code/schema.py``)::

    load_dataset(dataset_dir) -> Dataset
    label_for_event(event)    -> Optional[str]
    build_features(ds, msg)   -> Features

Everything here is derived from ``dataset/`` CSVs only -- no media decoding,
no model calls, no network. Stdlib only (numpy is permitted but not needed).

The design rationale for each rule lives in ARCHITECTURE.md:

* s2  engagement signatures  -> ``label_for_event``
* s3  relationship precedent -> ``precedent_*``
* s5  safety composite       -> ``is_impersonation`` / ``is_link_shortener``
* s8  media handling         -> ``media_*`` / ``caption``

Run this module directly for a self-check over all 110 messages.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# Allow `python3 code/features.py` as well as `from code import features`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schema import (  # noqa: E402
    DATASET_DIR,
    ENGAGEMENT_SIGNATURES,
    Dataset,
    Features,
    Message,
)

# --------------------------------------------------------------------------
# Expected row counts. Verified against the shipped dataset; asserted on load
# so a truncated or line-counted read fails loudly instead of silently
# degrading retrieval. Several message_text fields contain embedded newlines,
# which is why every read below goes through csv.DictReader.
# --------------------------------------------------------------------------

EXPECTED_ROWS = {
    "messages.csv": 110,
    "sample_messages.csv": 30,
    "message_history.csv": 412,
    "message_events.csv": 412,
    "business_accounts.csv": 110,
    "users.csv": 54,
    "groups.csv": 23,
    "group_members.csv": 401,
    "user_business_history.csv": 106,
    "daily_notification_summary.csv": 756,
    "images.csv": 20,
    "voice_notes.csv": 13,
}

MESSAGE_FIELDS = (
    "message_id", "user_id", "conversation_type", "group_id", "business_id",
    "sender_user_id", "created_at", "message_text", "media_type", "media_id",
    "forwarded_count",
)

# Impersonation composite (ARCHITECTURE.md s5): unverified, young account,
# real brand name, lookalike sender domain.
IMPERSONATION_MAX_ACCOUNT_AGE_DAYS = 60

# Link-shortener carve-out: a *verified*, long-lived account whose sender
# domain is itself long-lived is using a shortener, not spoofing.
SHORTENER_MIN_AGE_DAYS = 60


# --------------------------------------------------------------------------
# Lexical content flags
#
# Deliberately small, explicit and case-insensitive. These are contributing
# risk/urgency features, never gates -- the decision stack owns policy.
# --------------------------------------------------------------------------

def _any(*patterns: str) -> "re.Pattern[str]":
    """Compile an alternation of patterns, case-insensitive."""
    return re.compile("|".join(patterns), re.IGNORECASE)


# OTP / PIN / card / wallet / KYC solicitation.
RE_CREDENTIAL_REQUEST = _any(
    r"\botp\b",
    r"\bo\.t\.p\b",
    r"\bone[- ]time (?:password|code|pin)\b",
    r"\bpin\b",
    r"\bcvv\b",
    r"\bcard (?:details|number|pin|access)\b",
    r"\b(?:debit|credit) card\b",
    r"\bwallet (?:pin|verification|verify|details)\b",
    r"\bverify (?:your )?(?:wallet|card|account|identity)\b",
    r"\blogin code\b",
    r"\baccess code\b",
    r"\bverification code\b",
    r"\bsecurity code\b",
    r"\bkyc\b",
    r"\b(?:6|six)[- ]digit\b",
    r"\bre-?verify\b",
    r"\bconfirm your (?:pin|password|wallet|card|identity)\b",
)

# Time pressure. Scored against the message's OWN created_at downstream
# (ARCHITECTURE.md s6) -- this flag only says the language is present.
RE_DEADLINE = _any(
    r"\btoday\b",
    r"\btonight\b",
    r"\bbefore midnight\b",
    r"\bby (?:midnight|tonight|today|end of day|eod)\b",
    r"\bexpir(?:e|es|ed|ing|y)\b",
    r"\blast date\b",
    r"\blast day\b",
    r"\bdeadline\b",
    r"\bimmediately\b",
    r"\bright now\b",
    r"\bwithin \d+\s*(?:hour|hr|minute|min|day)s?\b",
    r"\bin the next \d+\s*(?:hour|hr|minute|min)s?\b",
    r"\bwithin the hour\b",
    r"\bclos(?:es|ing) (?:today|tonight|soon)\b",
    r"\btomorrow\b[^.\n]{0,40}\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",  # tomorrow + a time
    r"\bbefore \d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
    r"\burgent(?:ly)?\b",
)

# Money movement. Rupee amounts count in either notation.
RE_PAYMENT = _any(
    r"\bpay(?:ment|ments|able|ing)?\b",
    r"\brefund(?:s|ed)?\b",
    r"\binvoice\b",
    r"\bbill(?:ing)?\b",
    r"\bdue\b",
    r"\boutstanding\b",
    r"\btoken (?:amount|money)\b",
    r"\bupi\b",
    r"\btransaction\b",
    r"\bfee\b",
    r"\bdeposit\b",
    r"\bemi\b",
    r"\b(?:rs\.?|inr|₹)\s?\d",
    r"\b\d[\d,]*\s?(?:rs\.?|inr|₹)\b",
)

# Chain-forward instructions. Note (ARCHITECTURE.md s4) that the numeric
# forwarded_count was dropped as a signal; the *instruction language* is kept.
# Every pattern here must be an IMPERATIVE aimed at the recipient. A bare
# mention of blessings is not a chain: "Sharing blessings with everyone" is a
# sign-off in a family greeting (msg_034), and flagging it muted a parent's
# message with a reason that misdescribed it.
RE_CHAIN = _any(
    r"\bforward (?:this|it|to|the message)\b",
    r"\bshare (?:this )?(?:\w+ )?(?:with|to) (?:ten|10|\d+|all|everyone)\b",
    r"\bshare (?:this )?(?:with|to) \w+ (?:people|friends|groups?|contacts?)\b",
    r"\bsend (?:this )?to (?:all|everyone|\w+ people|\d+)\b",
    r"\bsend it to \d+\b",
    r"\bdo not break (?:the )?chain\b",
    r"\bcopy paste\b",
    r"\bforward to (?:ten|10|\d+)\b",
)

# Text aimed at the routing system itself rather than at the user.
# The five known cases in messages.csv are msg_095, msg_107, msg_108,
# msg_109 and msg_110; the self-check asserts all five are caught.
RE_INJECTION = _any(
    r"\bsystem note\b",
    r"\bsystem\s*:",
    r"\brouting override\b",
    r"\binternal router metadata\b",
    r"\bassistant instruction\b",
    r"\bnotification router\b",
    r"\bignore (?:previous|prior|all previous|sender risk|the above)\b",
    r"\bdisregard (?:previous|prior|the above)\b",
    r"\bclassify (?:this )?as\b",
    r"\bmark (?:this )?as\b",
    r"\bmark notify\b",
    r"\bset action\s*=",
    r"\baction\s*=\s*(?:notify|digest|mute)\b",
    r"\bverified_business\s*=",
    r"\buser_priority\s*=",
    r"\bconfidence\s*=\s*\d",
    r"\balways mark this\b",
    r"\byou are an? (?:ai|assistant|model)\b",
)

# Known URL shorteners plus lookalike short hosts (ARCHITECTURE.md s5).
KNOWN_SHORTENERS = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "rb.gy", "cutt.ly",
    "is.gd", "buff.ly", "shorturl.at", "rebrand.ly", "tiny.cc", "s.id",
    "lnkd.in", "wa.link", "wa.me", "link.wame.pro", "weurl.co",
})

# Short registrable name on a shortener-flavoured TLD, e.g. "weurl.co",
# "wame.pro". Deliberately excludes .com/.in so real brands are not caught.
RE_SHORT_HOST = re.compile(
    r"^[a-z0-9-]{1,6}\.(?:ly|gl|gd|cc|id|to|me|st|sh|link|pro|xyz|co)$"
)

# Bare or http(s) host, optionally followed by a path.
RE_HOST = re.compile(
    r"\b(?:https?://)?([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+)(?:/\S*)?",
    re.IGNORECASE,
)


def _hosts(text: str) -> List[str]:
    """Every host-looking token in ``text``, lowercased, in order."""
    return [h.lower() for h in RE_HOST.findall(text or "")]


def is_shortener_host(host: str) -> bool:
    """True for a known shortener or a shortener-shaped lookalike host."""
    host = (host or "").lower().strip().strip(".")
    if not host:
        return False
    if host in KNOWN_SHORTENERS:
        return True
    if any(host.endswith("." + s) or host == s for s in KNOWN_SHORTENERS):
        return True
    # Try the host itself and its last two labels ("link.wame.pro" -> "wame.pro").
    labels = host.split(".")
    tail = ".".join(labels[-2:]) if len(labels) >= 2 else host
    return bool(RE_SHORT_HOST.match(host) or RE_SHORT_HOST.match(tail))


# --------------------------------------------------------------------------
# Small parsing helpers -- every optional column may be an empty string
# --------------------------------------------------------------------------

def _int(value: Optional[str]) -> Optional[int]:
    """Parse an int, returning None for blank/garbage rather than raising."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _bool01(value: Optional[str]) -> Optional[bool]:
    """Parse a 0/1 flag column. None when blank."""
    i = _int(value)
    return None if i is None else bool(i)


def _read_csv(path: str, expected: Optional[int] = None) -> List[dict]:
    """Read a CSV into dicts. Never line-count: text fields contain newlines."""
    with open(path, newline="", encoding="utf-8") as handle:
        rows = [
            {(k or ""): ("" if v is None else v) for k, v in row.items()}
            for row in csv.DictReader(handle)
        ]
    if expected is not None and len(rows) != expected:
        raise AssertionError(
            f"{os.path.basename(path)}: expected {expected} rows, read {len(rows)}"
        )
    return rows


def _to_message(row: dict) -> Message:
    """Build a Message from any row carrying the eleven shared input columns."""
    return Message(**{f: (row.get(f) or "").strip() for f in MESSAGE_FIELDS})


def _resolve_dir(dataset_dir: str) -> str:
    """Resolve ``dataset_dir`` relative to cwd, then to the repo root.

    Lets the module be run from either the repo root or ``code/``.
    """
    if os.path.isdir(dataset_dir):
        return dataset_dir
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(repo_root, dataset_dir)
    if os.path.isdir(candidate):
        return candidate
    raise FileNotFoundError(f"dataset directory not found: {dataset_dir!r}")


def _minutes(hhmm: str) -> Optional[int]:
    """'22:30' -> 1350. None if unparseable."""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", hhmm or "")
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def in_quiet_hours(window: str, created_at: str) -> bool:
    """True when ``created_at`` falls inside a 'HH:MM-HH:MM' DND window.

    Windows wrap midnight (e.g. '22:00-07:00'), so the wrapped case tests the
    union of [start, 24:00) and [00:00, end).
    """
    if not window or "-" not in window or " " not in (created_at or ""):
        return False
    start_s, _, end_s = window.partition("-")
    start, end = _minutes(start_s), _minutes(end_s)
    at = _minutes(created_at.split(" ", 1)[1][:5])
    if start is None or end is None or at is None:
        return False
    if start == end:
        return False
    return at >= start or at < end if start > end else start <= at < end


# --------------------------------------------------------------------------
# load_dataset
# --------------------------------------------------------------------------

def load_dataset(dataset_dir: str = DATASET_DIR) -> Dataset:
    """Load every participant-facing file in ``dataset/`` into a Dataset.

    ``sample_messages.csv`` rows carry their own ``sample_msg_*`` ids and are
    *not* a subset of ``messages.csv``; they are kept as plain dicts (input
    columns plus the five label columns) in ``Dataset.samples``.
    """
    root = _resolve_dir(dataset_dir)

    def rows(name: str) -> List[dict]:
        return _read_csv(os.path.join(root, name), EXPECTED_ROWS.get(name))

    messages = [_to_message(r) for r in rows("messages.csv")]
    history = [_to_message(r) for r in rows("message_history.csv")]
    samples = rows("sample_messages.csv")

    events = {
        (r["user_id"], r["message_id"]): r for r in rows("message_events.csv")
    }
    users = {r["user_id"]: r for r in rows("users.csv")}
    groups = {r["group_id"]: r for r in rows("groups.csv")}
    group_members = {
        (r["user_id"], r["group_id"]): r for r in rows("group_members.csv")
    }
    businesses = {r["business_id"]: r for r in rows("business_accounts.csv")}
    user_business = {
        (r["user_id"], r["business_id"]): r for r in rows("user_business_history.csv")
    }
    daily_load = {
        (r["user_id"], r["date"]): r for r in rows("daily_notification_summary.csv")
    }
    images = {r["image_id"]: r["file_path"] for r in rows("images.csv")}
    voice_notes = {
        r["voice_note_id"]: r["file_path"] for r in rows("voice_notes.csv")
    }

    return Dataset(
        messages=messages,
        history=history,
        samples=samples,
        events=events,
        users=users,
        groups=groups,
        group_members=group_members,
        businesses=businesses,
        user_business=user_business,
        daily_load=daily_load,
        images=images,
        voice_notes=voice_notes,
    )


# --------------------------------------------------------------------------
# label_for_event
# --------------------------------------------------------------------------

EVENT_COLUMNS = (
    "message_opened", "message_replied", "reaction_time_minutes",
    "notification_dismissed", "muted_after_message", "message_reported",
)


def label_for_event(event: dict) -> Optional[str]:
    """Recover the routing label encoded by one ``message_events.csv`` row.

    ``message_events.csv`` holds only five distinct value-tuples across the six
    engagement columns (ARCHITECTURE.md s2); each is a label channel. Returns
    None for an unrecognised tuple -- guessing would corrupt every precedent
    that depends on it.

    Note the subtle one: ``1,0,9,0,0,0`` (opened fast, no reply) maps to
    ``mute``, not ``notify``. Fast-open-no-reply is a risk tell.
    """
    if not event:
        return None
    key = tuple((event.get(col) or "").strip() for col in EVENT_COLUMNS)
    return ENGAGEMENT_SIGNATURES.get(key)


# --------------------------------------------------------------------------
# Derived indexes, built once per Dataset
# --------------------------------------------------------------------------

@dataclass
class _Index:
    """Precomputed lookups over history. Internal; rebuilt per Dataset."""
    labels: Dict[str, Optional[str]] = field(default_factory=dict)
    by_relationship: Dict[Tuple[str, str], List[Message]] = field(default_factory=dict)
    by_media: Dict[str, List[Message]] = field(default_factory=dict)
    user_load: Dict[str, List[Tuple[str, int]]] = field(default_factory=dict)


_INDEX_CACHE: Dict[int, Tuple[Dataset, _Index]] = {}


def _sort_key(msg: Message) -> Tuple[str, str]:
    """Most-recent-first ordering, with message_id as a stable tiebreak."""
    return (msg.created_at, msg.message_id)


def _build_index(ds: Dataset) -> _Index:
    idx = _Index()

    by_relationship: Dict[Tuple[str, str], List[Message]] = defaultdict(list)
    by_media: Dict[str, List[Message]] = defaultdict(list)

    for hist in ds.history:
        idx.labels[hist.message_id] = label_for_event(
            ds.events.get((hist.user_id, hist.message_id), {})
        )
        source = hist.source
        if source:
            by_relationship[(hist.user_id, source)].append(hist)
        if hist.media_id:
            by_media[hist.media_id].append(hist)

    # Newest first, deterministic on ties.
    idx.by_relationship = {
        k: sorted(v, key=_sort_key, reverse=True)
        for k, v in sorted(by_relationship.items())
    }
    idx.by_media = {
        k: sorted(v, key=_sort_key, reverse=True) for k, v in sorted(by_media.items())
    }

    load: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for (user_id, date), row in sorted(ds.daily_load.items()):
        sent = _int(row.get("notifications_sent"))
        if sent is not None:
            load[user_id].append((date, sent))
    idx.user_load = {k: sorted(v) for k, v in sorted(load.items())}
    return idx


def _index_for(ds: Dataset) -> _Index:
    """Memoised index for ``ds`` (keeps a reference so ids cannot be reused)."""
    cached = _INDEX_CACHE.get(id(ds))
    if cached is not None and cached[0] is ds:
        return cached[1]
    idx = _build_index(ds)
    _INDEX_CACHE[id(ds)] = (ds, idx)
    return idx


def _summarise(
    rows: Sequence[Message], labels: Dict[str, Optional[str]]
) -> Tuple[Optional[str], bool, int, List[str]]:
    """Collapse labelled history rows into (label, unanimous, n, ids).

    ``rows`` must already be newest-first. Ties in the label vote are broken by
    recency -- the label of the most recent row among the tied candidates.
    """
    labelled = [r for r in rows if labels.get(r.message_id)]
    if not labelled:
        return None, False, 0, []
    votes = Counter(labels[r.message_id] for r in labelled)
    top = max(votes.values())
    tied = {lab for lab, n in votes.items() if n == top}
    label = next(labels[r.message_id] for r in labelled if labels[r.message_id] in tied)
    return label, len(votes) == 1, len(labelled), [r.message_id for r in labelled]


# --------------------------------------------------------------------------
# build_features
# --------------------------------------------------------------------------

def build_features(ds: Dataset, msg: Message) -> Features:
    """Compute every offline signal for one message.

    Structured signals only: business risk, relationship precedent, media
    precedent, user/group state and cheap lexical flags. No media decoding and
    no model calls, so this is safe and fast for all 110 messages.
    """
    idx = _index_for(ds)
    feats = Features(message_id=msg.message_id)
    text = msg.message_text or ""

    # --- business risk (ARCHITECTURE.md s5) -------------------------------
    biz = ds.businesses.get(msg.business_id) if msg.business_id else None
    if biz:
        official = (biz.get("official_domain") or "").strip().lower()
        used = (biz.get("domain_used_by_sender") or "").strip().lower()
        verified = (biz.get("verified") or "").strip()
        age = _int(biz.get("account_age_days"))
        domain_age = _int(biz.get("domain_used_by_sender_age_days"))

        feats.business_verified = _bool01(verified)
        feats.business_age_days = age
        feats.business_reports_30d = _int(biz.get("user_reports_30d"))
        feats.sender_domain_age_days = domain_age

        # A blank official_domain makes string comparison meaningless
        # (business_032, Green Cross Pharmacy: blank domain, 420d, legitimate).
        has_official = bool(official)
        feats.domain_mismatch = has_official and bool(used) and used != official

        # Brand impersonation: real brand, lookalike domain, unverified, young.
        # 21 of 110 business accounts; 7 of the 110 messages.
        feats.is_impersonation = (
            feats.domain_mismatch
            and verified == "0"
            and age is not None
            and age <= IMPERSONATION_MAX_ACCOUNT_AGE_DAYS
        )

        # Carve-out: verified + old account + old sender domain is a link
        # shortener (business_092 -> link.wame.pro, business_095 -> weurl.co).
        # Legitimate; a solved row labels this digest/promotion, not mute.
        feats.is_link_shortener = (
            feats.domain_mismatch
            and verified == "1"
            and age is not None
            and age > SHORTENER_MIN_AGE_DAYS
            and domain_age is not None
            and domain_age > SHORTENER_MIN_AGE_DAYS
        )

    # --- relationship precedent (ARCHITECTURE.md s3) ----------------------
    # (user, sender) is the retrieval key: unanimous for 79 of 110, versus a
    # global sender key which is mixed for 81 of 110.
    source = msg.source
    if source:
        rows = idx.by_relationship.get((msg.user_id, source), ())
        (
            feats.precedent_label,
            feats.precedent_unanimous,
            feats.precedent_n,
            feats.precedent_message_ids,
        ) = _summarise(rows, idx.labels)

    # --- media (ARCHITECTURE.md s8) ---------------------------------------
    feats.media_kind = msg.media_type
    if msg.media_type == "image":
        # All 15 image messages carry a usable caption; images route on it.
        feats.caption = text
    if msg.media_id:
        media_rows = idx.by_media.get(msg.media_id, ())
        label, _unanimous, _n, ids = _summarise(media_rows, idx.labels)
        feats.media_precedent_label = label
        feats.media_precedent_message_ids = ids

    # --- user / group state -----------------------------------------------
    user = ds.users.get(msg.user_id, {})
    feats.in_quiet_hours = in_quiet_hours(
        user.get("do_not_disturb_window", ""), msg.created_at
    )

    if msg.group_id:
        group = ds.groups.get(msg.group_id, {})
        feats.group_type = (group.get("group_type") or "").strip()
        member = ds.group_members.get((msg.user_id, msg.group_id), {})
        feats.group_muted = (member.get("group_muted_by_user") or "").strip() == "1"

    if msg.business_id:
        # "Opted out" means an explicit, dated opt-out event that predates the
        # message -- not merely allows_promotions == 0, which is the default
        # for 88 of 106 pairs and carries no intent.
        rel = ds.user_business.get((msg.user_id, msg.business_id), {})
        opted_out_at = (rel.get("promotions_opted_out_at") or "").strip()
        feats.user_opted_out = bool(opted_out_at) and opted_out_at <= msg.created_at

    feats.notifications_today = _notification_load(idx, msg)

    # --- lexical content flags --------------------------------------------
    feats.has_credential_request = bool(RE_CREDENTIAL_REQUEST.search(text))
    feats.has_deadline_language = bool(RE_DEADLINE.search(text))
    feats.has_payment_language = bool(RE_PAYMENT.search(text))
    feats.has_chain_instruction = bool(RE_CHAIN.search(text))
    feats.has_injection_attempt = bool(RE_INJECTION.search(text))
    feats.has_url_shortener = any(is_shortener_host(h) for h in _hosts(text))

    return feats


def _notification_load(idx: _Index, msg: Message) -> Optional[int]:
    """Notifications already sent to this user on the message's own date.

    ``daily_notification_summary.csv`` covers 2026-07-04..2026-07-17 while the
    scored messages run 2026-07-18..2026-07-31, so the exact-date key never
    hits on the live set. Rather than report nothing, fall back to the user's
    trailing average over the last seven observed days -- a stable per-user
    load baseline. The exact-date lookup is kept first so the function stays
    correct if a summary for the scored window is ever supplied.
    """
    date = msg.created_at[:10]
    series = idx.user_load.get(msg.user_id)
    if not series:
        return None
    for day, sent in series:
        if day == date:
            return sent
    window = [sent for _day, sent in series[-7:]]
    return int(round(sum(window) / len(window))) if window else None


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------

def _self_check() -> int:
    """Load everything, build all 110 feature rows, assert documented facts."""
    ds = load_dataset()
    idx = _index_for(ds)
    failures: List[str] = []

    def check(ok: bool, msg: str) -> None:
        print(("  ok   " if ok else "  FAIL ") + msg)
        if not ok:
            failures.append(msg)

    print("dataset")
    print(f"  messages={len(ds.messages)} history={len(ds.history)} "
          f"samples={len(ds.samples)} events={len(ds.events)}")
    print(f"  users={len(ds.users)} groups={len(ds.groups)} "
          f"members={len(ds.group_members)} businesses={len(ds.businesses)}")
    print(f"  user_business={len(ds.user_business)} daily_load={len(ds.daily_load)} "
          f"images={len(ds.images)} voice_notes={len(ds.voice_notes)}")

    print("\nengagement signatures (ARCHITECTURE.md s2)")
    labels = [label_for_event(e) for e in ds.events.values()]
    unmapped = sum(1 for lab in labels if lab is None)
    check(unmapped == 0, f"all 412 events map to a signature (unmapped={unmapped})")
    print("  " + " ".join(
        f"{k}={v}" for k, v in sorted(Counter(labels).items(), key=lambda kv: -kv[1])
    ))

    print("\nsample rows")
    sample_ids = {r["message_id"] for r in ds.samples}
    message_ids = {m.message_id for m in ds.messages}
    check(not (sample_ids & message_ids), "sample ids are disjoint from messages.csv")
    check(
        all("action" in r and "evidence_message_ids" in r for r in ds.samples),
        "sample rows carry label columns",
    )

    feats = {m.message_id: build_features(ds, m) for m in ds.messages}

    print("\nsafety (ARCHITECTURE.md s5)")
    impersonation = sorted(f.message_id for f in feats.values() if f.is_impersonation)
    expected_imp = ["msg_019", "msg_026", "msg_036", "msg_052",
                    "msg_076", "msg_085", "msg_108"]
    check(impersonation == expected_imp,
          f"impersonation fires on exactly 7 messages: {impersonation}")
    imp_biz = sorted(
        b for b, row in ds.businesses.items()
        if (row["official_domain"].strip()
            and row["domain_used_by_sender"].strip() != row["official_domain"].strip()
            and row["verified"] == "0"
            and (_int(row["account_age_days"]) or 0) <= IMPERSONATION_MAX_ACCOUNT_AGE_DAYS)
    )
    print(f"  note: {len(imp_biz)} business accounts match the composite")
    check(not feats["msg_086"].is_impersonation,
          "business_092 (Thrillophilia shortener) is NOT impersonation")
    shorteners = sorted(
        {m.business_id for m in ds.messages
         if m.business_id and feats[m.message_id].is_link_shortener}
    )
    check(shorteners == ["business_092", "business_095"],
          f"link-shortener carve-out: {shorteners}")
    # The non-empty official_domain guard exists for business_032 (Green Cross
    # Pharmacy: blank domain, 420d, 0 reports, legitimate). Four other accounts
    # also have a blank official_domain -- the "Unknown brand" high-report
    # cluster -- but none of them send any of the 110 scored messages, so the
    # guard costs nothing here. Their risk is still exposed via
    # business_reports_30d / business_age_days for the decision layer.
    blank_domain_biz = sorted(
        b for b, row in ds.businesses.items() if not row["official_domain"].strip()
    )
    check("business_032" in blank_domain_biz,
          f"blank official_domain guard covers business_032 (of {blank_domain_biz})")
    guarded_msgs = sorted(
        m.message_id for m in ds.messages if m.business_id in set(blank_domain_biz)
    )
    check(not any(feats[i].is_impersonation for i in guarded_msgs),
          f"blank-domain senders are not flagged as impersonation: {guarded_msgs}")

    print("\nprompt injection (ARCHITECTURE.md s5)")
    injections = sorted(f.message_id for f in feats.values() if f.has_injection_attempt)
    expected_inj = ["msg_095", "msg_107", "msg_108", "msg_109", "msg_110"]
    check(all(i in injections for i in expected_inj),
          f"all 5 known injection messages flagged: {injections}")

    print("\nrelationship precedent (ARCHITECTURE.md s3)")
    none_n = sum(1 for f in feats.values() if f.precedent_n == 0)
    unan = sum(1 for f in feats.values() if f.precedent_unanimous)
    mixed = len(feats) - none_n - unan
    check((none_n, unan, mixed) == (6, 79, 25),
          f"precedent coverage none={none_n} unanimous={unan} mixed={mixed}")

    print("\nmedia (ARCHITECTURE.md s8)")
    media = [f for f in feats.values() if f.media_kind]
    covered = [f for f in media if f.media_precedent_label]
    check(len(media) == 23, f"media messages: {len(media)} (15 image / 8 voice)")
    check(len(covered) == 16,
          f"media_id precedent covers {len(covered)} of {len(media)}")
    images = [f for f in media if f.media_kind == "image"]
    voices = [f for f in media if f.media_kind == "voice"]
    check(all(f.caption.strip() for f in images),
          f"all {len(images)} image messages carry a caption")
    check(all(not f.caption for f in voices),
          f"all {len(voices)} voice notes have no caption")
    check(all(f.precedent_n > 0 and f.precedent_unanimous for f in voices),
          "all 8 voice notes have unanimous (user, sender) precedent")

    print("\nuser / group state")
    print(f"  quiet hours={sum(1 for f in feats.values() if f.in_quiet_hours)}"
          f"  group_muted={sum(1 for f in feats.values() if f.group_muted)}"
          f"  opted_out={sum(1 for f in feats.values() if f.user_opted_out)}"
          f"  load_known={sum(1 for f in feats.values() if f.notifications_today is not None)}")

    print("\nlexical flags (count of 110)")
    for name in ("has_credential_request", "has_deadline_language",
                 "has_payment_language", "has_chain_instruction",
                 "has_injection_attempt", "has_url_shortener"):
        print(f"  {name:<24} {sum(1 for f in feats.values() if getattr(f, name)):>3}")

    check(len(feats) == 110 and len(idx.labels) == 412,
          "built features for all 110 messages")

    print("\n" + ("SELF-CHECK PASSED" if not failures
                  else f"SELF-CHECK FAILED ({len(failures)})"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(_self_check())
