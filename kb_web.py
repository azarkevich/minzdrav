"""Small local website for the expert system: ask a question, read the answer with excerpts and links.

    python -I kb_web.py [--kb kb_all/kb.json] [--llm] [--port 8000]

Stdlib only. Listens on 127.0.0.1. History is kept in the browser (localStorage).
"""
import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb  # noqa: E402

PAGE = (Path(__file__).resolve().parent / "web" / "index.html").read_text(encoding="utf-8")
LOCK = threading.Lock()


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/ask":
                return self._send(404, "not found", "text/plain")
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                question = (req.get("question") or "").strip()
                if not question:
                    raise ValueError("Пустой вопрос")
                with LOCK:
                    ans = engine.ask(question, population=req.get("population") or None)
                if not ans.get("error"):
                    for r in ans.get("results", []):
                        for e in r["evidence"]:
                            e["level_label"] = kb.LEVELS[e["level"]][1]
                    ans["text"] = kb.render(ans)
            except Exception as ex:  # noqa: BLE001 — show the problem in the page
                ans = {"error": f"{type(ex).__name__}: {ex}"}
            self._send(200, json.dumps(ans, ensure_ascii=False), "application/json")

        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=Path, default=Path("kb_all/kb.json"))
    ap.add_argument("--reference", type=Path, default=Path("reference"))
    ap.add_argument("--llm", action="store_true", help="ask the LLM when the diagnosis is not recognized confidently")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    engine = kb.Engine(args.kb, args.reference, llm=args.llm)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine))
    print(f"http://127.0.0.1:{args.port}/  (LLM {'on' if args.llm else 'off'}, KB {args.kb})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
