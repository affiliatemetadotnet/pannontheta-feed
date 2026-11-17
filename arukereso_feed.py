#!/usr/bin/env python3
"""Pannon-Theta → Árukereső XML feed (irodai és tárgyalószékek, raktáron / 3-5 nap)

Bemenet: Shoprenter nyers adatok (raw/products.json, raw/categories.json) –
  ezeket vagy a Shoprenter MCP, vagy a feed.py API-letöltése állítja elő.
Kimenet: output/arukereso.xml + output/arukereso_riport.txt

Futtatás:  python arukereso_feed.py --raw raw --out output [--check-urls]
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent

# ------------------------------------------------------------------ szabályok
SHOP_URL = "https://pannontheta.hu"
IMG_BASE = "https://pannontheta.cdn.shoprenter.hu/custom/pannontheta/image/data/"

STOCK_STATUS_3_5 = "14"          # "Külső raktárról 3-5 munkanap"
DELIVERY_IN_STOCK = "1 munkanap"
DELIVERY_3_5 = "5 munkanap"      # Árukereső csak egyetlen számot fogad el -> a sáv felső határa

# Kizárt kategóriák (innerId): alkatrészek, kiegészítők, asztalok, csomagok, outlet/bemutató darabok
EXCLUDED_CATEGORIES = {
    "198",  # SZÉKSZERVIZ ÉS ALKATRÉSZEK
    "200", "202", "217", "218", "223", "226",  # gázlift, lábkereszt, lábtartó gyűrű, mechanika, görgő, karfa
    "262", "265", "284",  # kiegészítők, székalátétek, állófogasok
    "188", "274", "277",  # asztalok
    "285",  # csomagajánlatok (asztal + szék)
    "221",  # szolgáltatások
    "270",  # OUTLET – bemutató / kiállított darabok, csak személyesen vásárolhatók (Árukereső tiltja)
}

# Árukereső kategória: "Irodai székek > <legspecifikusabb széktípus>"; prioritási sorrend
CATEGORY_PRIORITY = [
    ("171", "Gamer székek"),
    ("174", "Térdeplőszékek"),
    ("177", "Aktív ülések, labdaszékek"),
    ("183", "Speciális munkaszékek"),
    ("268", "Konferenciaszékek"),
    ("180", "Tárgyalószékek"),
    ("283", "24 órás székek"),
    ("159", "24 órás székek"),
    ("165", "Nagy teherbírású székek"),
    ("164", "Főnöki forgószékek"),
    ("251", "Főnöki forgószékek"),
    ("162", "Ergonomikus forgószékek"),
    ("168", "Ergonomikus forgószékek"),
    ("248", "Operatív forgószékek"),
    ("273", "Ifjúsági forgószékek"),
    ("163", "Irodai forgószékek"),
    ("158", "Irodai forgószékek"),
]
CATEGORY_ROOT = "Irodai székek"

ATTR_LABELS = {"szin": "Szín", "anyag": "Anyag", "teherbiras": "Teherbírás", "karfa": "Karfa",
               "termektipus": "Terméktípus"}
SKIP_ATTRS = {"gyarto", "bemutato"}
# Márkanevek helyes írásmódja (a boltban csupa nagybetűvel szerepelnek)
BRAND_NAMES = {"ANTARES": "Antares", "LINEA": "Linea", "HALMAR": "Halmar", "ICOTRADE": "Icotrade",
               "OFFICE LINE": "Office Line", "TOPSTAR": "Topstar", "NOVISTYL": "Novistyl", "SITZONE": "Sitzone",
               "NOWY STYL": "Nowy Styl", "STARTUP": "Startup", "RIM": "RIM"}
# "20"-29 kezdetű EAN = bolti belső kód, nem hivatalos gyártói EAN
INTERNAL_EAN_PREFIX = tuple(str(i) for i in range(20, 30))


# ------------------------------------------------------------------ segédek
def inner(b64: str | None) -> str:
    if not b64:
        return ""
    return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "ignore").rsplit("=", 1)[-1]


def to_int(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


_TAGS = re.compile(r"<[^>]+>")


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    t = text
    for _ in range(3):
        u = html.unescape(t)
        if u == t:
            break
        t = u
    if _TAGS.search(t):
        t = re.sub(r"\s*\n\s*", " ", t)
    t = re.sub(r"<\s*li\b[^>]*>", "\n– ", t, flags=re.I)
    t = re.sub(r"<\s*(br|/p|/li|/h\d|hr|/tr|/div|/ul)\b[^>]*>", "\n", t, flags=re.I)
    t = _TAGS.sub(" ", t)
    t = html.unescape(t).replace("\xa0", " ").replace("­", "")
    t = re.sub(r" +([.,;:!?)])", r"\1", t)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.splitlines()]
    out = []
    for ln in lines:
        if not ln:
            continue
        # kapcsolati adat, link, e-mail nem kerülhet a leírásba
        if re.search(r"https?://|www\.|@|\+36|\btel\.?:", ln, re.I):
            continue
        out.append(ln)
    return "\n".join(out).strip()


MIN_IMAGE_PX = 150  # Árukereső minimum


def probe_image(url: str) -> str:
    """'200 image/jpeg' ha valódi, legalább 150x150-es kép; különben hibaleírás.
    A Shoprenter hiányzó képre is 200-at ad egy 100x100-as "nincs kép" helyettesítővel – ezt kiszűrjük."""
    for attempt in range(3):  # átmeneti hálózati hiba ne ejtsen ki terméket
        r = subprocess.run(["curl", "-s", "-L", "-m", "60", "-o", "-", "-w", "\n%{http_code} %{content_type}", url],
                           capture_output=True)
        body, _, meta = r.stdout.rpartition(b"\n")
        meta = meta.decode(errors="ignore")
        if meta.startswith("200") or meta.startswith("404"):
            break
        time.sleep(2 * (attempt + 1))
    if not meta.startswith("200"):
        return meta or "hiba"
    try:
        from PIL import Image
        import io
        w, h = Image.open(io.BytesIO(body)).size
    except Exception:
        return "nem kép"
    if w < MIN_IMAGE_PX or h < MIN_IMAGE_PX:
        return f"túl kicsi/helyettesítő kép ({w}x{h})"
    return meta


_UP = "A-ZÁÉÍÓÖŐÚÜŰ"
_LO = "a-záéíóöőúüű"
# tételhatár: kisbetű/szám/zárójel után szóköz, majd
#   nagybetűs szó (Állítható) | "A"/"Az" névelő | 1D-4D karfa | betű+szám kód (BR25)
_ITEM_SPLIT = re.compile(
    rf"(?<=[{_LO}0-9)%°\"])\s+(?=[{_UP}][{_LO}]|Az?\s+[{_LO}]|[1-4]D\b|[A-Z]{{2,}}\d)")
# mondatvég: írásjel + szóköz + nagybetű/szám, de nem rövidítés (kb. 80 mm)
_LAST_SENTENCE_END = re.compile(
    rf"(?<!\bkb)(?<!\bmin)(?<!\bmax)(?<!\bpl)(?<!\bill)(?<!\bstb)(?<!\bcca)(?<!\bdb)[.!?]\s+(?=[{_UP}0-9])")
_HEADING = re.compile(r"^((?:Főbb )?[Jj]ellemzők|Méretek|Műszaki adatok):\s*(.+)$")


def restore_bullets(text: str) -> str:
    """A boltban sortörés nélkül tárolt leírások felsorolását visszabontja.
    Csak akkor nyúl hozzá, ha a leírás egyetlen sor, és a bevezető mondatok után
    legalább 3 írásjel nélkül egymás után következő tétel van. A szöveg szavai nem változnak."""
    if "\n" in text:
        return text
    ends = list(_LAST_SENTENCE_END.finditer(text))
    if not ends:
        return text
    cut = ends[-1].end()
    intro, tail = text[:cut].strip(), text[cut:].strip()
    items = [x.strip() for x in _ITEM_SPLIT.split(tail) if x.strip()]
    if len(items) < 3 or any(len(x) > 120 for x in items):
        return text
    lines = [intro]
    for x in items:
        m = _HEADING.match(x)
        if m:
            lines += [m.group(1) + ":", "– " + m.group(2)]
        else:
            lines.append("– " + x)
    return "\n".join(lines)


def ean_valid(e: str) -> bool:
    if not e.isdigit() or len(e) not in (8, 12, 13, 14) or e.startswith(INTERNAL_EAN_PREFIX):
        return False
    d = [int(x) for x in e]
    s = sum(x * (3 if (len(d) - 1 - i) % 2 else 1) for i, x in enumerate(d[:-1]))
    return (10 - s % 10) % 10 == d[-1]


def warranty_months(*texts: str) -> int | None:
    pats = [r"garancia:\s*(\d+)\s*év", r"(\d+)\s*év(?:es)?\s*(?:gyártói\s*)?garanci", r"jótállási idő:\s*(\d+)\s*év",
            r"garancia:\s*(\d+)\s*hónap"]
    for t in texts:
        t = clean_text(t).lower()
        for i, p in enumerate(pats):
            m = re.search(p, t)
            if m:
                n = int(m.group(1))
                return n if i == 3 else n * 12
    return None


def parse_parameters(text: str) -> list[tuple[str, str]]:
    out = []
    for line in clean_text(text).splitlines():
        if ":" not in line:
            continue
        k, v = (x.strip(" –-") for x in line.split(":", 1))
        if k and v and len(k) <= 50 and len(v) <= 120:
            out.append((k[0].upper() + k[1:], v))
    return out


def auto_name(p: dict) -> str:
    """Tartalék névképzés új termékekhez (a riport jelzi, ha ezt használtuk)."""
    raw = p["productDescriptions"][0]["name"]
    base = re.split(r",| - ", raw)[0]
    base = re.sub(r"\b(prémium|felső kategóriás|exkluzív|kényelmes|kiváló[^,]*)\b", "", base, flags=re.I)
    words = []
    for w in base.split():
        if w.isupper() and len(w) > 3 and not re.search(r"\d", w):
            w = w.capitalize()
        words.append(w)
    name = re.sub(r"\s+", " ", " ".join(words)).strip()
    return name[0].upper() + name[1:]


# ------------------------------------------------------------------ fő logika
class Builder:
    def __init__(self, products, categories, names, image_check=None):
        self.products = products
        self.cat_inner = {c["id"]: c["innerId"] for c in categories}
        self.names = names
        self.image_check = image_check or {}
        self.skipped: list[tuple[str, str, str]] = []
        self.warnings: list[str] = []
        self.auto_named: list[tuple[str, str]] = []
        # Shoprenter gyártó-ID -> leggyakoribb "gyarto" tulajdonság (pl. 34 -> ANTARES)
        votes = defaultdict(Counter)
        for p in products:
            g = self._attr(p, "gyarto")
            mid = inner((p.get("manufacturer") or {}).get("id"))
            if g and mid:
                votes[mid][g] += 1
        self.manu_by_id = {k: v.most_common(1)[0][0] for k, v in votes.items()}

    @staticmethod
    def _attr(p, name):
        for a in p.get("productAttributeExtend") or []:
            if a.get("name") == name and a.get("value"):
                return ", ".join(v["value"] for v in a["value"] if v.get("value"))
        return ""

    def prefetch_images(self, products):
        """Élő HTTP-ellenőrzés minden szóba jövő képre (webp esetén a jpg/png párjára is)."""
        paths = set()
        for p in products:
            for path in self._image_paths(p):
                if path not in self.image_check:
                    paths.add(path)
                if path.lower().endswith(".webp"):
                    paths.update(path[:-4] + e for e in ("jpg", "jpeg", "png"))
        paths -= set(self.image_check)

        def head(path):
            return path, probe_image(IMG_BASE + quote(path))
        with ThreadPoolExecutor(16) as ex:
            self.image_check.update(dict(ex.map(head, sorted(paths))))

    def availability(self, p) -> str | None:
        if str(p.get("status")) != "1" or str(p.get("orderable")) != "1":
            return None
        if to_int(p.get("stock1")) > 0 or to_int(p.get("stock2")) > 0:
            return DELIVERY_IN_STOCK
        all_zero = all(to_int(p.get(f"stock{i}")) <= 0 for i in range(1, 5))
        if all_zero and inner((p.get("noStockStatus") or {}).get("id")) == STOCK_STATUS_3_5:
            return DELIVERY_3_5
        return None

    def image_ok(self, path: str) -> bool:
        v = self.image_check.get(path, "")
        return v.startswith("200") and "image/" in v and "webp" not in v

    @staticmethod
    def _image_paths(p) -> list[str]:
        paths = [(p.get("mainPicture") or "").strip()]
        for k, u in sorted((p.get("allImages") or {}).items(), key=lambda kv: int(re.sub(r"\D", "", kv[0]) or 0)):
            if k == "mainImage":
                continue
            m = re.search(r"/image/cache/[^/]+/(.+?)(\?|$)", u)
            if m:
                paths.append(m.group(1))
        return [x for x in paths if x and not x.endswith("/")]

    def images(self, p) -> list[str]:
        paths = self._image_paths(p)
        out = []
        for path in paths:
            path = path.strip()
            if not path or path.endswith("/"):
                continue
            if path.lower().endswith(".webp"):  # Árukereső: csak jpg/gif/bmp/png
                alt = next((path[:-4] + e for e in ("jpg", "jpeg", "png") if self.image_ok(path[:-4] + e)), None)
                if not alt:
                    continue
                path = alt
            elif not self.image_ok(path):
                continue
            url = IMG_BASE + quote(path)
            if url not in out:
                out.append(url)
        return out

    def category(self, cats: set[str]) -> str:
        for cid, name in CATEGORY_PRIORITY:
            if cid in cats:
                return f"{CATEGORY_ROOT} > {name}"
        return CATEGORY_ROOT

    def build(self):
        rows = []
        for p in self.products:
            pid = str(p.get("innerId") or inner(p["id"]))
            desc = (p.get("productDescriptions") or [{}])[0]
            raw_name = desc.get("name", "")

            def skip(why):
                self.skipped.append((pid, why, raw_name[:90]))

            delivery = self.availability(p)
            if not delivery:
                continue  # nem raktári / nem 3-5 napos -> csendben kimarad (nem hiba)
            cats = {self.cat_inner.get((r.get("category") or {}).get("id")) for r in p.get("productCategoryRelations") or []}
            cats.discard(None)
            if cats & EXCLUDED_CATEGORIES:
                skip("kizárt kategória (alkatrész/kiegészítő/asztal/csomag/outlet)")
                continue
            if not cats:
                skip("nincs kategória")
                continue

            prices = p.get("productPrices") or []
            pr = next((x for x in prices if (x.get("customerGroup") or {}).get("default")), prices[0] if prices else None)
            if not pr:
                skip("nincs ár")
                continue
            gross = to_int(pr.get("grossSpecial") or pr.get("gross"))
            net = to_int(pr.get("netSpecial") or pr.get("net"))
            min_qty = max(1, to_int(p.get("minimalOrderNumber")))
            if gross <= 0:
                skip("nincs ár")
                continue

            images = self.images(p)
            if not images:
                if any(x.lower().endswith(".webp") for x in self._image_paths(p)):
                    skip("csak webp kép van (az Árukereső nem fogadja el) – jpg kell ugyanazon a néven")
                else:
                    skip("nincs termékkép")
                continue

            alias = next((a.get("urlAlias") for a in p.get("urlAliases") or [] if a.get("urlAlias")), "")
            if not alias:
                skip("nincs termék URL")
                continue

            attrs_raw = {a["name"]: ", ".join(v["value"] for v in a.get("value", []) if v.get("value"))
                         for a in p.get("productAttributeExtend") or [] if a.get("value")}
            manufacturer = attrs_raw.get("gyarto") or self.manu_by_id.get(inner((p.get("manufacturer") or {}).get("id")), "")
            if not attrs_raw.get("gyarto") and manufacturer:
                self.warnings.append(f"{pid}: a 'Gyártó' tulajdonság üres – a Shoprenter gyártó-hozzárendelésből: {manufacturer}")
            if not manufacturer:
                skip("nincs gyártó (az Árukereső kötelező mezője)")
                continue
            manufacturer = BRAND_NAMES.get(manufacturer.upper(), manufacturer)

            name = self.names.get(pid)
            if not name:
                name = auto_name(p)
                color = attrs_raw.get("szin")
                if color and color.lower() not in name.lower():
                    name += f", {color}"
                self.auto_named.append((pid, name))
            if min_qty > 1:
                name += f" ({min_qty} db)"

            attributes = []
            for k, v in attrs_raw.items():
                if k in SKIP_ATTRS:
                    continue
                attributes.append((ATTR_LABELS.get(k, k.capitalize()), v))
            have = {a[0].lower() for a in attributes}
            for k, v in parse_parameters(desc.get("parameters", "")):
                if k.lower() not in have and not k.lower().startswith(("jótállás", "garancia")):
                    attributes.append((k, v))
                    have.add(k.lower())

            description = restore_bullets(clean_text(desc.get("description")))
            if not description:
                description = clean_text(desc.get("shortDescription")) or raw_name
            if len(description) > 3000:
                description = description[:3000].rsplit("\n", 1)[0]

            ean = (p.get("gtin") or "").strip()
            if ean and not ean_valid(ean):
                self.warnings.append(f"{pid}: EAN '{ean}' nem hivatalos/érvénytelen – kihagyva")
                ean = ""

            warranty = warranty_months(raw_name, desc.get("parameters", ""), desc.get("description", ""))
            weight = float(p.get("weight") or 0)

            rows.append({
                "pid": pid,
                "group": inner((p.get("parentProduct") or {}).get("id")) or pid,
                "manufacturer": manufacturer,
                "name": name,
                "url": f"{SHOP_URL}/{alias}",
                "price": gross * min_qty,
                "net_price": net * min_qty,
                "images": images[:20],
                "category": self.category(cats),
                "description": description,
                "delivery": delivery,
                "free": str(p.get("freeShipping")) == "1",
                "ean": ean,
                "warranty": warranty,
                "weight": weight if 0 < weight < 500 else None,
                "attributes": attributes,
                "min_qty": min_qty,
                "main_img": images[0],
            })

        # GroupId csak ott, ahol több változat van ugyanabból a szülőből
        sizes = Counter(r["group"] for r in rows)
        for r in rows:
            r["group_out"] = r["group"] if sizes[r["group"]] > 1 else ""
        names = Counter(r["name"] for r in rows)
        for n, c in names.items():
            if c > 1:
                self.warnings.append(f"Ismétlődő terméknév ({c}x): {n}")
        imgs = Counter(r["main_img"] for r in rows)
        for r in rows:
            if imgs[r["main_img"]] > 1:
                self.warnings.append(f"{r['pid']}: a főkép más színváltozatnál is szerepel ({r['name']})")
        return rows


def to_xml(rows) -> bytes:
    root = ET.Element("products")

    def sub(parent, tag, val):
        if val in (None, ""):
            return
        ET.SubElement(parent, tag).text = str(val)

    for r in rows:
        e = ET.SubElement(root, "product")
        sub(e, "identifier", r["pid"])
        sub(e, "manufacturer", r["manufacturer"])
        sub(e, "name", r["name"])
        sub(e, "product_url", r["url"])
        sub(e, "price", r["price"])
        sub(e, "net_price", r["net_price"])
        sub(e, "currency", "HUF")
        sub(e, "image_url", r["images"][0])
        for i, img in enumerate(r["images"][1:], start=2):
            sub(e, f"image_url_{i}", img)
        sub(e, "category", r["category"])
        sub(e, "description", r["description"])
        sub(e, "Delivery_Time", r["delivery"])
        sub(e, "Delivery_Cost", "FREE" if r["free"] else "")
        sub(e, "EAN_code", r["ean"])
        sub(e, "GroupId", r["group_out"])
        sub(e, "warranty", r["warranty"])
        sub(e, "FreeDelivery", 1 if r["free"] else 0)
        if r["weight"]:
            sub(e, "ProductWeight", f"{r['weight']:g} kg")
        if r["attributes"]:
            a = ET.SubElement(e, "Attributes")
            for k, v in r["attributes"]:
                at = ET.SubElement(a, "Attribute")
                sub(at, "Attribute_name", k)
                sub(at, "Attribute_value", v)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def check_urls(rows) -> list[str]:
    def chk(r):
        res = subprocess.run(["curl", "-s", "-L", "-m", "30", "-o", "-", "-w", "\n%{http_code}", r["url"]],
                             capture_output=True, text=True)
        body, _, code = res.stdout.rpartition("\n")
        if code != "200":
            return f"{r['pid']}: termékoldal HTTP {code} – {r['url']}"
        unit = r["price"] // r["min_qty"]
        shown = f"{unit:,}".replace(",", " ")
        if shown not in body.replace("\xa0", " ").replace("&nbsp;", " ") and str(unit) not in body:
            return f"{r['pid']}: az ár ({shown} Ft) nem található a termékoldalon – {r['url']}"
        return None

    with ThreadPoolExecutor(8) as ex:
        return [x for x in ex.map(chk, rows) if x]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(HERE / "raw"))
    ap.add_argument("--out", default=str(HERE / "output"))
    ap.add_argument("--names", default=str(HERE / "arukereso_nevek.json"))
    ap.add_argument("--check-urls", action="store_true", help="termékoldalak és árak ellenőrzése (lassabb)")
    ap.add_argument("--cached-images", action="store_true", help="korábbi képellenőrzés újrahasznosítása")
    a = ap.parse_args()
    raw, out = Path(a.raw), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    products = json.loads((raw / "products.json").read_text(encoding="utf-8"))
    categories = json.loads((raw / "categories.json").read_text(encoding="utf-8"))
    names = {k: v for k, v in json.loads(Path(a.names).read_text(encoding="utf-8")).items() if not k.startswith("_")}
    ic = raw / "image_check.json"
    b = Builder(products, categories, names, json.loads(ic.read_text(encoding="utf-8")) if ic.exists() and a.cached_images else {})
    candidates = [p for p in products if b.availability(p)]
    b.prefetch_images(candidates)
    ic.write_text(json.dumps(b.image_check, ensure_ascii=False), encoding="utf-8")
    rows = b.build()
    url_issues = check_urls(rows) if a.check_urls else []

    xml = to_xml(rows)
    ET.fromstring(xml)  # jól formázottság ellenőrzése
    tmp = out / "arukereso.xml.tmp"
    tmp.write_bytes(xml)
    tmp.replace(out / "arukereso.xml")

    in_stock = sum(1 for r in rows if r["delivery"] == DELIVERY_IN_STOCK)
    rep = [f"Árukereső feed – {time.strftime('%Y-%m-%d %H:%M')}",
           f"Feedben: {len(rows)} termék  (raktáron: {in_stock}, 3-5 munkanap: {len(rows) - in_stock})", "",
           "Kimaradt (raktári/3-5 napos, de nem felel meg):"] + \
          [f"  {i}\t{w}\t{n}" for i, w, n in b.skipped] + ["", "Automatikus név (érdemes átnézni, majd az arukereso_nevek.json-ba írni):"] + \
          [f"  {i}\t{n}" for i, n in b.auto_named] + ["", "Figyelmeztetések:"] + [f"  {w}" for w in b.warnings] + \
          ["", "URL/ár ellenőrzés:"] + ([f"  {x}" for x in url_issues] or ["  rendben" if a.check_urls else "  nem futott"])
    (out / "arukereso_riport.txt").write_text("\n".join(rep), encoding="utf-8")
    print("\n".join(rep[:2]))
    print(f"Kimaradt: {len(b.skipped)}, auto név: {len(b.auto_named)}, figyelmeztetés: {len(b.warnings)}, URL hiba: {len(url_issues)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
