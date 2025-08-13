# MCP Servers: Tavily Web Search and PostgreSQL Vector Search

This project provides two containerized FastAPI services that function as MCP-style tool servers for CrewAI agents:

- Tavily Search MCP: wraps Tavily Search and Extract APIs
- Postgres Search MCP: provides keyword, semantic and hybrid search on AWS RDS PostgreSQL with pgvector/pgvectorscale/pgai

Both services are configured to reuse the same environment, config and key files as your `microservices_oracle_ia` repo.

## Quick start

1) Copy your existing files from `microservices_oracle_ia` into this repo root:
- `.env` and `.env.debug`
- `config.json` (place under `./config/config.json`)
- secret key file (place under `./secrets/secret.key` if applicable)
- SSH private keys (`.pem`/`.ppk`) for DB access (place under `./secrets/keys/`)

2) Start services
- Production-like:
  - `docker compose up --build`
- Debug env file:
  - `docker compose --env-file .env.debug up --build`

Services:
- Tavily MCP: http://localhost:8701
- Postgres MCP: http://localhost:8702

## Environment variables

This stack reads the same envs as your existing repo where possible, and also supports common `POSTGRES_*` variables.

Supported vars (read from container environment and/or `config/config.json`):
- Tavily
  - `TAVILY_API_KEY`
- PostgreSQL
  - `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
  - OR equivalents: `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`
  - Optional table/column overrides: `PG_TABLE_NAME` (default `documents`), `PG_TEXT_COLUMN` (default `content`), `PG_EMBEDDING_COLUMN` (default `embedding`), `PG_METADATA_COLUMN` (default `metadata`)
  - Semantic search options: `PGAI_ENABLED` (default `false`), `PGAI_MODEL` (default `text-embedding-3-small`)
  - Hybrid weighting: `HYBRID_TEXT_WEIGHT` (default `0.5`), `HYBRID_VECTOR_WEIGHT` (default `0.5`)
- Optional SSH tunnel to RDS via bastion
  - `SSH_TUNNEL_ENABLED` (true/false)
  - `SSH_HOST`, `SSH_PORT` (default 22), `SSH_USER`, `SSH_PRIVATE_KEY_FILE` (e.g., `/app/secrets/keys/db_bastion.pem`)

If `.env.debug` is desired, run docker compose with `--env-file .env.debug`.

## Secrets and config files

- Place `config.json` in `./config/config.json` (mounted read-only to `/app/config/config.json`)
- Place secret key file (if used by your repo) in `./secrets/secret.key`
- Place SSH private keys in `./secrets/keys/*.pem` (or `.ppk` if converted by OpenSSH)

Note: Paramiko/sshtunnel require OpenSSH PEM keys. If you have a `.ppk`, convert to `.pem` before use (`puttygen key.ppk -O private-openssh -o key.pem`).

## Tavily MCP API

- POST `http://localhost:8701/tavily/search`
  - Body: `{ "query": "string", "search_depth": "basic|advanced", "max_results": 10, ... }`
  - Returns Tavily search results

- POST `http://localhost:8701/tavily/extract`
  - Body: `{ "url": "https://...", "include_images": false, ... }`

Environment: `TAVILY_API_KEY` required

## Postgres MCP API

- GET `http://localhost:8702/healthz`
- POST `http://localhost:8702/db/search`
  - Body:
```json
{
  "query": "what is vector db",
  "search_type": "keyword|semantic|hybrid",
  "top_k": 10,
  "vector": [0.1, 0.2, 0.3],
  "use_pgai": true
}
```
  - Behavior:
    - keyword: ILIKE and full-text ranking
    - semantic: vector similarity using `pgvector`; if `use_pgai=true` and pgai configured, query-side embedding computed with `pgai` (server-side)
    - hybrid: combines full-text rank and vector distance with weights from env

Table assumptions (override via env):
- table: `documents`
- columns: `content` text, `embedding` vector, `metadata` jsonb

Requires DB to already have extensions installed: `pgvector`, `timescaledb/pgvectorscale` (for indexing), and `pgai` if server-side embeddings are used.

Indexing tips (run in your DB):
- `CREATE INDEX ON documents USING hnsw (embedding);` (pgvectorscale)
- `CREATE INDEX ON documents USING ivfflat (embedding vector_l2_ops);` (pgvector)
- `CREATE INDEX ON documents USING gin (to_tsvector('english', content));` (full-text)

## CrewAI integration example

A simple example showing how an agent can call these tools via HTTP:

```python
from crewai import Agent, Task, Crew
import requests

class TavilySearchTool:
    def __call__(self, query: str, **kwargs):
        resp = requests.post(
            "http://localhost:8701/tavily/search",
            json={"query": query, **kwargs}, timeout=60
        )
        resp.raise_for_status()
        return resp.json()

class PostgresSearchTool:
    def __call__(self, query: str, search_type: str = "hybrid", top_k: int = 10):
        resp = requests.post(
            "http://localhost:8702/db/search",
            json={"query": query, "search_type": search_type, "top_k": top_k}, timeout=60
        )
        resp.raise_for_status()
        return resp.json()

web_search = TavilySearchTool()
pg_search = PostgresSearchTool()

researcher = Agent(
    role="Web Researcher",
    goal="Gather up-to-date context from the web",
    backstory="You leverage Tavily's web search",
)

analyst = Agent(
    role="Data Analyst",
    goal="Retrieve relevant similar documents from Postgres",
    backstory="You use vector and hybrid search",
)

t1 = Task(description="Find latest mentions of pgvectorscale", agent=researcher, tools=[web_search])
t2 = Task(description="Find similar docs for 'pgvector indexing'", agent=analyst, tools=[pg_search])

crew = Crew(agents=[researcher, analyst], tasks=[t1, t2])
result = crew.kickoff()
print(result)
```

If your CrewAI install supports MCP natively, you can also treat these as external HTTP tools using the same endpoints.

## Security notes

- Never bake secrets into images. This stack mounts `./secrets` and `./config` read-only into containers.
- For SSH tunneling, mount your PEM file from `./secrets/keys/...` and set `SSH_TUNNEL_ENABLED=true`.
- For `pgai` usage, ensure your DB-side provider credentials are configured per the Timescale `pgai` docs.

## License

MIT