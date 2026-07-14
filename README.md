# 매일 영어 일기 (writing-diary)

매일 영어 문장을 GitHub 파일에 입력해두면, 문장마다 그 문장을 바탕으로 짧은 영어 일기와
응용 문장이 자동으로 생성되어 매일 아침 Hugo 블로그에 게시되는 파이프라인.

사이트: https://jeonck.github.io/writing-diary/

## 어떻게 동작하나

```
input/sentence.md (오늘의 문장들, 한 줄에 하나씩, GitHub 웹 UI에서 수정)
        │
        ▼  매일 07:00 KST (GitHub Actions cron)
pipeline/generate.py
  - 코드블록 안의 각 줄을 문장 하나로 읽음
  - 이미 게시된 적 있는 문장(해시 기준)은 건너뜀 — pipeline/state.json 으로 추적
  - 새 문장마다 claude CLI로 일기 + 응용 문장 2개 생성
  - content/posts/YYYY-MM-DD-....md 로 문장당 포스트 1개씩 저장
        │
        ▼  변경사항 커밋 & push
Hugo build → GitHub Pages 배포
```

## 매일 사용하는 방법

1. GitHub 저장소에서 [`input/sentence.md`](input/sentence.md) 파일을 연다.
2. 연필(✏️) 아이콘을 눌러 편집 모드로 들어간다. (블로그 상단 "오늘의 문장 입력 ✏️" 버튼으로 바로 이동 가능)
3. 코드블록(```) 안에 오늘 연습하고 싶은 영어 문장을 한 줄에 하나씩 적는다. 여러 문장을 적으면 문장마다 포스트가 하나씩 생성된다.
4. 우측 상단 "Commit changes"로 저장한다. (로컬 git 작업 불필요)
5. 다음날 07:00(KST)에 자동으로 새 문장들을 기준으로 일기 포스트가 게시된다.

이미 게시에 사용된 문장은 파일에 그대로 남아있어도(같은 날이든 다른 날이든) 다시 게시되지 않는다.

즉시 확인하고 싶다면 GitHub 저장소 → Actions 탭 → "Daily English Diary" →
"Run workflow" 로 수동 실행할 수 있다.

## 최초 설정 (1회만, 사람이 직접 해야 하는 단계)

자동 생성 단계는 Claude Code CLI를 사용한다. GitHub Actions에서 이 CLI를 인증하려면
Claude 구독 계정으로 발급한 OAuth 토큰을 저장소 Secret으로 등록해야 한다. 이 과정은
브라우저 로그인이 필요해 에이전트가 대신할 수 없다.

```bash
claude setup-token
```

터미널에 표시되는 인증 코드를 브라우저에 붙여넣고 로그인하면, **그 다음에** 터미널에
`sk-ant-oat01-...` 로 시작하는 토큰이 출력된다. (브라우저에 표시된 인증 코드 자체가
아니라, 붙여넣은 뒤 터미널에 최종 출력되는 토큰이어야 한다.)

```bash
gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo jeonck/writing-diary
# 위 토큰을 붙여넣기
```

등록 후 Actions 탭에서 워크플로를 한 번 수동 실행(`workflow_dispatch`)해 정상 동작을
확인한다.

## 저장소 구조

| 경로 | 역할 |
|---|---|
| `input/sentence.md` | 오늘의 영어 문장들 — 한 줄에 하나씩 (사람이 매일 수정) |
| `pipeline/generate.py` | 문장별 일기/응용문장 생성 → Hugo 포스트 작성 |
| `pipeline/state.json` | 게시에 사용된 문장 해시 목록 (중복 게시 방지) |
| `content/posts/` | 생성된 일기 포스트 |
| `.github/workflows/daily.yml` | 매일 07:00 KST 생성 + 배포 워크플로 |
| `themes/PaperMod` | Hugo 테마 (git submodule) |

## 로컬에서 테스트

```bash
hugo server -D                      # http://localhost:1313/writing-diary/
python3 pipeline/generate.py --dry-run   # 파일 생성 없이 결과만 확인
```

로컬에는 `claude` CLI 로그인 세션이 있으면 그대로 사용되고(`JUDGE_BACKEND=claude-code`),
없으면 `ANTHROPIC_API_KEY` 를 설정해 `JUDGE_BACKEND=api` 로 실행할 수 있다.
