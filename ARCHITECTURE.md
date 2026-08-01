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
| `verified` flag separates safe from unsafe businesses | business_092 (Thrillophilia) → `link.wame.pro` and business_095 (Polaris) → `weurl.co` are **verified spoofers**; business_032 (Green Cross Pharmacy) is **unverified but legit** at 420 days old with 0 reports | `verified` is weak. The composite in §5 is what separates |

---

## 5. Safety: The Permanent Guard

Per explicit direction, safety is an **unconditional top layer** — it runs before personalization and cannot be overridden by engagement history. A user who habitually opens messages from a scam sender must still not be interrupted by one.

**What actually separates scam accounts** is a composite, not any single flag:

```text
brand_name == "Unknown"  AND  account_age 12–36 days  AND  reports 16–29
```

This cleanly isolates business_049 / 098 / 099 / 100. Contributing (non-gating) risk features: domain mismatch, URL shorteners, payment-credential solicitation, chain-forward instruction language.

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

`evidence_message_ids` is scored separately. Current backtest: **13/28 correct at top-1** — much weaker than action accuracy, which reached **26/28** on solved rows.

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

## 11. Component Map

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

## 12. Open Questions

| Question | Status | Current default |
|---|---|---|
| Media backend | Undecided — needs key or local tooling | Metadata-only (degraded) |
| Quiet-hours / DND policy | **Unvalidated** — 0 of 30 solved rows exercise it; affects 8 of 110 | Demote non-urgent only; never demote urgent |
| `payment` conventions | **Unvalidated** — 0 of 30 solved rows; 24 of 110 use payment language | Urgent + this-week ⇒ notify; else digest; unknown sender ⇒ safety layer |
| Evidence top-1 rate | 13/28 — known weak | See §7 |

The two "unvalidated" rows are the honest risk in this design: both affect a meaningful slice of the 110 and neither can be checked against a single solved example. They are decided by policy reasoning, and this document is where that is on record.

---

## 13. Constraints Held Throughout

- Read only from `dataset/`. No organizer-only files, no hardcoded labels.
- Secrets from **environment variables only** — never committed, never logged.
- Deterministic where possible (§6.3).
- Runnable from the terminal; setup and run instructions ship in `code.zip`.
- Every conversation turn logged per `AGENTS.md` §5.2, secrets redacted.
