"""Анализ выдачи WB и бриф на кликабельный главный слайд."""
from __future__ import annotations

import colorsys
import io
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from PIL import Image, ImageFilter, ImageStat

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BG_LABELS = (
    ("white", (248, 248, 248)),
    ("light", (220, 218, 212)),
    ("beige", (214, 196, 168)),
    ("gray", (140, 140, 140)),
    ("black", (28, 28, 28)),
    ("navy", (28, 42, 78)),
    ("blue", (56, 110, 190)),
    ("red", (190, 48, 48)),
    ("orange", (210, 110, 42)),
    ("green", (48, 140, 86)),
    ("pink", (220, 140, 168)),
    ("gold", (196, 168, 86)),
    ("purple", (110, 72, 160)),
)

BG_RU = {
    "white": "белый",
    "light": "светлый",
    "beige": "бежевый",
    "gray": "серый",
    "black": "чёрный",
    "navy": "тёмно-синий",
    "blue": "синий",
    "red": "красный",
    "orange": "оранжевый",
    "green": "зелёный",
    "pink": "розовый",
    "gold": "золотой",
    "purple": "фиолетовый",
}


def _hex(rgb) -> str:
    r, g, b = [max(0, min(255, int(x))) for x in rgb]
    return f"#{r:02X}{g:02X}{b:02X}"


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _brightness(rgb) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def _classify_bg(rgb) -> str:
    return min(BG_LABELS, key=lambda item: _dist(rgb, item[1]))[0]


def _complementary(rgb) -> tuple[int, int, int]:
    r, g, b = [x / 255.0 for x in rgb]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.12:
        return (26, 24, 22) if v > 0.55 else (245, 240, 232)
    cr, cg, cb = colorsys.hsv_to_rgb((h + 0.5) % 1.0, min(1.0, s + 0.15), 0.28 if v > 0.6 else 0.92)
    return (int(cr * 255), int(cg * 255), int(cb * 255))


def _analyze_image(content: bytes) -> dict | None:
    try:
        im = Image.open(io.BytesIO(content)).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    if w < 8 or h < 8:
        return None

    small = im.resize((64, 64), Image.Resampling.BILINEAR)
    pixels = list(small.getdata())
    n = len(pixels) or 1
    avg = tuple(sum(p[i] for p in pixels) / n for i in range(3))

    border = []
    center = []
    for y in range(64):
        for x in range(64):
            p = pixels[y * 64 + x]
            if x < 8 or x >= 56 or y < 8 or y >= 56:
                border.append(p)
            elif 16 <= x < 48 and 16 <= y < 48:
                center.append(p)
    border_avg = tuple(sum(p[i] for p in border) / len(border) for i in range(3)) if border else avg
    center_avg = tuple(sum(p[i] for p in center) / len(center) for i in range(3)) if center else avg
    border_std = (
        sum(_dist(p, border_avg) for p in border) / len(border) if border else 0
    )

    gray = small.convert("L").filter(ImageFilter.FIND_EDGES)
    edge = ImageStat.Stat(gray).mean[0] / 255.0

    br = _brightness(border_avg)
    cr, cg, cb = [c / 255.0 for c in border_avg]
    _, sat, _ = colorsys.rgb_to_hsv(cr, cg, cb)

    if border_std < 28 and edge < 0.18:
        shot = "studio"
    elif edge > 0.32:
        shot = "busy"
    else:
        shot = "mixed"

    return {
        "avg_hex": _hex(avg),
        "bg_hex": _hex(border_avg),
        "fg_hex": _hex(center_avg),
        "bg_family": _classify_bg(border_avg),
        "brightness": round(br, 1),
        "saturation": round(sat, 3),
        "edge": round(edge, 3),
        "border_std": round(border_std, 1),
        "shot": shot,
    }


def _fetch_one(client: httpx.Client, url: str) -> bytes | None:
    try:
        resp = client.get(url)
        if resp.is_success and len(resp.content) > 400:
            return resp.content
    except Exception:
        return None
    return None


def _download_visual(thumb_url: str, fallbacks: list[str]) -> dict | None:
    headers = {
        "User-Agent": _UA,
        "Accept": "image/webp,image/jpeg,image/*;q=0.8",
        "Referer": "https://www.wildberries.ru/",
    }
    urls = [thumb_url] + [u for u in fallbacks if u and u != thumb_url]
    with httpx.Client(timeout=12, headers=headers, follow_redirects=True) as client:
        for url in urls[:4]:
            raw = _fetch_one(client, url)
            if not raw:
                continue
            parsed = _analyze_image(raw)
            if parsed:
                return parsed
    return None


def _median_rgb(hexes: list[str]) -> tuple[int, int, int]:
    rgbs = []
    for h in hexes:
        h = (h or "").lstrip("#")
        if len(h) != 6:
            continue
        try:
            rgbs.append((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
        except ValueError:
            continue
    if not rgbs:
        return (240, 240, 240)
    rgbs.sort(key=_brightness)
    return rgbs[len(rgbs) // 2]


def _query_tone(query: str) -> str:
    q = (query or "").lower()
    if any(w in q for w in ("женск", "девуш", "девоч", "для женщин")):
        return "women"
    if any(w in q for w in ("детск", "ребен", "ребён", "мальчик", "девочек")):
        return "kids"
    if any(w in q for w in ("мужск", "мужчин", "для мужчин")):
        return "men"
    return "unisex"


def _copy_for(tone: str, contrast_dark: bool) -> tuple[str, str]:
    if tone == "women":
        return ("AMOLED · тонкий ремешок", "Женские смарт-часы без лишнего")
    if tone == "kids":
        return ("GPS · звонок родителям", "Детские часы, которые находят")
    if tone == "men":
        return ("AMOLED · 7 дней", "Мужские смарт-часы без компромиссов")
    if contrast_dark:
        return ("AMOLED · 7 дней", "Часы, которые видно в выдаче")
    return ("AMOLED · всегда на связи", "Главный кадр, который кликают")


def build_brief(query: str, items: list[dict], palette: dict) -> dict:
    n = len(items) or 1
    families = Counter(it.get("bg_family") or "light" for it in items)
    shots = Counter(it.get("shot") or "mixed" for it in items)
    top_bg, top_bg_n = families.most_common(1)[0]
    top_shot, top_shot_n = shots.most_common(1)[0]
    bg_share = round(top_bg_n / n * 100)
    shot_share = round(top_shot_n / n * 100)

    median = palette.get("median_rgb") or (240, 240, 240)
    contrast_rgb = _complementary(median)
    contrast_dark = _brightness(contrast_rgb) < 90
    tone = _query_tone(query)
    headline, sub = _copy_for(tone, contrast_dark)

    holes = []
    if top_bg in ("white", "light", "beige") and bg_share >= 45:
        holes.append(
            f"{bg_share}% выдачи на {BG_RU.get(top_bg, top_bg)} фоне. "
            "Тёмный или насыщенный фон сразу выбивается из сетки."
        )
    elif top_bg in ("black", "navy", "gray") and bg_share >= 40:
        holes.append(
            f"{bg_share}% выдачи тёмные. Светлый тёплый фон или цветной акцент даст клик."
        )
    else:
        holes.append("Фоны смешанные — выигрывает самый чистый кадр с одним сильным акцентом.")

    if top_shot == "studio" and shot_share >= 50:
        holes.append(
            f"{shot_share}% — студийный предмет. Кадр «часы на запястье» или крупный циферблат с УТП."
        )
    elif top_shot == "busy" and shot_share >= 40:
        holes.append(
            f"{shot_share}% перегружены текстом и плашками. Чистый кадр без баннеров кликается лучше."
        )
    else:
        holes.append("Смешаны студия и lifestyle — не копируй соседа: один объект, один оффер.")

    ratings = [it.get("rating") for it in items if it.get("rating")]
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    prices = [it.get("price") for it in items if isinstance(it.get("price"), (int, float)) and it["price"] > 0]
    price_med = sorted(prices)[len(prices) // 2] if prices else None

    avoid = [
        "Белый фон + часы плашмя, если так сделано у половины топа",
        "Мелкий текст на весь кадр — в превью 3:4 его не читают",
        "Копия Apple Watch 1-в-1: модерация и нулевой контраст с сеткой",
    ]
    if top_bg in ("white", "light"):
        avoid.insert(0, "Ещё один белый каталожный кадр")

    concept_studio_bg = _hex(contrast_rgb)
    concept_studio_fg = "#F7F4F0" if contrast_dark else "#1A1816"
    accent = "#D4622A"
    light_alt = "#F3EDE4"
    dark_alt = "#12141A"

    concepts = [
        {
            "id": "studio",
            "title": "Контрастная студия",
            "why": holes[0],
            "bg": concept_studio_bg,
            "text": concept_studio_fg,
            "accent": accent,
            "headline": headline,
            "sub": sub,
            "composition": "Часы крупно по центру, чистый фон, одно УТП снизу. Без звёзд и без «хит».",
        },
        {
            "id": "wrist",
            "title": "На запястье",
            "why": "В топе мало живого кадра — рука + циферблат читается как премиум и даёт клик.",
            "bg": dark_alt if not contrast_dark else light_alt,
            "text": "#F7F4F0" if not contrast_dark else "#1A1816",
            "accent": accent,
            "headline": "На руке · AMOLED",
            "sub": "Покупатель сразу понимает размер и стиль",
            "composition": "Запястье на 70% кадра, циферблат в фокусе, фон размыт, текст минимум.",
        },
        {
            "id": "split",
            "title": "Фото + оффер",
            "why": "Если все молчат в кадре или наоборот кричат плашками — короткий оффер в нижней трети.",
            "bg": light_alt if contrast_dark else dark_alt,
            "text": "#1A1816" if contrast_dark else "#F7F4F0",
            "accent": accent,
            "headline": headline,
            "sub": "2–4 слова, не абзац",
            "composition": "Верх 65% — товар, низ 35% — цветовая плашка с УТП.",
        },
    ]

    return {
        "query": query,
        "tone": tone,
        "holes": holes,
        "avoid": avoid,
        "dominant_bg": top_bg,
        "dominant_bg_ru": BG_RU.get(top_bg, top_bg),
        "dominant_bg_share": bg_share,
        "dominant_shot": top_shot,
        "dominant_shot_share": shot_share,
        "avg_rating": avg_rating,
        "price_median": price_med,
        "contrast_hex": concept_studio_bg,
        "concepts": concepts,
        "note": (
            "CTR и CR подтягиваются из загруженных отчётов «Сравнение карточек». "
            "Если артикула нет в отчёте — остаётся визуальная оценка."
        ),
    }


def analyze_serp(query: str, products: list[dict]) -> dict:
    """Качает превью, считает фон/тип кадра, скорит контраст, собирает бриф."""
    analyzed = []

    def job(p):
        thumb = p.get("thumb") or ""
        fb = p.get("fallbacks") or []
        vis = _download_visual(thumb, fb) if thumb else None
        return p, vis

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(job, p) for p in products]
        for fut in as_completed(futs):
            try:
                p, vis = fut.result()
            except Exception as e:
                logger.warning(f"visual job: {e}")
                continue
            row = dict(p)
            if vis:
                row.update(vis)
            else:
                row.update({
                    "avg_hex": "#E8E4DE",
                    "bg_hex": "#E8E4DE",
                    "fg_hex": "#E8E4DE",
                    "bg_family": "light",
                    "brightness": 220,
                    "saturation": 0.05,
                    "edge": 0.1,
                    "shot": "mixed",
                    "visual_error": True,
                })
            analyzed.append(row)

    analyzed.sort(key=lambda x: x.get("position") or 999)
    hexes = [it.get("bg_hex") for it in analyzed if it.get("bg_hex") and not it.get("visual_error")]
    median_rgb = _median_rgb(hexes)
    families = Counter(it.get("bg_family") for it in analyzed if it.get("bg_family"))
    n = len(analyzed) or 1

    prices = [it["price"] for it in analyzed if isinstance(it.get("price"), (int, float)) and it["price"] > 0]
    price_med = sorted(prices)[len(prices) // 2] if prices else None

    for it in analyzed:
        bg = it.get("bg_hex") or "#E8E4DE"
        try:
            rgb = (int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16))
        except Exception:
            rgb = (232, 228, 222)
        color_gap = min(1.0, _dist(rgb, median_rgb) / 220.0)
        fam = it.get("bg_family") or "light"
        rarity = 1.0 - (families.get(fam, 0) / n)
        rating = float(it.get("rating") or 0)
        fb = float(it.get("feedbacks") or 0)
        social = min(1.0, rating / 5.0) * 0.6 + min(1.0, (fb ** 0.5) / 80) * 0.4
        price = it.get("price")
        price_score = 0.5
        if price_med and isinstance(price, (int, float)) and price > 0:
            # чуть дешевле медианы — легче клик, слишком дёшево — «китай»
            ratio = price / price_med
            if 0.75 <= ratio <= 1.05:
                price_score = 1.0
            elif 0.55 <= ratio < 0.75 or 1.05 < ratio <= 1.25:
                price_score = 0.7
            else:
                price_score = 0.4
        shot_bonus = 0.15 if it.get("shot") == "studio" and families.get("white", 0) / n > 0.5 and fam not in ("white", "light") else 0.0
        if it.get("shot") == "busy":
            shot_bonus -= 0.08
        click = (
            color_gap * 0.38
            + rarity * 0.22
            + social * 0.22
            + price_score * 0.18
            + shot_bonus
        )
        it["click_score"] = max(0, min(100, round(click * 100)))
        it["standout"] = round(color_gap * 100)

    palette = {
        "median_hex": _hex(median_rgb),
        "median_rgb": median_rgb,
        "families": [
            {"id": k, "label": BG_RU.get(k, k), "count": v, "share": round(v / n * 100)}
            for k, v in families.most_common()
        ],
        "shots": [
            {"id": k, "count": v, "share": round(v / n * 100)}
            for k, v in Counter(it.get("shot") for it in analyzed).most_common()
        ],
    }
    brief = build_brief(query, analyzed, palette)
    # median_rgb не сериализуем отдельно в ответ — убираем из palette для JSON
    palette_out = {k: v for k, v in palette.items() if k != "median_rgb"}
    return {
        "items": analyzed,
        "palette": palette_out,
        "brief": brief,
    }


def _median(nums: list[float]) -> float | None:
    if not nums:
        return None
    s = sorted(nums)
    return s[len(s) // 2]


def apply_competitor_funnel(analyzed: dict, funnel_by_nm: dict) -> dict:
    """Накладывает CTR/CR из «Сравнение карточек» и пересчитывает скор."""
    items = analyzed.get("items") or []
    brief = analyzed.get("brief") or {}
    if not items:
        analyzed["funnel"] = {"matched": 0, "total": 0}
        return analyzed

    for it in items:
        nm = it.get("nm_id")
        try:
            nm = int(nm)
        except (TypeError, ValueError):
            nm = None
        m = funnel_by_nm.get(nm) if nm is not None else None
        if not m:
            it["score_source"] = "visual"
            continue
        it["ctr"] = m.get("ctr")
        it["cart_conv"] = m.get("cart_conv")
        it["order_conv"] = m.get("order_conv")
        it["funnel_views"] = m.get("views")
        it["funnel_opens"] = m.get("card_opens")
        it["funnel_orders"] = m.get("orders")
        it["funnel_period"] = m.get("period")
        it["score_source"] = "ctr" if m.get("ctr") is not None else "visual"

    with_ctr = [it for it in items if isinstance(it.get("ctr"), (int, float))]
    ctrs = [float(it["ctr"]) for it in with_ctr]
    crs = [float(it["cart_conv"]) for it in items if isinstance(it.get("cart_conv"), (int, float))]

    if ctrs:
        lo, hi = min(ctrs), max(ctrs)
        span = (hi - lo) or 1.0
        for it in items:
            vis = (it.get("click_score") or 0) / 100.0
            if isinstance(it.get("ctr"), (int, float)):
                rel = (float(it["ctr"]) - lo) / span
                it["visual_score"] = it.get("click_score")
                it["click_score"] = max(0, min(100, round(rel * 80 + vis * 20)))
                it["score_source"] = "ctr"
            else:
                it["visual_score"] = it.get("click_score")
                it["score_source"] = "visual"

    matched = sum(1 for it in items if it.get("ctr") is not None or it.get("cart_conv") is not None)
    top = sorted(with_ctr, key=lambda x: float(x.get("ctr") or 0), reverse=True)[:5]
    holes = list(brief.get("holes") or [])
    if matched:
        period = next((it.get("funnel_period") for it in items if it.get("funnel_period")), "")
        med_ctr = _median(ctrs)
        med_cr = _median(crs)
        lead = top[0] if top else None
        line = (
            f"Из отчёта «Сравнение карточек» совпало {matched} из {len(items)} карточек выдачи"
            + (f" · период {period}" if period else "")
            + (f" · медиана CTR {med_ctr:.1f}%" if med_ctr is not None else "")
            + (f", CR в корзину {med_cr:.1f}%" if med_cr is not None else "")
            + "."
        )
        holes.insert(0, line)
        if lead:
            holes.insert(1, (
                f"Лидер CTR в этой выдаче: {lead.get('brand') or lead.get('nm_id')} — "
                f"{float(lead['ctr']):.1f}% "
                f"({BG_RU.get(lead.get('bg_family'), lead.get('bg_family') or 'фон')}, "
                f"{'студия' if lead.get('shot')=='studio' else 'плотный кадр' if lead.get('shot')=='busy' else 'смешанный кадр'})."
            ))
        if len(top) >= 3:
            fam = Counter(it.get("bg_family") or "light" for it in top).most_common(1)[0]
            holes.insert(2, f"У топ-CTR чаще {BG_RU.get(fam[0], fam[0])} фон — копируй этот контраст, не середняк выдачи.")
    else:
        holes.insert(0, "В загруженных отчётах «Сравнение карточек» нет артикулов из этой выдачи — скор пока визуальный. Залей отчёт в разделе Конкуренты.")

    brief["holes"] = holes
    brief["median_ctr"] = round(_median(ctrs), 2) if ctrs else None
    brief["median_cr"] = round(_median(crs), 2) if crs else None
    brief["funnel_matched"] = matched
    analyzed["brief"] = brief
    analyzed["funnel"] = {
        "matched": matched,
        "total": len(items),
        "median_ctr": brief.get("median_ctr"),
        "median_cr": brief.get("median_cr"),
        "period": next((it.get("funnel_period") for it in items if it.get("funnel_period")), None),
        "top": [
            {
                "nm_id": it.get("nm_id"),
                "brand": it.get("brand"),
                "ctr": it.get("ctr"),
                "cart_conv": it.get("cart_conv"),
                "order_conv": it.get("order_conv"),
                "position": it.get("position"),
            }
            for it in top[:5]
        ],
    }
    return select_winner(analyzed)


def select_winner(analyzed: dict) -> dict:
    """Один главный слайд, который должен выигрывать клик в местах 1–50."""
    brief = analyzed.get("brief") or {}
    items = analyzed.get("items") or []
    concepts = {c.get("id"): c for c in (brief.get("concepts") or []) if c.get("id")}
    n = len(items) or 1
    top_bg = brief.get("dominant_bg") or "light"
    top_shot = brief.get("dominant_shot") or "mixed"
    bg_share = brief.get("dominant_bg_share") or 0
    shot_share = brief.get("dominant_shot_share") or 0
    funnel = analyzed.get("funnel") or {}

    beats = []
    pick = "studio"
    if top_bg in ("white", "light", "beige") and bg_share >= 40:
        beats.append(
            f"{bg_share}% мест 1–{n} на {BG_RU.get(top_bg, top_bg)} фоне — "
            "тёмный или цветной кадр выбивается из сетки"
        )
        pick = "studio"
    elif top_bg in ("black", "navy", "gray") and bg_share >= 40:
        beats.append(f"{bg_share}% топа тёмные — светлый тёплый фон читается первым")
        pick = "studio"
    else:
        beats.append("Фоны в топ-50 смешанные — побеждает самый чистый кадр с одним акцентом")

    if top_shot == "studio" and shot_share >= 55:
        beats.append(
            f"{shot_share}% — часы плашмя. Кадр на запястье выглядит дороже середняка выдачи"
        )
        pick = "wrist"
    elif top_shot == "busy" and shot_share >= 40:
        beats.append(f"{shot_share}% перегружены текстом и плашками — чистая студия без баннеров")
        pick = "studio"
    elif top_shot == "mixed":
        beats.append("Студия и lifestyle перемешаны — один объект, одно УТП, без копии соседа")

    lead = (funnel.get("top") or [None])[0]
    if lead and lead.get("ctr") is not None:
        beats.append(
            f"Ориентир по отчёту: лидер CTR в этой выдаче — {lead.get('brand') or lead.get('nm_id')} "
            f"({float(lead['ctr']):.1f}%), место {lead.get('position') or '—'}"
        )

    winner = dict(concepts.get(pick) or concepts.get("studio") or {})
    winner["id"] = pick if pick in concepts else (winner.get("id") or "studio")
    winner["why_beats"] = beats
    winner["vs_places"] = n
    winner["title"] = winner.get("title") or "Главный слайд"
    brief["winner"] = winner
    analyzed["brief"] = brief
    return analyzed
