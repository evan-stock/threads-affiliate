# 득템연구소·찬복 스레드 제휴 — Claude Code 작업 설명서

이 저장소는 claude.ai 채팅(폰·클라우드)과 PC의 Claude Code가 **같이 쓰는** 저장소다. 지금 돌아가는 파이프라인(GitHub Actions 렌더·게시·예약)이 기준이며, 이 파일과 `.claude/`는 그 위에 얹은 안내일 뿐이다.

## 꼭 지킬 규칙
1. **시작할 때 `git pull`, 끝나면 `git push`.** 채팅 쪽에서도 같은 저장소에 수시로 커밋한다. 받지 않고 고치면 덮어쓰기·충돌이 난다. 충돌이 나면 임의로 해결하지 말고 사용자에게 묻는다.
2. **기존 파이프라인을 바꾸지 않는다.** `.github/workflows/`, 게시·예약 스크립트, `state/`는 사용자가 명시적으로 요청할 때만 고친다.
3. **push가 곧 실행이다.** 아래 경로에 push하면 GitHub Actions가 바로 렌더·게시를 시작한다. 게시까지 이어지는 파일은 사용자 확인 후에 올린다.
   - 실행 경로: `drafts/**`(원고 접수 → ingest), `docs/**`(운영판 페이지). 예약 게시는 poster 워크플로가 매시간 확인한다.
4. **키는 `.env`에만.** 이 저장소는 public이다. 토큰·키를 코드나 커밋에 넣지 않는다(`.env`는 .gitignore에 있음). GitHub Actions용 키는 GitHub Secrets에 있다.

## 스킬 (PC의 Claude Code에서 작업할 때)
채팅에서는 계정 스킬을 그대로 쓴다. Claude Code에서는 작업 방법이 `.claude/skills/`에 있다: gotitem-threads-ops
- 이 스킬들은 claude.ai 계정 스킬의 **복사본**이다. 원본이 바뀌면 채팅에서 "저장소 복사본도 업데이트"를 요청해 맞춘다.
- 스킬에 나오는 `/home/claude/<저장소>` 경로는 클라우드 기준이다. 여기서는 **이 저장소 폴더**로 읽는다.
- `add_repo`, `register_repo_root`(저장소 연결) 단계는 건너뛴다. 이미 이 폴더가 저장소다.
- `SendUserFile`, `Artifact`(채팅 전용 도구)가 나오면 결과 파일 경로를 알려 주는 것으로 대신한다.

## 렌더는 어디서 도나
기본은 지금처럼 **GitHub Actions**(원고 JSON을 push → 워크플로가 렌더). 다만 영상 다운로드·긴 렌더가 Actions에서 막히거나 느릴 때는 같은 스크립트를 PC에서 직접 돌려도 된다(`pip install -r requirements.txt`, 필요한 키는 `.env`).
