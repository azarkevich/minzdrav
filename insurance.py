"""Insurance rules check: is a diagnosis an insured event under Правила №20 добровольного
страхования медицинских расходов ЗАСО «ТАСК»? Answers with the excluding clause, its verbatim
text and a `<url>#page=N` link; kb.py adds this block to every answer.

    python -I insurance.py build                 # PDF → data/insurance/task20.json (texts + pages)
    python -I insurance.py check E06.3 [--service "ТТГ"] [--text "аутоиммунный тиреоидит"]

The clause ↔ МКБ-10 mapping is hand-written in data/insurance/task20.rules.json; `build`
adds each clause's text and page from reference/raw/task20_rules.pdf (downloaded once).
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir
from kb_text import Normalizer, close  # noqa: E402

HERE = Path(__file__).resolve().parent
RULES = HERE / "data" / "insurance" / "task20.rules.json"
BUILT = HERE / "data" / "insurance" / "task20.json"
PDF = HERE / "reference" / "raw" / "task20_rules.pdf"
CLAUSE_START_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\.?\s+(\S.*)$")
CUTOFF = (4, 3)  # nothing from п. 4.3 on is used (решение заказчика 2026-10-24)
HEADER_RE = re.compile(r"^\s*\d{1,2}\.\s+[А-ЯЁ ,.]{6,}$")  # «4. РАСХОДЫ, НЕПОКРЫВАЕМЫЕ СТРАХОВЩИКОМ»

VERDICTS = {
    "excluded": "НЕ СТРАХОВОЙ СЛУЧАЙ — заболевание исключено Правилами",
    "probable": "ВЕРОЯТНО НЕ СТРАХОВОЙ — диагноз входит в исключённую группу заболеваний, проверьте",
    "limited": "СТРАХОВОЙ, С ОГРАНИЧЕНИЯМИ — часть услуг по этому заболеванию не оплачивается",
    "covered": "ИСКЛЮЧЕНИЙ НЕ НАЙДЕНО — страховой случай при внезапном расстройстве здоровья или обострении "
               "хронического заболевания, в объёме Программы страхования",
    "unclear": "УТОЧНИТЕ ДИАГНОЗ — формулировка подходит под заболевания с разным страховым решением",
}
SHORT = {"excluded": "не страховой", "probable": "вероятно не страховой", "limited": "страховой, с ограничениями",
         "covered": "страховой"}


# ---------------------------------------------------------------------------- build
def clause_key(cid: str) -> tuple[int, ...]:
    """«4.1.17#2» → (4, 1, 17)."""
    return tuple(int(p) for p in cid.split("#")[0].split("."))


def parse_clauses(pages: list[str]) -> dict:
    """Numbered clauses of the rules → {id: {"page", "text"}}; a repeated number (4.1.5 twice) gets «#2»."""
    out, cur = {}, None
    for page_no, page in enumerate(pages, 1):
        for line in page.splitlines():
            if not line.strip() or re.fullmatch(r"\s*\d+\s*", line):
                continue
            if HEADER_RE.match(line):
                cur = None
                continue
            m = CLAUSE_START_RE.match(line)
            if m and clause_key(m.group(1)) >= CUTOFF:
                return finish(out)  # 4.3 and everything after it is dropped
            if m:
                cid = m.group(1)
                n = 2
                while cid in out:
                    cid = f"{m.group(1)}#{n}"
                    n += 1
                cur = out[cid] = {"page": page_no, "lines": [m.group(2).strip()]}
            elif cur:
                cur["lines"].append(line.strip())
    return finish(out)


def finish(out: dict) -> dict:
    for c in out.values():
        text = ""
        for ln in c.pop("lines"):
            text += ln if text.endswith("-") and not text.endswith(" -") else (" " if text else "") + ln
        c["text"] = re.sub(r"\s+", " ", text).strip()
    return out


def build():
    from protocol_chapter_extractor import pdf_to_pages  # pdftotext; not needed at answer time
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    late = [c["id"] for c in rules["clauses"] if clause_key(c["id"]) >= CUTOFF] + [
        cid for cid in rules["general"] if clause_key(cid) >= CUTOFF]
    if late:
        sys.exit(f"clauses from п. {'.'.join(map(str, CUTOFF))} on are not used, remove them from the rules: {late}")
    if not PDF.exists():
        PDF.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(rules["url"], PDF)
    parsed = parse_clauses(pdf_to_pages(PDF))
    missing = []
    for c in rules["clauses"]:
        p = parsed.get(c["id"])
        if not p:
            missing.append(c["id"])
            continue
        c["page"], c["text"] = p["page"], p["text"]
    rules["texts"] = {cid: parsed[cid] for cid in rules["general"] if cid in parsed}
    missing += [cid for cid in rules["general"] if cid not in parsed]
    if missing:
        sys.exit(f"clauses not found in the PDF: {missing}")
    rules.pop("_comment", None)
    BUILT.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{BUILT}: {len(rules['clauses'])} clauses")


# ---------------------------------------------------------------------------- check
def code_match(code: str, spec: str) -> str | None:
    """'exact' when `code` lies within `spec` («E06.3» in «E06», «A17» in «A15-A19»), 'broader' when
    `code` is a rubric only part of which `spec` covers («E06» vs «E06.3»), else None."""
    if "-" in spec:
        lo, hi = spec.split("-")
        if len(lo) == 3:
            return "exact" if lo <= code[:3] <= hi else None
        if lo <= code <= hi + "~":
            return "exact"
        return "broader" if lo.startswith(code + ".") or hi.startswith(code + ".") else None
    if code == spec or code.startswith(spec + "."):
        return "exact"
    return "broader" if spec.startswith(code + ".") else None


def any_match(code: str, specs) -> tuple[str, str] | None:
    """Best (how, spec) of a list of specs: an exact hit beats a broader one."""
    best = None
    for spec in specs or []:
        how = code_match(code, spec)
        if how == "exact":
            return how, spec
        if how and not best:
            best = (how, spec)
    return best


def in_ids(cid: str, ranges) -> bool:
    """«4.1.17» in ["4.1.12-4.1.28"]."""
    key = clause_key
    for r in ranges:
        lo, _, hi = r.partition("-")
        if cid == r or (hi and key(lo) <= key(cid) <= key(hi)):
            return True
    return False


def word_match(s: str, t: str) -> bool:
    """Stem `s` of a rule phrase occurs as text stem `t`: equal (one typo), or `t` continues it
    («гормон» → «гормональн»); a shorter `t` is not enough — «плазм» is not «плазмаферез»."""
    return close(s, t) or t.startswith(s) or (s.startswith(t) and len(t) >= len(s) - 2)


class Insurance:
    def __init__(self, path: Path = BUILT, norm: Normalizer | None = None):
        self.rules = json.loads(path.read_text(encoding="utf-8"))
        # an older task20.json may still hold п. 4.3+: never used
        self.rules["clauses"] = [c for c in self.rules["clauses"] if clause_key(c["id"]) < CUTOFF]
        self.norm = norm or Normalizer()
        self.url = self.rules["url"]
        self._stems = {}

    def stems(self, phrase):
        if phrase not in self._stems:
            self._stems[phrase] = self.norm.stems(phrase)
        return self._stems[phrase]

    def words_hit(self, phrases, text: str) -> str | None:
        ts = self.norm.stems(text) if text else []
        for p in phrases or []:
            ps = self.stems(p)
            if ps and all(any(word_match(s, t) for t in ts) for s in ps):
                return p
        return None

    def cite(self, c: dict, how: str) -> dict:
        return {"clause": c["id"].split("#")[0], "short": c["short"], "quote": c["text"], "page": c["page"],
                "url": f"{self.url}#page={c['page']}", "how": how, "unless": c.get("unless")}

    def general(self, cid: str) -> dict:
        t = self.rules["texts"][cid]
        return {"clause": cid, "quote": t["text"], "page": t["page"], "url": f"{self.url}#page={t['page']}"}

    def service_limits(self, service: str) -> list:
        out = []
        for c in self.rules["clauses"]:
            if c["kind"] == "service":
                w = self.words_hit(c.get("service_words"), service)
                if w:
                    out.append(self.cite(c, f"услуга «{w}»"))
        return out

    def check(self, code: str, name: str = "", dx_text: str = "", service: str | None = None,
              protocol_level: str | None = "-", items: list | None = None) -> dict:
        """`protocol_level`: the protocol verdict of a check answer (None = not provided for → п. 4.1.50);
        `items`: list mode — texts of the listed services, checked against the service limits."""
        dx_words_text = f"{dx_text} {name}"
        excluded, probable, limited, circumstances, exceptions, person = [], [], [], [], [], []
        for c in self.rules["clauses"]:
            kind = c["kind"]
            if kind == "service":
                continue
            if kind == "person":
                if any_match(code, c["codes"]) and any_match(code, c["codes"])[0] == "exact":
                    person.append(self.cite(c, f"код {code}"))
                continue
            if kind in ("circumstance", "partial", "program"):
                hit = any_match(code, c.get("codes"))
                w = self.words_hit(c.get("service_words"), service) if kind == "partial" and service else None
                if hit and hit[0] == "exact" or w:
                    cite = self.cite(c, f"код {code} ∈ {hit[1]}" if hit else f"услуга «{w}»")
                    (circumstances if kind == "circumstance" else limited).append(cite)
                continue
            exc = any_match(code, c.get("except_codes"))
            if exc and exc[0] == "exact":
                exceptions.append(self.cite(c, f"код {code} ∈ {exc[1]} — исключение из исключения"))
                continue
            named = any_match(code, c.get("named"))
            cls = any_match(code, c.get("class"))
            w = self.words_hit(c.get("dx_words"), dx_words_text)
            if named and named[0] == "exact":
                excluded.append(self.cite(c, f"код {code} ∈ {named[1]}"))
            elif w:
                excluded.append(self.cite(c, f"в диагнозе «{w}»"))
            elif cls and cls[0] == "exact":
                probable.append(self.cite(c, f"код {code} ∈ группа {cls[1]}"))
            elif named or cls:
                spec = (named or cls)[1]
                probable.append(self.cite(c, f"рубрика {code} включает исключённый {spec}"))
        level = "excluded" if excluded else "probable" if probable else "limited" if limited else "covered"
        notes = []
        until = [m["clause"] for m in excluded + probable if in_ids(m["clause"], self.rules["until_final_diagnosis"])]
        if until:
            notes.append(dict(self.general("4.2"), why="до установления окончательного диагноза обследование и лечение оплачиваются"))
        if level in ("covered", "limited"):
            if re.search(r"хронич", dx_words_text, re.I) and not re.search(r"обостр", dx_text, re.I):
                notes.append(dict(self.general("4.1.1"), why="хроническое заболевание вне обострения не оплачивается"))
            notes.append(dict(self.general("3.1"), why="что считается страховым случаем"))
        if protocol_level is None:
            notes.append(dict(self.general("4.1.50"), why="услуга не найдена в протоколе для этого диагноза — не оплачивается"))
        limits = self.service_limits(service) if service else []
        if items:
            seen = {}
            for t in items:
                for lim in self.service_limits(t):
                    seen.setdefault(lim["clause"], dict(lim, examples=[]))["examples"].append(t.rstrip(" ;,."))
            limits = list(seen.values())
        return {
            "rules": self.rules["title"], "url": self.url,
            "verdict_level": level,
            "verdict": VERDICTS[level] + (f" (п. {', '.join(m['clause'] for m in excluded or probable)})" if excluded or probable else ""),
            "excluded": excluded + probable, "limited": limited, "exceptions": exceptions,
            "circumstances": circumstances, "person": person, "service_excluded": limits, "notes": notes,
        }


def render(ins: dict) -> str:
    out = ["", "─" * 70, f"СТРАХОВАНИЕ: {ins['rules']}", f"ВЕРДИКТ: {ins['verdict']}"]

    def block(title, rows, quote=True):
        if not rows:
            return
        out.append("")
        out.append(title)
        for m in rows:
            out.append(f"  п. {m['clause']} — {m['short']}  [{m['how']}]")
            if m.get("unless"):
                out.append(f"    Оговорка: {m['unless']}")
            if m.get("examples"):
                out.append(f"    В перечне: {'; '.join(m['examples'][:3])}" + (f" и ещё {len(m['examples']) - 3}" if len(m["examples"]) > 3 else ""))
            if quote:
                out.append(f"    «{m['quote']}»")
            out.append(f"    {m['url']}")

    block("Исключение:", ins["excluded"])
    block("Не исключено (оговорка пункта):", ins["exceptions"])
    block("Ограничения по услугам для этого заболевания:", ins["limited"])
    block("Услуга не оплачивается:", ins["service_excluded"])
    if ins["circumstances"]:
        c = ins["circumstances"]
        out.append("")
        out.append(f"Травма не оплачивается, если получена (п. {c[0]['clause']}–{c[-1]['clause']}): " + "; ".join(m["short"].removeprefix("травма ") for m in c))
        out.append(f"    {c[0]['url']}")
    block("Договор не заключается с лицами (п. 1.4):", ins["person"], quote=False)
    for n in ins["notes"]:
        out.append("")
        out.append(f"п. {n['clause']}: {n['why']}")
        out.append(f"    «{n['quote']}»")
        out.append(f"    {n['url']}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    c = sub.add_parser("check")
    c.add_argument("code")
    c.add_argument("--text", default="")
    c.add_argument("--service")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd == "build":
        return build()
    print(render(Insurance().check(args.code, dx_text=args.text, service=args.service)))


if __name__ == "__main__":
    main()
