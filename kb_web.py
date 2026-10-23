"""Small local website for the expert system: ask a question, read the answer with excerpts and links.

    python -I kb_web.py [--kb kb_all/kb.idx.json] [--llm] [--port 8000]

Stdlib only. Listens on 127.0.0.1. History is kept in the browser (localStorage).
"""
import argparse
import base64
import hmac
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb  # noqa: E402

VERSION = "1.5"  # shown on the page; bump on user-visible changes
PAGE = (Path(__file__).resolve().parent / "web" / "index.html").read_text(encoding="utf-8").replace("__VERSION__", VERSION)
LOCK = threading.Lock()
FEEDBACK_LOG = Path(__file__).resolve().parent / "kb" / "feedback.jsonl"
ANSWERS: dict = {}  # recent answers by id, so a rating is stored together with what was answered


def summary(question, ans):
    """What a reviewer needs to judge the answer later (and to turn it into an eval case)."""
    ev = [{"protocol": e["protocol"], "clause": e["clause"], "pages": e["pdf_pages"], "url": e["url"]}
          for r in ans.get("results", []) for e in r["evidence"][:3]]
    return {"question": question, "diagnosis_text": ans.get("diagnosis_text"),
            "diagnosis": (ans.get("diagnosis") or {}).get("code"), "service_text": ans.get("service_text"),
            "service": [s["code"] for s in (ans.get("service_catalogue") or [])[:2]],
            "population": ans.get("population"), "verdict_level": ans.get("verdict_level"),
            "verdict": ans.get("verdict"), "evidence": ev, "method": ans.get("method"),
            "insurance": {k: (ans.get("insurance") or {}).get(k) for k in ("verdict_level", "verdict")}}


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            """Optional basic auth: any user name, password from $KB_WEB_PASSWORD (unset = open)."""
            pw = os.environ.get("KB_WEB_PASSWORD")
            if not pw:
                return True
            try:
                given = base64.b64decode(self.headers.get("Authorization", "")[6:]).decode("utf-8").partition(":")[2]
            except Exception:  # noqa: BLE001
                given = ""
            if hmac.compare_digest(given.encode(), pw.encode()):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="minzdrav", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self):
            if self.path == "/healthz":  # hosting health check, no password
                return self._send(200, "ok", "text/plain")
            if not self._authorized():
                return
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if not self._authorized():
                return
            if self.path == "/api/feedback":
                return self._feedback()
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
                    ans["answer_id"] = uuid.uuid4().hex[:12]
                    with LOCK:
                        ANSWERS[ans["answer_id"]] = summary(question, ans)
                        while len(ANSWERS) > 500:
                            ANSWERS.pop(next(iter(ANSWERS)))
            except Exception as ex:  # noqa: BLE001 — show the problem in the page
                ans = {"error": f"{type(ex).__name__}: {ex}"}
            self._send(200, json.dumps(ans, ensure_ascii=False), "application/json")

        def _feedback(self):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                stars = int(req.get("stars"))
                if not 1 <= stars <= 3:
                    raise ValueError("stars must be 1..3")
                rec = {"t": int(time.time()), "answer_id": req.get("answer_id"), "stars": stars,
                       "tags": [str(t)[:40] for t in (req.get("tags") or [])][:10],
                       "comment": str(req.get("comment") or "")[:2000],
                       "answer": ANSWERS.get(req.get("answer_id"))}
                FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
                with LOCK, FEEDBACK_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print("FEEDBACK " + json.dumps(rec, ensure_ascii=False), flush=True)  # hosts with a throwaway disk keep stdout logs
                out = {"ok": True}
            except Exception as ex:  # noqa: BLE001
                out = {"error": f"{type(ex).__name__}: {ex}"}
            self._send(200, json.dumps(out, ensure_ascii=False), "application/json")

        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=Path, default=Path("kb_all/kb.idx.json"))
    ap.add_argument("--reference", type=Path, default=Path("reference"))
    ap.add_argument("--llm", action="store_true", help="ask the LLM when the diagnosis is not recognized confidently")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to accept connections from other machines")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # logs and FEEDBACK lines contain Cyrillic; hosts may default to ASCII
    sys.stderr.reconfigure(encoding="utf-8")
    engine = kb.Engine(args.kb, args.reference, llm=args.llm)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(engine))
    print(f"http://{args.host}:{args.port}/  (version {VERSION}, LLM {'on' if args.llm else 'off'}, KB {args.kb})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
