# Architecture

## Two paths, one foundation

The system has two entry points that share everything underneath.

              ┌─────────────────────────────┐
AI agent ─────────▶│ MCP gateway :8000 │
│ authenticate │
│ evaluate policy │──▶ MCP server
│ bridge HTTP to stdio │ (child process)
└─────────────────────────────┘ │
▼
┌─────────────────────────────┐ SQLite on disk
application ──────▶│ LLM gateway :8001 │ ┌──────────────┐
│ authenticate │───▶│ customers │
│ check tenant budget │ │ refunds │
│ route to a live provider │ │ tenants │
│ redact the response │ │ token_usage │
└─────────────────────────────┘ │ audit_log │
│ └──────────────┘
▼
OpenAI ──failover──▶ Groq


## The MCP request path

1. The body is parsed. Malformed JSON is `-32700`; a batch array is refused,
   since the current MCP revision removed batching and partially authorising a
   batch would be worse than refusing it.
2. The bearer token is verified with a pinned algorithm, and a role of `admin`
   or `viewer` is read from it. Every authentication failure returns one
   identical sentence; the specific cause goes to the audit log.
3. The policy evaluates the method. `tools/list` and the handshake methods are
   transparent. For `tools/call`, the tool name is normalised and checked
   against the privileged prefix.
4. A refusal returns `-32001` **without contacting the downstream server**. This
   is the point of the component: once a privileged tool has executed, blocking
   it is too late.
5. An allowed request is forwarded across the stdio bridge and the reply is
   returned under the caller's original id.

### The bridge

An HTTP service cannot speak to a stdio server directly, so the gateway runs the
MCP server as a child process and translates between them. Three problems come
with that, and each is solved explicitly in `stdio_bridge.py`.

**One pipe, many callers.** Writes are serialised behind a lock and a single
reader loop dispatches each reply to the caller waiting on its id.

**Ids collide.** Two HTTP clients will both open with id 1. The bridge assigns
its own internal id per request and restores the caller's id on the way out.

**The session initialises once.** MCP servers accept a handshake once, but the
gateway has many clients. The bridge performs the handshake itself at startup
and replays the stored result to any client that sends its own `initialize`.

## The LLM request path

1. Authenticate, and read the tenant from the token's `tenant` claim. Identity
   and billing are separate concerns: the token says who you are, the tenant
   says whose budget you spend.
2. Estimate the cost and reserve it against the tenant's sliding window. Usage
   is stored as timestamped rows, not a running total, because a single counter
   cannot slide.
3. Route to the primary. On a 429, a 5xx, or silence past the timeout, cancel
   and fail over to the backup exactly once.
4. Stream the response back, passing every chunk through the redactor before it
   leaves.
5. Settle the reservation against the provider's reported token count, floored
   at the minimum charge.

### Why the reservation exists

The true cost of a request is unknown until it finishes, because the size of the
answer is not known in advance. Checking the budget without holding anything
would let concurrent requests each see room that only one of them can have. So
the estimate is reserved up front and corrected downward when the truth arrives,
or released if the request never happened.

### Where the timeout applies

For a non streaming call the budget covers the whole call.

For a streaming call the budget covers reaching the first chunk only. Once text
has reached the user, failing over would splice two different answers together,
so a failure after that point is reported inside the stream rather than retried.

## Redaction

The redactor keeps a short buffer. On each chunk it decides how much of the tail
might still be part of a sensitive value, holds that back, and releases and
cleans everything before it. Deciding what to hold happens *before* matching,
because matching text that has not finished arriving fires a pattern early: an
address ending `.co` matches the email pattern before its final `m` arrives.

The holdback is capped, so a stream containing no break characters cannot buffer
without limit. In ordinary prose almost every chunk ends at a space or a full
stop, so nearly all text is released immediately and the measured overhead is
under a millisecond.

Credit card candidates are confirmed with a Luhn check before being redacted, so
a long order or tracking number survives intact.

## The audit log

One table records every decision either gateway makes: blocked tool calls with
the role and tool, redaction counts by type, failovers with the reason, rate
limit refusals with the usage figures, and sanitised internal errors under the
error id shown to the caller.

Redacted values are never stored. Recording what was just redacted would defeat
the point of redacting it.

## Failure behaviour

| Failure | Response |
|---------|----------|
| Bad or missing token | 401, one flat message, reason in the audit log |
| Privileged tool, insufficient role | JSON-RPC `-32001`, downstream never contacted |
| Malformed tool arguments | JSON-RPC `-32602` |
| Valid request, impossible outcome | Tool result with `isError`, so the model can recover |
| Tenant over budget | 429 with `Retry-After` |
| Primary 429, 5xx or timeout | Silent failover to the backup |
| Both providers down | 503, one flat message, both causes logged |
| Unexpected internal error | Sanitised at the boundary, error id returned |
| Rate limiter store unavailable | Request allowed through, failure logged |

That last row is a deliberate choice. A metering component must not take down
the service it meters. The cost is that limits can be exceeded during a database
outage, which is the lesser harm.
