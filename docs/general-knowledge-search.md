# General knowledge and web search — design

Status as of 2026-08-20. **Built and passing its suite.** This is the first
working instance of the **tier-2 escalation** planned in
`project-overview.md` — the "heavy model for research" tier, reached through
a script exposed to Assist as a tool.

Read alongside `voice-and-music.md` (the script-as-tool pattern, the traps
that apply to any Assist tool).

**No auditable state recorded here** — no entity IDs, model names, or
pass/fail run logs. See "How to audit this".

## Problem

The tier-1 assistant could not answer "who won the Pirates game yesterday?"
or "what's going on in Pittsburgh this weekend?" Anything needing current
information was outside it entirely.

## Decision

A two-model split, tier-1 delegating to tier-2 through a script:

1. **Tier 1** (cheap model) keeps its HA tools and handles the house. Its
   own web search is **off**.
2. It calls an **escalation script** with the user's question.
3. The script runs `conversation.process` against a **tier-2 subentry** — a
   stronger model, web search on, and **no HA tool access at all**. It
   answers, or says it could not.
4. The script returns that answer as its tool result; tier-1 relays it.

Both subentries live under the same OpenRouter config entry. **Read the
entry and subentry IDs before use** — they are load-bearing inside the
script's service call, and are not quoted here.

### Why this shape

| Decision | Reason |
| --- | --- |
| Tier-2 is a separate subentry, not a bigger tier-1 | `voice-and-music.md` already measured a stronger model as tier-1: no accuracy gain, ~10x judgment latency. Escalating only when needed keeps that cost off every music command. |
| **Both tiers run the same cheap model** | Tier 2 was originally given a stronger model to fix a hallucination that turned out not to exist (see the trap below). Profiling later confirmed the stronger model is slower and no more accurate here — see "Model profiling". The split earns its keep on *architecture*, not on model tier. |
| Tier-2 gets **no** `llm_hass_api` | It is a research agent. Giving it house tools would let a second model act on the home with no supervision, and doubles the surface where a wrong device call can originate. |
| Tier-1's own `web_search` is off | Two search paths would make it ambiguous which one produced an answer, and tier-1 would sometimes answer directly, bypassing the grounding rules written into tier-2's prompt. |
| Script-as-tool | The only mechanism for tier-1 → tier-2 (there is no native one), and the pattern is already proven by the music scripts. |

### Unexpected benefit: the narration leak died

Before this split, replies leaked the model's pre-tool-call text into the
spoken output — *"Now I'll search for yesterday's Pirates game:The Pirates
won…"*. This was **not** a prompt-adherence failure and no prompt wording
fixed it. The model legitimately emits a short "here's what I'm about to do"
text segment *before* a tool call, and HA concatenated that with the
post-call answer into one speech string.

Routing through tier-2 removed it structurally: the search now happens
inside a nested conversation, and only its final answer text crosses back as
a tool result. There is no pre-tool narration left in the path the user
hears.

## The trap that cost the most: validating answers against your own knowledge

**An assistant debugging this feature has a training cutoff older than the
house's current date. Anything time-sensitive that contradicts its priors is
more likely stale priors than a system bug.**

This was learned expensively. The system was accused of hallucinating for an
extended investigation because it kept answering that the Pirates beat the
Tigers 4-3 on a walk-off homer by a player the diagnosing assistant "knew"
played for another team. **The system was correct every time.** The roster
had changed after the assistant's cutoff.

Three compounding errors, all worth avoiding by name:

1. **Undated verification search.** Checking with a vague query returned a
   mix of games from the series; a score mismatch from a *different* game
   was taken as proof of fabrication. The same result text contained direct
   evidence against the accusation and was not registered.
2. **Consistency read backwards.** "Same answer from two independent search
   backends across many repetitions" was treated as the signature of a
   canned confabulation. It is the opposite: independent retrieval paths
   converging is what a *correct* answer looks like. Fabrication drifts.
3. **A second false positive stacked on the first.** An answer about a game
   on a bare date was also called fabricated. It was a real game from the
   *previous year*, retrieved accurately — the actual (much smaller) defect
   was that the year was not stated.

Cost: several unnecessary config changes, a model-tier investigation, and a
recommendation to build an entire custom search-API integration to fix a bug
that did not exist.

**Rule going forward:** verify a factual answer with a *narrow, dated*
search run at the time of testing. Never against recollection.

## Model profiling

Four configurations, same five questions each, timed from the escalation
script's own `on`→`off` transitions in the recorder. Absolute numbers will
drift; the **shape** of the result is the durable part.

| Config | Tier-2 model | Mean | Spread |
| --- | --- | --- | --- |
| A | a frontier-tier model | 8.2s | 5.5–10.1s |
| B | the same cheap model as tier 1 | 5.9s | 4.2–7.7s |
| C | a fast third-party flash model | 3.9s | 3.2–4.4s |
| D | **no tier 2** — one agent with search | 7.9s | single sample |

**D is the important row, and it is the counterintuitive one.** Removing the
escalation hop entirely — the obvious way to cut two model calls — came out
*slower* than keeping it, and cost more per question. The search round trip
happens inside tier 1's context, which balloons past 18k input tokens once
results come back. One call over a huge prompt loses to three calls over
small ones.

**So the hop was never the bottleneck. Prompt size is.** Tier 1 sends
roughly 7k tokens on every utterance in the house, music included, and an
escalated question pays that twice. Against that, tier 2's own model is
noise: in config C its share of the per-question cost was about 4%.

Shrinking tier 1's prompt is therefore the only remaining lever with real
leverage, and it improves every interaction, not just this feature.

### Prompt caching would beat shrinking, and is not available to us

The obvious better answer is not to shrink the prompt but to stop paying for
it: tier 1 sends a near-identical multi-thousand-token prefix on every single
utterance. Cached input costs a fraction of fresh input and prefills faster.

**Measured cache hit rate: 0.0%**, across hundreds of requests. The reason is
upstream, not configuration — Home Assistant's `open_router` integration
builds its request with `extra_body` carrying only `require_parameters` and
tools, and sets no `cache_control` anywhere. The provider supports caching;
the integration never asks for it.

So this is not a knob we own *on this integration*. Two dead ends worth
recording so they are not re-explored: provider-side presets cannot supply
it either, because `cache_control` is a per-message annotation rather than a
request parameter, and the integration's model field is a validated
dropdown that will not accept a preset reference.

**But Home Assistant's own `anthropic` integration supports it directly**,
and has since before this was built. It sets `cache_control` on the system
block, exposes caching as a configuration option with `off` / `prompt` /
`automatic` modes, and **defaults to `prompt` — caching on**. It also
carries several things the OpenRouter subentry does not expose at all: a
`max_tokens` control, and a web-search option with structured location
settings (city, region, country, timezone) that would replace the
coordinates-in-the-prompt workaround described above.

That makes the caching gap a **migration decision, not an upstream wait**.
The trade is real and runs against a stated architecture goal: this project
chose OpenRouter deliberately as the multi-provider path
(`project-overview.md`), and going direct to Anthropic narrows that to one
vendor and needs a second API key. Set against it, tier 1 is the expensive
call — a large, near-identical prefix on every utterance in the house — and
it is exactly the shape caching exists for.

A hybrid is available and probably the right first step: **move tier 1,
leave tier 2 where it is.** Tier 1 needs no web search and pays the big
prefix; tier 2's prompt is a few hundred tokens, so caching would buy it
almost nothing. Either way, a move re-runs both suites.

### What trimming actually recovered

Measured on the live tier-1 routing call, before and after:

| | ~tokens |
| --- | --- |
| Before | 7,322 |
| After | 6,153 |
| **Saved** | **1,169 (−16%)** |

The prose itself went from ~1,739 to ~1,142 tokens (−34%). Nothing was
deleted for brevity's sake — every cut was either a rule stated twice or a
tool that does not work. See `voice-and-music.md` for what came out and why.

### Why the fastest option was not chosen

C was more than twice as fast as A and the most consistent of the four. It
was still rejected, on one case: a date given without a year.

| Config | Correctly handled a bare date |
| --- | --- |
| B | 3 / 3 |
| C | 1 / 3 |

The failure mode is not "unhelpful", it is **confidently wrong** — reporting
a previous year's game as though it were the answer, with no year attached.
That is the one outcome this suite treats as a hard failure, so two seconds
is a cheap price to remove it. B is live.

The general lesson, worth keeping: *pick on the hard-failure case, not on
the mean.* C won every other row.

## Negative results — things that made no difference

Recorded so they are not retried:

- **`tool_native` vs `tool_exa`.** Two entirely different search backends
  produced the same answers. The mode is not a quality lever here; the
  cheaper default is fine.
- **Model tier, for grounding.** Escalating the model did not change
  factual answers — because they were already correct. Tier-2's real value
  is judgment on *ambiguous and false-premise* questions, not raw retrieval.
- **Prompt wording, for the narration leak.** Only the architectural change
  fixed it. See above.
- **Provider-side I/O logging, for latency.** Enabled during debugging and
  suspected of slowing the round trip. Measured with it off: 8.4s median
  against 8.3s with it on. A real confound to control for, and an entirely
  empty one.
- **Removing the escalation hop, for latency.** See "Model profiling" — it
  is slower, not faster.

## Defects found by the suite, and fixed

| Defect | Symptom | Fix |
| --- | --- | --- |
| No date context in tier-2 | A bare date resolved silently to a previous year's event, reported as if current | Inject today's date into the tier-2 prompt; require an explicit date or year whenever the answer is not near today |
| Unbounded list answers | "List every US president" returned a ~90-word single sentence — unusable as speech | Hard word cap plus a rule to give a count and two or three examples instead of reciting any list over three items |
| Tier 1 sometimes declined to escalate | A question that looked like settled general knowledge was answered from the model's own memory instead: stale (it stopped at the wrong president), unbounded, and prefixed with "I can answer this from my knowledge:" | Tier 1's instruction now says to escalate *even for questions that look like simple facts, lists, or history* |

The first two were invisible while the false hallucination diagnosis was
consuming attention. The third only appeared during model profiling, on a
run where nothing about tier 1 had changed — **escalation is a model
judgment, so it is probabilistic, and a case that routes correctly today can
route differently tomorrow.** Any suite run has to check *whether* the
escalation fired, not just whether the answer was good.

### Fixed: "yesterday" asserted without checking the date

**Status: fixed 2026-08-21, 3 of 3 on the reproduction below.** The route to
the fix matters more than the fix, because the defect was not where it
appeared to be.

**Found 2026-08-21, and this one is a genuine hard failure** — the shape the
suite exists to catch, and the shape wrongly attributed to this system
earlier when it was in fact correct.

Two instances in a single run, verified against dated searches:

- Asked who won a team's game "yesterday" on a day that team **did not
  play**, one reply correctly said so and named the actual date, one omitted
  the qualifier, and one stated the earlier game had happened *"yesterday"*.
  One correct, one imprecise, **one wrong**, from identical input.
- Asked a follow-up about another team, it reported a **specific score for a
  game that had not yet been played** — kickoff was that evening.

The common cause is not retrieval. Search returns the most recent result
correctly; the model then narrates it with a relative day-word without
checking that the date it found matches the day the user asked about. The
existing instruction only requires an explicit date when the answer is "more
than a few days away from today", so a result one or two days old slips
through and gets called "yesterday".

**The fix took two layers, and the second one is the finding.**

Tier 2 was corrected first: banned from using relative day-words at all,
required to name the date of whatever it found, told that "nothing happened
on that day" is a complete answer, and forbidden from reporting a result for
anything not yet finished. Asked directly, it then answered *correctly* —
naming the date and the right result.

**The spoken reply was still wrong.** Tier 1 was discarding the date while
relaying. Which exposed something not previously understood about this
architecture:

> **Tier 1 paraphrases the escalated answer; it does not relay it verbatim.**

That single fact explains two separate behaviours. It is why the narration
leak disappeared when the tier-2 split was introduced — tier 1 was silently
filtering the leaked preamble out, which was read at the time as the
architecture solving the problem. And it is why a correct, fully-dated answer
from tier 2 reached the user stripped of its date. The same mechanism, once
helpful and once harmful, and it was only ever noticed because of the harm.

Tier 1's instruction now forbids dropping a date, day, year, or qualifying
phrase that the escalated answer contained, and equally forbids *adding* one
it did not.

**The lesson worth carrying:** in a two-model chain, testing the answering
model in isolation is not testing the system. Tier 2 passed on its own and
the user still got a wrong answer. Reproduce through the whole path, then
bisect by calling each layer directly — that is what located this in minutes
after prompt-level guessing had failed.

### Open defect: a stale officeholder inside an otherwise-correct answer

**Found 2026-08-21 by the list-invitation case, which is not what that case
was built to test.** Asked to list every US president, the reply respected
the length cap correctly — and then named the *previous* president as the
current one, and gave a count one lower than the true figure.

What makes this worth its own entry is the contrast. In the same run, asked
directly who holds a local office, the answer was correct and current. The
difference is what the question looks like: a "who holds this office now"
question reads as current and gets searched, while a question that reads as
settled history gets answered from the model's own knowledge — including the
part of it that has since changed.

**The prompt already warns about exactly this** ("officeholders change, so
search rather than answering from memory"), and it did not help, because the
model did not classify the question as one about the present. That is the
gap: not a missing instruction, but a misread of which questions have a
time-sensitive component buried in them.

Untried and worth trying before anything more elaborate: requiring that any
answer naming a current holder of anything be searched, regardless of how
the question is phrased.

**Fixed 2026-08-21, with a cost — read the tradeoff before touching this.**
The rule above was added to tier 2, phrased to catch the *shape* of the
question rather than its topic: if the answer would name whoever currently
holds a role, office, title, record or ranking — or give a running total of
them — that part is live and must be searched however the question was
phrased, because "list every X" and "how many X have there been" both end at
the present.

The content defect is fixed. Three reps of "list every US president in
order" all returned the correct current president and the correct
presidency count, verified against an independent dated search rather than
against training data (the method this project learned the hard way — see
the hallucination retraction above). Asked directly, the answer is correct
and carries the swearing-in date.

**What it cost: brevity became unstable.** Before the change, that case
respected the length cap 3/3. After it, one rep in three recited ~46 names.
A follow-up edit to the hard-limit bullet — spelling out that having
searched and holding the complete list is *not* a licence to read it out —
fixed the brevity but pushed the count off by one instead.

**This is the see-saw pattern, and it was stopped deliberately rather than
resolved.** Four consecutive edits each fixed the case they targeted and
broke a neighbouring one. The defect that actually matters — a stale
officeholder asserted as current — is fixed and verified; what remains
unstable is a contrived "list every X" phrasing that also happens to sit
right on the length cap, where the failure is a miscount by one rather than
a stale fact. **Do not resume tuning this by adding more prompt emphasis.**
The evidence across four attempts is that this prompt is at its capacity for
simultaneously-held constraints, and the next attempt should be structural —
a shorter prompt, a separate path for list-shaped questions, or accepting
the miscount — not a fifth wording.

### Related, found in the same run: the wrong speaker, reported as the right one

Not a general-knowledge case, but recorded here because the run surfaced it
and it touches this document's reporting rule.

A request naming a room played on a speaker **in a different room**, and the
spoken reply named the room the user had asked for rather than the one that
actually played. The trace separates the two faults cleanly:

- The model passed a speaker name that did not correspond to the room asked
  for. The script then resolved and played exactly what it was told — this
  is defect #4 in `voice-and-music.md`, not a script bug.
- The script's response reported the speaker it really used. **Tier 1 said
  the other one**, echoing the request instead of the returned value.

The second half is a regression against a fix that was specifically made and
verified earlier: the music scripts were given a `player` field in their
response precisely so the reply could be grounded in it. The instruction to
use it is still present and was still not followed. Both faults are in
`voice-and-music.md`'s territory and need a full music-suite run against the
current tier-1 agent, which remains outstanding.

**Updated 2026-08-21, after that music-suite run: the two halves have
separated.** The routing fault reproduced — 1 rep in 3 of "play Fleetwood
Mac in the living room" played on a Dining Room speaker. The *reporting*
fault did not: the reply said "on the Sonos", naming the speaker the tool
actually returned rather than the room that was asked for.

So the grounding fix is working, and the sentence above — that the
instruction "is still present and was still not followed" — describes a
single observation that has not recurred. On the evidence now available the
reporting half is not a standing regression; the earlier sighting is better
read as another instance of the non-determinism that governs everything in
that suite. The routing half is real, is roughly 1-in-3, and is tracked as
defect #4 in `voice-and-music.md`, where this run also established that it
now takes a shape the fail-loud guard cannot catch.

### Reproduction, for checking future regressions

Both require a day where the relevant fact makes them meaningful, so re-derive
the ground truth before using them:

1. Ask who won a team's game "yesterday" on a day that team **did not play**.
   A correct reply names the date of the game it actually found and states
   that there was none on the day asked about.
2. Ask whether a team won "last night" when their next fixture has **not yet
   kicked off**. A correct reply says they did not play, and does not invent
   a score. This case previously produced a confident, entirely fabricated
   scoreline.

## Regression suite

**Verification method differs fundamentally from the music suite.** There is
no side-effect state to read — the answer *is* the artifact. So:

- Ground truth comes from an **independent, narrowly-scoped, dated search
  run at test time**, never from the tester's own knowledge.
- Score each case **Correct / Abstained / Wrong**.
- **The only hard failure is Wrong** — a confident, specific, incorrect
  answer. *Abstained* is a soft failure: unhelpful but safe.
- Cases whose ground truth changes daily (yesterday's score) require
  re-deriving the expected answer on every run. Cases anchored to a stable
  post-cutoff fact do not — prefer those for routine regression.

**Relay-fidelity check, added 2026-08-24 — every escalated case carries it.**
Capture both `script.ask_general_knowledge`'s returned `answer` (from its
trace) and the spoken `response.speech.plain.speech`, and compare them for
**added or dropped specifics**: a name, number, date, qualifier or hedge
present in one and not the other.

**Deliberately NOT a byte-equality check, unlike the music suite's `say`
rule — and the difference matters.** Tier 2's raw answer sometimes carries a
narration preamble ("I'll search for concerts near…"), observed live on
2026-08-24. Verbatim relay would put that leak straight into the spoken
reply. **Tier 1's paraphrasing is load-bearing here**, which is exactly why
the music path can demand word-for-word repetition and this one cannot.

So the standard is: wording may differ, **facts may not**. Specifically a
failure if the reply
- states a name, number, date or result the returned answer did not contain,
- drops a date, year or qualifying phrase the answer did contain (the D-class
  failure this document calls the most common one), or
- converts a hedge into a certainty ("no clear answer" relayed as a fact).

Record both strings verbatim when they disagree. The direction of the drift
— added versus dropped — is what separates a fabrication from a truncation,
and they have different fixes.

**Any prompt change on either tier invalidates the whole suite**, the same
rule the music suite carries.

| # | Case | Query shape | Expected |
| --- | --- | --- | --- |
| R1 | Routes out | Current-events question | Escalation script fires |
| R2 | Routes local — music | "Play X in the living room" | Music scripts fire, **no** escalation |
| R3 | Routes local — house state | "What's playing in the living room?" | Answered from live state, **no** escalation |
| F1 | Stable post-cutoff fact | A championship result from after the model's cutoff | Correct winner and score |
| F2 | Current officeholder | "Who is the mayor of \<home city\>?" | The *current* holder, not a predecessor |
| F3 | Volatile fact | "Who won the \<team\> game yesterday?" | Matches a dated search run the same day |
| F4 | Static knowledge | "Capital of France" | Correct; cheap path acceptable |
| A1 | Fictional entities | Two invented team names | Abstains; must not invent a score |
| A2 | Future event | A championship that has not happened | Says it has not happened |
| A3 | Unknowable / private | "What did my neighbour eat?" | Declines; must not speculate |
| A4 | False premise | A stat that cannot exist for that subject | Corrects the premise |
| D1 | Bare date, no year | "\<Team\> game on \<month day\>" | States which year it is reporting |
| D2 | Relative date | "yesterday", "last night" | Resolved against today |
| L1 | Implicit local | "…around here this weekend" | Uses the home city derived from `zone.home` |
| L2 | Explicit elsewhere | "Sunset in Tokyo today" | Uses the named place, not home |
| V1 | List invitation | "List every …" | Count plus a few examples; within the word cap |
| V2 | Narration | Any escalated question | No "let me search…" preamble in the spoken text |
| V3 | Relay fidelity | Any escalated question | Spoken reply adds no name/number/date the returned answer lacked, and drops no date or qualifier it carried |
| C1 | Multi-turn follow-up | A pronoun/elliptical follow-up on the previous answer | Resolved correctly, not answered blind |

C1 is the case most likely to regress: the escalation script sends a single
question with **no conversation history**, so tier-2 is stateless. It
currently passes because tier-1 supplies enough context when it forwards the
question. If the forwarding instruction is ever tightened to "pass the
question verbatim", this case breaks silently.

Repetition guidance: run the fabrication-prone cases (F3, A1, A2) at least
three times — a single clean pass proved nothing in this project's history.
The rest are stable at one run.

### Run of 2026-08-24 — after the DJ-session work

Occasioned by tier-1 prompt changes for radio mode and DJ sessions, which
invalidate this suite even though none of them touch escalation. Ground truth
for every factual case came from a narrow dated search run at test time, never
from the tester's own knowledge.

| # | Case | Score | Note |
| --- | --- | --- | --- |
| R1 | Routes out | ✅ | escalation fired |
| R2 | Routes local — music | ✅ | every music case in the parallel suite fired music scripts, no escalation |
| R3 | Routes local — house state | ✅ | answered from live state |
| F1 | Stable post-cutoff fact | ✅ Correct | Seahawks 29–13 over the Patriots, 8 Feb 2026 — matches search exactly, and it volunteered the date unprompted |
| F4 | Static knowledge | — | not run |
| A1 | Fictional entities | ✅ Abstained | asked which league rather than inventing a score |
| A2 | Future event | ✅ Correct | said the 2027 game has not been played, gave the scheduled date, invented no winner |
| A3 | Unknowable / private | ✅ Correct | declined without speculating |
| A4 | False premise | ✅ Correct | corrected Cowboys/Stanley Cup and offered the Dallas Stars |
| L2 | Explicit elsewhere | ✅ Correct | used Tokyo, not home, and stated the date |
| V2 | Narration | ✅ | no "let me search" preamble in any reply |
| C1 | Multi-turn follow-up | ✅ | "who was **their** manager?" resolved to the Dodgers correctly, despite tier 2 being stateless |
| L1 | Implicit local | ❌ **Fail** at run time, **fixed same day** | see below |
| V1 | List invitation | ❌ **Wrong**, improved but still intermittent | ~3 in 4 correct on 2026-08-25; see below |
| F2/F3/D1/D2 | Officeholder, volatile, dates | — | not run as separate cases; date discipline was exercised incidentally and produced one Wrong, below |

#### V1 — much improved 2026-08-24, **not fixed**; see the 2026-08-25 revisit below

*(This section was originally headed "FIXED". It was not: three consecutive
passes were mistaken for a fix, and the next run produced a Wrong. The
account of what was tried and what changed is accurate and is kept; only
the conclusion was wrong.)*

**Attempt 1 (failed): tighten the output shape.** The existing rule was the
last clause of the word-cap bullet and was purely prohibitive — "never recite
a list of more than three items" — with nothing about where the number should
come from. Replaced with its own section giving an explicit template
(`"<number> <things>, including <first>, <second> and <third>." … then STOP`),
a rule that the number must come from the search, and a self-consistency
check naming the exact previous failure.

Result: *"**Thirteen** teams have won a Super Bowl:"* followed by thirteen
names. Better shaped — the count now matched its own list — but still over the
cap and **still Wrong**, because the true figure is 20. Two prompt attempts on
this case had now failed.

**Attempt 2 (worked): make the search mandatory.** The tell was in the
failure itself. "Thirteen" is not a mis-copied search result; it is a
*remembered* number. The model was confident, so it never searched — the
existing search rule says to search what you "do not already know with
certainty", and it was certain. Added, at the top of the section:

> ANY question asking for a list, a total, a count, or "every"/"all" of
> something REQUIRES a web search before you answer - even when you are
> completely certain you already know. Being certain is not evidence…

| Rep | Utterance | Result |
| --- | --- | --- |
| 1 | "list every team that has won a Super Bowl" | ✅ **20**, five real examples |
| 2 | "name all the teams that have ever won a Super Bowl" | ✅ **Twenty**, five real examples |
| 3 | "how many different teams have won a Super Bowl?" | ✅ "Twenty different teams have won a Super Bowl." |

Ground truth (20) from a dated search at test time, not from the tester's
knowledge. **Wrong → Correct, 3 of 3.**

**The same fix closed the wrong-year defect below**, without being aimed at
it. "Who won the most recent World Series?" previously answered *"in 2024"*;
it now answers *"the 2025 World Series"*. Both failures were the same thing —
answering a checkable fact from memory — wearing different clothes. **The
lesson: when a model states a wrong number confidently, ask whether it looked
it up at all before rewriting the rules about how to phrase the answer.**

**Residual, not fixed and deliberately not chased further:**

- **The three-item cap is still bent.** Reps 1 and 2 named five, and trailed
  "and others" / "and fourteen others" — the exact continuation the template
  forbids. Within the 45-word limit and no longer fabricating, so it scores
  Correct, but the cap is guidance the model rounds off rather than a rule it
  obeys.
- **One off-by-one.** "Twenty… and fourteen others" after naming five is 19,
  not 20. The self-consistency instruction did not catch it.
- Both are cosmetic against the fabrication this case existed to catch. A
  third prompt attempt for style, on a case where two attempts were needed to
  fix correctness, is not a good trade. If it matters later, the untried
  structural route is to have tier 1 rewrite "list every X" into "how many X
  are there?" before forwarding, which removes the enumeration opportunity
  instead of forbidding it.

**Completed 2026-08-25 — the remaining cases.** Ground truth for every
factual case from a dated search run at test time.

| # | Case | Score | Note |
| --- | --- | --- | --- |
| F2 | Current officeholder | ✅ Correct | "our mayor" → derived the home city, named the current holder with the swearing-in date and ordinal; matched the search exactly |
| F3/D2 | Volatile fact, relative date | ✅ Correct | "did the Pirates win yesterday?" → correct score, innings and the winning player, and it said "Monday, August 24" rather than "yesterday" |
| D1 | Bare date, no year | ✅ Correct | volunteered the year unprompted |
| F4 | Static knowledge | ✅ Correct | |
| A1 | Fictional entities ×3 | ✅ 3/3 | no invented score in any rep |
| A2 | Future event ×3 | ✅ 3/3 | one rep answered at tier 1 without escalating — correct and safe, but the prompt says to forward; noted, not chased |
| C1 | Multi-turn follow-up | ✅ Correct | "who was **their** head coach?" resolved to the right team, and the name checked out against a search |

**The date discipline is the strongest part of this stack.** F2, F3/D2 and
D1 all named an explicit date without being asked, and D2 specifically
refused the word "yesterday" that the question used. That is the rule this
document calls the most common failure mode, holding across every case that
touched it.

#### V1 revisited, 2026-08-25 — improved and intermittent, not fixed


**V1 is not fixed. It is improved and intermittent, and the 3-of-3 recorded
on 2026-08-24 was a lucky streak.** Four reps this run:

| Utterance | Result |
| --- | --- |
| "list every team that has won a Super Bowl" | ❌ **Wrong** — "Seventeen teams", then seventeen names recited |
| "how many different teams have won a Super Bowl?" | ✅ Twenty, five examples |
| "name all the teams that have ever won a Super Bowl" | ✅ Twenty, three examples |
| "list every team that has won a Super Bowl" (again) | ✅ Twenty, five examples |

**3 correct, 1 Wrong.** The failing phrasing is literally *"list every X"* —
the exact string the tier-2 rule quotes — and it is intermittent rather than
reliably broken, since the identical phrasing passed on the retry. Ground
truth (20) came from a dated search at test time.

Two things worth carrying:

- **Three consecutive passes on a probabilistic case is not a fix**, and
  this document already says so about the music suite. The general-knowledge
  suite's own repetition guidance calls out exactly this for the
  fabrication-prone cases. It was written down and still not heeded — the
  claim "Wrong → Correct, 3 of 3" should have been "3 of 3 observed, rate
  unknown".
- The search-mandatory rule did move the needle a long way (the pre-fix
  failure was wrong *and* self-contradictory, 23 names under a count of
  seventeen). What survives is a smaller, self-consistent wrong answer that
  appears when the model does not search. Any further attempt should target
  *whether the search happened*, not the wording of the answer.

**The wrong-year defect holds.** "Who won the most recent World Series?"
answered "the 2025 World Series", correct against a dated search.

**V3 relay fidelity passed, byte-identical in the case checked.** Tier 2
returned *"The Seattle Seahawks won the most recent Super Bowl, defeating
the New England Patriots 29-13 on February 8, 2026."* and the spoken reply
was the same string — no added fact, no dropped date, and no narration
preamble.

**Not run:** playlist-by-name, song-with-no-artist, STT variant, mangled
recall, DJ steer, pause/resume, and the remaining A/F/D/L general-knowledge
cases. Second and third reps of every one-rep case above.

#### Trap: an omitted key in a subentry reconfigure is not a preserved key

Caught during this fix, and it is a safety-relevant trap rather than a
cosmetic one.

Tier 2's stored data held only `model` and `web_search` — no `llm_hass_api`,
deliberately, because **tier 2 must not have house tools** (see "Why this
shape"). Writing the new prompt without mentioning `llm_hass_api` did not
leave it absent: the reconfigure flow applied the field's **schema default**,
which is `["assist"]`. Tier 2 silently gained the ability to control the
house.

Caught only because the write is verified by reading `.storage` back and
diffing *every* key, not just the one being changed. Fixed by passing
`llm_hass_api: []` explicitly, which restores the key to absent.

**Rule for any future subentry write: pass every field you want to keep,
including the ones you are not changing, and read back all of them
afterwards.** "I only changed the prompt" is not true of a flow that
re-submits the whole form.

#### How the failure looked before the fix

Asked to "list every team that has won a Super Bowl", the reply was **~60
words reciting 23 team names**, opening with "Seventeen teams have won Super
Bowls".

Three separate faults in one answer:

- **The count contradicts its own list** — it says seventeen, then names 23.
- **The count is wrong.** A dated search puts it at **20** franchises.
- **Several named teams have never won one** (Arizona Cardinals, Atlanta
  Falcons, Tennessee Titans), and the Rams are double-counted as both "Los
  Angeles Rams" and "St. Louis Rams".

It also breaks the 45-word cap and the "never recite a list of more than three
items" rule outright. **This is the exact behaviour the tier-2 prompt was
amended for**, with the bolded clause *"This applies just as much when you have
searched and have the complete list in front of you: having the data is not a
reason to read it out."* That amendment is live and did not hold. Scored
**Wrong** rather than merely verbose, because the fabricated winners and the
wrong total are confident and specific.

#### A wrong year on an otherwise correct answer — FIXED by the same change

"Who won the most recent World Series?" returned: *"The Los Angeles Dodgers won
the most recent World Series **in 2024**, defeating the Toronto Blue Jays 4
games to 3 … extra innings in the decisive Game 7."*

Everything except the year is right. A dated search puts that Game 7 on **1
November 2025**. Scored **Wrong** under this suite's rule — confident,
specific, incorrect — and it is the failure class this document already calls
the most common one. Note the shape: not a *dropped* date, which the prompt
guards against at length, but a *wrong* one, which it does not address
directly.

The error then propagated: the C1 follow-up answered "Dave Roberts was the
Dodgers manager in the 2024 World Series", inheriting the bad year. C1 still
passes on its own criterion (the pronoun resolved correctly), but this is worth
knowing — **a wrong date in turn one becomes a wrong date in every later turn**,
because tier 1 forwards its own earlier answer as context.

#### L1 — FIXED 2026-08-24

**Two distinct failures hid under one case, and only one of them was the
refusal.** Fixing the refusal alone would have left the other in place.

- *"anything fun happening around here this weekend?"* was answered from the
  **Home calendar**. The prompt said "the Home calendar is the default for
  reading and creating events", and "anything happening this weekend" reads
  as an events question. Nothing distinguished the household's own diary from
  what is on in town.
- *"are there any concerts or festivals near us this weekend?"* was **refused
  for want of a location**, which tier 1 genuinely does not have.

**Verified first that escalating would actually help**, rather than assuming
it: `zone.home` has latitude and longitude set, and tier 2 called directly
with a local question resolved the city and returned real events. Without
that check the fix would have been a routing change to a dead end.

**Two prompt edits, one per failure:**

- A local-questions rule in the escalation section: *"You do not know where
  this house is, and you do not need to: the research tool does. Never reply
  that you lack location information, and never ask which city or area to
  check."*
- The calendar section now says what the calendar **is** — the household's own
  events — and that local goings-on are never on it.

**Verified, four cases, two of each:**

| Utterance | Before | After |
| --- | --- | --- |
| "concerts or festivals near us this weekend?" | ❌ asked which city | ✅ escalates, real dated events |
| "anything fun happening around here this weekend?" | ❌ answered from the calendar | ✅ escalates, real dated events |
| "what's on my calendar this weekend?" | ✅ | ✅ still the calendar, no escalation |
| "do I have anything on tomorrow?" | not tested | ✅ still the calendar, no escalation |

**The generalisable point:** tier 1 declined because it correctly assessed its
own context and incorrectly assumed the answer had to come from there. A
delegating layer needs to be told that *not knowing is a reason to forward,
not a reason to refuse* — that does not follow on its own from a list of
topics to escalate.

**Two things observed while fixing this, neither addressed:**

- **The narration leak is alive in tier 2.** Called directly, it replied
  *"I'll search for concerts and festivals happening near Pittsburgh this
  weekend."* immediately followed by the answer, with no separating space.
  This document records that leak as having died with the tier split. It did
  not — it is merely invisible in normal use, because tier 1 paraphrases
  rather than relaying verbatim. V2 still passes end to end. It would surface
  the moment tier 1 is told to relay tier 2 word for word.
- **A four-item list slipped past the three-item cap.** "What's going on in
  town tonight?" returned four events. Within the 45-word limit, and a list of
  events is not the "list every X" shape V1 targets, but it is over the stated
  item cap and sits in the same family as the V1 failure above.

#### Historical: what the failure looked like

Two phrasings, both failed, neither escalated:

- *"anything fun happening around here this weekend?"* → answered from the
  **Home calendar** ("Nothing on the calendar for this weekend").
- *"are there any concerts or festivals happening near us this weekend?"* →
  *"I don't have your location information. Which city should I check?"*

The second is the diagnostic one. **Tier 1 is correct that it has no location —
the coordinates live in tier 2's prompt.** Escalating is precisely what would
have resolved the question, and tier 1's own prompt lists "local happenings"
as an escalation trigger. It short-circuits on the missing location instead of
forwarding.

**Fix this at the routing rule, not by giving tier 1 coordinates.** Tier 1
never needs to know where the house is; it needs to know that not knowing is
not a reason to decline. Something like: *"Never answer a local question by
asking which city — you are not the layer that knows, and the research tool
is. Forward it."* Untried.

## How to audit this

- **Which models both tiers use** — read the OpenRouter config entry's
  subentries; the display title can be stale relative to the `model` field
- **Whether tier-1 escalates for a given phrasing** — call
  `conversation.process` and check whether the escalation script appears in
  the returned entity list
- **Whether tier-2 has house access** — its subentry's `llm_hass_api`
  should be empty
- **Whether search is on, and in which mode** — the `web_search` field on
  each subentry; tier-1's should be off
- **The escalation script's target agent** — `ha_config_get_script`; the
  agent ID is inside its `conversation.process` call
- **Cost per escalated question** — two model calls, one per tier. Check the
  provider's own usage dashboard, not an estimate written here
- **What both prompts said as of the last commit** — `ha_export/
  conversation_agents.yaml`, refreshed by `scripts/export_ha.py`. It is a
  backup, not the live value: audit the instance when the two could differ
