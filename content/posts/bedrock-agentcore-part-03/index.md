---
title: "Building an Agent on Bedrock AgentCore, Part 3: Giving It a Memory That Outlives the Process"
date: 2026-06-27
draft: true
slug: "bedrock-agentcore-part-3"
tags: ["ai", "agents", "bedrock", "agentcore", "python", "strands"]
series: ["Building an Agent on Bedrock AgentCore"]
description: "The agent runs in the cloud now, and it still forgets everything the instant the box recycles. This post wires it to AgentCore Memory — durable conversation that survives a cold start, then long-term memory so it remembers you across sessions. Two honest scars included, free of charge."
cover:
    image: "agentcore_memory.webp"
    alt: "A notes agent with a filing cabinet bolted on where its short-term memory used to evaporate"
    relative: true
    hidden: false
---

[Last time](/posts/bedrock-agentcore-part-2/) we put the agent in the cloud behind a real endpoint. It works, it scales, two users don't trample each other — and it has the retention span of a mayfly. Notes live in a Python list that evaporates the instant the instance recycles. It remembers your note right up until it doesn't, which is arguably worse than a goldfish: at least the goldfish never promised.

This post gives it **AgentCore Memory**. Two kinds, in order of how much they'll surprise you:

- **Short-term:** the conversation survives a cold start. Durable, not "alive while the box stays warm."
- **Long-term:** it remembers *you* — your stated preferences, the facts you've dropped — across completely separate sessions, days apart.

The agent logic barely moves. The interesting parts are one new module, a deliberate provisioning detour, and two scars I'll show you so you don't have to bleed for them yourself.

## Get the Code

This post is the `post-03-memory` tag. As always, the diff is the lesson:

```bash
git clone https://github.com/kenkitts/bedrock-agentcore.git
cd bedrock-agentcore
git checkout post-03-memory
git diff post-02-runtime..post-03-memory     # the whole post, in one diff
```

## It Already Kind of Remembers — and That's the Trap

Before we add anything, an uncomfortable fact: the Part 2 agent *already* has a kind of memory, and if you don't understand it you'll either think Memory is redundant or ship something broken.

Runtime gives every session its own dedicated microVM. Reuse the same session id and your requests get routed back to the same warm box:

```bash
SID=notes-demo-session-0001-aaaaaaaaaaaa
agentcore invoke --session-id "$SID" "remember I like terse summaries"
agentcore invoke --session-id "$SID" "what did I just say?"   # it knows
```

It "knows" because our `Agent` object is built once and lives in that microVM's RAM, holding the conversation in memory between calls. That's not a feature we built. That's just a process that hasn't died yet.

And it dies. AgentCore's own docs are blunt about it: the microVM is **ephemeral**, its memory is wiped on stop, and it stops after ~15 minutes idle, or 8 hours max, or whenever a health check sneezes. So the continuity you get for free is:

- **Ephemeral** — gone when the box recycles.
- **Time-boxed** — a conversation that pauses over lunch can come back with amnesia.
- **Single-session** — nothing carries to a *different* session id, and nothing carries across users, ever.

This is the cloud equivalent of "it works because I haven't closed the terminal." Useful, real, and absolutely not the thing you build a product on. AgentCore Memory is the durable version — and, for the long-term case, something the warm box could never do no matter how long it lived.

## What "Memory" Actually Means Here

AgentCore Memory is a managed store built for two jobs:

**Short-term memory** is the raw conversation, persisted as events keyed by an *actor* (who) and a *session* (which conversation). When the agent boots, it reads the events back and rehydrates the transcript. Cold start, different instance, doesn't matter — the conversation is in durable storage, not in a process you're hoping stays up.

**Long-term memory** is the part that earns its keep. You attach *strategies* to the memory, and the service asynchronously reads the conversation and extracts durable insights into namespaces you query later:

- **semantic** — facts ("the Q3 doc is in the shared drive")
- **user-preference** — preferences ("I like terse, bullet-point summaries")
- **summarization** — running summaries of sessions

Facts and preferences are scoped to the *actor*, not the session — which is exactly why they survive into a brand-new conversation next week. That's the headline. The warm-box trick can fake short-term continuity; it can't do this.

## The Only Real New Code

The wiring is almost insultingly small, because Strands ships an AgentCore Memory **session manager** that does the event-writing and rehydration for you. The whole integration is one module:

```python
# notes_agent/memory.py
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig, RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from notes_agent.config import ACTOR_ID, MEMORY_ID, REGION

# These MUST match the namespaces the strategies were created with.
FACTS_NS = "/facts/{actorId}/"
PREFERENCES_NS = "/preferences/{actorId}/"
SUMMARIES_NS = "/summaries/{actorId}/{sessionId}/"

def build_session_manager(session_id, actor_id=None, memory_id=None, region=None):
    memory_id = memory_id or MEMORY_ID
    if not memory_id:
        return None                      # no memory configured → memoryless agent

    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        actor_id=actor_id or ACTOR_ID,
        session_id=session_id,
        retrieval_config={               # this is what turns ON long-term recall
            PREFERENCES_NS: RetrievalConfig(top_k=5, relevance_score=0.5),
            FACTS_NS: RetrievalConfig(top_k=10, relevance_score=0.3),
            SUMMARIES_NS: RetrievalConfig(top_k=3, relevance_score=0.3),
        },
    )
    return AgentCoreMemorySessionManager(config, region_name=region or REGION)
```

Two things worth pinning down:

**Short-term comes free with the session manager; long-term does not.** Just attaching the manager persists and restores the conversation. Long-term retrieval only happens if you pass `retrieval_config` — it's the switch that makes the agent actually *query* what it learned about you. Leave it off and you have durable conversations but an agent that never recalls a preference. (I know this because I left it off first.)

`build_agent` gains exactly one optional argument, so the memoryless Part 1/2 agent still exists when you want it:

```python
def build_agent(session_manager=None) -> Agent:
    ...
    if session_manager is not None:
        kwargs["session_manager"] = session_manager
    return Agent(**kwargs)
```

And `app.py` changes its mind about one thing from Part 2. Back then we built the agent **once** at module load, because it was stateless and reuse was a free speedup. Memory is session-scoped, so that optimization has to go — we build a per-invocation agent bound to *this* session:

```python
@app.entrypoint
def invoke(payload: dict, context) -> dict:
    prompt = payload.get("prompt", "")
    # The Runtime hands us the session id; reuse it as the memory session id.
    session_id = getattr(context, "session_id", None) or uuid.uuid4().hex
    session_manager = build_session_manager(session_id=session_id)
    agent = build_agent(session_manager=session_manager)   # None → memoryless
    return {"result": str(agent(prompt))}
```

That `context` argument is new: AgentCore passes request metadata alongside the payload, and `context.session_id` is the Runtime session — the same id that made the warm-box trick work. We reuse it so the durable memory lines up with the session the caller already thinks in. (The snippet omits one line from the repo — an optional `actor_id` pulled from the payload; *who* the memory belongs to is a single hardcoded user for now, and real per-user identity is Part 5's job.) Building the manager also triggers the rehydrate read, so a new instance picking up an old session id pulls the conversation back from storage. That's the whole durability story in one line.

## Scar #1: The CLI Will Provision Memory, but It Hides the Keys

The obvious way to create the memory resource is `agentcore add memory --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE`. One flag, done. And it *works* — right up until long-term recall returns nothing and you can't figure out why.

The problem: long-term retrieval queries a specific **namespace path**, and the namespace strings in your code have to match the ones the strategies were created with, exactly. The CLI picks those namespaces for you and doesn't surface them. So you'd be guessing at strings the service generated, and a mismatch fails *silently* — short-term memory keeps working, so everything looks fine until the cross-session demo comes up empty.

So we provision the resource ourselves, with the namespaces spelled out, in a tiny one-time script:

```python
# scripts/create_memory.py  (run once)
client.create_memory_and_wait(
    name="notesAgentMemory",
    strategies=[
        {"semanticMemoryStrategy":       {"name": "FactExtractor",
                                          "namespaceTemplates": ["/facts/{actorId}/"]}},
        {"userPreferenceMemoryStrategy": {"name": "PreferenceLearner",
                                          "namespaceTemplates": ["/preferences/{actorId}/"]}},
        {"summaryMemoryStrategy":        {"name": "SessionSummarizer",
                                          "namespaceTemplates": ["/summaries/{actorId}/{sessionId}/"]}},
    ],
    event_expiry_days=7,
)
```

Those namespaces are the *same strings* as in `memory.py`. Change one, change both. Run it once and record the printed id:

```bash
python scripts/create_memory.py        # prints NOTES_AGENT_MEMORY_ID=<id>
```

Where does the id go? Here's a small, dumb wrinkle: the AgentCore CLI has no way to inject an environment variable into the deployed runtime. So the id can't live in the environment for the cloud case — it has to ship *with the code*. We paste it into `config.py`, the same one place the model id and region already live:

```python
MEMORY_ID = os.environ.get("NOTES_AGENT_MEMORY_ID", "")   # paste your id as the default
```

(For local runs you can skip the paste and just `export NOTES_AGENT_MEMORY_ID=<id>`.)

## Scar #2: BYO Memory Means BYO Permissions

We deploy with the same bring-your-own-code flow as Part 2, with one telling flag:

```bash
agentcore add agent --name notesAgent --type byo \
    --framework Strands --model-provider Bedrock --memory none \
    --code-location ../bedrock-agentcore --entrypoint app.py --language Python
agentcore deploy -y
```

`--memory none`. We're not asking the CLI for managed memory — we built our own so we could own the namespaces. The catch arrives on the first invocation:

```
AccessDeniedException: User: arn:aws:sts::...:assumed-role/AgentCore-notesAgentRunti-.../...
is not authorized to perform: bedrock-agentcore:ListEvents on resource: .../memory/notesAgentMemory-...
```

The agent authenticates perfectly. It just isn't *allowed* to touch the memory. Because we said `--memory none`, the execution role the CLI generated has zero Memory permissions — it has no idea our memory exists. **You brought your own resource; you bring its permissions too.**

The fix is a least-privilege inline policy scoped to the one memory ARN, granting exactly the five data-plane actions the session manager calls — restore (`ListEvents`, `GetEvent`), persist (`CreateEvent`), redact (`DeleteEvent`), and long-term recall (`RetrieveMemoryRecords`):

```bash
# the role name is in `agentcore status`, or staring at you in the error above
scripts/grant_memory_access.sh <execution-role-name> "$NOTES_AGENT_MEMORY_ID"
```

IAM propagates in a few seconds. Then it works. The policy itself lives in [`scripts/memory-policy.json`](https://github.com/kenkitts/bedrock-agentcore/blob/post-03-memory/scripts/memory-policy.json) if you'd rather read it than run it. Good news for cleanup: that policy is attached to the runtime's execution role, so it gets deleted *with* the role at teardown — one less thing to forget.

## Running It Locally First

Same instinct as every post: prove it on your machine before paying for it. With `NOTES_AGENT_MEMORY_ID` exported, the REPL wires memory exactly like the cloud does:

```bash
export NOTES_AGENT_MEMORY_ID=<id>
python -m notes_agent.main
# Notes assistant (local, memory on). Type 'exit' or Ctrl-D to quit.
```

Each REPL run gets a fresh session id, which is a feature: it means long-term memory (preferences, facts — actor-scoped) carries across runs, while the short-term conversation doesn't — the same behavior a brand-new cloud session would show. Without the memory id set, you get the plain Part 1 agent. No memory, no AWS, no surprises.

## See It Actually Remember

The demo only proves something if it defeats the warm-box trick. So we use *two* sessions. First conversation, tell it things:

```bash
SID=notes-demo-session-0001-aaaaaaaaaaaa
agentcore invoke --session-id "$SID" "I prefer terse, bullet-point summaries"
agentcore invoke --session-id "$SID" "the Q3 planning doc is in the shared drive"
```

Now a **completely different session** — a new microVM, an empty conversation, the warm box from before long since irrelevant:

```bash
agentcore invoke --session-id notes-demo-session-0002-bbbbbbbbbbbb \
    "how do I like my summaries, and where's the Q3 doc?"
# -> Terse and bullet-pointed. The Q3 planning doc is in the shared drive.
```

It recalls both, across a session boundary that wiped every trace of short-term context. That's long-term memory doing the one thing session affinity fundamentally cannot.

One honest caveat: long-term extraction is **asynchronous**. The service reads the conversation and distills facts and preferences on its own schedule, not the instant you speak. If session two comes up blank immediately, it isn't broken — the extraction just hasn't finished. Wait a minute and ask again. (Short-term recall within a session is immediate; it's only the cross-session insight that needs a beat.)

## Clean Up — Now There Are Two Bills

Part 2 added a Runtime to tear down. Part 3 adds a Memory resource, which is billable and hoards events until they expire. Delete it along with everything else:

```bash
# from the repo root: deletes the Memory resource, then points you at the Runtime teardown
scripts/teardown_03_memory.sh <memory-id>
```

Tearing down the Runtime removes the execution role, which takes the inline memory policy with it. The only thing that lingers is whatever you forget to run this on, so run it.

## Next: The Notes Are Still a Lie

The agent remembers your conversation and your preferences now. What it *still* doesn't have is anywhere real to put your actual notes — `add_note` is dumping them into the same in-memory Python list it always was, which evaporates with the instance exactly like before. We gave the agent a memory and left the filing cabinet made of smoke.

Next post: **AgentCore Gateway**, where the notes finally get a real home — an actual notes/bookmarks API, exposed to the agent as governed MCP tools. The agent stops pretending to store things and starts calling something that does.
