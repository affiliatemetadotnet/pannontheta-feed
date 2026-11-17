"""Shoprenter Admin API kliens: hitelesítés, lapozás, újrapróbálkozás.

Két hitelesítési mód:
  * OAuth2 client credentials (ajánlott, Shoprenter admin > Beállítások > API beállítások > egyedi API kliens)
      SHOPRENTER_CLIENT_ID, SHOPRENTER_CLIENT_SECRET
  * Régi Basic Auth API felhasználó (ha a bolt még ezt használja)
      SHOPRENTER_API_USER, SHOPRENTER_API_KEY
"""
from __future__ import annotations

import os
import time
import logging

import requests

log = logging.getLogger(__name__)

OAUTH_URL = "https://oauth.app.shoprenter.net/{shop}/app/token"
API2_BASE = "https://{shop}.api2.myshoprenter.hu/api"   # OAuth tokennel
API1_BASE = "https://{shop}.api.myshoprenter.hu"        # Basic Auth-tal


class ShoprenterClient:
    def __init__(self, shop: str, page_size: int = 200, pause: float = 0.35):
        self.shop = shop
        self.page_size = page_size
        self.pause = pause
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self._token_expires = 0.0

        cid, secret = os.getenv("SHOPRENTER_CLIENT_ID"), os.getenv("SHOPRENTER_CLIENT_SECRET")
        user, key = os.getenv("SHOPRENTER_API_USER"), os.getenv("SHOPRENTER_API_KEY")
        if cid and secret:
            self.mode = "oauth"
            self._cid, self._secret = cid, secret
            self.base = API2_BASE.format(shop=shop)
            self._refresh_token()
        elif user and key:
            self.mode = "basic"
            self.s.auth = (user, key)
            self.base = API1_BASE.format(shop=shop)
        else:
            raise SystemExit(
                "Hiányzó belépési adatok. Töltsd ki a .env fájlt "
                "(SHOPRENTER_CLIENT_ID + SHOPRENTER_CLIENT_SECRET, vagy SHOPRENTER_API_USER + SHOPRENTER_API_KEY)."
            )

    # ---- auth -------------------------------------------------------------
    def _refresh_token(self) -> None:
        r = requests.post(
            OAUTH_URL.format(shop=self.shop),
            json={"grant_type": "client_credentials", "client_id": self._cid, "client_secret": self._secret},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            raise SystemExit(f"OAuth token kérés sikertelen ({r.status_code}): {r.text[:300]}")
        data = r.json()
        self.s.headers["Authorization"] = f"Bearer {data['access_token']}"
        self._token_expires = time.time() + int(data.get("expires_in", 3600)) - 120

    # ---- low level --------------------------------------------------------
    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        for attempt in range(6):
            if self.mode == "oauth" and time.time() > self._token_expires:
                self._refresh_token()
            try:
                r = self.s.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                wait = 2 ** attempt
                log.warning("Hálózati hiba (%s), újra %ss múlva", e, wait)
                time.sleep(wait)
                continue
            if r.status_code == 200:
                time.sleep(self.pause)
                return r.json()
            if r.status_code == 401 and self.mode == "oauth":
                self._refresh_token()
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                log.warning("%s válasz (%s), újra %ss múlva", r.status_code, path, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(f"API hiba {r.status_code} – {url}: {r.text[:300]}")
        raise RuntimeError(f"Többszöri próbálkozás után sem sikerült: {url}")

    def get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Végiglapozza az erőforrást (page=0..pageCount-1)."""
        params = dict(params or {})
        params.setdefault("full", 1)
        params["limit"] = self.page_size
        items, page = [], 0
        while True:
            params["page"] = page
            data = self.get(path, params)
            batch = data.get("items", [])
            items.extend(batch)
            page_count = int(data.get("pageCount", 1) or 1)
            log.info("%s: %d/%d oldal, eddig %d tétel", path, page + 1, page_count, len(items))
            page += 1
            if page >= page_count or not batch:
                break
        return items

    # ---- resources --------------------------------------------------------
    def products(self) -> list[dict]:
        return self.get_all("productExtend")

    def categories(self) -> list[dict]:
        return self.get_all("categoryExtend")

    def manufacturers(self) -> list[dict]:
        return self.get_all("manufacturers")

    def stock_statuses(self) -> list[dict]:
        return self.get_all("stockStatuses")
