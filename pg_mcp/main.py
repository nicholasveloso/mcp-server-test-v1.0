import json
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple
from urllib.parse import urlparse, unquote

import orjson
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sshtunnel import SSHTunnelForwarder

import psycopg
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector, Vector
from typing import Literal

CONFIG_PATH = Path("/app/config/config.json")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
	try:
		return json.loads(path.read_text()) if path.exists() else None
	except Exception:
		return None


def _deep_get(data: Dict[str, Any], keys: List[str]) -> Optional[Any]:
	cur: Any = data
	for k in keys:
		if isinstance(cur, dict) and k in cur:
			cur = cur[k]
		else:
			return None
	return cur


@dataclass
class DbConfig:
	host: str
	port: int
	database: str
	user: str
	password: str
	table: str = "documents"
	text_column: str = "content"
	embedding_column: str = "embedding"
	metadata_column: str = "metadata"


class SearchRequest(BaseModel):
	query: str = Field(..., description="Text query for keyword/full-text; for semantic, if use_pgai=false, client should also send 'vector'")
	search_type: Literal["keyword", "semantic", "hybrid"] = "hybrid"
	top_k: int = 10
	vector: Optional[List[float]] = None
	use_pgai: bool = False
	model: Optional[str] = None


def load_config_value(key: str, default: Optional[str] = None) -> Optional[str]:
	if CONFIG_PATH.exists():
		try:
			config = json.loads(CONFIG_PATH.read_text())
			if key in config and isinstance(config[key], (str, int, float, bool)):
				return str(config[key])
		except Exception:
			pass
	return os.getenv(key, default)


def load_db_config() -> DbConfig:
	cfg_json = _read_json(CONFIG_PATH) or {}
	# Try nested locations commonly used
	nested_candidates = [
		["postgres"], ["database"], ["db"], ["rds"], ["aws", "rds"], ["datasource"],
	]
	nested: Dict[str, Any] = {}
	for path_keys in nested_candidates:
		val = _deep_get(cfg_json, path_keys)
		if isinstance(val, dict):
			nested = val
			break

	# DSN/URL support
	dsn = (os.getenv("DATABASE_URL") or nested.get("database_url") or nested.get("url"))
	dsn_host = dsn_port = dsn_db = dsn_user = dsn_pass = None
	if dsn:
		try:
			o = urlparse(dsn)
			# postgresql://user:pass@host:port/dbname?params
			dsn_host = o.hostname or None
			dsn_port = o.port or None
			dsn_db = (o.path[1:] if o.path else None) or None
			dsn_user = unquote(o.username) if o.username else None
			dsn_pass = unquote(o.password) if o.password else None
		except Exception:
			pass

	host = (os.getenv("POSTGRES_HOST") or os.getenv("PGHOST") or dsn_host or nested.get("host") or nested.get("hostname") or nested.get("endpoint") or "localhost")
	port_val = (os.getenv("POSTGRES_PORT") or os.getenv("PGPORT") or (dsn_port if dsn_port is not None else None) or nested.get("port") or 5432)
	try:
		port = int(port_val)
	except Exception:
		port = 5432
	database = (os.getenv("POSTGRES_DB") or os.getenv("PGDATABASE") or dsn_db or nested.get("db") or nested.get("database") or "postgres")
	user = (os.getenv("POSTGRES_USER") or os.getenv("PGUSER") or dsn_user or nested.get("user") or nested.get("username") or "postgres")
	password = (os.getenv("POSTGRES_PASSWORD") or os.getenv("PGPASSWORD") or dsn_pass or nested.get("password") or "")
	# Optional password file
	pw_file = (os.getenv("PGPASSWORD_FILE") or nested.get("password_file"))
	if not password and pw_file and Path(pw_file).exists():
		try:
			password = Path(pw_file).read_text().strip()
		except Exception:
			pass

	table = (os.getenv("PG_TABLE_NAME") or nested.get("table") or "documents")
	text_col = (os.getenv("PG_TEXT_COLUMN") or nested.get("text_column") or "content")
	emb_col = (os.getenv("PG_EMBEDDING_COLUMN") or nested.get("embedding_column") or "embedding")
	meta_col = (os.getenv("PG_METADATA_COLUMN") or nested.get("metadata_column") or "metadata")
	return DbConfig(host, port, database, user, password, table, text_col, emb_col, meta_col)


@dataclass
class SshConfig:
	enabled: bool
	host: Optional[str]
	port: int
	user: Optional[str]
	key_file: Optional[str]


def load_ssh_config() -> SshConfig:
	cfg_json = _read_json(CONFIG_PATH) or {}
	ssh_obj = _deep_get(cfg_json, ["ssh"]) if isinstance(cfg_json, dict) else None
	enabled = (load_config_value("SSH_TUNNEL_ENABLED") or (str(ssh_obj.get("enabled")) if isinstance(ssh_obj, dict) and ssh_obj.get("enabled") is not None else "false")).lower() == "true"
	host = load_config_value("SSH_HOST") or (ssh_obj.get("host") if isinstance(ssh_obj, dict) else None)
	port_val = load_config_value("SSH_PORT") or (ssh_obj.get("port") if isinstance(ssh_obj, dict) else 22)
	try:
		port = int(port_val)
	except Exception:
		port = 22
	user = load_config_value("SSH_USER") or (ssh_obj.get("user") if isinstance(ssh_obj, dict) else None)
	key_file = load_config_value("SSH_PRIVATE_KEY_FILE") or (ssh_obj.get("private_key_file") if isinstance(ssh_obj, dict) else None)
	return SshConfig(enabled, host, port, user, key_file)


@contextmanager
def ssh_tunnel_if_needed(db_conf: DbConfig, ssh_conf: SshConfig) -> Generator[Tuple[str, int], None, None]:
	if not ssh_conf.enabled:
		yield db_conf.host, db_conf.port
		return
	if not (ssh_conf.host and ssh_conf.user and ssh_conf.key_file):
		raise RuntimeError("SSH_TUNNEL_ENABLED is true but SSH_HOST/SSH_USER/SSH_PRIVATE_KEY_FILE not fully set")
	with SSHTunnelForwarder(
		(ssh_conf.host, ssh_conf.port),
		ssh_username=ssh_conf.user,
		ssh_pkey=ssh_conf.key_file,
		remote_bind_address=(db_conf.host, db_conf.port),
	) as tunnel:
		local_port = tunnel.local_bind_port
		yield "127.0.0.1", local_port


def open_connection(host: str, port: int, db_conf: DbConfig) -> psycopg.Connection:
	conn = psycopg.connect(
		host=host,
		port=port,
		dbname=db_conf.database,
		user=db_conf.user,
		password=db_conf.password,
		row_factory=dict_row,
		autocommit=True,
	)
	# Register pgvector type handlers
	register_vector(conn)
	return conn


def build_fulltext_rank_sql(table: str, text_col: str) -> str:
	return f"to_tsvector('english', {text_col})"


def validate_vector(vec: Optional[List[float]]):
	if vec is None or len(vec) == 0:
		raise HTTPException(status_code=400, detail="vector must be provided for semantic search when use_pgai=false")


def hybrid_sql(db: DbConfig, use_pgai: bool, model: Optional[str]) -> str:
	text_weight = float(load_config_value("HYBRID_TEXT_WEIGHT") or 0.5)
	vec_weight = float(load_config_value("HYBRID_VECTOR_WEIGHT") or 0.5)
	ft = build_fulltext_rank_sql(db.table, db.text_column)
	if use_pgai:
		model_name = model or (load_config_value("PGAI_MODEL") or "text-embedding-3-small")
		# pgai: ai.embed(text, 'model') returns vector
		return (
			f"SELECT *, "
			f"({text_weight} * ts_rank_cd({ft}, plainto_tsquery('english', %(q)s)) - "
			f"{vec_weight} * ({db.embedding_column} <=> ai.embed(%(q)s, %(model)s))) AS score "
			f"FROM {db.table} "
			f"ORDER BY score DESC "
			f"LIMIT %(k)s"
		)
	else:
		return (
			f"SELECT *, "
			f"({text_weight} * ts_rank_cd({ft}, plainto_tsquery('english', %(q)s)) - "
			f"{vec_weight} * ({db.embedding_column} <=> %(v)s)) AS score "
			f"FROM {db.table} "
			f"ORDER BY score DESC "
			f"LIMIT %(k)s"
		)


def semantic_sql(db: DbConfig, use_pgai: bool, model: Optional[str]) -> str:
	if use_pgai:
		model_name = model or (load_config_value("PGAI_MODEL") or "text-embedding-3-small")
		return (
			f"SELECT * FROM {db.table} "
			f"ORDER BY {db.embedding_column} <=> ai.embed(%(q)s, %(model)s) "
			f"LIMIT %(k)s"
		)
	else:
		return (
			f"SELECT * FROM {db.table} "
			f"ORDER BY {db.embedding_column} <=> %(v)s "
			f"LIMIT %(k)s"
		)


def keyword_sql(db: DbConfig) -> str:
	ft = build_fulltext_rank_sql(db.table, db.text_column)
	return (
		f"SELECT *, ts_rank_cd({ft}, plainto_tsquery('english', %(q)s)) AS rank "
		f"FROM {db.table} "
		f"WHERE {db.text_column} ILIKE %(ilike)s "
		f"ORDER BY rank DESC "
		f"LIMIT %(k)s"
	)


app = FastAPI(title="Postgres MCP")


def db_query(conn: psycopg.Connection, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
	with conn.cursor() as cur:
		cur.execute(sql, params)
		rows = cur.fetchall()
		return rows


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
	return {"status": "ok"}


@app.post("/db/search")
async def db_search(req: SearchRequest) -> Dict[str, Any]:
	db_conf = load_db_config()
	ssh_conf = load_ssh_config()
	use_pgai = req.use_pgai or ((load_config_value("PGAI_ENABLED") or "false").lower() == "true")
	model = req.model or load_config_value("PGAI_MODEL")

	with ssh_tunnel_if_needed(db_conf, ssh_conf) as (host, port):
		try:
			conn = open_connection(host, port, db_conf)
		except Exception as e:
			raise HTTPException(status_code=500, detail=f"DB connection failed: {e}")
		try:
			if req.search_type == "keyword":
				sql = keyword_sql(db_conf)
				params = {"q": req.query, "ilike": f"%{req.query}%", "k": req.top_k}
				rows = db_query(conn, sql, params)
			elif req.search_type == "semantic":
				sql = semantic_sql(db_conf, use_pgai, model)
				if use_pgai:
					params = {"q": req.query, "model": model or "text-embedding-3-small", "k": req.top_k}
				else:
					validate_vector(req.vector)
					params = {"v": Vector(req.vector), "k": req.top_k}
				rows = db_query(conn, sql, params)
			elif req.search_type == "hybrid":
				sql = hybrid_sql(db_conf, use_pgai, model)
				if use_pgai:
					params = {"q": req.query, "model": model or "text-embedding-3-small", "k": req.top_k}
				else:
					validate_vector(req.vector)
					params = {"q": req.query, "v": Vector(req.vector), "k": req.top_k}
				rows = db_query(conn, sql, params)
			else:
				raise HTTPException(status_code=400, detail="Invalid search_type")
		finally:
			conn.close()

	return {"results": rows}