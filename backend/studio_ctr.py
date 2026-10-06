"""CTR-доска Studio: свои карточки против полочных конкурентов из топ-50.

Цифры — из еженедельных файлов «Сравнение карточек».
В зачёт попадает конкурент, у которого есть заказы, он в топ-50
хотя бы по одному боевому ключу и связан полкой «Смотрите также».
Свои карточки: от 3 000 показов в день. Конкурент в зачёте: больше 5 000 в день.
Ниже своего порога — «мало данных», в победу и проигрыш не идёт.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request

from studio_auth import studio_user_from_request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/studio", tags=["studio-ctr"])

OWN_VIEWS_PER_DAY = 3000
RIVAL_VIEWS_PER_DAY = 5000
DEST_MOSCOW = -1257786
SERP_TTL_SEC = 6 * 3600
SERP_CACHE_KEY = "studio_serp_cache"
REVERSE_SHELF_CAP = 8
PRICE_BAND_RUB = 400

# Сначала более длинные семьи, чтобы «серебристый» не склеился с «серый».
_COLOR_FAMILIES = (
    ("серебристый", ("серебр", "silver")),
    ("золотой", ("золот", "gold")),
    ("черный", ("черн", "black")),
    ("розовый", ("розов", "pink")),
    ("голубой", ("голуб",)),
    ("синий", ("синий", "синяя", "синие", "blue")),
    ("зеленый", ("зелен", "green")),
    ("красный", ("красн", "red")),
    ("бордовый", ("бордов",)),
    ("фиолетовый", ("фиолет", "сирен", "purple")),
    ("бежевый", ("беж",)),
    ("коричневый", ("корич", "brown")),
    ("белый", ("белый", "белая", "белые", "white")),
    ("серый", ("серый", "серая", "серые", "gray", "grey")),
    ("оранжевый", ("оранж",)),
    ("желтый", ("желт", "yellow")),
)

# «водонипроницаемые» — написание, которое вводят покупатели.
QUERIES = (
    "смарт-часы",
    "смарт часы женские",
    "смарт часы женские круглые",
    "смарт часы для андроид",
    "умные часы",
    "умные женские часы",
    "смарт часы мужские",
    "смарт часы водонепроницаемые",
)


def _sb():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    return url, headers


def _user(request: Request) -> dict:
    user = studio_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Нужен вход")
    return user


def _days(value) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 7
    if days not in (7, 14, 30):
        raise HTTPException(status_code=400, detail="Период: 7, 14 или 30 дней")
    return days


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _num(value) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sb_get(path: str) -> list:
    url, headers = _sb()
    if not url:
        return []
    rows = []
    offset = 0
    while offset <= 20000:
        join = "&" if "?" in path else "?"
        try:
            resp = httpx.get(
                f"{url}/rest/v1/{path}{join}limit=1000&offset={offset}",
                headers=headers,
                timeout=25,
            )
        except Exception as e:
            logger.warning(f"studio ctr get {path}: {e}")
            break
        if not resp.is_success:
            logger.warning(f"studio ctr get {path}: {resp.status_code} {resp.text[:180]}")
            break
        batch = resp.json() or []
        if not isinstance(batch, list):
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def pick_sessions(sessions: list, days: int, today: date | None = None) -> list:
    """Берём недельные файлы внутри окна, без двойного подсчёта пересекающихся дат."""
    today = today or date.today()
    window_start = today - timedelta(days=days - 1)
    best = {}
    for raw in sessions or []:
        begin = _parse_date(raw.get("period_begin"))
        end = _parse_date(raw.get("period_end"))
        sid = _as_int(raw.get("id"))
        if begin is None or end is None or sid is None:
            continue
        if end < begin:
            begin, end = end, begin
        if end < window_start or begin > today:
            continue
        key = (begin.isoformat(), end.isoformat())
        prev = best.get(key)
        if prev is None or sid > prev["id"]:
            best[key] = {"id": sid, "period_begin": begin.isoformat(), "period_end": end.isoformat()}
    def span_of(sess) -> int:
        begin = _parse_date(sess["period_begin"])
        end = _parse_date(sess["period_end"])
        return (end - begin).days + 1

    weekly = [s for s in best.values() if 5 <= span_of(s) <= 10]
    pool = weekly or list(best.values())
    ordered = sorted(pool, key=lambda s: (s["period_end"], s["id"]), reverse=True)
    if days == 7:
        return [ordered[0]] if ordered else []
    chosen = []
    covered = []
    for sess in ordered:
        begin = _parse_date(sess["period_begin"])
        end = _parse_date(sess["period_end"])
        overlaps = any(not (end < c0 or begin > c1) for c0, c1 in covered)
        if overlaps:
            continue
        chosen.append(sess)
        covered.append((begin, end))
    chosen.sort(key=lambda s: s["period_begin"])
    return chosen


def _opens_of(row: dict) -> float:
    opens = row.get("card_opens")
    if opens is not None and opens != "":
        return _num(opens)
    views = _num(row.get("views"))
    ctr = row.get("ctr")
    if views and ctr is not None and ctr != "":
        return _num(ctr) / 100.0 * views
    return 0.0


def aggregate_metrics(rows: list) -> dict:
    by_nm = {}
    for row in rows or []:
        nm = _as_int(row.get("nm_id"))
        if nm is None:
            continue
        bucket = by_nm.setdefault(nm, {
            "nm_id": nm,
            "views": 0.0,
            "card_opens": 0.0,
            "orders": 0.0,
            "name": "",
            "brand": "",
        })
        bucket["views"] += _num(row.get("views"))
        bucket["card_opens"] += _opens_of(row)
        bucket["orders"] += _num(row.get("orders"))
        name = (row.get("name") or "").strip()
        brand = (row.get("brand") or "").strip()
        if name:
            bucket["name"] = name
        if brand:
            bucket["brand"] = brand
    for bucket in by_nm.values():
        views = bucket["views"]
        opens = bucket["card_opens"]
        bucket["views"] = int(round(views))
        bucket["card_opens"] = int(round(opens))
        bucket["orders"] = int(round(bucket["orders"]))
        bucket["ctr"] = round(opens / views * 100, 2) if views else None
        bucket["url"] = f"https://www.wildberries.ru/catalog/{bucket['nm_id']}/detail.aspx"
    return by_nm


def _own_catalog() -> dict:
    """nm_id → vendor_code по остаткам и поставкам кабинета."""
    own = {}

    def take(nm, vendor):
        nm = _as_int(nm)
        if nm is None:
            return
        vendor = (vendor or "").strip()
        if vendor == str(nm):
            vendor = ""
        prev = own.get(nm) or ""
        if vendor and (not prev or prev == str(nm)):
            own[nm] = vendor
        elif nm not in own:
            own[nm] = prev

    for row in _sb_get("supply_report?select=nm_id,vendor_code"):
        take(row.get("nm_id"), row.get("vendor_code"))
    for row in _sb_get("stock_totals?select=nm_id,vendor_code"):
        take(row.get("nm_id"), row.get("vendor_code"))
    for row in _sb_get("ratings_official?select=nm_id,article"):
        take(row.get("nm_id"), row.get("article"))
    return own


def covered_days(sessions: list, window_days: int) -> int:
    """Сколько календарных дней реально закрыто файлами сравнения."""
    total = 0
    for sess in sessions or []:
        begin = _parse_date(sess.get("period_begin"))
        end = _parse_date(sess.get("period_end"))
        if not begin or not end:
            continue
        if end < begin:
            begin, end = end, begin
        total += (end - begin).days + 1
    return total if total > 0 else max(int(window_days or 1), 1)


def _views_ok(views: int, span_days: int, is_own: bool) -> bool:
    span = max(int(span_days or 1), 1)
    per_day = (views or 0) / span
    if is_own:
        return per_day >= OWN_VIEWS_PER_DAY
    return per_day > RIVAL_VIEWS_PER_DAY


def _card_public(bucket: dict, vendor_code: str = "", span_days: int = 1, is_own: bool = True) -> dict:
    out = dict(bucket)
    out["vendor_code"] = vendor_code or ""
    out["reliable"] = _views_ok(int(out.get("views") or 0), span_days, is_own)
    out["low_data"] = not out["reliable"]
    if out.get("views", 0) <= 0 and out.get("ctr") is None:
        out["status"] = "нет в сравнении"
    elif out["low_data"]:
        out["status"] = "мало данных"
    else:
        out["status"] = "можно сравнивать"
    return out


_MONTHS_RU = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _overlap_days(a0: date, a1: date, b0: date, b1: date) -> int:
    start = max(a0, b0)
    end = min(a1, b1)
    if end < start:
        return 0
    return (end - start).days + 1


def _period_label(start: date, end: date) -> str:
    if start.month == end.month and start.year == end.year:
        return f"{start.day}–{end.day} {_MONTHS_RU[start.month]} {start.year}"
    return (
        f"{start.day} {_MONTHS_RU[start.month]} {start.year} – "
        f"{end.day} {_MONTHS_RU[end.month]} {end.year}"
    )


def pick_previous_month(sessions: list, today: date | None = None) -> tuple[list, int, str]:
    """Прошлый календарный месяц. Неделя берётся, если больше половины дней внутри месяца.
    Файл добавляем, если новых дней больше, чем повторно посчитанных на стыке."""
    today = today or date.today()
    month_end = today.replace(day=1) - timedelta(days=1)
    month_start = month_end.replace(day=1)
    groups = {}
    for raw in sessions or []:
        begin = _parse_date(raw.get("period_begin"))
        end = _parse_date(raw.get("period_end"))
        sid = _as_int(raw.get("id"))
        if begin is None or end is None or sid is None:
            continue
        if end < begin:
            begin, end = end, begin
        span = (end - begin).days + 1
        inside = _overlap_days(begin, end, month_start, month_end)
        if inside == 0 or inside < span - inside:
            continue
        key = (begin.isoformat(), end.isoformat())
        bucket = groups.setdefault(key, {
            "period_begin": begin.isoformat(),
            "period_end": end.isoformat(),
            "begin": begin,
            "end": end,
            "ids": [],
        })
        bucket["ids"].append(sid)
    ordered = sorted(groups.values(), key=lambda item: (item["end"], max(item["ids"])), reverse=True)
    chosen = []
    for item in ordered:
        span = (item["end"] - item["begin"]).days + 1
        overlap = sum(_overlap_days(item["begin"], item["end"], c["begin"], c["end"]) for c in chosen)
        added = span - overlap
        if added > overlap:
            chosen.append(item)
    covered = set()
    for item in chosen:
        day = item["begin"]
        while day <= item["end"]:
            if month_start <= day <= month_end:
                covered.add(day)
            day += timedelta(days=1)
    if covered:
        label = _period_label(min(covered), max(covered))
        span_days = len(covered)
    else:
        label = _period_label(month_start, month_end)
        span_days = (month_end - month_start).days + 1
    out = []
    for item in sorted(chosen, key=lambda row: row["begin"]):
        out.append({
            "id": max(item["ids"]),
            "period_begin": item["period_begin"],
            "period_end": item["period_end"],
            "session_ids": sorted(item["ids"]),
        })
    return out, span_days, label


def _dedupe_week_metrics(rows: list, session_period: dict) -> list:
    """В одной неделе несколько файлов. Артикул берём один раз, из более поздней загрузки."""
    best = {}
    for row in rows or []:
        sid = _as_int(row.get("session_id"))
        nm = _as_int(row.get("nm_id"))
        period = session_period.get(sid)
        if sid is None or nm is None or not period:
            continue
        key = (period, nm)
        prev = best.get(key)
        if prev is None or sid > prev[0]:
            best[key] = (sid, row)
    return [item[1] for item in best.values()]


def load_board(days: int) -> dict:
    sessions = _sb_get("competitor_sessions?select=id,period_begin,period_end,uploaded_at&order=id.desc")
    period_label = ""
    if days == 30:
        chosen, span, period_label = pick_previous_month(sessions)
    else:
        chosen = pick_sessions(sessions, days)
        span = covered_days(chosen, days)
    metrics = []
    if chosen:
        id_list = []
        session_period = {}
        for sess in chosen:
            period = (sess.get("period_begin"), sess.get("period_end"))
            ids = sess.get("session_ids") or [sess.get("id")]
            for sid in ids:
                sid = _as_int(sid)
                if sid is None:
                    continue
                id_list.append(sid)
                session_period[sid] = period
        if id_list:
            ids = ",".join(str(sid) for sid in id_list)
            raw_metrics = _sb_get(
                f"competitor_metrics?session_id=in.({ids})&select=session_id,nm_id,name,brand,views,card_opens,ctr,orders"
            )
            metrics = _dedupe_week_metrics(raw_metrics, session_period) if days == 30 else raw_metrics
    by_nm = aggregate_metrics(metrics)
    catalog = _own_catalog()
    own_ids = set(catalog)
    # Карточка из файла, которую кабинет уже знает. Чужие nm в каталог не попадают.
    own_cards = []
    for nm, vendor in catalog.items():
        bucket = by_nm.get(nm) or {
            "nm_id": nm,
            "views": 0,
            "card_opens": 0,
            "orders": 0,
            "ctr": None,
            "name": "",
            "brand": "",
            "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        }
        own_cards.append(_card_public(bucket, vendor, span, True))
    own_cards.sort(key=lambda c: (
        0 if c.get("ctr") is not None else 1,
        -(c.get("views") or 0),
        (c.get("vendor_code") or "").lower(),
    ))
    return {
        "days": days,
        "period_label": period_label,
        "covered_days": span,
        "own_views_per_day": OWN_VIEWS_PER_DAY,
        "rival_views_per_day": RIVAL_VIEWS_PER_DAY,
        "own_min_views": OWN_VIEWS_PER_DAY * span,
        "rival_min_views": RIVAL_VIEWS_PER_DAY * span,
        "min_views": OWN_VIEWS_PER_DAY * span,
        "queries": list(QUERIES),
        "sessions": chosen,
        "own": own_cards,
        "by_nm": by_nm,
        "own_ids": own_ids,
        "catalog": catalog,
    }


def _verdict(own: dict, rival: dict, in_score: bool) -> str:
    if not in_score:
        return "вне зачёта"
    if not own.get("reliable") or not rival.get("reliable"):
        return "мало данных"
    own_ctr = own.get("ctr")
    rival_ctr = rival.get("ctr")
    if own_ctr is None or rival_ctr is None:
        return "мало данных"
    if abs(float(own_ctr) - float(rival_ctr)) < 0.05:
        return "наравне"
    if float(own_ctr) > float(rival_ctr):
        return "выше"
    return "ниже"


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def color_families(text: str) -> set:
    t = _norm(text)
    found = set()
    for family, keys in _COLOR_FAMILIES:
        if any(key in t for key in keys):
            found.add(family)
    return found


def _vendor_color(vendor: str) -> str:
    u = _norm(vendor)
    if "gold" in u or "золот" in u:
        return "золотой"
    if "silver" in u or "серебр" in u:
        return "серебристый"
    if "pink" in u or "розов" in u:
        return "розовый"
    if "black" in u or "черн" in u:
        return "черный"
    if "white" in u or ("бел" in u and "беж" not in u):
        return "белый"
    if "беж" in u:
        return "бежевый"
    if "сер" in u:
        return "серый"
    return ""


def classify_shape(
    name: str,
    gender: str = "",
    form: str = "",
    vendor: str = "",
    description: str = "",
    trust_line: bool = False,
) -> str:
    """Прямоугольные, круглые женские или круглые мужские. Пусто, если не разобрать."""
    blob = _norm(" ".join(
        part for part in (name, form, description, vendor if trust_line else "") if part
    ))
    g = _norm(gender)
    women = "жен" in g or "женск" in blob or "женщин" in blob
    men = "муж" in g or "мужск" in blob or "мужчин" in blob
    rectangular = "прямоуг" in blob or "квадрат" in blob
    form_l = _norm(form)
    roundish = "кругл" in blob or ("круг" in form_l and "вокруг" not in form_l)
    if trust_line and "zk" in _norm(vendor):
        roundish = True
    if trust_line and not roundish and any(
        word in blob for word in ("ultra", "ультра", "мини", "mini", "x10", "hw-w", "apple")
    ):
        rectangular = True
    if rectangular and not roundish:
        return "прямоугольные"
    if roundish and not rectangular:
        if women and not men:
            return "круглые женские"
        if men and not women:
            return "круглые мужские"
    return ""


def _query_for_shape(shape: str) -> str:
    if shape == "круглые женские":
        return "смарт часы женские круглые"
    if shape == "круглые мужские":
        return "смарт часы мужские"
    return "смарт часы"


def _option_texts(data: dict) -> dict:
    buckets = {"color": [], "gender": [], "form": [], "name": ""}
    if not isinstance(data, dict):
        return buckets
    options = list(data.get("options") or [])
    for group in data.get("grouped_options") or []:
        if isinstance(group, dict):
            options.extend(group.get("options") or [])
    for opt in options:
        if not isinstance(opt, dict):
            continue
        label = _norm(str(opt.get("name") or ""))
        value = str(opt.get("value") or "").strip()
        if not value:
            continue
        if "цвет" in label:
            if opt.get("is_variable") or ";" in value:
                continue
            buckets["color"].append(value)
        elif label == "пол" or label.startswith("пол "):
            buckets["gender"].append(value)
        elif "форм" in label:
            buckets["form"].append(value)
    title = data.get("imt_name") or data.get("subj_name") or data.get("name") or ""
    buckets["name"] = str(title or "")
    buckets["description"] = str(data.get("description") or "")
    buckets["vendor"] = str(data.get("vendor_code") or "")
    return buckets


def _main():
    import main as app_main
    return app_main


def _basket_card_url(nm_id: int) -> str:
    app_main = _main()
    img = app_main.wb_product_img_url(int(nm_id), "tm")
    if "/images/" not in img:
        return ""
    return img.split("/images/")[0] + "/info/ru/card.json"


def _fetch_card_json(nm_id: int) -> dict:
    url = _basket_card_url(nm_id)
    if not url:
        return {}
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=12,
            follow_redirects=True,
        )
    except Exception as e:
        logger.warning(f"card.json {nm_id}: {e}")
        return {}
    if not resp.is_success:
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _storefront_products(nm_ids: list) -> dict:
    """nm → цена для клиента, цвета и название с витрины card.wb.ru."""
    app_main = _main()
    ids = []
    for raw in nm_ids:
        nm = _as_int(raw)
        if nm and nm not in ids:
            ids.append(nm)
    found = {}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
    }
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        nm_param = ";".join(str(x) for x in batch)
        urls = [
            f"https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest={DEST_MOSCOW}&nm={nm_param}",
            f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest={DEST_MOSCOW}&nm={nm_param}",
        ]
        for url in urls:
            try:
                resp = httpx.get(url, headers=headers, timeout=30)
            except Exception as e:
                logger.warning(f"storefront: {e}")
                continue
            if not resp.is_success:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            products = data.get("products") or (data.get("data") or {}).get("products") or []
            if not products:
                continue
            for product in products:
                if not isinstance(product, dict):
                    continue
                info = app_main._parse_client_product(product)
                nm = info.get("nm_id")
                if not nm:
                    continue
                colors = []
                for color in product.get("colors") or []:
                    if isinstance(color, dict) and color.get("name"):
                        colors.append(str(color["name"]))
                    elif isinstance(color, str) and color.strip():
                        colors.append(color.strip())
                found[int(nm)] = {
                    "price": info.get("client_price"),
                    "name": info.get("name") or "",
                    "colors": colors,
                    "brand": product.get("brand") or "",
                }
            break
    return found


def _content_face(nm_id: int) -> dict:
    app_main = _main()
    if not getattr(app_main, "WB_TOKEN", ""):
        return {}
    try:
        resp = httpx.post(
            f"{app_main.WB_CONTENT_URL}/content/v2/get/cards/list",
            headers=app_main.wb_headers(),
            json={
                "settings": {
                    "filter": {"textSearch": str(nm_id), "withPhoto": -1},
                    "cursor": {"limit": 20},
                }
            },
            timeout=20,
        )
    except Exception as e:
        logger.warning(f"content face: {e}")
        return {}
    if not resp.is_success:
        return {}
    try:
        cards = resp.json().get("cards") or []
    except Exception:
        return {}
    for card in cards:
        if _as_int(card.get("nmID") or card.get("nmId")) != nm_id:
            continue
        texts = {"color": [], "gender": [], "form": []}
        for ch in card.get("characteristics") or []:
            if not isinstance(ch, dict):
                continue
            label = _norm(str(ch.get("name") or ""))
            value = ch.get("value")
            if isinstance(value, list):
                text = " ".join(str(v) for v in value if v)
            else:
                text = str(value or "").strip()
            if not text:
                continue
            if "цвет" in label:
                texts["color"].append(text)
            elif label == "пол" or label.startswith("пол "):
                texts["gender"].append(text)
            elif "форм" in label:
                texts["form"].append(text)
        return {
            "name": card.get("title") or "",
            "vendor": card.get("vendorCode") or "",
            "texts": texts,
        }
    return {}


def _product_colors(product: dict) -> list:
    colors = []
    for color in product.get("colors") or []:
        if isinstance(color, dict) and color.get("name"):
            colors.append(str(color["name"]))
        elif isinstance(color, str) and color.strip():
            colors.append(color.strip())
    return colors


def _chrome_get(url: str, params: dict, headers: dict):
    """httpx, а при 403 — запрос с отпечатком Chrome. WB режет датацентр."""
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=25, follow_redirects=True)
        if resp.status_code != 403:
            return resp.status_code, resp.json()
    except Exception as e:
        logger.warning(f"search httpx: {e}")
    try:
        from curl_cffi import requests as creq
        resp = creq.get(
            url,
            params=params,
            headers=headers,
            impersonate="chrome131",
            timeout=20,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return resp.status_code, None
        return resp.status_code, resp.json()
    except Exception as e:
        logger.warning(f"search chrome: {e}")
        return 0, None


def _search_rows(query: str, limit: int = 50) -> dict:
    """Выдача WB с ценой для клиента и цветом варианта."""
    app_main = _main()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
    }
    last_err = None
    params = {
        "appType": 1,
        "curr": "rub",
        "dest": DEST_MOSCOW,
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": 30,
        "page": 1,
    }
    for attempt in range(4):
        app_main._wb_search_throttle(0.35 if attempt == 0 else 0.8)
        status, data = _chrome_get(app_main._wb_search_next_host(), params, headers)
        if status == 429:
            last_err = "429"
            continue
        if status != 200 or not isinstance(data, dict):
            last_err = f"http {status}" if status else "search failed"
            continue
        products = data.get("products") or (data.get("data") or {}).get("products") or []
        rows = []
        for i, product in enumerate(products):
            if not isinstance(product, dict):
                continue
            info = app_main._parse_client_product(product)
            nm = info.get("nm_id")
            if not nm:
                continue
            rows.append({
                "position": i + 1,
                "nm_id": int(nm),
                "brand": product.get("brand") or "",
                "name": info.get("name") or product.get("name") or "",
                "price": info.get("client_price"),
                "colors": _product_colors(product),
                "thumb": app_main.wb_product_img_url(int(nm), "c246x328"),
                "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
            })
            if len(rows) >= limit:
                break
        if rows:
            return {"products": rows, "error": None}
        last_err = last_err or "empty"
    return {"products": [], "error": last_err}


def _single_color(*texts: str) -> str:
    found = set()
    for text in texts:
        found |= color_families(text or "")
    if len(found) == 1:
        return next(iter(found))
    return ""


def _card_face(nm_id: int) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
    }
    status, data = _chrome_get(
        "https://card.wb.ru/cards/v4/detail",
        {"appType": 1, "curr": "rub", "dest": DEST_MOSCOW, "spp": 30, "nm": nm_id},
        headers,
    )
    if status != 200 or not isinstance(data, dict):
        return {}
    products = data.get("products") or (data.get("data") or {}).get("products") or []
    app_main = _main()
    for product in products:
        if not isinstance(product, dict):
            continue
        info = app_main._parse_client_product(product)
        if info.get("nm_id") != nm_id:
            continue
        return {
            "price": info.get("client_price"),
            "name": info.get("name") or "",
            "colors": _product_colors(product),
        }
    return {}


def _profile_own(nm_id: int, vendor: str, board_name: str) -> dict:
    face = _fetch_card_json(nm_id)
    opts = _option_texts(face)
    content = _content_face(nm_id)
    content_texts = content.get("texts") or {}
    found = _search_rows(str(nm_id), limit=8)
    store = next((row for row in found.get("products") or [] if row["nm_id"] == nm_id), {})
    if store.get("price") is None:
        card = _card_face(nm_id)
        if card:
            store = {**store, **{k: v for k, v in card.items() if v not in (None, "", [])}}
    name = opts.get("name") or content.get("name") or store.get("name") or board_name or ""
    vendor = opts.get("vendor") or content.get("vendor") or vendor or ""
    color = _single_color(" ".join(store.get("colors") or []))
    if not color:
        color = _vendor_color(vendor)
    if not color:
        color = _single_color(" ".join(opts.get("color") or []), " ".join(content_texts.get("color") or []))
    families = [color] if color else []
    gender = " ".join((opts.get("gender") or []) + (content_texts.get("gender") or []))
    form = " ".join((opts.get("form") or []) + (content_texts.get("form") or []))
    shape = classify_shape(
        name, gender, form, vendor,
        description=opts.get("description") or "",
        trust_line=True,
    )
    price = store.get("price")
    try:
        price = int(round(float(price))) if price is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "nm_id": nm_id,
        "vendor_code": vendor,
        "name": name,
        "color": ", ".join(families),
        "colors": families,
        "shape": shape,
        "price": price,
        "gender": gender,
        "url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
    }


def _shapes_for(nm_ids: list, names: dict) -> dict:
    """Форма конкурента: сначала из названия, иначе из публичной карточки."""
    out = {}
    need = []
    for nm in nm_ids:
        shape = classify_shape(names.get(nm) or "")
        if shape:
            out[nm] = {"shape": shape, "color": "", "source": "name"}
        else:
            need.append(nm)

    def one(nm):
        try:
            data = _fetch_card_json(nm)
            opts = _option_texts(data)
            shape = classify_shape(
                " ".join(part for part in (names.get(nm), opts.get("name")) if part),
                " ".join(opts.get("gender") or []),
                " ".join(opts.get("form") or []),
                description=opts.get("description") or "",
            )
            return nm, shape, " ".join(opts.get("color") or [])
        except Exception as e:
            logger.warning(f"shape {nm}: {e}")
            return nm, "", ""

    if need:
        workers = min(8, len(need))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for nm, shape, color in pool.map(one, need):
                out[nm] = {"shape": shape, "color": color, "source": "card"}
    return out


def compare_with_top(board: dict, nm_id: int) -> dict:
    catalog = board.get("catalog") or {}
    if nm_id not in board.get("own_ids", set()) and nm_id not in catalog:
        raise HTTPException(status_code=404, detail="Карточка не найдена в кабинете")
    own_bucket = (board.get("by_nm") or {}).get(nm_id) or {}
    vendor = catalog.get(nm_id) or ""
    own = _profile_own(nm_id, vendor, own_bucket.get("name") or "")
    own["ctr"] = own_bucket.get("ctr")
    own["orders"] = own_bucket.get("orders")
    own["views"] = own_bucket.get("views")
    if not own["colors"]:
        return {"ok": False, "reason": "У нашей карточки не читается цвет часов.", "own": own, "matches": []}
    if not own["shape"]:
        return {
            "ok": False,
            "reason": "У нашей карточки не читается форма. Нужны прямоугольные, круглые женские или круглые мужские.",
            "own": own,
            "matches": [],
        }

    query = _query_for_shape(own["shape"])
    live = _search_rows(query, limit=50)
    products = live.get("products") or []
    if own["price"] is None:
        hit = next((row for row in products if row["nm_id"] == nm_id and row.get("price") is not None), None)
        if hit is None and own.get("name"):
            by_name = _search_rows(own["name"], limit=50)
            hit = next((row for row in by_name.get("products") or [] if row["nm_id"] == nm_id and row.get("price") is not None), None)
        if hit is not None:
            own["price"] = int(round(float(hit["price"])))
    if own["price"] is None:
        err = live.get("error") or ""
        return {
            "ok": False,
            "reason": "Нет цены для клиента на витрине WB." + (f" {err}" if err else ""),
            "own": own,
            "matches": [],
        }
    if not products:
        return {
            "ok": False,
            "reason": f"Выдача по ключу «{query}» не снялась." + (f" {live.get('error')}" if live.get("error") else ""),
            "own": own,
            "query": query,
            "matches": [],
        }

    own_ids = set(board.get("own_ids") or [])
    own_colors = set(own["colors"])
    skipped = {"own": 0, "price": 0, "color": 0, "shape": 0}
    priced = []
    for product in products:
        nm = _as_int(product.get("nm_id"))
        if not nm or nm == nm_id:
            continue
        if nm in own_ids:
            skipped["own"] += 1
            continue
        price = product.get("price")
        try:
            price = int(round(float(price))) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or abs(price - own["price"]) > PRICE_BAND_RUB:
            skipped["price"] += 1
            continue
        priced.append({
            "nm_id": nm,
            "position": product.get("position"),
            "brand": product.get("brand") or "",
            "name": product.get("name") or "",
            "price": price,
            "colors_raw": product.get("colors") or [],
            "url": product.get("url") or f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
            "thumb": product.get("thumb") or "",
        })

    color_ok = []
    for row in priced:
        color_text = " ".join(row.get("colors_raw") or []) or row["name"]
        families = color_families(color_text)
        if not (families & own_colors):
            skipped["color"] += 1
            continue
        row["colors"] = sorted(families)
        row["color"] = ", ".join(row["colors"])
        color_ok.append(row)

    shapes = _shapes_for(
        [row["nm_id"] for row in color_ok],
        {row["nm_id"]: row["name"] for row in color_ok},
    )
    matches = []
    by_nm = board.get("by_nm") or {}
    for row in color_ok:
        info = shapes.get(row["nm_id"]) or {}
        shape = info.get("shape") or ""
        if not shape and own["shape"] == "круглые женские":
            blob = _norm(row["name"])
            if "прямоуг" not in blob and "квадрат" not in blob and "мужск" not in blob:
                shape = own["shape"]
        if shape != own["shape"]:
            skipped["shape"] += 1
            continue
        bucket = by_nm.get(row["nm_id"]) or {}
        matches.append({
            "nm_id": row["nm_id"],
            "position": row["position"],
            "brand": row["brand"],
            "name": row["name"],
            "price": row["price"],
            "delta": row["price"] - own["price"],
            "color": row["color"],
            "shape": shape,
            "ctr": bucket.get("ctr"),
            "orders": bucket.get("orders"),
            "views": bucket.get("views"),
            "url": row["url"],
            "thumb": row["thumb"],
        })
    matches.sort(key=lambda row: (row.get("position") or 999, row["nm_id"]))
    return {
        "ok": True,
        "reason": "",
        "query": query,
        "price_band": PRICE_BAND_RUB,
        "period_label": board.get("period_label") or "",
        "own": own,
        "matches": matches,
        "seen": len(products),
        "skipped": skipped,
    }


def _serp_top50() -> tuple[dict, dict]:
    """query → [nm_id в порядке выдачи]. Кэш 6 часов."""
    app_main = _main()
    cache = app_main.get_setting_json(SERP_CACHE_KEY, {}) or {}
    if not isinstance(cache, dict):
        cache = {}
    now = datetime.now(timezone.utc)
    fresh = {}
    report = {}
    changed = False
    for query in QUERIES:
        key = f"{query}|{DEST_MOSCOW}"
        hit = cache.get(key) if isinstance(cache.get(key), dict) else None
        age_ok = False
        if hit and hit.get("at") and hit.get("nm_ids"):
            try:
                at = datetime.fromisoformat(hit["at"])
                age_ok = (now - at).total_seconds() < SERP_TTL_SEC
            except ValueError:
                age_ok = False
        if age_ok:
            fresh[query] = [int(x) for x in hit["nm_ids"] if _as_int(x)]
            report[query] = {"count": len(fresh[query]), "cached": True, "error": None}
            continue
        live = app_main.fetch_wb_serp_products(query, DEST_MOSCOW, limit=50)
        ids = []
        for product in live.get("products") or []:
            nm = _as_int(product.get("nm_id"))
            if nm and nm not in ids:
                ids.append(nm)
        err = live.get("error")
        if ids:
            cache[key] = {"at": now.isoformat(), "nm_ids": ids}
            changed = True
        fresh[query] = ids
        report[query] = {"count": len(ids), "cached": False, "error": err}
    if changed:
        app_main.save_setting_value(SERP_CACHE_KEY, cache)
    positions = {}
    for query, ids in fresh.items():
        for index, nm in enumerate(ids, 1):
            positions.setdefault(nm, []).append({"query": query, "position": index})
    return positions, report


def _shelf_ids(nm_id: int, allow_live: bool) -> tuple[list[int], str | None]:
    app_main = _main()
    cached = app_main._shelf_cache_get(nm_id, DEST_MOSCOW)
    if cached and not allow_live:
        ids = []
        for item in cached.get("items") or []:
            nm = _as_int(item.get("nm_id"))
            if nm:
                ids.append(nm)
        return ids, None
    try:
        payload = app_main.get_competitor_shelf(nm_id, dest=DEST_MOSCOW, limit=30, refresh=False)
    except Exception as e:
        logger.warning(f"shelf {nm_id}: {e}")
        return [], str(e)[:160]
    ids = []
    for item in payload.get("items") or []:
        nm = _as_int(item.get("nm_id"))
        if nm:
            ids.append(nm)
    return ids, payload.get("error")


def qualify_card(board: dict, nm_id: int, manual_ids: list[int]) -> dict:
    by_nm = board["by_nm"]
    catalog = board["catalog"]
    own_bucket = by_nm.get(nm_id) or {
        "nm_id": nm_id,
        "views": 0,
        "card_opens": 0,
        "orders": 0,
        "ctr": None,
        "reliable": False,
        "name": "",
        "brand": "",
        "url": f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
    }
    span = board.get("covered_days") or covered_days(board.get("sessions"), board.get("days") or 7)
    own = _card_public(own_bucket, catalog.get(nm_id) or "", span, True)
    positions, serp_report = _serp_top50()
    my_shelf, shelf_error = _shelf_ids(nm_id, allow_live=True)
    my_shelf_set = set(my_shelf)

    candidates = []
    for rival_nm, bucket in by_nm.items():
        if rival_nm == nm_id or rival_nm in board["own_ids"]:
            continue
        if bucket.get("orders", 0) <= 0:
            continue
        if rival_nm not in positions:
            continue
        candidates.append(rival_nm)

    manual_set = {nm for nm in manual_ids if nm and nm != nm_id}
    need_reverse = []
    for rival_nm in candidates:
        if rival_nm in my_shelf_set:
            continue
        need_reverse.append(rival_nm)
    need_reverse.sort(key=lambda nm: -(by_nm[nm].get("orders") or 0))
    reverse_checked = {}
    live_budget = REVERSE_SHELF_CAP
    for rival_nm in need_reverse:
        cached_only = live_budget <= 0
        ids, err = _shelf_ids(rival_nm, allow_live=not cached_only)
        if not cached_only:
            live_budget -= 1
        reverse_checked[rival_nm] = nm_id in ids
        if err and rival_nm not in reverse_checked:
            reverse_checked[rival_nm] = False

    rows = []
    seen = set()

    def add_row(rival_nm: int, manual: bool):
        if rival_nm in seen or rival_nm == nm_id:
            return
        seen.add(rival_nm)
        bucket = by_nm.get(rival_nm)
        if not bucket:
            rows.append({
                "nm_id": rival_nm,
                "name": "",
                "brand": "",
                "views": 0,
                "orders": 0,
                "ctr": None,
                "reliable": False,
                "low_data": True,
                "url": f"https://www.wildberries.ru/catalog/{rival_nm}/detail.aspx",
                "on_my_shelf": rival_nm in my_shelf_set,
                "i_on_their_shelf": False,
                "in_top50": rival_nm in positions,
                "top50": positions.get(rival_nm) or [],
                "in_score": False,
                "manual": manual,
                "verdict": "нет в сравнении",
            })
            return
        on_my = rival_nm in my_shelf_set
        i_on = bool(reverse_checked.get(rival_nm))
        in_top = rival_nm in positions
        has_orders = bucket.get("orders", 0) > 0
        in_score = (on_my or i_on) and in_top and has_orders
        card = _card_public(bucket, "", span, False)
        card.update({
            "on_my_shelf": on_my,
            "i_on_their_shelf": i_on,
            "in_top50": in_top,
            "top50": positions.get(rival_nm) or [],
            "in_score": in_score,
            "manual": manual,
            "verdict": _verdict(own, card, in_score),
        })
        if manual and not in_score:
            card["verdict"] = "вне зачёта"
        rows.append(card)

    for rival_nm in candidates:
        add_row(rival_nm, False)
    for rival_nm in manual_set:
        add_row(rival_nm, True)

    rows = [r for r in rows if r.get("in_score") or r.get("manual")]

    def sort_key(card):
        rank = {"выше": 0, "ниже": 1, "наравне": 2, "мало данных": 3, "вне зачёта": 4, "нет в сравнении": 5}
        return (rank.get(card.get("verdict"), 9), -(card.get("views") or 0))

    rows.sort(key=sort_key)
    scored = [r for r in rows if r.get("in_score")]
    summary = {
        "scored": len(scored),
        "above": sum(1 for r in scored if r["verdict"] == "выше"),
        "below": sum(1 for r in scored if r["verdict"] == "ниже"),
        "tie": sum(1 for r in scored if r["verdict"] == "наравне"),
        "low_data": sum(1 for r in scored if r["verdict"] == "мало данных"),
        "shelf_error": shelf_error,
        "shelf_count": len(my_shelf),
        "reverse_checked": len(reverse_checked),
        "top50_with_orders": len(candidates),
        "own_reliable": bool(own.get("reliable")),
    }
    our_positions = positions.get(nm_id) or []
    return {
        "nm_id": nm_id,
        "own": own,
        "our_positions": our_positions,
        "queries": [
            {"query": q, **serp_report.get(q, {"count": 0, "cached": False, "error": "нет данных"})}
            for q in QUERIES
        ],
        "rivals": rows,
        "summary": summary,
    }


def _ads_for_window(days: int) -> dict:
    app_main = _main()
    end = date.today()
    start = end - timedelta(days=days - 1)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    current, _prev = app_main.fetch_ad_nm_windows(start_dt, end_dt, start_dt, end_dt)
    return current or {}


def attach_ads(own_cards: list, ads_by_nm: dict) -> list:
    out = []
    for card in own_cards:
        nm = card["nm_id"]
        ad = ads_by_nm.get(nm) or ads_by_nm.get(str(nm)) or {}
        views = int(ad.get("views") or 0)
        clicks = int(ad.get("clicks") or 0)
        row = dict(card)
        row["ad_views"] = views
        row["ad_clicks"] = clicks
        row["ad_ctr"] = round(clicks / views * 100, 2) if views else None
        total_views = int(card.get("views") or 0)
        total_opens = int(card.get("card_opens") or 0)
        org_views = total_views - views
        org_clicks = total_opens - clicks
        if views and org_views >= 0 and org_clicks >= 0 and org_views > 0:
            row["org_views"] = org_views
            row["org_clicks"] = org_clicks
            row["org_ctr"] = round(org_clicks / org_views * 100, 2)
            row["org_note"] = None
        elif views:
            row["org_views"] = None
            row["org_clicks"] = None
            row["org_ctr"] = None
            row["org_note"] = "периоды рекламы и сравнения не сходятся"
        else:
            row["org_views"] = None
            row["org_clicks"] = None
            row["org_ctr"] = None
            row["org_note"] = "нет показов рекламы"
        out.append(row)
    return out


@router.get("/ctr-board")
def ctr_board(request: Request, days: int = 7):
    _user(request)
    days = _days(days)
    board = load_board(days)
    return {
        "days": board["days"],
        "period_label": board.get("period_label") or "",
        "covered_days": board["covered_days"],
        "own_views_per_day": board["own_views_per_day"],
        "rival_views_per_day": board["rival_views_per_day"],
        "own_min_views": board["own_min_views"],
        "rival_min_views": board["rival_min_views"],
        "min_views": board["own_min_views"],
        "queries": board["queries"],
        "sessions": board["sessions"],
        "own": board["own"],
        "own_count": len(board["own"]),
        "compared_count": sum(1 for c in board["own"] if c.get("ctr") is not None),
    }


@router.post("/ctr-qualify")
def ctr_qualify(request: Request, body: dict):
    _user(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    days = _days(body.get("days") or 7)
    nm_id = _as_int(body.get("nm_id"))
    if not nm_id:
        raise HTTPException(status_code=400, detail="nm_id required")
    manual = []
    for raw in body.get("manual_nm_ids") or []:
        nm = _as_int(raw)
        if nm:
            manual.append(nm)
    text = str(body.get("manual_text") or "")
    for part in text.replace(";", ",").replace("\n", ",").split(","):
        nm = _as_int(part.strip())
        if nm:
            manual.append(nm)
    board = load_board(days)
    if nm_id not in board["own_ids"] and nm_id not in board["by_nm"]:
        raise HTTPException(status_code=404, detail="Карточка не найдена в кабинете и в сравнениях")
    return qualify_card(board, nm_id, manual)


@router.post("/ctr-ads")
def ctr_ads(request: Request, body: dict):
    _user(request)
    days = _days((body or {}).get("days") or 7)
    app_main = _main()
    if not getattr(app_main, "WB_TOKEN", ""):
        raise HTTPException(status_code=503, detail="WB_TOKEN не задан — рекламный CTR посчитать нельзя")
    board = load_board(days)
    try:
        ads = _ads_for_window(days)
    except Exception as e:
        logger.exception(f"ctr ads: {e}")
        raise HTTPException(status_code=502, detail=f"Реклама кабинета не ответила: {str(e)[:160]}")
    cards = attach_ads(board["own"], ads)
    with_ads = sum(1 for c in cards if c.get("ad_views"))
    return {"days": days, "with_ads": with_ads, "own": cards}


@router.post("/compare")
def studio_compare(request: Request, body: dict):
    _user(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    nm_id = _as_int(body.get("nm_id"))
    if not nm_id:
        raise HTTPException(status_code=400, detail="nm_id required")
    board = load_board(30)
    return compare_with_top(board, nm_id)
