"""FFmpeg H.264 relay for H.265 camera streams (browser WHEP/HLS playback)."""

import asyncio
import logging

from mediamtx_client import ensure_path, get_path, remove_path

log = logging.getLogger("video-ingest.transcode")

_procs: dict[str, asyncio.subprocess.Process] = {}
_lock = asyncio.Lock()


def view_path_id(camera_id: str) -> str:
    return f"{camera_id}_view"


def _relay_cmd(src: str, dst: str) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", src,
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-g", "25", "-bf", "0",
        "-an",
        "-f", "rtsp", "-rtsp_transport", "tcp", dst,
    ]


async def ensure_h264_relay(
    mediamtx_url: str,
    rtsp_base: str,
    camera_id: str,
) -> str | None:
    """Start FFmpeg relay cam03 → cam03_view when source is live. Returns view id if running."""
    view_id = view_path_id(camera_id)
    src = f"{rtsp_base}/{camera_id}"
    dst = f"{rtsp_base}/{view_id}"

    async with _lock:
        proc = _procs.get(view_id)
        if proc and proc.returncode is None:
            return view_id

        source = await get_path(mediamtx_url, camera_id)
        if not source or not source.get("ready"):
            return None

        tracks = source.get("tracks") or []
        video = next((t for t in tracks if t not in ("Opus", "G711", "MPEG4Audio")), None)
        if video != "H265":
            return None

        await ensure_path(mediamtx_url, view_id, source="publisher", source_on_demand=False)

        if proc and proc.returncode is not None:
            _procs.pop(view_id, None)

        log.info(f"Starting H.264 relay {camera_id} → {view_id}")
        proc = await asyncio.create_subprocess_exec(
            *_relay_cmd(src, dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _procs[view_id] = proc
        asyncio.create_task(_watch_relay(view_id, proc))
        return view_id


async def _watch_relay(view_id: str, proc: asyncio.subprocess.Process) -> None:
    _, stderr = await proc.communicate()
    if proc.returncode not in (0, -15, None):
        err = (stderr or b"").decode(errors="replace").strip()
        log.warning(f"H.264 relay {view_id} exited ({proc.returncode}): {err[:200]}")
    _procs.pop(view_id, None)


async def stop_h264_relay(mediamtx_url: str, camera_id: str) -> None:
    view_id = view_path_id(camera_id)
    async with _lock:
        proc = _procs.pop(view_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        await remove_path(mediamtx_url, view_id)


async def sync_h265_relays(mediamtx_url: str, rtsp_base: str, camera_ids: list[str]) -> None:
    """Ensure relays for live H.265 paths; stop orphans."""
    active_views: set[str] = set()
    for cid in camera_ids:
        view_id = await ensure_h264_relay(mediamtx_url, rtsp_base, cid)
        if view_id:
            active_views.add(view_id)

    async with _lock:
        for view_id, proc in list(_procs.items()):
            if view_id not in active_views and proc.returncode is None:
                log.info(f"Stopping orphan relay {view_id}")
                proc.terminate()
                _procs.pop(view_id, None)
