"""실사 사진·영상 → 쿠팡 파트너스 글 자동 작성.

흐름
1) 텔레그램으로 사진/영상 + 캡션("상품명" 또는 "상품명 / 한줄 메모")을 보낸다
2) 감시(poster)가 받아서 media/<id>/ 에 저장하고 state/inbox/<id>.json 을 만든다
3) 앨범이 다 들어오면(마지막 파일 후 30초) Claude API가 사진을 보고 본문+댓글을 쓴다
   → state/posts/<id>.json (source coupang, link 비어 있음) + 텔레그램 미리보기
4) 쿠팡 파트너스 링크는 사용자 PC의 Claude 예약 작업이 찾아서 post["link"]에 넣고 push
   (못 찾으면 link_note를 남김). 사용자가 미리보기에 답장으로 링크를 보내도 된다
5) 링크가 들어오면 텔레그램으로 알리고, ✅ 승인 + 예약 시각이 되면 게시
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re

import requests
from PIL import Image

import main as M

INBOX = M.ROOT / "state" / "inbox"
SLOTS = ["12:30", "19:30", "21:30"]      # 광고 글 기본 시간대 (승인은 별도)
SETTLE_SEC = 30                          # 앨범 파일이 다 들어올 때까지 기다리는 시간


# ---------------------------------------------------------------- 1·2 받기
def _download(file_id: str, dest) -> None:
    info = M.tg("getFile", {"file_id": file_id})
    token = M.env("TELEGRAM_BOT_TOKEN")
    r = requests.get(f"{M.TG_API}/file/bot{token}/{info['file_path']}", timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)


def _parse_caption(cap: str) -> tuple[str, str]:
    cap = (cap or "").strip()
    if not cap:
        return "", ""
    parts = re.split(r"\s*/\s*|\n", cap, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def _new_id(name: str) -> str:
    base = M.now().strftime("%Y%m%d-cp%H%M%S")
    return base


def load_inbox(pid: str) -> dict | None:
    p = INBOX / f"{pid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_inbox(item: dict):
    INBOX.mkdir(parents=True, exist_ok=True)
    (INBOX / f"{item['id']}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


def all_inbox() -> list[dict]:
    if not INBOX.exists():
        return []
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(INBOX.glob("*.json"))]


def handle_media_message(msg: dict, st: dict) -> bool:
    """사진/영상 메시지면 받아서 inbox에 넣고 True."""
    photo = msg.get("photo")
    video = msg.get("video")
    doc = msg.get("document") or {}
    is_img_doc = (doc.get("mime_type") or "").startswith("image/")
    is_vid_doc = (doc.get("mime_type") or "").startswith("video/")
    if not (photo or video or is_img_doc or is_vid_doc):
        return False
    gid = msg.get("media_group_id")
    groups = st.setdefault("media_groups", {})
    pid = groups.get(gid) if gid else None
    item = load_inbox(pid) if pid else None
    if not item:
        pid = _new_id("")
        while load_inbox(pid) or M.load_post(pid):
            pid = pid + "x"
        item = {"id": pid, "name": "", "memo": "", "files": [], "thumbs": [], "status": "collecting",
                "created_at": M.now().isoformat()}
        if gid:
            groups[gid] = pid
    name, memo = _parse_caption(msg.get("caption", ""))
    if name and not item["name"]:
        item["name"], item["memo"] = name, memo
    out = M.MEDIA_DIR / pid
    out.mkdir(parents=True, exist_ok=True)
    n = len(item["files"]) + 1
    try:
        if photo or is_img_doc:
            fid = photo[-1]["file_id"] if photo else doc["file_id"]
            raw = out / f"raw{n}"
            _download(fid, raw)
            img = Image.open(raw)
            img = img.convert("RGB")
            dest = out / f"{n}.jpg"
            img.save(dest, "JPEG", quality=92)
            raw.unlink()
            item["files"].append(str(dest.relative_to(M.ROOT)))
            item["thumbs"].append(str(dest.relative_to(M.ROOT)))
        else:
            v = video or doc
            if (v.get("file_size") or 0) > 20 * 1024 * 1024:
                M.tg_text("⚠️ 영상이 20MB를 넘어서 받을 수 없어요. 짧게 자르거나 화질을 낮춰서 다시 보내 주세요.")
                return True
            dest = out / f"{n}.mp4"
            _download(v["file_id"], dest)
            item["files"].append(str(dest.relative_to(M.ROOT)))
            th = (v.get("thumbnail") or v.get("thumb") or {}).get("file_id")
            if th:
                tdest = out / f"thumb{n}.jpg"
                _download(th, tdest)
                item["thumbs"].append(str(tdest.relative_to(M.ROOT)))
    except Exception as e:
        M.tg_text(f"⚠️ 파일 받기 실패: {str(e)[:200]}")
        return True
    item["last_file_at"] = M.now().isoformat()
    if len(item["files"]) == 1 and not item.get("ack_msg_id"):
        if item["name"]:
            item["ack_msg_id"] = M.tg_text(f"📥 [{pid}] 접수: {item['name']}\n글 쓰는 중이에요. 쿠팡 링크는 따로 찾아서 붙일게요.")
        else:
            item["ack_msg_id"] = M.tg_text(f"📥 [{pid}] 사진 받았어요. 상품명을 이 메시지에 답장으로 보내 주세요.\n(예: 다이소 실리콘 냄비받침 / 한 달째 쓰는 중)")
    save_inbox(item)
    return True


def handle_name_reply(msg: dict) -> bool:
    """상품명 요청 메시지에 답장으로 온 이름."""
    rt = (msg.get("reply_to_message") or {}).get("message_id")
    text = (msg.get("text") or "").strip()
    if not rt or not text:
        return False
    for item in all_inbox():
        if item.get("ack_msg_id") == rt and item["status"] == "collecting" and not item["name"]:
            item["name"], item["memo"] = _parse_caption(text)
            save_inbox(item)
            M.tg_text(f"📝 [{item['id']}] 상품명: {item['name']} · 글 쓰는 중이에요.")
            return True
    return False


# ---------------------------------------------------------------- 3 글 쓰기
WRITER_SYSTEM = """너는 스레드 계정 '득템연구소(@gotitem.lab)' 운영자다. 사용자가 직접 찍은 실물 사진(또는 영상 장면)과 상품명을 보고 쿠팡 파트너스 글을 쓴다.

말투와 형식
- 반말, 친구한테 말하듯이. 사람이 쓴 것처럼 자연스럽게. ㅋㅋ는 가끔, 이모지는 0~1개
- 본문 4~5줄. 첫 줄은 20자 이내 후킹(상황 공감, 의외의 포인트, 질문 등). 같은 문장 틀 반복 금지
- 점(·)이나 번호 목록, 제목 같은 문장 금지. 말하듯이 줄바꿈만
- 댓글 3개: ① 이 물건이 필요한 상황·생활 속 장면 ② 사진에서 보이는 특징이나 쓰는 법 + 질문 하나 ③ 링크 바로 앞에 붙을 짧은 한 줄(예: "궁금한 사람 여기~")
- 대가성 문구와 링크는 시스템이 자동으로 붙이니 쓰지 않는다

사실 규칙 (꼭 지킨다)
- 사용자는 이 물건을 실제로 갖고 있다(직접 찍은 사진). "샀다", "집에 들였다" 정도는 써도 된다
- 써보니 어떻다는 경험(성능, 효과, 맛, 내구성)은 '사용자 메모'에 있는 내용만 쓴다. 메모가 없으면 사진에서 보이는 것(디자인, 크기감, 구성, 색, 질감)과 일반적인 쓰임새만 말한다
- 가격, 할인율, 판매량, 후기 수, 최저가, 품절 임박은 쓰지 않는다(확인 안 된 숫자 금지)
- 효능·건강·치료 효과 주장 금지. 다른 브랜드 비하 금지

출력은 JSON 하나만: {"text": "본문", "replies": ["댓글1", "댓글2", "링크 앞 한 줄"]}"""


def _img_block(path) -> dict:
    img = Image.open(path).convert("RGB")
    img.thumbnail((1280, 1280))
    b = io.BytesIO()
    img.save(b, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(b.getvalue()).decode()}}


def write_copy(item: dict) -> dict:
    key = M.env("ANTHROPIC_API_KEY")
    content = [_img_block(M.ROOT / p) for p in item["thumbs"][:5]]
    note = item["memo"] or "(없음)"
    vid = " (영상 장면 캡처 포함)" if any(f.endswith(".mp4") for f in item["files"]) else ""
    content.append({"type": "text", "text": f"상품명: {item['name']}\n사용자 메모: {note}\n사진{vid}을 보고 글을 써줘."})
    body = {"model": os.environ.get("WRITER_MODEL", "claude-sonnet-5-5"), "max_tokens": 1200,
            "system": WRITER_SYSTEM, "messages": [{"role": "user", "content": content}]}
    hdr = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    r = requests.post(f"{M.ANTHROPIC_API}/v1/messages", json=body, timeout=120, headers=hdr)
    if r.status_code in (400, 404) and "model" in r.text:
        body["model"] = M.ANTHROPIC_MODEL
        r = requests.post(f"{M.ANTHROPIC_API}/v1/messages", json=body, timeout=120, headers=hdr)
    j = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"글 생성 실패: {str(j)[:300]}")
    out = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    try:
        data = json.loads(m.group(0) if m else out, strict=False)
    except Exception:
        raise RuntimeError(f"글 형식을 못 읽었어요: {out[:200]}")
    if not data.get("text") or not isinstance(data.get("replies"), list):
        raise RuntimeError(f"글 형식이 이상해요: {out[:200]}")
    return data


def next_slot() -> dt.datetime:
    """오늘·내일 광고 시간대 중 다른 광고 글과 겹치지 않는 가장 빠른 자리 (지금부터 1시간 이후)."""
    t = M.now() + dt.timedelta(hours=1)
    taken = {M.parse_time(p["scheduled_at"]).strftime("%Y-%m-%d %H:%M")
             for p in M.all_posts() if p.get("source") != "none" and p["status"] in M.ACTIVE | {"published"}}
    for d in range(0, 7):
        day = (t + dt.timedelta(days=d)).date()
        for s in SLOTS:
            hh, mm = map(int, s.split(":"))
            cand = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=M.KST)
            if cand >= t and cand.strftime("%Y-%m-%d %H:%M") not in taken:
                return cand
    return t


def process_inbox() -> bool:
    changed = False
    for item in all_inbox():
        if item["status"] != "collecting" or not item["files"] or not item["name"]:
            continue
        if (M.now() - M.parse_time(item.get("last_file_at"))).total_seconds() < SETTLE_SEC:
            continue
        pid = item["id"]
        try:
            copy = write_copy(item)
        except Exception as e:
            item["attempts"] = item.get("attempts", 0) + 1
            M.log("글 쓰기 실패:", pid, str(e)[:300])
            if item["attempts"] >= 3:
                item["status"] = "failed"
                M.safe_notify(f"❌ [{pid}] 글 쓰기 실패: {str(e)[:300]}")
            save_inbox(item)
            changed = True
            continue
        d = {"id": pid, "account": "A", "source": "coupang", "mode": "experience",
             "experience_note": item["memo"] or "사용자 실물 사진 제공",
             "scheduled_at": next_slot().strftime("%Y-%m-%dT%H:%M"),
             "text": copy["text"], "replies": copy["replies"][:3], "images": item["files"]}
        errs, warns = M.lint(d)
        if errs:
            M.safe_notify(f"❌ [{pid}] 자동 글이 검사에 걸렸어요: {' / '.join(errs)}\n원문:\n{copy['text']}")
            item["status"] = "failed"
            save_inbox(item)
            changed = True
            continue
        main, chain = M.build_texts({**d, "link": None})
        post = {
            "id": pid, "account": "A", "source": "coupang", "mode": "experience",
            "scheduled_at": M.parse_time(d["scheduled_at"]).isoformat(),
            "main_text": main, "reply_texts": chain, "reply_ids": [], "link": "",
            "images": item["files"], "warnings": warns, "experience_note": d["experience_note"],
            "status": "pending", "approved": False, "created_at": M.now().isoformat(), "reply_attempts": 0,
            "coupang_query": item["name"], "origin": "photo",
        }
        post["preview_msg_id"] = M.tg_text(M.preview_text(post) + "\n\n🔎 쿠팡 링크는 찾는 대로 붙여요. 직접 넣으려면 이 메시지에 답장으로 보내 주세요.",
                                          reply_markup=M.buttons(pid))
        M.save_post(post)
        item["status"] = "drafted"
        save_inbox(item)
        changed = True
    return changed


# ---------------------------------------------------------------- 5 링크 알림
def announce_links() -> bool:
    changed = False
    for p in M.all_posts():
        if p.get("origin") != "photo" or p["status"] not in ("pending", "approved"):
            continue
        if p.get("link") and not p.get("link_announced"):
            extra = ""
            if p.get("coupang_title"):
                extra = f"\n상품: {p['coupang_title']}" + (f" · {p['coupang_price']:,}원" if p.get("coupang_price") else "")
            nxt = "승인돼 있어서 시간 되면 게시해요" if p.get("approved") else "✅ 승인을 누르면 예약 시간에 게시돼요"
            M.tg_text(f"🔗 [{p['id']}] 쿠팡 링크 찾았어요{extra}\n{p['link']}\n{nxt}", reply_to=p.get("preview_msg_id"))
            p["link_announced"] = True
            M.save_post(p)
            changed = True
        elif not p.get("link") and p.get("link_note") and not p.get("link_note_announced"):
            M.tg_text(f"🔎 [{p['id']}] {p['link_note']}\n파트너스에서 링크를 만들어 미리보기에 답장으로 보내 주세요.", reply_to=p.get("preview_msg_id"))
            p["link_note_announced"] = True
            M.save_post(p)
            changed = True
    return changed
