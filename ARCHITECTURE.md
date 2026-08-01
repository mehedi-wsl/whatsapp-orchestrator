# Architecture — Message Notification Router

How this system decides `notify` / `digest` / `mute` for every row in `dataset/messages.csv`, and why it is built this way.

This document is the design record. Every non-obvious choice below is tied to a measurement on the provided data, and where a plausible-sounding rule was tested and **failed**, that is recorded too — those are the expensive mistakes to not re-make.

---

## 1. The Shape of the Problem

| Quantity | Value |
|---|---|
| Messages to route | **110** |
| Solved examples (`sample_messages.csv`) | **30** |
| Historical messages | **412** |
| Historical engagement events | **412** (1:1 with history) |
| Conversation mix | group 63 / business 30 / personal 17 |
| Media mix | text 87 / image 15 / voice 8 |
| Distinct users receiving | 32 |
| Date range | 2026-07-18 → 2026-07-31 |

Two facts dominate the design:

1. **Only 30 labeled rows exist.** That is enough to catch gross errors and to calibrate output conventions. It is *not* enough to tune fine-grained thresholds without overfitting. The system is therefore built as explicit policy with a small number of hand-justified parameters, not as a fitted model.
2. **~21% of the scored set is media** (23 of 110 messages are image or voice). Their `message_text` is empty or uninformative. Any approach that ignores the media files forfeits accuracy on roughly a fifth of the set.

### Output contract (non-negotiable)

```text
message_id,action,message_type,reason,confidence,evidence_message_ids
```

Exactly one row per `message_id`, exact column order, `none` when no useful evidence exists.

---

## 2. The Central Discovery: Engagement Signatures

`message_events.csv` has 412 rows but only **5 distinct value-tuples** across
`(opened, replied, reaction_time_minutes, dismissed, muted_after, reported)`:

| Signature | Count | Reads as |
|---|---|---|
| `1,1,2,0,0,0` | 153 | opened + replied fast → **notify**-like |
| `1,0,120,0,0,0` | 110 | opened late, no reply → **digest**-like |
| `0,0,,1,1,0` | 79 | dismissed + muted → **mute**-like |
| `0,0,,1,1,1` | 55 | dismissed + muted + **reported** → **mute** (abuse) |
| `1,0,9,0,0,0` | 15 | opened fast, no reply, not dismissed → **mute** (scam-curiosity) |

This is not natural user behavior — it is a generated label channel. Each historical message carries a recoverable routing label. That converts the task from "classify cold" into "**retrieve the right precedent, then read its label**," which is a far easier problem and also happens to be exactly what `evidence_message_ids` is scored on.

**This is the single highest-value signal in the dataset.** The rest of the architecture exists to retrieve against it correctly and to override it when safety demands.

> The 5th signature is the subtle one. `1,0,9,...` looks like mild engagement — opened quickly, not dismissed. It maps to **mute**. The interpretation that fits: the user opened it *because* it was alarming (a scam hook), then did nothing. Fast-open-no-reply is a risk tell, not an interest tell. A naive "opened = interested" heuristic gets these 15 exactly backwards.

---

## 3. The Retrieval Key: Relationship, Not Reputation

The obvious move is to build a sender reputation score and use it as a prior. **Measured, that is close to useless.** Keying historical labels four different ways and asking "for each of the 110 messages, are that key's historical labels unanimous?":

| Key | No history | Unanimous | **Mixed** |
|---|---|---|---|
| sender / business (global) | 3 | 26 | **81** |
| group | 47 | 5 | **58** |
| **(user, sender) — the relationship** | **6** | **79** | **25** |
| (user, group) | 47 | 36 | 27 |

Global sender reputation is mixed for **81 of 110** messages — the same sender is notify-worthy for one recipient and mute-worthy for another. That is the whole point of the problem statement's "the routing decision must be personalized to the receiving user."

The **(user, sender) pair is unanimous for 79 of 110** with only 6 uncovered. That pair is the primary retrieval key.

**Design consequence:** reputation is a *feature*, never a *gate*. The unit of personalization is the relationship, not the sender.

---

## 4. Rules That Sounded Right and Failed

Recorded because each was proposed in good faith and disproved by the data.

| Proposed rule | Counter-evidence | Resolution |
|---|---|---|
| Domain mismatch (`official_domain` ≠ `domain_used_by_sender`) ⇒ hard `mute` as spoofing | A solved row shows a **verified** business with a **spoofed** sender domain labeled `digest`/`promotion` | Mismatch became a contributing risk feature, not a gate |
| A direct `@mention` re-promotes over a group mute | `group_001` (muted by both u_007 and u_001) holds `msg_040` `@u_007 forward this to ten people for blessings` (→mute) and `msg_056` `@u_001 doctor appointment moved to 6 PM` (→notify). Identical structure, opposite answers | Precedence cannot resolve this. Content classification must emit urgency and chain-likeness as separate features |
| `forwarded_count ≥ 6` is a cheap strong spam signal | Same bucket holds legit urgent content: fwd=11 water tanker, fwd=7 fire alarm, fwd=11 family personal | **Dropped entirely** (user decision). Chain-*instruction language* is retained; the counter is not |
| Marketplace groups are duplicate-heavy ⇒ pure personalization play | Actually 16 distinct texts across 17 messages (one duplicate pair). Marketplace is a scam **delivery vector** | No marketplace rule |
| Urgent media from family/friends/groups ⇒ `notify`/`urgent` | No family/friends media exists (all 23 media rows are group or business, 0 personal). Two "HDFC Bank" voice notes — one legitimate, one 20-day-old phisher — both sound urgent | Media traverses the same six-layer stack; **no promotion path** (§8) |
| Prioritise voice notes from family | Also an empty set: 0 of 8 voice notes come from family/friends groups or personal chats; all 11 family-group and all 17 personal messages are text | Replaced by `(user, sender)` precedent, which is unanimous for **all 8** voice notes (§8) |
| `verified` flag separates safe from unsafe businesses | business_092 (Thrillophilia) → `link.wame.pro` and business_095 (Polaris) → `weurl.co` are **verified spoofers**; business_032 (Green Cross Pharmacy) is **unverified but legit** at 420 days old with 0 reports | `verified` is weak. The composite in §5 is what separates |

---

## 5. Safety: The Permanent Guard

Per explicit direction, safety is an **unconditional top layer** — it runs before personalization and cannot be overridden by engagement history. A user who habitually opens messages from a scam sender must still not be interrupted by one.

**What actually separates scam accounts** is a composite, not any single flag:

```text
official_domain is non-empty
  AND domain_used_by_sender != official_domain
  AND verified == 0
  AND account_age_days <= 60
```

This isolates **21 of 110 business accounts**, firing on exactly **7 of the 110 messages**
(`msg_019`, `msg_026`, `msg_036`, `msg_052`, `msg_076`, `msg_085`, `msg_108`).

> Corrected during implementation: an earlier draft said 24 accounts. 24 is the count *before*
> the blank-domain guard below is applied — which the same section already required, so the
> figure was internally inconsistent. Measured value with the guard applied is **21**.

The cluster is brand impersonation: real brand names paired with lookalike domains, all
unverified, all 20–35 days old — `paytm.com → paytm-kyc.in`, `sbi.bank.in → sbireward.in`,
`icicibank.com → icici-secure.net`, `flipkart.com → flipkart-refund.in`, and 20 more.

`domain_used_by_sender_age_days` corroborates sharply: the caught senders' domains are
**2–17 days old**. A corporate domain registered two days ago is not a corporate domain.

### Two guards this rule needs

Both are real rows in the data, and both would be false positives without the guard:

| Guard | Case |
|---|---|
| Require non-empty `official_domain` | **business_032** Green Cross Pharmacy — blank official domain, so string-comparison "mismatch" is spurious. Legit: 420 days old, unverified |
| `verified` + old account + old sender domain ⇒ **link shortener**, not spoofing | **business_092** Thrillophilia → `link.wame.pro` (domain 3368 d) and **business_095** Polaris → `weurl.co` (domain 3455 d). Both verified, both ~4300 days old. A solved row confirms this pattern is `digest`/`promotion`, **not** mute |

> **Superseded (recorded so it is not reintroduced):** an earlier version of this composite was
> `brand_name == "Unknown" AND account_age 12–36 AND reports 16–29`. It was wrong twice over — it
> referenced a column named `reports_count` that **does not exist** (the real column is
> `user_reports_30d`), and it caught only 4 accounts while missing the entire 21-account
> impersonation cluster above. Impersonators use a **real** brand name; that is the whole point of
> impersonation. Keying on `brand_name == "Unknown"` looks for the one thing a competent
> impersonator never does.

Contributing (non-gating) risk features: fresh sender domain, URL shorteners,
payment-credential solicitation, chain-forward instruction language.

### Prompt injection

Five messages (`msg_095`, `msg_107`, `msg_108`, `msg_109`, `msg_110`) contain instructions aimed at the routing system itself. Handling:

- Message text is passed to any model as **data inside a delimited block, never as instructions**.
- The classifier returns a **constrained schema** (enum action, enum type, bounded confidence). Free-form model output can never become a routing decision directly.
- Injection attempts are themselves a positive signal for `mute` / `scam`.

This is worth real care: it is a scored functional requirement, and it is also the failure mode that looks worst in review.

---

## 6. Decision Stack

Six layers, evaluated in order. First layer to produce a decision wins.

```text
┌─ 1. SAFETY GUARD ─────────────────────────────────────────┐
│  scam composite · injection attempt · credential          │  → mute / scam|spam
│  solicitation.  Unconditional. Never overridden.          │
├─ 2. HARD USER STATE ──────────────────────────────────────┤
│  explicit opt-out · blocked sender                        │  → mute
├─ 3. URGENCY ──────────────────────────────────────────────┤
│  deadline/emergency language, scored against the          │  → notify / urgent
│  message's OWN created_at, not wall-clock                 │
├─ 4. RELATIONSHIP PRECEDENT ───────────────────────────────┤
│  (user, sender) retrieval → engagement signature label    │  → notify|digest|mute
│  Covers 79/110 unanimously                                │
├─ 5. CONTENT CLASS ────────────────────────────────────────┤
│  promotion/greeting/forward → digest or mute by           │  → digest / mute
│  relationship strength                                    │
└─ 6. DEFAULT ──────────────────────────────────────────────┘
   digest — the safe middle. Never default to notify.
```

**Why `digest` is the default:** the three actions have asymmetric costs. A wrong `notify` is an unwanted interruption; a wrong `mute` loses a message the user needed. `digest` is wrong-but-recoverable in both directions. With only 30 labeled rows, the uncertain mass belongs in the middle.

**Deadline urgency is relative to each message's own `created_at`.** The data spans 2026-07-18 → 2026-07-31 and is being scored well after; "within this week" must mean the week of the message, not of the run. This also keeps behavior deterministic across runs, per the §6.3 constraint.

---

## 7. Evidence Selection — The Weakest Link

`evidence_message_ids` is scored separately. Current backtest: **18/28 correct at top-1** (recall@3 24/28) — still the weakest axis, against action accuracy of **26/30 rules-only** and **28/30 with the model layer** on the solved rows.

An earlier draft of this section quoted 13/28 and 26/28. Both were stale: the first predated the retrieval rewrite, and the second used the wrong denominator — action is scored over all 30 solved rows, evidence over the 28 that carry a gold citation.

Action accuracy survives the weak retrieval only because sources tend to be label-consistent — the system often picks the *wrong* precedent from the *right* source and still lands the right action. **That is luck, and it does not transfer to the evidence score.**

Observed conventions from the 30 solved rows: 25 rows use exactly 1 evidence ID, 3 use 2, 2 use `none`. So the target is a strong top-1 with a sparing second.

Planned retrieval: `(user, sender)` restriction first, then lexical + normalized-text similarity within that candidate set, then recency tiebreak. **This is the item that most deserves remaining time**, and it can be developed against the 87 text messages without any media tooling.

---

## 8. Media Handling

`images.csv` and `voice_notes.csv` provide **paths only** — the README is explicit that the system should inspect the files themselves. Confirmed real: 20 JPEGs (JFIF, up to 1.9 MB) and 13 MP3s (ID3), 12 MB total.

**Current environment gap:** no OCR/ASR tooling and no model API key is configured. `numpy` is present; `pip3` and network are available.

The pipeline is therefore built with a **pluggable media backend and an on-disk transcript cache**:

```text
media file ──▶ [backend] ──▶ transcript ──▶ cache/media_transcripts.json ──▶ normalize ──▶ retrieval
                   │
                   ├─ metadata-only  (no dependencies — degraded)
                   ├─ OCR / ASR      (local tooling)
                   └─ model API      (key from environment only)
```

The cache is what makes this safe: transcription runs once, results are committed as data, and every later run is deterministic and offline. Backend choice changes accuracy, never the contract.

### Media does NOT get a promotion path

Media transcripts are normalized into the **same** text pipeline and traverse the **identical**
six-layer stack. Reading the media changes only what layer 3 can *see*; it never grants an
earlier or more permissive route. There is no media-specific escalation rule.

This was tested as a proposal — "urgent media from family/friends/groups ⇒ urgent" — and the
data rejects it on three counts:

**1. It targets an empty set.** All 23 media messages are group (15) or business (8).
**Zero are personal**, and **none** come from the `family`, `extended_family`, or `friends`
group types. The 15 group media are: marketplace 5, coworker 3, real_estate 2, school_group 2,
college_faculty 1, investment_tips 1, society 1.

**2. Media skews toward the scam vectors.** marketplace + real_estate + investment_tips
account for 8 of the 15 group media messages.

**3. The decisive pair — two voice notes, same claimed brand, opposite answers:**

| Msg | Sender | Verified | Age | Domain | Verdict |
|---|---|---|---|---|---|
| `msg_084` | business_002 "HDFC Bank" | ✅ 1 | 974 d | `hdfc.bank.in` = official | legitimate |
| `msg_085` | business_033 "HDFC Bank" | ❌ 0 | **20 d** | `hdfcbank-kyc.in` ≠ `hdfc.bank.in` | **KYC phishing** |

Both are bank voice notes; both will *sound* urgent, because that is what bank audio sounds
like. A rule that promotes urgent-sounding audio escalates the phishing one to `notify`.

**Urgency is the scam's primary instrument, not a safety signal.** `msg_064`: *"Verify wallet
and card details before midnight or refund processing will close tonight."* `msg_074`: *"Pay Rs
11,000 token today to block 1200 sqft."* Treating urgency as a promoter inverts the defense —
which is precisely why the safety guard is layer 1 and urgency is layer 3.

**What is kept from the proposal:** media must genuinely be read — an empty `message_text` must
not make content invisible. Real urgent media does exist (`msg_062`, society fire-alarm notice;
`msg_031`, coworker deployment sync). It simply does not come from family or friends in this
dataset. Absent a transcript, media falls to layer 6 (`digest`) — never to `notify`.

### Media routing procedure (resolved)

Images and voice notes are **different problems** and are handled separately. The split is not
a judgement call — it is categorical in the data:

| | Count | Caption present |
|---|---|---|
| Images (the 110) | 15 | **15 / 15** |
| Voice notes (the 110) | 8 | **0 / 8** |
| Images (history) | 19 | 19 / 19 |
| Voice notes (history) | 4 | 0 / 4 |

**Images → route on the caption.** Every image carries usable text (`msg_062` "Fire alarm test
tomorrow 9 AM to 11 AM"; `msg_074` "Pay Rs 11,000 token today to block 1200 sqft"). Images enter
the ordinary text pipeline unchanged. OCR is enrichment, never a prerequisite.

**Voice notes → route on relationship precedent.** All 8 have **unanimous** `(user, sender)`
history, each with an evidence ID:

| Message | User | Sender | History | Action |
|---|---|---|---|---|
| `msg_082` | u_028 | u_046 (coworker) | 3/3 | notify |
| `msg_081` | u_001 | u_045 (school) | 1/1 | notify |
| `msg_086` | u_004 | Thrillophilia | 1/1 | notify |
| `msg_083` | u_029 | u_046 (coworker) | 1/1 | digest |
| `msg_088` | u_033 | u_048 (marketplace) | 7/7 | mute |
| `msg_087` | u_040 | u_052 (real estate) | 8/8 | mute |
| `msg_085` | u_009 | HDFC Bank (phisher) | 1/1 | mute |
| `msg_084` | u_040 | HDFC Bank (legitimate) | 1/1 | mute |

`msg_082` and `msg_083` share sender `u_046` and land on **opposite actions** for different
recipients — the personalization requirement in its purest form, and the reason a category label
like "family" would be too coarse even if the dataset contained one. What matters is not what the
relationship is *called* but how this user has actually treated this sender.

`msg_084` is worth noting separately: HDFC Bank, verified, 974 days old, correct domain —
entirely legitimate, and u_040 mutes it anyway. **Legitimacy is not importance.**

### The action / type split

Precedent yields `action` for all 8 voice notes with no ASR. It **cannot** yield `message_type` —
nothing in an engagement signature distinguishes `event` from `payment` from `promotion`, and type
is a separately scored axis.

```text
action        ← relationship precedent   (all 23 media rows covered, no tooling)
message_type  ← content (caption / OCR / ASR)
safety        ← layer 1, above both — msg_085 is muted on sender metadata alone
```

Content therefore refines the **type**; precedent decides the **action**. The payoff is
containment: if ASR is unavailable or wrong, type accuracy degrades and routing is untouched.

### `media_id` as a retrieval key

**16 of 23 media messages reuse a file that already appears in `message_history.csv` with a
label, and all 16 agree.** Six are same-user matches (`msg_005`→`message_0401`,
`msg_062`→`message_0410`, `msg_077`→`message_0403`, …). This is free, deterministic evidence for
`evidence_message_ids` — directly addressing the weakest scored axis (§7).

**It is capped at layer 4 and is never a safety override.** `msg_064` uses `img_002`, whose
history label is `notify`; its caption reads *"Verify wallet and card details before midnight."*
`img_008` is shared across `msg_005`, `msg_029`, and `msg_030` with different senders.
**Same file ≠ same intent.**

**Net:** all 23 media messages route deterministically with **zero tooling**. ASR over the 5
residual voice notes (`msg_086`, `msg_083`, `msg_082`, `msg_081`, `msg_084`) becomes a
type-accuracy upgrade rather than a blocker.

---

## 9. Build Strategy: End-to-End First, Degraded

The pipeline is built **complete, with degraded backends**, before any backend is upgraded. Rule-based classification stands in for the model; media routes on metadata and sender context until transcripts exist.

Two reasons this beats waiting for tooling:

1. **A valid 110-row `output.csv` exists from the first pass.** In a fixed-length window, never being in a state where you'd submit nothing is worth more than any single accuracy improvement.
2. **Every upgrade becomes measurable.** With the backtest harness in place, the value of OCR, of the model classifier over rules, and of each retrieval change can be quantified against the 30 solved rows instead of guessed at.

Only two things are genuinely hard-blocked, and both are *runtime*, not code: **executing** OCR/ASR, and **executing** the model classifier. Everything else — feature extraction, retrieval, decision layers, banding, the writer, packaging — is buildable now.

---

## 10. Output Conventions

**Confidence bands**, calibrated to the solved rows (observed range 0.78–0.91 overall):

| Action | Observed band | n |
|---|---|---|
| `notify` | 0.85 – 0.91 | 9 |
| `mute` | 0.81 – 0.87 | 10 |
| `digest` | 0.78 – 0.84 | 11 |

Confidence is emitted **within the band for the chosen action**, positioned by evidence strength — high on unanimous relationship precedent or a fired safety gate, low on default-digest with no precedent. Calibration is a scored criterion, so the number must move with actual certainty rather than being a constant.

**`reason`** is one short human-readable clause naming the deciding factor, in the register of the solved rows.

**Determinism** (§6.3) is enforced by the transcript cache, fixed tiebreak ordering in retrieval, and temperature-0 with cached responses for any model call.

---

## 11. The Local Model Layer

The rule layer answers "which gate fires." It cannot answer "what is this message about," and every remaining miss is of the second kind. `code/llm.py` adds that judgment and nothing else.

### Why a local model and not a free API tier

No API key exists in this environment, and a hosted free tier makes the submission depend on a key the grader does not have and a rate limit nobody controls. A local `llama.cpp` server with Qwen2.5-3B-Instruct (Q4_K_M, ~1.9 GB) needs **no key at all**, which also means §6.3's "read secrets from environment variables only" is satisfied vacuously — there is no secret. Responses are cached to `code/llm_cache.json`, so a run reproduces exactly on a machine that never downloads the weights.

### What the model is allowed to do

| Job | Scope | Constraint |
|---|---|---|
| `message_type` adjudication | Only rows where ≥2 lexical patterns collide | GBNF-limited to the colliding options |
| `is_directed_request` | One boolean, consumed only by gates 3–4 | GBNF-limited to `yes`/`no` |
| `reason` prose | Non-safety rows | GBNF-limited to one bounded sentence |

**The model cannot set `action`.** It never runs on a row the safety gate decided, so injected text never reaches a model whose output is trusted. Because every answer is grammar-constrained, a successful injection can at most flip one tiebreak boolean or one type label on a row that already passed safety — it cannot widen the output space.

### Three findings worth recording

**Thread count dominated everything.** Generation ran at **0.23 tok/s** until the cause was found: this host is an Intel Core Ultra 7 155H, and `-t 20` schedules work across E-cores that stall every sync. `-t 6` (P-cores only) gives **8.66 tok/s** — a 36× improvement from one flag, on the same weights.

**The anti-injection preamble destroyed the task.** Opening each prompt with a paragraph explaining that the fenced text was untrusted made the 3B answer `no` to all seven `is_directed_request` probes, including *"when you get 5 mins can you call?"*. Removing it recovered the judgment. A small model spends its limited attention on whatever you put first. The defence that survives is structural (gate ordering + grammar constraints), not a written instruction — which is the more reliable defence anyway.

**Narrow the choice, not the definitions.** Offering all 11 types made the model drift on rows the regex already had right. Offering only the 2 colliding options fixed `sample_msg_007` (`event` → `promotion`, a travel advert containing the word "itinerary") and `sample_msg_048` (`payment` → `business_update`, an advisory saying the brand *never* asks for payment details).

### What it does not fix

`sample_msg_042` is a voice note with empty text, gold `notify/urgent`. No amount of reasoning helps — 8 of the 110 messages have no text at all, and they need ASR, not a language model. `sample_msg_002` (`urgent` vs `event` for a same-day bus change) is a genuine taxonomy judgment the model gets wrong and a human might too.

One miss was fixed with **no model at all**: gold labels a stranger's message `unknown`, not `personal`, so `type_candidates` now keys that on whether any precedent exists. That took message_type from 83.3% to 86.7% before the first model call.

---

## 12. Component Map

| # | Component | Depends on | Notes |
|---|---|---|---|
| 1 | Feature extraction — reputation, business risk, relationship, group/mute, DND, load | — | Structured CSV only |
| 2 | Validation harness — backtest vs 30 solved rows | — | Build early; gates every later change |
| 3 | Media resolution + cache | backend choice *(runtime only)* | Pluggable, degrades cleanly |
| 4 | Text normalization (Hinglish, French → gloss) | 3 for media rows | Retrieval key, **not** a classification aid |
| 5 | **Retrieval + evidence selection** | 4 | **Highest-risk item.** Works on 87 text rows today |
| 6 | Content classifier (11-class, injection-hardened) | key *(runtime only)* | Rule-based fallback ships first |
| 7 | Decision stack (§6) | 1, 5, 6 | Pure policy |
| 8 | Reason + confidence banding | 7 | Constants known (§10) |
| 9 | Output writer | 8 | Exact columns, 110 rows |
| 10 | Packaging — README, run instructions | 9 | Incremental |

---

## 13. Open Questions

| Question | Status | Current default |
|---|---|---|
| Media `action` routing | **Resolved** — captions + precedent cover all 23 with no tooling (§8) | — |
| Media backend (for `message_type` only) | Open — ASR would refine type on 5 voice notes | Type from sender/context; routing unaffected |
| Media routing conventions | **Unvalidated** — 0 of 30 solved rows are media; affects 23 of 110 | Same six-layer stack as text; no promotion path (§8) |
| Quiet-hours / DND policy | **Unvalidated** — 0 of 30 solved rows exercise it; affects 8 of 110 | Demote non-urgent only; never demote urgent |
| `payment` conventions | **Unvalidated** — 0 of 30 solved rows; 24 of 110 use payment language | Urgent + this-week ⇒ notify; else digest; unknown sender ⇒ safety layer |
| Evidence top-1 rate | 18/28 — known weak; never emits `none` (§7) | See §7 |

The two "unvalidated" rows are the honest risk in this design: both affect a meaningful slice of the 110 and neither can be checked against a single solved example. They are decided by policy reasoning, and this document is where that is on record.

---

## 14. Constraints Held Throughout

- Read only from `dataset/`. No organizer-only files, no hardcoded labels.
- Secrets from **environment variables only** — never committed, never logged.
- Deterministic where possible (§6.3).
- Runnable from the terminal; setup and run instructions ship in `code.zip`.
- Every conversation turn logged per `AGENTS.md` §5.2, secrets redacted.
