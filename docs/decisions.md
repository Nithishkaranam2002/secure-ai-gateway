# Design decisions

Judgement calls made while building, and what each one costs.

## Task 1: MCP server

### The brief says CUST-XXXXX without defining X

It does not state whether the five characters are digits or letters. We require
five digits. Digits are the narrower reading, and a validation layer that is
too strict fails loudly and visibly, whereas one that is too loose fails
silently and lets bad data through. The pattern lives in one constant in
`src/mcp_server/schemas.py`, so widening it later is a one line change.

### Low level server rather than the decorator API

The SDK offers a high level API where a decorated Python function becomes a
tool. It is less code and it is the right default for most servers, but it
cannot satisfy this brief. In the high level API an exception raised inside a
tool is caught and returned as a tool result with `isError` set, so a schema
failure would never reach the wire as JSON-RPC `-32602`. In the low level API an
exception from a handler is always a protocol error. We accept the extra code to
get the error semantics the brief asks for.

### Protocol errors and business errors are deliberately different

Two failures that look similar are treated differently on purpose.

A malformed message, such as `customer_id: "not-a-customer"` or a negative
amount, is a protocol failure. It raises, and becomes JSON-RPC `-32602`. The
message did not match the advertised schema, so it was never a valid request.

A well formed message that cannot be completed, such as a lookup for a customer
who does not exist, returns a normal result with `isError` set. The caller is a
language model, and a readable explanation in a result is something it can act
on and retry, whereas a transport level error is not.

Getting this backwards is the most common mistake in MCP servers. Returning
everything as `isError` means malformed input looks like success to the
protocol. Raising on everything means the model cannot recover from ordinary
situations like a typo in an id.

### Money does not get type coercion

`amount` is typed `StrictFloat | StrictInt`, so `50` and `49.99` are accepted
and the string `"50"` is rejected. Pydantic would happily convert the string,
and for most fields that is convenient. For a field that moves money, a caller
that cannot send a number is a caller we do not trust to have meant the amount
it sent.

Amounts are also limited to two decimal places, because a refund of `49.999` is
a rounding argument waiting to happen.

### Unknown fields are rejected

All input models set `extra="forbid"`. If a caller sends `ammount` instead of
`amount`, a permissive model would ignore the typo and reject the request for a
missing field, or worse, apply a default. Refusing unknown fields turns a silent
misunderstanding into a clear error naming the offending field.

`reason` is also checked after stripping whitespace, because a ten character
minimum that accepts ten spaces is not a check.

### A refund ceiling that the brief did not ask for

`trigger_refund` refuses amounts above 10,000 with `refund_ceiling_exceeded`.
The brief does not require it. It is here because the tool is called by a
language model that can be talked into things by the text it is reading, and a
well formed request for a very large refund is exactly what a successful prompt
injection produces. The ceiling is a business error rather than a validation
error, because the request itself is legitimate in shape.

### Logging to stderr is enforced in one place

`src/core/logging_setup.py` clears the root handlers and installs a single
stderr handler. Nothing in the project configures logging itself. This is the
whole defence for the stdout requirement, and it is one file rather than a
convention everyone has to remember.

### The stdout requirement is proved, not asserted

`tests/test_stdio_isolation.py` runs the server as a real child process, drives
it over an actual pipe, captures every byte of stdout, and asserts that each
line parses as JSON and carries `jsonrpc: "2.0"`. The child is forced to
`LOG_LEVEL=INFO` so it produces as much log output as possible during the test.
A stray `print` anywhere in the process, including inside a dependency, fails
this test.

## Task 3 and 4: providers

### httpx rather than the provider SDKs

`src/llm_gateway/providers.py` is the only module that makes outbound calls,
and it uses httpx directly. The SDKs were rejected for three specific reasons.
They retry a 429 internally, which hides the signal the router needs in order to
fail over, so the failover path would silently never run. They apply timeouts
per attempt rather than per call, so a 3000 ms budget can take far longer than
3000 ms. And they parse the SSE stream into their own objects, which this
gateway would then have to rebuild in order to forward it on intact.

Because Groq accepts the OpenAI request shape, one `Provider` class serves both
with a different base URL, key and model.

### The timeout bounds silence, not total duration

httpx is given a 3000 ms connect, read, write and pool timeout, and no overall
timeout. This is deliberate. A healthy streaming response legitimately stays
open for far longer than the budget, so an overall cap would truncate long
answers mid sentence on every request. What the brief is protecting against is a
provider that has stopped responding, which is a gap between chunks, which is
the read timeout.

### The backup provider does not behave like the primary

Two things surfaced only because both providers are real.

Groq retires model names often. `llama-3.1-8b-instant` was gone by the time this
was built. The README explains how to list the models a given key can actually
serve rather than hardcoding an assumption.

More interestingly, `openai/gpt-oss-20b` is a reasoning model. It spends output
tokens on internal reasoning before writing anything, so a request with a small
`max_tokens` returns HTTP 200, reports `completion_tokens` spent, and leaves
`message.content` empty. A failover to that model would have produced a
successful looking response containing no text.

`qwen/qwen3.6-27b` failed the same way for a different reason. Groq accepts a
`reasoning_format` parameter, and both `hidden` and `parsed` correctly kept the
thinking out of `content`, but the model still exhausted a 200 token budget
before writing an answer. Left at its default it returned its raw reasoning
inside `content` instead.

The backup is therefore `allam-2-7b`, a plain chat model with no reasoning step.
The wider point is that a gateway cannot assume its providers are
interchangeable just because they share a wire format. Three backup candidates
were tried. One had been retired, one returned HTTP 200 with an empty body, and
one returned its internal reasoning as the answer. All three accepted an
identical request and reported success.
