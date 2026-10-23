"""MCP server (stdio) over the expert system, so Claude Desktop / Claude Code can serve as
the chat interface: the model talks to the user, the verdict comes from kb.py.

Tools:
  check_service(diagnosis, service, population?)  — is the service provided for? verdict +
                                                    verbatim excerpts + <url>#page=N links
  find_diagnosis(text)                             — МКБ-10 candidates for a diagnosis wording
  list_diagnostics(diagnosis, population?)         — every diagnostics list item for a diagnosis

Standard library only (newline-delimited JSON-RPC 2.0 over stdin/stdout, MCP stdio transport).
Register in Claude Code:
    claude mcp add minzdrav -- python -I C:/w/minzdrav/kb_mcp.py
Knowledge base: $KB_PATH or kb_all/kb.idx.json (else kb_all/kb.json) next to this script (falls back to kb/kb.json).
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir
from kb import LEVELS, Engine, render  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
POPULATION = {"type": "string", "enum": ["adult", "child"],
              "description": "adult — взрослые, child — дети; не указывать, если неизвестно"}
TOOLS = [
    {
        "name": "check_service",
        "description": "Предусмотрена ли диагностическая услуга клиническими протоколами Минздрава РБ "
                       "при диагнозе. Возвращает вердикт (обязательная / дополнительная диагностика / "
                       "наблюдение / не предусмотрено), дословные выдержки из протокола и ссылки на PDF "
                       "с номером страницы. Цитируй выдержки и ссылки пользователю дословно.",
        "inputSchema": {"type": "object", "properties": {
            "diagnosis": {"type": "string", "description": "диагноз словами или код МКБ-10 (E06.3)"},
            "service": {"type": "string", "description": "диагностическая услуга, например «анализ мочи по Нечипоренко»"},
            "population": POPULATION}, "required": ["diagnosis", "service"]},
    },
    {
        "name": "find_diagnosis",
        "description": "Коды МКБ-10 для формулировки диагноза (с учетом опечаток и синонимов из протоколов).",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "list_diagnostics",
        "description": "Все позиции перечней диагностики (обязательная, дополнительная, наблюдение) "
                       "в протоколах для диагноза, со ссылками на страницы.",
        "inputSchema": {"type": "object", "properties": {
            "diagnosis": {"type": "string", "description": "диагноз словами или код МКБ-10"},
            "population": POPULATION}, "required": ["diagnosis"]},
    },
]

_engine = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        kb = Path(os.environ.get("KB_PATH") or (HERE / "kb_all" / "kb.idx.json"))
        if not kb.exists() and not os.environ.get("KB_PATH"):
            kb = HERE / "kb_all" / "kb.json"
        if not kb.exists():
            kb = HERE / "kb" / "kb.json"
        _engine = Engine(kb, HERE / "reference")
    return _engine


def list_diagnostics(diagnosis: str, population: str | None) -> str:
    e = engine()
    dx = e.resolve_diagnosis(diagnosis)
    if not dx:
        return f"Диагноз «{diagnosis}» не распознан. Укажите код МКБ-10."
    code = dx[0]["code"]
    out = [f"Диагноз: {code} {dx[0]['name']}"]
    for s in sorted(e.sections_for(code, population, diagnosis), key=lambda s: (s["tier"], s["doc_id"], s["page"])):
        items = [i for i in s["items"] if i["level"] != "text"]
        if not items:
            continue
        doc = e.kb["documents"][s["doc_id"]]
        where = f"Глава {s['chapter']} «{s['chapter_title']}»" if s["chapter"] is not None else s["chapter_title"]
        out.append(f"\n## {s['protocol'] or doc['title']}\n{where}" + (" (смежный раздел)" if s["tier"] else ""))
        for it in items:
            cond = f" [{it['condition']}]" if it.get("condition") else ""
            out.append(f"- {LEVELS[it['level']][1]}{cond}: {it['text']} — {doc['url']}#page={it['page']}")
    return "\n".join(out) if len(out) > 1 else out[0] + "\nПеречни диагностики для этого диагноза не найдены."


def call_tool(name: str, args: dict) -> str:
    if name == "check_service":
        a = engine().ask(diagnosis=args["diagnosis"], service=args["service"], population=args.get("population"))
        return render(a) if not a.get("error") else "Ошибка: " + a["error"]
    if name == "find_diagnosis":
        return json.dumps(engine().resolve_diagnosis(args["text"]), ensure_ascii=False, indent=1)
    if name == "list_diagnostics":
        return list_diagnostics(args["diagnosis"], args.get("population"))
    raise KeyError(name)


def handle(msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "minzdrav-protocols", "version": "0.1"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = msg["params"]
        try:
            text = call_tool(p["name"], p.get("arguments") or {})
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as e:  # report to the model instead of killing the server
            return {"content": [{"type": "text", "text": f"Ошибка: {e!r}"}], "isError": True}
    if method == "ping":
        return {}
    if mid is not None:
        raise LookupError(method)
    return None  # notifications (notifications/initialized, …)


def main():
    stdin = open(sys.stdin.fileno(), encoding="utf-8", newline="\n")
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n")
    for line in stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        try:
            result = handle(msg)
            if msg.get("id") is None:
                continue
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except LookupError as e:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": f"unknown method {e}"}}
        stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
