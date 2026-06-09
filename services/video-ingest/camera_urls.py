"""Universal RTSP URL builder for common IP camera brands."""

from urllib.parse import quote

BRAND_TEMPLATES: dict[str, dict] = {
    "hikvision": {
        "label": "Hikvision",
        "template": "rtsp://{auth}{host}:{port}/Streaming/Channels/{channel}01",
        "default_port": 554,
        "notes": "Channel 1 = main stream, 2 = sub stream (use channel=102 for sub).",
    },
    "dahua": {
        "label": "Dahua / Amcrest",
        "template": "rtsp://{auth}{host}:{port}/cam/realmonitor?channel={channel}&subtype=0",
        "default_port": 554,
        "notes": "subtype=0 main stream, subtype=1 sub stream.",
    },
    "axis": {
        "label": "Axis",
        "template": "rtsp://{auth}{host}:{port}/axis-media/media.amp",
        "default_port": 554,
        "notes": "Append ?camera=N for multi-sensor models.",
    },
    "tplink": {
        "label": "TP-Link / Tapo",
        "template": "rtsp://{auth}{host}:{port}/stream1",
        "default_port": 554,
        "notes": "stream1 = HD, stream2 = SD on most Tapo models.",
    },
    "reolink": {
        "label": "Reolink",
        "template": "rtsp://{auth}{host}:{port}/h264Preview_{channel}_main",
        "default_port": 554,
        "notes": "Use h264Preview_01_sub for sub stream.",
    },
    "uniview": {
        "label": "Uniview",
        "template": "rtsp://{auth}{host}:{port}/media/video{channel}",
        "default_port": 554,
        "notes": "video1 = main stream on most models.",
    },
    "onvif": {
        "label": "ONVIF / Generic",
        "template": "rtsp://{auth}{host}:{port}/onvif1",
        "default_port": 554,
        "notes": "Path varies by firmware — use Custom URL if this fails.",
    },
    "custom": {
        "label": "Custom URL",
        "template": "",
        "default_port": 554,
        "notes": "Paste the full RTSP URL from your camera manual or ONVIF tool.",
    },
}


def _auth_segment(username: str | None, password: str | None) -> str:
    if not username:
        return ""
    user = quote(username, safe="")
    if password is not None:
        return f"{user}:{quote(password, safe='')}@"
    return f"{user}@"


def build_rtsp_url(
    brand: str,
    *,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    channel: int = 1,
    rtsp_url: str | None = None,
) -> str:
    brand = (brand or "custom").lower()
    meta = BRAND_TEMPLATES.get(brand, BRAND_TEMPLATES["custom"])

    if brand == "custom":
        if not rtsp_url:
            raise ValueError("rtsp_url is required for custom brand")
        return rtsp_url.strip()

    if not host:
        raise ValueError("host is required for brand-based connection")

    port = port or meta["default_port"]
    auth = _auth_segment(username, password)
    return meta["template"].format(
        auth=auth, host=host, port=port, channel=channel
    )


def list_brands() -> list[dict]:
    return [
        {
            "id": key,
            "label": val["label"],
            "default_port": val["default_port"],
            "notes": val["notes"],
            "example_fields": ["host", "port", "username", "password", "channel"],
        }
        for key, val in BRAND_TEMPLATES.items()
    ]
