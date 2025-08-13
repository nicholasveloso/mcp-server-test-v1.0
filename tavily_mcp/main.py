import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

CONFIG_PATH = Path("/app/config/config.json")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
	try:
		return json.loads(path.read_text()) if path.exists() else None
	except Exception:
		return None


def load_config_value(key: str, default: Optional[str] = None) -> Optional[str]:
	cfg = _read_json(CONFIG_PATH) or {}
	if key == "TAVILY_API_KEY":
		# Try env first
		val = os.getenv(key)
		if val:
			return val
		# Try common nested keys
		for path in (["tavily", "api_key"], ["tavily", "key"], ["tavily_api_key"], ["tavily", "token" ]):
			cur: Any = cfg
			ok = True
			for k in path:
				if isinstance(cur, dict) and k in cur:
					cur = cur[k]
				else:
					ok = False
					break
			if ok and isinstance(cur, str) and cur:
				return cur
		# Fallback to top-level
		if key in cfg and isinstance(cfg[key], str) and cfg[key]:
			return cfg[key]
		return default
	# Generic
	if CONFIG_PATH.exists():
		try:
			if key in cfg and isinstance(cfg[key], str) and cfg[key]:
				return cfg[key]
		except Exception:
			pass
	return os.getenv(key, default)


class SearchBody(BaseModel):
	query: str = Field(..., description="Search query")
	search_depth: Optional[str] = Field(None, description="basic or advanced")
	max_results: Optional[int] = Field(10, description="Max results")
	include_images: Optional[bool] = False
	include_image_descriptions: Optional[bool] = False
	include_answer: Optional[bool] = False
	include_raw_content: Optional[bool] = False
	include_domains: Optional[list[str]] = None
	exclude_domains: Optional[list[str]] = None
	time_range: Optional[str] = None
	days: Optional[int] = None
	topic: Optional[str] = None


class ExtractBody(BaseModel):
	url: str
	include_images: Optional[bool] = False
	include_image_descriptions: Optional[bool] = False
	include_raw_content: Optional[bool] = False


app = FastAPI(title="Tavily MCP")


def get_tavily_api_key() -> str:
	api_key = load_config_value("TAVILY_API_KEY")
	if not api_key:
		raise HTTPException(status_code=500, detail="TAVILY_API_KEY not configured")
	return api_key


@app.get("/")
async def index() -> Dict[str, str]:
	return {"service": "tavily_mcp"}


@app.post("/tavily/search")
async def tavily_search(body: SearchBody) -> Dict[str, Any]:
	api_key = get_tavily_api_key()
	headers = {"Content-Type": "application/json", "X-API-Key": api_key}
	payload = body.model_dump(exclude_none=True)
	async with httpx.AsyncClient(timeout=60) as client:
		resp = await client.post("https://api.tavily.com/search", headers=headers, json=payload)
		if resp.status_code != 200:
			raise HTTPException(status_code=resp.status_code, detail=resp.text)
		return resp.json()


@app.post("/tavily/extract")
async def tavily_extract(body: ExtractBody) -> Dict[str, Any]:
	api_key = get_tavily_api_key()
	headers = {"Content-Type": "application/json", "X-API-Key": api_key}
	payload = body.model_dump(exclude_none=True)
	async with httpx.AsyncClient(timeout=60) as client:
		resp = await client.post("https://api.tavily.com/extract", headers=headers, json=payload)
		if resp.status_code != 200:
			raise HTTPException(status_code=resp.status_code, detail=resp.text)
		return resp.json()


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
	return {"status": "ok"}