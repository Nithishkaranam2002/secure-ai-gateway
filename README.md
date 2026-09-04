# Secure AI Gateway

A security gateway for AI traffic. It controls which tools an agent is allowed
to call, strips private data out of model responses while they are still
streaming, meters usage per tenant, and keeps serving when a model provider
fails.

Built for the Forward Deployed Engineer assessment. Four tasks, one system:
the MCP gateway proxies to the MCP server, and the LLM gateway runs every
response through the rate limiter, the router and the redactor in a single
request path.

## Where each task lives

| Task | What it is | Code | Tests |
|------|-----------|------|-------|
| 1 | MCP server, strict validation, stdio transport | `src/mcp_server/` | `test_schemas.py`, `test_tools.py`, `test_stdio_isolation.py` |
| 2 | MCP security gateway, bearer auth and tool filtering | `src/mcp_gateway/` | `test_auth.py`, `test_policy.py`, `test_mcp_gateway.py` |
| 3 | Streaming PII redaction | `src/llm_gateway/redaction.py`, `stream_handler.py` | `test_redaction.py`, `test_stream_handler.py` |
| 4 | Rate limiting and model failover | `src/llm_gateway/rate_limiter.py`, `router.py` | `test_rate_limiter.py`, `test_router.py` |

Shared foundation in `src/core/`: config, database, stderr only logging, audit
log, error sanitisation.

## Running it

Needs Python 3.11 or later. Copy the environment file and add your keys:

```bash
cp .env.example .env
# set OPENAI_API_KEY and GROQ_API_KEY
```

### With Docker

```bash
docker compose up --build -d
./scripts/demo.sh
```

### Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.seed_database

uvicorn src.mcp_gateway.app:app --port 8000 &
uvicorn src.llm_gateway.app:app --port 8001 &
sleep 5
./scripts/demo.sh
```

`scripts/demo.sh` exercises every requirement of all four tasks and prints a
pass or fail line for each. It is the fastest way to see the whole system work.

### Tests

```bash
pytest
```

251 tests, no network access required.

## Two things that will save you time

**Use `127.0.0.1`, not `localhost`.** On some machines `localhost` resolves to
IPv6 first and lands somewhere else entirely.

**Groq retires model names often.** If the backup returns a 404, list what your
key can actually serve and update `BACKUP_MODEL`:

```bash
python -c "
import httpx, os
from dotenv import load_dotenv; load_dotenv()
r = httpx.get('https://api.groq.com/openai/v1/models',
              headers={'Authorization': 'Bearer ' + os.environ['GROQ_API_KEY']})
print('\n'.join(sorted(d['id'] for d in r.json()['data'])))
"
```

Pick a plain chat model. Reasoning models spend their token budget thinking and
can return a successful response with an empty body, which is a poor thing for a
failover target to do. See `docs/decisions.md`.

## Endpoints

**MCP gateway, port 8000**

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/mcp` | Authenticated MCP JSON-RPC proxy |
| GET | `/health` | Liveness and downstream status |

**LLM gateway, port 8001**

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/v1/chat/completions` | Chat completions, streaming or not |
| GET | `/v1/usage` | Token usage for the calling tenant |
| GET | `/health` | Liveness and configured models |

Both serve interactive docs at `/docs`.

## Trying it by hand

Tokens carry a role and a tenant:

```bash
export VIEWER=$(python -m scripts.issue_token viewer --tenant tk_live_acme_9f2b)
export ADMIN=$(python -m scripts.issue_token admin --tenant tk_live_acme_9f2b)
```

A viewer is refused a privileged tool, and the request never reaches the
downstream server:

```bash
curl -s -X POST 127.0.0.1:8000/mcp -H "Authorization: Bearer $VIEWER" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

The same call with an admin token is forwarded. Swap `$VIEWER` for `$ADMIN` and
compare the audit log:

```bash
python -c "
from src.core.database import get_connection
with get_connection() as c:
    for r in c.execute('SELECT component, action, decision, actor FROM audit_log ORDER BY id DESC LIMIT 6'):
        print(dict(r))
"
```

A blocked call leaves a gateway row with no server row beneath it. That gap is
the proof the refusal happened before execution.

Redaction, mid stream:

```bash
curl -sN -X POST 127.0.0.1:8001/v1/chat/completions -H "Authorization: Bearer $VIEWER" \
  -H 'Content-Type: application/json' \
  -d '{"stream":true,"messages":[{"role":"user","content":"Reply with exactly: Card 4111 1111 1111 1111 belongs to test@example.com."}],"max_tokens":60}'
```

## How it fits together

AI agent ──▶ MCP gateway ──▶ MCP server (Tasks 2 and 1)
auth, stdio, strict
policy validation
app ──▶ LLM gateway ──▶ OpenAI (Tasks 4 and 3)
rate limit, └ fails over to Groq
route,
redact
│
▼
SQLite on disk
customers, usage, audit log




Both gateways write every decision to one audit log: blocked tool calls,
redaction counts by type, failovers, rate limit refusals. The values that were
redacted are never recorded.

Design decisions and their trade offs are in `docs/decisions.md`. The request
path in detail is in `docs/architecture.md`.

## Notable details

- The MCP server uses the SDK's low level API rather than the decorator API,
  because only the low level API turns a schema failure into JSON-RPC `-32602`
  rather than a tool result marked as an error.
- Malformed input and impossible requests are treated differently on purpose. A
  bad `customer_id` is a protocol error; an unknown customer is a readable
  result the model can act on.
- The gateway bridges HTTP to the stdio MCP server by running it as a child
  process, multiplexing concurrent callers over the single pipe by JSON-RPC id.
- Redaction holds back only a short tail of text, so a value split across chunk
  boundaries is still caught while the response streams. `test_redaction.py`
  asserts the output is identical at every chunk size from 1 to 39 characters.
- Provider calls use `httpx` directly rather than a vendor SDK, because the SDKs
  retry a 429 internally and would hide the signal the failover depends on.
- The measured gateway overhead on time to first token is under a millisecond;
  the audit log records upstream latency and gateway latency separately.
