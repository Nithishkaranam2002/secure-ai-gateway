# Secure AI Gateway

A security gateway that sits between AI agents and the systems they talk to.
It controls which tools an agent may call, removes private data from model
responses while they stream, limits how much each customer can spend, and
keeps working when a model provider fails.

## The four parts

1. MCP server (src/mcp_server)
   An MCP server over stdio transport exposing two tools:
   get_customer_record, taking a customer_id in the format CUST-12345
   (five digits), and trigger_refund, taking a customer_id, a positive
   float amount, and a reason of at least 10 characters.
   All input validated with Pydantic. Invalid input returns JSON-RPC
   error -32602. Requests that are well formed but impossible, such as an
   unknown customer, return a normal MCP tool result marked as an error.
   stdout carries JSON-RPC only. All logs go to stderr.

2. MCP gateway (src/mcp_gateway)
   An HTTP JSON-RPC reverse proxy in front of the MCP server. Reads a
   Bearer JWT from the Authorization header and extracts a role of admin
   or viewer. Forwards tools/list unchanged. For tools/call, if
   params.name starts with "admin_" and the role is not admin, it returns
   JSON-RPC error -32001 "Unauthorized Tool Call" without contacting the
   MCP server at all. Missing or invalid tokens are rejected. The gateway
   speaks HTTP outward and bridges to the stdio MCP server by running it
   as a child process, matching replies to requests by JSON-RPC id.

3. Streaming guardrail (src/llm_gateway)
   A streaming chat completions endpoint that proxies to a model provider
   and redacts emails, US social security numbers and credit card numbers
   from the response, replacing each with [REDACTED], while the response
   is still streaming. Credit card matches are confirmed with a Luhn
   check to avoid false positives. A rolling buffer holds back only a
   short tail so a value split across chunk boundaries is still caught.
   The full response is never accumulated in memory. Time to first token
   stays low. The buffer is flushed at end of stream.

4. Rate limiter and model router (src/llm_gateway)
   A token aware sliding window rate limiter, default 50000 tokens per
   minute per tenant API key, stored in on disk SQLite so limits survive
   a restart. Usage rows older than the window are deleted. If the
   primary provider returns 429 or does not respond within 3000 ms, the
   request fails over to the backup provider and the in flight request is
   cancelled cleanly. A 400 does not trigger failover. Outward errors are
   short and sanitised with an error id; full detail goes to the log and
   audit table.

## Providers

OpenAI is primary, Groq is backup. Groq uses the same request format as
OpenAI, so one provider class covers both with a different base URL, key
and model. All provider calls use httpx directly. The openai and groq
SDKs are deliberately not used, because the SDK retries 429s internally
which would hide the signal the router needs, applies timeouts per
attempt rather than per call, and unpacks the SSE stream that this
gateway needs to forward intact.

## Shared foundation

src/core holds config, database, logging setup, audit and errors. Every
service imports from it. Nothing in core is duplicated elsewhere.

## Data

SQLite on disk at data/gateway.db. Tables: customers, refunds, tenants,
token_usage, audit_log. The audit log records every decision the gateway
makes, including blocked tool calls, redaction counts by type, failovers
and rate limit rejections. It never stores a redacted value itself.

## Non negotiables

- No print statements anywhere. Logging goes to stderr only.
- No hardcoded keys. Everything through src/core/config.py from .env.
- Pydantic v2 with extra="forbid" on all external input.
- Every module has a matching test file.
- Test doubles live only in tests/fault_injection and are never imported
  by anything under src/.
