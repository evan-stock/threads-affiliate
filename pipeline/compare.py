"""단가 비교표 이미지 생성 (1080x1350, 직접 만든 이미지라 가공 금지 규정과 무관).

compare 예시:
{
  "title": "한입삼겹살 2kg",
  "unit": "100g", "per": 100,              # amount 단위당 몇을 기준으로 볼지 (g이면 100 → 100g당)
  "checked_at": "10/16 17:50",
  "rows": [
    {"name": "토스쇼핑", "price": 15600, "amount": 2000, "amount_label": "500g×4", "source": "앱 확인"},
    {"name": "쿠팡", "price": 18900, "amount": 2000, "amount_label": "500g×4", "source": "사용자 확인"}
  ]
}
"""
from __future__ import annotations

import glob
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350
BG = (250, 247, 240)
INK = (30, 30, 30)
SUB = (120, 115, 105)
CARD = (255, 255, 255)
BEST = (255, 214, 64)
ACCENT = (232, 80, 50)


def _font_path(bold: bool) -> str:
    cands = []
    for pat in ["/usr/share/fonts/opentype/noto/NotoSansCJK-{w}.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-{w}.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-{w}.ttc"]:
        cands.append(pat.format(w="Bold" if bold else "Regular"))
    cands += glob.glob("/usr/share/fonts/**/*CJK*.tt*", recursive=True)
    cands += glob.glob("/usr/share/fonts/**/Nanum*.ttf", recursive=True)
    for c in cands:
        if os.path.exists(c):
            return c
    raise RuntimeError("한글 글꼴이 없어요 (fonts-noto-cjk 설치 필요)")


def _f(size: int, bold: bool = False):
    p = _font_path(bold)
    # NotoSansCJK ttc: index 1 = KR
    try:
        return ImageFont.truetype(p, size, index=1)
    except Exception:
        return ImageFont.truetype(p, size)


def won(n: float) -> str:
    return f"{round(n):,}원"


def unit_prices(c: dict) -> list[float]:
    per = c.get("per", 100)
    return [r["price"] / r["amount"] * per for r in c["rows"]]


def validate(c: dict) -> list[str]:
    errs = []
    rows = c.get("rows") or []
    if len(rows) < 1:
        errs.append("단가 카드에 rows가 1개 이상 필요해요")
    for r in rows:
        for k in ("name", "price", "amount"):
            if not r.get(k):
                errs.append(f"비교표 항목에 {k}가 없어요")
        if not r.get("source"):
            errs.append(f"'{r.get('name', '?')}' 가격 출처(source)를 적어 주세요 (확인 안 된 가격은 못 넣어요)")
    if not c.get("checked_at"):
        errs.append("비교표에 checked_at(가격 확인 시각)이 필요해요")
    return errs


def render_single(c: dict, out_path: str) -> str:
    """비교할 곳이 없거나 차이가 작을 때: 같은 디자인의 단가 카드(단가 크게 + 한 줄 메모).
    공식 상품 사진은 가공하지 않고 두 번째 이미지로 따로 붙인다."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    unit = c.get("unit", "1개")
    r = c["rows"][0]
    up = unit_prices(c)[0]
    d.text((70, 70), "득템연구소 단가 계산", font=_f(36, True), fill=ACCENT)
    d.text((70, 125), c.get("title", ""), font=_f(60, True), fill=INK)
    d.text((70, 210), f"{unit}당 가격", font=_f(40), fill=SUB)
    y0, y1 = 330, 1010
    d.rounded_rectangle((60, y0, W - 60, y1), radius=36, fill=CARD, outline=BEST, width=8)
    d.text((110, y0 + 50), r["name"], font=_f(48, True), fill=INK)
    meta = f"{won(r['price'])} · {r.get('amount_label', '')}".strip(" ·")
    d.text((110, y0 + 120), meta, font=_f(38), fill=SUB)
    lab = f"{unit}당"
    lf = _f(48, True)
    d.text(((W - d.textlength(lab, font=lf)) / 2, y0 + 250), lab, font=lf, fill=SUB)
    big = won(up)
    bf = _f(150, True)
    d.text(((W - d.textlength(big, font=bf)) / 2, y0 + 310), big, font=bf, fill=ACCENT)
    if c.get("note"):
        tf = _f(36, True)
        tw = d.textlength(c["note"], font=tf)
        x = (W - tw) / 2
        d.rounded_rectangle((x - 30, y1 - 130, x + tw + 30, y1 - 64), radius=30, fill=BEST)
        d.text((x, y1 - 124), c["note"], font=tf, fill=INK)
    foot = f"가격 확인: {c['checked_at']} 기준 · 배송비·쿠폰·옵션에 따라 달라질 수 있어요"
    d.text((70, H - 150), foot, font=_f(28), fill=SUB)
    d.text((70, H - 105), "@gotitem.lab", font=_f(32, True), fill=INK)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "JPEG", quality=92)
    return out_path


def summary(c: dict) -> str:
    """본문에 쓸 한 줄 요약: 가장 싼 곳이 다음 곳보다 몇 % 싼지."""
    ups = unit_prices(c)
    order = sorted(range(len(ups)), key=lambda i: ups[i])
    best, second = order[0], order[1]
    pct = (ups[second] - ups[best]) / ups[second] * 100
    unit = c.get("unit", "100g")
    return f"{c['rows'][best]['name']} {unit}당 {won(ups[best])}, {c['rows'][second]['name']}보다 {pct:.0f}% 쌈"


def render(c: dict, out_path: str) -> str:
    if len(c["rows"]) == 1:
        return render_single(c, out_path)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    unit = c.get("unit", "100g")
    ups = unit_prices(c)
    best = min(range(len(ups)), key=lambda i: ups[i])
    worst = max(ups)

    d.text((70, 70), "득템연구소 단가 계산", font=_f(36, True), fill=ACCENT)
    title = c.get("title", "")
    d.text((70, 125), title, font=_f(64, True), fill=INK)
    d.text((70, 215), f"{unit}당 가격 비교", font=_f(40), fill=SUB)

    rows = c["rows"]
    n = len(rows)
    top, bottom = 310, H - 190
    gap = 28
    ch = min(250, (bottom - top - gap * (n - 1)) // n)
    for i, r in enumerate(rows):
        y = top + i * (ch + gap)
        is_best = i == best
        d.rounded_rectangle((60, y, W - 60, y + ch), radius=28,
                            fill=CARD, outline=BEST if is_best else (225, 220, 210), width=8 if is_best else 3)
        d.text((100, y + 30), r["name"], font=_f(46, True), fill=INK)
        meta = f"{won(r['price'])} · {r.get('amount_label', '')}".strip(" ·")
        d.text((100, y + 95), meta, font=_f(34), fill=SUB)
        up = f"{unit}당 {won(ups[i])}"
        fnt = _f(56 if is_best else 48, True)
        tw = d.textlength(up, font=fnt)
        d.text((W - 100 - tw, y + 40), up, font=fnt, fill=ACCENT if is_best else INK)
        if is_best:
            tag = "제일 쌈"
            tf = _f(32, True)
            tw2 = d.textlength(tag, font=tf)
            d.rounded_rectangle((W - 100 - tw2 - 36, y + ch - 78, W - 100, y + ch - 28), radius=22, fill=BEST)
            d.text((W - 100 - tw2 - 18, y + ch - 74), tag, font=tf, fill=INK)
        elif worst > 0:
            diff = (ups[i] - ups[best]) / ups[best] * 100
            tag = f"제일 싼 것보다 {diff:.0f}% 비쌈"
            tf = _f(30)
            tw2 = d.textlength(tag, font=tf)
            d.text((W - 100 - tw2, y + 108), tag, font=tf, fill=SUB)

    foot = f"가격 확인: {c['checked_at']} 기준 · 배송비·쿠폰·옵션에 따라 달라질 수 있어요"
    d.text((70, H - 150), foot, font=_f(28), fill=SUB)
    d.text((70, H - 105), "@gotitem.lab", font=_f(32, True), fill=INK)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "JPEG", quality=92)
    return out_path
