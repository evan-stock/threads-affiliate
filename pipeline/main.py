"""스레드 제휴 링크 자동 게시 파이프라인.

명령:
  ping                 텔레그램·스레드 연결 확인
  ingest [--force]     drafts/*.json 원고를 게시 대기열로 넣고 텔레그램 미리보기 전송
  tick                 텔레그램 승인/링크 반영, 시간 된 글 게시, 만료 처리
  watch [--max-minutes N] [--redispatch]  tick을 1분마다 반복
  publish --id ID      승인·시간 무시하고 즉시 게시 (링크는 필수)
  refresh-tokens       스레드 장기 토큰 갱신 후 GitHub Secrets에 저장
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import io
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
POSTS_DIR = ROOT / "state" / "posts"
TG_STATE = ROOT / "state" / "telegram.json"
MEDIA_DIR = ROOT / "media"
DRAFTS_DIR = ROOT / "drafts"

KST = dt.timezone(dt.timedelta(hours=CONFIG.get("timezone_offset_hours", 9)))
THREADS_API = os.environ.get("THREADS_API_BASE", "https://graph.threads.net/v1.0")
THREADS_ROOT = os.environ.get("THREADS_ROOT_BASE", "https://graph.threads.net")
TG_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
POLL_SLEEP = float(os.environ.get("POLL_SLEEP", "5"))

ACTIVE = {"pending", "approved", "publishing", "reply_failed"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")


# ---------------------------------------------------------------- 공통 유틸
def now() -> dt.datetime:
    fake = os.environ.get("FAKE_NOW")
    if fake:
        return dt.datetime.fromisoformat(fake)
    return dt.datetime.now(KST)


def log(*a):
    print(f"[{now():%H:%M:%S}]", *a, flush=True)


def parse_time(s: str | None) -> dt.datetime:
    if not s:
        return now()
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=KST)
    return t


def threads_len(text: str) -> int:
    """스레드 글자 수: 이모지는 UTF-8 바이트 수로 센다."""
    n = 0
    for ch in text:
        cp = ord(ch)
        if cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0xFE00 <= cp <= 0xFE0F or cp == 0x200D:
            n += len(ch.encode("utf-8"))
        else:
            n += 1
    return n


def env(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "")
    if required and not v:
        raise RuntimeError(f"환경변수 {name} 가 없어요. GitHub Secrets에 등록했는지 확인해 주세요.")
    return v


# ---------------------------------------------------------------- 상태 파일
def post_path(pid: str) -> Path:
    return POSTS_DIR / f"{pid}.json"


def load_post(pid: str) -> dict | None:
    p = post_path(pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_post(post: dict):
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    post["updated_at"] = now().isoformat()
    post_path(post["id"]).write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")


def all_posts() -> list[dict]:
    out = []
    for f in sorted(POSTS_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            log("상태 파일 읽기 실패:", f)
    return out


def load_tg_state() -> dict:
    return json.loads(TG_STATE.read_text()) if TG_STATE.exists() else {"offset": 0}


def save_tg_state(s: dict):
    TG_STATE.parent.mkdir(parents=True, exist_ok=True)
    TG_STATE.write_text(json.dumps(s, indent=2))


# ---------------------------------------------------------------- git 동기화
def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def git_enabled() -> bool:
    return os.environ.get("GIT_SYNC") == "1"


def git_pull():
    if git_enabled():
        r = git("pull", "--rebase", "--autostash", "-q", check=False)
        if r.returncode != 0:
            log("git pull 실패:", r.stderr[:300])


DASHBOARD = ROOT / "state" / "dashboard.json"


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("[광고]"):
            return s[:60]
    return ""


def build_dashboard():
    """대시보드(docs/index.html)가 읽는 요약 파일. 글 본문 첫 줄·상태·시각만 담는다."""
    posts = []
    for p in all_posts():
        posts.append({
            "id": p["id"],
            "account": p.get("account", "A"),
            "source": p.get("source", "none"),
            "mode": p.get("mode", ""),
            "status": p.get("status", ""),
            "scheduled_at": p.get("scheduled_at", ""),
            "published_at": p.get("published_at", ""),
            "permalink": p.get("permalink", ""),
            "title": _first_line(p.get("main_text", "")),
            "replies": len(p.get("reply_texts") or []),
            "warnings": p.get("warnings") or [],
            "note": p.get("note", "") or p.get("error", ""),
        })
    c = load_comments()
    counts: dict[str, int] = {}
    per_post: dict[str, int] = {}
    for it in c.get("items", {}).values():
        st = it.get("status", "")
        counts[st] = counts.get(st, 0) + 1
        per_post[it.get("post_id", "")] = per_post.get(it.get("post_id", ""), 0) + 1
    for p in posts:
        p["comments"] = per_post.get(p["id"], 0)
    data = {
        "generated_at": now().isoformat(),
        "watch_last_check": c.get("last_check", ""),
        "comments": counts,
        "posts": posts,
    }
    rev = ROOT / "state" / "revenue.json"
    if rev.exists():
        try:
            data["revenue"] = json.loads(rev.read_text(encoding="utf-8"))
        except Exception:
            pass
    DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def git_push(msg: str):
    if not git_enabled():
        return
    try:
        build_dashboard()
    except Exception as e:  # 대시보드 실패가 게시를 막으면 안 된다
        log("대시보드 요약 실패:", e)
    git("add", "state", "media", check=False)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-q", "-m", msg)
    for i in range(5):
        if git("push", "-q", check=False).returncode == 0:
            return
        git("pull", "--rebase", "--autostash", "-q", check=False)
        time.sleep(2 + i * 2)
    raise RuntimeError("git push 5회 실패")


# ---------------------------------------------------------------- 텔레그램
def tg(method: str, data: dict | None = None, files=None, timeout=60) -> dict:
    token = env("TELEGRAM_BOT_TOKEN")
    url = f"{TG_API}/bot{token}/{method}"
    r = requests.post(url, data=data, files=files, timeout=timeout)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"텔레그램 {method} 실패: {j}")
    return j["result"]


def tg_text(text: str, reply_markup: dict | None = None, reply_to: int | None = None) -> int:
    data = {"chat_id": env("TELEGRAM_CHAT_ID"), "text": text[:4000], "disable_web_page_preview": "true"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)
    return tg("sendMessage", data)["message_id"]


def tg_photos(paths: list[Path]):
    paths = [p for p in paths if str(p).lower().endswith((".jpg", ".jpeg", ".png"))][:10]
    if not paths:
        return
    chat = env("TELEGRAM_CHAT_ID")
    if len(paths) == 1:
        with open(paths[0], "rb") as fh:
            tg("sendPhoto", {"chat_id": chat}, files={"photo": fh})
        return
    files, media = {}, []
    for i, p in enumerate(paths):
        files[f"p{i}"] = open(p, "rb")
        media.append({"type": "photo", "media": f"attach://p{i}"})
    try:
        tg("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)}, files=files)
    finally:
        for fh in files.values():
            fh.close()


def safe_notify(text: str):
    try:
        tg_text(text)
    except Exception as e:
        log("텔레그램 알림 실패:", e)


# ---------------------------------------------------------------- 스레드 API
def acct_creds(account: str) -> tuple[str, str]:
    # 사용자 ID를 따로 등록하지 않으면 토큰 주인(me)으로 게시
    uid = os.environ.get(f"THREADS_USER_ID_{account}") or "me"
    return uid, env(f"THREADS_TOKEN_{account}")


def th_post(account: str, path: str, data: dict) -> dict:
    uid, token = acct_creds(account)
    r = requests.post(f"{THREADS_API}/{uid}/{path}", data={**data, "access_token": token}, timeout=60)
    j = r.json()
    if r.status_code >= 400 or "error" in j:
        raise RuntimeError(f"스레드 {path} 실패 ({r.status_code}): {j}")
    return j


def th_get(account: str, obj_id: str, fields: str) -> dict:
    _, token = acct_creds(account)
    r = requests.get(f"{THREADS_API}/{obj_id}", params={"fields": fields, "access_token": token}, timeout=60)
    j = r.json()
    if r.status_code >= 400 or "error" in j:
        raise RuntimeError(f"스레드 조회 실패 ({r.status_code}): {j}")
    return j


def wait_container(account: str, cid: str, limit_s: int = 300):
    t0 = time.time()
    while True:
        j = th_get(account, cid, "status,error_message")
        st = j.get("status")
        if st in ("FINISHED", "PUBLISHED"):
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"컨테이너 {cid} 처리 실패: {j}")
        if time.time() - t0 > limit_s:
            raise RuntimeError(f"컨테이너 {cid} 처리 시간 초과 (상태 {st})")
        time.sleep(POLL_SLEEP)


def publish_container(account: str, cid: str) -> str:
    wait_container(account, cid)
    # 스레드가 가끔 방금 만든 컨테이너를 "Media Not Found"로 거절함 → 잠시 뒤 다시 시도
    for i in range(3):
        try:
            return th_post(account, "threads_publish", {"creation_id": cid})["id"]
        except RuntimeError as e:
            if ("4279009" not in str(e) and "Media Not Found" not in str(e)) or i == 2:
                raise
            log(f"게시 재시도 {i + 1}/2 (Media Not Found)")
            time.sleep(20 * (i + 1))
            wait_container(account, cid)


def with_retry(make, tries: int = 2):
    """컨테이너 생성부터 다시 하는 재시도 (게시 전에 실패한 경우만 → 중복 게시 없음)."""
    for i in range(tries):
        try:
            return make()
        except RuntimeError as e:
            if ("4279009" not in str(e) and "Media Not Found" not in str(e)) or i == tries - 1:
                raise
            log("컨테이너를 새로 만들어 다시 시도")
            time.sleep(15)


def create_main(account: str, text: str, image_urls: list[str]) -> str:
    return with_retry(lambda: _create_main(account, text, image_urls))


def _media_fields(url: str) -> dict:
    if url.lower().endswith(".mp4"):
        return {"media_type": "VIDEO", "video_url": url}
    return {"media_type": "IMAGE", "image_url": url}


def _create_main(account: str, text: str, image_urls: list[str]) -> str:
    if not image_urls:
        cid = th_post(account, "threads", {"media_type": "TEXT", "text": text})["id"]
    elif len(image_urls) == 1:
        cid = th_post(account, "threads", {**_media_fields(image_urls[0]), "text": text})["id"]
        if image_urls[0].endswith(".mp4"):
            wait_container(account, cid, 600)
    else:
        children = []
        for u in image_urls[:20]:
            c = th_post(account, "threads", {**_media_fields(u), "is_carousel_item": "true"})["id"]
            children.append(c)
        for c in children:
            wait_container(account, c)
        cid = th_post(account, "threads", {"media_type": "CAROUSEL", "children": ",".join(children), "text": text})["id"]
    return publish_container(account, cid)


def create_reply(account: str, reply_to: str, text: str) -> str:
    def make():
        cid = th_post(account, "threads", {"media_type": "TEXT", "text": text, "reply_to_id": reply_to})["id"]
        return publish_container(account, cid)
    return with_retry(make)


# ---------------------------------------------------------------- 원고 → 게시물
def build_texts(d: dict) -> tuple[str, list[str]]:
    """본문과 댓글 체인. 링크와 정식 대가성 문구는 마지막 댓글에 자동으로 붙는다."""
    src = CONFIG["sources"][d["source"]]
    acct = CONFIG["accounts"][d["account"]]
    if d["source"] == "none":
        return d["text"].strip(), [r.strip() for r in (d.get("replies") or []) if r and r.strip()]
    main = d["text"].strip()
    if acct.get("persona_notice"):
        main += "\n\n" + acct["persona_notice"]
    if CONFIG.get("header_position", "top") == "bottom":
        main = main + "\n\n" + src["header"]
    else:
        main = src["header"] + "\n\n" + main
    chain = [r.strip() for r in (d.get("replies") or []) if r and r.strip()]
    if chain:
        last_intro = chain.pop()
    else:
        last_intro = (d.get("reply_text") or CONFIG["default_reply_intro"]).strip()
    link = d.get("link") or "{링크}"
    chain.append(f"{src['disclosure']}\n\n{last_intro}\n{link}")
    return main, chain


def lint(d: dict) -> tuple[list[str], list[str]]:
    """(오류, 경고)"""
    errs, warns = [], []
    if not ID_RE.match(str(d.get("id", ""))):
        errs.append("id는 영문·숫자·-_ 2~40자로 적어 주세요")
    if d.get("account") not in CONFIG["accounts"]:
        errs.append(f"account는 {list(CONFIG['accounts'])} 중 하나")
    if d.get("source") not in CONFIG["sources"]:
        errs.append(f"source는 {list(CONFIG['sources'])} 중 하나")
    if d.get("source") == "none":
        if d.get("mode") != "talk":
            errs.append("일반 글(source none)은 mode를 talk로 적어 주세요")
        if d.get("link"):
            errs.append("일반 글에는 제휴 링크를 넣지 않아요")
    elif d.get("mode") not in ("curation", "experience"):
        errs.append("mode는 curation 또는 experience")
    if not (d.get("text") or "").strip():
        errs.append("text(본문)가 비어 있어요")
    if errs:
        return errs, warns
    text = d["text"]
    if d["mode"] == "talk":
        pass
    elif d["mode"] == "experience":
        if not (d.get("experience_note") or "").strip():
            errs.append("경험담 모드는 experience_note(실제 사용 메모)가 필요해요")
    else:
        hit = [w for w in CONFIG["curation_banned_phrases"] if w in text]
        if hit:
            errs.append(f"큐레이션 모드인데 직접 써본 듯한 표현이 있어요: {', '.join(hit)}")
    main, chain = build_texts(d)
    alltext = text + "\n" + "\n".join(chain)
    claims = [w for w in CONFIG["claim_warning_words"] if w in alltext]
    if claims:
        warns.append(f"효능·건강 표현 확인 필요: {', '.join(claims)}")
    if d["mode"] == "curation":
        hit = [w for w in CONFIG["curation_banned_phrases"] if w in "\n".join(chain)]
        if hit:
            errs.append(f"큐레이션 모드인데 댓글에 직접 써본 듯한 표현이 있어요: {', '.join(hit)}")
    ad = [w for w in CONFIG.get("ad_words", []) if w in alltext]
    if ad:
        warns.append(f"광고 느낌 단어: {', '.join(ad)}")
    first = text.strip().splitlines()[0] if text.strip() else ""
    if threads_len(first) > CONFIG.get("hook_max_len", 20):
        warns.append(f"첫 줄이 {threads_len(first)}자예요 ({CONFIG.get('hook_max_len', 20)}자 이내 권장)")
    mx = CONFIG["max_text_len"]
    if threads_len(main) > mx:
        errs.append(f"본문이 {threads_len(main)}자로 {mx}자를 넘어요 (대가성 문구 포함)")
    for i, r in enumerate(chain, 1):
        if threads_len(r.replace("{링크}", "x" * 60)) > mx:
            errs.append(f"댓글 {i}이 {mx}자를 넘어요")
    if len(chain) > 5:
        errs.append("댓글은 최대 5개까지")
    if len(d.get("images") or []) > 20:
        errs.append("이미지는 최대 20장")
    if d.get("link"):
        ok, msg = check_link(d["source"], d["link"])
        if not ok:
            errs.append(msg)
        elif msg:
            warns.append(msg)
    if d.get("compare"):
        import compare as cmpmod
        errs += cmpmod.validate(d["compare"])
    try:
        parse_time(d.get("scheduled_at"))
    except Exception:
        errs.append("scheduled_at 형식 예: 2026-10-16T12:30")
    return errs, warns


def needs_link(post: dict) -> bool:
    return post.get("source") != "none"


def ready_link(post: dict) -> bool:
    return bool(post.get("link")) or not needs_link(post)


def check_link(source: str, url: str) -> tuple[bool, str]:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        return False, "링크가 URL 형식이 아니에요"
    host = u.netloc.lower()
    hosts = CONFIG["sources"][source]["link_hosts"]
    match = any(host == h or host.endswith("." + h) for h in hosts)
    if source == "coupang" and not match:
        if "coupang.com" in host:
            return False, "쿠팡 일반 상품 주소예요. 파트너스에서 만든 link.coupang.com 링크를 넣어야 수익이 잡혀요"
        return False, f"쿠팡파트너스 링크가 아니에요 ({host})"
    if source == "toss" and not match:
        return True, f"토스 쉐어링크 주소인지 확인해 주세요 ({host})"
    return True, ""


def media_base() -> str:
    base = os.environ.get("MEDIA_BASE_URL")
    if base:
        return base.rstrip("/")
    repo = env("GITHUB_REPOSITORY")
    return f"https://raw.githubusercontent.com/{repo}/{CONFIG.get('branch', 'main')}"


def fetch_images(pid: str, sources: list[str]) -> list[str]:
    """이미지를 JPEG로 저장(형식 변환만, 크롭·편집 없음). 저장소 상대 경로 목록 반환."""
    out_dir = MEDIA_DIR / pid
    out_dir.mkdir(parents=True, exist_ok=True)
    rels = []
    for i, src in enumerate(sources):
        if src.startswith(("http://", "https://")):
            r = requests.get(src, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw = r.content
        else:
            raw = (ROOT / src).read_bytes()
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB",):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        dest = out_dir / f"{i + 1}.jpg"
        img.save(dest, "JPEG", quality=92)
        if dest.stat().st_size > 8 * 1024 * 1024:
            img.save(dest, "JPEG", quality=80)
        rels.append(str(dest.relative_to(ROOT)))
    return rels


def preview_text(post: dict) -> str:
    acct = CONFIG["accounts"][post["account"]]["label"]
    src = CONFIG["sources"][post["source"]]["label"]
    when = parse_time(post["scheduled_at"]).strftime("%m/%d %H:%M")
    mode = {"curation": "큐레이션", "experience": "경험담", "talk": "일반 글"}.get(post["mode"], post["mode"])
    lines = [f"📝 [{post['id']}] {acct} · {src} · {mode} · {when} 예정", "", "── 본문 ──", post["main_text"]]
    chain = post["reply_texts"]
    for i, r in enumerate(chain, 1):
        lines += ["", f"── 댓글 {i}/{len(chain)} ──", r.replace("{링크}", post.get("link") or "🔗(링크 대기)")]
    if post.get("warnings"):
        lines += ["", "⚠️ " + "\n⚠️ ".join(post["warnings"])]
    if not ready_link(post):
        lines += ["", "🔗 이 메시지에 답장으로 링크를 보내 주세요."]
    return "\n".join(lines)


def buttons(pid: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ 승인", "callback_data": f"ok:{pid}"}, {"text": "❌ 거절", "callback_data": f"no:{pid}"}]]}


def read_drafts() -> list[dict]:
    items = []
    for f in sorted(glob.glob(str(DRAFTS_DIR / "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        j = json.loads(Path(f).read_text(encoding="utf-8"))
        items += j if isinstance(j, list) else j.get("posts", [j])
    return items


def cmd_ingest(force: bool = False, only_id: str | None = None) -> bool:
    changed = False
    problems = []
    for d in read_drafts():
        pid = str(d.get("id", ""))
        if only_id and pid != only_id:
            continue
        old = load_post(pid) if ID_RE.match(pid) else None
        if old and (not force or old["status"] in ("published", "publishing", "reply_failed")):
            continue
        errs, warns = lint(d)
        if errs:
            problems.append(f"❌ [{pid or '?'}] " + " / ".join(errs))
            continue
        main, chain = build_texts({**d, "link": None})
        try:
            images = fetch_images(pid, d.get("images") or [])
            if d.get("compare"):
                import compare as cmpmod
                out = MEDIA_DIR / pid / "compare.jpg"
                cmpmod.render(d["compare"], str(out))
                images = [str(out.relative_to(ROOT))] + images
        except Exception as e:
            problems.append(f"❌ [{pid}] 이미지 받기 실패: {e}")
            continue
        post = {
            "id": pid, "account": d["account"], "source": d["source"], "mode": d["mode"],
            "scheduled_at": parse_time(d.get("scheduled_at")).isoformat(),
            "main_text": main, "reply_texts": chain, "reply_ids": [], "link": (d.get("link") or "").strip(),
            "images": images, "warnings": warns, "experience_note": d.get("experience_note", ""),
            "status": "pending", "approved": bool(d.get("publish_now")), "created_at": now().isoformat(),
            "reply_attempts": 0,
        }
        tg_photos([ROOT / p for p in images])
        post["preview_msg_id"] = tg_text(preview_text(post), reply_markup=buttons(pid))
        if post["approved"]:
            post["status"] = "approved"
        save_post(post)
        changed = True
        log("대기열 추가:", pid)
    if problems:
        safe_notify("원고 확인이 필요해요\n" + "\n".join(problems))
    return changed


# ---------------------------------------------------------------- tick
URL_RE = re.compile(r"https?://\S+")


def toss_fast_slot(post: dict) -> dt.datetime:
    """토스 광고 승인 시 앞당길 시각: 지금 바로(같은 계정 광고와 15분 이상 띄움), 원래 예약보다 늦지 않게."""
    orig = parse_time(post["scheduled_at"])
    t = now().replace(microsecond=0)
    taken = [parse_time(p["scheduled_at"]) for p in all_posts()
             if p["id"] != post["id"] and p.get("account") == post.get("account")
             and p.get("source") != "none" and p["status"] in ("approved", "publishing", "published")]
    gap = dt.timedelta(minutes=15)
    while t < orig and any(abs(t - x) < gap for x in taken):
        t += dt.timedelta(minutes=10)
    return min(t, orig)


def handle_updates() -> bool:
    st = load_tg_state()
    chat = str(env("TELEGRAM_CHAT_ID"))
    ups = tg("getUpdates", {"offset": st["offset"], "timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])})
    changed = False
    posts_by_msg = {p.get("preview_msg_id"): p for p in all_posts() if p.get("preview_msg_id")}
    for u in ups:
        st["offset"] = u["update_id"] + 1
        changed = True
        if "callback_query" in u:
            cq = u["callback_query"]
            if str(cq["message"]["chat"]["id"]) != chat:
                continue
            act, _, pid = cq.get("data", "").partition(":")
            if act in ("cok", "cno"):
                try:
                    if act == "cok":
                        item = load_comments()["items"].get(pid, {})
                        note = send_comment_reply(pid, item.get("draft", "")) if item.get("draft") else "초안이 없어요. 답장으로 보내 주세요."
                    else:
                        note = skip_comment(pid)
                except Exception as e:
                    note = f"⚠️ 답글 실패: {str(e)[:200]}"
                try:
                    tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": note[:180]})
                except Exception:
                    pass
                tg_text(note)
                continue
            post = load_post(pid)
            note = "이미 처리된 글이에요"
            if post and post["status"] in ("pending", "approved"):
                if act == "ok":
                    post["approved"] = True
                    post["status"] = "approved"
                    if post.get("source") == "toss" and CONFIG.get("toss_publish_on_approve", True) and parse_time(post["scheduled_at"]) > now():
                        # 토스 특가는 금방 마감되니 승인하면 바로 게시
                        post["scheduled_at"] = toss_fast_slot(post).isoformat()
                    when = parse_time(post["scheduled_at"]).strftime("%m/%d %H:%M")
                    note = f"✅ [{pid}] 승인됨 · {when} 게시 예정" if ready_link(post) else f"✅ [{pid}] 승인됨 · 링크를 받으면 {when}에 게시해요"
                elif act == "no":
                    post["status"] = "rejected"
                    note = f"❌ [{pid}] 거절됨"
                save_post(post)
            try:
                tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": note[:180]})
            except Exception:
                pass
            tg_text(note)
            continue
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat:
            continue
        import photo_post as pp
        try:
            if pp.handle_media_message(msg, st) or pp.handle_name_reply(msg):
                continue
        except Exception as e:
            safe_notify(f"⚠️ 사진 접수 오류: {str(e)[:300]}")
            continue
        text = (msg.get("text") or "").strip()
        target = None
        rt = (msg.get("reply_to_message") or {}).get("message_id")
        cmt = next((cid for cid, it in load_comments()["items"].items() if it.get("msg_id") == rt), None) if rt else None
        if cmt and text:
            try:
                tg_text(send_comment_reply(cmt, text))
            except Exception as e:
                tg_text(f"⚠️ 답글 실패: {str(e)[:200]}")
            continue
        if rt in posts_by_msg:
            target = load_post(posts_by_msg[rt]["id"])
        m = re.match(r"^/link\s+(\S+)\s+(\S+)", text)
        if m:
            target = load_post(m.group(1))
        if text.startswith("/list"):
            act = [p for p in all_posts() if p["status"] in ACTIVE]
            tg_text("대기 중\n" + ("\n".join(f"· {p['id']} {p['status']} {parse_time(p['scheduled_at']):%m/%d %H:%M} {'🔗' if p.get('link') else '링크없음'}" for p in act) or "없음"))
            continue
        if target is None:
            if URL_RE.search(text):
                tg_text("어느 글의 링크인지 몰라요. 미리보기 메시지에 답장으로 보내거나 /link 글ID 링크 형식으로 보내 주세요.")
            continue
        if target["status"] not in ("pending", "approved"):
            tg_text(f"[{target['id']}] 은 이미 {target['status']} 상태라 링크를 바꿀 수 없어요.")
            continue
        urls = URL_RE.findall(text)
        if not urls:
            tg_text("링크(https://...)를 찾지 못했어요.")
            continue
        ok, why = check_link(target["source"], urls[-1])
        if not ok:
            tg_text(f"[{target['id']}] {why}")
            continue
        target["link"] = urls[-1]
        target["link_announced"] = True
        save_post(target)
        extra = f"\n⚠️ {why}" if why else ""
        state = "승인돼 있어서 시간 되면 게시해요" if target["approved"] else "✅ 승인 버튼을 누르면 게시돼요"
        tg_text(f"🔗 [{target['id']}] 링크 저장 · {state}{extra}")
    if changed:
        save_tg_state(st)
    return changed


# ---------------------------------------------------------------- 댓글 답글 (승인제)
COMMENTS = ROOT / "state" / "comments.json"
COMMENT_WINDOW_H = float(os.environ.get("COMMENT_WINDOW_HOURS", "72"))
COMMENT_EVERY_MIN = float(os.environ.get("COMMENT_EVERY_MIN", "10"))
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_API = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com")


def load_comments() -> dict:
    return json.loads(COMMENTS.read_text(encoding="utf-8")) if COMMENTS.exists() else {"last_check": "", "items": {}}


def save_comments(c: dict):
    COMMENTS.parent.mkdir(parents=True, exist_ok=True)
    COMMENTS.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


def recent_published(include_remote: bool = False) -> list[dict]:
    t = now()
    out = []
    for p in all_posts():
        if p.get("status") == "published" and p.get("main_id") and p.get("published_at"):
            if t - parse_time(p["published_at"]) <= dt.timedelta(hours=COMMENT_WINDOW_H):
                out.append(p)
    if include_remote:
        # 앱에서 직접 올린 글도 댓글을 지켜본다 (계정별 최근 글)
        known = {p["main_id"] for p in out}
        for acc in CONFIG["accounts"]:
            if not os.environ.get(f"THREADS_TOKEN_{acc}"):
                continue
            try:
                uid, _ = acct_creds(acc)
                items = th_get(acc, f"{uid}/threads", "id,text,timestamp,permalink").get("data", [])
            except Exception as e:
                log("최근 글 조회 실패:", acc, str(e)[:200])
                continue
            for it in items:
                mid = str(it.get("id"))
                if mid in known or not it.get("timestamp"):
                    continue
                if t - parse_time(it["timestamp"]) > dt.timedelta(hours=COMMENT_WINDOW_H):
                    continue
                out.append({"id": f"th-{acc}-{mid}", "account": acc, "main_id": mid,
                            "main_text": it.get("text") or "", "published_at": it["timestamp"],
                            "permalink": it.get("permalink", ""), "remote": True})
    return out


def fetch_comments(post: dict) -> list[dict]:
    j = th_get(post["account"], f"{post['main_id']}/conversation",
               "id,text,username,timestamp,is_reply_owned_by_me")
    return [c for c in j.get("data", []) if not c.get("is_reply_owned_by_me") and (c.get("text") or "").strip()]


REPLY_SYSTEM = """너는 스레드 계정 '득템연구소(@gotitem.lab)' 운영자로서 내 글에 달린 댓글에 답글을 쓴다.
- 반말, 짧게 1~2문장, 친근하게. ㅋㅋ는 가끔. 이모지는 0~1개.
- 상품 정보는 아래 '내 글'에 있는 사실만 말한다. 모르는 건 모른다고 하거나 상품 페이지에서 확인해보라고 한다.
- 먹어보지 않은 상품의 맛·효과를 단정하지 않는다 (글이 경험담이면 그 경험 범위 안에서만).
- 건강·효능 주장, 다른 쇼핑몰과의 가격 비교 단정, 품절 임박 같은 근거 없는 재촉은 하지 않는다.
- 링크를 새로 붙이지 않는다. 구매처를 물으면 '댓글에 링크 있어~'처럼 안내한다.
- 악플이나 시비에는 차분하게 짧게 답하거나, 답할 가치가 없으면 SKIP 한 단어만 출력한다.
- 상대가 '스하리'(팔로우·하트·리포스트 해줬다는 뜻)를 남기면 고마워하며 '나도 스하리할게~'처럼 자연스럽게 받아준다.
- 답글 문장만 출력한다."""


REPLY_SYSTEM_B = """너는 스레드 계정 @just.chanbok의 주인 '김찬복'이다. 40대 독거남 직장인, 원룸 혼자 산다.
내 글에 달린 댓글에 답글을 쓴다.
- 반말 섞인 친근한 말투, 1~2문장. 자조적인 유머로 가볍게 받아친다. 이모지는 0~1개.
- 상대 말에 공감하고, 가능하면 짧은 펀치라인 하나. 훈계·조언·긴 설명 금지.
- 내 글에 없는 사실(직업 세부, 사는 곳, 가족 정보)을 새로 지어내지 않는다.
- 광고 글이면 상품 얘기는 글에 있는 사실만, 링크를 새로 붙이지 않는다('댓글에 링크 있음' 정도).
- 상대가 '스하리'(팔로우·하트·리포스트 해줬다는 뜻)를 남기면 고마워하며 '나도 스하리할게~'처럼 받아준다.
- 악플·스팸·이상한 홍보에는 SKIP 한 단어만 출력한다.
- 답글 문장만 출력한다."""


def draft_reply(post: dict, comment: dict) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return ""
    system = REPLY_SYSTEM_B if post.get("account") == "B" else REPLY_SYSTEM
    body = {
        "model": ANTHROPIC_MODEL, "max_tokens": 200, "system": system,
        "messages": [{"role": "user", "content":
            f"내 글:\n{post['main_text']}\n\n댓글 (@{comment.get('username', '?')}):\n{comment['text']}\n\n답글:"}],
    }
    r = requests.post(f"{ANTHROPIC_API}/v1/messages", json=body, timeout=60,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    j = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"답글 초안 생성 실패: {str(j)[:300]}")
    return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()


def comment_buttons(cid: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ 답글 보내기", "callback_data": f"cok:{cid}"},
                                 {"text": "❌ 무시", "callback_data": f"cno:{cid}"}]]}


def check_comments(force: bool = False) -> bool:
    store = load_comments()
    last = store.get("last_check")
    if not force and last and now() - parse_time(last) < dt.timedelta(minutes=COMMENT_EVERY_MIN):
        return False
    store["last_check"] = now().isoformat()
    # API 키가 없던 때 들어온 댓글: 키가 생기면 한 번만 초안을 다시 만들어 보낸다
    if os.environ.get("ANTHROPIC_API_KEY"):
        posts_by_id = {p["id"]: p for p in all_posts()}
        for cid, item in store["items"].items():
            if item.get("status") != "pending" or item.get("draft") or item.get("redrafted"):
                continue
            post = posts_by_id.get(item.get("post_id"))
            if not post:
                continue
            item["redrafted"] = True
            try:
                d = draft_reply(post, {"username": item.get("username"), "text": item.get("text", "")})
            except Exception as e:
                log(str(e)[:200])
                continue
            if not d or d.strip().upper() == "SKIP":
                continue
            item["draft"] = d
            lines = [f"💬 [{post['id']}] @{item.get('username', '')}", item.get("text", ""), "",
                     "✏️ 답글 초안", d, "", "다르게 쓰려면 이 메시지에 답장으로 보내 주세요."]
            item["msg_id"] = tg_text("\n".join(lines), reply_markup=comment_buttons(cid))
    for post in recent_published(include_remote=True):
        try:
            comments = fetch_comments(post)
        except Exception as e:
            log("댓글 조회 실패:", post["id"], str(e)[:200])
            continue
        for c in comments:
            cid = str(c["id"])
            if cid in store["items"]:
                continue
            try:
                draft = draft_reply(post, c)
            except Exception as e:
                draft = ""
                log(str(e)[:200])
            item = {"post_id": post["id"], "account": post["account"], "username": c.get("username", ""),
                    "text": c["text"], "draft": "" if draft.strip().upper() == "SKIP" else draft,
                    "status": "pending", "created_at": now().isoformat()}
            warn = [w for w in CONFIG["claim_warning_words"] if w in item["draft"]]
            who = CONFIG["accounts"].get(post["account"], {}).get("label", post["account"])
            snippet = (post.get("main_text") or "").strip().splitlines()[0][:30] if (post.get("main_text") or "").strip() else post["id"]
            lines = [f"💬 [{who}] '{snippet}' 글에 @{item['username']}", item["text"], ""]
            if item["draft"]:
                lines += ["✏️ 답글 초안", item["draft"]]
                if warn:
                    lines += ["", "⚠️ 표현 확인: " + ", ".join(warn)]
                lines += ["", "다르게 쓰려면 이 메시지에 답장으로 보내 주세요."]
            else:
                lines += ["(초안 없음) 답글을 쓰려면 이 메시지에 답장으로 보내 주세요."]
            item["msg_id"] = tg_text("\n".join(lines), reply_markup=comment_buttons(cid) if item["draft"] else None)
            store["items"][cid] = item
    save_comments(store)
    return True


def send_comment_reply(cid: str, text: str) -> str:
    store = load_comments()
    item = store["items"].get(cid)
    if not item or item["status"] != "pending":
        return "이미 처리된 댓글이에요"
    rid = create_reply(item["account"], cid, text)
    item.update({"status": "sent", "reply": text, "reply_id": rid, "sent_at": now().isoformat()})
    save_comments(store)
    return f"✅ @{item['username']}에게 답글 달았어요"


def skip_comment(cid: str) -> str:
    store = load_comments()
    item = store["items"].get(cid)
    if not item or item["status"] != "pending":
        return "이미 처리된 댓글이에요"
    item["status"] = "skipped"
    save_comments(store)
    return "❌ 무시했어요"


def do_publish(post: dict, sync_msg: str = "publish"):
    post["status"] = "publishing"
    save_post(post)
    git_push(f"publishing {post['id']}")
    acc = post["account"]
    try:
        if not post.get("main_id"):
            urls = [f"{media_base()}/{p}" for p in post["images"]]
            post["main_id"] = create_main(acc, post["main_text"], urls)
            save_post(post)
        # 댓글 체인: 이미 올라간 댓글은 건너뛰고 이어서 단다 (실패 후 재시도해도 중복 없음)
        ids = post.setdefault("reply_ids", [])
        for i, r in enumerate(post["reply_texts"]):
            if i < len(ids):
                continue
            parent = ids[-1] if ids else post["main_id"]
            ids.append(create_reply(acc, parent, r.replace("{링크}", post.get("link", ""))))
            save_post(post)
            if i + 1 < len(post["reply_texts"]):
                time.sleep(float(os.environ.get("REPLY_GAP", "3")))
        try:
            post["permalink"] = th_get(acc, post["main_id"], "permalink").get("permalink", "")
        except Exception:
            post["permalink"] = ""
        post["status"] = "published"
        post["published_at"] = now().isoformat()
        save_post(post)
        tg_text(f"🚀 [{post['id']}] 게시 완료\n{post.get('permalink', '')}")
    except Exception as e:
        if post.get("main_id"):
            post["status"] = "reply_failed"
            post["reply_attempts"] = post.get("reply_attempts", 0) + 1
            if post["reply_attempts"] >= 3:
                post["status"] = "failed"
            save_post(post)
            safe_notify(f"⚠️ [{post['id']}] 본문은 올라갔는데 링크 댓글 실패 ({post['reply_attempts']}/3)\n{str(e)[:500]}\n" + ("직접 댓글로 링크를 달아 주세요." if post["status"] == "failed" else "다음 확인 때 다시 시도해요."))
        else:
            post["status"] = "failed"
            post["error"] = str(e)[:1000]
            save_post(post)
            safe_notify(f"❌ [{post['id']}] 게시 실패\n{str(e)[:500]}\nActions 로그를 확인해 주세요. 고친 뒤 원고를 다시 올리면 돼요.")
    git_push(f"{sync_msg} {post['id']}")


def send_requested_replies() -> bool:
    """채팅에서 Claude가 정해 둔 답글(status send_requested)을 게시한다."""
    store = load_comments()
    todo = [cid for cid, it in store["items"].items() if it.get("status") == "send_requested" and it.get("send_text")]
    for cid in todo:
        it = load_comments()["items"][cid]
        it_text = it["send_text"]
        store2 = load_comments()
        store2["items"][cid]["status"] = "pending"
        save_comments(store2)
        try:
            note = send_comment_reply(cid, it_text)
        except Exception as e:
            note = f"⚠️ 답글 실패: {str(e)[:200]}"
        safe_notify(note + f"\n└ {it_text}")
    return bool(todo)


def cmd_tick() -> bool:
    changed = handle_updates()
    try:
        changed = send_requested_replies() or changed
    except Exception as e:
        log("요청 답글 처리 오류:", str(e)[:300])
    import photo_post as pp
    for fn in (pp.process_inbox, pp.announce_links):
        try:
            changed = fn() or changed
        except Exception as e:
            log("사진 글 처리 오류:", str(e)[:300])
    try:
        changed = check_comments() or changed
    except Exception as e:
        log("댓글 확인 오류:", str(e)[:300])
    t = now()
    exp = dt.timedelta(hours=CONFIG.get("expire_hours", 12))
    for post in sorted(all_posts(), key=lambda p: p["scheduled_at"]):
        st = post["status"]
        when = parse_time(post["scheduled_at"])
        if st == "reply_failed":
            do_publish(post, "retry-reply")
            changed = True
        elif st == "approved" and ready_link(post) and t >= when:
            do_publish(post)
            changed = True
        elif st in ("pending", "approved") and t >= when + exp:
            post["status"] = "expired"
            save_post(post)
            why = "승인을" if st == "pending" else "링크를"
            safe_notify(f"⌛ [{post['id']}] {why} 받지 못해 만료됐어요.")
            changed = True
        elif st == "publishing" and post.get("main_id") is None:
            # 이전 실행이 게시 도중 끊김: 중복 게시를 막기 위해 자동 재시도하지 않음
            post["status"] = "failed"
            post["error"] = "게시 도중 실행이 끊겼어요. 스레드에 올라갔는지 확인 후 필요하면 Actions의 tools → publish로 올려 주세요."
            save_post(post)
            safe_notify(f"⚠️ [{post['id']}] {post['error']}")
            changed = True
    return changed


def has_active() -> bool:
    # 대기 글이 있거나, 최근 게시 글의 댓글을 지켜봐야 하면 감시를 계속한다
    return any(p["status"] in ACTIVE for p in all_posts()) or bool(recent_published())


def cmd_watch(max_minutes: float, redispatch: bool):
    t0 = time.time()
    errors = 0
    while True:
        try:
            git_pull()
            if cmd_tick():
                git_push("tick")
            errors = 0
        except Exception as e:
            # 한 번의 오류로 감시 전체가 죽지 않게: 알리고 다음 분에 다시 시도
            errors += 1
            log("감시 중 오류:", str(e)[:500])
            if errors in (1, 10, 60):
                safe_notify(f"⚠️ 게시 감시 오류 ({errors}회째, 계속 재시도 중)\n{str(e)[:300]}")
            git("rebase", "--abort", check=False)
            git("pull", "--rebase", "--autostash", "-q", check=False)
        if not has_active():
            log("대기 중인 글이 없어 종료")
            return
        if time.time() - t0 > max_minutes * 60:
            log("시간 한도 도달")
            if redispatch:
                dispatch_self()
            return
        time.sleep(float(os.environ.get("WATCH_INTERVAL", "60")))


def dispatch_self():
    repo = env("GITHUB_REPOSITORY")
    r = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/actions/workflows/poster.yml/dispatches", "-f", f"ref={CONFIG.get('branch', 'main')}"], capture_output=True, text=True)
    log("다음 감시 실행 예약:", "성공" if r.returncode == 0 else r.stderr[:300])


def cmd_publish(pid: str):
    post = load_post(pid)
    if not post:
        raise SystemExit(f"{pid} 없음")
    if not ready_link(post):
        raise SystemExit("링크가 없어 게시할 수 없어요")
    if post["status"] == "published":
        raise SystemExit("이미 게시됨")
    do_publish(post, "manual-publish")


def cmd_ping():
    tg_text("🏓 텔레그램 연결 OK")
    for acc in CONFIG["accounts"]:
        if not os.environ.get(f"THREADS_TOKEN_{acc}"):
            safe_notify(f"계정 {acc}: 토큰 미등록")
            continue
        try:
            uid, token = acct_creds(acc)
            j = requests.get(f"{THREADS_API}/me", params={"fields": "id,username", "access_token": token}, timeout=30).json()
            if not j.get("id"):
                tg_text(f"계정 {acc}: ⚠️ 연결 실패 {str(j)[:300]}")
                continue
            ok = uid == "me" or str(j.get("id")) == str(uid)
            tg_text(f"계정 {acc}: @{j.get('username')} " + ("연결 OK" if ok else f"⚠️ 사용자 ID 불일치 ({j.get('id')} ≠ {uid})"))
        except Exception as e:
            safe_notify(f"계정 {acc}: 연결 실패 {e}")


def cmd_refresh():
    for acc in CONFIG["accounts"]:
        token = os.environ.get(f"THREADS_TOKEN_{acc}")
        if not token:
            continue
        r = requests.get(f"{THREADS_ROOT}/refresh_access_token", params={"grant_type": "th_refresh_token", "access_token": token}, timeout=30)
        j = r.json()
        new = j.get("access_token")
        if not new:
            safe_notify(f"⚠️ 계정 {acc} 토큰 갱신 실패: {str(j)[:300]}")
            continue
        p = subprocess.run(["gh", "secret", "set", f"THREADS_TOKEN_{acc}", "--repo", env("GITHUB_REPOSITORY")], input=new, capture_output=True, text=True)
        days = int(j.get("expires_in", 0)) // 86400
        if p.returncode == 0:
            log(f"계정 {acc} 토큰 갱신 완료 ({days}일)")
        else:
            safe_notify(f"⚠️ 계정 {acc} 토큰은 받았지만 Secrets 저장 실패: {p.stderr[:300]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ping", "ingest", "tick", "watch", "publish", "refresh-tokens", "dashboard"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--id")
    ap.add_argument("--max-minutes", type=float, default=340)
    ap.add_argument("--redispatch", action="store_true")
    a = ap.parse_args()
    try:
        if a.cmd == "ping":
            cmd_ping()
        elif a.cmd == "ingest":
            git_pull()
            if cmd_ingest(a.force, a.id):
                git_push("ingest drafts")
        elif a.cmd == "tick":
            git_pull()
            if cmd_tick():
                git_push("tick")
        elif a.cmd == "watch":
            cmd_watch(a.max_minutes, a.redispatch)
        elif a.cmd == "publish":
            git_pull()
            cmd_publish(a.id)
        elif a.cmd == "refresh-tokens":
            cmd_refresh()
        elif a.cmd == "dashboard":
            build_dashboard()
    except SystemExit:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        safe_notify(f"🔥 {a.cmd} 실행 오류\n{str(e)[:500]}\nActions 로그를 확인해 주세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
