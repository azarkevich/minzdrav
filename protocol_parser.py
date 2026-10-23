"""Parse every protocol PDF in ./protocols into a structured JSON document tree —
the input of the knowledge-base extraction step of the expert system.

    file → protocol (a file may hold several) → chapter («ГЛАВА N») → clause («73.»)
         → paragraph (page, sub-clause id, text, МКБ-10 codes, diagnostics marker)

Every paragraph keeps the PDF page it starts and ends on, so an answer can cite
`<url>#page=N` together with a verbatim quote. Page numbers are physical PDF pages
(what a viewer's #page= uses); the number printed in the page footer is kept as
`printed`.

Requires `pdftotext` on PATH and the reference built by reference_builder.py
(reference/icd10.json — used to validate МКБ-10 codes). Standard library only otherwise.

Usage:
    python -I protocol_parser.py [--protocols protocols] [--out parsed] [--only ТЕКСТ] [--force] [-w 4]

Output (<out>, default ./parsed):
    docs/<doc_id>.json   one per PDF; doc_id = first 16 hex chars of the file's sha256
    index.json           doc_id → path, url, title, population, stats
    coverage.csv         per-document stats to spot files that need special handling
A document is re-parsed only when its sha256 or PARSER_VERSION changes.
"""
import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir
from protocol_chapter_extractor import (CHAPTER_RE, CLAUSE_RE, PARA_INDENT, build_glossary,
                                        pdf_to_pages)

PARSER_VERSION = 3

PAGE_NO_RE = re.compile(r"^\s*(\d{1,4})\s*$")
HEADER_RE = re.compile(r"^\s*\(?Национальный правовой Интернет-портал.*$")
PROTOCOL_RE = re.compile(r"^\s*КЛИНИЧЕСКИ[ЙЕ]\s+ПРОТОКОЛ[Ы]?\b")
HEADING_INDENT = 15  # centered headings (chapter titles) are indented further than this

# МКБ-10 code as printed: Latin or look-alike Cyrillic letter, "E06", "E06.3", "O90.5*".
ICD_RE = re.compile(r"(?<![\wА-ЯЁа-яё])([A-ZАВЕКМНОРСТХ])(\d{2})(?:\.(\d{1,2}))?[*+]?(?![\wА-ЯЁа-яё])")
ICD_RANGE_RE = re.compile(
    r"(?<![\wА-ЯЁа-яё])([A-ZАВЕКМНОРСТХ]\d{2}(?:\.\d{1,2})?)\s*[–—-]\s*([A-ZАВЕКМНОРСТХ]?\d{2}(?:\.\d{1,2})?)(?![\wА-ЯЁа-яё])")
# «Е 21.0» — a space after the letter; only trusted next to «МКБ» («С 20 лет» is not C20)
ICD_SPACED_RE = re.compile(r"(?<![\wА-ЯЁа-яё])([A-ZАВЕКМНОРСТХ])\s(\d{2})(?:\.(\d{1,2}))?(?![\wА-ЯЁа-яё])")
CYR2LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")

# Rough markers of diagnostics lists — only for the coverage report and as hints for
# the extraction step; the wording varies a lot between protocols.
DX_MARKERS = [
    ("mandatory", re.compile(r"обязательн\w*\s+(?:диагност|исследован|лабораторн|инструментальн|медицинск\w+\s+(?:услуг|вмешательств|обследован))", re.I)),
    ("additional", re.compile(r"дополнительн\w*\s+(?:диагност|исследован|лабораторн|инструментальн|медицинск\w+\s+(?:услуг|вмешательств|обследован))", re.I)),
    ("diagnostics", re.compile(r"(?:для\s+(?:постановки|подтверждения|установления)\s+(?:клинического\s+)?диагноза|диагностическ\w+\s+(?:мероприяти|исследовани|процедур))", re.I)),
    ("monitoring", re.compile(r"(?:план|объем|кратность)\s+(?:медицинского\s+|диспансерного\s+)?наблюдени|медицинское\s+наблюдение", re.I)),
]


class Icd:
    """МКБ-10 dictionary from reference/icd10.json, for validating and expanding codes."""

    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.names = {c["code"]: c["name"] for c in data["codes"] if re.fullmatch(r"[A-Z]\d{2}(\.\d{1,2})?", c["code"])}
        self.ordered = sorted(self.names)

    def norm(self, letter, num, sub=None):
        code = letter.translate(CYR2LAT) + num + (f".{sub}" if sub else "")
        return code if code in self.names else None

    def expand(self, lo: str, hi: str) -> list[str]:
        lo = lo.translate(CYR2LAT)
        hi = hi.translate(CYR2LAT)
        if hi[0].isdigit():  # "E10–14"
            hi = lo[0] + hi
        if lo not in self.names or hi not in self.names or hi < lo:
            return []
        depth = len(lo)
        return [c for c in self.ordered if lo <= c <= hi and len(c) == depth]

    def find(self, text: str) -> list[str]:
        codes = []
        for m in ICD_RANGE_RE.finditer(text):
            codes += self.expand(m.group(1), m.group(2))
        for m in ICD_RE.finditer(text):
            c = self.norm(m.group(1), m.group(2), m.group(3))
            if not c or re.search(r"CD\s*$", text[max(0, m.start() - 4):m.start()]):
                continue  # "CD B20" is an antigen
            if c[0] in "VWXY" and "МКБ" not in text:
                continue  # external causes are rare in protocols; "V03"/"V75" are ATC codes, doses
            codes.append(c)
        if "МКБ" in text:
            for m in ICD_SPACED_RE.finditer(text):
                c = self.norm(m.group(1), m.group(2), m.group(3))
                if c:
                    codes.append(c)
        return sorted(set(codes))


def page_lines(pages: list[str]):
    """Per page: printed page number and body lines (page header/footer removed)."""
    out = []
    for page in pages:
        printed, body = None, []
        for line in page.splitlines():
            if HEADER_RE.match(line):
                continue
            m = PAGE_NO_RE.match(line)
            if m:
                printed = int(m.group(1))
                continue
            body.append(line.rstrip())
        out.append((printed, body))
    return out


def table_like(lines: list[str]) -> bool:
    """Multi-column table pages: many lines with wide gaps between text runs."""
    text_lines = [l for l in lines if len(l.strip()) > 3]
    if len(text_lines) < 10:
        return False
    # body text pages: < 0.1; table pages: 0.3–0.9
    gappy = sum(1 for l in text_lines if re.search(r"\S\s{3,}\S", l.strip()))
    return gappy / len(text_lines) > 0.3


def flat_pages(per_page) -> set[int]:
    """Pages where pdftotext lost the indentation: no line starts with a paragraph indent."""
    flat = set()
    for n, (_, body) in enumerate(per_page, start=1):
        text = [l for l in body if l.strip()]
        indents = [len(l) - len(l.lstrip()) for l in text]
        if len(text) >= 10 and not any(PARA_INDENT <= i <= HEADING_INDENT for i in indents):
            flat.add(n)
    return flat


def paragraphs(lines, flat: set[int] = frozenset()):
    """[(page, line)] → [{"page", "page_end", "text", "heading"}] rebuilt from indentation.
    On `flat` pages a paragraph starts at a numbered clause or after a line that ends
    a sentence or a list item."""
    paras, cur = [], None
    for page, line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        heading = indent > HEADING_INDENT
        starts = cur is None or indent >= PARA_INDENT or heading != cur["heading"] \
            or CHAPTER_RE.match(line) or PROTOCOL_RE.match(line)
        if page in flat and cur is not None and not starts:
            starts = bool(CLAUSE_RE.match(text)) or cur["text"].endswith((";", ":")) \
                or cur["text"].endswith(".") and text[:1].isupper()
        if cur is not None and cur["heading"] and heading and not CHAPTER_RE.match(cur["text"]) \
                and not CHAPTER_RE.match(line):
            starts = False  # multi-line centered title
        if starts:
            cur = {"page": page, "page_end": page, "text": text, "heading": heading}
            paras.append(cur)
            continue
        cur["page_end"] = page
        if cur["text"].endswith("-") and not cur["text"].endswith(" -"):
            cur["text"] += text  # "врачей-" + "специалистов"
        else:
            cur["text"] += " " + text
    for p in paras:
        p["text"] = re.sub(r"\s{2,}", " ", p["text"])
    return paras


def split_protocols(paras):
    """A file can contain several protocols, each starting with «КЛИНИЧЕСКИЙ ПРОТОКОЛ»."""
    starts = [i for i, p in enumerate(paras) if PROTOCOL_RE.match(p["text"]) and p["text"].isupper()
              or PROTOCOL_RE.match(p["text"]) and p["heading"]]
    if not starts:
        return [(None, paras)]
    starts[0] = 0  # preamble (decree text) goes with the first protocol
    out = []
    for k, s in enumerate(starts):
        part = paras[s:starts[k + 1] if k + 1 < len(starts) else len(paras)]
        out.append((protocol_title(part), part))
    return out


def protocol_title(part):
    for i, p in enumerate(part):
        if PROTOCOL_RE.match(p["text"]):
            title = PROTOCOL_RE.sub("", p["text"]).strip()
            j = i + 1
            while not title and j < len(part) and part[j]["heading"]:
                title = part[j]["text"]
                j += 1
            return title.strip(" «»\"") or None
    return None


def build_chapters(paras, icd: Icd):
    """Group paragraphs into chapters and top-level clauses."""
    chapters, ch, clause = [], None, None
    pending_title = False
    for p in paras:
        m = CHAPTER_RE.match(p["text"])
        if m or ch is None:
            ch = {"no": int(m.group(1)) if m else None, "title": "", "page": p["page"], "clauses": []}
            chapters.append(ch)
            clause = None
            pending_title = bool(m)
            if m:
                continue
        # long titles are not centered, but they are upper case
        if pending_title and (p["heading"] or p["text"].isupper() and not CLAUSE_RE.match(p["text"])):
            ch["title"] = (ch["title"] + " " + p["text"]).strip()
            continue
        pending_title = False
        cm = CLAUSE_RE.match(p["text"])
        if cm and "." not in cm.group(1) or clause is None:
            clause = {"id": cm.group(1) if cm and "." not in cm.group(1) else None,
                      "page": p["page"], "page_end": p["page_end"], "paragraphs": []}
            ch["clauses"].append(clause)
        clause["page_end"] = p["page_end"]
        para = {"page": p["page"], "page_end": p["page_end"], "text": p["text"]}
        if cm and "." in cm.group(1):
            para["sub"] = cm.group(1)
        if p["heading"]:
            para["heading"] = True
        codes = icd.find(p["text"])
        if codes:
            para["icd10"] = codes
        markers = [name for name, rx in DX_MARKERS if rx.search(p["text"])]
        if markers:
            para["dx"] = markers
        clause["paragraphs"].append(para)
    for ch in chapters:
        for cl in ch["clauses"]:
            cl["icd10"] = sorted({c for p in cl["paragraphs"] for c in p.get("icd10", [])})
        ch["icd10"] = sorted({c for cl in ch["clauses"] for c in cl["icd10"]})
    return chapters


def population(title: str) -> list[str]:
    t = (title or "").lower()
    pop = []
    if re.search(r"взросл|\bвзр\b", t):
        pop.append("adult")
    if re.search(r"дет|новорожд|недоношен|ребен|подрост|\bд-нас", t):
        pop.append("child")
    return pop


def decree(title: str) -> dict:
    t = title or ""
    m = re.search(r"от\s+(\d{1,2}[.\s]\d{1,2}[.\s]\d{4}|\d{1,2}\s+\w+\s+\d{4})\s*(?:г\.?)?\s*№\s*([\w\-/.]+?)(?:\)|$|\s)", t)
    return {"date": m.group(1), "number": m.group(2)} if m else {}


def parse_doc(pdf: Path, meta: dict, url: str, icd: Icd) -> dict:
    pages = pdf_to_pages(pdf)
    if pages and not pages[-1].strip():
        pages.pop()  # pdftotext ends every page with "\f", so the last split is empty
    per_page = page_lines(pages)
    lines = [(n, l) for n, (_, body) in enumerate(per_page, start=1) for l in body]
    flat = flat_pages(per_page)
    paras = paragraphs(lines, flat)
    glossary = build_glossary("\n".join(p["text"] for p in paras))

    protocols = []
    for k, (title, part) in enumerate(split_protocols(paras)):
        chapters = build_chapters(part, icd)
        title = title or meta.get("title")
        protocols.append({
            "index": k,
            "title": title,
            "population": population(title) or population(meta.get("title")) or population(pdf.name),
            "page": part[0]["page"] if part else 1,
            "icd10": sorted({c for ch in chapters for c in ch["icd10"]}),
            "chapters": chapters,
        })

    page_info = []
    for n, ((printed, body), raw) in enumerate(zip(per_page, pages), start=1):
        info = {"page": n, "chars": len("".join(body).replace(" ", ""))}
        if printed is not None:
            info["printed"] = printed
        if table_like(body):
            info["table"] = True
        if n in flat:
            info["flat"] = True
        page_info.append(info)

    all_paras = [p for pr in protocols for ch in pr["chapters"] for cl in ch["clauses"] for p in cl["paragraphs"]]
    stats = {
        "pages": len(page_info),
        "chars": sum(p["chars"] for p in page_info),
        "no_text_pages": sum(1 for p in page_info if p["chars"] < 100),
        "table_pages": sum(1 for p in page_info if p.get("table")),
        "flat_pages": len(flat),
        "protocols": len(protocols),
        "chapters": sum(1 for pr in protocols for ch in pr["chapters"] if ch["no"] is not None),
        "clauses": sum(1 for pr in protocols for ch in pr["chapters"] for cl in ch["clauses"] if cl["id"]),
        "paragraphs": len(all_paras),
        "icd10": len({c for pr in protocols for c in pr["icd10"]}),
        **{f"dx_{name}": sum(1 for p in all_paras if name in p.get("dx", [])) for name, _ in DX_MARKERS},
    }
    if stats["no_text_pages"] > stats["pages"] / 2:
        kind = "scan"  # image pages, often with only the cover order recognized as text
    elif stats["table_pages"] > stats["pages"] * 0.3:
        kind = "table"
    elif stats["chapters"] == 0:
        kind = "no_chapters"
    else:
        kind = "standard"
    stats["kind"] = kind

    return {
        "doc_id": meta["sha256"][:16],
        "sha256": meta["sha256"],
        "parser_version": PARSER_VERSION,
        "url": url,
        "path": meta["path"].replace("\\", "/"),
        "title": meta.get("title"),
        "categories": meta.get("categories", []),
        "decree": decree(meta.get("title")),
        "population": population(meta.get("title")) or population(pdf.name),
        "stats": stats,
        "pages": page_info,
        "glossary": glossary,
        "protocols": protocols,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--protocols", type=Path, default=Path("protocols"))
    ap.add_argument("--reference", type=Path, default=Path("reference"))
    ap.add_argument("--out", type=Path, default=Path("parsed"))
    ap.add_argument("--only", help="parse only files whose path contains this text")
    ap.add_argument("--force", action="store_true", help="re-parse even if up to date")
    ap.add_argument("-w", "--workers", type=int, default=4)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    icd_path = args.reference / "icd10.json"
    if not icd_path.exists():
        sys.exit(f"{icd_path} not found — run reference_builder.py first")
    icd = Icd(icd_path)
    manifest = json.loads((args.protocols / "manifest.json").read_text(encoding="utf-8"))["documents"]
    docs_dir = args.out / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(url, m) for url, m in manifest.items()
            if not m.get("removed") and m.get("sha256") and (not args.only or args.only in m["path"])]

    def run(job):
        url, m = job
        out = docs_dir / f"{m['sha256'][:16]}.json"
        if out.exists() and not args.force:
            try:
                old = json.loads(out.read_text(encoding="utf-8"))
                if old.get("parser_version") == PARSER_VERSION and old.get("sha256") == m["sha256"]:
                    return old, False
            except (ValueError, OSError):
                pass
        doc = parse_doc(args.protocols / m["path"], m, url, icd)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return doc, True

    results, failed = [], []
    with ThreadPoolExecutor(args.workers) as pool:
        for job, fut in [(j, pool.submit(run, j)) for j in jobs]:
            try:
                results.append(fut.result())
            except Exception as e:  # keep going; report at the end
                failed.append((job[1]["path"], repr(e)))

    if not args.only:
        index = {d["doc_id"]: {k: d[k] for k in ("path", "url", "title", "categories", "decree", "population")}
                 | {"stats": d["stats"], "protocols": [{"title": p["title"], "population": p["population"],
                                                        "icd10": p["icd10"]} for p in d["protocols"]]}
                 for d, _ in results}
        (args.out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        with (args.out / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as f:
            cols = list(results[0][0]["stats"]) if results else []
            w = csv.writer(f, delimiter=";")
            w.writerow(["doc_id", "path"] + cols)
            for d, _ in sorted(results, key=lambda r: r[0]["path"]):
                w.writerow([d["doc_id"], d["path"]] + [d["stats"][c] for c in cols])

    kinds = {}
    for d, _ in results:
        kinds[d["stats"]["kind"]] = kinds.get(d["stats"]["kind"], 0) + 1
    print(f"{len(results)} documents ({sum(1 for _, n in results if n)} parsed, "
          f"{sum(1 for _, n in results if not n)} up to date) → {args.out}")
    print("  kinds: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print(f"  with МКБ-10 codes: {sum(1 for d, _ in results if d['stats']['icd10'])}, "
          f"with 'обязательная диагностика' markers: {sum(1 for d, _ in results if d['stats']['dx_mandatory'])}")
    for path, err in failed:
        print(f"  FAILED {path}: {err}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
