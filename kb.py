"""Expert system CLI: is a diagnostic service provided for by the clinical protocols for
a diagnosis? Answers with the verdict, a verbatim excerpt and a `<url>#page=N` link.

    python -I kb.py ask "Диагноз - аутоиммунный тиреоидит, разрешён ли анализ мочи по Ничипоренко"
    python -I kb.py ask --diagnosis E06.3 --service "ТТГ"
    python -I kb.py ask "…" --json                  # machine-readable answer
    python -I kb.py eval [eval/golden_endocrinology.jsonl]

Reads kb/kb.json (kb_build.py) and reference/ (reference_builder.py). Rule-based: the
question is split into diagnosis and service by phrasing; diagnosis is matched to МКБ-10
(typos tolerated), service to stems that must all occur in a protocol list item.
"""
import argparse
import functools
import json
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir
from kb_text import GENERIC, Normalizer, close, contains, trigram_sim
from kb_build import Catalogue

LEVELS = {  # priority order and wording for the answer
    "mandatory": (1, "обязательная диагностика"),
    "additional": (2, "дополнительная диагностика (по показаниям)"),
    "listed": (3, "перечень диагностики протокола (обязательность не указана)"),
    "differential": (3, "дифференциальная диагностика"),
    "monitoring": (4, "медицинское наблюдение"),
    "text": (5, "упоминается в тексте протокола"),
}
VERDICTS = {
    "mandatory": "ПРЕДУСМОТРЕНО — обязательная диагностика",
    "additional": "ПРЕДУСМОТРЕНО — дополнительная диагностика (по медицинским показаниям)",
    "differential": "ПРЕДУСМОТРЕНО — для дифференциальной диагностики",
    "listed": "ПРЕДУСМОТРЕНО — в перечне диагностики протокола (обязательная или по показаниям — не указано)",
    "monitoring": "ПРЕДУСМОТРЕНО — в рамках медицинского наблюдения",
    "text": "УПОМИНАЕТСЯ в тексте протокола, но не в перечне диагностики — проверьте по выдержке",
    "manual": "ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ — протокол для этого диагноза есть, но не разобран автоматически",
    None: "НЕ ПРЕДУСМОТРЕНО протоколом для этого диагноза",
}
ICD_CODE_RE = re.compile(r"\b([A-ZА-Я])(\d{2})(?:\.(\d{1,2}))?\b")
CYR2LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")
SPLIT_RES = [
    # «Диагноз – X, разрешён ли Y», «X: можно ли Y»
    re.compile(r"^(?:диагноз\w*\s*[:\-–—]?\s*)?(?P<d>.+?)\s*[,.;:]?\s*(?:разреш\w*|предусмотр\w*|можно|допуст\w*|положен\w*|показан\w*|входит)\s+(?:ли\s+)?(?P<s>.+?)\??$", re.I),
    # «разрешён ли Y при X», «Y при X»
    re.compile(r"^(?:(?:разреш\w*|предусмотр\w*|можно|допуст\w*|положен\w*|показан\w*)\s+(?:ли\s+)?)?(?P<s>.+?)\s+(?:при|для)\s+(?:диагнозе?\s*[:\-–—]?\s*)?(?P<d>.+?)\??$", re.I),
]
# Words of МКБ-10 names that don't narrow a diagnosis down: «Гипотиреоз неуточненный» is the
# plain «гипотиреоз», «Другие формы гипотиреоза» is not more specific than it.
DX_NEUTRAL = {"неуточнен", "уточнен", "друг", "форм", "состоян", "болезн", "синдром", "класс", "рубрик"}
MIN_DX_SCORE = 0.6
LLM_DX_SCORE = 0.95  # with --llm: below this the LLM is asked (exact name matches score ≥ 1.0)
CHILD_RE = re.compile(r"\b(?:реб[её]н|дет[еи]й?|детск|новорожд|подрост|младен)\w*", re.I)
ADULT_RE = re.compile(r"\bвзросл\w*", re.I)


POP_ORDER = ["взрослые", "взрослые и дети", "население не указано", "дети"]


def pop_label(pop) -> str:
    pop = set(pop or [])
    if pop == {"adult"}:
        return "взрослые"
    if pop == {"child"}:
        return "дети"
    return "взрослые и дети" if pop else "население не указано"


def split_question(q: str):
    q = q.strip()
    for rx in SPLIT_RES:
        m = rx.match(q)
        if m:
            d = re.sub(r"^(?:у\s+)?(?:пациента?|ребенка|ребёнка|взрослого)\s+(?:с\s+)?", "", m.group("d").strip(" ,.;:–-"), flags=re.I)
            s = re.sub(r"^(?:ли\s+)?(?:проведение|выполнение|назначение|делать|сделать|провести|определить|определение|выполнить|назначить|сдать|исследовать|измерить)\s+", "", m.group("s").strip(" ,.;:?"), flags=re.I)
            return d, s
    return None, None


@functools.lru_cache(maxsize=64)
def _read_items(blob: Path, offset: int, size: int):
    with blob.open("rb") as f:
        f.seek(offset)
        return json.loads(zlib.decompress(f.read(size)))


class LazySection(dict):
    """Section whose `items` are read from the packed blob on first access."""

    def __init__(self, meta: dict, blob: Path):
        super().__init__(meta)
        self._blob = blob

    def with_extra(self, **extra):  # the copy keeps reading items lazily
        out = LazySection(self, self._blob)
        out.update(extra)
        return out

    def __missing__(self, key):
        if key != "items":
            raise KeyError(key)
        return _read_items(self._blob, *self["_blob"])


class Engine:
    def __init__(self, kb_path: Path, ref: Path, llm: bool = False):
        self.kb = json.loads(kb_path.read_text(encoding="utf-8"))
        if self.kb.get("packed"):  # kb.idx.json: section items stay on disk (see kb_pack.py)
            blob = kb_path.parent / self.kb["packed"]
            self.kb["sections"] = [LazySection(s, blob) for s in self.kb["sections"]]
        self.llm = llm
        cc = kb_path.parent / "crosscheck.json"  # kb_llm.py crosscheck
        self.crosscheck = {}
        if cc.exists():
            for sec in json.loads(cc.read_text(encoding="utf-8"))["sections"].values():
                self.crosscheck.update(sec["items"])
        self.norm = Normalizer()
        self.cat = Catalogue(ref / "services.json", self.norm)
        self.icd = {c["code"]: c["name"] for c in json.loads((ref / "icd10.json").read_text(encoding="utf-8"))["codes"]}
        self.covered = {c for s in self.kb["sections"] for c in s["icd10"]}
        # names to match a diagnosis against: МКБ-10 names + chapter titles
        self.names = [(c, n) for c, n in self.icd.items() if re.fullmatch(r"[A-Z]\d{2}(\.\d{1,2})?", c)]
        self.name_stems = {}
        for s in self.kb["sections"]:  # chapter titles and synonyms from definitions
            if s["icd10"] and s["icd10_source"] in ("classification", "title", "table") and len(s["icd10"]) <= 8:
                for n in s.get("names", []):
                    self.names.append((s["icd10"][0], n.lower()))
                    full = self.norm.expand(n)  # «АИО» → «аутоиммунная офтальмопатия»
                    if full != n:
                        self.names.append((s["icd10"][0], full.lower()))
        # protocol titles: «… пациентов с артериальной гипертензией» → the protocol's scope codes;
        # the code whose МКБ-10 name is closest to the phrase, else the first one
        for pn in self.kb.get("protocol_names", []):
            codes = [c for c in pn["codes"] if c in self.icd]
            if not codes:
                continue
            best = max(codes, key=lambda c: trigram_sim(pn["name"], self.icd[c]))
            code = best if trigram_sim(pn["name"], self.icd[best]) - trigram_sim(pn["name"], self.icd[codes[0]]) > 0.1 else codes[0]
            self.names.append((code, pn["name"].lower()))
            full = self.norm.expand(pn["name"])
            if full != pn["name"]:
                self.names.append((code, full.lower()))
        for _, name in self.names:
            if name not in self.name_stems:
                self.name_stems[name] = [x for x in self.norm.stems(name) if x not in GENERIC and x not in DX_NEUTRAL]

    # ---------------------------------------------------------------- diagnosis
    def resolve_diagnosis(self, text: str, top=5):
        m = ICD_CODE_RE.search(text.translate(CYR2LAT) if text[:1] in "АВЕКМНОРСТХ" else text)
        if m:
            code = m.group(1).translate(CYR2LAT) + m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
            if code in self.icd:
                return [{"code": code, "name": self.icd[code], "score": 1.0}]
        q = text.lower()
        qs = [s for s in self.norm.stems(text) if s not in GENERIC and s not in DX_NEUTRAL]
        best = {}
        for code, name in self.names:
            ns = self.name_stems[name]
            if qs and not any(contains(ns, x) for x in qs):
                continue
            sim = trigram_sim(q, name)
            if ns and qs:  # stem overlap tolerates word order and typos
                hit = sum(1 for s in qs if contains(ns, s)) / len(qs)
                cover = sum(1 for s in ns if contains(qs, s)) / len(ns)
                sim = max(sim, 0.6 * hit + 0.4 * cover)
            if code in self.covered:
                sim += 0.05  # prefer diagnoses the protocols actually cover
            if sim > best.get(code, (0,))[0]:
                best[code] = (sim, name)
        ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[:top]
        return [{"code": c, "name": self.icd.get(c, n), "score": round(s, 3)} for c, (s, n) in ranked if s >= 0.35]

    def _weak(self, diagnosis: str) -> bool:
        """Worth asking the LLM: no exact name match (≥ LLM_DX_SCORE) — «болезнь Аддисона»
        lands on «Аддисонов криз» with 0.8 — or two different diagnoses score alike.
        Exact matches (typos included) stay rule-only and free."""
        dx = self.resolve_diagnosis(diagnosis)
        return not dx or dx[0]["score"] < LLM_DX_SCORE or (
            len(dx) > 1 and dx[1]["score"] >= dx[0]["score"] - 0.03 and dx[1]["code"][:3] != dx[0]["code"][:3])

    # ---------------------------------------------------------------- sections
    def primary(self, doc_id: str, dx_stems: list[str]) -> bool:
        """The protocol's title names this disease: «…пациентов с сахарным диабетом» for
        «сахарный диабет 2 типа»."""
        if not dx_stems:
            return True
        for pn in self.kb.get("protocol_names", []):
            if pn["doc_id"] != doc_id:
                continue
            ns = [x for x in self.norm.stems(pn["name"]) if x not in GENERIC and x not in DX_NEUTRAL]
            if ns and (all(contains(dx_stems, x) for x in ns) or all(contains(ns, x) for x in dx_stems)):
                return True
        return False

    def sections_for(self, code: str, population: str | None, dx_text: str = ""):
        dx_stems = [x for x in self.norm.stems(dx_text) if x not in GENERIC and x not in DX_NEUTRAL]             if dx_text and not ICD_CODE_RE.fullmatch(dx_text.strip()) else []
        dx_stems = dx_stems or [x for x in self.norm.stems(self.icd.get(code, "")) if x not in GENERIC and x not in DX_NEUTRAL]
        out = []
        for s in self.kb["sections"]:
            # tier 0: the section names this code or a broader one (E11 for E11.9);
            # tier 1: only narrower codes (E11.2 «диабетическая болезнь почек» for E11) —
            # a complication chapter, cited after the main protocol; tier 2 — see below
            if any(c == code or code.startswith(c + ".") for c in s["icd10"]):
                # a chapter bound only by the scope of a protocol about another disease
                # («…с нефрологическими заболеваниями» for «сахарный диабет») comes second
                tier = 1 if s["icd10_source"] == "protocol" and not self.primary(s["doc_id"], dx_stems) else 0
            elif any(c.startswith(code + ".") for c in s["icd10"]):
                tier = 2
            else:
                continue
            s = s.with_extra(tier=tier) if isinstance(s, LazySection) else dict(s, tier=tier)
            if population and s["population"] and population not in s["population"]:
                continue
            out.append(s)
        return out

    def resolve_service(self, text: str):
        """Service list entries for the user's wording (display only — matching against the
        protocols goes by stems): the clear winner, or the top few if it's close."""
        cands = self.cat.match(self.norm.stems(text), top=3, min_score=0.25, query=True)
        if not cands:
            return []
        if len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= 0.08 or cands[0]["score"] >= 0.7:
            return cands[:1]
        return [c for c in cands if c["score"] >= cands[0]["score"] - 0.08]

    # ---------------------------------------------------------------- matching
    def required_stems(self, service: str):
        st = [s for s in dict.fromkeys(self.norm.stems(service)) if s not in GENERIC]
        return st or list(dict.fromkeys(self.norm.stems(service)))

    @staticmethod
    def covers(stems: list[str], required: list[str]) -> bool:
        return all(contains(stems, r) for r in required)

    def find(self, sections, required):
        hits = []
        for s in sections:
            for it in s["items"]:
                how = None
                for seg in it["segments"]:
                    if any(self.covers(m["stems"], required) for m in seg.get("members", [])):
                        how = "exact"
                        break
                    if self.covers(seg["stems"], required):
                        how = how or ("exact" if it["level"] == "text" else "segment")
                if how:
                    hits.append((s, it, how))
        hits.sort(key=lambda h: (h[0].get("tier", 0), LEVELS[h[1]["level"]][0], h[2] != "exact",
                                 h[0]["doc_id"], h[1]["page"]))
        return hits

    # ---------------------------------------------------------------- answer
    def cite(self, sec, it):
        doc = self.kb["documents"][sec["doc_id"]]
        quote = it["text"]
        if it.get("header") and it["header"] != it["text"]:
            quote = f"{it['header']} … {('[' + it['condition'] + ':] ') if it.get('condition') else ''}{it['text']}"
        return {
            "protocol": sec["protocol"] or doc["title"],
            "decree": doc["decree"],
            "population": sec["population"],
            "chapter": sec["chapter"],
            "chapter_title": sec["chapter_title"],
            "clause": it["clause"] if not it.get("sub") else it["sub"],
            "level": it["level"],
            "condition": it.get("condition"),
            "quote": quote,
            "url": f"{doc['url']}#page={it.get('header_page') or it['page']}",
            "pdf_pages": sorted({it.get("header_page") or it["page"], it["page"], it["page_end"]}),
            "printed_page": it.get("printed"),
            "check": self.check_status(sec, it),
        }

    def check_status(self, sec, it):
        if it["level"] == "text":
            return None
        from kb_llm import item_key
        st = self.crosscheck.get(item_key(sec["id"], it["text"]))
        return st["status"] if st else "rules_only"

    def ask(self, question: str | None = None, diagnosis: str | None = None, service: str | None = None,
            population: str | None = None):
        llm_used = None
        if question and not (diagnosis and service):
            d, s = split_question(question)
            diagnosis, service = diagnosis or d, service or s
        asked_diagnosis = diagnosis  # the user's wording, shown even when the LLM supplies the code
        if self.llm and question and (not diagnosis or not service or self._weak(diagnosis)):
            from kb_llm import parse_question  # needs the anthropic package and credentials
            parsed = parse_question(question)
            codes = [c["code"].translate(CYR2LAT) for c in parsed["icd10"] if c["code"].translate(CYR2LAT) in self.icd]
            # prefer the LLM's code that the protocols cover (E27.1 over E27.2 for Аддисон)
            covered = [c for c in codes if c in self.covered
                       or any(x.startswith(c + ".") or c.startswith(x + ".") for x in self.covered)]
            diagnosis = (covered or codes or [parsed["diagnosis"]])[0]
            service = service or parsed["service"]
            if population is None and parsed["population"] != "unknown":
                population = parsed["population"]
            llm_used = f"диагноз и услуга выделены LLM: «{parsed['diagnosis']}» → {', '.join(codes) or 'код не предложен'}"
        if not diagnosis or not service:
            return {"error": "Не удалось выделить диагноз и услугу. Сформулируйте как "
                             "«Диагноз – …, разрешено ли …» или задайте --diagnosis и --service."}
        if population is None and question:
            population = "child" if CHILD_RE.search(question) else "adult" if ADULT_RE.search(question) else None

        dx = self.resolve_diagnosis(diagnosis)
        if not dx or dx[0]["score"] < MIN_DX_SCORE:
            hint = "; ".join(f"{d['code']} {d['name']}" for d in dx[:4])
            return {"diagnosis_text": diagnosis, "service_text": service, "verdict_level": "unknown_diagnosis",
                    "error": f"Диагноз «{diagnosis}» не распознан уверенно"
                             + (f". Возможно: {hint}. Уточните или укажите код МКБ-10." if hint else ". Укажите код МКБ-10.")}
        chosen = dx[0]
        if chosen["code"] not in self.covered and not any(c.startswith(chosen["code"] + ".") or chosen["code"].startswith(c + ".")
                                                         for c in self.covered):
            # «хламидийная инфекция» → A74.9 (no protocol) vs A56.x (STI protocol) scored alike:
            # take the close candidate the protocols cover
            for d in dx[1:]:
                if d["score"] >= chosen["score"] - 0.1 and d["code"] in self.covered:
                    chosen = d
                    break
        ambiguous = len(dx) > 1 and dx[1]["score"] >= dx[0]["score"] - 0.03 and dx[1]["code"][:3] != dx[0]["code"][:3]
        sections = self.sections_for(chosen["code"], population, diagnosis)
        # Table-layout and scanned documents are not parsed into lists yet (stage 4):
        # their "text" is column soup, so they are only reported, never used for a verdict.
        usable = [s for s in sections if self.kb["documents"][s["doc_id"]]["kind"] not in ("table", "scan")
                  or s["icd10_source"] == "table"]
        unparsed = sorted({s["doc_id"] for s in sections} - {s["doc_id"] for s in usable})
        required = self.required_stems(service)

        groups = {}
        for s in usable:
            groups.setdefault(pop_label(s["population"]), []).append(s)
        results = []
        for label in sorted(groups, key=lambda l: POP_ORDER.index(l)):
            secs = groups[label]
            hits = self.find(secs, required)
            listed = [h for h in hits if h[1]["level"] != "text"]
            texts = [h for h in hits if h[1]["level"] == "text"]
            level = listed[0][1]["level"] if listed else ("text" if texts else None)
            if listed and listed[0][0].get("tier"):  # only complication chapters cite it
                main_tier = [h for h in listed if not h[0].get("tier")]
                level = main_tier[0][1]["level"] if main_tier else level
            evidence = listed[:6] + texts[:2 if listed else 4]
            docs = list(dict.fromkeys(s["doc_id"] for s in secs))
            res = {
                "population": label,
                "verdict_level": level,
                "verdict": VERDICTS[level],
                "evidence": [dict(self.cite(s, it), match=how) for s, it, how in evidence],
                "checked": [{"protocol": self.kb["documents"][d]["title"], "url": self.kb["documents"][d]["url"],
                             "chapters": list(dict.fromkeys(f"{s['chapter']}. {s['chapter_title']}" if s["chapter"] is not None
                                                        else s["chapter_title"] for s in secs if s["doc_id"] == d))}
                            for d in docs],
                "notes": [],
            }
            if level in (None, "text"):
                for d in docs:
                    doc = self.kb["documents"][d]
                    other = doc.get("other_services")
                    if other:
                        res["notes"].append({"quote": other["text"], "clause": other["clause"],
                                             "url": f"{doc['url']}#page={other['page']}"})
            results.append(res)

        main = results[0] if results else None
        if not results and unparsed:
            main = {"verdict_level": "manual", "evidence": [],
                    "verdict": "ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ — протокол для этого диагноза есть, но не разобран автоматически"}
        return {
            "question": question,
            "diagnosis_text": asked_diagnosis or diagnosis,
            "diagnosis": chosen,
            "diagnosis_alternatives": dx[1:4] if ambiguous else [],
            "service_text": service,
            "service_catalogue": self.resolve_service(service),
            "population": population,
            # headline = first population group (adults before children)
            "verdict_level": main["verdict_level"] if main else None,
            "verdict": main["verdict"] if main else "ПРОТОКОЛ НЕ НАЙДЕН для этого диагноза",
            "evidence": main["evidence"] if main else [],
            "results": results,
            "unparsed": [{"protocol": self.kb["documents"][d]["title"], "url": self.kb["documents"][d]["url"],
                          "kind": self.kb["documents"][d]["kind"]} for d in unparsed],
            "method": ("правила + перекрёстная проверка LLM" if self.crosscheck else "правила (без перекрёстной проверки LLM)")
                      + (f"; {llm_used}" if llm_used else ""),
        }


def render(a: dict) -> str:
    if a.get("error"):
        return "Ошибка: " + a["error"]
    out = []
    d = a["diagnosis"]
    out.append(f"Диагноз: {d['code']} {d['name']}   (из «{a['diagnosis_text']}»)")
    for alt in a["diagnosis_alternatives"]:
        out.append(f"   или? {alt['code']} {alt['name']}")
    svc = a["service_catalogue"]
    out.append(f"Услуга:  «{a['service_text']}»" + (f" → пост. №123: {svc[0]['code']} {svc[0]['name']}" if svc else ""))
    for alt in svc[1:]:
        out.append(f"   или? {alt['code']} {alt['name']}")
    if a["population"]:
        out.append(f"Население: {'дети' if a['population'] == 'child' else 'взрослые'}")
    if not a["results"]:
        out.append("")
        out.append(f"ВЕРДИКТ: {a['verdict']}")
        if a.get("diagnosis_alternatives") or not a["unparsed"]:
            out.append("  (проверьте, верно ли распознан диагноз)")
    for r in a["results"]:
        out.append("")
        out.append(f"=== {r['population'].upper()}: {r['verdict']}")
        for i, e in enumerate(r["evidence"], 1):
            out.append("")
            dec = e["decree"]
            out.append(f"[{i}] {e['protocol']}" + (f" (пост. МЗ от {dec['date']} № {dec['number']})" if dec and dec['number'] not in e['protocol'] else ""))
            where = (f"Глава {e['chapter']} «{e['chapter_title']}», п. {e['clause']}" if e["chapter"] is not None
                     else f"{e['chapter_title']}" + (f", п. {e['clause']}" if e["clause"] else ""))
            out.append(f"    {where} — {LEVELS[e['level']][1]}"
                       + (" [совпадение по строке списка, проверьте]" if e["match"] == "segment" else "")
                       + {"verified": " [подтверждено LLM]", "uncertain": " [правила и LLM расходятся — проверьте]"}.get(e.get("check"), ""))
            if e["condition"]:
                out.append(f"    Условие: {e['condition']}")
            out.append(f"    «{e['quote']}»")
            pages = f"{e['pdf_pages'][0]}–{e['pdf_pages'][-1]}" if len(e["pdf_pages"]) > 1 else str(e["pdf_pages"][0])
            out.append(f"    {e['url']}   (стр. PDF {pages}" + (f", печатная {e['printed_page']})" if e["printed_page"] else ")"))
        if r["verdict_level"] in (None, "text"):
            out.append("")
            out.append("Проверены разделы:")
            for c in r["checked"]:
                out.append(f"  {c['protocol']}")
                out.append(f"    {c['url']}")
                out.append(f"    главы: {'; '.join(c['chapters'])}")
        for n in r["notes"]:
            out.append("")
            out.append(f"Оговорка (п. {n['clause']}): «{n['quote']}»")
            out.append(f"    {n['url']}")
    for u in a["unparsed"]:
        out.append("")
        out.append(f"Не разобран автоматически ({'таблицы' if u['kind'] == 'table' else 'скан'}), проверьте вручную: {u['protocol']}")
        out.append(f"    {u['url']}")
    out.append("")
    out.append(f"Метод: {a['method']}.")
    return "\n".join(out)


def run_eval(engine: Engine, path: Path):
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    ok = 0
    for c in cases:
        a = engine.ask(c["question"])
        got_level = a.get("verdict_level")
        got_code = a.get("diagnosis", {}).get("code")
        clause_ok = True
        if c.get("clause") and a.get("evidence"):
            clause_ok = any(str(e["clause"]).split(".")[0] == str(c["clause"]) for e in a["evidence"][:3])
        good = got_level == c["level"] and (not c.get("icd") or got_code == c["icd"]) and (got_level is None or clause_ok)
        ok += good
        print(f"{'OK  ' if good else 'FAIL'} {c['question']}")
        if not good:
            print(f"       expected {c.get('icd')} {c['level']} п.{c.get('clause')}; got {got_code} {got_level} "
                  f"п.{[e['clause'] for e in a.get('evidence', [])[:3]]}  {a.get('error', '')}")
    print(f"\n{ok}/{len(cases)} correct")
    return ok == len(cases)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("question", nargs="?")
    a.add_argument("--diagnosis")
    a.add_argument("--service")
    a.add_argument("--population", choices=["adult", "child"])
    a.add_argument("--json", action="store_true")
    e = sub.add_parser("eval")
    e.add_argument("file", nargs="?", type=Path, default=Path("eval/golden_endocrinology.jsonl"))
    for p in (a, e):
        p.add_argument("--llm", action="store_true", help="use Claude when the diagnosis is not recognized confidently")
        p.add_argument("--kb", type=Path, default=Path("kb/kb.json"))
        p.add_argument("--reference", type=Path, default=Path("reference"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    engine = Engine(args.kb, args.reference, llm=args.llm)
    if args.cmd == "eval":
        sys.exit(0 if run_eval(engine, args.file) else 1)
    ans = engine.ask(args.question, args.diagnosis, args.service, args.population)
    print(json.dumps(ans, ensure_ascii=False, indent=1) if args.json else render(ans))


if __name__ == "__main__":
    main()
