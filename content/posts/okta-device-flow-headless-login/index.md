---
title: "A Toaster Can Log In Now, Apparently"
date: 2026-08-07
draft: false
slug: "okta-device-flow-headless-login"
tags: ["ai", "security", "oauth", "python", "claude-code", "okta", "device-flow"]
description: "The PKCE post covered the flow with a browser. This is the other branch: SSH into a headless box with no display, and Okta will happily let you approve the login from your phone instead. Same script, same cache, a completely different handshake — device_code, user_code, and a poll loop with opinions."
cover:
    image: "device-auth-flow.webp"
    alt: "A terminal on a headless box waiting on approval from a phone"
    relative: true
    hidden: false
---

You're SSH'd into a box with no display. No X11, no Wayland, `$DISPLAY` unset, as browser-capable as a toaster. Claude Code, running against your LLM gateway, needs a fresh JWT, and the script backing its `apiKeyHelper` is about to try to `webbrowser.open()` a URL into a void where a browser should be.

It doesn't. It notices the void, shrugs, and prints a six-character code to your terminal instead — with instructions to go type it into a browser on some *other* device. Your phone. Your laptop. Whatever's within arm's reach that isn't the toaster you're SSH'd into. You do that, tap approve, and the headless box — the one that, and I cannot stress this enough, has no browser and never will — gets a real, human-approved OAuth login anyway.

That's the Device Authorization Grant, [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628.html), and it's the other half of the same script I wrote about in [the last post](/posts/claude-code-okta-pkce-helper/). That post was about Authorization Code + PKCE — the flow for when you *do* have a browser sitting right there, and PKCE stands in for a client secret you're not allowed to have. This post is about what the script does when there's no browser to redirect to at all, which turns out to be a genuinely different trick, not just PKCE with the browser part removed.

## Same Script, Different Fork

Quick recap for anyone who didn't read the last one: [`okta-claude-code-token.py`](https://github.com/kenkitts/okta-claude-code-token-helper) is a Claude Code `apiKeyHelper` — a script Claude Code shells out to whenever it needs a credential, which prints a bare access token to stdout and gets treated as authenticated. The caching, the 60-second expiry safety margin, the cross-process file lock so five parallel tool calls don't all try to log in at once — none of that changes here. It's the same cache file, the same lock, the same "why not just use a static key" argument from before. Go read that post if you want the full case against static keys; I'm not re-litigating it.

What's different is a single fork, and it only matters the moment an *interactive* login is actually needed — cache miss, no usable refresh token, first run or a revoked session:

```python
use_device_flow = args.device or OKTA_AUTH_MODE == "device" or (
    OKTA_AUTH_MODE == "auto" and not has_usable_browser()
)
tokens = run_device_flow() if use_device_flow else run_interactive_login()
```

`has_usable_browser()` is a heuristic, not a promise — `webbrowser.open()` has no honest way to say "there's no display here" before you try it, so the script guesses from the environment instead: an SSH session (`SSH_CONNECTION`/`SSH_TTY`) with no `DISPLAY` or `WAYLAND_DISPLAY` set means there's no window system for a browser to appear in, so it's headless until proven otherwise. macOS and Windows get a pass — they always have a GUI shell, SSH or not. It's not clever. It's a handful of environment variable checks pretending to be situational awareness, and it's right...often enough.

Get it wrong and the fallback is graceful: you can force either flow by hand with `OKTA_AUTH_MODE=browser`/`device`, or `--device` for a one-off. More on that at the end. For now, assume the heuristic fires correctly, `has_usable_browser()` returns `False`, and we're in `run_device_flow()`.

## Getting a Code Instead of a Redirect

The browser flow's whole opening move was standing up a throwaway HTTP server on `localhost:5309` to catch a redirect. The device flow doesn't bother, because there's nothing to redirect *to* — no local browser means no local endpoint for Okta to send anyone back to. Instead, the script just asks Okta for a code:

```python
def request_device_code() -> dict:
    return http_post_form(
        f"{OKTA_ISSUER}/v1/device/authorize",
        {
            "client_id": OKTA_CLIENT_ID,
            "scope": OKTA_SCOPES,
        },
    )
```

One POST, no PKCE challenge, no redirect URI, no `state` parameter. Okta hands back something like this:

```json
{
  "device_code": "4ebdb4de-1f8b-4497-be01-ddfaf83c4e9c",
  "user_code": "MHXTFRPK",
  "verification_uri": "https://your-org.okta.com/activate",
  "verification_uri_complete": "https://your-org.okta.com/activate?user_code=MHXTFRPK",
  "expires_in": 600,
  "interval": 5
}
```

Two codes, and they are not the same thing, which trips people up on first read:

- **`user_code`** is the short one — eight characters here, deliberately typeable by a human staring at a terminal over SSH. This is what *you* enter.
- **`device_code`** is the long, high-entropy one, and it never touches your eyeballs. The script holds onto it and uses it to ask Okta, repeatedly, "has anyone approved this yet?"

The lengths are asymmetric on purpose, and RFC 8628 is explicit about why: the `user_code` has to survive a human typing it into a phone keyboard, so it's short — which means it has to burn through relatively little entropy, which the spec addresses head-on by requiring rate-limiting and a short expiry window instead of just making the code longer and calling it a day. `device_code` has no such usability constraint, so it gets to be an ugly, effectively unguessable UUID. Different codes, different threat models, deliberately.

The script prints the verification step and moves on:

```python
log("No local browser available - using device authorization instead.")
log("On any device with a browser, go to:")
log(f"  {verification_uri}")
if not device_response.get("verification_uri_complete"):
    log(f"and enter this code when prompted: {user_code}")
```

You take that URL — or, if Okta returned `verification_uri_complete`, a version with the code already baked into the query string, letting you skip typing entirely — over to a browser on literally any device with a screen, log in with your actual Okta credentials, MFA and all, and approve. The headless box never sees any of that. It's just sitting there, waiting for something to happen.

## The Part That's Actually New: Polling With Opinions

Here's where device flow stops being "PKCE minus the browser" and becomes its own thing. The browser flow *waited* — a thread blocked on an `Event`, listening for exactly one HTTP request to arrive at the loopback server, and then it was done. The device flow doesn't get a callback. Nobody's going to phone home. It has to go ask:

```python
def poll_device_token(device_code: str, interval: int, timeout: int) -> dict:
    deadline = time.time() + timeout
    while True:
        try:
            return http_post_form(
                f"{OKTA_ISSUER}/v1/token",
                {
                    "client_id": OKTA_CLIENT_ID,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
            )
        except RuntimeError as e:
            message = str(e)
            if "authorization_pending" in message:
                pass  # user hasn't finished approving yet - keep polling
            elif "slow_down" in message:
                interval += 5
            else:
                raise RuntimeError(f"device authorization failed: {message}") from e

        if time.time() >= deadline:
            raise RuntimeError(
                f"timed out after {timeout}s waiting for device authorization approval"
            )
        time.sleep(interval)
```

Same endpoint as a refresh token exchange, different grant type, and — this is the part I actually like about the spec — four distinct outcomes, each with its own manners:

**`authorization_pending`** is the polite "not yet." You haven't finished typing the code into your phone. The script shrugs, sleeps for `interval` seconds, asks again. This is what almost every poll returns, because approving an OAuth login on your phone takes longer than 5 seconds, and that's fine — the whole design assumes the client is patient and the human is slow.

**`slow_down`** is Okta telling you to stop being an annoying little shit. RFC 8628 §3.5 doesn't leave this to interpretation: on `slow_down`, the client "MUST" increase the interval by 5 seconds, for this request and every one after it. Not "should," not "consider" — must. It's a rate limiter with a name, baked directly into the OAuth error vocabulary instead of being left to an `X-RateLimit` header nobody reads. The script does exactly what it's told: `interval += 5`, forever, no cap. Ask Okta the same question too eagerly and it makes you wait a little longer every single time until you learn some patience. This is what a well-behaved backoff looks like when the *protocol itself* enforces it instead of leaving you to invent one, poorly, while sleep deprived.

**`access_denied`** is a person looking at "approve login for a script running on some server you probably forgot about" and clicking no. Fair. The script doesn't retry — it can't, the `device_code` this session was built around is done — it just raises and dies. Somewhere, a security-conscious human just made a correct decision, and the script respects it instantly instead of asking again in case they changed their mind.

**`expired_token`** is what happens if nobody does anything for 600 seconds — Okta's default window, confirmed straight from [their own docs](https://developer.okta.com/docs/guides/device-authorization-grant/main/): the `user_code` and `device_code` are both dead, full stop, start over. Ten minutes sounds generous until you remember the actual sequence of events required: notice the terminal message, unlock your phone, open a browser, navigate to a URL, type an eight-character code without a typo, authenticate, clear MFA, click approve. Ten minutes is not generous. Ten minutes is exactly enough time if nothing goes wrong.

Everything that isn't one of those four falls through to `raise`, which is the correct move — the spec is explicit that any error code other than `authorization_pending` or `slow_down` means stop polling immediately, not "keep trying and hope." A script that doesn't know when to give up on a dead code isn't resilient, it's just going to poll a dead endpoint until its own timeout kills it anyway, having learned nothing and wasted a few hundred HTTP requests finding out.

## "Wait, Where's the PKCE?"

If you read the last post, you might be asking yourself how secure this all is: no `code_verifier`, no `code_challenge`, no proof-of-possession dance anywhere in `poll_device_token`. Did the script just quietly drop its security posture the moment a browser wasn't handy?

No — PKCE exists to solve one specific problem: an authorization `code` traveling through a browser redirect can be intercepted by something else on the same machine (a second app registered for the same redirect scheme, a local proxy) and replayed by an attacker who never touched your credentials. Device flow has no redirect. There's no URL bouncing the `code` through anything interceptable — the `device_code` goes from Okta to the script over one direct HTTPS POST the script made itself. There's no redirect to intercept, so there's nothing for PKCE to protect against. It's not a downgrade. It's a different attack surface that happens to not include the one PKCE was invented for.

## The Auto-Detection Is Just Vibes, and That's Fine

One paragraph, because it doesn't deserve more: `has_usable_browser()` is checking `SSH_CONNECTION`, `SSH_TTY`, `DISPLAY`, and `WAYLAND_DISPLAY` — four environment variables standing in for "does this process have any realistic path to a browser." It's a heuristic borrowed from a decade of "how do I tell if this is a headless session" folklore, not an Okta API call that actually answers the question. It'll be wrong occasionally — a forwarded X11 session that's really slow, a container with `DISPLAY` set to something that doesn't work. When it's wrong, `OKTA_AUTH_MODE=device` (or the one-shot `--device` flag) overrides it entirely, and that override exists for a second reason too: sometimes you've got a browser sitting right there and would simply rather approve from your phone anyway, because typing a URL and a code into a terminal you're already looking at beats context-switching your desktop browser.

## What This Actually Buys You

Two things, and they're not the same thing.

The first is the neat trick, and it's genuinely neat: a machine with no display, no browser, and no plausible way to ever have one can still complete a real, human-approved, MFA-cleared interactive login. Not a workaround, not a lesser version of login — the same Okta authentication your desktop session gets, just relayed through a phone (or any browser-capable device) instead of a loopback listener. RFC 8628 exists because someone had to solve this for smart TVs and printers, and it turns out "SSH session with no X server" has exactly the same shape as "printer with no keyboard." Different device, same fundamental problem, same fix.

The second is the thing the last post was actually about, and it comes along for free: whichever flow got you here, you land in the exact same cache, with the exact same short lease. The device flow doesn't grant some lesser, more-suspicious token — it's the identical access/refresh token pair, the same `chmod 600` file, the same silent refresh loop, the same blast radius if someone steals it later. There's still no static key sitting in `~/.claude/settings.json` waiting to outlive your attention span. There's still a clock on it. The headless box just got there by a different door.

The [full script](https://github.com/kenkitts/okta-claude-code-token-helper) is on GitHub, same repo as last time — clone it, SSH into something with no display, and watch a machine that has never seen a browser in its life log in anyway.

---

*This is a companion piece to [Part 1](/posts/claude-code-okta-pkce-helper/), which covers the Authorization Code + PKCE branch of the same script — the one that runs when a local browser actually is available. Same cache, same lock, same "why not a static key" argument; this post is just the other fork.*
