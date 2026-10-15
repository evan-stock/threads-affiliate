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


def git_push(msg: str):
    if not git_enabled():
        return
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
    paths = paths[:10]
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
    return env(f"THREADS_USER_ID_{account}"), env(f"THREADS_TOKEN_{account}")


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
    return th_post(account, "threads_publish", {"creation_id": cid})["id"]


def create_main(account: str, text: str, image_urls: list[str]) -> str:
    if not image_urls:
        cid = th_post(account, "threads", {"media_type": "TEXT", "text": text})["id"]
    elif len(image_urls) == 1:
        cid = th_post(account, "threads", {"media_type": "IMAGE", "image_url": image_urls[0], "text": text})["id"]
    else:
        children = []
        for u in image_urls[:20]:
            c = th_post(account, "threads", {"media_type": "IMAGE", "image_url": u, "is_carousel_item": "true"})["id"]
            children.append(c)
        for c in children:
            wait_container(account, c)
        cid = th_post(account, "threads", {"media_type": "CAROUSEL", "children": ",".join(children), "text": text})["id"]
    return publish_container(account, cid)


def create_reply(account: str, reply_to: str, text: str) -> str:
    cid = th_post(account, "threads", {"media_type": "TEXT", "text": text, "reply_to_id": reply_to})["id"]
    return publish_container(account, cid)


# ---------------------------------------------------------------- 원고 → 게시물
def build_texts(d: dict) -> tuple[str, str]:
    src = CONFIG["sources"][d["source"]]
    acct = CONFIG["accounts"][d["account"]]
    main = src["header"] + "\n\n" + d["text"].strip()
    if acct.get("persona_notice"):
        main += "\n\n" + acct["persona_notice"]
    intro = (d.get("reply_text") or CONFIG["default_reply_intro"]).strip()
    link = d.get("link") or "{링크}"
    reply = f"{intro}\n{link}\n\n{src['disclosure']}"
    return main, reply


def lint(d: dict) -> tuple[list[str], list[str]]:
    """(오류, 경고)"""
    errs, warns = [], []
    if not ID_RE.match(str(d.get("id", ""))):
        errs.append("id는 영문·숫자·-_ 2~40자로 적어 주세요")
    if d.get("account") not in CONFIG["accounts"]:
        errs.append(f"account는 {list(CONFIG['accounts'])} 중 하나")
    if d.get("source") not in CONFIG["sources"]:
        errs.append(f"source는 {list(CONFIG['sources'])} 중 하나")
    if d.get("mode") not in ("curation", "experience"):
        errs.append("mode는 curation 또는 experience")
    if not (d.get("text") or "").strip():
        errs.append("text(본문)가 비어 있어요")
    if errs:
        return errs, warns
    text = d["text"]
    if d["mode"] == "experience":
        if not (d.get("experience_note") or "").strip():
            errs.append("경험담 모드는 experience_note(실제 사용 메모)가 필요해요")
    else:
        hit = [w for w in CONFIG["curation_banned_phrases"] if w in text]
        if hit:
            errs.append(f"큐레이션 모드인데 직접 써본 듯한 표현이 있어요: {', '.join(hit)}")
    claims = [w for w in CONFIG["claim_warning_words"] if w in text]
    if claims:
        warns.append(f"효능·건강 표현 확인 필요: {', '.join(claims)}")
    main, reply = build_texts(d)
    mx = CONFIG["max_text_len"]
    if threads_len(main) > mx:
        errs.append(f"본문이 {threads_len(main)}자로 {mx}자를 넘어요 (대가성 문구 포함)")
    if threads_len(reply.replace("{링크}", "x" * 60)) > mx:
        errs.append("댓글이 너무 길어요")
    if len(d.get("images") or []) > 20:
        errs.append("이미지는 최대 20장")
    if d.get("link"):
        ok, msg = check_link(d["source"], d["link"])
        if not ok:
            errs.append(msg)
        elif msg:
            warns.append(msg)
    try:
        parse_time(d.get("scheduled_at"))
    except Exception:
        errs.append("scheduled_at 형식 예: 2026-10-16T12:30")
    return errs, warns


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
    mode = "큐레이션" if post["mode"] == "curation" else "경험담"
    lines = [f"📝 [{post['id']}] {acct} · {src} · {mode} · {when} 예정", "", "── 본문 ──", post["main_text"], "", "── 댓글 ──", post["reply_text"].replace("{링크}", post.get("link") or "🔗(링크 대기)")]
    if post.get("warnings"):
        lines += ["", "⚠️ " + "\n⚠️ ".join(post["warnings"])]
    if not post.get("link"):
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


def cmd_ingest(force: bool = False) -> bool:
    changed = False
    problems = []
    for d in read_drafts():
        pid = str(d.get("id", ""))
        old = load_post(pid) if ID_RE.match(pid) else None
        if old and (not force or old["status"] in ("published", "publishing", "reply_failed")):
            continue
        errs, warns = lint(d)
        if errs:
            problems.append(f"❌ [{pid or '?'}] " + " / ".join(errs))
            continue
        main, reply = build_texts({**d, "link": None})
        try:
            images = fetch_images(pid, d.get("images") or [])
        except Exception as e:
            problems.append(f"❌ [{pid}] 이미지 받기 실패: {e}")
            continue
        post = {
            "id": pid, "account": d["account"], "source": d["source"], "mode": d["mode"],
            "scheduled_at": parse_time(d.get("scheduled_at")).isoformat(),
            "main_text": main, "reply_text": reply, "link": (d.get("link") or "").strip(),
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
            post = load_post(pid)
            note = "이미 처리된 글이에요"
            if post and post["status"] in ("pending", "approved"):
                if act == "ok":
                    post["approved"] = True
                    post["status"] = "approved"
                    when = parse_time(post["scheduled_at"]).strftime("%m/%d %H:%M")
                    note = f"✅ [{pid}] 승인됨 · {when} 게시 예정" if post.get("link") else f"✅ [{pid}] 승인됨 · 링크를 받으면 {when}에 게시해요"
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
        text = (msg.get("text") or "").strip()
        target = None
        rt = (msg.get("reply_to_message") or {}).get("message_id")
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
        save_post(target)
        extra = f"\n⚠️ {why}" if why else ""
        state = "승인돼 있어서 시간 되면 게시해요" if target["approved"] else "✅ 승인 버튼을 누르면 게시돼요"
        tg_text(f"🔗 [{target['id']}] 링크 저장 · {state}{extra}")
    if changed:
        save_tg_state(st)
    return changed


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
        reply_text = post["reply_text"].replace("{링크}", post["link"])
        post["reply_id"] = create_reply(acc, post["main_id"], reply_text)
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


def cmd_tick() -> bool:
    changed = handle_updates()
    t = now()
    exp = dt.timedelta(hours=CONFIG.get("expire_hours", 12))
    for post in sorted(all_posts(), key=lambda p: p["scheduled_at"]):
        st = post["status"]
        when = parse_time(post["scheduled_at"])
        if st == "reply_failed":
            do_publish(post, "retry-reply")
            changed = True
        elif st == "approved" and post.get("link") and t >= when:
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
    return any(p["status"] in ACTIVE for p in all_posts())


def cmd_watch(max_minutes: float, redispatch: bool):
    t0 = time.time()
    while True:
        git_pull()
        if cmd_tick():
            git_push("tick")
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
    if not post.get("link"):
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
            ok = str(j.get("id")) == str(uid)
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
    ap.add_argument("cmd", choices=["ping", "ingest", "tick", "watch", "publish", "refresh-tokens"])
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
            if cmd_ingest(a.force):
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
    except SystemExit:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        safe_notify(f"🔥 {a.cmd} 실행 오류\n{str(e)[:500]}\nActions 로그를 확인해 주세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
