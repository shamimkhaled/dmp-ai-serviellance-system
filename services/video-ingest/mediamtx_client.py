"""MediaMTX v3 API client — register camera paths dynamically."""

import logging
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("video-ingest.mediamtx")


def _redact_source(source: str) -> str:
    if source == "publisher" or "://" not in source:
        return source
    parts = urlsplit(source)
    if not parts.username and not parts.password:
        return source
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def ensure_path(
    base_url: str,
    path_name: str,
    *,
    source: str,
    source_on_demand: bool = True,
) -> None:
    """Create or update a MediaMTX path (pull RTSP or publisher)."""
    payload: dict = {"source": source}
    if source != "publisher":
        payload["sourceOnDemand"] = source_on_demand
        payload["rtspTransport"] = "tcp"

    async with httpx.AsyncClient(timeout=10) as client:
        # Patch first (path usually already exists after first connect)
        patch = await client.patch(
            f"{base_url}/v3/config/paths/patch/{path_name}",
            json=payload,
        )
        if patch.status_code in (200, 201):
            log.info(f"MediaMTX path updated: {path_name} → {_redact_source(source)}")
            return

        add = await client.post(
            f"{base_url}/v3/config/paths/add/{path_name}",
            json=payload,
        )
        if add.status_code in (200, 201):
            log.info(f"MediaMTX path added: {path_name} → {_redact_source(source)}")
            return

        if "already exists" in (add.text or ""):
            log.info(f"MediaMTX path {path_name} already exists (config unchanged)")
            return

        detail = patch.text or add.text
        raise RuntimeError(f"MediaMTX path {path_name} failed: {detail}")


async def remove_path(base_url: str, path_name: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{base_url}/v3/config/paths/delete/{path_name}")
        if resp.status_code not in (200, 201, 404):
            log.warning(f"MediaMTX delete {path_name}: {resp.status_code} {resp.text}")


async def get_path(base_url: str, path_name: str) -> dict | None:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{base_url}/v3/paths/get/{path_name}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def list_paths(base_url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{base_url}/v3/paths/list")
        resp.raise_for_status()
        return resp.json().get("items", [])
