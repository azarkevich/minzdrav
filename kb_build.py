"""Build the knowledge base of the expert system from parsed protocols (stage 2, rules).

    parsed/docs/*.json + reference/{services,icd10}.json  →  kb/kb.json

For every chapter of every protocol:
  * diagnoses it covers — МКБ-10 codes from its classification paragraphs, or the
    protocol's code whose name matches the chapter title;
  * diagnostics lists — «Обязательная диагностика (включает):», «Дополнительная
    диагностика…», «План наблюдения:», «Для дифференциальной диагностики проводятся:»,
    split into items (one per list line) with the condition sub-header they fall under
    («при макроаденоме:»);
  * every other paragraph as level "text" — a service named only in running text
    («ТАБ … рекомендуется в случаях…») is still worth citing;
  * each item split into segments («ОАК; БАК: АЛТ, АСТ» → «ОАК», «БАК: АЛТ, АСТ»),
    segments matched against the service list (пост. МЗ №123), best candidates kept.

Only rule-based for now; an LLM pass for cross-checking (EXPERT_SYSTEM_DESIGN.md §2.2а)
plugs in at the same level (sections / items).

Usage:
    python -I kb_build.py [--category Эндокринология] [--parsed parsed] [--out kb]
"""
import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir
from kb_text import GENERIC, METHOD, Normalizer, contains, trigram_sim
import table_extract

KB_VERSION = 3

_DX = r"(?:диагностик\w*|(?:лабораторн\w+\s+и\s+инструментальн\w+\s+)?(?:диагностическ\w+\s+|клиническ\w+\s+|общеклиническ\w+\s+)?(?:исследовани\w*|обследовани\w*|мероприяти\w*)|инструментальн\w+|лабораторн\w+)"
HEADER_RES = [
    ("mandatory", re.compile(r"^(?:[\d.]+\s*)?(?:к\s+)?обязательн\w*\s+(?:диагностическ\w+\s+)?" + _DX + r"\b[^:]{0,320}:", re.I)),
    ("mandatory", re.compile(r"^(?:[\d.]+\s*)?перечень\s+обязательн\w+\s+[^:]{0,80}обследовани\w*[^:]{0,80}:", re.I)),
    ("mandatory", re.compile(r"^(?:[\d.]+\s*)?первичн\w+\s+диагностическ\w+\s+комплекс[^:]{0,160}:", re.I)),
    ("additional", re.compile(r"^(?:[\d.]+\s*)?дополнительн\w*\s+(?:диагностическ\w+\s+)?(?:\([^)]*\)\s*)?" + _DX + r"\b[^:]{0,320}:", re.I)),
    ("additional", re.compile(r"^(?:[\d.]+\s*)?перечень\s+дополнительн\w+\s+[^:]{0,80}обследовани\w*[^:]{0,80}:", re.I)),
    ("additional", re.compile(r"^(?:[\d.]+\s*)?(?:иные|другие)\s+диагностическ\w+\s+(?:исследовани\w*|мероприяти\w*)[^:]{0,80}:", re.I)),
    ("mandatory", re.compile(r"^(?:[\d.]+\s*)?для\s+(?:постановки|подтверждения|установления|верификации)\s[^:]{0,80}диагноза[^:]{0,60}:", re.I)),
    ("differential", re.compile(r"^(?:[\d.]+\s*)?(?:с\s+целью\s+|для\s+)?дифференциальн\w+\s+диагностик\w*[^:]{0,80}(?:проводят|проводятся|выполняют|оценивают|используют)[^:]{0,40}:", re.I)),
    ("monitoring", re.compile(r"^(?:[\d.]+\s*)?(?:план\s+(?:медицинского\s+|диспансерного\s+)?наблюдения|(?:медицинское\s+|диспансерное\s+|динамическое\s+|активное\s+)?наблюдение\s+[^:]{0,80}(?:включает|осуществляется)|(?:динамическое|активное)\s+наблюдение|(?:режим|объем|сроки)\s+(?:медицинского\s+|диспансерного\s+)?наблюдения|частота\s+контрольного\s+[^:]{0,60}обследования|объем\s+(?:клинического\s+)?обследования|перечень\s+диагностических\s+исследований,?\s+необходимых\s+для\s+контроля|обследовани\w+\s+на\s+этапах\s+терапии)[^:]{0,120}:", re.I)),
    # a list without a stated status: «Диагностические мероприятия:», «Диагностика:»
    ("listed", re.compile(r"^(?:[\d.]+\s*)?(?:диагностическ\w+\s+(?:мероприяти\w*|исследовани\w*)|диагностика|обследование)(?:\s+[^:]{0,100})?:", re.I)),
]
# sub-headers inside a list: «лабораторные исследования:», «инструментальные методы обследования:»
SUBGROUP_RE = re.compile(r"^(?:[\d.]+\s*)?(?:клинико-)?(?:лабораторн\w+|инструментальн\w+|клиническ\w+|лучев\w+|функциональн\w+|эндоскопическ\w+|цитологическ\w+|гистологическ\w+|морфологическ\w+|генетическ\w+|иммунологическ\w+|микробиологическ\w+|бактериологическ\w+)(?:\s+(?:диагностическ\w+|методы|метод))?(?:\s+(?:исследовани\w*|обследовани\w*|методы\s+исследования|методы\s+обследования))?(?:\s*\([^)]*\))?:$", re.I)
# «при микроаденоме:», «у женщин:», «в стационарных условиях:» — condition sub-headers in a list
CONDITION_RE = re.compile(r"^(?:при|у|в|во|для|после|перед|на|с|если|по)\s[^;]{0,150}:$", re.I)
OTHER_SERVICES_RE = re.compile(r"иные,?\s+не\s+указанные\s+в\s+(?:данном|настоящем)\s+клиническом\s+протоколе", re.I)
ICD_LINE_RE = re.compile(r"^([A-Z])[\s.]?(\d{2}(?:\.\d{1,2})?)\s*[*+]?\s+(.+)")  # «E06.3», «Е 21.0», «M.81.0»
CYR2LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")
DEFINITION_RE = re.compile(r"^\d+\.\s+([А-ЯЁ][^()–—:]{3,80}?)\s*(?:\(([^)]{3,200})\))?\s+[–—-]\s")


def title_diseases(title: str) -> list[str]:
    """«Диагностика и лечение пациентов (взрослое население) с ишемическим инсультом и
    транзиторной ишемической атакой» → ["ишемическим инсультом", "транзиторной ишемической атакой"]."""
    m = re.search(r"«([^»]+)»", title or "")
    t = m.group(1) if m else (title or "")
    m = re.search(r"\s(?:с|со)\s+(?!применением|использованием|помощью)(.+)$", t) or         re.search(r"(?:профилактика|диагностика|лечение|реабилитация)\s+(?!и\s|пациентов|детей|взрослых|населения)([а-яё][^,]+)$", t)
    if not m:
        return []
    t = re.sub(r"\s+(?:в|во)\s+(?:стационарных|амбулаторных)\s+условиях.*$", "", m.group(1))
    t = re.sub(r"\([^)]*\)", " ", t)
    parts = re.split(r",\s*|\s+и\s+(?=[а-яё]+(?:ым|ой|ом|ей|ами|ями|ими|ыми)\s)", t)
    return [re.sub(r"\s+", " ", x).strip() for x in parts if len(x.strip()) > 3][:6]


def has_lists(ch) -> bool:
    return any(header_level(p["text"])[0] or SUBGROUP_RE.match(p["text"])
               for cl in ch["clauses"] for p in cl["paragraphs"])


def header_level(text: str):
    """(level, text after the colon) if the paragraph opens a diagnostics list. On flat pages a
    chapter title can be glued in front: «Диагностика и лечение … экземой 62. Обязательные
    диагностические мероприятия:» — a precise header after a clause number wins over «listed»."""
    starts = [0] + [m.start() for m in re.finditer(r"(?<=\s)\d+(?:\.\d+)*\.\s+(?=[А-ЯЁа-яё])", text)]
    fallback = None
    for st in starts:
        for level, rx in HEADER_RES:
            m = rx.match(text[st:])
            if m:
                if level != "listed":
                    return level, text[st + m.end():].strip()
                fallback = fallback or (level, text[st + m.end():].strip())
    return fallback or (None, None)


def split_segments(text: str) -> list[dict]:
    """«ОАК; БАК: АЛТ, АСТ, С-реактивный белок;» → [{text: ОАК}, {group: БАК, text: АЛТ, АСТ, …}]."""
    segs = []
    for part in re.split(r";\s*", text.strip().rstrip(".;")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([^:()]{2,90}(?:\([^)]*\))?[^:()]{0,60}):\s*(.+)$", part)
        if m and not CONDITION_RE.match(part):
            segs.append({"group": m.group(1).strip(), "text": m.group(2).strip()})
        else:
            segs.append({"text": part})
    return segs


def split_members(text: str) -> list[str]:
    """Comma-separated list outside parentheses: «ЭКГ, ХМ-ЭКГ, ЭхоКГ» → 3 members."""
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [m for m in out if m]


def members(seg: dict, norm: Normalizer, glossary: dict) -> list[str]:
    """Single services named by a segment, as text to match against the service list."""
    parts = split_members(seg["text"])
    if seg.get("group"):
        return [f"{seg['group']}: {p}" for p in parts]
    head = parts[0].split()[0] if parts and parts[0].split() else ""
    out = []
    for i, p in enumerate(parts):
        has_method = any(any(s.startswith(m) for m in METHOD) for s in norm.stems(p, glossary))
        out.append(p if i == 0 or has_method or not head else f"{head} {p}")
    return out


class Catalogue:
    """Diagnostic services of the list (пост. МЗ №123) with an inverted stem index."""

    def __init__(self, path: Path, norm: Normalizer):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.services = [s for s in data["services"] if s["diagnostic"] and not s["excluded"]]
        self.norm = norm
        self.stems = []
        df = Counter()
        for s in self.services:
            # «(ТТГ)», «(T4)», «(СМАД)» repeat the name; expanding them only adds noise
            name = re.sub(r"\((?:[A-ZА-ЯЁ][A-Za-zА-ЯЁа-яё0-9-]{0,7}(?:,\s*)?)+\)", " ", s["name"])
            st = sorted({x for x in norm.stems(name) if x not in GENERIC})
            self.stems.append(st)
            df.update(st)
        n = len(self.services)
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self._similar = {}
        self.index = defaultdict(set)  # stem → services
        for i, st in enumerate(self.stems):
            for t in st:
                self.index[t].add(i)

    def similar(self, t):
        """Index stems equal or close to t (memoized: the same stems recur all the time)."""
        hit = self._similar.get(t)
        if hit is None:
            hit = self._similar[t] = [k for k in self.index if contains([k], t)]
        return hit

    def weight(self, t):
        return self.idf.get(t, math.log(1 + len(self.services)))

    def match(self, stems: list[str], top=3, min_score=0.45, query=False):
        """Best services for a text: Dice over IDF-weighted stems (fuzzy stem equality).
        query=True (a user's short wording): mostly how much of the query the name covers."""
        q = [t for t in dict.fromkeys(stems) if t not in GENERIC]
        if not q:
            return []
        cand = set()
        for t in q:  # fuzzy lookup: typos and suffix variants («ничипоренк» ~ «нечипоренк»)
            for k in self.similar(t):
                cand |= self.index[k]
        out = []
        wq = sum(self.weight(t) for t in q)
        for i in cand:
            st = self.stems[i]
            if not st:
                continue
            hit_s = sum(self.weight(t) for t in st if contains(q, t))
            hit_q = sum(self.weight(t) for t in q if contains(st, t))
            ws = sum(self.weight(t) for t in st)
            score = (hit_q / wq) * (hit_s / ws) ** 0.5 if query else (hit_s + hit_q) / (ws + wq)
            if score >= min_score:
                out.append((score, i))
        out.sort(reverse=True)
        return [{"code": self.services[i]["code"], "num": self.services[i]["num"],
                 "name": self.services[i]["name"], "score": round(sc, 3)} for sc, i in out[:top]]


ICD_MENTION_RE = re.compile(r"МКБ|классификаци\w+\s+болезней|шифр", re.I)
EXCEPT_RE = re.compile(r"кроме\s+([A-ZАВЕКМНОРСТХ]\d{2}(?:\.\d{1,2})?)")


def chapter_codes(ch, icd_names):
    """МКБ-10 codes a chapter is about: lines of its classification («E06.3 Аутоиммунный
    тиреоидит;») and paragraphs that mention МКБ (also spelled out: «Международной
    статистической классификации болезней…», «шифр»). «(кроме M05.0 Синдром Фелти)» is
    excluded. Differential lists mostly name diseases without codes, so this is a fair
    approximation."""
    codes, names, excluded = [], {}, set()
    for cl in ch["clauses"]:
        clause_has_icd = any(ICD_MENTION_RE.search(p["text"]) for p in cl["paragraphs"])
        for p in cl["paragraphs"]:
            m = ICD_LINE_RE.match(p["text"].translate(CYR2LAT) if p["text"][:1] in "АВЕКМНОРСТХ" else p["text"])
            if m and m.group(1) + m.group(2) in icd_names:
                code = m.group(1) + m.group(2)
                codes.append(code)
                names[code] = m.group(3).strip(" ;.")
            elif clause_has_icd:
                codes += p.get("icd10", [])
                excluded |= {x.translate(CYR2LAT) for x in EXCEPT_RE.findall(p["text"])}
    return [c for c in dict.fromkeys(codes) if c not in excluded], names


def build(parsed: Path, ref: Path, category: str | None, protocols_dir: Path | None = Path("protocols")):
    norm = Normalizer()
    cat = Catalogue(ref / "services.json", norm)
    icd = {c["code"]: c["name"] for c in json.loads((ref / "icd10.json").read_text(encoding="utf-8"))["codes"]}
    if protocols_dir and not protocols_dir.exists():
        protocols_dir = None
    index = json.loads((parsed / "index.json").read_text(encoding="utf-8"))

    documents, sections, report, protocol_names = {}, [], [], []
    for doc_id, meta in sorted(index.items(), key=lambda kv: kv[1]["path"]):
        if category and not any(category.lower() in c.lower() for c in meta["categories"]):
            continue
        doc = json.loads((parsed / "docs" / f"{doc_id}.json").read_text(encoding="utf-8"))
        glossary = doc["glossary"]
        printed = {p["page"]: p.get("printed") for p in doc["pages"]}
        dinfo = {k: doc[k] for k in ("title", "url", "path", "decree", "population", "categories")}
        dinfo["kind"] = doc["stats"]["kind"]
        documents[doc_id] = dinfo
        n_blocks = n_items = 0

        for pr in doc["protocols"]:
            pop = pr["population"] or doc["population"]
            # codes and names declared anywhere in the protocol, for title matching
            proto_names = {}
            per_ch = []
            for ch in pr["chapters"]:
                codes, names = chapter_codes(ch, icd)
                proto_names.update(names)
                per_ch.append(codes)
            for code in pr["icd10"]:
                proto_names.setdefault(code, icd.get(code, ""))

            # Codes of the classification chapters (codes but no diagnostics lists) — the
            # protocol's scope, for chapters that have lists but name no codes
            # («ДИАГНОСТИКА СД» after «КЛАССИФИКАЦИЯ СД»: E10–E14).
            # Only chapters before the first one with lists count: in a multi-disease protocol
            # a later list-less chapter is just another disease.
            # Codes of the preamble and chapter 1 («Настоящий клинический протокол устанавливает …
            # пациентам с ревматоидным артритом (шифры по МКБ-10 – M05…)») always count.
            scope = []
            for ch, codes in zip(pr["chapters"], per_ch):
                if (ch["no"] or 0) <= 1:
                    scope += codes
                    continue
                if has_lists(ch):
                    break
                scope += codes
            scope = list(dict.fromkeys(scope))

            if scope:
                for nm in title_diseases(pr["title"] or meta["title"]):
                    protocol_names.append({"name": nm, "codes": scope, "doc_id": doc_id})

            for ch, codes in zip(pr["chapters"], per_ch):
                source = "classification" if codes else None
                if not codes and ch["title"]:
                    title = norm.expand(ch["title"]).lower()
                    scored = sorted(((trigram_sim(title, norm.expand(nm).lower()), c) for c, nm in proto_names.items() if nm),
                                    reverse=True)
                    if scored and scored[0][0] >= 0.45:
                        codes = [c for s, c in scored if s >= scored[0][0] - 0.05][:3]
                        source = "title"
                        if scored[0][0] < 0.6 and scope:  # weak title match: keep the protocol scope too
                            codes = list(dict.fromkeys(codes + scope))
                if not codes and scope and has_lists(ch):
                    codes, source = scope, "protocol"
                # «105. Хронический аутоиммунный тиреоидит (тиреоидит Хашимото, хронический
                # лимфоцитарный тиреоидит) – воспалительное заболевание…» → names of the diagnosis
                names = [ch["title"]] if ch["title"] else []
                for cl in ch["clauses"][:3]:
                    if not cl["paragraphs"]:
                        continue
                    first = cl["paragraphs"][0]["text"]
                    syn = re.search(r"Синоним\w*(?:\s+термина)?\s*[–—-]\s*([^.]+)", first)
                    if syn:  # «Синонимы термина – эндокринная офтальмопатия, офтальмопатия Грейвса, …»
                        names += [n.strip() for n in split_members(syn.group(1)) if len(n.strip()) > 3]
                    m = DEFINITION_RE.match(first)
                    if m:
                        names.append(m.group(1).strip())
                        if m.group(2):
                            names += [n.strip() for n in split_members(m.group(2))
                                      if len(n.strip()) > 3 and not n.strip().startswith("далее")]
                        break
                sec = {
                    "id": f"{doc_id}:{pr['index']}:{ch['no'] if ch['no'] is not None else 0}",
                    "doc_id": doc_id,
                    "protocol": pr["title"],
                    "population": pop,
                    "chapter": ch["no"],
                    "chapter_title": ch["title"],
                    "page": ch["page"],
                    "icd10": codes,
                    "icd10_source": source,
                    "names": names,
                    "items": [],
                }
                block = None  # current list: {"level", "header", "clause", "page"}
                condition = None
                for cl in ch["clauses"]:
                    block, condition, subgroup = None, None, None  # lists never span clauses
                    clause_level = clause_header = suspended = None
                    for p in cl["paragraphs"]:
                        text = p["text"]
                        level, tail = header_level(text)
                        if not level and block and block.get("closing"):
                            first = text.split()[0] if text.split() else ""
                            if p.get("sub"):
                                block = {k: v for k, v in block.items() if k != "closing"}  # «12.2. …» goes on
                            elif not (text[:1].islower() or len(first) > 1 and first.rstrip(",;:").isupper()):
                                # the list ended with "." — an explanatory paragraph follows; keep the list
                                # suspended: the next numbered sub-clause of this clause resumes it
                                suspended = {k: v for k, v in block.items() if k != "closing"}
                                block, condition = None, None
                        elif not level and not block and suspended and p.get("sub"):
                            block = suspended
                        if level:
                            block = {"level": level, "header": text, "clause": cl["id"], "page": p["page"]}
                            condition, subgroup = None, None
                            clause_level, clause_header, suspended = level, text, None
                            n_blocks += 1
                            if OTHER_SERVICES_RE.search(text):
                                dinfo.setdefault("other_services", {"text": text, "clause": cl["id"], "page": p["page"]})
                            if not tail:
                                continue
                            text = tail  # «Обязательная диагностика: УЗИ …» on one line
                        elif block and SUBGROUP_RE.match(text):
                            subgroup = re.sub(r"^[\d.]+\s*", "", text).rstrip(":")
                            block = {k: v for k, v in block.items() if k != "closing"}
                            continue
                        elif not block and SUBGROUP_RE.match(text) and re.search(r"исследовани|обследовани|метод", text, re.I):
                            # «Лабораторные исследования:» opening a list on its own; within a
                            # clause that started with «Обязательные …:» it keeps that status
                            block = {"level": clause_level or "listed", "header": clause_header or text,
                                     "clause": cl["id"], "page": p["page"]}
                            subgroup = re.sub(r"^[\d.]+\s*", "", text).rstrip(":") if clause_level else None
                            condition = None
                            n_blocks += 1
                            continue
                        elif block and CONDITION_RE.match(re.sub(r"^[\d.]+\s*", "", text)):
                            condition = re.sub(r"^[\d.]+\s*", "", text).rstrip(":")
                            continue
                        if OTHER_SERVICES_RE.search(text):
                            dinfo.setdefault("other_services", {"text": text, "clause": cl["id"], "page": p["page"]})
                        item = {
                            "level": block["level"] if block else "text",
                            "text": text,
                            "clause": cl["id"],
                            "sub": p.get("sub"),
                            "page": p["page"],
                            "page_end": p["page_end"],
                            "printed": printed.get(p["page"]),
                        }
                        if block:
                            item["header"] = block["header"]
                            item["header_page"] = block["page"]
                            n_items += 1
                        if condition:
                            item["condition"] = condition
                        if block and subgroup:
                            item["subgroup"] = subgroup
                        segs = []
                        for seg in split_segments(text):
                            full = (seg.get("group", "") + " " + seg["text"]).strip()
                            st = norm.stems(full, glossary)
                            seg["stems"] = st
                            if block:  # catalogue mapping only for list items (cost)
                                seg["members"] = []
                                for m in members(seg, norm, glossary):
                                    ms = norm.stems(m, glossary)
                                    seg["members"].append({"text": m, "stems": ms, "services": cat.match(ms)})
                            segs.append(seg)
                        item["segments"] = segs
                        sec["items"].append(item)
                        # a list ends with an item ending in "." unless the next one
                        # continues it (lowercase start or an abbreviation)
                        if block and text.rstrip().endswith(".") and not text.rstrip().endswith(" г."):
                            block = {"level": block["level"], "header": block["header"], "clause": block["clause"],
                                     "page": block["page"], "closing": True}
                    # end of clause
                sections.append(sec)
        # table-layout pages: one section per table row (nosology) — stage 4
        if protocols_dir and doc["stats"]["table_pages"] >= 2:
            try:
                rows = table_extract.extract(protocols_dir / meta["path"], icd)
            except Exception as e:  # a broken table must not stop the build
                print(f"  table extraction failed: {meta['path']}: {e!r}")
                rows = []
            pop = doc["population"]
            title = doc["protocols"][0]["title"] if doc["protocols"] else doc["title"]
            for k, row in enumerate(rows):
                sec = {"id": f"{doc_id}:t:{k}", "doc_id": doc_id, "protocol": title, "population": pop,
                       "chapter": None, "chapter_title": f"Таблица: {row['name']}", "page": row["page"],
                       "icd10": row["icd10"], "icd10_source": "table",
                       "names": [re.sub(r"\s*\([^)]*\)", "", row["name"]).strip()], "items": []}
                for level in ("mandatory", "additional"):
                    for text in row[level]:
                        segs = []
                        for seg in split_segments(text):
                            st = norm.stems((seg.get("group", "") + " " + seg["text"]).strip(), glossary)
                            seg["stems"] = st
                            seg["members"] = [{"text": m, "stems": norm.stems(m, glossary),
                                               "services": cat.match(norm.stems(m, glossary))}
                                              for m in members(seg, norm, glossary)]
                            segs.append(seg)
                        sec["items"].append({
                            "level": level, "text": text, "clause": None, "sub": None,
                            "page": row["page"], "page_end": row["page_end"], "printed": printed.get(row["page"]),
                            "header": f"Таблица, «{row['name']}», диагностика "
                                      + ("обязательная" if level == "mandatory" else "дополнительная (по показаниям)") + ":",
                            "header_page": row["page"], "segments": segs})
                        n_items += 1
                sections.append(sec)
            n_blocks += len(rows)
        report.append({"doc_id": doc_id, "path": dinfo["path"], "kind": dinfo["kind"],
                       "blocks": n_blocks, "items": n_items})
    return {"version": KB_VERSION, "built": time.strftime("%Y-%m-%dT%H:%M:%S"), "category": category,
            "documents": documents, "sections": sections, "protocol_names": protocol_names}, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parsed", type=Path, default=Path("parsed"))
    ap.add_argument("--reference", type=Path, default=Path("reference"))
    ap.add_argument("--out", type=Path, default=Path("kb"))
    ap.add_argument("--category", help="only documents of this category (substring), e.g. Эндокринология")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    kb, report = build(args.parsed, args.reference, args.category)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "kb.json").write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from kb_pack import pack
    pack(kb, args.out)  # kb.idx.json + kb.items.bin for low-memory serving

    secs = kb["sections"]
    items = [i for s in secs for i in s["items"]]
    listed = [i for i in items if i["level"] != "text"]
    mems = [m for i in listed for g in i["segments"] for m in g.get("members", [])]
    print(f"{len(kb['documents'])} documents, {len(secs)} chapters "
          f"({sum(1 for s in secs if s['icd10'])} with МКБ-10 codes), "
          f"{len(listed)} list items ({', '.join(f'{k} {v}' for k, v in Counter(i['level'] for i in listed).items())}), "
          f"{len(items) - len(listed)} text paragraphs")
    print(f"  list members mapped to the service list: {sum(1 for m in mems if m['services'])}/{len(mems)}")
    for r in report:
        if not r["blocks"]:
            print(f"  no diagnostics lists found: [{r['kind']}] {r['path']}")
    print(f"→ {args.out / 'kb.json'}")


if __name__ == "__main__":
    main()
