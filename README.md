# Message Notification Router — submission

Routes every message in `dataset/messages.csv` to **notify**, **digest**, or **mute**, assigns a
`message_type`, writes a human-readable `reason`, a calibrated `confidence`, and the historical
message used as evidence.

Measured on the 30 solved rows in `dataset/sample_messages.csv`: **action 96.7%**, message_type 83.3%.

---

## Run it

```bash
python3 code/main.py --rules
```

Python 3.9+. **Standard library only — nothing to install, no API key, no network, no model.**
Takes about two seconds and writes `output.csv` to the repo root:

```
message_id,action,message_type,reason,confidence,evidence_message_ids
```

One row per input message, in input order. Verified **byte-identical to the submitted
`output.csv`** from a fresh clone with no key and no network — including the written `reason`
prose, which replays from the committed cache (see *Reproducibility* below).

| flag | effect |
|---|---|
| `--rules` | route with the declarative policy in `code/rules.json` — **the submitted path** |
| *(none)* | route with the older hand-ordered gate stack in `code/decide.py`, kept for comparison |
| `--llm` | allow model calls to fill any label not already cached; requires `ANTHROPIC_API_KEY` |
| `--dataset DIR` / `--out FILE` | override input and output paths |

Reproduce the backtests:

```bash
python3 code/score_rules.py    # the submitted path
python3 code/evaluate.py       # the gate stack
```

---

## Approach

### The core problem

Two messages can be word-for-word identical and belong in different buckets. `msg_082` and
`msg_083` are voice notes from the *same sender* (`u_046`) and land on **opposite** actions,
because they go to different recipients. Any system that classifies message content alone is
solving the wrong problem — the label lives in the relationship, not in the text.

### 1. Recovering the missing supervision

`dataset/messages.csv` ships unlabelled. But `message_events.csv` records how users reacted to
412 historical messages, and those 412 reaction tuples `(opened, replied, dismissed, muted,
reported)` collapse into **exactly 5 distinct signatures**, not the ~32 that free-form behaviour
would produce:

| signature | count | reads as |
|---|---|---|
| opened + replied | 153 | notify |
| opened, no reply | 110 | notify |
| dismissed | 79 | digest |
| muted | 55 | mute |
| reported | 15 | mute |

Five clean buckets is not how organic behaviour looks — it is how *generated* behaviour looks.
The history is effectively labelled, which turns an unsupervised problem into a retrieval problem.

### 2. The retrieval key is the relationship

Precedent is keyed on `(user_id, sender)` — not on sender reputation, and not on message
similarity. Over the 110 messages that key gives **79 unanimous**, 25 mixed, 6 with no history.
Unanimous precedent alone matches gold action on 21 of the 22 applicable solved rows.

This is what makes decisions personalized. `msg_084` is from HDFC Bank: verified, 974 days old,
correct official domain, entirely legitimate — and user `u_040` mutes it every time.
**Legitimacy is not importance.**

### 3. Policy is data, not code

The routing logic lives in two JSON files that a reviewer can read without reading any Python:

- **`code/labels.json`** — eleven yes/no questions about what a message *is*. The wording of the
  question **is** the definition: *"Is the sender waiting on the recipient?"*, *"Does this message
  ask the reader to provide a one-time password, PIN, card number, CVV, or account password?"*
  Nineteen further labels are read straight off the dataset CSVs with no judgement involved.
- **`code/rules.json`** — 18 action rules and 17 type rules. Ordered, first match wins, each with
  an `id`, a condition, and a written rationale. `code/engine.py` evaluates this table and contains
  **no routing knowledge of its own**.

Every output row records the rule that produced it, so any decision traces back to one named line
of policy. Changing behaviour means editing a rule, not hunting through branches.

An earlier version of this system encoded policy as regexes over message text. It scored higher —
93.3% on message_type — until the memorised literals were stripped out (`alert threshold`,
`nothing dramatic`, `no crash damage`, `leaving 15 mins early`, `we can talk tomorrow`), at which
point it fell to 70.0%. **The entire 23-point margin was memorisation of the dev set.** That
measurement is why the declarative path is the one submitted, despite a lower headline type score.

### 4. Safety sits above everything, and out of reach

Five of the 110 messages contain instructions aimed at the router itself. So the safety rules
(`S0`–`S4`, `U1`) are evaluated **in code and never shown to a model**, and they key on structural
facts that message text cannot influence: account age, domain mismatch, verification status, an
explicit opt-out.

The decisive pair: `msg_084` and `msg_085` are both voice notes claiming to be HDFC Bank. One is
verified, 974 days old, on `hdfc.bank.in`. The other is unverified, **20 days old**, on
`hdfcbank-kyc.in`. Both will *sound* urgent, because that is what bank audio sounds like — so
urgency is treated as the scam's primary instrument, never as a promoter.

Where a model does participate, trust is asymmetric: **a model may add a mute on its own, but it
can never remove one.** Each lexical safety detector is OR'd with its model-judged twin.

### 5. Media

Images and voice notes are categorically different problems in this data, so they are handled
separately:

- **Images (15/15 carry a caption)** → route on the caption through the ordinary text pipeline.
  OCR is enrichment, never a prerequisite.
- **Voice notes (0/8 carry any text)** → route on relationship precedent, which is unanimous for
  all 8. ASR would improve `message_type`; it cannot change `action`.

All 23 media messages therefore route deterministically with zero tooling, and 16 of them reuse a
media file that already appears in `message_history.csv` with a label — free, exact evidence.

### 6. Confidence and evidence

Confidence is banded by action and scaled by the strength of the matching rule, landing in
0.79–0.90 against the gold range of 0.78–0.91. Evidence is the single most relevant historical
message from the same relationship; 102 of 110 rows carry one and 8 correctly emit `none`
(gold emits `none` on 6.7%).

**`DECISIONS.md`** is the short version of the above: what we prioritised, what each choice cost,
and the ideas that were tried and thrown away. `ARCHITECTURE.md` is the full design record,
including §4 — the rules that sounded right and were disproved by the data.

---

## Layout

```
README.md              you are here
DECISIONS.md           short read: what was prioritised and why, with the trade-offs
ARCHITECTURE.md        full design record, incl. what was tried and rejected
output.csv             the submitted predictions (110 rows)
code/
  main.py              entry point; writes output.csv
  labels.json          the label specification  (the questions ARE the definitions)
  rules.json           the routing policy       (ordered, first-match-wins)
  engine.py            evaluates rules.json; holds no routing knowledge
  route.py             turns a rule match into an output row
  extract.py           fills label_store.json from the label questions
  label_store.json     cached label answers — see below
  features.py          structural features from the dataset CSVs
  retrieval.py         precedent lookup and evidence selection
  llm.py               model backends (hosted / local / absent), disk-cached
  llm_cache.json       every model answer ever used — makes runs offline & exact
  decide.py            the older gate stack, kept runnable for comparison
  evaluate.py          backtest for the gate stack
  score_rules.py       backtest for the submitted path
  README.md            developer notes
dataset/               provided unchanged
```

### Reproducibility

A model wrote the `reason` prose and answered the eleven label questions. Both sets of answers are
committed as data — `llm_cache.json` and `label_store.json` — so **without `--llm` the model layer
is frozen rather than disabled**: cached answers still replay, nothing new is requested, and no
backend is contacted. That is why a keyless offline run reproduces the submitted file exactly
instead of a blander variant with generic reasons.

`--llm` is what unfreezes it, allowing new calls for anything not already cached. The routing
fields never depend on it: a model may rewrite the prose and may *add* a mute, but it can never
change an action assigned by policy or remove a mute.

### About `code/label_store.json`

**This is not a table of hardcoded answers and contains no ground-truth labels.**

It caches model answers to the eleven questions defined in `labels.json` — *"does it ask for an
OTP?"*, *"is the sender waiting on a reply?"*. Nothing in it derives from `sample_messages.csv` or
any organizer-only file, and no entry names an action, a message type, or an expected output.

It is committed so the submission runs with no key, no model and no network, and reproduces
byte-identically. To rebuild it from scratch:

```bash
rm code/label_store.json
ANTHROPIC_API_KEY=... python3 code/main.py --rules --llm
```

Roughly five minutes and a few cents.

---

## Known limitations

Stated plainly rather than left to be discovered:

- **`message_type` is the weaker axis (83.3%).** Four of the five misses are `event` / `personal` /
  `business_update` confusions where the label `is_scheduled_event` fires slightly too eagerly.
- **Two rows are unwinnable as specified.** `002` and `003` produce identical label sets with
  opposite gold types; separating them needs a twelfth label, not a rule change.
- **Eight messages have no text at all** (voice notes without transcripts). Their `action` is
  correct via precedent; their `message_type` is the best available inference.
- **The eleven labels are extracted in one call, so they are coupled** — editing one definition can
  shift another's answers. Isolating them into separate calls was tried and scored *worse*
  (action 96.7% → 86.7%), because judging urgency alongside "is a reply wanted" calibrates both.
  A documented cost, not a bug: any edit to `labels.json` needs a full re-extract and re-score.

## Secrets

Keys are read from the environment only, via `ANTHROPIC_API_KEY`. A `.env` file in the repo root
or in `code/` is loaded if present; both are gitignored, and `.env.example` is a placeholder with
no key in it. No key is written to disk, logged, or included in any cache key — and **none is
needed to run this submission.**
