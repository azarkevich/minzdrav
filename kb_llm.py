"""LLM side of the expert system (Claude API). Two jobs, both optional — the rule-based
system works without them:

  1. parse a question the rules can't handle confidently: «болезнь Аддисона» → E27.1
     (kb.py calls parse_question() when the diagnosis is not recognized with confidence);
  2. cross-check the rule-based extraction (EXPERT_SYSTEM_DESIGN.md §2.2а): the model reads
     a chapter and lists its diagnostics items with verbatim quotes; quotes are verified
     against the source text, then compared with the rules' items:
        verified  — both found the item with the same level;
        uncertain — only one side found it, or the levels differ.
     Result: crosscheck/kb.json (kb_all.json for kb_all), tracked in git, read by kb.py to label the evidence.

Provider ($KB_LLM_PROVIDER, or chosen by which key is set):
  openrouter  — KB_OPENROUTER_API_KEY, via `requests`;
  anthropic   — KB_ANTHROPIC_API_KEY (else the SDK's own ANTHROPIC_API_KEY / `ant auth login`),
                `pip install anthropic`;
  claude-cli  — `claude -p`, runs on the Claude Code subscription limits.
The KB_ names keep the system off the user's other keys (a plain OPENROUTER_API_KEY is ignored).
Models: cross-check — --model / $KB_LLM_MODEL (default claude-opus-5-5); question parsing at
runtime — $KB_LLM_QUESTION_MODEL (default claude-haiku-5-5). Anthropic ids are mapped to
OpenRouter ids (claude-haiku-5-5 → anthropic/claude-haiku-5.5); OpenRouter ids pass through.

Usage:
    python -I kb_llm.py crosscheck [--limit 5] [--dry-run]      # cross-check kb/kb.json
    python -I kb_llm.py parse "Диагноз – болезнь Аддисона, разрешен ли тест с инсулином"
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir

MODEL = os.environ.get("KB_LLM_MODEL", "claude-opus-5-5")  # knowledge-base cross-check
# runtime question parsing: a short, simple task — a cheap model is enough
QUESTION_MODEL = os.environ.get("KB_LLM_QUESTION_MODEL", "claude-haiku-5-5")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LEVELS = ["mandatory", "additional", "differential", "monitoring"]

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string", "description": "диагноз как в вопросе"},
        "icd10": {"type": "array", "items": {"type": "object", "properties": {
            "code": {"type": "string"}, "name": {"type": "string"}},
            "required": ["code", "name"], "additionalProperties": False},
            "description": "1–3 наиболее вероятных кода МКБ-10, самый вероятный первым"},
        "service": {"type": "string", "description": "диагностическая услуга как в вопросе"},
        "population": {"type": "string", "enum": ["adult", "child", "unknown"]},
    },
    "required": ["diagnosis", "icd10", "service", "population"],
    "additionalProperties": False,
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "clause": {"type": "string", "description": "номер пункта, например «73» или «45.2»"},
            "level": {"type": "string", "enum": LEVELS},
            "condition": {"type": "string", "description": "условие/показание, если указано, иначе пустая строка"},
            "quote": {"type": "string", "description": "дословная строка списка из текста, без изменений"},
        }, "required": ["clause", "level", "condition", "quote"], "additionalProperties": False}},
    },
    "required": ["items"],
    "additionalProperties": False,
}

EXTRACT_PROMPT = """Ниже — глава клинического протокола Министерства здравоохранения Республики Беларусь.
Найди в ней все перечни диагностических медицинских услуг (лабораторные, инструментальные
исследования, консультации врачей-специалистов, диагностические тесты) и выпиши каждую строку
перечня отдельным элементом:
- level: mandatory — «обязательная диагностика»; additional — «дополнительная диагностика»
  (в т.ч. «по показаниям»); differential — исследования для дифференциальной диагностики;
  monitoring — исследования в плане медицинского наблюдения / контроля лечения;
- condition: подзаголовок-условие, под которым стоит строка («при макроаденоме», «у женщин»),
  или пустая строка;
- quote: строка перечня ДОСЛОВНО, как в тексте (без номера пункта), не сокращай и не перефразируй.
Не включай критерии диагноза, лечение, лекарства, показания к госпитализации.
Если перечней нет — верни пустой список.

Глава {chapter}. {title}
{text}"""

QUESTION_PROMPT = """Вопрос сотрудника страховой компании о том, предусмотрена ли диагностическая услуга
клиническим протоколом при диагнозе. Выдели диагноз и услугу, предложи коды МКБ-10 для диагноза
(учти синонимы, эпонимы и опечатки) и определи, о ребенке или взрослом речь (если не сказано — unknown).

Вопрос: {question}"""


def client():
    try:
        import anthropic
    except ImportError:
        sys.exit("pip install anthropic")
    key = os.environ.get("KB_ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def call(prompt: str, schema: dict, model: str, effort: str, max_tokens: int = 16000) -> dict:
    """One structured-output request: OpenRouter if KB_OPENROUTER_API_KEY is set (and no
    KB_ANTHROPIC_API_KEY), else the Anthropic API — unless KB_LLM_PROVIDER says otherwise."""
    provider = os.environ.get("KB_LLM_PROVIDER") or (
        "openrouter" if os.environ.get("KB_OPENROUTER_API_KEY") and not os.environ.get("KB_ANTHROPIC_API_KEY")
        else "anthropic")
    if provider == "claude-cli":
        return call_claude_cli(prompt, schema, model, effort)
    if provider == "openrouter":
        return call_openrouter(prompt, schema, model, effort, max_tokens)
    return call_anthropic(prompt, schema, model, effort, max_tokens)


USAGE_LOG = Path("kb/llm_usage.jsonl")
RATE_LIMITS = Path.home() / ".claude" / "rate-limits.json"  # written by the statusline


def log_usage(rec: dict):
    """One line per LLM call in kb/llm_usage.jsonl — what the runtime actually spends."""
    import time
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": int(time.time()), **rec}, ensure_ascii=False) + "\n")


def rate_limits() -> dict:
    try:
        return json.loads(RATE_LIMITS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def call_claude_cli(prompt: str, schema: dict, model: str, effort: str) -> dict:
    """Claude Code in headless mode (`claude -p`): runs on the user's subscription limits
    instead of an API key. No tools, no MCP, no session saved; prompt goes via stdin."""
    import subprocess
    import time
    alias = {"claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet", "claude-haiku-5-5": "haiku"}.get(model, model)
    cmd = ["claude", "-p", "--output-format", "json", "--model", alias, "--effort", effort,
           "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", "Ты извлекаешь данные из текста. Отвечай только по заданной JSON-схеме.",
           "--json-schema", json.dumps(schema, ensure_ascii=False)]
    t0 = time.time()
    r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p failed ({r.returncode}): {r.stderr.decode('utf-8', 'replace')[:500]}")
    out = json.loads(r.stdout.decode("utf-8"))
    if out.get("is_error"):
        raise RuntimeError(f"claude -p error: {out.get('result')}")
    u = out.get("usage", {})
    log_usage({"provider": "claude-cli", "sec": round(time.time() - t0, 1), "model": alias,
               "input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
               + u.get("cache_read_input_tokens", 0),
               "output": u.get("output_tokens", 0), "cost_usd_list": out.get("total_cost_usd"),
               "five_hour": rate_limits().get("five_hour", {}).get("used_percentage")})
    data = out.get("structured_output")
    if isinstance(data, str):
        data = json.loads(data)
    return data if data is not None else json.loads(out["result"])


def call_openrouter(prompt: str, schema: dict, model: str, effort: str, max_tokens: int) -> dict:
    """OpenAI-compatible chat completions with a strict JSON schema. Model ids are
    OpenRouter's («anthropic/claude-opus-5.5»); a bare Anthropic id is mapped to it."""
    import requests
    if "/" not in model:  # claude-opus-5-5 → anthropic/claude-opus-5.5
        model = "anthropic/" + re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)
    r = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {os.environ['KB_OPENROUTER_API_KEY']}",
                 "X-Title": "minzdrav protocols expert system"},
        json={"model": model, "max_tokens": max_tokens,
              "reasoning": {"effort": effort}, "usage": {"include": True},
              "messages": [{"role": "user", "content": prompt}],
              "response_format": {"type": "json_schema",
                                  "json_schema": {"name": "result", "strict": True, "schema": schema}}},
        timeout=600,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:500]}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"OpenRouter error: {data['error']}")
    u = data.get("usage") or {}
    log_usage({"provider": "openrouter", "model": data.get("model", model), "input": u.get("prompt_tokens"),
               "output": u.get("completion_tokens"), "cost_usd": u.get("cost")})
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("output truncated (max_tokens)")
    text = choice["message"]["content"] or ""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())  # in case a model wraps it
    return json.loads(text)


def call_anthropic(prompt: str, schema: dict, model: str, effort: str, max_tokens: int) -> dict:
    """Anthropic API. Server-side refusal fallback is on (Opus 5.5 default)."""
    resp = client().beta.messages.create(
        model=model,
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"refused: {getattr(resp, 'stop_details', None)}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("output truncated (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def parse_question(question: str, model: str = QUESTION_MODEL) -> dict:
    return call(QUESTION_PROMPT.format(question=question), QUESTION_SCHEMA, model, effort="low", max_tokens=4000)


# ------------------------------------------------------------------ cross-check

def norm_quote(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"^[\d.]+\s*", "", s.strip())
    s = re.sub(r"[\s­]+", " ", s)
    return s.strip(" ;.,:")


def item_key(section_id: str, text: str) -> str:
    return section_id + ":" + hashlib.sha1(norm_quote(text).encode("utf-8")).hexdigest()[:12]


def chapter_text(sec: dict) -> str:
    """The chapter as numbered lines, the way the model should quote it."""
    lines, last = [], None
    for it in sec["items"]:
        if it.get("header") and it["header"] != last:
            lines.append(it["header"])
            last = it["header"]
        if it.get("condition"):
            pass  # condition headers are not items; they are in the source text below
        lines.append(it["text"])
    return "\n".join(lines)


def crosscheck_section(sec: dict, model: str) -> dict:
    src = chapter_text(sec)
    got = call(EXTRACT_PROMPT.format(chapter=sec["chapter"], title=sec["chapter_title"], text=src),
               EXTRACT_SCHEMA, model, effort="high")
    src_norm = norm_quote(src)
    llm = {}
    rejected = []
    for x in got["items"]:
        q = norm_quote(x["quote"])
        if not q or q not in src_norm:  # not verbatim → discarded, never trusted
            rejected.append(x)
            continue
        llm[q] = x
    result = {}
    rules = {norm_quote(it["text"]): it for it in sec["items"] if it["level"] != "text"}
    for q, it in rules.items():
        hit = llm.get(q) or next((v for k, v in llm.items() if k in q or q in k), None)
        status = "verified" if hit and hit["level"] == it["level"] else "uncertain"
        result[item_key(sec["id"], it["text"])] = {"status": status, "rules": it["level"],
                                                   "llm": hit["level"] if hit else None}
    for q, x in llm.items():
        if not any(q in k or k in q for k in rules):
            result[item_key(sec["id"], x["quote"])] = {"status": "llm_only", "rules": None, "llm": x["level"],
                                                       "quote": x["quote"], "clause": x["clause"]}
    return {"items": result, "rejected": rejected}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crosscheck")
    c.add_argument("--kb", type=Path, default=Path("kb/kb.json"))
    c.add_argument("--out", type=Path, help="default: crosscheck/<kb folder name>.json (tracked in git)")
    c.add_argument("--limit", type=int, help="only the first N chapters (trial run)")
    c.add_argument("--dry-run", action="store_true", help="show what would be sent and a token estimate")
    c.add_argument("--stop-at", type=float, help="stop when the 5-hour limit (~/.claude/rate-limits.json) reaches this %%")
    p = sub.add_parser("parse")
    p.add_argument("question")
    c.add_argument("--model", default=MODEL)
    p.add_argument("--model", default=QUESTION_MODEL)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "parse":
        print(json.dumps(parse_question(args.question, args.model), ensure_ascii=False, indent=1))
        return

    kb = json.loads(args.kb.read_text(encoding="utf-8"))
    secs = [s for s in kb["sections"] if any(i["level"] != "text" for i in s["items"])
            and kb["documents"][s["doc_id"]]["kind"] not in ("table", "scan")]
    if args.limit:
        secs = secs[:args.limit]
    chars = sum(len(chapter_text(s)) for s in secs)
    # ~3 characters per token for Russian is a rough guess; check with count_tokens for real numbers
    print(f"{len(secs)} chapters, {chars} characters ≈ {chars // 3} input tokens (+ prompt)")
    if args.dry_run:
        print(EXTRACT_PROMPT.format(chapter=secs[0]["chapter"], title=secs[0]["chapter_title"],
                                    text=chapter_text(secs[0]))[:3000])
        return

    if args.out is None:
        args.out = Path("crosscheck") / f"{args.kb.resolve().parent.name}.json"
    args.out.parent.mkdir(exist_ok=True)
    done = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {"sections": {}}
    for i, s in enumerate(secs, 1):
        if s["id"] in done["sections"]:
            continue
        five = rate_limits().get("five_hour", {}).get("used_percentage")
        if args.stop_at is not None and five is not None and five >= args.stop_at:
            print(f"stopped: 5-hour limit at {five}% (>= {args.stop_at}%)")
            break
        try:
            done["sections"][s["id"]] = crosscheck_section(s, args.model)
        except Exception as e:  # keep what's done; rerun resumes
            print(f"  {s['id']}: {e!r}")
            continue
        st = [v["status"] for v in done["sections"][s["id"]]["items"].values()]
        print(f"[{i}/{len(secs)}] {s['chapter']}. {s['chapter_title'][:50]}: "
              f"{st.count('verified')} verified, {st.count('uncertain')} uncertain, {st.count('llm_only')} llm-only")
        args.out.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
