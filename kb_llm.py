"""LLM side of the expert system (Claude API). Two jobs, both optional — the rule-based
system works without them:

  1. parse a question the rules can't handle confidently: «болезнь Аддисона» → E27.1
     (kb.py calls parse_question() when the diagnosis is not recognized with confidence);
  2. cross-check the rule-based extraction (EXPERT_SYSTEM_DESIGN.md §2.2а): the model reads
     a chapter and lists its diagnostics items with verbatim quotes; quotes are verified
     against the source text, then compared with the rules' items:
        verified  — both found the item with the same level;
        uncertain — only one side found it, or the levels differ.
     Result: kb/crosscheck.json, read by kb.py to label the evidence.

Needs `pip install anthropic` and credentials (ANTHROPIC_API_KEY, or `ant auth login`).

Usage:
    python -I kb_llm.py crosscheck [--limit 5] [--dry-run]      # cross-check kb/kb.json
    python -I kb_llm.py parse "Диагноз – болезнь Аддисона, разрешен ли тест с инсулином"
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir

MODEL = "claude-opus-5-5"
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
    return anthropic.Anthropic()


def call(prompt: str, schema: dict, model: str, effort: str, max_tokens: int = 16000) -> dict:
    """One structured-output request. Server-side refusal fallback is on (Opus 5.5 default)."""
    import anthropic
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


def parse_question(question: str, model: str = MODEL) -> dict:
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
    c.add_argument("--out", type=Path, default=Path("kb/crosscheck.json"))
    c.add_argument("--limit", type=int, help="only the first N chapters (trial run)")
    c.add_argument("--dry-run", action="store_true", help="show what would be sent and a token estimate")
    p = sub.add_parser("parse")
    p.add_argument("question")
    for x in (c, p):
        x.add_argument("--model", default=MODEL)
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

    done = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {"sections": {}}
    for i, s in enumerate(secs, 1):
        if s["id"] in done["sections"]:
            continue
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
