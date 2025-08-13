Postgres MCP

Endpoints
- GET /healthz
- POST /db/search

POST /db/search body example:
{
	"query": "vector indexes",
	"search_type": "hybrid",
	"top_k": 10,
	"use_pgai": true
}

Env/config
- Reads DB connection from env or /app/config/config.json
- Optional SSH tunnel via env SSH_* variables
- Optional pgai embedding if PGAI_ENABLED=true or request.use_pgai=true