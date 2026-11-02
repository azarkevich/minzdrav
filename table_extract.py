"""Diagnostics from table-layout protocols (stage 4): older protocols (2005–2012), big
consolidated ones and tables inside modern protocols

    Наименование нозологических форм (шифр по МКБ-10) | Диагностика: обязательная |
    кратность | дополнительная (по показаниям) | Лечение | …
                1                    2            3          4             5

Tables are found by their ruling lines with pdfplumber (text layout alone cannot tell where
a table row ends: cells wrap independently). The column roles come from the header cells,
merged header cells included («Диагностика» over «обязательная | кратность |
дополнительная»); a table that continues on the next page (only the «1 2 3 …» numbers row,
or no header at all, with the same columns) inherits the header of the previous page. A row
whose name cell is empty or starts in lower case continues the previous nosology (the
«Районный / Областной уровень» sub-rows, a row cut by a page break). Bodies without ruling
lines (2007 orders: only the header is ruled) are cut into the header's columns by the x of
the words, rows by vertical gaps.

Each nosology row becomes {page, page_end, name, icd10, mandatory: [...], additional: [...],
listed: [...]} (listed = a diagnostics column that says neither «обязательная» nor
«дополнительная»). Rows are cached per PDF sha256 in parsed/tables/ (pdfplumber is slow).

Used by kb_build.py; standalone for inspection:
    python -I table_extract.py "protocols/Урология/<file>.pdf" [N rows]
"""
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

import pdfplumber

VERSION = 5  # bump to invalidate parsed/tables/ caches
CACHE = Path("parsed/tables")

ICD_RE = re.compile(r"(?<![\wА-ЯЁа-яё])([A-ZАВЕКМНОРСТХ])\s?(\d{2})(?:(?:\s?\.\s?|\s(?=\d(?:[;,)]|\s|$))|(?=\d(?!\d)))(\d{1,2}))?")
CYR2LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")
NAME_RE = re.compile(r"нозолог|наименовани\w* (?:но|заб)|наименование$|^диагноз|шифр|название синдрома|осложнени", re.I)
DIAG_RE = re.compile(r"диагност|обследова|исследова", re.I)
NOT_DIAG_RE = re.compile(r"лечени|кратн|уровень|длительн|организаци|исход|примечан|критери|реабилитаци(?!онно-эксперт)", re.I)
CELL_BREAK = "\x00"
CONT_WORD_RE =re.compile(r"(?:^|\s)(?:и|или|на|в|во|к|с|со|по|для|при|от|до|из|о|об|без|под|над|через|между)$", re.I)
SPANNING_RE =re.compile(r"(?:Диагностика и лечение|Объемы оказания|При оказании)\b", re.I)
HEADER_WORDS =re.compile(r"наименовани|нозолог|диагност|обязательн|дополнительн|лечени|кратн|№\s*п/п", re.I)


def norm(s) -> str:
    s = re.sub(r"(\w)-\s*\n\s*(?=[а-яё])", r"\1", s or "")  # «обязатель-\nная»
    return re.sub(r"\s+", " ", s.replace("\n", " ")).strip()


def is_numbers_row(row) -> bool:
    vals = [norm(c) for c in row if norm(c)]
    return len(vals) >= 2 and all(re.fullmatch(r"\d{1,2}", v) for v in vals) \
        and [int(v) for v in vals] == list(range(int(vals[0]), int(vals[0]) + len(vals)))


def icd_codes(text, icd_names):
    out = []
    for m in ICD_RE.finditer(text):
        c = m.group(1).translate(CYR2LAT) + m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
        if c in icd_names:
            out.append(c)
        elif c[:3] in icd_names and m.group(3):
            out.append(c[:3])
    return list(dict.fromkeys(out))


def join_lines(parts):
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out.endswith("-") and not out.endswith(" -"):
            # «обследова-» + «ние» is a wrap; «врача-» + «инфекциониста» keeps its hyphen
            compound = re.search(r"(?:врач\w*|медико|социально|рентгено)-$", out, re.I)
            out = out[:-1] + p if p[:1].islower() and not compound else out + p
        else:
            out = (out + " " + p).strip()
    return out


def split_items(lines):
    """Cell lines → items: a new item starts with an upper-case letter or «1.» unless the
    previous line was cut by a hyphen, ended with a comma/colon/dash or a preposition, or left
    a bracket open («(далее –» + «ПЦР)»). Cells whose items end with «.» / «;» are split only
    there (or at «1.»): «…выявление ДНК» + «ВЭБ, ЦМВ…» is one item. CELL_BREAK = cell border."""
    text = [l.strip() for l in lines if l.strip() and l != CELL_BREAK]
    ends = sum(1 for l in text if l.endswith((".", ";")))
    punctuated = ends >= 2 and ends >= 0.25 * len(text)
    items, cur = [], []
    for l in lines:
        if l == CELL_BREAK:
            if cur:
                items.append(join_lines(cur))
                cur = []
            continue
        l = l.strip()
        if not l:
            continue
        if re.fullmatch(r"[\d,\s-]+", l):  # «кратность» numbers that leaked into the cell
            continue
        numbered = re.match(r"\d{1,2}[.)]\s", l)
        new = l[:1].isupper() or numbered
        joined = " ".join(cur)
        if punctuated and cur and not numbered and not cur[-1].endswith((".", ";")):
            new = False
        if cur and new and not cur[-1].endswith(("-", ",", ":", "(", "–", "—")) \
                and not CONT_WORD_RE.search(cur[-1]) and joined.count("(") <= joined.count(")"):
            items.append(join_lines(cur))
            cur = []
        cur.append(l)
    if cur:
        items.append(join_lines(cur))
    return [i for i in items if len(i) > 2]


# ---------------------------------------------------------------- header → column roles
def column_spans(table):
    """(x0, x1) of each grid column: the narrowest cell starting in it (`table.columns` is
    widened by merged cells)."""
    spans = []
    for j, col in enumerate(table.columns):
        cells = [row.cells[j] for row in table.rows if j < len(row.cells) and row.cells[j]]
        spans.append((cells[0][0], min(c[2] for c in cells)) if cells else (col.bbox[0], col.bbox[2]))
    return spans


def header_texts(table, rows, n_header):
    """Column index → text of every header cell covering it (merged cells cover several)."""
    spans = column_spans(table)
    texts = [[] for _ in spans]
    for r in range(n_header):
        for j, bbox in enumerate(table.rows[r].cells):
            if bbox is None:
                continue
            t = norm(rows[r][j])
            if not t:
                continue
            for i, (x0, x1) in enumerate(spans):
                if bbox[0] - 1 <= (x0 + x1) / 2 <= bbox[2] + 1:
                    texts[i].append(t)
    return [" / ".join(t) for t in texts]


def roles_of(htexts):
    """Column roles from header texts: name, mandatory, additional, listed."""
    roles = {}
    for i, h in enumerate(htexts):
        last = h.split(" / ")[-1] if h else ""
        if "name" not in roles and NAME_RE.search(h) and not re.search(r"обследовани|исследовани|мероприят", last, re.I):
            roles["name"] = i
            continue
        diag = DIAG_RE.search(h)
        if re.search(r"обязательн", last, re.I) and diag:
            roles.setdefault("mandatory", i)
        elif re.search(r"дополнительн|по показани", last, re.I) and diag:
            roles.setdefault("additional", i)
        elif diag and not NOT_DIAG_RE.search(last) and not re.search(r"обязательн|дополнительн", h, re.I):
            roles.setdefault("listed", i)
    if not ({"mandatory", "additional", "listed"} & roles.keys()) or "name" not in roles:
        return None
    if "mandatory" in roles or "additional" in roles:
        roles.pop("listed", None)
    return roles


def remap(role_spans: dict, spans) -> dict | None:
    """Roles of a continuation table: each role → the column overlapping its header column most."""
    out = {}
    for role, (a0, a1) in role_spans.items():
        best = max(range(len(spans)), key=lambda i: min(a1, spans[i][1]) - max(a0, spans[i][0]))
        if min(a1, spans[best][1]) - max(a0, spans[best][0]) < (a1 - a0) * 0.5:
            return None
        out[role] = best
    return out if len(set(out.values())) == len(out) else None


def header_rows(rows):
    """Number of header rows: up to the «1 2 3 …» row (dropped too), else the leading rows
    with header words and no МКБ-10 code."""
    for k, row in enumerate(rows[:6]):
        if is_numbers_row(row):
            return k, k + 1
    k = 0
    while k < min(len(rows), 4) and HEADER_WORDS.search(" ".join(norm(c) for c in rows[k])) \
            and not ICD_RE.search(" ".join(norm(c) for c in rows[k])):
        k += 1
    return k, k


# ---------------------------------------------------------------- rows
class Builder:
    def __init__(self):
        self.records, self.cur = [], None

    def add(self, page, name_lines, cells: dict, new: bool, ruled: bool = False):
        if new or self.cur is None:
            self.cur = {"page": page, "page_end": page, "name_lines": [], "mandatory": [], "additional": [], "listed": []}
            self.records.append(self.cur)
        self.cur["page_end"] = page
        self.cur["name_lines"] += name_lines
        for role, lines in cells.items():
            first = lines[0].strip() if lines else ""
            if ruled and self.cur[role] and (first[:1].isupper() or first[:1].isdigit()):
                self.cur[role].append(CELL_BREAK)  # a new cell (not the rest of one cut by the page break)
            self.cur[role] += lines

    def close(self):
        self.cur = None


def starts_row(name: str, cur) -> bool:
    """A name cell opens a new nosology unless it continues the previous name (lower case,
    a bracket or a code cut by the page break)."""
    if not name:
        return False
    if cur is None or not cur["name_lines"]:
        return True
    return bool(name[:1].isupper() or name[:1].isdigit() or ICD_RE.match(name))


def ruled_body(b: Builder, page_no, table, rows, start, roles):
    for row in rows[start:]:
        if is_numbers_row(row):
            continue
        name = (row[roles["name"]] or "").strip() if roles["name"] < len(row) else ""
        cells = {}
        for role in ("mandatory", "additional", "listed"):
            col = roles.get(role)
            if col is not None and col < len(row) and row[col]:
                cells[role] = row[col].split("\n")
        if not name and not cells:
            continue
        b.add(page_no, name.split("\n") if name else [], cells, new=starts_row(name.split("\n")[0], b.cur), ruled=True)


def unruled_body(b: Builder, page, page_no, top, spans, roles):
    """Text under a ruled header (or a ruled numbers row) on a page without ruling lines in
    the body: words → columns by x, lines by `top`, rows by vertical gaps."""
    words = page.crop((0, top, page.width, page.height)).extract_words(keep_blank_chars=False, x_tolerance=1.5)
    if not words:
        return
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1]["top"] - w["top"]) < 3:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    if len(lines) < 2:
        return
    steps = [b2["top"] - a["top"] for a, b2 in zip(lines, lines[1:]) if b2["top"] - a["top"] > 3]
    step = statistics.median(steps) if steps else 12
    x_min, x_max = spans[0][0] - 5, spans[-1][1] + 5
    prev_top, prev_name = None, ""
    for ln in lines:
        ws = [w for w in ln["words"] if x_min <= (w["x0"] + w["x1"]) / 2 <= x_max]
        if not ws:
            continue
        text = " ".join(w["text"] for w in ws)
        if re.fullmatch(r"\d{1,3}", text) and ln["top"] > page.height * 0.85:
            continue  # page number
        cols = [[] for _ in spans]
        for w in ws:
            c = (w["x0"] + w["x1"]) / 2
            i = min(range(len(spans)), key=lambda i: 0 if spans[i][0] <= c <= spans[i][1]
                    else min(abs(c - spans[i][0]), abs(c - spans[i][1])))
            cols[i].append(w["text"])
        if SPANNING_RE.match(text) and sum(1 for c in cols if c) >= 3:
            b.close()  # «Диагностика и лечение при оказании медицинской помощи в …» across the table
            prev_top, prev_name = None, ""
            continue
        name = " ".join(cols[roles["name"]])
        gap = prev_top is None or ln["top"] - prev_top > step * 1.6
        prev_top = ln["top"]
        cells = {role: [" ".join(cols[roles[role]])] for role in ("mandatory", "additional", "listed")
                 if role in roles and cols[roles[role]]}
        # rows are separated by a gap; nosologies listed one under another without a gap
        # («Камни почки (N20.0)», «Камни мочеточника (N20.1)») share one row
        new = bool(name) and name[:1].isupper() and gap and (not prev_name or prev_name.endswith(")") or b.cur is None)
        if not name and not cells:
            continue
        b.add(page_no, [name] if name else [], cells, new=new or b.cur is None)
        if name:
            prev_name = name


def extract_pages(pdf: Path, pages=None):
    """Raw records of every diagnostics table in the PDF (pages: 1-based numbers to read)."""
    b = Builder()
    header = None  # (column spans, roles) of the last diagnostics table, for continuations
    with pdfplumber.open(pdf) as doc:
        for page_no in pages or range(1, len(doc.pages) + 1):
            page = doc.pages[page_no - 1]
            tables = sorted(page.find_tables(), key=lambda t: t.bbox[1])
            if not tables:
                b.close()
                continue
            for t in tables:
                rows = t.extract()
                if not rows:
                    continue
                spans = column_spans(t)
                n_head, body = header_rows(rows)
                roles = roles_of(header_texts(t, rows, n_head)) if n_head else None
                if roles:
                    header = {"bbox": t.bbox, "roles": {r: spans[i] for r, i in roles.items()}}
                    b.close()
                elif header and abs(header["bbox"][0] - t.bbox[0]) < 30 and abs(header["bbox"][2] - t.bbox[2]) < 30 \
                        and (roles := remap(header["roles"], spans)):
                    pass  # the table goes on from the previous page (its grid may differ)
                else:
                    if n_head:  # a table with a header of its own (drugs, criteria…) ends the diagnostics table
                        header = None
                    b.close()
                    continue
                if body >= len(rows) and t is tables[-1] and t.bbox[3] < page.height * 0.8:
                    # only the header is ruled: the body is plain text under it
                    unruled_body(b, page, page_no, t.bbox[3] + 1, spans, roles)
                else:
                    ruled_body(b, page_no, t, rows, body, roles)
    return b.records


# ---------------------------------------------------------------- layout-text fallback
# Version 1 of this module: tables without ruling lines found from `pdftotext -layout`
# column positions. Its rows are kept only for МКБ-10 codes the ruled tables do not give.
NUM_ROW_RE = re.compile(r"^\s*1(?:\s{2,}\d{1,2}){2,}\s*$")
NOISE_RE = re.compile(r"^\s*(\d{1,3}|\(?Национальный правовой Интернет-портал.*)\s*$")


def number_row(lines):
    """(index, [x of "1", x of "2", …]) of the column numbers row, numbers consecutive."""
    for i, l in enumerate(lines):
        if NUM_ROW_RE.match(l):
            nums = [(m.start(), int(m.group())) for m in re.finditer(r"\d+", l)]
            if [n for _, n in nums] == list(range(1, len(nums) + 1)):
                return i, [x for x, _ in nums]
    return None, None


def boundaries(lines, xs):
    """Column cut positions: in each gap between column numbers, the x where most text
    lines have a blank (the channel between two columns)."""
    body = [l for l in lines if l.strip()]
    width = max((len(l) for l in body), default=0)

    def blank(x):
        return sum(1 for l in body if x >= len(l) or l[x] == " ") / len(body) if body else 1

    cuts = []
    for a, b in zip(xs, xs[1:]):
        lo = a + 1
        best = max(range(lo, max(lo + 1, b)), key=lambda x: (round(blank(x), 2), -abs(x - (a + b) / 2)))
        cuts.append(best)
    return [0] + cuts + [max(width, xs[-1] + 1) + 1000]


def text_cells(line, cuts):
    return [line[cuts[i]:cuts[i + 1]].strip() if cuts[i] < len(line) else "" for i in range(len(cuts) - 1)]


def text_header_roles(header_lines, cuts):
    roles = {}
    for l in header_lines:
        for word, role in (("обязательн", "mandatory"), ("дополнительн", "additional"),
                           ("наименовани", "name"), ("нозолог", "name"), ("шифр", "name")):
            for m in re.finditer(word, l, re.I):
                col = max(i for i in range(len(cuts) - 1) if cuts[i] <= m.start())
                roles.setdefault(role, col)
    return roles


def extract_layout_text(pdf: Path) -> list[dict]:
    """Raw records from `pdftotext -layout` text (rows separated by blank lines)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir
    from protocol_chapter_extractor import pdf_to_pages
    rows, roles, cur = [], None, None
    for pno, page in enumerate(pdf_to_pages(pdf), start=1):
        lines = [l.rstrip() for l in page.splitlines() if not NOISE_RE.match(l)]
        idx, xs = number_row(lines)
        if idx is None:
            continue
        body = lines[idx + 1:]
        cuts = boundaries(body, xs)
        r = text_header_roles(lines[max(0, idx - 12):idx], cuts)
        if "mandatory" in r:
            roles = r
        if not roles or "mandatory" not in roles:
            continue
        name_col = roles.get("name", 0)
        prev_name, gap = "", True
        for l in body:
            if not l.strip():
                gap = True
                continue
            c = text_cells(l, cuts)
            name = c[name_col] if name_col < len(c) else ""
            starts = name and name[:1].isupper() and gap and (not prev_name or prev_name.endswith(")"))
            gap = False
            if starts or cur is None:
                cur = {"page": pno, "page_end": pno, "name_lines": [], "mandatory": [], "additional": [], "listed": [],
                       "legacy": True}
                rows.append(cur)
            cur["page_end"] = pno
            if name:
                cur["name_lines"].append(name)
            for role in ("mandatory", "additional"):
                col = roles.get(role)
                if col is not None and col < len(c) and c[col]:
                    cur[role].append(c[col])
            prev_name = name or prev_name
    return rows


# ---------------------------------------------------------------- output
def finish(records, icd_names) -> list[dict]:
    out, legacy = [], []
    for r in records:
        name = join_lines(r["name_lines"])
        codes = icd_codes(name, icd_names)
        lists = {k: split_items(r[k]) for k in ("mandatory", "additional", "listed")}
        if not codes or not any(lists.values()):
            continue
        (legacy if r.get("legacy") else out).append(
            {"page": r["page"], "page_end": r["page_end"], "name": name, "icd10": codes, **lists})
    found = {c for r in out for c in r["icd10"]}
    heads = {c[:3] for c in found}
    # a legacy row counts as found when the ruled tables give its code, its parent (N11 for
    # N11.8) or, for a bare 3-character code, any of its subcodes
    out += [r for r in legacy if not any(c in found or c[:3] in found or len(c) == 3 and c in heads
                                         for c in r["icd10"])]
    return sorted(out, key=lambda r: r["page"])


def pages_to_read(doc_pages) -> list[int]:
    """The parser's table pages (`parsed/docs/*.json` pages[].table) plus the page before each
    run of them: a table often starts under body text, with its header, on a page that is not
    table-like enough to be flagged."""
    tp = {p["page"] for p in doc_pages if p.get("table")}
    return sorted(tp | {n - 1 for n in tp if n > 1 and n - 1 not in tp})


def extract(pdf: Path, icd_names, pages=None, cache: Path | None = CACHE) -> list[dict]:
    """Table rows of one PDF; cached by the file's sha256 (and VERSION) in `cache`."""
    key = None
    if cache is not None:
        key = hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]
        f = cache / f"{key}.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("version") == VERSION and data.get("pages") == (list(pages) if pages else None):
                return finish(data["records"], icd_names)
    records = extract_pages(pdf, pages) + extract_layout_text(pdf)
    if key:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{key}.json").write_text(json.dumps(
            {"version": VERSION, "pages": list(pages) if pages else None, "records": records}, ensure_ascii=False),
            encoding="utf-8")
    return finish(records, icd_names)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    icd = {c["code"] for c in json.loads(Path("reference/icd10.json").read_text(encoding="utf-8"))["codes"]}
    rows = extract(Path(sys.argv[1]), icd, cache=None)
    print(f"{len(rows)} rows")
    for row in rows[: int(sys.argv[2]) if len(sys.argv) > 2 else 8]:
        print(f"p.{row['page']}-{row['page_end']} {row['icd10']} {row['name'][:80]}")
        for k, lbl in (("mandatory", "обяз."), ("additional", "доп."), ("listed", "перечень")):
            if row[k]:
                print(f"   {lbl}: " + " | ".join(row[k]))
