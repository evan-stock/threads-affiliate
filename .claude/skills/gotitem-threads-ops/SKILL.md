---
name: "gotitem-threads-ops"
description: "득템연구소(@gotitem.lab)·찬복(@just.chanbok) 스레드 제휴 계정 운영: 토스·쿠팡 광고 글과 일반 글 작성·예약·취소, 글감 접수함 처리, 댓글 답글, 감시 재시작, 운영판 확인을 할 때 사용."
---

# 득템·찬복 스레드 운영

저장소 `evan-stock/threads-affiliate`(공개)를 GitHub Actions + 텔레그램 승인으로 돌린다. 이 스킬은 이미 만들어진 시스템을 매일 운영하는 방법이다. 처음부터 만드는 법은 sns-auto-posting-pipeline 스킬.

**새 대화를 시작하면 먼저** claude.ai 프로젝트 "스레드 쿠팡파트너스 자동화"의 `claude/현재-상황.md`(예약 현황·다음 할 일·최근 흐름)를 읽고, 사용자에게 처음부터 다시 묻지 말고 이어간다. 그다음 필요한 만큼 `claude/설계-결정.md`, `claude/원고-가이드.md`, `claude/득템-계정-콘셉트.md`, `claude/찬복-계정-콘셉트.md`를 읽는다. 프로젝트 문서가 이 스킬보다 우선한다. 큰 작업이 끝나면 `claude/현재-상황.md`를 갱신한다.

## 0. 준비
```bash
gh repo clone evan-stock/threads-affiliate ~/threads-affiliate -- -q || git -C ~/threads-affiliate pull -q --rebase --autostash
cd ~/threads-affiliate && git config user.name threads-bot && git config user.email threads-bot@users.noreply.github.com
```
- 커밋 메시지 끝에는 시스템이 주는 attribution 줄을 붙인다.
- push가 거절되면 `git pull -q --rebase --autostash` 후 재시도(감시가 계속 커밋하므로 흔함).
- gh는 REST만 된다. 워크플로 실행: `gh api -X POST repos/evan-stock/threads-affiliate/actions/workflows/<poster|ingest|pages>.yml/dispatches -f ref=main`. 특정 원고 재생성: ingest.yml에 `-f 'inputs[force]=true' -f 'inputs[id]=<id>'`.
- 기다릴 때는 sleep 대신 `until <조건>; do sleep 5; done` 루프.

## 1. 구조
- 계정: A 득템(@gotitem.lab), B 찬복(@just.chanbok). Secrets THREADS_TOKEN_A / THREADS_TOKEN_B
- `drafts/*.json` → push하면 ingest가 `state/posts/<id>.json`을 만들고 텔레그램 미리보기(✅/❌)를 보냄. 이미 있는 id는 건너뜀(force 필요)
- `poster.yml` 감시: 1분마다 git pull → 텔레그램 처리 → 예약 시각 된 승인 글 게시 → 댓글 확인 → 답글 요청 게시. 오류가 나도 루프 유지, 매시 17분 예비 실행
- 상태: pending / approved / publishing / published / reply_failed / failed / rejected / expired / held
- 광고 글은 ✅ 필요, 일반 글(`publish_now: true`)은 자동 승인
- 운영판: https://evan-stock.github.io/threads-affiliate/ (state/dashboard.json, 수익은 state/revenue.json)

## 2. 원고 JSON
```json
{"posts": [{
  "id": "20261018-xxx", "account": "A | B",
  "source": "toss | coupang | none",
  "mode": "curation | experience | talk",
  "scheduled_at": "2026-10-18T12:30",
  "text": "본문 (광고 표시는 자동으로 맨 위에 붙음)",
  "images": ["https://... 공식 썸네일 또는 media/<id>/1.jpg"],
  "link": "https://toss.shopping/... 또는 https://link.coupang.com/a/...",
  "replies": ["댓글1", "댓글2", "링크 바로 앞 한 줄"],
  "experience_note": "experience 모드일 때 실제 사용 메모",
  "publish_now": true
}]}
```
- 마지막 댓글에 정식 대가성 문구 + 링크가 자동으로 붙는다. 일반 글은 source none, mode talk, link 없음.
- 쓰기 전 검사: `python3 -c "import sys,json;sys.path.insert(0,'pipeline');import main;[print(d['id'],main.lint(d)) for d in json.load(open('drafts/파일.json'))['posts']]"` (curation 금지어 예: 쟁여두)

## 3. 글감 접수함 ("올렸어")
- 페이지 https://claude.ai/artifact/UUq7yWnTQa6MoZvp9ZHT7i, ArtifactData query `uploads` where status == "waiting"
- 종류: ad(쿠팡 실사 사진) / share(공유 링크: 토스쉐어·쿠팡 공유, `link` 필드) / daily(일상 사진)
- 사진은 Artifact read(path=asset id)로 받아 media/<id>/에 저장
- 쿠팡: 앱 브라우저로 `https://partners.coupang.com/#affiliate/ws/link/0/<검색어 URL인코딩>` → 상품 카드 hover → '링크 생성' → `https://link.coupang.com/a/...`. 목록 제목은 잘리니 가격으로 고른다. 쿠팡 앱 일반 공유 링크는 수익 안 잡히니 같은 상품 파트너스 링크로 바꾼다
- 토스쉐어: 앱 브라우저로 링크를 열어 상품명·og:image·평점·리뷰 수·순위·배송을 읽는다. 웹엔 가격이 없으니 사용자에게 받거나 뺀다
- 처리 후 문서를 status "queued", post_id, note(예약 시각)로 update(if_version 필수)

## 4. 글쓰기
- 계정별 페르소나는 프로젝트 콘셉트 문서대로. 득템 일상 5개 중 1~2개는 직업 글(세무 토막상식 숫자는 공식 자료로 확인, 단정·개인 조언 금지)
- 광고: 본문 4~5줄 + 댓글 3개, 사람 말투, 첫 줄 20자 이내. 확인한 사실만, 효과 단정 금지, 가격엔 확인 날짜
- 일반 글: 일상·취미·메뉴·드라마 등 사람 사는 얘기. 지어낸 에피소드와 구체적 지명 OK. 실제 가게 후기·광고 상품 경험은 지어내지 않는다
- 스레드 말투: 스하리 = 팔로우·하트·리포스트 해줬다는 뜻("스하리할게~"), 스친 = 스레드 친구
- 링크 글은 하루 게시의 30% 이하, 같은 계정 일반 글과 간격 두기

### 4-1. 득템 광고 첫 줄 실험 (2026-11-08~11-14, 사용자 승인 11/07)
근거: 빌더로그(@krblue_) 글 37개를 보니 "이런 것도 있더라" 식 소개는 조회 40~90, '대상 + 막아 주는 손해 + 숫자 + 반전' 사례형은 300~1,300.
- 기간 중 득템(A) 광고 글을 번갈아 쓴다: 홀수 번째는 사례형, 짝수 번째는 지금 방식. 사례형 글은 id 끝에 `-case` 를 붙여 나중에 비교한다.
- 사례형 틀(첫 줄 20자 제한·"(광고)" 자동 표시는 그대로):
  ① 첫 줄(20자 이내): 누가 + 어떤 손해를 막는지 + 숫자 하나. 예: "전기요 잘못 사면 전기료 더 냄", "자취생 수납함 3개 사다 버린 이유"
  ② 둘째 줄: "근데 차이는 A가 아니라 B였음"처럼 답을 반쯤 숨긴다
  ③ 나머지 본문 2~3줄에 숫자(용량·소비전력·크기·개수 등) 2~3개, 댓글 3개는 기존 규칙대로
- 숫자는 상품 상세페이지·공식 사양처럼 확인한 것만. 가격·할인율·판매량·후기 수·품절 임박은 기존 규칙대로 쓰지 않는다(가격을 쓸 때는 확인 날짜). 써 본 경험은 사용자 메모에 있는 것만, 효과 단정 금지.
- 11/15 이후 운영판(state/dashboard.json)의 `-case` 글과 나머지 글의 댓글·답글 수, revenue.json 클릭을 비교해 사용자에게 한 줄로 보고하고, 이 실험 항목을 '계속/중단'으로 정리한다.

## 5. 예약 글 바꾸기·취소
- 안 올라간 글은 `state/posts/<id>.json`을 직접 고쳐 push(시간: scheduled_at, 본문: main_text, 댓글: reply_texts — 광고 표시·대가성 문구는 그대로). drafts 파일도 같이 고친다
- 본문을 크게 바꿔 다시 승인받으려면 ingest force + id
- 취소: status "rejected", note에 이유

## 6. 댓글 답글
- state/comments.json `items`(키 = 댓글 id). 계정은 `account`, 원글은 `post_id`(th-<계정>-<미디어id>는 사용자가 직접 쓴 글)
- 원글 내용은 앱 브라우저로 threads.com/@계정 페이지에서 확인
- 답글: 항목에 `status: "send_requested"`, `send_text` 넣고 push → 감시가 게시 후 "sent". 사용자가 이미 단 것은 "skipped"
- 사용자가 요청하거나 OK한 답글만 단다. 남의 글 댓글은 자동화하지 않는다
- API 크레딧이 없어 자동 초안은 비어 있다(충전 여부는 사용자가 결정)

## 7. 감시 재시작 (코드를 바꿨거나 토큰을 교체한 뒤)
```bash
for id in $(gh api "repos/evan-stock/threads-affiliate/actions/runs?per_page=8" --jq '.workflow_runs[]|select(.name=="poster" and (.status=="in_progress" or .status=="queued" or .status=="pending"))|.id'); do gh api -X POST repos/evan-stock/threads-affiliate/actions/runs/$id/cancel; done
gh api -X POST repos/evan-stock/threads-affiliate/actions/workflows/poster.yml/dispatches -f ref=main
```
- 대기 중이던 실행은 예전 커밋으로 돈다. 새 코드를 쓰려면 모두 취소하고 새로 dispatch

## 8. 하지 않는 것
- 토큰·비밀번호·API 키 입력, 로그인 대행, 앱 권한 승인(사용자가 직접)
- 남의 게시물에 링크 댓글, DM 유도, 팔로워 구매·봇·다계정 조작
- 타 플랫폼 영상·사진 재업로드
- 가짜 상품 후기, 확인 안 된 가격·할인·품절 임박, 효능 주장
- 네이버 쇼핑 검색 API(종료)

## 9. 사용자 성향
- 결과만 짧게. 쉬운 말. "AI 티" 나는 문장 싫어함
- 할 수 있는 건 Claude가 직접(브라우저 포함) 하고, 사용자가 해야 하는 것만 알려 준다
- 틀린 건 바로 인정하고 고친다