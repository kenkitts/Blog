---
title: "Strands' Model Router: Let the Cheap Model Answer 'Hi'"
date: 2026-09-10
draft: false
slug: strands-model-router
tags:
  - ai
  - agents
  - strands
  - bedrock
  - aws
description: Strands' ModelRouter lets a cheap model handle small talk and a capable one handle the hard stuff, decided by a classifier call before every turn. Here's how it works.
cover:
  image: model-router.webp
  alt: A signpost splitting into a cheap path and an expensive path
  relative: true
  hidden: false
---

I'm building a [travel-planning agent](https://github.com/kenkitts/travel-planning-agent), the kind of thing that answers "plan a seven-day trip to Kyoto" and also "hi." Those two messages should not route to the same model. One needs a capable model with tool access and multi-step reasoning. The other needs a model that says "hi" back without deep thinking. The AI equivalent of hiring a 500-dollar-an-hour attorney to make small talk.

Strands Agents has an answer for this baked into the SDK: `ModelRouter`. Point it at two or more models, hand it a classifier, and it picks which one serves each turn before the real call ever happens. Here's how the pieces fit together.

## The Shape of It

A `ModelRouter` wraps a list of `RoutingCandidate` objects, each one a stateless model plus a name, a description, and optional metadata, along with a `RoutingStrategy` that decides which candidate serves a given turn. You hand the whole router to `Agent(model=router)` and nothing else about your code changes. The agent still calls `.stream_async()`, tools still dispatch the same way. From the outside, it's still one model. From the inside, it's a shell game, and you're the one running it.

```python
router = ModelRouter(
    models=[capable_candidate, cheap_candidate],
    strategy=ClassifierStrategy(classifier_model, system_prompt=routing_policy),
)
agent = Agent(model=router)
```

Candidates must be stateless. Conversation history lives on the `Agent`, not on any one model provider, so the router can swap which model serves the call mid-conversation without the agent noticing anything happened. Any Strands `Model` subclass works as a candidate, and you're not locked to one provider: mix `BedrockModel` and `AnthropicModel` instances in the same router if you want. A candidate is just a model with a metadata attached.

Here's the actual construction from my agent: two serving candidates wrapping Anthropic's Sonnet and Haiku, plus a third model that never gets to answer a real question in its life.

```python
classifier_model = AnthropicModel(
    model_id="claude-haiku-4-5",
    max_tokens=64,
    temperature=0,
    streaming=False,
)
```

That's the whole classifier. It's the same Haiku model I'm about to use as the cheap serving candidate, but hobbled on purpose. `max_tokens=64` because its entire job is one forced tool call naming a candidate, not an essay on the meaning of life. `temperature=0` because I want the same input to produce the same routing decision every single time, not a mood. And `streaming=False` because nobody needs token-by-token suspense for "cheap or capable, pick one." This model exists purely to make a decision and shut up immediately afterward, the workplace ideal we all aspire to and none of us achieve.

Don't get clever and reuse the cheap candidate's model instance here just because it's the same underlying model ID. `ModelRouter` rejects duplicate instances across roles. Two separate objects, even for the same model, no exceptions, because apparently someone tried this before you did and it went badly enough to earn a guardrail.

```python
cheap_candidate = RoutingCandidate(
    haiku_model,
    name="cheap",
    description=(
        "Lower-cost model for purely conversational turns: greetings, "
        "small talk, recalling something already said in this chat."
    ),
)
capable_candidate = RoutingCandidate(
    sonnet_model,
    name="capable",
    description=(
        "Higher-capability model for turns needing tool calls (weather, "
        "places, search) or multi-step reasoning: itineraries, dates, "
        "places, budget math."
    ),
)

router = ModelRouter(
    models=[capable_candidate, cheap_candidate],
    strategy=ClassifierStrategy(classifier_model, system_prompt=routing_policy),
)
```

The `description` fields aren't fluff. They're the actual evidence the strategy reads to make its call, the only résumé line either candidate gets to submit. Declaration order doesn't bias the choice either; the router isn't swayed by seniority, so list order is free for other purposes, like the one below.

## Two Strategies, Two Different Jobs

`RoutingStrategy` is a small protocol: one method, `select(context, **kwargs) -> RoutingCandidate | None`, called before the first model call and again after any call fails without a retry claiming it. Strands ships two implementations, and conflating them is the fastest way to build a router that does nothing you intended.

**`FallbackStrategy`** is the default if you don't specify one, and it is not doing what you're hoping it does. It's ordered failover, not complexity-based routing: walk candidates in declaration order on failure, and after a success, favor whoever's failed least (ties broken by seniority again). A `max_switches` knob caps how many times it'll actually swap candidates per invocation. This is "keep trying things until one of them works," the on-call engineer's core competency, ported into a strategy class. It has no opinion whatsoever about whether your question was hard.

**`ClassifierStrategy`** is the one that actually does complexity-based routing: cheap model for the easy stuff, capable model for the stuff that matters. Before serving a turn, it makes one extra model call to that starved classifier model above, and asks it to pick a name via forced structured output. One multiple-choice question, answered as fast and as cheaply as the architecture allows, because the entire economic case for routing collapses if the thing deciding "should this be cheap" costs as much as just using the expensive model unconditionally.

Critically, the classifier's input is bounded, not the full conversation. It sees the latest user message, your system prompt, and the candidates' descriptions and metadata, each piece capped by its own character limit, but never the message history. Route a forty-message conversation and the classifier still only glances at the newest line. That's deliberate: the whole point evaporates the moment "decide cheaply" requires re-reading everything that's ever been said.

The default policy is "pick the least capable candidate that can still deliver a complete and accurate result," cheap by default, escalate when needed. You can override it with your own `system_prompt`, which is worth doing the moment the default's generic notion of "complexity" doesn't match yours. Mine isn't vibes-based; it's specific tools:

```python
ROUTING_POLICY = (
    "Select exactly one candidate for the latest message. Prefer "
    "cheap only for pure small talk: greetings, thanks, simple "
    "clarifying questions. Escalate to capable whenever the message "
    "mentions weather, specific places or dates, budget math, or an "
    "itinerary. When unsure, prefer capable: a wrongly-escalated "
    "simple turn just costs more, but a wrongly-cheapened complex "
    "turn gives the traveler a worse answer."
)
```

That last sentence isn't just a tiebreaker. It's half of a decision I made twice, in two different files, because I don't trust myself to only say a thing once.

## Fail Open, Not Fail Cheap

`ModelRouter` treats the first candidate in your `models` list as the default, the one served if the strategy ever declines to pick, whether from a classifier timeout, a malformed response, or any other day the classifier decides to have. I declared `capable` first, not `cheap`:

```python
return ModelRouter(
    models=[capable_candidate, cheap_candidate],  # capable first, on purpose
    strategy=ClassifierStrategy(classifier_model, system_prompt=ROUTING_POLICY),
)
```

That's the one-line version of a real tradeoff: when the classifier faceplants, do you fail toward "cheap but maybe wrong" or "expensive but probably right"? I picked expensive, and said so twice, once as list order, once as a prompt instruction nobody but the classifier will ever read. 

The math behind it: a classifier hiccup that routes a real question to the capable model costs a few extra cents, the kind of overspend that shows up as a rounding error on a bill nobody reads carefully. A classifier hiccup that routes it to the cheap model risks a traveler getting a confidently wrong answer about whether it's going to rain on their trip, with zero tool calls behind it and zero indication anything went wrong. Overspending is a shrug. Under-delivering is the failure mode that makes someone quietly stop using your product and never tell you why. Given that choice, I'd rather the router err toward doing too much, and `ModelRouter`'s "first candidate is the default" behavior turns that principle into a one-line decision about list order instead of a pile of exception-handling nobody wants to write.

## Retry vs. Switch

One more mechanic worth knowing before you touch this: a "failure round" starts at the opening candidate (or right after a success), and each candidate gets selected at most once per round. Same-model retries don't count as a fresh pick, which is the only thing stopping the router from ping-ponging between two candidates forever like a couple who can't agree on a restaurant. The actual order of operations on a failed call: Strands' own retry logic gets first crack at retrying the *same* model. Only if nothing claims that retry does the router ask its strategy for someone else. The replacement gets its own fresh retry budget, and a clean success reopens every other candidate's eligibility for next time, water under the bridge.

`ClassifierStrategy` specifically only makes the *opening* pick for a turn. If the selected candidate faceplants mid-serving and no retry claims it, `ClassifierStrategy` shrugs and lets the original error surface. It does not go find you a replacement. It's a routing decision, not a safety net, and it will not pretend otherwise. If you want both complexity-based routing *and* actual failover, that's a custom `RoutingStrategy` composing the two, where `ClassifierStrategy` picks the opener and `FallbackStrategy`-style logic catches it if that pick eats it. Strands doesn't ship the combo pre-built, but the protocol is small enough that rolling your own is doable without much effort.

## What You're Actually Getting

None of this changes your agent's public shape. Tools dispatch the same way, `stream_async()` streams the same events, and every consumer of the agent keeps working exactly as before. The only thing that moved is which model answers, decided fresh on every turn by a call cheap enough that making it doesn't defeat the entire point of routing in the first place. For a chatty, low-stakes turn, that's a Haiku-cost answer and nobody's the wiser. For a turn that needs tools and multi-step reasoning, it's the model that can actually pull it off. The router's whole value proposition is that you don't have to guess which one you need before the user's even finished typing. You just have to trust a much smaller, much cheaper model to guess correctly on your behalf.
