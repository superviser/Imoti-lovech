"""Scraper за всички обяви за продажби в град Ловеч от imot.bg."""
import argparse
import csv
import io
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Принтиране на кирилица на Windows конзола
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE = "https://www.imot.bg"
LISTING_URL = f"{BASE}/obiavi/prodazhbi/grad-lovech"
WORKERS = 25
TIMEOUT = 20
MAX_PAGES = 60  # таван за пагинацията (защита срещу безкраен цикъл)
EUR_TO_BGN = 1.95583

OUT_DIR = Path(__file__).parent
CSV_PATH = OUT_DIR / "listings_lovech.csv"
REPORT_PATH = OUT_DIR / "report.md"
FAILED_PATH = OUT_DIR / "failed.log"
DEFAULT_JSON_PATH = OUT_DIR / "public" / "data.json"
STALE_DAYS = 7  # листинги изчезнали от сайта се пазят толкова дни преди изтриване
SAFETY_DROP_RATIO = 0.5  # ако новият брой < 50% от стария → не пишем (защита от bad scrape)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

TYPE_SLUG_MAP = {
    "ednostaen-apartament": "Едностаен",
    "dvustaen-apartament": "Двустаен",
    "tristaen-apartament": "Тристаен",
    "chetiristaen-apartament": "Четиристаен",
    "mnogostaen-apartament": "Многостаен",
    "mezonet": "Мезонет",
    "atelie-tavan": "Ателие",
    "garsoniera": "Гарсониера",
    "kashta": "Къща",
    "etazh-ot-kashta": "Етаж от къща",
    "vila": "Вила",
    "partsel": "Парцел",
    "zemedelska-zemya": "Земеделска земя",
    "gora": "Гора",
    "ofis": "Офис",
    "magazin": "Магазин",
    "sklad": "Склад",
    "promishleno-pomeshtenie": "Промишлено помещение",
    "biznes-imot": "Бизнес имот",
    "garazh-parkomyasto": "Гараж",
    "hotel": "Хотел",
    "zavedenie": "Заведение",
    "staya": "Стая",
}

REGION_SLUG_MAP = {
    "tsentar": "Център",
    "shirok-tsentar": "Широк център",
    "shirok": "Широк център",
    "mladost": "Младост",
    "zdravets": "Здравец",
    "varosha": "Вароша",
    "dikisana": "Дикисана",
    "goznitsa": "Гозница",
    "promishlena-zona": "Промишлена зона",
    "promishlena": "Промишлена зона",
    "prodimchets": "Продимчец",
    "cherven-bryag": "Червен бряг",
    "cherven": "Червен бряг",
    "drastene": "Дръстене",
    "v-promishlena-zona": "В промишлена зона",
    "v": "В промишлена зона",
}

APARTMENT_TYPES = {
    "Едностаен", "Двустаен", "Тристаен", "Четиристаен",
    "Многостаен", "Мезонет", "Ателие", "Гарсониера", "Стая",
}
HOUSE_TYPES = {"Къща", "Етаж от къща", "Вила"}
LAND_TYPES = {"Парцел", "Земеделска земя", "Гора"}


def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch(session, url, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                if r.encoding is None or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding or "windows-1251"
                return r.text
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    return None


def collect_ad_urls(session):
    """Динамично минава през всички страници на пагинацията.

    Спира когато: страница върне 0 нови обяви (подминали сме последната),
    fetch фейлне, или достигнем MAX_PAGES (защита срещу безкраен цикъл).
    Така работи независимо дали обявите са на 8, 9, 10+ страници.
    """
    urls = []
    seen = set()
    pattern = re.compile(r"/obiava-([a-z0-9-]+?)(?=[\"'#?\s<])")
    page = 1
    while page <= MAX_PAGES:
        url = LISTING_URL if page == 1 else f"{LISTING_URL}/p-{page}"
        html = fetch(session, url)
        if not html:
            print(f"  page {page}: няма съдържание (край на пагинацията)")
            break
        found = 0
        for m in pattern.finditer(html):
            ad_id = m.group(1)
            # ID-то трябва да започва с буква + цифри (напр. 1a1773...)
            if not re.match(r"^[0-9][a-z][0-9]", ad_id):
                continue
            if ad_id in seen:
                continue
            seen.add(ad_id)
            urls.append(f"{BASE}/obiava-{ad_id}")
            found += 1
        print(f"  page {page}: +{found} нови (общо {len(urls)})")
        # 0 нови обяви → подминали сме последната страница (или дубликати)
        if found == 0:
            break
        page += 1
        time.sleep(0.5)
    if page > MAX_PAGES:
        print(f"  [!] достигнат лимит MAX_PAGES={MAX_PAGES}")
    return urls


# ---------- Парсване на индивидуална обява ----------

def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _to_num(raw):
    """Превръща '129 000' или '1,234.5' към float."""
    raw = raw.replace(" ", "").replace("\xa0", "")
    # Ако има само ','  → десетичен разделител; '1,234' → 1234
    if "," in raw and "." not in raw:
        if raw.count(",") == 1 and len(raw.split(",")[1]) <= 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    return float(raw)


def parse_title_fields(title):
    """От title pattern '- 112 кв.м / 129 000 €' извлича (area, eur, bgn)."""
    area = eur = bgn = None
    # Площ преди '/'
    m = re.search(r"[-–]\s*(\d+(?:[.,]\d+)?)\s*кв\.?\s*м", title)
    if m:
        try:
            area = _to_num(m.group(1))
        except ValueError:
            pass
    # Цена в EUR (€)
    m = re.search(r"([\d\s.,]+?)\s*€", title)
    if m:
        try:
            eur = _to_num(m.group(1).strip())
        except ValueError:
            pass
    # Цена в BGN (лв)
    m = re.search(r"([\d\s.,]+?)\s*лв", title)
    if m:
        try:
            bgn = _to_num(m.group(1).strip())
        except ValueError:
            pass
    if eur and not bgn:
        bgn = eur * EUR_TO_BGN
    if bgn and not eur:
        eur = bgn / EUR_TO_BGN
    return area, eur, bgn


def parse_price_fallback(text):
    """Резервен парсър на цена от пълен текст."""
    eur = bgn = None
    m = re.search(r"([\d\s.,]+?)\s*EUR\b", text)
    if m:
        try:
            eur = _to_num(m.group(1).strip())
        except ValueError:
            pass
    m = re.search(r"([\d\s.,]+?)\s*BGN\b", text)
    if m:
        try:
            bgn = _to_num(m.group(1).strip())
        except ValueError:
            pass
    if eur and not bgn:
        bgn = eur * EUR_TO_BGN
    if bgn and not eur:
        eur = bgn / EUR_TO_BGN
    return eur, bgn


def parse_area_fallback(text):
    """Резервен парсър на площ от пълен текст. Първи match с разумна стойност."""
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(?:m²|кв\.?\s*м|м²|m2|м2)", text):
        try:
            v = _to_num(m.group(1))
            if 10 <= v <= 100000:
                return v
        except ValueError:
            continue
    return None


def parse_floor(text):
    """Връща (floor, total_floors) или (None, None)."""
    m = re.search(r"Етаж[:\s]+(\d+)\s*(?:от|/)\s*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)[-\s]*ти?\s*от\s*(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"Етаж[:\s]+(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), None
    return None, None


def parse_year(text):
    """Връща година/период като (year_from, year_to)."""
    m = re.search(r"(\d{4})\s*[-–]\s*(\d{4})\s*г", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"Строителство[:\s]+(\d{4})", text, re.IGNORECASE)
    if m:
        y = int(m.group(1))
        return y, y
    m = re.search(r"\b(19\d{2}|20\d{2})\s*г\.?", text)
    if m:
        y = int(m.group(1))
        return y, y
    return None, None


def parse_construction(text):
    """Тухла / Панел / EPK / Гредоред."""
    low = text.lower()
    for key, label in [
        ("тухла", "Тухла"),
        ("панел", "Панел"),
        ("epk", "EPK"),
        ("пк", "ПК"),
        ("гредоред", "Гредоред"),
        ("масивна", "Масивна"),
    ]:
        if key in low:
            return label
    return None


def parse_type_and_region(url):
    """Извлича тип имот и район от URL slug. Връща (type, region)."""
    m = re.search(r"prodava-(.+?)-grad-lovech-(.+?)$", url)
    if not m:
        return None, None
    type_slug = m.group(1)
    region_slug = m.group(2)
    ptype = TYPE_SLUG_MAP.get(type_slug)
    region = REGION_SLUG_MAP.get(region_slug) or region_slug.replace("-", " ").title()
    return ptype, region


def parse_ad(session, url):
    html = fetch(session, url)
    if not html:
        return {"url": url, "error": "fetch_failed"}
    soup = BeautifulSoup(html, "html.parser")
    # Изчисти scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    full_text = _clean(soup.get_text(" "))
    title = _clean(soup.title.string if soup.title else "")
    h1 = soup.find("h1")
    h1_text = _clean(h1.get_text(" ") if h1 else "")

    # Title-ът има надежден формат: '- N кв.м / X €'
    area, eur, bgn = parse_title_fields(title)
    if not eur:
        eur, bgn = parse_price_fallback(full_text)
    if not area:
        area = parse_area_fallback(full_text)
    floor, total_floor = parse_floor(full_text)
    year_from, year_to = parse_year(full_text)
    construction = parse_construction(full_text)
    ptype, region = parse_type_and_region(url)

    ppm_eur = (eur / area) if eur and area else None

    # Извличане на ID от URL (напр. 1c177755021109178)
    id_match = re.search(r"/obiava-([a-z0-9]+)", url)
    ad_id = id_match.group(1) if id_match else None

    return {
        "id": ad_id,
        "url": url,
        "title": title[:200],
        "type": ptype,
        "region": region,
        "area_m2": area,
        "price_eur": round(eur, 2) if eur else None,
        "price_bgn": round(bgn, 2) if bgn else None,
        "price_per_m2_eur": round(ppm_eur, 2) if ppm_eur else None,
        "floor": floor,
        "total_floors": total_floor,
        "year_from": year_from,
        "year_to": year_to,
        "construction": construction,
        "error": None,
    }


# ---------- Анализ ----------

def analyse(rows):
    ok = [r for r in rows if not r.get("error")]
    failed = [r for r in rows if r.get("error")]

    def has_ppm(r):
        return r.get("price_per_m2_eur") and r.get("area_m2")

    apartments = [r for r in ok if has_ppm(r) and r.get("type") in APARTMENT_TYPES]
    houses = [r for r in ok if has_ppm(r) and r.get("type") in HOUSE_TYPES]
    lands = [r for r in ok if has_ppm(r) and r.get("type") in LAND_TYPES]
    other = [r for r in ok if has_ppm(r) and r.get("type") not in
             (APARTMENT_TYPES | HOUSE_TYPES | LAND_TYPES)]

    def stats(group, key="price_per_m2_eur"):
        vals = [r[key] for r in group if r.get(key)]
        if not vals:
            return None
        return {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 2),
            "median": round(statistics.median(vals), 2),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "stdev": round(statistics.stdev(vals), 2) if len(vals) > 1 else 0,
        }

    report = {
        "total_fetched": len(rows),
        "ok": len(ok),
        "failed": len(failed),
        "with_price_and_area": sum(1 for r in ok if has_ppm(r)),
        "apartments": stats(apartments),
        "houses": stats(houses),
        "lands": stats(lands),
        "other": stats(other),
        "by_type": {},
        "by_region": {},
        "by_period": {},
        "by_construction": {},
        "type_counts": Counter(r.get("type") or "Неизвестен" for r in ok),
        "region_counts": Counter(r.get("region") or "Неизвестен" for r in ok),
    }

    # По тип
    by_type = defaultdict(list)
    for r in ok:
        if has_ppm(r):
            by_type[r.get("type") or "Неизвестен"].append(r)
    for t, group in by_type.items():
        report["by_type"][t] = stats(group)

    # По район
    by_region = defaultdict(list)
    for r in apartments + houses:  # без парцели
        by_region[r.get("region") or "Неизвестен"].append(r)
    for reg, group in by_region.items():
        if len(group) >= 2:
            report["by_region"][reg] = stats(group)

    # По период на строителство
    def period_bucket(r):
        y = r.get("year_from")
        if not y:
            return "Неизвестен"
        if y < 1960:
            return "до 1960"
        if y < 1980:
            return "1960-1979"
        if y < 2000:
            return "1980-1999"
        if y < 2015:
            return "2000-2014"
        return "2015+"

    by_period = defaultdict(list)
    for r in apartments + houses:
        by_period[period_bucket(r)].append(r)
    for p, group in by_period.items():
        if group:
            report["by_period"][p] = stats(group)

    # По конструкция
    by_constr = defaultdict(list)
    for r in apartments:
        c = r.get("construction") or "Неизвестен"
        by_constr[c].append(r)
    for c, group in by_constr.items():
        if group:
            report["by_construction"][c] = stats(group)

    # Outliers
    apts_sorted = sorted(apartments, key=lambda r: r["price_per_m2_eur"])
    report["cheapest_apt"] = apts_sorted[:5]
    report["priciest_apt"] = apts_sorted[-5:][::-1]

    return report, ok, failed


def write_csv(rows):
    fields = [
        "url", "title", "type", "region", "area_m2",
        "price_eur", "price_bgn", "price_per_m2_eur",
        "floor", "total_floors", "year_from", "year_to",
        "construction", "error",
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def fmt_stats(s):
    if not s:
        return "_няма данни_"
    return (f"n={s['n']}, mean={s['mean']} €/m², median={s['median']} €/m², "
            f"min={s['min']}, max={s['max']}, σ={s['stdev']}")


def write_report(report):
    lines = []
    lines.append("# Анализ на 290 обяви за продажби – град Ловеч")
    lines.append("Източник: imot.bg/obiavi/prodazhbi/grad-lovech\n")

    lines.append("## 1. Общи статистики")
    lines.append(f"- Заявени обяви: **{report['total_fetched']}**")
    lines.append(f"- Успешно парснати: **{report['ok']}**")
    lines.append(f"- С грешка: **{report['failed']}**")
    lines.append(f"- С цена + квадратура: **{report['with_price_and_area']}**\n")

    lines.append("## 2. Средна цена на квадратен метър (EUR/m²)")
    lines.append(f"- **Апартаменти**: {fmt_stats(report['apartments'])}")
    lines.append(f"- **Къщи / етаж от къща / вила**: {fmt_stats(report['houses'])}")
    lines.append(f"- **Парцели / земя**: {fmt_stats(report['lands'])}")
    lines.append(f"- **Други (офис/магазин/гараж/...)**: {fmt_stats(report['other'])}\n")

    lines.append("## 3. По тип имот")
    lines.append("| Тип | n | mean €/m² | median €/m² | min | max |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for t, s in sorted(report["by_type"].items(), key=lambda x: -(x[1]["n"] if x[1] else 0)):
        if s:
            lines.append(f"| {t} | {s['n']} | {s['mean']} | {s['median']} | {s['min']} | {s['max']} |")
    lines.append("")

    lines.append("## 4. По район в Ловеч (≥2 обяви)")
    lines.append("| Район | n | mean €/m² | median €/m² | min | max |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for r, s in sorted(report["by_region"].items(), key=lambda x: -(x[1]["n"] if x[1] else 0)):
        if s:
            lines.append(f"| {r} | {s['n']} | {s['mean']} | {s['median']} | {s['min']} | {s['max']} |")
    lines.append("")

    lines.append("## 5. По период на строителство")
    lines.append("| Период | n | mean €/m² | median €/m² |")
    lines.append("|---|---:|---:|---:|")
    order = ["до 1960", "1960-1979", "1980-1999", "2000-2014", "2015+", "Неизвестен"]
    for p in order:
        s = report["by_period"].get(p)
        if s:
            lines.append(f"| {p} | {s['n']} | {s['mean']} | {s['median']} |")
    lines.append("")

    lines.append("## 6. По конструкция (апартаменти)")
    lines.append("| Конструкция | n | mean €/m² | median €/m² |")
    lines.append("|---|---:|---:|---:|")
    for c, s in sorted(report["by_construction"].items(), key=lambda x: -(x[1]["n"] if x[1] else 0)):
        if s:
            lines.append(f"| {c} | {s['n']} | {s['mean']} | {s['median']} |")
    lines.append("")

    lines.append("## 7. Outliers")
    lines.append("### Най-евтини апартаменти €/m²")
    for r in report["cheapest_apt"]:
        lines.append(f"- {r['price_per_m2_eur']} €/m² – {r['type']}, "
                     f"{r['area_m2']} m², {r['price_eur']} € | {r['url']}")
    lines.append("\n### Най-скъпи апартаменти €/m²")
    for r in report["priciest_apt"]:
        lines.append(f"- {r['price_per_m2_eur']} €/m² – {r['type']}, "
                     f"{r['area_m2']} m², {r['price_eur']} € | {r['url']}")
    lines.append("")

    lines.append("## 8. Разпределение на типове обяви")
    for t, n in report["type_counts"].most_common():
        lines.append(f"- {t}: {n}")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ---------- JSON output + merge с предишен run ----------

def _utc_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def merge_with_existing(new_rows, existing_json_path, now_iso):
    """Merge с предишен data.json:
    - запазва `first_seen` за вече видяни ID
    - новите → first_seen = now
    - изчезналите → still_active=false, изтриват се след STALE_DAYS
    """
    old_by_id = {}
    if existing_json_path.exists():
        try:
            with open(existing_json_path, encoding="utf-8") as f:
                old_data = json.load(f)
            for r in old_data.get("listings", []):
                if r.get("id"):
                    old_by_id[r["id"]] = r
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [warn] не мога да чета {existing_json_path}: {e}")

    merged = []
    current_ids = set()
    new_count = 0
    for r in new_rows:
        if r.get("error") or not r.get("id"):
            continue
        ad_id = r["id"]
        current_ids.add(ad_id)
        old = old_by_id.get(ad_id)
        first_seen = old["first_seen"] if old and old.get("first_seen") else now_iso
        if not old or not old.get("first_seen"):
            new_count += 1
        merged.append({
            **r,
            "first_seen": first_seen,
            "last_seen": now_iso,
            "still_active": True,
        })

    # Запазване на изчезнали, които не са по-стари от STALE_DAYS
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    kept_stale = 0
    for ad_id, old in old_by_id.items():
        if ad_id in current_ids:
            continue
        last_seen_str = old.get("last_seen") or old.get("first_seen")
        if not last_seen_str:
            continue
        try:
            last_seen_dt = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if last_seen_dt < stale_cutoff:
            continue
        merged.append({**old, "still_active": False})
        kept_stale += 1

    return merged, new_count, kept_stale


def build_top_stats(merged_active, report):
    return {
        "total": len(merged_active),
        "with_price_and_area": report.get("with_price_and_area"),
        "apartments": report.get("apartments"),
        "houses": report.get("houses"),
        "lands": report.get("lands"),
    }


def content_signature(listings):
    """Нормализиран отпечатък на обявите БЕЗ нестабилните полета.

    Игнорира `last_seen` (мени се всеки run) и сортира по ID + ключове,
    за да е независим от реда на сваляне. Така две идентични по същество
    свалки дават еднакъв отпечатък → не презаписваме файла напразно.
    """
    norm = []
    for r in sorted(listings, key=lambda x: x.get("id") or ""):
        norm.append({k: r[k] for k in sorted(r) if k != "last_seen"})
    return json.dumps(norm, ensure_ascii=False, sort_keys=True)


def read_existing_signature(json_path):
    if not json_path.exists():
        return None
    try:
        with open(json_path, encoding="utf-8") as f:
            old = json.load(f)
        return content_signature(old.get("listings", []))
    except (json.JSONDecodeError, OSError):
        return None


def write_json(merged, report, json_path, now_iso):
    json_path.parent.mkdir(parents=True, exist_ok=True)
    active = [r for r in merged if r.get("still_active")]
    payload = {
        "generated_at": now_iso,
        "city": "Ловеч",
        "source": LISTING_URL,
        "eur_to_bgn": EUR_TO_BGN,
        "stats": build_top_stats(active, report),
        "listings": merged,  # вече сортирани по ID
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def main():
    parser = argparse.ArgumentParser(description="imot.bg Ловеч scraper")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_PATH,
                        help="Път за изходния data.json (default: public/data.json)")
    parser.add_argument("--no-csv", action="store_true",
                        help="Не пиши CSV/report.md (само JSON)")
    args = parser.parse_args()

    print(f"== imot.bg Ловеч scraper ({WORKERS} workera) ==\n")
    session = get_session()

    print("[1/3] Събиране на URL-и от пагинацията...")
    urls = collect_ad_urls(session)
    print(f"  -> общо уникални: {len(urls)}\n")

    print(f"[2/3] Паралелно сваляне с {WORKERS} workera...")
    rows = []
    start = time.time()
    sessions = [get_session() for _ in range(WORKERS)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(parse_ad, sessions[i % WORKERS], u): u
            for i, u in enumerate(urls)
        }
        done = 0
        for fut in as_completed(futures):
            try:
                rows.append(fut.result())
            except Exception as e:
                rows.append({"url": futures[fut], "error": f"exception:{e}"})
            done += 1
            if done % 25 == 0:
                print(f"  ... {done}/{len(urls)} ({time.time()-start:.1f}s)")
    print(f"  край за {time.time()-start:.1f}s\n")

    print("[3/3] Анализ + запис...")
    report, ok, failed = analyse(rows)
    if not args.no_csv:
        write_csv(rows)
        write_report(report)
    if failed:
        FAILED_PATH.write_text(
            "\n".join(f"{r['url']}\t{r.get('error')}" for r in failed),
            encoding="utf-8",
        )

    # JSON output с merge за first_seen + safety check
    now_iso = _utc_iso()
    ok_with_id = [r for r in ok if r.get("id")]
    json_path = args.output

    # Safety: ако новият брой пада драстично, не пишем (вероятен fail на imot.bg)
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                old_count = len([r for r in json.load(f).get("listings", []) if r.get("still_active")])
            if old_count > 10 and len(ok_with_id) < old_count * SAFETY_DROP_RATIO:
                print(f"\n[!] SAFETY ABORT: нови {len(ok_with_id)} < {SAFETY_DROP_RATIO*100:.0f}% от старите {old_count}")
                print("    data.json НЕ е презаписан.")
                sys.exit(2)
        except (json.JSONDecodeError, OSError):
            pass

    merged, new_count, stale_kept = merge_with_existing(ok_with_id, json_path, now_iso)
    merged.sort(key=lambda r: r.get("id") or "")  # стабилен ред за чисти diff-ове

    # Записваме само ако има реална промяна в обявите (спестява Cloudflare build-ове)
    old_sig = read_existing_signature(json_path)
    new_sig = content_signature(merged)
    if old_sig is not None and new_sig == old_sig:
        print(f"\n  Няма промяна в обявите — data.json НЕ е презаписан (спестен build).")
        print(f"  Активни: {len([r for r in merged if r.get('still_active')])}")
    else:
        write_json(merged, report, json_path, now_iso)
        print(f"\n  JSON презаписан: {json_path}")
        print(f"  Активни: {len([r for r in merged if r.get('still_active')])}, "
              f"новопоявили се: {new_count}, неактивни (запазени): {stale_kept}")

    # Резюме в конзолата
    print("\n" + "=" * 60)
    print("РЕЗЮМЕ")
    print("=" * 60)
    print(f"Успешни: {report['ok']}/{report['total_fetched']}  |  "
          f"С цена+площ: {report['with_price_and_area']}")
    if report["apartments"]:
        a = report["apartments"]
        print(f"\nАпартаменти ({a['n']}):")
        print(f"  Средна цена/m²: {a['mean']} €  (≈ {a['mean']*EUR_TO_BGN:.0f} лв)")
        print(f"  Медиана:        {a['median']} €  (≈ {a['median']*EUR_TO_BGN:.0f} лв)")
        print(f"  Диапазон:       {a['min']} – {a['max']} €/m²")
    if report["houses"]:
        h = report["houses"]
        print(f"\nКъщи/вили ({h['n']}):")
        print(f"  Средна цена/m²: {h['mean']} €  |  медиана: {h['median']} €")
    if report["lands"]:
        l = report["lands"]
        print(f"\nПарцели ({l['n']}):")
        print(f"  Средна цена/m²: {l['mean']} €  |  медиана: {l['median']} €")
    print(f"\nФайлове:")
    if not args.no_csv:
        print(f"  {CSV_PATH}")
        print(f"  {REPORT_PATH}")
    print(f"  {json_path}")
    if failed:
        print(f"  {FAILED_PATH}  ({len(failed)} грешки)")


if __name__ == "__main__":
    main()
