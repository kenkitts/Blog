---
title: "Building an Agent on Bedrock AgentCore, Part 2: Teaching It to Run Somewhere I Can Close My Laptop"
date: 2026-06-25
draft: true
slug: "bedrock-agentcore-part-2"
tags: ["ai", "agents", "bedrock", "agentcore", "python", "strands"]
series: ["Building an Agent on Bedrock AgentCore"]
description: "In Part 1 the agent ran on my laptop, which is a great place for software to live right up until you close the lid. This post hands the exact same agent to AgentCore Runtime — one new file, no changes to the agent — and gets it running in the cloud behind a real endpoint."
cover:
    image: "agentcore_runtime.webp"
    alt: "A notes agent lifted off a laptop and dropped into a managed cloud runtime box"
    relative: true
    hidden: false
---

[Last time](/posts/bedrock-agentcore-part-1/) we built a notes agent with Strands and ran it in a terminal. It worked. It also ran exactly as long as I left the terminal open, which is to say: it was production-ready in the same sense that a campfire is central heating.

This post fixes the "on my laptop" problem and nothing else. Same agent. Same tools. Same `build_agent()` from Part 1, not one line changed. The only new thing is *where it runs* — and the surprising part is how little code that takes.

**The agent doesn't change. The address it lives at does.**

## Get the Code

This post is the `post-02-runtime` tag, and the lesson is the diff — clone it and read along:

```bash
git clone https://github.com/kenkitts/bedrock-agentcore.git
cd bedrock-agentcore
git checkout post-02-runtime
git diff post-01-foundations..post-02-runtime    # the whole post, in one diff
```

One new source file (`app.py`), one new dependency, and the package shuffled to the repo root. That's the whole post — the rest is me explaining why.

## The Problem: An Agent Is Just a Process, and Processes Die

Strip away the AI and what you have is a Python process holding some state in memory, waiting for input on stdin. That's fine for a demo. It's useless the moment you want anything else to talk to it — a web app, a Slack bot, your phone, a cron job at 3 AM — because all of those need an *address*, not a terminal you're personally babysitting.

So the naive next step is the one we've all done: wrap it in a web server, write a Dockerfile, push it somewhere, wire up a load balancer, set up logging, figure out how two users hitting it at once don't stomp on each other's conversation. Congratulations, you've spent a week on plumbing and your agent is exactly as smart as it was on Friday.

That whole pile of plumbing is what AgentCore Runtime is for.

## What a "Runtime" Actually Buys You

Runtime is managed hosting purpose-built for agents. You hand it your agent; it gives you back a secure endpoint and runs the thing. The parts worth caring about:

- **A standard HTTP contract.** Your agent becomes a `POST /invocations` endpoint. That's the whole interface. Boring, and boring is correct — it's the same reason every printer on earth speaks the same protocol.
- **Session isolation.** Two users talking to your agent get genuinely separate sessions, not two threads quietly corrupting the same global dict. This is the bug you *would* have shipped, pre-fixed.
- **Scale-to-zero-ish economics.** It's consumption-priced. An idle agent isn't burning a server you're renting by the hour.
- **It doesn't care what's inside.** Strands, LangGraph, a raw loop — Runtime hosts the HTTP contract, not your framework opinions.

The mental model: Part 1's `while` loop is the engine. Runtime is the chassis, the wheels, and the registration plate that makes it legal to drive on a road other than your desk.

## The Only New Code: `app.py`

Here's the entire diff from Part 1. One file:

```python
# app.py
from bedrock_agentcore import BedrockAgentCoreApp

from notes_agent.agent import build_agent

app = BedrockAgentCoreApp()

# Build once at module load so warm invocations reuse the agent instead of
# reconstructing the model client on every request.
agent = build_agent()

@app.entrypoint
def invoke(payload: dict) -> dict:
    prompt = payload.get("prompt", "")
    result = agent(prompt)
    return {"result": str(result)}

if __name__ == "__main__":
    app.run()
```

That's it. That's the post, almost. Let me point at the three lines that matter:

`BedrockAgentCoreApp()` is the adapter. It turns a decorated Python function into the `POST /invocations` HTTP server Runtime knows how to host — so you don't write the Flask app, the route, the JSON parsing, or the Dockerfile's `CMD`.

`@app.entrypoint` marks *the* function that handles a request. The `payload` is the request body; we fish out `prompt`, run the same agent we built last post, and hand back the text. If you wanted token-by-token streaming you'd make this `async` and `yield` from `agent.stream_async(prompt)` — but this is a post about hosting, so we're keeping it a plain request-in, answer-out function.

`app.run()` starts the server. Locally that's a real HTTP server on port 8080. In the cloud, Runtime runs this same file. The code doesn't know or care which.

Building the agent at module load instead of inside `invoke` is the one bit of actual thought here: a warm instance reuses it across requests instead of standing up a new Bedrock client every single call. The loop, as ever, is blissfully unaware any of this is happening.

## One Layout Change, and Why the Cloud Forced It

If you're following from Part 1, the package moved: `src/notes_agent/` is now just `notes_agent/`, sitting right next to `app.py` at the repo root. That's not housekeeping — it's a scar.

A `src/` layout is tidy, but it only works if the package is *installed* (`pip install -e .` and friends). On your laptop that's invisible — you install once and forget. In a deployment container it is very much not invisible: the agent imported fine locally, deployed without complaint, and then face-planted on the first invocation with `ModuleNotFoundError: No module named 'notes_agent'`. The third-party libraries were there; *my own code* wasn't, because the editable install didn't survive the trip into the container.

The fix is to stop depending on an install at all. When Python runs a script, it puts that script's own directory on the import path. Put `notes_agent/` next to `app.py` and the import just works — laptop, container, anywhere — no install step, no `PYTHONPATH` incantation, nothing to forget.

**The lesson, free of charge: "works on my machine" and "imports in the container" are two different claims, and the gap between them is usually an install step you didn't know you were relying on.**

## Running It Locally First

Before paying AWS anything, run the server on your own machine. It's the exact thing that'll run in the cloud, which makes "works on my machine" an actually useful data point for once:

```bash
python app.py
# serving on http://localhost:8080
```

In another terminal, talk to it like the cloud will:

```bash
curl -X POST http://localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "remember that runtime is post 2"}'
# {"result": "Saved note #1."}
```

Same agent from Part 1, now answering over HTTP instead of a REPL. If that works, deployment is almost an afterthought.

## Shipping It to the Cloud

Deployment goes through the [AgentCore CLI](https://github.com/aws/agentcore-cli) — a Node tool, which is a slightly funny way to deploy a Python agent, but it's the front door AWS is putting its weight behind so we'll walk through it.

One wrinkle worth understanding before you copy-paste: the CLI is *project-oriented*. `agentcore create` doesn't bless your current directory — it scaffolds a whole new project directory of its own (config, a generated CDK stack, the works). That's great if you're starting from its template, and slightly in the way when, like us, you already have an agent and just want it deployed. So we lean on the "bring your own code" path: make a small **deployment project next to the repo** that points back at our `app.py`. Keeping it as a sibling (not nested inside the repo) is the difference between packaging your code and packaging a directory that contains itself.

```bash
npm install -g @aws/agentcore

# Make the deploy project alongside the repo. Alphanumeric name only —
# the CLI rejects hyphens.
cd ..
agentcore create --project-name notesAgentRuntime --no-agent
cd notesAgentRuntime

# Register THIS repo's app.py as a bring-your-own-code agent.
agentcore add agent --name notesAgent --type byo \
    --framework Strands --model-provider Bedrock --memory none \
    --code-location ../bedrock-agentcore --entrypoint app.py --language Python

agentcore deploy -y
```

The deploy project is pure scaffolding — a thin wrapper that knows how to ship our code. The agent still lives in the repo; this directory just holds the deployment paperwork. (`--memory none` is honest for now. The agent has no memory yet. That's literally the next post.)

Under the hood `deploy` does the week of plumbing I griped about earlier: it packages the code, builds a container image, pushes it to ECR, and stands up the Runtime — fronted by a CDK stack it generates, so the infrastructure isn't a black box you can't inspect. Then you talk to the deployed thing:

```bash
agentcore invoke "remember that runtime is post 2"
# -> Done! I've saved **"Runtime is post 2"** as note #1.
agentcore logs      # watch the loop run, in the cloud
```

Ask it in a *second* call what you just told it and you may get a blank stare — each invocation can land on a fresh instance with an empty list. That's not a bug to fix here; it's the cliffhanger for Part 3.

One security note worth saying out loud, because nobody enjoys finding out the hard way: that endpoint is **not** open to the internet. By default the agent uses `AWS_IAM` auth, so calls have to be SigV4-signed by a principal you allow — the CLI is signing as you. We are *not* standing up an open `POST` endpoint that any passerby can run up your Bedrock bill on. Actual per-user identity — "your notes" vs. "a stranger's notes" — is a different and harder problem, and it gets its own post (Part 5).

## Clean Up — and Yes, This Time There's a Bill

Part 1 ran on your laptop, so the cleanup section was a victory lap. Not anymore. You now have a deployed Runtime, a container image in ECR, and supporting resources, and AgentCore is consumption-priced — idle is cheap, but cheap isn't free, and "I forgot it was running" is the most expensive five words in cloud computing.

The repo ships a teardown script — run it from inside the deploy project (that's where the CLI's config lives):

```bash
cd ../notesAgentRuntime
~/bedrock-agentcore/scripts/teardown_02_runtime.sh
```

which is just the honest version of:

```bash
agentcore remove all -y     # mark everything for removal
agentcore deploy -y         # apply it — tears the resources down
```

Run it when you're done poking. Future posts each get their own teardown, because the alternative is a surprise line item and a sad Slack message to your finance team.

## Next: It Still Has the Memory of a Goldfish

The agent runs in the cloud now, behind a real endpoint, with sessions that don't trample each other. It's also still storing notes in a Python list that evaporates the instant the instance recycles — so it remembers your note right up until it doesn't.

Next post we give it **AgentCore Memory**: short-term memory so a conversation survives more than one breath, then long-term memory so it remembers *you* across sessions. The notes will finally outlive the process.
