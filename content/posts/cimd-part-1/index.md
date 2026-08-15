---
title: "Your Auth Server Now Fetches URLs Strangers Pick"
date: 2026-08-17
draft: true
slug: "cimd-part-1"
tags: ["oauth", "security", "mcp", "aws", "cdk"]
series: ["The Client ID Is a URL"]
description: "MCP deprecated Dynamic Client Registration in favor of a spec where the client_id is a URL the authorization server fetches at request time. Here's what that invites, and how to defend it."
cover:
    image: "cimd.webp"
    alt: "A traveler hands a worn document marked CIMD to an armored checkpoint officer while guards look on, in a neon-lit dystopian city"
    relative: true
    hidden: false
---

Every OAuth client you have ever built began with a small act of bureaucracy. A human opened a developer console, clicked "Register New Application," and received a `client_id`. That string is now a row in a database you don't own, on a server you'll never see, maintained by people you'll never meet. It is proof of identity for exactly one reason: somebody wrote your name down in advance. This act is known as client pre-registration.

The naive answer to the pre-registration bottleneck: allow clients to register themselves with the AS in a process called Dynamic Client Registration (DCR). It was well intentioned and, as it turns out, utterly broken. That's changed.

[MCP's authorization spec](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration) has now demoted the client pre-registration ritual, killed DCR, and elevated Client ID Metadata Documents. As of the 2026-07-28 revision, in the section where registration mechanisms are ranked against each other:

> "Dynamic Client Registration is deprecated. New implementations should use Client ID Metadata Documents instead."

DCR is still there. It still works. It's just been demoted.

## Why DCR Walked the Plank

Let's be precise: **RFC 7591 is not dead.** The IETF hasn't touched it, nobody issued a correction, and if you're wiring together two parties with an actual ongoing relationship, DCR remains a perfectly reasonable thing to reach for. This deprecation is MCP's house rule, in MCP's house.

The reason it's a house rule is documented in [SEP-991](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/991), the accepted proposal that changed it:

> "DCR requires servers to manage unbounded databases, handle expiration, and trust self-asserted metadata."

Sit with "unbounded" for a second. MCP's ecosystem has vastly more servers than clients — the way there are more web pages than browsers — and most client-server introductions are one-night stands. Under DCR, every one of those leaves behind a permanent registration record. Nobody comes back to clean it up, because nobody has ever, in the history of computing, come back to clean it up.

So authorization servers started expiring old registrations, which is where it gets genuinely funny in the way that only auth failures are funny. [The OAuth working group's own slides](https://datatracker.ietf.org/meeting/121/materials/slides-121-oauth-sessc-client-id-metadata-document-00.pdf) trace the death spiral: the AS garbage-collects a stale client, the client has no standard way to discover it's been garbage-collected, RFC 6749 forbids the AS from redirecting the user anywhere helpful on an invalid client error, and your user lands face-first on `invalid_client` with no path forward and no idea what they did wrong. The industry's collective workaround was to re-register on *every single login*, which solves the dead-end by turning the unbounded database into a *rapidly* unbounded database. We fixed the leak by opening the tap.

CIMD's answer is to stop storing anything at all. Identity becomes a fact the client publishes about itself, and the server goes and checks...every time.

## "Wait, what?"

A CIMD client never registers. It publishes a small JSON file at an HTTPS URL it controls, and then uses *that URL, verbatim*, as its `client_id` forever after.

Here's the document my demo's CLI client publishes, lifted from the CDK construct that serves it:

```python
document = {
    "client_id": self.metadata_url,   # must equal the URL it's served from
    "client_name": "CIMD Demo CLI Client",
    "client_uri": f"https://{self.domain_name}/",
    "redirect_uris": [f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code"],
    "response_types": ["code"],
}
```

That's the entire identity. No compute, no API, no signup, no email confirmation, no "we've received your request and will respond within 3 business days." `ClientMetadataConstruct` is deliberately the dumbest thing in my stack — a private S3 bucket, a CloudFront distribution, one static file — and its own docstring says it better than I can: *"a fact about the client, published as a plain file the client controls, not a service the client runs."*

"Registering a client" is now "hosting a JSON file." Which is a bit like replacing the passport office with a personal website. Anyone can put anything there, and everyone instinctively feels why that might be a problem.

## The Trust Inversion

The old model: the AS decides who you are *ahead of time*. It wrote your `client_id` down at some point in the past, and every request since is just it consulting its own memory of a decision it already made. Comfortable. Auditable. Requires somebody to have been paying attention at some point.

The CIMD model: you *assert* who you are at request time by handing over a URL, and the AS goes and checks your URL, on every authorization request, before it does anything else. OAuth's oldest assumption — that identity is granted in advance and remembered — quietly turned on its head.

In practice, the sequence runs like this. My demo's CLI starts by calling `GET /data` on the resource server with no token at all, purely to get slapped: a `401` carrying `WWW-Authenticate: Bearer resource_metadata="..."`. That header is the resource server pointing at its own [RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728) metadata document, which names the authorization servers it actually trusts. The CLI fetches that, then fetches the AS's own [RFC 8414](https://datatracker.ietf.org/doc/html/rfc8414) metadata, which advertises `client_id_metadata_document_supported: true` — the AS announcing it's willing to play this game.

Then the interesting part. The CLI makes its authorization request using its CIMD URL as `client_id`, and the AS — before minting a session, before rendering consent, before touching a database — stops and goes to fetch that URL. A stranger just told your authorization server to make an outbound HTTP request, and it did.

Nobody pre-provisioned anything. It worked. That should bother you, at least a little, and the rest of this post is about the specific ways it *should* bother you.

## Yes, About That Headline

Say it slowly, because it's the entire security story of the spec: **an unauthenticated party gets to make your authorization server issue an outbound HTTP request to an address of their choosing.**

If you've spent any time near server security, you already hear the alarm bells. That is the textbook shape of [server-side request forgery](https://owasp.org/www-community/attacks/Server_Side_Request_Forgery). Point that `client_id` at `http://169.254.169.254/`, or at some internal admin endpoint your AS happens to have a route to, and you are no longer authenticating a client. You are unwittingly helping bad actors probe your infrastructure.

The spec knows. Section 8 is essentially a list of ways your AS can avoid becoming a confused deputy with network access. My demo implements all of them, each one mapped to the attack it stops.

**The SSRF guard.** Resolve the hostname, inspect where it actually lands, refuse anything in special-use space — *before* making the request:

```python
def _guard_against_ssrf(url: str) -> None:
    hostname = urllib.parse.urlparse(url).hostname
    resolved_addrs = {info[4][0] for info in socket.getaddrinfo(hostname, None)}

    for addr in resolved_addrs:
        ip = ipaddress.ip_address(addr)
        if (
            ip.is_loopback or ip.is_link_local or ip.is_private
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise CimdValidationError(
                f"refusing to fetch CIMD document: {hostname} resolves to "
                f"special-use address {addr} (SSRF guard, spec Section 8.6)"
            )
```

Note that this resolves DNS and judges the *result*, rather than squinting at the hostname and deciding it looks trustworthy. A name like `totally-normal-client.example.com` can resolve straight into your private address space if the attacker controls the record, and it will do so while looking impeccable in your logs.

**No redirect-following.** The spec is blunt: *"The authorization server MUST NOT automatically follow HTTP redirects when fetching the Client ID Metadata Document."* Python's `urllib` chases 3xx by default, so refusing has to be deliberate:

```python
class _NoRedirectHandler(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):
        return response  # every response is final; caller checks status itself
    https_response = http_response
```

Skip this and your SSRF guard becomes decorative. You can inspect the front door as rigorously as you like — a `302` just walks the attacker around to the side entrance while you're still admiring your own doormat.

**A hard size cap.** 5 KB, per the spec's recommendation. Past that it isn't a metadata document, it's a method for keeping your Auth Server reading someone else's firehose one `read()` at a time, forever.

**Exact `client_id` match.** The fetched document has to declare a `client_id` equal to the exact URL it came from. This is what stops someone from copying *your* metadata to *their* URL and pointing the AS at it. The document doesn't merely claim an identity; it has to vouch for the specific address where it lives.

**No shared secrets, ever.** This one is elegant. CIMD provides no channel through which an AS and a never-registered client could have agreed on a shared secret — there was no registration call, that's the whole premise. So a document declaring `client_secret_post`, `client_secret_basic`, `client_secret_jwt`, or shipping an actual `client_secret` is either broken or probing to see whether your validation logic is awake:

```python
def _validate_no_shared_secrets(document: dict) -> None:
    auth_method = document.get("token_endpoint_auth_method")
    if auth_method in FORBIDDEN_AUTH_METHODS:
        raise CimdValidationError(...)
    present_forbidden = FORBIDDEN_FIELDS & document.keys()
    if present_forbidden:
        raise CimdValidationError(...)
```

**Exact `redirect_uri` match.** The `redirect_uri` in the request must appear verbatim in the document's `redirect_uris`. [RFC 9700](https://datatracker.ietf.org/doc/html/rfc9700) is current best practice here and it means *exact* — not prefix, not same-origin, not "starts with the right thing and probably fine." An allowlist you're willing to pattern-match against is a suggestion box.

All of these run before anything else happens. `fetch_and_validate_cimd_document()` is a gate, and it's all-or-nothing: one violation raises, and the authorization request dies where it stands.

## Watching It Fail on Purpose

Guards nobody has ever seen trigger are folklore, so the demo ships two scenarios that make them fire:

```bash
python3 cli/demo.py --demo-redirect-mismatch
python3 cli/demo.py --demo-ssrf
```

The first sends a `redirect_uri` that isn't in the document's list. The AS rejects it before a session exists, before a login form renders, before a human is ever asked to approve anything. The second aims `client_id` at a URL resolving to loopback; the SSRF guard kills it before a single byte is read, because there was never a document there — that was the point.

Both fail identically: an exception, an OAuth error, nothing minted, nothing stored, nobody consulted. Which is the correct behavior for a validation gate. It isn't clever and it doesn't negotiate. HAL didn't argue about the pod bay doors. It simply didn't open them.

"I'm sorry Dave, I'm afraid I can't do that."

## What's Actually Running

Four constructs in one CDK stack, no cross-stack references, so it tears down as cleanly as it goes up — which matters when the thing exists to be deployed, demoed, and destroyed on repeat.

`ClientMetadataConstruct` is the star: S3 and CloudFront serving one static file, and the reason the registration step no longer exists. `AuthorizationServerConstruct` is a Lambda doing the real work — the CIMD validation above, plus ordinary OAuth mechanics like PKCE verification, JWT signing, and a consent page. `ResourceServerConstruct` wants a valid, correctly-audienced bearer token and is magnificently indifferent to the fact that the client presenting it was never registered anywhere; it is the least neurotic component in the system. And `DownstreamServiceConstruct` sits there this entire post doing absolutely nothing.

## What's Next

Every token this AS issues can carry a `may_act` claim, stamped on when the resource owner ticks a delegation box during consent. It's standing permission for the resource server to later trade that token for a narrower, re-audienced one and call a downstream service *as the user*. That's [RFC 8693 token exchange](https://datatracker.ietf.org/doc/html/rfc8693), and it's a different question than CIMD asks: not "who is this client," but "on whose authority is this request still operating, three hops from the human who agreed to any of it." Part 2 gets into `act`/`may_act`, why this demo implements delegation and deliberately refuses impersonation, and how audience restriction stops a 120-second downstream token from being replayed somewhere it was never meant to go.

---

*This is Part 1 of a short series on the OAuth Client ID Metadata Document draft and the MCP auth changes around it. The [code](https://github.com/kenkitts/client-id-metadata-document) is Python end to end — CDK infrastructure, Lambda handlers, and a stdlib-only CLI — including both negative demos above.*
