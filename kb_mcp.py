"""MCP server (stdio) over the expert system, so Claude Desktop / Claude Code can serve as
the chat interface: the model talks to the user, the verdict comes from kb.py.

Tools:
  check_service(diagnosis, service, population?)  — is the service provided for? verdict +
                                                    verbatim excerpts + <url>#page=N links
  find_diagnosis(text)                             — МКБ-10 candidates for a diagnosis wording
  find_diagnoses(services[], population?) — reverse search: diagnoses whose protocols list the services
  list_diagnostics(diagnosis, category?, service?, population?) — diagnostics list items for a
                                                    diagnosis: all, lab / instrumental / consult, or one service

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
from kb import Engine, render  # noqa: E402

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
        "description": "Перечень диагностики в протоколах для диагноза (обязательная, дополнительная, "
                       "наблюдение), сгруппированный по протоколам и главам, со ссылками на страницы. "
                       "Можно сузить видом исследований (category) и/или услугой (service: «КТ» — все виды КТ).",
        "inputSchema": {"type": "object", "properties": {
            "diagnosis": {"type": "string", "description": "диагноз словами или код МКБ-10"},
            "category": {"type": "string", "enum": ["lab", "instrumental", "consult"],
                         "description": "lab — лабораторные, instrumental — инструментальные, consult — консультации"},
            "service": {"type": "string", "description": "услуга-фильтр, например «КТ», «УЗИ», «гормоны»"},
            "population": POPULATION}, "required": ["diagnosis"]},
    },
    {
        "name": "find_diagnoses",
        "description": "Обратный поиск: при каких диагнозах протоколы предусматривают перечисленные услуги "
                       "(исследования, анализы). Диагнозы ранжированы по числу совпавших услуг, редкости услуги "
                       "и уровню (обязательная выше «по показаниям»); с кодами МКБ-10, цитатами и страховым вердиктом.",
        "inputSchema": {"type": "object", "properties": {
            "services": {"type": "array", "items": {"type": "string"}, "description": "услуги, например [\"ТТГ\", \"свободный Т4\"]"},
            "population": POPULATION}, "required": ["services"]},
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


def call_tool(name: str, args: dict) -> str:
    if name == "check_service":
        a = engine().ask(diagnosis=args["diagnosis"], service=args["service"], population=args.get("population"))
        return render(a) if not a.get("error") else "Ошибка: " + a["error"]
    if name == "find_diagnosis":
        return json.dumps(engine().resolve_diagnosis(args["text"]), ensure_ascii=False, indent=1)
    if name == "list_diagnostics":
        a = engine().list_services(None, args["diagnosis"], args.get("category"), args.get("service"),
                                   args.get("population"))
        return render(a)
    if name == "find_diagnoses":
        return render(engine().reverse_search(None, list(args["services"]), args.get("population")))
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
