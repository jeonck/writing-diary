#!/usr/bin/env python3
"""Daily English writing diary pipeline.

input/sentence.md 에서 오늘의 영어 문장을 읽어, Claude로 짧은 영어 일기와
응용 문장을 생성한 뒤 Hugo 포스트로 저장한다. 문장이 어제와 동일하면 건너뛴다.

Usage:
    python pipeline/generate.py [--dry-run]

Env:
    JUDGE_BACKEND            "claude-code" | "api" (기본: 자동 — claude CLI가 있으면
                             claude-code, 없으면 api)
    CLAUDE_CODE_OAUTH_TOKEN  claude-code 백엔드 CI 인증 (claude setup-token으로 발급,
                             로컬은 claude 로그인 세션 사용)
    ANTHROPIC_API_KEY        api 백엔드 필수
    CLAUDE_MODEL             생성 모델 (기본 claude-sonnet-4-6)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENTENCE_FILE = ROOT / "input" / "sentence.md"
STATE_FILE = ROOT / "pipeline" / "state.json"
CONTENT_DIR = ROOT / "content" / "posts"

KST = timezone(timedelta(hours=9))

SYSTEM_PROMPT = """당신은 영어 학습자를 위한 다이어리 작문 도우미다. 사용자가 오늘의 영어
문장을 입력하면, 그 문장을 자연스럽게 활용한 짧은 영어 일기와 학습에 도움이 되는 응용
문장을 만든다. 일기는 실제 일상적인 상황처럼 자연스럽게 쓰고, 과장하거나 억지로 늘리지
않는다."""

GENERATE_PROMPT = """아래 "오늘의 문장"을 반드시 활용해서(그대로 포함하거나 자연스럽게
녹여서) 짧은 영어 일기를 작성하라. 반드시 다음 JSON 형식으로만 답하라. 다른 텍스트 금지.

{{"title_ko": "일기 주제를 요약한 한국어 제목 한 줄",
 "diary_en": "5~7문장 분량의 영어 일기 전체 텍스트. 자연스러운 구어체 일기 톤",
 "diary_ko": "diary_en의 자연스러운 한국어 번역",
 "applied_sentences": [
   {{"en": "오늘의 문장과 비슷한 문형/어휘를 재사용한 응용 문장 1", "ko": "한국어 해석"}},
   {{"en": "응용 문장 2 (문형은 같지만 다른 상황)", "ko": "한국어 해석"}}
 ],
 "tags": ["kebab-case-태그", "최대 3개"]}}

오늘의 문장: {sentence}"""


def log(msg: str) -> None:
    print(msg, flush=True)


def sentence_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return (slug or "diary")[:60].rstrip("-")


def read_sentence() -> str:
    if not SENTENCE_FILE.exists():
        log(f"오류: {SENTENCE_FILE} 파일이 없습니다")
        sys.exit(1)
    text = SENTENCE_FILE.read_text(encoding="utf-8")
    fenced = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    body = fenced.group(1) if fenced else text
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(("<!--", "-", "#")):
            return line
    log("오류: input/sentence.md 에서 문장을 찾지 못했습니다")
    sys.exit(1)


class FatalAPIError(Exception):
    """재시도가 무의미한 오류(크레딧 부족, 인증 실패) — 실행 전체 중단."""


def is_fatal_api_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in (
        "credit balance", "authenticat", "invalid x-api-key",
        "invalid api key", "invalid bearer token", "oauth token", "/login",
        "401",
    ))


def parse_result(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    required = ("title_ko", "diary_en", "diary_ko", "applied_sentences")
    if not all(isinstance(data.get(k), (str, list)) and data.get(k) for k in required):
        return None
    applied = data.get("applied_sentences") or []
    if not isinstance(applied, list) or not applied:
        return None
    tags = data.get("tags") or []
    data["tags"] = [slugify(str(t)) for t in tags[:3] if str(t).strip()] or ["english-diary"]
    return data


def generate_api(client, model: str, sentence: str) -> dict | None:
    prompt = GENERATE_PROMPT.format(sentence=sentence)
    for attempt in (1, 2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            if is_fatal_api_error(exc):
                raise FatalAPIError(str(exc)) from exc
            log(f"  API 오류 (시도 {attempt}): {exc}")
            if attempt == 2:
                return None
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        result = parse_result(text)
        if result:
            return result
        log(f"  JSON 파싱 실패 (시도 {attempt}): {text[:120]!r}")
    return None


def generate_cli(model: str, sentence: str) -> dict | None:
    prompt = GENERATE_PROMPT.format(sentence=sentence)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    cmd = ["claude", "-p", "--model", model, "--tools", "",
           "--output-format", "text", "--append-system-prompt", SYSTEM_PROMPT]
    for attempt in (1, 2):
        try:
            result = subprocess.run(cmd, input=prompt, env=env, timeout=180,
                                     capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            log(f"  CLI 타임아웃 (시도 {attempt})")
            continue
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            if is_fatal_api_error(RuntimeError(err)):
                raise FatalAPIError(err[:300])
            log(f"  CLI 오류 (시도 {attempt}): {err[:200]}")
            if attempt == 2:
                return None
            continue
        parsed = parse_result(result.stdout)
        if parsed:
            return parsed
        log(f"  JSON 파싱 실패 (시도 {attempt}): {result.stdout[:120]!r}")
    return None


def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_post(sentence: str, result: dict, date: datetime) -> Path:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{date.date().isoformat()}-{slugify(result['title_ko'])}"
    path = CONTENT_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = CONTENT_DIR / f"{base}-{n}.md"
        n += 1
    tags = ", ".join(yaml_quote(t) for t in result["tags"])
    applied_md = "\n".join(
        f"{i}. **{a['en']}**\n   {a['ko']}"
        for i, a in enumerate(result["applied_sentences"], 1)
    )
    body = f"""---
title: {yaml_quote(f"{date.date().isoformat()} {result['title_ko']}")}
date: {date.isoformat()}
tags: [{tags}]
---
## 오늘의 문장

> {sentence}

## 일기 (Diary)

{result['diary_en']}

> {result['diary_ko']}

## 응용 문장 (Applied Sentences)

{applied_md}
"""
    path.write_text(body, encoding="utf-8")
    return path


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily English writing diary pipeline")
    parser.add_argument("--dry-run", action="store_true",
                         help="파일 생성/state.json 갱신 없이 결과만 출력")
    args = parser.parse_args()

    backend = os.environ.get("JUDGE_BACKEND", "").strip() or (
        "claude-code" if shutil.which("claude") else "api"
    )
    client = None
    if backend == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log("오류: api 백엔드에는 ANTHROPIC_API_KEY 환경변수가 필요합니다")
            return 1
        import anthropic  # 지연 임포트

        client = anthropic.Anthropic()
    elif backend == "claude-code":
        if not shutil.which("claude"):
            log("오류: claude-code 백엔드에는 claude CLI가 PATH에 있어야 합니다")
            return 1
    else:
        log(f"오류: 알 수 없는 JUDGE_BACKEND={backend!r} (claude-code | api)")
        return 1

    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    sentence = read_sentence()
    log(f"오늘의 문장: {sentence}")

    state = load_state()
    h = sentence_hash(sentence)
    if state.get("last_hash") == h:
        log("어제와 동일한 문장 — 중복 게시 방지를 위해 건너뜁니다")
        log("(input/sentence.md 를 새 문장으로 바꾸면 다음 실행에서 게시됩니다)")
        return 0

    log(f"=== 생성 시작 (backend={backend}, model={model}, dry_run={args.dry_run}) ===")
    try:
        if backend == "claude-code":
            result = generate_cli(model, sentence)
        else:
            result = generate_api(client, model, sentence)
    except FatalAPIError as exc:
        log(f"중단: 복구 불가능한 API 오류 — {exc}")
        log("→ Anthropic 크레딧/API 키(또는 CLAUDE_CODE_OAUTH_TOKEN)를 확인하세요.")
        return 1

    if result is None:
        log("생성 실패 — 이번 실행은 건너뜁니다 (다음 실행에서 재시도)")
        return 1

    now = datetime.now(KST)
    log(f"→ {result['title_ko']}")

    if args.dry_run:
        log("\n--- diary_en ---\n" + result["diary_en"])
        log("\n--- diary_ko ---\n" + result["diary_ko"])
        log("\n--- applied_sentences ---")
        for a in result["applied_sentences"]:
            log(f"  - {a['en']} / {a['ko']}")
        log("\n(dry-run — 파일 생성/기록 갱신 없음)")
        return 0

    path = write_post(sentence, result, now)
    log(f"생성 파일: {path.relative_to(ROOT)}")

    state["last_hash"] = h
    state["last_date"] = now.date().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
