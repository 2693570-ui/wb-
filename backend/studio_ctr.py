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


def _main():
    import main as app_main
    return app_main


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
