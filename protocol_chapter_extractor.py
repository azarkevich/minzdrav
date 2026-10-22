"""Extract one chapter ("ГЛАВА N <TITLE>") from a text PDF and save it as
Markdown + JSONL chunks ready for embedding / RAG.

Requires `pdftotext` (poppler) on PATH. Standard library only otherwise.

Usage:
    python protocol_chapter_extractor.py <file.pdf> [--title ДИАГНОСТИКА] [--chapter 3]
                                         [--out-dir chapters] [--max-chars 1500]

Output goes to <out-dir>/<pdf name>/chapterNN.{md,jsonl} + glossary.json.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Running header/footer lines repeated on every page of the source document.
NOISE_RE = re.compile(r"^\s*(\d{1,3}|Национальный правовой Интернет-портал.*)\s*$")
CHAPTER_RE = re.compile(r"^\s*ГЛАВА\s+(\d+)\s*$")
# Numbered clause at the start of a paragraph: "11." / "11.1." / "11.1.2."
CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s")
ABBR_DEF_RE = re.compile(r"\(далее\s*[–-]\s*([A-Za-zА-ЯЁа-яё\-]+)\)")
STOP_WORDS = {"и", "в", "во", "с", "со", "по", "при", "на", "к", "о", "об"}
PARA_INDENT = 5 # body paragraphs start with ~7 spaces; continuation lines with 0


def pdf_to_pages(pdf: Path) -> list[str]:
    # The Windows xpdf pdftotext can't open non-ASCII paths (every file in
    # protocols/ has a Cyrillic name) and can't read stdin, so hand it a copy.
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input.pdf"
        shutil.copyfile(pdf, src)
        out = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(src), "-"],
            capture_output=True, check=True,
        ).stdout.decode("utf-8")
    return out.split("\f")


def logical_lines(pages: list[str]):
    """Yield (page_no, line) with page headers/footers removed."""
    for page_no, page in enumerate(pages, start=1):
        for line in page.splitlines():
            if NOISE_RE.match(line):
                continue
            yield page_no, line.rstrip()


def find_chapter(lines, chapter_no, title):
    """Return (chapter_no, title, [(page, line)...]) for the requested chapter."""
    starts = []
    for i, (_, line) in enumerate(lines):
        m = CHAPTER_RE.match(line)
        if m:
            # Title = following non-empty lines until the first body paragraph.
            title_lines, j = [], i + 1
            while j < len(lines) and (not lines[j][1].strip() or len(lines[j][1]) - len(lines[j][1].lstrip()) > 15):
                if lines[j][1].strip():
                    title_lines.append(lines[j][1].strip())
                j += 1
            starts.append((i, j, int(m.group(1)), " ".join(title_lines)))
    if not starts:
        sys.exit("No 'ГЛАВА N' headings found")

    for k, (i, body, no, ttl) in enumerate(starts):
        hit = (chapter_no is not None and no == chapter_no) or (
            chapter_no is None and ttl.upper().startswith(title.upper()))
        if hit:
            end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
            return no, ttl, lines[body:end]
    found = "\n".join(f"  {no}: {ttl}" for _, _, no, ttl in starts)
    sys.exit(f"Chapter not found. Available:\n{found}")


def reflow(lines):
    """Join wrapped lines into paragraphs. Returns [(first_page, text)]."""
    paras, cur, cur_page = [], [], None
    for page, line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent >= PARA_INDENT and cur:
            paras.append((cur_page, " ".join(cur)))
            cur = []
        if not cur:
            cur_page = page
        text = line.strip()
        if cur and cur[-1].endswith("-") and not cur[-1].endswith(" -"):
            cur[-1] += text  # "врачей-" + "специалистов"
        else:
            cur.append(text)
    if cur:
        paras.append((cur_page, " ".join(cur)))
    return [(p, re.sub(r"\s{2,}", " ", t)) for p, t in paras]


def build_glossary(all_text: str) -> dict:
    """Best-effort: '... низкой плотности (далее – ЛПНП)' -> {'ЛПНП': '...'}.
    Matches abbreviation letters against initials of the preceding words."""
    glossary = {}
    for m in ABBR_DEF_RE.finditer(all_text):
        abbr = m.group(1)
        words = re.findall(r"[A-Za-zА-ЯЁа-яё0-9\-]+", all_text[max(0, m.start() - 200):m.start()])
        letters = [c for c in abbr.upper() if c.isalpha()]
        taken, li = [], len(letters) - 1
        for w in reversed(words):
            if li < 0:
                break
            parts = [p for p in w.split("-") if p]
            initials = "".join(p if p.isupper() else p[0] for p in parts).upper()
            if initials and "".join(letters[max(0, li - len(initials) + 1):li + 1]) == initials:
                taken.insert(0, w)
                li -= len(initials)
            elif taken and w.lower() in STOP_WORDS:
                taken.insert(0, w)
            else:
                break
        if li < 0:
            glossary.setdefault(abbr, " ".join(taken))
            continue
        # Fallback for non-initialism abbreviations (ЭКГ, ЛПНП): back up to the
        # nearest word starting with the abbreviation's first letter.
        for n, w in enumerate(reversed(words[-6:]), start=1):
            if w[0].upper() == letters[0]:
                glossary.setdefault(abbr, " ".join(words[-n:]))
                break
    return glossary


def split_clauses(paras):
    """Group paragraphs into top-level clauses ("11.", "12.", ...)."""
    clauses, cur = [], None
    for page, text in paras:
        m = CLAUSE_RE.match(text)
        if m and "." not in m.group(1):
            cur = {"clause": m.group(1), "page": page, "paras": []}
            clauses.append(cur)
        elif cur is None:
            cur = {"clause": None, "page": page, "paras": []}
            clauses.append(cur)
        cur["paras"].append((page, text))
    return clauses


def chunk_clause(clause, max_chars):
    """One chunk per clause; oversized clauses split on sub-clause boundaries,
    each part prefixed with the clause intro so it stays self-contained."""
    paras = clause["paras"]
    full = "\n".join(t for _, t in paras)
    if len(full) <= max_chars:
        return [(clause["clause"], clause["page"], full)]
    intro_page, intro = paras[0]
    groups, cur = [], None
    for page, text in paras[1:]:
        m = CLAUSE_RE.match(text)
        if m and m.group(1).count(".") == 1 or cur is None:
            cur = {"id": m.group(1) if m else clause["clause"], "page": page, "texts": []}
            groups.append(cur)
        cur["texts"].append(text)
    return [(g["id"], g["page"], intro + "\n" + "\n".join(g["texts"])) for g in groups]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--title", default="ДИАГНОСТИКА", help="chapter title prefix (case-insensitive)")
    ap.add_argument("--chapter", type=int, help="chapter number (overrides --title)")
    ap.add_argument("--out-dir", type=Path, default=Path("chapters"))
    ap.add_argument("--max-chars", type=int, default=1500)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    pages = pdf_to_pages(args.pdf)
    lines = list(logical_lines(pages))
    all_paras = reflow(lines)
    all_text = "\n".join(t for _, t in all_paras)
    glossary = build_glossary(all_text)

    doc_title_m = re.search(r"клинический протокол\s+«([^»]+)»", all_text, re.I)
    doc_title = doc_title_m.group(1) if doc_title_m else args.pdf.stem

    ch_no, ch_title, ch_lines = find_chapter(lines, args.chapter, args.title)
    paras = reflow(ch_lines)

    out_dir = args.out_dir / args.pdf.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"chapter{ch_no:02d}"

    # Markdown: human-readable, also fine for loaders that do their own splitting.
    md = [f"# {doc_title}", "", f"## Глава {ch_no}. {ch_title}", ""]
    md += [t + "\n" for _, t in paras]
    base.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")

    # JSONL: one self-contained chunk per line with metadata for filtering/citing.
    chunks = []
    for clause in split_clauses(paras):
        for clause_id, page, text in chunk_clause(clause, args.max_chars):
            used = sorted(a for a in glossary if re.search(rf"(?<![\wА-ЯЁа-яё]){re.escape(a)}(?![\wА-ЯЁа-яё])", text))
            context = f"{doc_title}. Глава {ch_no}. {ch_title}." + (f" Пункт {clause_id}." if clause_id else "")
            chunks.append({
                "id": f"{args.pdf.stem}:ch{ch_no}:p{clause_id or 0}:{len(chunks)}",
                "source": args.pdf.name,
                "document": doc_title,
                "chapter": ch_no,
                "chapter_title": ch_title,
                "clause": clause_id,
                "page": page,
                "text": text,
                # Text to embed: context header + body + expanded abbreviations.
                "embedding_text": context + "\n" + text + (
                    "\nСокращения: " + "; ".join(f"{a} – {glossary[a]}" for a in used) if used else ""),
                "abbreviations": {a: glossary[a] for a in used},
                "appendix_refs": sorted(set(re.findall(r"приложени[июея]+\s+(\d+)", text))),
            })
    with base.with_suffix(".jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    (out_dir / "glossary.json").write_text(
        json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Chapter {ch_no}: {ch_title}")
    print(f"  {base.with_suffix('.md')}  ({len(paras)} paragraphs)")
    print(f"  {base.with_suffix('.jsonl')}  ({len(chunks)} chunks)")
    print(f"  {out_dir / 'glossary.json'}  ({len(glossary)} abbreviations)")


if __name__ == "__main__":
    main()
