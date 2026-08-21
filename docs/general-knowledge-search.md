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

### Open defect: "yesterday" is asserted without checking the date

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

**Two lessons for the fix, neither of which is more prompt emphasis:**
relative day-words are the failure surface, so the answer should carry the
date it actually found rather than a word like "yesterday"; and "no event
happened in that window" is a distinct answer the model currently reaches
only sometimes, rather than a case it is required to consider.

Recorded rather than fixed, so the fix can be verified against a stated
reproduction instead of a vague memory.

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
| C1 | Multi-turn follow-up | A pronoun/elliptical follow-up on the previous answer | Resolved correctly, not answered blind |

C1 is the case most likely to regress: the escalation script sends a single
question with **no conversation history**, so tier-2 is stateless. It
currently passes because tier-1 supplies enough context when it forwards the
question. If the forwarding instruction is ever tightened to "pass the
question verbatim", this case breaks silently.

Repetition guidance: run the fabrication-prone cases (F3, A1, A2) at least
three times — a single clean pass proved nothing in this project's history.
The rest are stable at one run.

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
