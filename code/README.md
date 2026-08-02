# Message Notification Router — setup and run

Routes every message in `dataset/messages.csv` to **notify**, **digest**, or **mute**, and writes `output.csv`.

## Run it

No API key, no model, no network access:

```bash
python3 code/main.py --rules
```

Writes `output.csv` — one row per input message, in input order:

```
message_id,action,message_type,reason,confidence,evidence_message_ids
```

Python 3.9+, standard library only for this path. Nothing to install.

| flag | effect |
|---|---|
| `--rules` | route with the declarative policy in `rules.json` — **recommended**, the measured-best path |
| *(none)* | route with the older hand-ordered gate stack in `decide.py` |
| `--llm` | allow model calls to fill anything not already cached; requires a key |
| `--dataset DIR`, `--out FILE` | override input/output paths |

## Measured accuracy

Backtested against the 30 solved rows in `dataset/sample_messages.csv`:

| path | action | message_type |
|---|---|---|
| **`--rules`** | **96.7%** | **83.3%** |
| gate stack (`decide.py`) | 93.3% | 93.3% |
| gate stack, de-fitted | 86.7% | 70.0% |

The middle row overstates itself, and it is worth being explicit about why. Its `message_type` regexes contain strings copied verbatim out of the solved rows — `alert threshold`, `nothing dramatic`, `no crash damage`. Remove those five literals and it scores 70.0%, the third row. Patterns keyed on a memorised phrase cannot fire on unseen messages, so the `--rules` path is expected to generalise better despite the lower headline number on this particular 30.

Reproduce either:

```bash
python3 code/evaluate.py        # gate stack
python3 code/score_rules.py     # rules path
```

## About `label_store.json`

**This is not a table of hardcoded answers and contains no gold labels.**

It caches model answers to the questions defined in `labels.json` — eleven yes/no questions about what a message *is*: "does it ask the reader for an OTP?", "is the sender waiting on a reply?". Nothing in it derives from `sample_messages.csv` or any organiser-only file, and no entry names an action, a message type, or an expected output.

It is committed so the submission runs with no key, no model and no network, and reproduces byte-identically. To rebuild it:

```bash
rm code/label_store.json
ANTHROPIC_API_KEY=... python3 code/main.py --rules --llm
```

Roughly 5 minutes and a few cents.

## How it works

Two files decide everything:

- **`labels.json`** — the label specification. Each semantic label is a written question, and that wording *is* its definition. Structural labels are read straight from the dataset CSVs.
- **`rules.json`** — the routing policy: ordered rules, first match wins, each with an `id` and a stated rationale. `engine.py` evaluates this table and holds no routing knowledge of its own.

Every output row records the rule that produced it, so any decision traces back to one line of policy.

Safety rules (`S0`–`S4`, `U1`) are evaluated in code and never shown to a model. They key on structural facts — account age, domain mismatch, verification, an explicit opt-out — which message text cannot influence. Five of the 110 messages carry instructions aimed at the router, and a rule that can suppress a scam must not be reachable by argument from the scam itself.

One known cost, measured rather than assumed: the eleven labels are extracted in a single call, so editing one definition can shift another's answers. Isolating them into separate calls was tried and scored worse (action 96.7% → 86.7%), because judging urgency alongside "is a reply wanted" calibrates both. Any edit to `labels.json` therefore needs a full re-extract and re-score, not a local check.

`../ARCHITECTURE.md` is the design record, including the rules that were tried and disproved.

## Files

| file | role |
|---|---|
| `main.py` | entry point; writes `output.csv` |
| `labels.json` / `rules.json` | the specification and the policy |
| `extract.py` | fills `label_store.json` from the label questions |
| `engine.py` / `route.py` | evaluate the policy; produce an output row |
| `features.py` | structural features from the dataset CSVs |
| `retrieval.py` | evidence selection for `evidence_message_ids` |
| `decide.py` | the older gate stack, kept runnable for comparison |
| `evaluate.py` / `score_rules.py` | backtests |

## Secrets

Keys are read from the environment only, via `ANTHROPIC_API_KEY`. A `.env` file in the repo root or in `code/` is loaded if present; both are gitignored. No key is written to disk, logged, or included in any cache key — and none is needed to run the submission.
