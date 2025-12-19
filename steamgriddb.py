from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


class SteamGridDBError(RuntimeError):
    pass


@dataclass(frozen=True)
class SteamGridDBGame:
    id: int
    name: str


def _get_score(item: dict[str, Any]) -> int:
    # SteamGridDB responses vary across endpoints; be defensive.
    for key in ("score", "votes", "upvotes"):
        v = item.get(key)
        if isinstance(v, int):
            return v
    up = item.get("upvotes")
    down = item.get("downvotes")
    if isinstance(up, int) and isinstance(down, int):
        return up - down
    return 0


class SteamGridDBClient:
    BASE = "https://www.steamgriddb.com/api/v2"

    def __init__(self, api_key: Optional[str] = None, timeout_s: int = 15):
        self.api_key = api_key or os.environ.get("STEAMGRIDDB_API_KEY", "")
        self.timeout_s = timeout_s
        if not self.api_key:
            raise SteamGridDBError("Missing API key (set STEAMGRIDDB_API_KEY)")

    def _request_json(self, path: str) -> dict[str, Any]:
        url = f"{self.BASE}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "mGBA-Forwarder-Tools",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            raise SteamGridDBError(f"HTTP {e.code} for {url}: {body[:300]}") from e
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            raise SteamGridDBError(f"Request failed for {url}: {e}") from e

    def search_autocomplete(self, query: str) -> list[SteamGridDBGame]:
        q = urllib.parse.quote(query)
        data = self._request_json(f"/search/autocomplete/{q}")
        items = data.get("data") or []
        games: list[SteamGridDBGame] = []
        for it in items:
            try:
                games.append(SteamGridDBGame(id=int(it["id"]), name=str(it["name"])))
            except Exception:
                continue
        return games

    def _best_asset_url(self, path: str) -> Optional[str]:
        data = self._request_json(path)
        items = data.get("data") or []
        if not items:
            return None
        best = max(items, key=_get_score)
        url = best.get("url")
        return str(url) if url else None

    def _asset_list(self, path: str) -> list[dict[str, Any]]:
        data = self._request_json(path)
        items = data.get("data") or []
        assets: list[dict[str, Any]] = []
        for it in items:
            try:
                assets.append(
                    {
                        "url": it.get("url"),
                        "thumb": it.get("thumb") or it.get("thumb_url"),
                        "width": it.get("width"),
                        "height": it.get("height"),
                        "score": _get_score(it),
                    }
                )
            except Exception:
                continue
        return assets

    def best_icon_url(self, game_id: int) -> Optional[str]:
        return self._best_asset_url(f"/icons/game/{game_id}")

    def best_logo_url(self, game_id: int) -> Optional[str]:
        return self._best_asset_url(f"/logos/game/{game_id}")

    def icons(self, game_id: int) -> list[dict[str, Any]]:
        return self._asset_list(f"/icons/game/{game_id}")

    def logos(self, game_id: int) -> list[dict[str, Any]]:
        return self._asset_list(f"/logos/game/{game_id}")

    def download(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "mGBA-Forwarder-Tools"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise SteamGridDBError(f"HTTP {e.code} downloading {url}") from e
        except urllib.error.URLError as e:
            raise SteamGridDBError(f"Download failed {url}: {e}") from e
