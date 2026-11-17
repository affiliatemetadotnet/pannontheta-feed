#!/usr/bin/env python3
"""Shoprenter API -> raw/products.json + raw/categories.json (az arukereso_feed.py bemenete).

Belépési adatok környezeti változóból (vagy .env / --env fájlból):
  SHOPRENTER_CLIENT_ID, SHOPRENTER_CLIENT_SECRET
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    from feedgen.shoprenter import ShoprenterClient  # noqa: E402
except ImportError:  # önálló futtatás (Drive-ról letöltve): a kliens mellette van
    from shoprenter_client import ShoprenterClient  # noqa: E402


def load_env(path: Path) -> None:
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_HREF_ID = re.compile(r"myshoprenter\.hu/(?:api/)?[A-Za-z]+/([^?]+?)/?$")


def add_ids(obj):
    """Az API csak href-et ad sok hivatkozásnál – az utolsó URL-szakaszból pótoljuk az 'id'-t."""
    if isinstance(obj, dict):
        if "href" in obj and "id" not in obj and "?" not in obj["href"]:
            m = _HREF_ID.search(obj["href"])
            if m:
                obj["id"] = m.group(1)
        for v in obj.values():
            add_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            add_ids(v)
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(HERE / "raw"))
    ap.add_argument("--env", default=str(HERE / ".env"))
    a = ap.parse_args()
    load_env(Path(a.env))
    raw = Path(a.raw)
    raw.mkdir(parents=True, exist_ok=True)

    c = ShoprenterClient("pannontheta", page_size=200)
    products = add_ids(c.get_all("productExtend"))
    categories = add_ids(c.get_all("categoryExtend"))
    if len(products) < 100:
        raise SystemExit(f"Gyanúsan kevés termék jött le ({len(products)}) – megszakítva")
    (raw / "products.json").write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
    (raw / "categories.json").write_text(json.dumps(categories, ensure_ascii=False), encoding="utf-8")
    print(f"Letöltve: {len(products)} termék, {len(categories)} kategória -> {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
