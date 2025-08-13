MCP Servers for CrewAI: Tavily and Postgres

- See `MCP-README.md` for full instructions
- Copy `.env`, `.env.debug`, `config.json`, and keys from your `microservices_oracle_ia` repo into this project:
  - `.env` and `.env.debug` to project root
  - `config.json` to `./config/config.json`
  - secret files and PEM/PPK keys to `./secrets` (e.g., `./secrets/keys/*.pem`)

Run
- `docker compose up --build`
- Or with debug env: `docker compose --env-file .env.debug up --build`

Endpoints
- Tavily: http://localhost:8701
- Postgres: http://localhost:8702 
