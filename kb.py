"""Expert system CLI: is a diagnostic service provided for by the clinical protocols for
a diagnosis? Answers with the verdict, a verbatim excerpt and a `<url>#page=N` link.

    python -I kb.py ask "Диагноз - аутоиммунный тиреоидит, разрешён ли анализ мочи по Ничипоренко"
    python -I kb.py ask --diagnosis E06.3 --service "ТТГ"
    python -I kb.py ask "Сахарный диабет, перечень лабораторных анализов"   # list mode
    python -I kb.py ask "Ангиомиолипома, какой вид КТ используется"         # list filtered by a service
    python -I kb.py ask "…" --json                  # machine-readable answer
    python -I kb.py ask "При каких диагнозах назначают метанефрины"           # reverse search
    python -I kb.py ask "ТТГ, свободный Т4, антитела к ТПО"                     # reverse: a list of services
    python -I kb.py eval [eval/golden_endocrinology.jsonl]
    python -I kb.py index --kb kb_all/kb.idx.json     # kb.stems.json for the reverse search

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
from kb_text import GENERIC, METHOD, Normalizer, close, contains, trigram_sim
from kb_build import Catalogue
import insurance

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
# stems of procedure names: «трахеоскопия», «флебография», «капилляроскопия», «инъекция», «катетеризация»
SERVICE_STEM_RE = re.compile(r"(?:скоп|графи?|метри?|терапи?|грамм?)$|центез$|^(?:кариотип|инъекц|катетериз|введен|получен|применен|сканирован|стимуляц|снимк|сбор$|пункц|биопс|рентген|гамма|ультразвук|узи$|кт$|мрт$|экг$)")
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


def _clean_dx(d: str) -> str:
    # «Пациент, диагноз – …», «у ребенка с …», «пациентка: …»
    d = re.sub(r"^(?:у\s+)?(?:пациент(?:а|ка|ки|у)?|ребенка?|ребёнка?|взрослого)(?:[\s,:–—-]+(?:с\s+)?|$)", "",
               d.strip(" ,.;:–-"), flags=re.I)
    return re.sub(r"^диагноз\w*\s*[:\-–—]?\s*", "", d.strip(" ,.;:–-"), flags=re.I).strip(" ,.;:–-")


WHO_RE = re.compile(r"^(?:у\s+)?(?:пациент(?:а|ка|ки|у)?|ребенка?|ребёнка?|застрахованн\w*)\s*[,:–—-]\s*", re.I)


def split_question(q: str):
    q = WHO_RE.sub("", q.strip())  # «Пациент, …»
    for rx in SPLIT_RES:
        m = rx.match(q)
        if m:
            d, s = m.group("d"), m.group("s")
            break
    else:
        # «Сахарный диабет 2 типа, определение гликированного гемоглобина»
        d, _, s = q.partition(",")
        if not s.strip():
            return None, None
    s = re.sub(r"^[а-яё]+\s+ли\s+", "", s.strip(" ,.;:?"), flags=re.I)  # a misspelt verb: «разреешна ли УЗИ»
    s = re.sub(r"^(?:ли\s+)?(?:проведение|выполнение|назначение|делать|сделать|провести|определить|определение|выполнить|назначить|сдать|исследовать|измерить)\s+", "", s, flags=re.I)
    return _clean_dx(d), s


# «перечень лабораторных анализов», «какой вид КТ используется», «какие исследования при …»
LIST_RE = re.compile(r"(?:^|[\s,.;:–—-])(?:(?:полный\s+)?(?:перечень|перечня|список|списка|объ[её]м)|как(?:ой|ая|ое|ие|их)|что\s+входит)(?=\s|$|[,.?])", re.I)
CATEGORIES = {  # what a list question asks for → its label and the words that name it
    "lab": ("лабораторные исследования", re.compile(r"лаборатор|анализ", re.I)),
    "instrumental": ("инструментальные исследования", re.compile(r"инструмент|лучев|визуализ|аппаратн", re.I)),
    "consult": ("консультации врачей-специалистов", re.compile(r"консульт|специалист", re.I)),
}
# words of a list question that are neither the category nor a service filter
LIST_FILLER_RE = re.compile(
    r"(?<![\w-])(?:полн\w*|перечень|перечня|список|списка|объ[её]м\w*|как\w*|что|входит|вид\w*|тип\w*|метод\w*|"
    r"использ\w*|применя\w*|назнача\w*|проводит\w*|провод\w*|выполня\w*|нужн\w*|необходим\w*|требу\w*|"
    r"предусмотр\w*|разреш\w*|положен\w*|показан\w*|делают|сда[её]т\w*|сдают|есть|ли|для|при|в|во|по|с|и|"
    r"диагностик\w*|диагноз\w*|обследовани\w*|исследовани\w*|анализ\w*|лаборатор\w*|инструмент\w*|лучев\w*|"
    r"визуализ\w*|аппаратн\w*|консультац\w*|специалист\w*|врач\w*|услуг\w*|протокол\w*|мероприят\w*|"
    r"диагностическ\w*|медицинск\w*|клиническ\w*|нужно|надо|можно|сделать|пройти)(?![\w-])", re.I)


def parse_list_question(q: str):
    """«андрогения перечень лабораторных анализов» → ("андрогения", "lab", None);
    «ангиомиолипома, какой вид кт используется для диагностики» → ("ангиомиолипома", None, "кт");
    None when the question is not a request for a list."""
    q = WHO_RE.sub("", q.strip().rstrip("?"))
    m = LIST_RE.search(q)
    if not m:
        return None
    head, tail = q[:m.start()], q[m.start():]
    d = _clean_dx(head)
    if not d:  # «какие анализы сдают при гипотиреозе»
        for dm in re.finditer(r"\s(?:при|у\s+(?:пациент\w*|дет\w*|взросл\w*)\s+с|для)\s+(?:диагноз\w*\s*[:\-–—]?\s*)?(.+)$", tail, re.I):
            if LIST_FILLER_RE.sub("", dm.group(1)).strip(" ,.;:–-"):
                d, tail = _clean_dx(dm.group(1)), tail[:dm.start()]
                break
    if not d:
        return None
    category = next((k for k, (_, rx) in CATEGORIES.items() if rx.search(tail)), None)
    rest = re.sub(r"\s+", " ", LIST_FILLER_RE.sub(" ", tail)).strip(" ,.;:–-")
    rest = re.sub(r"^(?:крови|мочи)$", "", rest)  # «какие анализы крови» is the lab category itself
    return d, category, rest or None


# reverse questions: «при каких диагнозах назначают X», «X — при каком диагнозе», «для чего назначают X»
REVERSE_RE = re.compile(
    r"при\s+как(?:их|ом|ой)\s+(?:диаг|заболев|болезн|состоян|патолог)\w*|"
    r"(?:как(?:ие|ой|их)|что\s+за)\s+(?:диагноз|заболевани|болезн)\w*|"
    r"для\s+(?:чего|каких\s+(?:диагноз|заболевани)\w*)|"
    r"в\s+каких\s+(?:протокол|случа)\w*|"
    r"показани[яей]\w*\s+(?:к|для)(?=\s)", re.I)
REVERSE_FILLER_RE = re.compile(
    r"(?<![\w-])(?:назнача\w*|проводит\w*|провод\w*|дела\w*|выполня\w*|сда[её]т\w*|сдают|нужн\w*|необходим\w*|"
    r"предусмотр\w*|показан\w*|применя\w*|использу\w*|требу\w*|ли|по\s+протокол\w*|протокол\w*|согласно|"
    r"есть|встреча\w*|упомина\w*|содерж\w*|входит|включ\w*)(?![\w-])", re.I)
# substances measured by lab tests: «кальций, фосфор, паратгормон» is a list of tests, not diagnoses
ANALYTES = ("гормон кальций фосфор калий натрий магний хлор железо ферритин трансферрин глюкоза холестерин триглицериды "
            "креатинин мочевина билирубин альбумин кортизол альдостерон ренин инсулин тестостерон эстрадиол прогестерон "
            "пролактин антитела антиген онкомаркер фермент амилаза липаза гемоглобин тропонин пептид витамин "
            "иммуноглобулин кальцитонин тиреоглобулин метанефрины катехоламины электролиты липидограмма коагулограмма "
            "фибриноген d-димер лактат прокальцитонин гликемия гормоны микроальбумин протеинурия тироксин трийодтиронин "
            "тиреотропин ттг т4 т3 hba1c сывороточный свободный общий").split()
SERVICE_SPLIT_RE = re.compile(r"\s*(?:[,;+\n]|\s[–—-]\s)\s*")


def parse_reverse_question(q: str):
    """«При каких диагнозах назначают сцинтиграфию щитовидной железы» → ["сцинтиграфию щитовидной железы"];
    None without a reverse wording (a bare list of services is recognised in Engine.ask)."""
    m = REVERSE_RE.search(q)
    if not m:
        return None
    rest = REVERSE_FILLER_RE.sub(" ", q[:m.start()] + " " + q[m.end():])
    # a misspelt question verb: «назанчают», «проовдят»
    rest = re.sub(r"[А-Яа-яЁё]{6,}", lambda w: " " if any(close(w.group(0).lower(), v) for v in REVERSE_VERBS) else w.group(0), rest)
    return split_services(rest) or None


REVERSE_VERBS = ("назначают", "назначается", "назначаются", "проводят", "проводится", "выполняют", "выполняется",
                 "используют", "используется", "применяют", "применяется", "показано", "показаны")


def split_services(text: str) -> list:
    parts = [re.sub(r"\s+", " ", x).strip(" ,.;:?!–—-") for x in SERVICE_SPLIT_RE.split(text)]
    return list(dict.fromkeys(x for x in parts if len(x) >= 2))


def lookalike(text: str) -> str:
    """Lower case, Cyrillic look-alike letters → Latin, so «С-пептид» (Cyrillic С) = «C-пептид»."""
    return text.lower().replace("ё", "е").translate(str.maketrans("асеорхук", "aceopxyk"))


GENERIC_CAP = 150  # a service in more sections than this is «common» (see reverse_search)
REVERSE_WEIGHT = {"mandatory": 1.0, "additional": 0.8, "listed": 0.7, "differential": 0.5, "monitoring": 0.5}


# Kind of a list member (lab / instrumental / consult / exam): words of the text first,
# the code of the matched catalogue service (пост. №123) when the words say nothing.
KIND_WORDS = [
    ("consult", re.compile(r"консультац|(?:при[её]м|осмотр)\w*\s*(?:\([^)]*\)\s*)?(?:\w+\s+)?врач|осмотр\w*\s+(?:лечащ|специал)", re.I)),
    ("exam", re.compile(r"физикальн|осмотр|пальпац|аускультац|анамнез|жалоб|тонометр|антропометр|дневник", re.I)),
    ("instrumental", re.compile(r"(?<![А-ЯЁA-Z])(?:УЗИ|КТ|МРТ|МСКТ|ПЭТ|ЭКГ|ХМ|ЭхоКГ|ЭЭГ|ЭНМГ|ФГДС|ЭГДС|СМАД|ЭФГДС)(?![А-ЯЁA-Z])|"
                                r"ультразвук|томограф|рентген|скопи|графи[яюи]|графию|метри[яюи]|сцинти|"
                                r"эхо|доплер|допплер|дуплекс|мониторир|биопси|пункци|ангиограф|денситометр", re.I)),
    ("lab", re.compile(r"(?<![А-ЯЁA-Z])(?:ОАК|ОАМ|БАК|ПЦР|ИФА|МАНК|РИФ|РПГА|HbA1c|АТ|АЛТ|АСТ|ТТГ|СРБ|СОЭ|ПТИ|МНО|АЧТВ|КЩС|КОС)(?![А-ЯЁA-Z])|"
                       r"анализ|кров[иь]|моч[иа]|кала\b|сыворот|плазм|мазк|соскоб|антител|антиген|гормон|"
                       r"бактериолог|бактериоскоп|микробиолог|посев|микроскоп|цитолог|гистолог|иммуногистохим|"
                       r"биохим|коагулогр|гемостаз|липидогр|глюкоз|гемоглоб|креатинин|мочевин|билирубин|холестерин|"
                       r"ферритин|маркер|генетическ|кариотип|серологич", re.I)),
]
LAB_CODES = ("A09", "A26", "A08", "A12.05", "A12.06", "A12.22", "A12.28", "B03.014")
INSTRUMENTAL_CODES = ("A02", "A03", "A04", "A05", "A06", "A07", "A11", "A12")


def member_kind(text: str, services: list) -> str | None:
    for kind, rx in KIND_WORDS:
        if rx.search(text):
            # «анализ крови» but «УЗИ … с биопсией» — an instrumental word wins over «крови» only
            # when no lab word is there: «рентгенография» is instrumental, «микроскопия мочи» lab
            if kind == "instrumental" and KIND_WORDS[-1][1].search(text) and not re.search(
                    r"(?<![А-ЯЁA-Z])(?:УЗИ|КТ|МРТ|ЭКГ|ЭхоКГ)(?![А-ЯЁA-Z])|ультразвук|томограф|рентген", text, re.I):
                return "lab"
            return kind
    code = services[0]["code"] if services else ""
    if code.startswith(LAB_CODES):
        return "lab"
    if code.startswith(INSTRUMENTAL_CODES):
        return "instrumental"
    if code.startswith("B01"):
        return "consult"
    if code.startswith("A01"):
        return "exam"
    return None


def item_kinds(it: dict) -> set:
    sub = it.get("subgroup") or ""
    if re.search(r"лаборатор", sub, re.I):
        return {"lab"}
    if re.search(r"инструмент|лучев|функциональн|эндоскоп", sub, re.I):
        return {"instrumental"}
    kinds = {member_kind(m["text"], m.get("services") or []) for seg in it["segments"] for m in seg.get("members", [])}
    kinds.discard(None)
    if not kinds:
        k = member_kind(it["text"], [])
        kinds = {k} if k else set()
    return kinds


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


def bucket_keys(keys) -> tuple:
    """Stems grouped by length and by first 3 letters, for `bucket_match`."""
    by_len, by_head = {}, {}
    for k in keys:
        by_len.setdefault(len(k), []).append(k)
        by_head.setdefault(k[:3], []).append(k)
    return by_len, by_head


def bucket_match(buckets, r: str) -> set:
    """The stems `contains` accepts for `r`, comparing only those that can match: a one-letter typo keeps the
    length within ±1, a different suffix keeps the first letters (`prefix_match` needs 5 in common)."""
    by_len, by_head = buckets
    cand = set(by_head.get(r[:3], ())) | {k for n in (len(r) - 1, len(r), len(r) + 1) for k in by_len.get(n, ())}
    return {k for k in cand if contains([k], r)}


def build_stem_index(engine) -> dict:
    """stem → sorted indexes of usable sections whose list items (not text mentions) contain it."""
    idx = {}
    for i, s in enumerate(engine.kb["sections"]):
        if not engine.usable(s):
            continue
        items = _read_items(engine.kb_path.parent / engine.kb["packed"], *s["_blob"]) if "_blob" in s else s["items"]
        for it in items:
            if it["level"] == "text":
                continue
            for seg in it["segments"]:
                for st in list(seg["stems"]) + [x for m in seg.get("members", []) for x in m["stems"]]:
                    idx.setdefault(st, set()).add(i)
    return {k: sorted(v) for k, v in idx.items()}


class Engine:
    def __init__(self, kb_path: Path, ref: Path, llm: bool = False):
        self.kb_path = kb_path
        self._stem_index = None  # reverse search: stem → usable section indexes (kb.stems.json)
        self._analytes = None
        self._name_index = None  # name stem → diagnosis names (resolve_diagnosis pre-filter)
        self.kb = json.loads(kb_path.read_text(encoding="utf-8"))
        if self.kb.get("packed"):  # kb.idx.json: section items stay on disk (see kb_pack.py)
            blob = kb_path.parent / self.kb["packed"]
            self.kb["sections"] = [LazySection(s, blob) for s in self.kb["sections"]]
        self.llm = llm
        cc = Path(__file__).parent / "crosscheck" / f"{kb_path.resolve().parent.name}.json"  # kb_llm.py crosscheck
        self.crosscheck = {}
        if cc.exists():
            for sec in json.loads(cc.read_text(encoding="utf-8"))["sections"].values():
                self.crosscheck.update(sec["items"])
        self.norm = Normalizer()
        # insurance rules (insurance.py build) — answers get a separate «insured event?» block
        self.insurance = insurance.Insurance(norm=self.norm) if insurance.BUILT.exists() else None
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
        up = text.upper()  # «e06.3» typed in lower case
        m = ICD_CODE_RE.search(up.translate(CYR2LAT) if up[:1] in "АВЕКМНОРСТХ" else up)
        if m:
            code = m.group(1).translate(CYR2LAT) + m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
            if code in self.icd:
                return [{"code": code, "name": self.icd[code], "score": 1.0}]
        q = text.lower()
        q_norm = re.sub(r"\s+", " ", re.sub(r"[^а-яa-z0-9 ]", "", q.replace("ё", "е"))).strip()
        qs = [s for s in self.norm.stems(text) if s not in GENERIC and s not in DX_NEUTRAL]
        best = {}
        # only names sharing a (fuzzy) stem with the question — the same filter as before, via a stem index
        names = self.names
        if qs:
            if self._name_index is None:
                self._name_index = {}
                for n, ns in self.name_stems.items():
                    for st in ns:
                        self._name_index.setdefault(st, set()).add(n)
                self._name_buckets = bucket_keys(self._name_index)
            keep = {n for x in qs for k in bucket_match(self._name_buckets, x) for n in self._name_index[k]}
            names = [(c, n) for c, n in self.names if n in keep]
        for code, name in names:
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
            if q_norm == re.sub(r"\s+", " ", re.sub(r"[^а-яa-z0-9 ]", "", name.lower().replace("ё", "е"))).strip():
                sim += 0.08  # the very name beats a sibling with the same stems («ларингит» / «ларинготрахеит»)
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

    def literal_tokens(self, service: str) -> list:
        """«C-пептид»: the stemmer drops the one-letter part and «пептид» alone also matches «натрийуретический
        пептид», so «c-пепт» must occur in the item text. Only when one stem is left — «С-реактивный белок»
        keeps two stems and may be written «СРБ» or «С-реактивного»."""
        if len(self.required_stems(service)) > 1:
            return []
        return [lookalike(m.group(1) + "-" + m.group(2)[:4])
                for m in re.finditer(r"(?<![\w-])([A-Za-zА-Яа-яЁёα-ω])-([A-Za-zА-Яа-яЁё]{3,})", service)]

    def find(self, sections, required, literal=(), keysets=None):
        """`keysets`: {required stem: set of stems that match it} (reverse search, from the index) — set
        lookups instead of fuzzy comparisons of every member."""
        if keysets:
            covers = lambda stems, req: all(not keysets[r].isdisjoint(stems) for r in req)  # noqa: E731
        else:
            covers = self.covers
        hits = []
        for s in sections:
            for it in s["items"]:
                how = None
                for seg in it["segments"]:
                    if any(covers(m["stems"], required) for m in seg.get("members", [])):
                        how = "exact"
                        break
                    if covers(seg["stems"], required):
                        how = how or ("exact" if it["level"] == "text" else "segment")
                if how and literal:
                    text = lookalike(it["text"] + " " + (it.get("header") or ""))
                    if not all(t in text for t in literal):
                        how = None
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

    def _llm_parse(self, question: str):
        """(diagnosis — the LLM's МКБ-10 code the protocols cover, else its first code or wording;
        service; population or None; note for the answer)."""
        from kb_llm import parse_question  # needs the anthropic package and credentials
        parsed = parse_question(question)
        codes = [c["code"].translate(CYR2LAT) for c in parsed["icd10"] if c["code"].translate(CYR2LAT) in self.icd]
        # prefer the LLM's code that the protocols cover (E27.1 over E27.2 for Аддисон)
        covered = [c for c in codes if c in self.covered
                   or any(x.startswith(c + ".") or c.startswith(x + ".") for x in self.covered)]
        service = parsed["service"].strip()
        if re.fullmatch(r"(?:не\s+)?(?:указан\w*|нет|отсутству\w*|-|—)(?:\s+в\s+вопросе)?\.?", service, re.I):
            service = ""  # «не указана в вопросе»
        return ((covered or codes or [parsed["diagnosis"]])[0], service or None,
                parsed["population"] if parsed["population"] != "unknown" else None,
                f"диагноз выделен LLM: «{parsed['diagnosis']}» → {', '.join(codes) or 'код не предложен'}")

    def insurance_check(self, chosen, diagnosis: str, service, **kw):
        """Insurance block for the chosen code — unless the wording fits several МКБ-10 rubrics the rules
        treat differently («сахарный диабет»: E10 excluded, E11 insured): then ask to clarify."""
        if not self.insurance:
            return None
        ins = self.insurance.check(chosen["code"], chosen["name"], diagnosis, service, **kw)
        qs = [x for x in self.norm.stems(diagnosis) if x not in GENERIC and x not in DX_NEUTRAL]
        if not qs or ICD_CODE_RE.search(diagnosis.translate(CYR2LAT)):
            return ins
        # every rubric whose МКБ-10 name has all the words of the question (exact stems, no prefixes:
        # «псориаз» must not pull in «псориатическая артропатия»)
        fits = [c for c, n in self.icd.items() if (len(c) == 3 or c == chosen["code"]) and re.fullmatch(r"[A-Z]\d{2}(\.\d{1,2})?", c)
                and all(any(close(s, w) for w in self.name_stems.get(n.lower()) or self.norm.stems(n)) for s in qs)]
        if not 1 < len(fits) <= 15:
            return ins
        # pregnancy / perinatal / congenital rubrics («врожденная пневмония») only when the question is about them
        fits = [c for c in fits if c[0] not in "OPQ" or c[0] == chosen["code"][0]]
        variants = []
        for c in fits:
            v = self.insurance.check(c, self.icd[c], "", service)
            variants.append({"code": c, "name": self.icd[c], "verdict_level": v["verdict_level"],
                             "clauses": [{"clause": m["clause"], "short": m["short"], "url": m["url"]} for m in v["excluded"]]})
        # only a named exclusion counts: a parent rubric with one excluded subcode (K29 ∋ K29.2 alcoholic) is not one
        if not any(v["verdict_level"] == "excluded" for v in variants)                 or not any(v["verdict_level"] in ("covered", "limited") for v in variants):
            return ins
        parts = []
        for level in ("excluded", "probable", "limited", "covered"):
            vs = [v for v in variants if v["verdict_level"] == level]
            if vs:
                cl = sorted({m["clause"] for v in vs for m in v["clauses"]})
                parts.append(f"{insurance.SHORT[level]}" + (f" (п. {', '.join(cl)})" if cl else "") + ": "
                             + ", ".join(f"{v['code']} {v['name']}" for v in vs))
        ins.update(verdict_level="unclear", excluded=[], variants=variants,
                   verdict=f"УТОЧНИТЕ ДИАГНОЗ — «{diagnosis.strip()}» может означать разные заболевания. " + "; ".join(parts))
        return ins

    # ---------------------------------------------------------------- reverse search
    def usable(self, s) -> bool:
        """Table-layout and scanned documents count only through their extracted table rows."""
        return self.kb["documents"][s["doc_id"]]["kind"] not in ("table", "scan") or s["icd10_source"] == "table"

    def stem_index(self) -> dict:
        """stem → indexes of usable sections having a list item with that stem. Read from kb.stems.json
        next to the KB (`kb.py index`), else built once from the items (a few seconds) and kept."""
        if self._stem_index is None:
            f = self.kb_path.parent / "kb.stems.json"
            if f.exists():
                self._stem_index = json.loads(f.read_text(encoding="utf-8"))
            else:
                self._stem_index = build_stem_index(self)
        return self._stem_index

    def matching_keys(self, r: str) -> set:
        """Index stems that `contains` accepts for `r`. Only keys that can match are compared: a one-letter
        typo keeps the length within ±1, a different suffix keeps the first letters (`prefix_match` needs 5)."""
        if not hasattr(self, "_key_buckets"):
            self._key_buckets, self._key_cache = bucket_keys(self.stem_index()), {}
        if r not in self._key_cache:
            self._key_cache[r] = bucket_match(self._key_buckets, r)
        return self._key_cache[r]

    def service_like(self, text: str) -> bool:
        """«ТТГ», «метанефрины», «сцинтиграфия щитовидной железы» name a service; «гипотиреоз»,
        «мочекаменная болезнь», «псориаз СОЭ» do not (a diagnosis wins or is part of it)."""
        if ICD_CODE_RE.search(text.translate(CYR2LAT)):
            return False
        stems = self.norm.stems(text)
        if not stems:
            return False
        if any(x in GENERIC or x in METHOD or SERVICE_STEM_RE.search(x) for x in stems):
            return True
        if self._analytes is None:
            self._analytes = {x for w in ANALYTES for x in self.norm.stems(w)}
        analyte = any(x in self._analytes for x in stems)
        # «ТТГ», «кальций», «антитела к ТПО»: an abbreviation or a substance, at most two words — no diagnosis
        # lookup (it is the slow part); longer wordings may still be a diagnosis («недостаточность витамина D»)
        if analyte and len(text.split()) <= 2 and not re.search(r"(?i)недостаточн|избыт|дефицит|отравлен|болезн|синдром", text):
            return True
        dx = self.resolve_diagnosis(text)
        dx_score = dx[0]["score"] if dx else 0
        if dx_score >= LLM_DX_SCORE:  # the very name of a diagnosis: «недостаточность витамина D»
            return False
        if analyte or dx_score < MIN_DX_SCORE:
            return True
        # «Комплекс исследований для диагностики мочекаменной болезни» names the disease, not a test
        svc = [c for c in self.cat.match(stems, top=3, min_score=0.25, query=True)
               if not re.search(r"диагностик|комплекс", c["name"], re.I)]
        return bool(svc) and svc[0]["score"] >= 0.85 and svc[0]["score"] > dx_score + 0.1

    def reverse_search(self, question, services: list, population: str | None = None, top: int = 15):
        """Diagnoses whose protocols list the services: sections ranked by how many of the services
        they list (rare ones weigh more, mandatory more than «по показаниям»)."""
        import math
        idx = self.stem_index()
        sections = self.kb["sections"]
        n_usable = max(1, len({i for v in idx.values() for i in v}))
        keys = list(idx)
        if population is None and question:
            population = "child" if CHILD_RE.search(question) else "adult" if ADULT_RE.search(question) else None
        # candidates of every service from the index alone (cheap); the items are read only to confirm
        plan = []
        for svc in services:
            req = self.required_stems(svc)
            keysets = {r: self.matching_keys(r) for r in req}  # typo-tolerant over the index keys
            posting = {r: {i for k in keysets[r] for i in idx[k]} for r in req}
            # a word no protocol list has («назанчают» — a misspelt verb) is dropped if others remain
            if len(req) > 1 and any(posting[r] for r in req):
                req = [r for r in req if posting[r]]
            cand = None
            for r in req:
                cand = posting[r] if cand is None else cand & posting[r]
            plan.append((svc, req, keysets, cand or set()))
        # a common service (ОАК, глюкоза: hundreds of sections — reading them all takes seconds on the host) is
        # confirmed only where the rarer services of the question are; alone, in its first GENERIC_CAP sections
        rare = {i for _, _, _, c in plan if len(c) <= GENERIC_CAP for i in c}
        pos = {s["id"]: i for i, s in enumerate(sections)}
        svc_out, per_section, confirmed = [], {}, set()
        for svc, req, keysets, cand in sorted(plan, key=lambda x: len(x[3])):
            common = len(cand) > GENERIC_CAP
            look = sorted(cand & confirmed if common and rare else cand)[:GENERIC_CAP if common and not rare else None]
            hits = [h for h in self.find([sections[i] for i in look], req, self.literal_tokens(svc), keysets)
                    if h[1]["level"] != "text"]
            secs = {}
            for sec, it, how in hits:
                if population and sec["population"] and population not in sec["population"]:
                    continue
                best = secs.get(sec["id"])
                if best is None or LEVELS[it["level"]][0] < LEVELS[best[1]["level"]][0]:
                    secs[sec["id"]] = (sec, it, how)
            if not common:
                confirmed |= {pos[sid] for sid in secs}
            df = len(cand) if common else len(secs)  # a common service: the index count (not every one confirmed)
            svc_out.append({"text": svc, "catalogue": self.resolve_service(svc)[:1], "sections": df, "found": bool(secs) or (common and bool(cand)),
                            "_order": services.index(svc)})
            idf = math.log((n_usable + 1) / (df + 1)) + 0.1
            for sid, (sec, it, how) in secs.items():
                e = per_section.setdefault(sid, {"sec": sec, "matched": {}, "score": 0.0})
                e["matched"][svc] = (it, how)
                e["score"] += REVERSE_WEIGHT.get(it["level"], 0.5) * idf
        svc_out.sort(key=lambda x: x.pop("_order"))
        found = [x["text"] for x in svc_out if x["found"]]
        # one entry per protocol + set of codes (a chapter split into table rows or populations)
        merged = {}
        for e in per_section.values():
            sec = e["sec"]
            key = (sec["doc_id"], tuple(sorted(sec["icd10"])) or sec["chapter_title"])
            m = merged.setdefault(key, {"secs": [], "matched": {}, "score": 0.0})
            m["secs"].append(sec)
            for svc, (it, how) in e["matched"].items():
                if svc not in m["matched"] or LEVELS[it["level"]][0] < LEVELS[m["matched"][svc][1]["level"]][0]:
                    m["matched"][svc] = (sec, it, how)
            m["score"] = max(m["score"], e["score"])
        entries = []
        for m in merged.values():
            sec = m["secs"][0]
            # chapters bound only by a protocol's scope or with no codes are weaker evidence of «the» diagnosis
            weight = 1.0 if sec["icd10_source"] in ("classification", "title", "table") else 0.6 if sec["icd10"] else 0.4
            doc = self.kb["documents"][sec["doc_id"]]
            codes = [c for c in dict.fromkeys(c for x in m["secs"] for c in x["icd10"]) if c in self.icd]
            entries.append({
                "diagnoses": [{"code": c, "name": self.icd[c]} for c in codes[:6]], "more_codes": max(0, len(codes) - 6),
                "title": sec["chapter_title"] or (sec.get("names") or [""])[0], "names": sec.get("names", [])[:3],
                "protocol": sec["protocol"] or doc["title"], "decree": doc["decree"], "url": doc["url"],
                "chapter": sec["chapter"], "population": pop_label(sec["population"]),
                "icd10_source": sec["icd10_source"],
                "matched": [dict(self.cite(x, it), service=svc, text=it["text"], match=how)
                            for svc, (x, it, how) in sorted(m["matched"].items(), key=lambda kv: found.index(kv[0]))],
                "missing": [x for x in found if x not in m["matched"]],
                "coverage": f"{len(m['matched'])} из {len(found)}",
                "score": round(m["score"] * weight, 3), "_n": len(m["matched"]),
            })
        entries.sort(key=lambda e: (-e["_n"], -e["score"]))
        entries = entries[:top]
        for e in entries:
            e.pop("_n")
            if self.insurance and e["diagnoses"]:
                d0 = e["diagnoses"][0]
                ins = self.insurance.check(d0["code"], d0["name"])
                e["insurance"] = {"verdict_level": ins["verdict_level"], "short": insurance.SHORT.get(ins["verdict_level"], ""),
                                  "clauses": [{"clause": x["clause"], "short": x["short"], "url": x["url"], "quote": x["quote"]}
                                              for x in ins["excluded"]]}
        share = [x["sections"] / n_usable for x in svc_out if x["found"]]
        generic = bool(share) and min(share) > 0.05
        n = len(entries)
        word = "диагноз" + ("" if n % 10 == 1 and n % 100 != 11 else "а" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "ов")
        all_found = bool(entries) and len(found) > 1 and entries[0]["missing"] == []
        return {
            "mode": "reverse", "question": question, "population": population,
            "services": svc_out, "not_found": [x["text"] for x in svc_out if not x["found"]],
            "results": [{"population": "все", "verdict_level": "reverse", "entries": entries, "verdict": f"{n} {word}",
                         # flat list, so consumers of check answers (feedback summary) keep working
                         "evidence": [x for e in entries[:3] for x in e["matched"]]}] if entries else [],
            "verdict_level": "reverse" if entries else None,
            "verdict": (f"ОБРАТНЫЙ ПОИСК: {n} {word}" + (" — у первых в протоколе есть все перечисленные услуги" if all_found else ""))
                       if entries else "Услуги не найдены в перечнях диагностики протоколов",
            "generic_warning": (f"Все указанные услуги общие (каждая — в {round(100 * min(share))}% и более разделов протоколов): "
                                "по ним диагноз не определить, добавьте специфичные исследования") if generic else None,
            "evidence": [x for e in entries[:3] for x in e["matched"]],
            "unparsed": [],
            "method": ("правила: услуги → разделы протоколов, ранжирование по числу совпавших услуг, "
                       "редкости услуги и уровню (обязательная диагностика выше «по показаниям»)"),
        }

    def _choose(self, dx):
        """The top МКБ-10 candidate, or a close one the protocols cover when it isn't covered."""
        chosen = dx[0]
        if chosen["code"] not in self.covered and not any(c.startswith(chosen["code"] + ".") or chosen["code"].startswith(c + ".")
                                                         for c in self.covered):
            # «хламидийная инфекция» → A74.9 (no protocol) vs A56.x (STI protocol) scored alike:
            # take the close candidate the protocols cover
            for d in dx[1:]:
                if d["score"] >= chosen["score"] - 0.1 and d["code"] in self.covered:
                    return d
        return chosen

    def ask(self, question: str | None = None, diagnosis: str | None = None, service: str | None = None,
            population: str | None = None):
        llm_used = None
        if question and not (diagnosis or service):
            # «при каких диагнозах назначают X»; a bare list of services «ТТГ, свободный Т4, АТ к ТПО»
            rev = parse_reverse_question(question)
            if rev:
                return self.reverse_search(question, rev, population=population)
        if question and not (diagnosis and service):
            lst = parse_list_question(question)
            if lst:
                return self.list_services(question, *lst, population=population)
            if not (diagnosis or service) and not any(rx.match(question.strip()) for rx in SPLIT_RES):
                parts = split_services(WHO_RE.sub("", question))
                if parts and all(self.service_like(x) for x in parts):
                    return self.reverse_search(question, parts, population=population)
            d, s = split_question(question)
            if not (d or s or diagnosis or service):
                # only a diagnosis («сахарный диабет 2 типа», «E06.3»): insurance verdict + every
                # diagnostic measure of the protocols
                cand = _clean_dx(question.strip(" ?."))
                # «анализ крови», «УЗИ щитовидной железы» name a service, not a diagnosis
                is_service = any(x in GENERIC or x in METHOD or SERVICE_STEM_RE.search(x) for x in self.norm.stems(cand))
                dx = self.resolve_diagnosis(cand) if cand and not is_service else []
                if dx and dx[0]["score"] >= (LLM_DX_SCORE if self.llm else MIN_DX_SCORE):
                    return dict(self.list_services(question, cand, population=population), diagnosis_only=True)
            diagnosis, service = diagnosis or d, service or s
        asked_diagnosis = diagnosis  # the user's wording, shown even when the LLM supplies the code
        if self.llm and question and (not diagnosis or not service or self._weak(diagnosis)):
            diagnosis, s, pop, llm_used = self._llm_parse(question)
            service = service or s
            population = population or pop
            if diagnosis and not service:  # the LLM found only a diagnosis
                res = self.list_services(None, diagnosis, population=population)
                if "method" in res:
                    res["method"] += f"; {llm_used}"
                return dict(res, question=question, diagnosis_only=True)
        if not diagnosis or not service:
            return {"error": "Не удалось выделить диагноз и услугу. Сформулируйте как «Диагноз – …, разрешено ли …», "
                             "«…, перечень лабораторных анализов» или укажите только диагноз (или код МКБ-10)."}
        if population is None and question:
            population = "child" if CHILD_RE.search(question) else "adult" if ADULT_RE.search(question) else None

        dx = self.resolve_diagnosis(diagnosis)
        if not dx or dx[0]["score"] < MIN_DX_SCORE:
            hint = "; ".join(f"{d['code']} {d['name']}" for d in dx[:4])
            return {"diagnosis_text": diagnosis, "service_text": service, "verdict_level": "unknown_diagnosis",
                    "error": f"Диагноз «{diagnosis}» не распознан уверенно"
                             + (f". Возможно: {hint}. Уточните или укажите код МКБ-10." if hint else ". Укажите код МКБ-10.")}
        chosen = self._choose(dx)
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
            hits = self.find(secs, required, self.literal_tokens(service))
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
            # «not provided for» only when a protocol was checked: п. 4.1.50 of the insurance rules
            "insurance": self.insurance_check(chosen, diagnosis, service,
                                              protocol_level=main["verdict_level"] if results else "-"),
        }

    def list_services(self, question: str | None, diagnosis: str, category: str | None = None,
                      service: str | None = None, population: str | None = None):
        """Every diagnostics list item for a diagnosis — of one kind (`category`: lab /
        instrumental / consult) and/or matching a service (`service`: «КТ» → all kinds of CT)."""
        llm_used = None
        asked_diagnosis = diagnosis
        if self.llm and question and self._weak(diagnosis):
            diagnosis, _, pop, llm_used = self._llm_parse(question)
            population = population or pop
        if population is None and question:
            population = "child" if CHILD_RE.search(question) else "adult" if ADULT_RE.search(question) else None
        base = {"mode": "list", "question": question, "diagnosis_text": asked_diagnosis, "category": category,
                "category_label": CATEGORIES[category][0] if category else "все диагностические мероприятия",
                "service_text": service, "population": population}
        dx = self.resolve_diagnosis(diagnosis)
        if not dx or dx[0]["score"] < MIN_DX_SCORE:
            hint = "; ".join(f"{d['code']} {d['name']}" for d in dx[:4])
            return dict(base, verdict_level="unknown_diagnosis",
                        error=f"Диагноз «{diagnosis}» не распознан уверенно"
                              + (f". Возможно: {hint}. Уточните или укажите код МКБ-10." if hint else ". Укажите код МКБ-10."))
        chosen = self._choose(dx)
        ambiguous = len(dx) > 1 and dx[1]["score"] >= dx[0]["score"] - 0.03 and dx[1]["code"][:3] != dx[0]["code"][:3]
        dx_stems = [x for x in self.norm.stems(diagnosis) if x not in GENERIC and x not in DX_NEUTRAL] \
            if not ICD_CODE_RE.fullmatch(diagnosis.strip()) else []
        dx_stems = dx_stems or [x for x in self.norm.stems(chosen["name"]) if x not in GENERIC and x not in DX_NEUTRAL]
        sections = self.sections_for(chosen["code"], population, diagnosis)
        usable = [s for s in sections if self.kb["documents"][s["doc_id"]]["kind"] not in ("table", "scan")
                  or s["icd10_source"] == "table"]
        unparsed = sorted({s["doc_id"] for s in sections} - {s["doc_id"] for s in usable})
        required = self.required_stems(service) if service else None

        def about(s):  # the chapter / table row / protocol title names the diagnosis
            ns = [x for x in self.norm.stems(" ".join([s["chapter_title"]] + s.get("names", []))) if x not in GENERIC]
            return all(contains(ns, x) for x in dx_stems) or self.primary(s["doc_id"], dx_stems)

        picked = []  # (section, [(item, match)])
        for s in usable:
            if required:
                hits = [(it, how) for _, it, how in self.find([s], required, self.literal_tokens(service)) if it["level"] != "text"]
            else:
                hits = [(it, None) for it in s["items"] if it["level"] != "text"]
            if category:
                hits = [(it, how) for it, how in hits if category in item_kinds(it)]
            seen, uniq = set(), []
            for it, how in hits:
                k = (it["text"], it["level"], it.get("condition"))
                if k not in seen:
                    seen.add(k)
                    uniq.append((it, how))
            if uniq:
                uniq.sort(key=lambda h: LEVELS[h[0]["level"]][0])  # stable: protocol order within a level
                picked.append((s, uniq, s["tier"] == 0 and about(s)))
        # chapters bound only by the scope of a protocol about another disease are noise when
        # anything closer exists; protocols about the diagnosis itself go first
        if any(s["tier"] != 1 for s, _, _ in picked):
            picked = [p for p in picked if p[0]["tier"] != 1]
        picked.sort(key=lambda p: (not p[2], p[0]["tier"], p[0]["doc_id"], p[0]["page"]))
        # «ДИАГНОСТИКА СД» states its tests in the text (criteria tables), not as a list
        got = {p[0]["id"] for p in picked}
        text_only = []
        for s in usable:
            if s["id"] not in got and s["tier"] == 0 and re.search(r"диагност", s["chapter_title"], re.I) and about(s):
                doc = self.kb["documents"][s["doc_id"]]
                text_only.append({"protocol": s["protocol"] or doc["title"], "chapter": s["chapter"],
                                  "chapter_title": s["chapter_title"], "url": f"{doc['url']}#page={s['page']}"})

        groups = {}
        for p in picked:
            groups.setdefault(pop_label(p[0]["population"]), []).append(p)
        results = []
        for label in sorted(groups, key=lambda l: POP_ORDER.index(l)):
            secs = []
            has_main = any(p[2] for p in groups[label])  # «смежный» only next to a protocol about the diagnosis
            for s, hits, main in groups[label]:
                doc = self.kb["documents"][s["doc_id"]]
                secs.append({"protocol": s["protocol"] or doc["title"], "decree": doc["decree"], "url": doc["url"],
                             "chapter": s["chapter"], "chapter_title": s["chapter_title"], "related": has_main and not main,
                             "items": [dict(self.cite(s, it), text=it["text"], match=how, kinds=sorted(item_kinds(it)))
                                       for it, how in hits]})
            n = sum(len(x["items"]) for x in secs)
            results.append({"population": label, "verdict_level": "list", "sections": secs,
                            "verdict": f"{n} позиц{'ия' if n % 10 == 1 and n % 100 != 11 else 'ии' if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 'ий'}"
                                       f" в {len(secs)} раздел{'е' if len(secs) % 10 == 1 and len(secs) % 100 != 11 else 'ах'}",
                            # flat list, so consumers of check answers (feedback summary) keep working
                            "evidence": [e for x in secs for e in x["items"]]})
        return dict(
            base, diagnosis=chosen, diagnosis_alternatives=dx[1:4] if ambiguous else [],
            service_catalogue=self.resolve_service(service) if service else [],
            verdict_level="list" if results else ("manual" if unparsed else None),
            verdict=("ПЕРЕЧЕНЬ: " + "; ".join(f"{r['population']} — {r['verdict']}" for r in results)) if results
            else VERDICTS["manual"] if unparsed
            else "В перечнях диагностики протоколов для этого диагноза ничего не найдено" if sections
            else "ПРОТОКОЛ НЕ НАЙДЕН для этого диагноза",
            evidence=results[0]["evidence"] if results else [], results=results, text_only=text_only,
            insurance=self.insurance_check(chosen, diagnosis, service,
                                           items=list(dict.fromkeys(e["text"] for r in results for e in r["evidence"]))),
            unparsed=[{"protocol": self.kb["documents"][d]["title"], "url": self.kb["documents"][d]["url"],
                       "kind": self.kb["documents"][d]["kind"]} for d in unparsed],
            method=("правила + перекрёстная проверка LLM" if self.crosscheck else "правила (без перекрёстной проверки LLM)")
                   + "; вид исследования (лабораторное / инструментальное) определён по словам и коду услуги пост. №123"
                   + (f"; {llm_used}" if llm_used else ""))


def render_list(a: dict) -> str:
    out = []
    d = a["diagnosis"]
    out.append(f"Диагноз: {d['code']} {d['name']}   (из «{a['diagnosis_text']}»)")
    for alt in a["diagnosis_alternatives"]:
        out.append(f"   или? {alt['code']} {alt['name']}")
    out.append(f"Перечень: {a['category_label']}" + (f", услуга «{a['service_text']}»" if a["service_text"] else "")
               + ("   (указан только диагноз)" if a.get("diagnosis_only") else ""))
    if a["population"]:
        out.append(f"Население: {'дети' if a['population'] == 'child' else 'взрослые'}")
    if not a["results"]:
        out.append("")
        out.append(a["verdict"])
    for r in a["results"]:
        out.append("")
        out.append(f"=== {r['population'].upper()}: {r['verdict']}")
        for s in r["sections"]:
            out.append("")
            dec = s["decree"]
            out.append(f"## {s['protocol']}" + (f" (пост. МЗ от {dec['date']} № {dec['number']})" if dec and dec['number'] not in s['protocol'] else ""))
            out.append(("Глава " + str(s["chapter"]) + " «" + s["chapter_title"] + "»" if s["chapter"] is not None else s["chapter_title"] or "")
                       + ("   [смежный раздел — проверьте применимость]" if s["related"] else ""))
            level = None
            for e in s["items"]:
                if e["level"] != level:
                    level = e["level"]
                    out.append(f"  {LEVELS[level][1].capitalize()}:")
                pages = f"{e['pdf_pages'][0]}–{e['pdf_pages'][-1]}" if len(e["pdf_pages"]) > 1 else str(e["pdf_pages"][0])
                cond = f"[{e['condition']}] " if e["condition"] else ""
                clause = f"п. {e['clause']}, " if e["clause"] else ""
                out.append(f"    - {cond}{e['text']}   ({clause}стр. {pages})")
                out.append(f"      {e['url']}")
    for t in a["text_only"]:
        out.append("")
        out.append(f"В тексте, не перечнем (проверьте по главе): {t['protocol']}, "
                   + (f"глава {t['chapter']} «{t['chapter_title']}»" if t["chapter"] is not None else t["chapter_title"]))
        out.append(f"    {t['url']}")
    for u in a["unparsed"]:
        out.append("")
        out.append(f"Не разобран автоматически ({'таблицы' if u['kind'] == 'table' else 'скан'}), проверьте вручную: {u['protocol']}")
        out.append(f"    {u['url']}")
    out.append("")
    out.append(f"Метод: {a['method']}.")
    return "\n".join(out)


def render_reverse(a: dict) -> str:
    out = ["Обратный поиск: при каких диагнозах протоколы предусматривают услуги"]
    for x in a["services"]:
        cat = f" → пост. №123: {x['catalogue'][0]['code']} {x['catalogue'][0]['name']}" if x["catalogue"] else ""
        out.append(f"  «{x['text']}»{cat} — " + (f"в {x['sections']} разделах протоколов" if x["found"] else "не найдена в перечнях протоколов"))
    if a["population"]:
        out.append(f"Население: {'дети' if a['population'] == 'child' else 'взрослые'}")
    out.append("")
    out.append(a["verdict"])
    if a.get("generic_warning"):
        out.append("ВНИМАНИЕ: " + a["generic_warning"])
    for i, e in enumerate((a["results"] or [{}])[0].get("entries", []), 1):
        out.append("")
        codes = "; ".join(f"{d['code']} {d['name']}" if j < 2 else d["code"] for j, d in enumerate(e["diagnoses"])) \
            + (f" и ещё {e['more_codes']}" if e["more_codes"] else "")
        out.append(f"[{i}] {e['title']} — совпало {e['coverage']}" + (f" ({e['population']})" if e["population"] != "население не указано" else ""))
        out.append(f"    МКБ-10: {codes or 'коды не указаны'}")
        dec = e["decree"]
        out.append(f"    {e['protocol']}" + (f" (пост. МЗ от {dec['date']} № {dec['number']})" if dec and dec['number'] not in e['protocol'] else ""))
        if e.get("insurance"):
            ins = e["insurance"]
            out.append(f"    Страхование ({e['diagnoses'][0]['code']}): {ins['short']}" + (f" — п. {', '.join(c['clause'] for c in ins['clauses'])}" if ins["clauses"] else ""))
        for m in e["matched"]:
            out.append(f"    + {m['service']}: {LEVELS[m['level']][1]}, п. {m['clause']}, стр. {m['pdf_pages'][0]}")
            out.append(f"      «{m['quote']}»")
            out.append(f"      {m['url']}")
        if e["missing"]:
            out.append(f"    − нет в перечнях этого раздела: {', '.join(e['missing'])}")
    out.append("")
    out.append(f"Метод: {a['method']}.")
    return "\n".join(out)


def render(a: dict) -> str:
    if a.get("error"):
        return "Ошибка: " + a["error"]
    if a.get("mode") == "reverse":
        return render_reverse(a)
    text = render_list(a) if a.get("mode") == "list" else render_check(a)
    return text + ("\n" + insurance.render(a["insurance"]) if a.get("insurance") else "")


def render_check(a: dict) -> str:
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
        if c.get("mode") == "reverse":  # a code starting with one of expect_any among the top entries' diagnoses
            entries = (a.get("results") or [{}])[0].get("entries", [])[:c.get("top", 3)]
            got = [d["code"] for e in entries for d in e["diagnoses"]]
            good = a.get("mode") == "reverse" and (
                any(g.startswith(x) for g in got for x in c["expect_any"]) if c["expect_any"] else True) \
                and (not c.get("generic") or bool(a.get("generic_warning")))
            ok += good
            print(f"{'OK  ' if good else 'FAIL'} {c['question']}")
            if not good:
                print(f"       expected one of {c['expect_any']} in top {c.get('top', 3)}; got {a.get('mode')} "
                      f"{[e['diagnoses'][0]['code'] if e['diagnoses'] else e['title'][:30] for e in entries]} "
                      f"services {[(x['text'], x['sections']) for x in a.get('services', [])]}  {a.get('error', '')}")
            continue
        if c.get("insurance"):  # insurance verdict: level and the excluding clause
            ins = a.get("insurance") or {}
            got = [m["clause"] for m in ins.get("excluded", [])]
            good = ins.get("verdict_level") == c["insurance"] and (not c.get("clause") or c["clause"] in got)
            ok += good
            print(f"{'OK  ' if good else 'FAIL'} {c['question']}")
            if not good:
                print(f"       expected {c['insurance']} п.{c.get('clause')}; got {ins.get('verdict_level')} п.{got}  {a.get('error', '')}")
            continue
        if c.get("mode") == "list":  # list question: diagnosis, category and items that must be listed
            texts = [e["text"].lower() for r in a.get("results", []) for e in r["evidence"]]
            missing = [t for t in c.get("contains", []) if not any(t.lower() in x for x in texts)]
            good = (a.get("mode") == "list" and (a.get("diagnosis") or {}).get("code") == c["icd"]
                    and a.get("category") == c.get("category") and a.get("service_text") == c.get("service")
                    and not missing)
            ok += good
            print(f"{'OK  ' if good else 'FAIL'} {c['question']}")
            if not good:
                print(f"       expected {c['icd']} {c.get('category')} {c.get('service')}; got {a.get('mode')} "
                      f"{(a.get('diagnosis') or {}).get('code')} {a.get('category')} {a.get('service_text')}; "
                      f"missing {missing}  {a.get('error', '')}")
            continue
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
    ix = sub.add_parser("index", help="write kb.stems.json (reverse search index) next to the KB")
    for p in (a, e, ix):
        p.add_argument("--llm", action="store_true", help="use Claude when the diagnosis is not recognized confidently")
        p.add_argument("--kb", type=Path, default=Path("kb/kb.json"))
        p.add_argument("--reference", type=Path, default=Path("reference"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    engine = Engine(args.kb, args.reference, llm=args.llm)
    if args.cmd == "index":
        idx = build_stem_index(engine)
        out = args.kb.parent / "kb.stems.json"
        out.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"{out}: {len(idx)} stems, {out.stat().st_size / 2**20:.1f} MB")
        return
    if args.cmd == "eval":
        sys.exit(0 if run_eval(engine, args.file) else 1)
    ans = engine.ask(args.question, args.diagnosis, args.service, args.population)
    print(json.dumps(ans, ensure_ascii=False, indent=1) if args.json else render(ans))


if __name__ == "__main__":
    main()
