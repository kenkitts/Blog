---
title: "Building an Agent on Bedrock AgentCore, Part 4: The Notes Were Always Ephemeral"
date: 2026-06-30
draft: false
slug: bedrock-agentcore-part-4
tags:
  - ai
  - agents
  - bedrock
  - agentcore
  - python
  - strands
  - mcp
series:
  - Building an Agent on Bedrock AgentCore
description: For three posts the agent has saved your notes to a Python list that dies with the process. This post gives them a real home — DynamoDB behind a Lambda, served to the agent as MCP tools through an AgentCore Gateway. The agent can't tell the remote tools from the local ones. That's the trick, and the knife.
cover:
  image: agentcore_gateway.webp
  alt: A notes agent reaching through a managed gateway to a real filing cabinet bolted to a database
  relative: true
  hidden: false
---

[Last time](/posts/bedrock-agentcore-part-3/) the agent learned to remember you: durable conversations, your preferences trailing you across sessions. It knows you like terse summaries. It knows the Q3 doc is in the shared drive.

It does not have the Q3 doc. It has never had your notes. For three posts `add_note` has been dropping them into a Python list that lives in RAM and dies with the process, like a goldfish keeping a diary. The agent remembers the *conversation* about your notes. The notes themselves were a rumor it was too polite to correct.

Last post I called it a filing cabinet made of smoke. Today we build the cabinet, and move the tools out of the agent's spare room into a building with its own locks, plumbing, and bill.

**The tools stop being Python functions in the agent's process and become MCP tools behind an AgentCore Gateway, backed by a Lambda and a DynamoDB table. The agent discovers them over the network and can't tell them from the local ones.** That's the trick. It's also the knife.

## Get the Code

This post is the `post-04-gateway` tag. The diff is the lesson:

```bash
git clone https://github.com/kenkitts/bedrock-agentcore.git
cd bedrock-agentcore
git checkout post-04-gateway
git diff post-03-memory..post-04-gateway     # the whole post, in one diff
```

## The Tools Were Roommates With the Agent

Since Part 1 the tool and the agent have shared a process, a kitchen, and a toothbrush:

```python
# notes_agent/tools.py (Posts 1-3)
_STORE = _NoteStore()   # a Python list. In RAM. Gone when the process exits.

@tool
def add_note(text: str) -> str:
    return f"Saved note #{_STORE.add(text)['id']}."
```

Two problems people love to mash into one. The data isn't durable: Part 3 made the *conversation* survive a cold start and did precisely nothing for the notes, because the notes were never the conversation. And the tool isn't independent — it ships inside the agent, runs as the agent, inherits the agent's permissions. You can't update it without redeploying the agent, can't lend it to a second agent, and can't put a wall between "the model felt like calling this" and "this ran against your database."

Real tools live somewhere else, behind an interface, holding their own keys.

## MCP, and the Gateway That Serves It

The vendor-neutral piece is **MCP**, the Model Context Protocol. If you read [the from-scratch series](/posts/agent-harness-part-4/) you've met it: a tool lives on a server, advertises "here's what I do and what I take," and a client calls it over JSON-RPC. That's the whole opera. It's USB for tool calls, boring in exactly the way good infrastructure is boring. Nobody keynotes USB.

The payoff earns a prickle on the back of the neck: **the model can't tell a local tool from a remote one.** A function in the same process, a Lambda one network hop and 200ms away — a tool is a tool. You bolt on real, separately-deployed, separately-permissioned tools and the agent loop doesn't move a line.

What you don't want is to run the MCP server yourself: host it, auth it, scale it, patch it, get paged about it at an hour with a single digit in it. **AgentCore Gateway** is that server, operated by someone who isn't you. You point it at *targets* — a Lambda, an OpenAPI spec, an MCP server you already have — and it aggregates them into one MCP endpoint. We use a Lambda target: hand it a function ARN and a **tool schema** (a little JSON menu the model reads), and the gateway does the MCP-to-Lambda translation, credential injection, and inbound auth. You write logic; it writes plumbing.

## Prerequisites and the Bill

Everything from Posts 2-3 (the CLI, a deployed Runtime, model access), plus rights to make a DynamoDB table, a Lambda, and a role. Cost is still pennies, but it's no longer *zero* and no longer *one thing*: on-demand DynamoDB, a near-free Lambda, the consumption-priced Gateway, and the Runtime and Memory still humming from before. The Clean Up section is not decoration.

## Build It

Four moving parts: a real backend, the tool schema, the wiring that discovers tools over MCP, and — at long last — tests.

### The backend: logic the model never touches

The notes logic moves into a Lambda, split into a pure core (no AWS imports) and a thin DynamoDB adapter:

```python
# scripts/notes_backend/notes_core.py (trimmed) — pure, no boto3
def handle_tool(tool_name, args, store):
    if tool_name == "add_note":
        text = str(args.get("text", "")).strip()
        if not text:
            return "Cannot save an empty note."
        note = build_note(text)
        store.add(note)
        return render_saved(note)
    if tool_name == "list_notes":
        return render_list(store.all())
    if tool_name == "search_notes":
        q = str(args.get("query", ""))
        return render_search(search_notes(store.all(), q), q)
    raise ValueError(f"Unknown tool: {tool_name!r}")
```

The split isn't fussiness; it's what lets the tests run without standing up AWS. `handle_tool` takes a `store` — DynamoDB in production, a list in a test.

The adapter is the part that earns the bill:

```python
# scripts/notes_backend/handler.py (trimmed)
def lambda_handler(event, context):
    raw = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = strip_tool_prefix(raw)          # NotesTarget___add_note -> add_note
    table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return handle_tool(tool_name, event, DynamoNoteStore(table))
```

Two things in those four lines will eat an afternoon if nobody warns you. The gateway hands you the tool name wearing a disguise — `NotesTarget___add_note`, the target name bolted on with three underscores — and you peel it off before you know what you were asked to do. The arguments arrive as a flat dict in `event` (`{"text": "..."}`), shaped exactly like the `inputSchema` you declare next. And notice what's *missing*: the model never sees this function's permissions. The Lambda touches DynamoDB with its own role; the agent has no database access and never asks for any. That wall is the whole reason we evicted the roommate.

### The tool schema: a menu the model reads

```json
{
  "name": "add_note",
  "description": "Save a note for the user. Call this whenever the user shares something they want to remember.",
  "inputSchema": {
    "type": "object",
    "properties": { "text": { "type": "string", "description": "The note content, in the user's words." } },
    "required": ["text"]
  }
}
```

That `description` is not documentation. It's prompt. The model reads it to decide when to reach for the tool, so write it for the model: "call this whenever the user shares something they want to remember" pulls more weight than the function name ever will.

### The wiring: tools fetched, not imported

The tools aren't imported anymore; they're pulled off the gateway at runtime.

```python
# notes_agent/gateway.py (trimmed)
def build_gateway_client():
    if not GATEWAY_URL:
        return None                          # no gateway -> fall back to local tools
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp.mcp_client import MCPClient
    return MCPClient(lambda: streamablehttp_client(_mcp_endpoint(GATEWAY_URL)))
```

`build_agent` grows one optional argument, so the local Posts 1-3 agent still exists when no gateway is set: `tools=tools if tools is not None else NOTE_TOOLS`.

The change that matters is in `app.py`, and it's about a *lifecycle*. An MCP client holds a live connection, and the tools only work while it's open, so you can't grab them and wander off. The agent has to run *inside* the connection:

```python
@app.entrypoint
def invoke(payload, context):
    ...
    mcp_client = build_gateway_client()
    if mcp_client is None:                    # no gateway: local tools, like Part 1
        return {"result": str(build_agent(session_manager=session_manager)(prompt))}
    with mcp_client:                          # connection stays open for the whole turn
        tools = list_gateway_tools(mcp_client)
        agent = build_agent(session_manager=session_manager, tools=tools)
        return {"result": str(agent(prompt))}
```

Memory and Gateway compose without drama here: a per-session memory manager wrapping an MCP-tool agent, both optional, both quietly degrading to nothing when unconfigured.

### Tests, finally

The plan put tests at this post for a reason — we finally have deterministic, LLM-free logic worth pinning down. The pure core takes a fake store: no AWS, no model, no network.

```python
def test_handle_tool_search_notes_filters():
    store = FakeStore([_note("buy milk", "1"), _note("call dentist", "2")])
    out = notes_core.handle_tool("search_notes", {"query": "milk"}, store)
    assert "buy milk" in out and "dentist" not in out
```

That's the "B-lite" deal: assert on the deterministic tool code, and leave grading the *model's* judgment for the observability post. The model has no business near a unit test, which is exactly where we keep it.

## Run It

Provision the backend once — table, Lambda, least-privilege role:

```bash
pip install -r requirements.txt
python scripts/create_notes_backend.py   # prints NOTES_AGENT_BACKEND_LAMBDA_ARN=<arn>
```

Deploy with the same bring-your-own-code flow as Posts 2-3, then bolt on a gateway and the Lambda target:

```bash
cd .. && agentcore create --project-name notesAgentRuntime --no-agent && cd notesAgentRuntime
agentcore add agent --name notesAgent --type byo \
    --framework Strands --model-provider Bedrock --memory none \
    --code-location ../bedrock-agentcore --entrypoint app.py --language Python

# A gateway with NONE inbound auth (hold that thought), wired to the agent.
agentcore add gateway --name NotesGateway --authorizer-type NONE --runtimes notesAgent

# The Lambda target + the tool schema the model reads.
agentcore add gateway-target --name NotesTarget --type lambda-function-arn \
    --lambda-arn <NOTES_AGENT_BACKEND_LAMBDA_ARN> \
    --tool-schema-file ../bedrock-agentcore/scripts/notes_backend/tools.json \
    --gateway NotesGateway

agentcore deploy -y
```

The gateway doesn't exist until that first deploy, so it's two-phase: deploy, then fish the URL out of `agentcore status --json`. (Plain `agentcore status` cheerfully reports "Deployed" and withholds the URL like it's doing you a favor; the real thing hides under `deployedState → … → mcp.gateways.NotesGateway.gatewayUrl`.) Paste it into `config.py`, `agentcore deploy -y` again so it ships with the code. Same song as the Memory id in Part 3: the CLI can't inject env vars, so the URL hitchhikes with the bundle.

Then the test that would have face-planted in every prior post — say something, then ask from a *fresh* instance:

```bash
agentcore invoke "add a note: the launch retro is Thursday at 2pm"
# -> Saved note: 'the launch retro is Thursday at 2pm'
agentcore invoke "what do I have saved?"
# -> 1. the launch retro is Thursday at 2pm
```

It survived. The note is in DynamoDB, not in some microVM's RAM that's already been wiped and salted. The cabinet is real.

## Under the Hood

**The agent genuinely doesn't know.** `list_gateway_tools` asks the gateway for `tools/list`, gets back `NotesTarget___add_note` and friends, and wraps each as a callable. From the loop's seat they're indistinguishable from Part 1's decorated functions. The model picks `add_note`; Strands fires an MCP `tools/call`; the gateway invokes the Lambda; the Lambda hits DynamoDB; a string comes back. The model sees none of the journey, which is the entire point.

**The scar: finding the URL, then fixing it.** Two papercuts, stacked. First, plain `agentcore status` shows you everything except the one thing you came for — the URL only appears under `--json`. Second, the URL it finally coughs up is the *host*, and the MCP endpoint is that host plus `/mcp`. Point a client at the bare host and it answers, warmly, with an AgentCore envelope that is not MCP, which Strands then tries to parse as JSON-RPC and dies on with eleven pydantic errors under the immortal banner `client initialization failed: unhandled errors in a TaskGroup`. I donated an afternoon to this so you wouldn't have to; the repo now appends `/mcp` for you. If you ever meet that error, suspect your URL before anything else.

**The other scar, this one on purpose: the door has no lock.** Read `--authorizer-type NONE` again. The gateway has no inbound auth. Anyone with the URL can list your tools and call them — read every note, write garbage, run your Bedrock bill up for sport. That is not a thing you ship. I did it anyway, to keep this post about tools instead of auth, and because the fix has earned its own post. But say it out loud: **the notes finally have a real home, and the front door is propped open with a brick.**

## Clean Up

Post 4 stacks a DynamoDB table, a Lambda, and a role onto the Runtime and Memory. One script for the backend:

```bash
# from the repo root: deletes the table, Lambda, and role,
# then prints the Gateway + Runtime teardown steps.
scripts/teardown_04_gateway.sh
```

The Gateway lives in the deploy project, so it leaves with the Runtime (`agentcore remove gateway --name NotesGateway && agentcore deploy -y`, then the Part 2 teardown — the script spells out the order). Run it. An unauthenticated, internet-reachable tool endpoint you forgot you left running is how war stories start.

## Next: A Lock for the Door

The notes are real. They outlive restarts, they live in a database, they're served by a Lambda the agent can't pick out of a lineup. We also left the door propped open, on purpose, and I'm not going to stand here and tell you that's fine.

Next post: **AgentCore Identity**. Inbound auth, so the gateway stops answering strangers — and the change that actually makes this a product: "your notes" and "someone else's notes" becoming two different things instead of one undifferentiated pile under a single demo user. A lock, and a name on the mailbox.
