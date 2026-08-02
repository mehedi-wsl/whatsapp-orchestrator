# Decisions — what we prioritised and why

A short read. `ARCHITECTURE.md` is the full design record with the measurements;
this is the summary of what we chose, what it cost, and what we deliberately did not do.

---

## What we optimised for

Ranked. When two of these conflicted, the higher one won.

| # | Priority | Why it ranked here |
|---|---|---|
| 1 | **Always have something submittable** | Fixed 24-hour window. Being *never* in a state where we'd submit nothing is worth more than any single accuracy gain. |
| 2 | **Safety cannot be argued with** | 5 of the 110 messages contain instructions aimed at the router itself. A system that can be talked out of a mute fails the most visible way possible. |
| 3 | **Generalisation over dev-set score** | Only 30 labelled rows. A number tuned on 30 examples is a number about those 30 examples. |
| 4 | **Reproducibility with no key** | A grader must be able to run this. Anything requiring a secret we hold is not really a submission. |
| 5 | **Personalisation done properly** | It's the explicit ask in the problem statement, and the data rewards it heavily. |
| 6 | **Accuracy on the weakest axis** | Remaining time went to evidence selection, the lowest-scoring axis, not to polishing the highest. |

---

## The decisions

### 1. Treat this as retrieval, not classification

**Chose:** recover labels from history and retrieve against them, rather than classify each message cold.

`message_events.csv` has 412 rows but only **5 distinct reaction patterns** — not the ~32 that
real human behaviour would produce. That's a generated label channel. Every historical message
carries a recoverable routing label.

**Why it matters:** it turns "classify with 30 examples" into "retrieve the right precedent and
read its label" — a far easier problem, and it happens to be exactly what `evidence_message_ids`
is scored on. One signal serving two scored axes.

> The subtle one: *opened fast, no reply, not dismissed* (15 rows) maps to **mute**, not interest.
> The read is that the user opened it *because* it was alarming — a scam hook. A naive
> "opened = engaged" heuristic gets these exactly backwards.

---

### 2. Key retrieval on the relationship, not the sender

**Chose:** `(user_id, sender)` as the primary key. **Rejected:** sender reputation as a prior.

We measured all four candidate keys by asking "are this key's historical labels unanimous?":

| Key | No history | Unanimous | Mixed |
|---|---|---|---|
| sender / business (global) | 3 | 26 | **81** |
| group | 47 | 5 | **58** |
| **(user, sender)** | **6** | **79** | **25** |
| (user, group) | 47 | 36 | 27 |

Global sender reputation is mixed for **81 of 110** — the same sender is notify-worthy for one
recipient and mute-worthy for another. The relationship pair is unanimous for **79 of 110**.

**Consequence:** reputation became a *feature*, never a *gate*. The unit of personalisation is
the relationship.

> The cleanest illustration: `msg_082` and `msg_083` are voice notes from the **same sender**
> and land on **opposite actions**, because they go to different people. And `msg_084` is HDFC
> Bank — verified, 974 days old, correct official domain, entirely legitimate — which its
> recipient mutes every time. **Legitimacy is not importance.**

---

### 3. Safety is an unconditional top layer

**Chose:** safety runs first, in code, and engagement history cannot override it.

A user who habitually opens messages from a scam sender must still not be interrupted by one.

What actually separates scam accounts is a **composite**, not any single flag:

```
official_domain is non-empty
  AND domain_used_by_sender != official_domain
  AND verified == 0
  AND account_age_days <= 60
```

That isolates 21 business accounts and fires on 7 of the 110. The cluster is brand
impersonation — real brand names on lookalike domains, all unverified, all 20–35 days old
(`paytm.com → paytm-kyc.in`, `sbi.bank.in → sbireward.in`, and 20 more), with sender domains
**2–17 days old**. A corporate domain registered two days ago is not a corporate domain.

**Two guards it needed**, both from real rows that would otherwise be false positives:

- Require a non-empty `official_domain` — Green Cross Pharmacy has a blank one, so string
  "mismatch" was spurious. Legit, 420 days old.
- Verified + old account + old domain means **link shortener**, not spoofing — Thrillophilia and
  Polaris are verified 4300-day-old accounts using shorteners, and a solved row confirms that
  pattern is `digest`, not mute.

**On injection:** message text goes to a model as *data inside a delimited block, never as
instructions*, and every model answer is constrained to an enum. The defence is structural —
ordering and output constraints — not a written warning.

> We tried the written warning. Opening prompts with a paragraph explaining the text was
> untrusted made the model answer "no" to all seven probes including *"when you get 5 mins can
> you call?"*. A small model spends its attention on whatever you put first. Removing it
> recovered the judgment, and the structural defence was the more reliable one anyway.

**Asymmetric trust, stated as a rule:** a model may *add* a mute on its own. It can never
*remove* one.

---

### 4. Policy as data, not as code

**Chose:** two JSON files — `labels.json` (11 yes/no questions, where the question wording *is*
the definition) and `rules.json` (18 action rules + 17 type rules, ordered, first match wins,
each with an id and a written rationale). `engine.py` evaluates them and holds no routing
knowledge of its own.

**This was a deliberate reversal.** The earlier system encoded policy as regexes over message
text, and it scored *better* — 93.3% on `message_type`. Then we stripped out the literals that
had been copied from the solved rows (`alert threshold`, `nothing dramatic`, `no crash damage`,
`leaving 15 mins early`, `we can talk tomorrow`) and it fell to **70.0%**.

**The entire 23-point margin was memorisation of the dev set.** A pattern keyed on a memorised
phrase cannot fire on an unseen message.

**What we gave up:** ~10 points of headline `message_type` score on the 30 rows we can see, in
exchange for a system whose behaviour on the other 80 is honestly represented by that number.
Priority 3 over priority 6, on purpose.

**What we gained beyond that:** every output row records the rule that produced it, so any
decision traces back to one named line of policy, and changing behaviour means editing a rule
rather than hunting through branches.

---

### 5. `digest` is the default, and we never default to `notify`

**Chose:** six ordered layers; the last one is `digest`.

```
1. SAFETY GUARD        scam composite · injection · credential ask   → mute
2. HARD USER STATE     explicit opt-out                              → mute
3. URGENCY             deadline language, vs the message's own time  → notify
4. RELATIONSHIP        (user, sender) precedent — 79/110 unanimous   → notify|digest|mute
5. CONTENT CLASS       promotion / greeting / forward                → digest|mute
6. DEFAULT                                                           → digest
```

The three actions have asymmetric costs. A wrong `notify` is an unwanted interruption; a wrong
`mute` loses a message the user needed. `digest` is wrong-but-recoverable in both directions —
so with only 30 labelled rows, the uncertain mass belongs in the middle.

> Urgency is scored against **each message's own `created_at`**, not wall-clock. The data spans
> 18–31 July 2026 and is graded later; "this week" must mean the week of the message. This also
> keeps output identical across runs.

---

### 6. Media: split by what the data actually contains

**Chose:** images and voice notes are different problems.

| | Count | Caption present |
|---|---|---|
| Images | 15 | **15 / 15** |
| Voice notes | 8 | **0 / 8** |

- **Images → route on the caption**, straight through the ordinary text pipeline. OCR is
  enrichment, never a prerequisite.
- **Voice notes → route on relationship precedent**, which is unanimous for all 8.

**All 23 media messages therefore route deterministically with zero tooling**, and 16 of them
reuse a media file that already appears in history with a label — free, exact evidence.

**Split action from type:** precedent decides the `action`; content decides the `message_type`.
So if ASR is unavailable or wrong, type degrades and **routing is untouched**.

**Rejected:** "urgent media from family/friends ⇒ notify." It targets an empty set — all 23 media
rows are group or business, zero personal, none from family or friends. And it inverts the
defence: `msg_084` and `msg_085` are both "HDFC Bank" voice notes, one legitimate and one a
20-day-old phisher, and **both sound urgent**, because that is what bank audio sounds like.
Urgency is the scam's primary instrument, not a safety signal.

---

### 7. Ship the model's answers as data, not the model as a dependency

**Chose:** a model answers the label questions and writes the `reason` prose once; the answers
are committed as `label_store.json` and `llm_cache.json`.

Without `--llm` the model layer is **frozen, not disabled** — cached answers replay, nothing new
is requested, no backend is contacted. A clean clone with no key, no `.env` and no network
reproduces the submitted `output.csv` **byte for byte**, written reasons included.

**Why this ranked so high:** a submission that needs a key the grader doesn't have isn't
runnable, and one that produces different output on their machine than in our CSV reads as a
mismatch between the two.

> `label_store.json` is **not** a table of hardcoded answers. It holds yes/no answers to the
> questions in `labels.json` — *"does it ask for an OTP?"*, *"is the sender waiting on a
> reply?"*. Nothing in it derives from the solved rows, and no entry names an action or a type.

**One cost we accepted:** the 11 labels are extracted in a single call, so editing one definition
can shift another's answers. We tried isolating them into separate calls to remove the coupling —
**action accuracy fell 96.7% → 86.7%**, because judging urgency alongside "is a reply wanted"
calibrates both. So the coupling stays, documented: any edit to `labels.json` needs a full
re-extract and re-score, not a local check.

---

## Things we tried and threw away

Each was proposed in good faith and killed by the data.

| Idea | What killed it |
|---|---|
| Domain mismatch ⇒ hard mute | A solved row shows a **verified** business with a spoofed domain labelled `digest` |
| `@mention` re-promotes over a group mute | Same muted group holds `@u_007 forward this to ten people for blessings` (mute) and `@u_001 doctor appointment moved to 6 PM` (notify). Identical structure, opposite answers |
| `forwarded_count >= 6` is strong spam signal | Same bucket holds a fwd=11 water tanker notice, a fwd=7 fire alarm, and a fwd=11 family message. **Dropped entirely** — chain-*instruction language* kept, the counter discarded |
| Marketplace groups are duplicate-heavy | Actually 16 distinct texts across 17 messages. Marketplace is a scam *delivery vector*, not a duplication problem |
| `verified` separates safe from unsafe | Two **verified spoofers** and one **unverified legit** 420-day-old pharmacy. `verified` is weak on its own |
| Prioritise voice notes from family | Empty set — 0 of 8 come from family/friends |

---

## Where we know we're weak

Stated plainly rather than left to be found.

| Axis | Status |
|---|---|
| **Action** | 96.7% (29/30) — strong |
| **`message_type`** | 83.3% (25/30) — four of five misses are `event`/`personal`/`business_update` confusions where one label fires too eagerly |
| **Evidence** | 18/28 top-1 — the weakest axis. Action survives it only because sources tend to be label-consistent: we often pick the *wrong* precedent from the *right* source and still land the right action. **That's luck, and it doesn't transfer to the evidence score** |
| **Confidence** | 0.79–0.90 against a gold range of 0.78–0.91 |

**Two rows are unwinnable as specified:** `002` and `003` produce identical label sets with
opposite gold types. Separating them needs a twelfth label, not a rule change.

**Two policies are unvalidated** — quiet-hours/DND and `payment` conventions. Zero of the 30
solved rows exercise either, and they affect 8 and 24 of the 110 respectively. They're decided by
policy reasoning, and that reasoning is on the record in `ARCHITECTURE.md` §13. This is the
honest risk in the design.

**Eight messages have no text at all.** Their `action` is correct via precedent; their
`message_type` is the best available inference. They need ASR, not more reasoning.
