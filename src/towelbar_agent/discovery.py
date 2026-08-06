from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .protocol import endpoint_candidates


@dataclass(frozen=True)
class Asset:
    url: str
    content_type: str
    sha256: str
    path: str


@dataclass(frozen=True)
class PortalSnapshot:
    base_url: str
    final_url: str
    status_code: int
    captured_at: str
    assets: tuple[Asset, ...]
    endpoint_candidates: tuple[str, ...]


ASSET_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|css)(?:\?[^"']*)?)["']""",
    re.I,
)


def snapshot_portal(
    base_url: str,
    output_dir: str | Path,
    timeout: float = 8,
    transport: httpx.BaseTransport | None = None,
) -> PortalSnapshot:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    sources: list[str] = []
    assets: list[Asset] = []
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "towelbar-discovery/0.1"},
        transport=transport,
    ) as client:
        root = client.get(base_url)
        root.raise_for_status()
        root_path = destination / "index.html"
        root_path.write_bytes(root.content)
        sources.append(root.text)
        seen: set[str] = set()
        for reference in ASSET_RE.findall(root.text):
            url = urljoin(str(root.url), reference)
            parsed = urlparse(url)
            if parsed.netloc != urlparse(str(root.url)).netloc or url in seen:
                continue
            seen.add(url)
            response = client.get(url)
            response.raise_for_status()
            name = Path(parsed.path).name or "asset"
            path = destination / name
            if path.exists():
                path = destination / f"{len(assets):02d}-{name}"
            path.write_bytes(response.content)
            if "javascript" in response.headers.get("content-type", "") or name.endswith(".js"):
                sources.append(response.text)
            assets.append(
                Asset(
                    url=url,
                    content_type=response.headers.get("content-type", ""),
                    sha256=hashlib.sha256(response.content).hexdigest(),
                    path=str(path),
                )
            )
    snapshot = PortalSnapshot(
        base_url=base_url,
        final_url=str(root.url),
        status_code=root.status_code,
        captured_at=datetime.now(timezone.utc).isoformat(),
        assets=tuple(assets),
        endpoint_candidates=tuple(endpoint_candidates("\n".join(sources))),
    )
    (destination / "snapshot.json").write_text(
        json.dumps(asdict(snapshot), indent=2) + "\n"
    )
    return snapshot
