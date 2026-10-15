# 스레드 제휴 링크 자동 게시

채팅에서 Claude가 원고를 쓰면 → 텔레그램으로 미리보기가 오고 → 링크를 답장으로 보내고 ✅ 누르면 → 정해진 시간에 스레드에 본문(+사진)이 올라가고, 링크와 대가성 문구가 내 댓글로 달려요.
쿠팡파트너스와 토스쇼핑 쉐어링크를 모두 지원하고, 스레드 계정 2개(A, B)를 씁니다. 컴퓨터를 켜 둘 필요는 없어요 (GitHub Actions에서 무료로 실행).

## 흐름

1. **원고**: Claude가 채팅에서 상품 고르기·글 쓰기 → `drafts/` 폴더에 원고 파일을 올림
2. **미리보기**: 텔레그램으로 사진 + 본문 + 댓글 미리보기와 [✅ 승인] [❌ 거절] 버튼이 옴
3. **링크**: 쿠팡파트너스(또는 토스 앱의 쉐어링크 공유하기)에서 링크를 만들어 **미리보기 메시지에 답장**으로 보냄
   - 또는 `/link 글ID 링크` 형식으로 보내도 돼요
   - 쿠팡 일반 상품 주소(www.coupang.com)는 수익이 안 잡혀서 거절해요
4. **게시**: 승인 + 링크가 모두 있고 예약 시간이 되면 자동 게시 → 텔레그램으로 완료 알림
5. 예약 시간 12시간 뒤까지 승인·링크가 없으면 만료돼요

텔레그램 명령: `/list` 대기 중인 글 보기

## 자동으로 붙는 것

- 본문 첫 줄: `[광고] …` 짧은 대가성 문구 (토스 정책상 댓글에만 두면 안 돼요)
- 댓글: 안내 문장 + 링크 + 정식 대가성 문구
- 계정별 `persona_notice`(config.json): AI 가상인물 프로필을 쓴다면 여기에 "AI로 만든 가상인물 계정이에요" 같은 문구를 넣어 주세요. 토스는 이 표시를 의무로 정하고 있어요.

## 원고가 거절되는 경우 (안전장치)

- **큐레이션 모드**(`mode: curation`)인데 "써봤", "샀는데", "효과 봤" 같은 직접 경험 표현이 있으면 거절
- **경험담 모드**(`mode: experience`)는 `experience_note`(실제로 쓴 내용 메모)가 없으면 거절
- 대가성 문구 포함 500자 초과 시 거절
- "치료", "여드름", "다이어트" 같은 효능·건강 표현은 미리보기에 ⚠️ 경고로 표시

## 원고 파일 형식

`drafts/아무이름.json` (`_`로 시작하는 파일은 무시돼요. 예시는 `drafts/_example.json`)

| 필드 | 설명 |
|---|---|
| id | 글 ID (영문·숫자·-_, 예: 20261016-a1) |
| account | `A` 또는 `B` |
| source | `coupang` 또는 `toss` |
| mode | `curation` 또는 `experience` |
| scheduled_at | 게시 시각 (한국시간, 예: 2026-10-16T12:30). 비우면 승인 즉시 |
| text | 본문 (대가성 문구는 자동으로 붙음) |
| images | 사진 URL 또는 저장소 안 경로 (최대 20장, 2장 이상이면 캐러셀) |
| link | 미리 알면 넣고, 비우면 텔레그램으로 받음 |
| reply_text | 댓글 첫 문장 (기본: "여기서 볼 수 있어~") |
| experience_note | 경험담 모드일 때 실제 사용 메모 |
| publish_now | true면 승인 없이 진행 (링크는 필요) |

사진은 형식만 JPEG로 바꾸고 크롭·편집은 하지 않아요 (토스 이미지 정책). 내가 찍은 사진은 `media/uploads/`에 올려서 경로로 쓰면 돼요.

---

## 설치 (한 번만)

### 1. GitHub 저장소
1. github.com에서 **공개(Public)** 저장소 만들기 (예: `threads-affiliate`) — 공개여야 Actions가 무료이고 스레드가 사진을 가져갈 수 있어요
2. 이 폴더의 파일을 전부 올리기
3. 저장소 Settings → Actions → General → Workflow permissions → **Read and write permissions** 선택 후 저장
4. Claude가 원고를 올릴 수 있게 Claude GitHub App에 이 저장소 접근 허용

### 2. 텔레그램 봇
1. 텔레그램에서 @BotFather → `/newbot` → 봇 토큰 받기
2. 만든 봇에게 아무 메시지나 보내기
3. 브라우저에서 `https://api.telegram.org/bot<봇토큰>/getUpdates` 열기 → `"chat":{"id":숫자` 의 숫자가 Chat ID

### 3. 스레드 API (계정 A, B 각각)
1. developers.facebook.com → 앱 만들기 → 사용 사례에서 **Threads API 액세스** 선택
2. 권한: `threads_basic`, `threads_content_publish`, `threads_manage_replies`
3. 앱 역할 → **Threads 테스터**로 두 계정을 초대 → 스레드 앱(설정 → 계정 → 웹사이트 권한 → 초대)에서 수락
4. Threads API 사용 사례 화면의 토큰 생성기에서 계정별 **장기 액세스 토큰**과 **사용자 ID** 받기

### 4. GitHub Secrets 등록
저장소 Settings → Secrets and variables → Actions → New repository secret
(**Name** 칸에 아래 이름, **Secret** 칸에 실제 값)

| Name | 값 |
|---|---|
| TELEGRAM_BOT_TOKEN | 봇 토큰 |
| TELEGRAM_CHAT_ID | Chat ID |
| THREADS_USER_ID_A / THREADS_TOKEN_A | 계정 A 사용자 ID / 토큰 |
| THREADS_USER_ID_B / THREADS_TOKEN_B | 계정 B 사용자 ID / 토큰 |
| GH_PAT | github.com/settings/personal-access-tokens/new 에서 이 저장소만 선택, **Secrets: Read and write** 권한으로 만든 토큰 (스레드 토큰 자동 갱신용) |

토큰은 채팅에 붙여넣지 말고 여기에만 넣어 주세요.

### 5. 시험
1. Actions → **tools** → Run workflow → cmd `ping` → 텔레그램에 "연결 OK"가 오면 성공
2. `drafts/_example.json`을 복사해 `drafts/test.json`으로 만들고, 사진·시간을 바꿔 올리기 → 텔레그램 미리보기 확인
3. 링크 답장 → ✅ → 시간 되면 게시 확인

## 워크플로

| 이름 | 하는 일 |
|---|---|
| ingest | drafts 변경 시 실행. 원고 검사 → 사진 저장 → 미리보기 전송 → poster 깨우기 |
| poster | 1분마다 승인·링크 확인, 시간 된 글 게시. 대기 글이 남으면 약 5시간 40분마다 스스로 이어서 실행. 예비로 3시간마다도 실행 |
| tools | ping / publish(즉시 게시) / refresh-tokens(매주 자동 토큰 갱신) |

## 운영 팁

- 두 계정에 같은 상품·비슷한 글을 같은 시간에 올리지 마세요. 스팸 판정 위험이 커요. 계정별 콘셉트를 나누고 시간을 엇갈리게 두는 게 좋아요.
- 스레드 API 게시 한도는 계정당 24시간 250개라 하루 5~10개는 여유 있어요.
- 게시 도중 실행이 끊기면 중복 게시를 막기 위해 자동 재시도하지 않고 텔레그램으로 알려 줘요. 스레드에서 확인 후 필요하면 tools → publish로 올리세요.
- 본문은 올라갔는데 링크 댓글만 실패하면 최대 3번 다시 시도하고, 그래도 안 되면 직접 댓글을 달라고 알려 줘요.
