---
title: "I Gave Claude Code a Static API Key. Then I Took It Back."
date: 2026-08-03
draft: false
slug: "claude-code-okta-pkce-helper"
tags: ["ai", "security", "oauth", "python", "claude-code", "okta", "pkce"]
description: "Claude Code's apiKeyHelper contract is one line: print a token to stdout. The easy way is a static key sitting in plaintext forever. Here's the harder way — Okta, PKCE, and a script that logs in so you don't have to leave a credential lying around."
cover:
    image: "claude-code-okta-pkce-helper.webp"
    alt: "A key being swapped for a short-lived token"
    relative: true
    hidden: false
---

I got Claude Code talking to my self-hosted gateway in about fifteen minutes. Mint a scoped API key, export `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`, done. It worked immediately, which should have been my first warning — security problems that work immediately are the ones that stick around.

The key I minted was budget-capped and scoped to a few models, not the master key to the kingdom. Bounded risk, technically. But it lived in plaintext in `~/.claude/settings.json`, which meant anyone who could read a file on my machine — a compromised dependency, a misconfigured backup, a `cat` fat-fingered into the wrong Slack channel — now had a usable credential for as long as I forgot to revoke it. I noted the risk, decided it was acceptable for now, and moved on with my life, the way you do with a smoke detector that's beeping every ninety seconds and you just haven't gotten around to the battery yet.

This post is me getting around to the battery.

## The Contract Is One Line

Claude Code has a setting called [`apiKeyHelper`](https://docs.claude.com/en/docs/claude-code/settings#apikeyhelper): a command it runs whenever it needs a credential. Whatever that command prints to stdout becomes both the `X-Api-Key` header and the `Authorization: Bearer` header on every request to your configured `ANTHROPIC_BASE_URL`. That's the entire contract. No SDK, no callback, no negotiation — print a token, get treated as authenticated.

The lazy implementation is one line:

```bash
echo "$STATIC_KEY"
```

This is not wrong, exactly. It satisfies the contract. It's also how a "temporary" static key ends up living in a config file for the rest of its natural life, because nothing about the *contract* forces it to expire, rotate, or ever leave.

What the contract *doesn't* forbid is far more interesting: `apiKeyHelper` can be any executable. Nothing says the credential has to be static. Nothing says it can't be minted fresh, cached briefly, and thrown away. Claude Code doesn't know the difference between `echo` and a script that just ran you through a full OAuth dance — it just reads stdout either way.

So instead of a value, I gave it a *process*: [`okta-claude-code-token.py`](https://github.com/kenkitts/okta-claude-code-token-helper), a script that gets a short-lived Okta access token via OAuth 2.0 Authorization Code + PKCE, caches it, refreshes it silently, and only bothers you with a real login when it truly has no other option. Claude Code doesn't know it's talking to Okta. It just thinks its API key helper is unusually chatty about opening browser tabs sometimes.

## Why Not Just Rotate the Static Key?

Because rotation is a process you have to remember to run, and a credential's danger is proportional to how long it's valid times how little attention anyone is paying to it. A key that lives for an hour and renews itself without asking is a fundamentally smaller target than a key that lives until someone remembers to `/key/delete` it. You don't need to trust your future self's discipline if the credential expires on a timer you don't control.

There's also the client secret question, which PKCE exists specifically to answer. The obvious OAuth alternative — Client Credentials grant — needs a client secret, and a client secret stored on a developer's laptop for a CLI tool is the same plaintext-on-disk problem I was trying to get away from, just moved one layer down. Authorization Code + PKCE lets the script act as a **public client**: no secret, anywhere, ever. Instead of "prove you know the secret," the flow proves "you're the same process that started this login" using a value only that process ever held in memory. That's the whole trick, and it's worth unpacking, because it's doing real security work, not just OAuth ceremony.

## PKCE, Concretely

Here's the part that actually replaces the secret. Before opening the browser, the script generates a random verifier and hashes it:

```python
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def make_pkce_pair() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge
```

The `challenge` (the hash) goes to Okta up front, in the `/authorize` URL, in plain sight in the browser. The `verifier` (the thing that was hashed) stays in the script's memory and is *never* sent anywhere until the very last step — the token exchange:

```python
def exchange_code_for_tokens(code: str, code_verifier: str) -> dict:
    return http_post_form(
        f"{OKTA_ISSUER}/v1/token",
        {
            "grant_type": "authorization_code",
            "client_id": OKTA_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
        },
    )
```

Okta hashes the `code_verifier` you send here and checks it matches the `challenge` you sent earlier. If someone intercepted the authorization `code` mid-flight — a malicious app on the same machine registering the same redirect scheme, a nosy proxy, whatever — they still can't redeem it, because they never had the verifier. They only saw its hash. That's the entire value proposition of PKCE in one sentence: **proof of possession of a secret you generated for this login and never disclosed**, standing in for a secret you'd otherwise have had to hardcode and protect forever.

Getting the authorization `code` back to a CLI script (which, unlike a web app, has no URL of its own for Okta to redirect to) is the other half of the puzzle:

```python
def wait_for_redirect(expected_state: str, timeout: int) -> _CallbackResult:
    result = _CallbackResult()
    handler_cls = _make_handler(result, expected_state)
    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), handler_cls)
    server.timeout = 1
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5})
    thread.daemon = True
    thread.start()
    finished = result.event.wait(timeout)
    server.shutdown()
    server.server_close()
    ...
```

The script stands up a throwaway HTTP server on `localhost:5309`, opens your real browser to Okta's login page, and waits. You log in with actual Okta credentials — MFA and all, whatever your org enforces — and Okta redirects your browser back to that loopback listener with the authorization code in the query string. The handler checks the OAuth `state` parameter against the one the script generated (cheap CSRF protection: refuses a redirect that doesn't match a login *this* invocation actually started), grabs the code, and shuts the tiny server down. It existed for one request and then it's gone, which is about as much attack surface as I'm willing to open on my own laptop.

## The Part That Makes It Not Annoying: Caching and Silent Refresh

None of the above happens on most invocations. If it did, Claude Code would pop open a browser tab every time it needed to make a request, and you'd close this blog post and go back to `echo "$STATIC_KEY"` out of pure self-preservation. Security that's annoying enough gets worked around; that's not a cynical aside, it's the actual failure mode this design has to survive.

So the script checks a cache first, before touching the network at all:

```python
access_token = cache.get("access_token")
expires_at = cache.get("expires_at", 0)

if access_token and now < (expires_at - EXPIRY_SAFETY_MARGIN_SECONDS):
    log("Using cached access token (still valid).")
    print(access_token)
    return 0
```

`EXPIRY_SAFETY_MARGIN_SECONDS` is 60 — the token gets treated as expired a full minute before it actually is, so a request that starts right at the edge doesn't race an Okta clock and fail mid-flight. Cheap insurance.

If the cached access token is gone or stale, but a refresh token is sitting in the cache, the script uses it — silently, no browser, no user interaction:

```python
def refresh_access_token(refresh_token: str) -> dict:
    return http_post_form(
        f"{OKTA_ISSUER}/v1/token",
        {
            "grant_type": "refresh_token",
            "client_id": OKTA_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
```

This is the case that actually matters for day-to-day use: your Okta access token is probably good for an hour, but the *refresh* token — requested via the `offline_access` scope — lives much longer. So the real login happens once, and then for however long the refresh token stays valid, "getting a fresh token" is a single silent HTTP call, indistinguishable in experience from a static key, except that nothing long-lived is sitting on disk waiting to be stolen. Only when the refresh token itself is expired or revoked does the script fall back to the full interactive dance.

The cache file itself gets the same paranoid treatment as the tokens inside it:

```python
os.chmod(claude_dir, 0o700)
fd = os.open(TOKEN_CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(data, f)
os.chmod(TOKEN_CACHE_PATH, 0o600)
```

`chmod 600` on the file, `chmod 700` on the directory it lives in. It's still a credential on disk — I haven't achieved zero-trust nirvana here, just replaced a credential with a *much* shorter shelf life. If someone reads this file the instant after it's written, they get roughly an hour of access instead of forever. That's the trade this whole design is making: not "no secret exists," but "the secret that exists is worth far less to whoever steals it."

## One Paragraph on the Part That's Actually Just Plumbing

Claude Code can invoke `apiKeyHelper` more than once at the same time — parallel tool calls, sub-agents, whatever's running concurrently in a session — and without any coordination, each of those invocations would independently decide the cache is stale and each would try to open its own browser tab, with all but one failing to bind the same loopback port. The fix is a boring, correct cross-process file lock (`fcntl.flock` on a dedicated lock file): only one invocation actually talks to Okta or opens a browser at a time, and everyone else blocks, then re-reads the cache the winner just wrote instead of racing it. It's not a security mechanism, just a script behaving like an adult when called twice at once — but it's the difference between "clean login" and "user sees three browser tabs and picks the wrong one to finish."

## The Part I Didn't Build

There's one more thing the contract cares about that's easy to miss: `apiKeyHelper` can be invoked in contexts where popping open a browser is actively wrong — imagine this running unattended, headless, somewhere a login prompt would just hang forever. The script checks for that and refuses to guess:

```python
if helper_context and helper_context not in ("interactive", "setup-test"):
    log(
        f"No valid cached/refreshable token and helper context is "
        f"'{helper_context}' (non-interactive) - refusing to open a "
        "browser. Run this script manually once to establish a session."
    )
    return 1
```

Failing loudly and telling you exactly what to do next (run it manually once) beats hanging forever waiting for a browser interaction nobody's there to complete. A non-zero exit here is Claude Code's cue that the credential helper failed — better that than silently blocking a background context indefinitely.

## What I Actually Bought

Compare the two failure modes honestly. Static key, stolen: usable until someone notices and revokes it — hours, days, whenever someone happens to check. This script's cache, stolen: usable for the remaining life of one access token, an hour at most, and the refresh token is the only thing that extends that window — which means revoking access on the Okta side (deactivate the user, kill the session, rotate the app) kills it fast, without touching a single gateway config or hunting down which of your five exported shell variables still has the old value.

I'm not going to tell you this is bulletproof. The cache file is still bytes on a disk; PKCE stops a stolen *authorization code* from being redeemed by someone else, it doesn't stop a stolen *cache file* from being read by someone with your user's filesystem access. What it buys is a smaller blast radius and a shorter clock — the two things a static key sitting in a JSON file for months has neither of.

The [full script](https://github.com/kenkitts/okta-claude-code-token-helper) is on GitHub — clone it, point `OKTA_ISSUER`/`OKTA_CLIENT_ID` at your own tenant, and see how far "one line in a config file" can be pushed before it turns into a real login flow. Standard library only, no dependencies, because the last thing an auth script needs is its own supply chain to worry about.

---

*I originally built this for [guidance-for-multi-provider-generative-ai-gateway-on-aws](https://github.com/aws-solutions-library-samples/guidance-for-multi-provider-generative-ai-gateway-on-aws), a self-hosted LiteLLM gateway fronting Bedrock and other providers — but the script doesn't know or care what's listening on the other end of `ANTHROPIC_BASE_URL`. Anything that accepts an Okta access token as a bearer credential works.*
