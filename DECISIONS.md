# Decisions

The short version of why this system is built the way it is.
`ARCHITECTURE.md` has the full detail and the measurements.

---

## What we cared about most

1. **Never be stuck with nothing to submit.** Build the whole pipeline early, improve it after.
2. **Safety can't be argued with.** Five messages try to instruct the router itself.
3. **Don't tune to the 30 solved rows.** A number fitted to 30 examples is a number about those 30 examples.
4. **It has to run without our API key.** Otherwise it isn't really submittable.

---

## The five decisions

### 1. Let past behaviour decide

We don't judge a message on its own merits. We look at how this user has handled this sender
before, and follow it.

That works because the history is already labelled. The 412 past messages have only **5 distinct
reaction patterns** between them — that isn't real human behaviour, it's a label channel — so
every past message effectively carries a routing decision we can read off.

The thing to match on is the **pair**, not the sender. Sender reputation on its own gives a
conflicting answer for **81 of 110** messages; the user-and-sender pair gives a unanimous answer
for **79 of 110**. So reputation is only ever a hint.

Two voice notes in the data come from the same sender and get opposite actions, because they go
to different people. And one bank message is completely legitimate — verified, 974 days old,
correct domain — and its recipient mutes it every time. Legitimate isn't the same as important.

This also gives us `evidence_message_ids` for free: the past message we matched on *is* the
evidence.

### 2. Safety runs first, and history can't override it

If someone habitually opens messages from a scammer, they should still not be interrupted by one.
So the safety check runs before anything personalised.

No single flag identifies a scam account. `verified` doesn't work — the data has verified
spoofers and an unverified legitimate pharmacy. What works is a combination: a real brand name,
a lookalike domain, unverified, and an account under 60 days old. That catches 21 accounts, all
of them impersonation (`paytm.com` → `paytm-kyc.in`, and 20 more).

Where a model is involved, it can *add* a mute but never remove one.

### 3. Rules live in JSON, not in code

The routing logic is two files anyone can read: `labels.json` (11 yes/no questions about a
message) and `rules.json` (the ordered rules). The code just evaluates them.

We didn't start here. The first version matched patterns against message text and scored
**better** — 93.3% on message type. Then we removed the phrases that had been copied out of the
solved rows, and it dropped to **70%**. The whole 23-point lead was memorising the answer key.

So we took the lower honest number. It's about 10 points worse on the rows we can see, and a
truer picture of the rows we can't.

### 4. When unsure, `digest`

A wrong `notify` is an annoying interruption. A wrong `mute` loses something the user needed.
`digest` is the only one that's recoverable either way — so anything uncertain goes there, and
nothing ever defaults to `notify`.

### 5. Images and voice notes are different problems

Every image has a caption; no voice note has any text at all. So images go through the normal
text path using their caption, and voice notes are routed purely on relationship history, which
happens to be unanimous for all 8 of them.

That means all 23 media messages route correctly with no OCR or transcription at all. Reading the
audio would only improve the *type* label, never the routing.

We considered treating urgent-sounding media as important, and dropped it. Two voice notes both
claim to be from HDFC Bank — one real, one a 20-day-old phishing account — and both sound urgent,
because that's how bank recordings sound. Urgency is the scam's main tool, not a safety signal.

---

## Ideas we dropped

| Idea | Why it failed |
|---|---|
| Wrong domain = definitely a scam | A verified business with a spoofed domain is labelled `digest` in the solved rows |
| An `@mention` beats a muted group | The same muted group has `@you forward this for blessings` (mute) and `@you doctor appointment moved to 6 PM` (notify) |
| Heavily forwarded = spam | The same bucket holds a water tanker notice, a fire alarm, and a family message |
| `verified` means safe | Two verified spoofers; one unverified legitimate pharmacy |
| Prioritise family voice notes | There aren't any — none of the 8 come from family or friends |

---

## Where it's weak

| | Score |
|---|---|
| Action | **96.7%** (29/30) |
| Message type | 83.3% (25/30) |
| Evidence | 18/28 — the weakest part |
| Confidence | 0.79–0.90, against 0.78–0.91 in the solved rows |

Evidence is the honest weak spot. Action accuracy partly survives it by luck: we often pick the
wrong past message from the right conversation, and still land the right action. That luck
doesn't carry over to the evidence score.

Two rows can't be got right as specified — `002` and `003` look identical to the system but have
different correct answers. Eight messages have no text at all, so their type is a best guess.
And two policies — quiet hours and payment handling — aren't tested by any solved row, so they're
reasoned rather than verified.
