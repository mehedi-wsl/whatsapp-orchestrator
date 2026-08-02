"""Evidence retrieval for the Message Notification Router.

Public surface (see ARCHITECTURE.md s7 and code/schema.py):

    candidates(ds, msg, k=5) -> List[Candidate]
    select_evidence(cands, limit=1) -> List[str]

Design, in the order the signals are applied
--------------------------------------------

1.  **Same-user filter (hard).**  `history.user_id == msg.user_id`.  Measured
    on the 30 solved rows in `sample_messages.csv`: 31/31 cited evidence ids
    are addressed to the same receiving user, and the gold id is inside that
    pool for 28/28 rows that cite evidence.  The filter is therefore lossless.
    Pools are 5-32 rows (mean 16), so no further pruning is needed.

    Same *sender* (26/31) and same *group* (17/31) are NOT filters -- the real
    relation is near-duplicate / paraphrase content, and it crosses senders and
    groups freely ("never ask for OTP or payment details on calls" from a
    business matches a group message from a neighbour).  They are used only as
    ordering tiers below.

2.  **Lexical similarity (primary key).**  IDF-weighted Dice over token sets.
    Dice beat cosine, BM25, plain TF-IDF, char n-gram Jaccard, overlap
    coefficient and every blend of those on the dev set (see `__main__`).
    IDF is what suppresses the heavy template boilerplate in this corpus
    ("Dear Customer,", "Reply STOP to unsubscribe") without a stopword list,
    which matters because the corpus mixes English, Hinglish and French.

3.  **Relationship tiers (secondary key).**  same source > same conversation
    type > same group.  These only break ties in the similarity score, they
    never outrank it.  The corpus contains verbatim template repeats, so exact
    1.0 ties are common (12 of 28 dev rows have a tied top score); the tiers
    resolve most of them on relationship grounds rather than by luck.

4.  **Recency (tertiary key), direction chosen by match strength.**  For a
    substantive content match (score >= 0.35) the *earliest* receipt wins --
    that is the occurrence that established the precedent, later ones are
    re-sends of it.  For a merely topical link the *latest* contact wins,
    being the freshest context.  This is what actually resolves the verbatim
    template ties, and it makes the ranking independent of the final key.

5.  **message_id ascending** as the final key, purely so repeated runs are
    byte-identical.  Nothing in the ranking is derived from id order or id
    range.  This is verified, not asserted: `__main__` re-scores the dev set
    60 times with that key replaced by a seeded random order and gets the
    identical top-1 every time (min == max == the reported score), i.e. the
    key is never actually reached.  Dropping the tiers and the recency rule
    exposes what an id-ordered tiebreak was worth -- 21/28 with ascending ids
    but a 13-19 spread under random orders.

Empty-text messages (voice notes) get a separate path: identical `media_id`
first, then the same relationship tiers, then recency.  Text similarity is
meaningless there and would otherwise leave the whole pool tied at zero.

Stdlib only.  Deterministic.  No network, no model calls.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:  # works both as `import retrieval` and as `from . import retrieval`
    from schema import (
        ENGAGEMENT_SIGNATURES,
        Candidate,
        Dataset,
        Message,
    )
except ImportError:  # pragma: no cover - packaging fallback
    from .schema import (  # type: ignore
        ENGAGEMENT_SIGNATURES,
        Candidate,
        Dataset,
        Message,
    )


# --------------------------------------------------------------------------
# Tunables -- every one of these was chosen by the sweep in __main__
# --------------------------------------------------------------------------

DEFAULT_SIMILARITY = "idf_dice"

TIER_SAME_SOURCE = 4.0
TIER_SAME_CONVERSATION = 2.0
TIER_SAME_GROUP = 1.0
TIER_MAX = TIER_SAME_SOURCE + TIER_SAME_CONVERSATION + TIER_SAME_GROUP

# At or above this score the two texts are a substantive content match (the
# same template or a close paraphrase), and the *earliest* such receipt wins
# the tiebreak.  Below it the link is only topical, and the *latest* contact
# wins.  Flat 0.30-0.40 on the dev set; 0.45-1.00 costs one row, <0.20 costs
# three.  See the sweep printed by __main__.
SUBSTANTIVE_MATCH_SCORE = 0.35

# Media-path pseudo-scores, on the same 0..1 scale as the text similarity so
# select_evidence() can apply one floor to both paths.
MEDIA_ID_MATCH_SCORE = 0.95
MEDIA_RELATION_BASE = 0.20
MEDIA_RELATION_SPAN = 0.10
MEDIA_NO_RELATION_SCORE = 0.05

# Below this, the pool holds nothing that justifies a citation -> "none".
# 2 of the 30 solved rows are legitimately `none`; neither is separable by
# score (one has an exact 1.0 duplicate available), so this floor is set to
# only catch genuinely unrelated pools rather than fitted to those two rows.
EVIDENCE_FLOOR = 0.10

# A second id is cited only when its score is an exact tie with the first.
# 25 of 30 solved rows cite exactly 1 id, 3 cite 2, 2 cite none -- so limit=1
# is the right default: at limit=2 the corpus's verbatim duplicates make this
# emit 1.33 ids/row against a gold mean of 1.03, buying one extra hit for
# roughly nine spurious citations.
SECOND_EVIDENCE_RATIO = 0.999

# Keeps "account-login.in", "e-mail", "don't" as single tokens -- shared hosts
# and hyphenated compounds are the highest-signal matches in this corpus, and
# splitting them scatters that signal across common fragments.  `[^\W_]`
# is unicode word chars minus underscore, so accented French and transliterated
# Hinglish tokens survive intact.
_TOKEN_RE = re.compile(r"[^\W_]+(?:[.'’-][^\W_]+)*", re.UNICODE)
_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


# --------------------------------------------------------------------------
# Text normalisation -- language agnostic, no translation, no stemming
# --------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase, collapse whitespace.  Accents and non-ASCII scripts survive."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.lower()).strip()


def tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(normalize(text))


def char_ngrams(text: str, n: int) -> List[str]:
    s = re.sub(r"[^\w ]+", " ", normalize(text), flags=re.UNICODE)
    s = " " + re.sub(r"\s+", " ", s).strip() + " "
    if len(s) < n:
        return [s]
    return [s[i:i + n] for i in range(len(s) - n + 1)]


def _parse_time(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Corpus statistics
# --------------------------------------------------------------------------

class Index:
    """Document frequencies over history + the messages we must route.

    Both are provided inputs, so using them is transductive but legitimate --
    no labels are touched.  Built once per Dataset and cached.
    """

    def __init__(self, texts: Sequence[str]):
        self.n_docs = max(1, len(texts))
        self.df: Dict[str, int] = defaultdict(int)
        self.char_df: Dict[int, Dict[str, int]] = {}
        total_len = 0
        for text in texts:
            toks = tokenize(text)
            total_len += len(toks)
            for tok in set(toks):
                self.df[tok] += 1
        self.avg_len = total_len / self.n_docs if self.n_docs else 1.0
        self._texts = list(texts)
        self._token_cache: Dict[str, Tuple[str, ...]] = {}
        self._ngram_cache: Dict[Tuple[str, int], Tuple[str, ...]] = {}

    # -- cached views ------------------------------------------------------
    def tokens(self, text: str) -> Tuple[str, ...]:
        cached = self._token_cache.get(text)
        if cached is None:
            cached = tuple(tokenize(text))
            self._token_cache[text] = cached
        return cached

    def ngrams(self, text: str, n: int) -> Tuple[str, ...]:
        key = (text, n)
        cached = self._ngram_cache.get(key)
        if cached is None:
            cached = tuple(char_ngrams(text, n))
            self._ngram_cache[key] = cached
        return cached

    def char_frequencies(self, n: int) -> Dict[str, int]:
        table = self.char_df.get(n)
        if table is None:
            table = defaultdict(int)
            for text in self._texts:
                for gram in set(char_ngrams(text, n)):
                    table[gram] += 1
            self.char_df[n] = table
        return table

    # -- weights -----------------------------------------------------------
    def idf(self, token: str) -> float:
        df = self.df.get(token, 0)
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def char_idf(self, gram: str, n: int) -> float:
        df = self.char_frequencies(n).get(gram, 0)
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))


_INDEX_CACHE: Dict[int, Index] = {}


def index_for(ds: Dataset) -> Index:
    key = id(ds)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        texts = [m.message_text for m in ds.history]
        texts += [m.message_text for m in ds.messages]
        idx = Index(texts)
        _INDEX_CACHE[key] = idx
    return idx


# --------------------------------------------------------------------------
# Similarity functions -- signature (index, query_text, doc_text) -> [0, 1]
# --------------------------------------------------------------------------

def sim_idf_dice(idx: Index, q: str, d: str) -> float:
    """IDF-weighted Dice over token *sets*.  The production ranker."""
    qs, ds_ = set(idx.tokens(q)), set(idx.tokens(d))
    if not qs or not ds_:
        return 0.0
    shared = sum(idx.idf(t) for t in qs & ds_)
    total = sum(idx.idf(t) for t in qs) + sum(idx.idf(t) for t in ds_)
    return (2.0 * shared / total) if total else 0.0


def sim_idf_set_cosine(idx: Index, q: str, d: str) -> float:
    qs, ds_ = set(idx.tokens(q)), set(idx.tokens(d))
    if not qs or not ds_:
        return 0.0
    shared = sum(idx.idf(t) ** 2 for t in qs & ds_)
    norm = math.sqrt(sum(idx.idf(t) ** 2 for t in qs) * sum(idx.idf(t) ** 2 for t in ds_))
    return shared / norm if norm else 0.0


def sim_idf_overlap(idx: Index, q: str, d: str) -> float:
    """Containment-style: normalised by the shorter side."""
    qs, ds_ = set(idx.tokens(q)), set(idx.tokens(d))
    if not qs or not ds_:
        return 0.0
    shared = sum(idx.idf(t) for t in qs & ds_)
    smaller = min(sum(idx.idf(t) for t in qs), sum(idx.idf(t) for t in ds_))
    return shared / smaller if smaller else 0.0


def sim_tfidf_cosine(idx: Index, q: str, d: str) -> float:
    qt, dt = idx.tokens(q), idx.tokens(d)
    if not qt or not dt:
        return 0.0
    qv, dv = Counter(qt), Counter(dt)
    num = sum(qv[t] * dv[t] * idx.idf(t) ** 2 for t in qv if t in dv)
    qn = math.sqrt(sum((qv[t] * idx.idf(t)) ** 2 for t in qv))
    dn = math.sqrt(sum((dv[t] * idx.idf(t)) ** 2 for t in dv))
    return num / (qn * dn) if qn and dn else 0.0


def _bm25(idx: Index, q: str, d: str, k1: float, b: float) -> float:
    qt, dt = idx.tokens(q), idx.tokens(d)
    if not qt or not dt:
        return 0.0
    dv = Counter(dt)
    dl = len(dt)
    score = 0.0
    for tok in set(qt):
        f = dv.get(tok, 0)
        if not f:
            continue
        score += idx.idf(tok) * f * (k1 + 1.0) / (f + k1 * (1.0 - b + b * dl / idx.avg_len))
    # squash onto roughly [0, 1] so one evidence floor works across rankers
    denom = sum(idx.idf(tok) for tok in set(qt)) or 1.0
    return min(1.0, score / denom)


def sim_bm25(idx: Index, q: str, d: str) -> float:
    return _bm25(idx, q, d, 1.2, 0.75)


def sim_bm25_b50(idx: Index, q: str, d: str) -> float:
    return _bm25(idx, q, d, 1.5, 0.5)


def _char_jaccard(idx: Index, q: str, d: str, n: int) -> float:
    qg, dg = set(idx.ngrams(q, n)), set(idx.ngrams(d, n))
    if not qg or not dg:
        return 0.0
    return len(qg & dg) / len(qg | dg)


def sim_char3_jaccard(idx: Index, q: str, d: str) -> float:
    return _char_jaccard(idx, q, d, 3)


def sim_char4_jaccard(idx: Index, q: str, d: str) -> float:
    return _char_jaccard(idx, q, d, 4)


def sim_char4_tfidf(idx: Index, q: str, d: str) -> float:
    qg, dg = Counter(idx.ngrams(q, 4)), Counter(idx.ngrams(d, 4))
    if not qg or not dg:
        return 0.0
    num = sum(qg[g] * dg[g] * idx.char_idf(g, 4) ** 2 for g in qg if g in dg)
    qn = math.sqrt(sum((qg[g] * idx.char_idf(g, 4)) ** 2 for g in qg))
    dn = math.sqrt(sum((dg[g] * idx.char_idf(g, 4)) ** 2 for g in dg))
    return num / (qn * dn) if qn and dn else 0.0


def sim_rare_token_overlap(idx: Index, q: str, d: str, max_df: int = 20) -> float:
    """Only tokens seen in <= max_df documents count.  Drops boilerplate hard."""
    qs = {t for t in idx.tokens(q) if idx.df.get(t, 0) <= max_df}
    ds_ = {t for t in idx.tokens(d) if idx.df.get(t, 0) <= max_df}
    if not qs or not ds_:
        return 0.0
    shared = sum(idx.idf(t) for t in qs & ds_)
    norm = math.sqrt(sum(idx.idf(t) for t in qs) * sum(idx.idf(t) for t in ds_))
    return shared / norm if norm else 0.0


def _mix(a: str, b: str, wa: float) -> Callable[[Index, str, str], float]:
    fa, fb = SIMILARITIES[a], SIMILARITIES[b]
    return lambda idx, q, d: wa * fa(idx, q, d) + (1.0 - wa) * fb(idx, q, d)


SIMILARITIES: Dict[str, Callable[[Index, str, str], float]] = {
    "idf_dice": sim_idf_dice,
    "idf_set_cosine": sim_idf_set_cosine,
    "idf_overlap": sim_idf_overlap,
    "tfidf_cosine": sim_tfidf_cosine,
    "bm25_k1.2_b.75": sim_bm25,
    "bm25_k1.5_b.50": sim_bm25_b50,
    "char3_jaccard": sim_char3_jaccard,
    "char4_jaccard": sim_char4_jaccard,
    "char4_tfidf": sim_char4_tfidf,
    "rare_token_overlap": sim_rare_token_overlap,
}
SIMILARITIES["dice.7+char4.3"] = _mix("idf_dice", "char4_jaccard", 0.7)
SIMILARITIES["dice.7+bm25.3"] = _mix("idf_dice", "bm25_k1.2_b.75", 0.7)
SIMILARITIES["dice.5+overlap.5"] = _mix("idf_dice", "idf_overlap", 0.5)


# --------------------------------------------------------------------------
# Engagement label recovery
# --------------------------------------------------------------------------

_EVENT_FIELDS = (
    "message_opened",
    "message_replied",
    "reaction_time_minutes",
    "notification_dismissed",
    "muted_after_message",
    "message_reported",
)


def label_for_event(event: Optional[dict]) -> Optional[str]:
    """Routing label encoded by one message_events.csv row, or None.

    Local mirror of features.label_for_event so retrieval never imports the
    feature layer at module scope; both read the same ENGAGEMENT_SIGNATURES
    table from schema.py, so they cannot drift.
    """
    if not event:
        return None
    key = tuple(str(event.get(f, "") or "").strip() for f in _EVENT_FIELDS)
    return ENGAGEMENT_SIGNATURES.get(key)


def _label(ds: Dataset, hist: Message) -> Optional[str]:
    return label_for_event(ds.events.get((hist.user_id, hist.message_id)))


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------

def _relationship_tier(msg: Message, hist: Message) -> float:
    tier = 0.0
    src = msg.source
    if src and src == hist.source:
        tier += TIER_SAME_SOURCE
    if msg.conversation_type and msg.conversation_type == hist.conversation_type:
        tier += TIER_SAME_CONVERSATION
    if msg.group_id and msg.group_id == hist.group_id:
        tier += TIER_SAME_GROUP
    return tier


def _age_days(msg: Message, hist: Message) -> float:
    a, b = _parse_time(msg.created_at), _parse_time(hist.created_at)
    if a is None or b is None:
        return 0.0
    return (a - b).total_seconds() / 86400.0


def pool_for(ds: Dataset, msg: Message) -> List[Message]:
    """Mandatory, lossless same-user restriction."""
    return [h for h in ds.history
            if h.user_id == msg.user_id and h.message_id != msg.message_id]


def _score_text(idx: Index, sim: Callable[[Index, str, str], float],
                msg: Message, hist: Message) -> float:
    # An explicit "shared host/domain" bonus was measured and is exactly
    # neutral here (0.00 / 0.05 / 0.15 all give the same table): the tokenizer
    # already keeps "account-login.in" atomic, and such a host has near-maximal
    # IDF, so the similarity term picks it up on its own.  Not kept.
    return sim(idx, msg.message_text, hist.message_text)


def _score_media(msg: Message, hist: Message, tier: float) -> float:
    """Scoring path for messages with no usable text (voice notes)."""
    if msg.media_id and msg.media_id == hist.media_id:
        return MEDIA_ID_MATCH_SCORE
    if tier <= 0.0:
        return MEDIA_NO_RELATION_SCORE
    return MEDIA_RELATION_BASE + MEDIA_RELATION_SPAN * (tier / TIER_MAX)


def rank(ds: Dataset, msg: Message,
         similarity: str = DEFAULT_SIMILARITY,
         use_tiers: bool = True,
         use_recency: bool = True,
         _tiebreak_seed: Optional[int] = None) -> List[Candidate]:
    """Full ranking of the same-user pool, best first.  Deterministic.

    `_tiebreak_seed` is a diagnostic only: it replaces the final message_id key
    with a seeded pseudo-random one so the harness can report how much of the
    dev score survives without any id ordering at all.  Never set in production.
    """
    pool = pool_for(ds, msg)
    if not pool:
        return []

    idx = index_for(ds)
    sim = SIMILARITIES[similarity]
    # "Has text" means "has at least one token": a caption of pure punctuation
    # carries no more signal than a voice note, so it takes the media path too.
    has_text = bool(idx.tokens(msg.message_text))

    keyed: List[Tuple[Tuple[float, float, float, str], Candidate]] = []
    for hist in pool:
        tier = _relationship_tier(msg, hist)
        if has_text:
            score = _score_text(idx, sim, msg, hist)
        else:
            score = _score_media(msg, hist, tier)

        # Substantive matches: the earliest receipt established the precedent;
        # later ones are re-sends of it.  Weak/topical matches: the freshest
        # contact with that person is the better context.
        direction = -1.0 if score >= SUBSTANTIVE_MATCH_SCORE else 1.0
        age = _age_days(msg, hist) if use_recency else 0.0

        if _tiebreak_seed is None:
            last_key = hist.message_id       # last resort, determinism only
        else:
            last_key = hashlib.md5(
                f"{_tiebreak_seed}:{hist.message_id}".encode()).hexdigest()

        sort_key = (
            -round(score, 9),
            -(tier if use_tiers else 0.0),
            direction * age,
            last_key,
        )
        keyed.append((sort_key, Candidate(
            message=hist,
            score=round(score, 6),
            label=_label(ds, hist),
            same_source=bool(msg.source) and msg.source == hist.source,
            same_group=bool(msg.group_id) and msg.group_id == hist.group_id,
        )))

    keyed.sort(key=lambda item: item[0])
    return [cand for _, cand in keyed]


def candidates(ds: Dataset, msg: Message, k: int = 5) -> List[Candidate]:
    """Top-k historical precedents for `msg`, best first.

    The pool is restricted to history addressed to the same user (lossless on
    the dev set) and ranked by IDF-weighted Dice similarity, then relationship
    tiers, then recency.  See the module docstring for why each step is there.
    """
    if k <= 0:
        return []
    return rank(ds, msg)[:k]


def select_evidence(cands: Sequence[Candidate], limit: int = 1) -> List[str]:
    """Ids to cite in `evidence_message_ids`.  [] means "none".

    Cites the top candidate when it clears EVIDENCE_FLOOR, and a second only
    when that second is effectively tied with the first (the corpus contains
    verbatim duplicate pairs, and the 3 dev rows that cite 2 ids are exactly
    those).  Everything else is left uncited rather than guessed.
    """
    if not cands or limit <= 0:
        return []
    best = cands[0]
    if best.score < EVIDENCE_FLOOR:
        return []
    chosen = [best.message.message_id]
    if limit > 1 and best.score > 0.0:
        for cand in cands[1:]:
            if len(chosen) >= limit:
                break
            if cand.score >= EVIDENCE_FLOOR and cand.score >= best.score * SECOND_EVIDENCE_RATIO:
                chosen.append(cand.message.message_id)
            else:
                break
    return chosen


# ==========================================================================
# Reproducible evaluation against dataset/sample_messages.csv
# ==========================================================================

def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_dataset_dir(dataset_dir: Optional[str] = None) -> str:
    if dataset_dir and os.path.isdir(dataset_dir):
        return dataset_dir
    env = os.environ.get("DATASET_DIR")
    if env and os.path.isdir(env):
        return env
    for cand in ("dataset", os.path.join(_repo_root(), "dataset")):
        if os.path.isdir(cand):
            return cand
    raise SystemExit("dataset/ not found; set DATASET_DIR")


def _read_csv(path: str) -> List[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _to_message(row: dict) -> Message:
    return Message(
        message_id=row.get("message_id", ""),
        user_id=row.get("user_id", ""),
        conversation_type=row.get("conversation_type", ""),
        group_id=row.get("group_id", "") or "",
        business_id=row.get("business_id", "") or "",
        sender_user_id=row.get("sender_user_id", "") or "",
        created_at=row.get("created_at", ""),
        message_text=row.get("message_text", "") or "",
        media_type=row.get("media_type", "") or "",
        media_id=row.get("media_id", "") or "",
        forwarded_count=row.get("forwarded_count", "") or "0",
    )


def _local_dataset(dataset_dir: str) -> Tuple[Dataset, List[dict]]:
    """Minimal loader so this module is testable without features.py.

    Production code should use features.load_dataset(); only the fields this
    module touches are populated here.
    """
    p = lambda name: os.path.join(dataset_dir, name)
    messages = [_to_message(r) for r in _read_csv(p("messages.csv"))]
    history = [_to_message(r) for r in _read_csv(p("message_history.csv"))]
    sample_rows = _read_csv(p("sample_messages.csv"))
    events = {(r["user_id"], r["message_id"]): r for r in _read_csv(p("message_events.csv"))}
    ds = Dataset(
        messages=messages,
        history=history,
        samples=sample_rows,
        events=events,
        users={}, groups={}, group_members={}, businesses={},
        user_business={}, daily_load={}, images={}, voice_notes={},
    )
    return ds, sample_rows


def _gold(row: dict) -> List[str]:
    raw = (row.get("evidence_message_ids") or "").strip()
    if not raw or raw.lower() == "none":
        return []
    return [x.strip() for x in raw.split(";") if x.strip()]


def _score_variant(ds: Dataset, solved: List[dict], similarity: str,
                   use_tiers: bool, use_recency: bool,
                   seed: Optional[int] = None) -> dict:
    top1 = r3 = r5 = 0
    scored = 0
    misses: List[Tuple[str, str, List[str]]] = []
    for row in solved:
        gold = _gold(row)
        if not gold:
            continue
        scored += 1
        ranked = rank(ds, _to_message(row), similarity, use_tiers, use_recency, seed)
        ids = [c.message.message_id for c in ranked]
        gset = set(gold)
        if ids and ids[0] in gset:
            top1 += 1
        else:
            misses.append((row["message_id"], ids[0] if ids else "-", gold))
        if gset & set(ids[:3]):
            r3 += 1
        if gset & set(ids[:5]):
            r5 += 1
    return {"top1": top1, "r3": r3, "r5": r5, "n": scored, "misses": misses}


def _evidence_report(ds: Dataset, solved: List[dict], limit: int = 1) -> dict:
    exact = 0
    cited_when_none = 0
    none_when_cited = 0
    any_hit = 0
    for row in solved:
        gold = _gold(row)
        picked = select_evidence(candidates(ds, _to_message(row), k=5), limit=limit)
        if not gold and picked:
            cited_when_none += 1
        if gold and not picked:
            none_when_cited += 1
        if (picked and picked[0] in set(gold)) or (not picked and not gold):
            exact += 1
        if set(picked) & set(gold):
            any_hit += 1
    return {
        "rows": len(solved),
        "correct": exact,
        "any_hit": any_hit,
        "cited_when_none": cited_when_none,
        "none_when_cited": none_when_cited,
        "avg_cited": sum(len(select_evidence(candidates(ds, _to_message(r), 5), limit))
                         for r in solved) / max(1, len(solved)),
    }


def main() -> int:
    dataset_dir = _resolve_dataset_dir(sys.argv[1] if len(sys.argv) > 1 else None)
    ds, sample_rows = _local_dataset(dataset_dir)
    solved = [r for r in sample_rows if (r.get("action") or "").strip()]
    n_ev = sum(1 for r in solved if _gold(r))

    print(f"dataset      : {dataset_dir}")
    print(f"solved rows  : {len(solved)}   (rows citing evidence: {n_ev})")
    print(f"history rows : {len(ds.history)}   messages: {len(ds.messages)}")

    pools = [len(pool_for(ds, _to_message(r))) for r in solved]
    print(f"same-user pool: min={min(pools)} max={max(pools)} mean={sum(pools)/len(pools):.1f}")

    print("\n-- similarity function (with relationship tiers + recency) "
          "-------------------")
    print(f"{'ranker':26s} {'top-1':>9s} {'recall@3':>9s} {'recall@5':>9s}")
    rows = []
    for name in SIMILARITIES:
        res = _score_variant(ds, solved, name, True, True)
        rows.append((res["top1"], name, res))
        print(f"{name:26s} {res['top1']:>4d}/{res['n']:<4d} "
              f"{res['r3']:>4d}/{res['n']:<4d} {res['r5']:>4d}/{res['n']:<4d}")

    print("\n-- ablation on the winning similarity "
          "----------------------------------")
    print(f"{'configuration':26s} {'top-1':>9s} {'recall@3':>9s} {'recall@5':>9s}")
    for label, tiers, recency in (
        ("full", True, True),
        ("no recency key", True, False),
        ("no relationship tiers", False, True),
        ("similarity only", False, False),
    ):
        res = _score_variant(ds, solved, DEFAULT_SIMILARITY, tiers, recency)
        print(f"{label:26s} {res['top1']:>4d}/{res['n']:<4d} "
              f"{res['r3']:>4d}/{res['n']:<4d} {res['r5']:>4d}/{res['n']:<4d}")

    print("\n-- how much of that is the message_id last key? "
          "------------------------")
    print("   The dev gold ids happen to be low-numbered, so an ascending id")
    print("   tiebreak flatters any ranker.  Re-scored with the final key")
    print("   replaced by a seeded random order (60 seeds), the id effect goes")
    print("   away and only the content + relationship + recency signal is left.")
    for label, tiers, recency in (
        ("full", True, True),
        ("similarity only", False, False),
    ):
        vals = [_score_variant(ds, solved, DEFAULT_SIMILARITY, tiers, recency, s)["top1"]
                for s in range(60)]
        n = _score_variant(ds, solved, DEFAULT_SIMILARITY, tiers, recency)["n"]
        print(f"   {label:20s} top-1 mean {sum(vals)/len(vals):5.2f}/{n}"
              f"   (min {min(vals)}, max {max(vals)})")

    best = _score_variant(ds, solved, DEFAULT_SIMILARITY, True, True)
    print(f"\n-- top-1 misses for '{DEFAULT_SIMILARITY}' "
          "------------------------------------")
    for mid, got, gold in best["misses"]:
        print(f"  {mid:16s} picked {got:14s} gold {';'.join(gold)}")

    print("\n-- SUBSTANTIVE_MATCH_SCORE sweep (recency direction switch) "
          "--------------")
    original = globals()["SUBSTANTIVE_MATCH_SCORE"]
    line = []
    for thr in [x / 20.0 for x in range(0, 21)]:
        globals()["SUBSTANTIVE_MATCH_SCORE"] = thr
        line.append((thr, _score_variant(ds, solved, DEFAULT_SIMILARITY, True, True)["top1"]))
    globals()["SUBSTANTIVE_MATCH_SCORE"] = original
    print("   " + "  ".join(f"{t:.2f}:{v}" for t, v in line[:11]))
    print("   " + "  ".join(f"{t:.2f}:{v}" for t, v in line[11:]))
    print(f"   in use: {original} (flat optimum 0.30-0.40)")

    for limit in (1, 2):
        ev = _evidence_report(ds, solved, limit)
        print(f"\n-- select_evidence() end to end (limit={limit}) "
              "------------------------------")
        print(f"  first id correct / 'none' agreed : {ev['correct']}/{ev['rows']}")
        print(f"  any cited id is gold             : {ev['any_hit']}/{ev['rows']}")
        print(f"  cited when gold was none         : {ev['cited_when_none']}")
        print(f"  said none when gold cited        : {ev['none_when_cited']}")
        print(f"  mean ids cited                   : {ev['avg_cited']:.2f}"
              f"   (gold mean {sum(len(_gold(r)) for r in solved)/len(solved):.2f})")

    # Determinism check: two runs must agree byte for byte.
    a = [select_evidence(candidates(ds, _to_message(r), 5), 1) for r in solved]
    _INDEX_CACHE.clear()
    b = [select_evidence(candidates(ds, _to_message(r), 5), 1) for r in solved]
    print(f"\ndeterministic across runs      : {a == b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
