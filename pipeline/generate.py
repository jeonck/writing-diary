#!/usr/bin/env python3
"""Daily English writing diary pipeline.

input/sentence.md 에서 오늘의 영어 문장(들)을 읽어, 문장마다 Claude로 짧은 영어 일기와
응용 문장을 생성해 Hugo 포스트로 저장한다. 코드블록 안에 여러 줄을 적으면 줄마다 별도
포스트가 생성된다. 이미 게시에 사용된 문장(문장 텍스트 해시 기준)은 다시 나타나도
건너뛴다. 입력이 비어 있으면 여러 위인(니체, 마르쿠스 아우렐리우스, 공자 등)의 명언
목록에서 그날의 문장을 대신 사용한다.

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

# 문장이 비어 있을 때 대신 쓰는 명언 풀 — 저자 다양화 (모두 공개 저작물/고전 인용구)
FALLBACK_QUOTES = [
    {"text": "What does not kill me makes me stronger.", "author": "Friedrich Nietzsche"},
    {"text": "He who has a why to live can bear almost any how.", "author": "Friedrich Nietzsche"},
    {"text": "Without music, life would be a mistake.", "author": "Friedrich Nietzsche"},
    {"text": "And if you gaze long enough into an abyss, the abyss will gaze back into you.", "author": "Friedrich Nietzsche"},
    {"text": "One must still have chaos in oneself to be able to give birth to a dancing star.", "author": "Friedrich Nietzsche"},
    {"text": "Become who you are.", "author": "Friedrich Nietzsche"},
    {"text": "You have power over your mind, not outside events. Realize this, and you will find strength.", "author": "Marcus Aurelius"},
    {"text": "The happiness of your life depends upon the quality of your thoughts.", "author": "Marcus Aurelius"},
    {"text": "Waste no more time arguing about what a good man should be. Be one.", "author": "Marcus Aurelius"},
    {"text": "Luck is what happens when preparation meets opportunity.", "author": "Seneca"},
    {"text": "We suffer more in imagination than in reality.", "author": "Seneca"},
    {"text": "It's not what happens to you, but how you react to it that matters.", "author": "Epictetus"},
    {"text": "No man is free who is not master of himself.", "author": "Epictetus"},
    {"text": "It does not matter how slowly you go as long as you do not stop.", "author": "Confucius"},
    {"text": "Our greatest glory is not in never falling, but in rising every time we fall.", "author": "Confucius"},
    {"text": "A journey of a thousand miles begins with a single step.", "author": "Lao Tzu"},
    {"text": "Nature does not hurry, yet everything is accomplished.", "author": "Lao Tzu"},
    {"text": "We are what we repeatedly do. Excellence, then, is not an act, but a habit.", "author": "Aristotle"},
    {"text": "The only true wisdom is in knowing you know nothing.", "author": "Socrates"},
    {"text": "Do not go where the path may lead, go instead where there is no path and leave a trail.", "author": "Ralph Waldo Emerson"},
    {"text": "What lies behind us and what lies before us are tiny matters compared to what lies within us.", "author": "Ralph Waldo Emerson"},
    {"text": "Go confidently in the direction of your dreams. Live the life you have imagined.", "author": "Henry David Thoreau"},
    {"text": "The wound is the place where the light enters you.", "author": "Rumi"},
    {"text": "The secret of getting ahead is getting started.", "author": "Mark Twain"},
    {"text": "Twenty years from now you will be more disappointed by the things you didn't do than by the ones you did do.", "author": "Mark Twain"},
    {"text": "Alone we can do so little; together we can do so much.", "author": "Helen Keller"},
    {"text": "Well done is better than well said.", "author": "Benjamin Franklin"},
    {"text": "An investment in knowledge pays the best interest.", "author": "Benjamin Franklin"},
    {"text": "Simplicity is the ultimate sophistication.", "author": "Leonardo da Vinci"},
    {"text": "What we think, we become.", "author": "Buddha"},
]

SYSTEM_PROMPT = """당신은 영어 학습자를 위한 다이어리 작문 도우미다. 사용자가 오늘의 영어
문장을 입력하면, 그 문장을 자연스럽게 활용한 짧은 영어 일기와 학습에 도움이 되는 응용
문장을 만든다. 일기는 실제 일상적인 상황처럼 자연스럽게 쓰고, 과장하거나 억지로 늘리지
않는다."""

GENERATE_PROMPT = """아래 "오늘의 문장"을 반드시 활용해서(그대로 포함하거나 자연스럽게
녹여서) 짧은 영어 일기를 작성하라.{note} 반드시 다음 JSON 형식으로만 답하라. 다른 텍스트 금지.

{{"title_ko": "일기 주제를 요약한 한국어 제목 한 줄",
 "diary_en": "5~7문장 분량의 영어 일기 전체 텍스트. 자연스러운 구어체 일기 톤",
 "diary_ko": "diary_en의 자연스러운 한국어 번역",
 "applied_sentences": [
   {{"en": "오늘의 문장과 비슷한 문형/어휘를 재사용한 응용 문장 1", "ko": "한국어 해석"}},
   {{"en": "응용 문장 2 (문형은 같지만 다른 상황)", "ko": "한국어 해석"}}
 ],
 "tags": ["kebab-case-태그", "최대 3개"]}}

오늘의 문장: {sentence}"""

QUOTE_NOTE = (
    " 이 문장은 오늘 사용자가 직접 쓴 문장이 아니라 {author}의 명언이다."
    " 이 명언을 오늘 일상 속에서 문득 떠올리게 된 것처럼, 명언과 어울리는"
    " 소소한 상황을 상상해서 일기를 써라."
)


def log(msg: str) -> None:
    print(msg, flush=True)


def sentence_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return (slug or "diary")[:60].rstrip("-")


def read_sentences() -> list[str]:
    """input/sentence.md 에서 사용자가 적은 문장들을 읽는다. 없으면 빈 리스트."""
    if not SENTENCE_FILE.exists():
        log(f"오류: {SENTENCE_FILE} 파일이 없습니다")
        sys.exit(1)
    text = SENTENCE_FILE.read_text(encoding="utf-8")
    fenced = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    body = fenced.group(1) if fenced else text
    sentences = []
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(("<!--", "-", "#")):
            sentences.append(line)
    return sentences


def fallback_quote_item(today: "datetime.date") -> dict:
    """input이 비어 있을 때 사용할 명언 — 날짜 기준으로 여러 저자의 명언 풀을 순환 선택."""
    idx = today.timetuple().tm_yday % len(FALLBACK_QUOTES)
    quote = FALLBACK_QUOTES[idx]
    return {
        "text": quote["text"],
        "source": quote["author"],
        # 날짜를 해시에 포함 — 같은 명언이 몇 주 뒤 다시 나와도 새로 게시되도록
        "dedup_key": sentence_hash(f"{today.isoformat()}::{quote['text']}"),
    }


def build_queue(sentences: list[str], today: "datetime.date") -> list[dict]:
    if sentences:
        return [
            {"text": s, "source": None, "dedup_key": sentence_hash(s)}
            for s in sentences
        ]
    return [fallback_quote_item(today)]


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


def build_prompt(sentence: str, source: str | None) -> str:
    note = QUOTE_NOTE.format(author=source) if source else ""
    return GENERATE_PROMPT.format(sentence=sentence, note=note)


def generate_api(client, model: str, sentence: str, source: str | None = None) -> dict | None:
    prompt = build_prompt(sentence, source)
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


def generate_cli(model: str, sentence: str, source: str | None = None) -> dict | None:
    prompt = build_prompt(sentence, source)
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


def write_post(sentence: str, result: dict, date: datetime, source: str | None = None) -> Path:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{date.date().isoformat()}-{slugify(result['title_ko'])}"
    path = CONTENT_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = CONTENT_DIR / f"{base}-{n}.md"
        n += 1
    tags = list(result["tags"])
    if source:
        tags = (tags + ["quote"])[:4]
    tags_str = ", ".join(yaml_quote(t) for t in tags)
    applied_md = "\n".join(
        f"{i}. **{a['en']}**\n   {a['ko']}"
        for i, a in enumerate(result["applied_sentences"], 1)
    )
    heading = "오늘의 명언" if source else "오늘의 문장"
    quote_block = f"> {sentence}\n>\n> — {source}" if source else f"> {sentence}"
    body = f"""---
title: {yaml_quote(f"{date.date().isoformat()} {result['title_ko']}")}
date: {date.isoformat()}
tags: [{tags_str}]
---
## {heading}

{quote_block}

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
    today = datetime.now(KST).date()
    sentences = read_sentences()
    queue = build_queue(sentences, today)
    if sentences:
        log(f"입력된 문장 {len(sentences)}개")
    else:
        log(f"input/sentence.md 에 문장이 없어 명언으로 대체합니다: {queue[0]['text']} (— {queue[0]['source']})")

    state = load_state()
    processed: dict = state.get("processed", {})
    if not processed and state.get("last_hash"):  # 이전(단일 문장) 버전 state.json 마이그레이션
        processed[state["last_hash"]] = state.get("last_date", "")

    log(f"=== 생성 시작 (backend={backend}, model={model}, dry_run={args.dry_run}) ===")

    new_count = 0
    skipped_dup = 0
    failed = 0
    fatal_error = None
    for item in queue:
        sentence, source, h = item["text"], item["source"], item["dedup_key"]
        if h in processed:
            skipped_dup += 1
            continue

        log(f"\n오늘의 문장: {sentence}" + (f" (— {source})" if source else ""))
        try:
            if backend == "claude-code":
                result = generate_cli(model, sentence, source)
            else:
                result = generate_api(client, model, sentence, source)
        except FatalAPIError as exc:
            fatal_error = exc
            break

        if result is None:
            log("  생성 실패 — 건너뜁니다 (다음 실행에서 재시도)")
            failed += 1
            continue

        now = datetime.now(KST)
        log(f"  → {result['title_ko']}")

        if args.dry_run:
            log("  --- diary_en ---\n  " + result["diary_en"])
            log("  --- diary_ko ---\n  " + result["diary_ko"])
            log("  --- applied_sentences ---")
            for a in result["applied_sentences"]:
                log(f"    - {a['en']} / {a['ko']}")
            continue

        path = write_post(sentence, result, now, source)
        log(f"  생성 파일: {path.relative_to(ROOT)}")
        processed[h] = now.date().isoformat()
        new_count += 1

    log(f"\n=== 결과: 신규 {new_count} / 중복 스킵 {skipped_dup} / 생성 실패 {failed} ===")

    if args.dry_run:
        log("(dry-run — 파일 생성/기록 갱신 없음)")
        return 1 if fatal_error else 0

    if new_count:
        state["processed"] = processed
        state.pop("last_hash", None)
        state.pop("last_date", None)
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    if fatal_error:
        log(f"\n중단: 복구 불가능한 API 오류 — {fatal_error}")
        log("→ Anthropic 크레딧/API 키(또는 CLAUDE_CODE_OAUTH_TOKEN)를 확인하세요.")
        log("→ 성공한 문장은 이미 게시/기록되었습니다.")
        return 1
    return 1 if failed and not new_count else 0


if __name__ == "__main__":
    sys.exit(main())
