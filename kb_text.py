"""Text normalization shared by the knowledge-base builder (kb_build.py) and the query
side (kb.py): abbreviation expansion, a small Russian stemmer, fuzzy stem matching.

Standard library only. Matching works on "stems" — lower-cased words with the
inflectional ending cut off — so «гормонов крови» and «гормоны в крови» compare equal.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

WORD_RE = re.compile(r"[A-Za-zА-ЯЁа-яё0-9]+(?:[-.][A-Za-zА-ЯЁа-яё0-9]+)*")
# Longest first. Cut only if at least 3 letters remain.
ENDINGS = sorted("""
иями ями ами ией иях ях ах ием ем ом ой ей ий ый ая яя ое ее ые ие ую юю ого его ому ему ими ыми ых их ым им
ов ев ам ям ия ии ию иею ию ья ье ьи ью ьям ьями ьях
ы и а я о е у ю ь й
""".split(), key=len, reverse=True)
STOP = {"и", "в", "во", "с", "со", "по", "при", "на", "к", "о", "об", "от", "до", "для", "или", "из",
        "у", "за", "под", "над", "без", "не", "а", "также", "числе", "том", "т", "ч", "др", "его", "их",
        "мл", "мг", "г", "мин", "раз", "год", "лет"}
# Method words that don't distinguish services: «исследование ТТГ» = «определение ТТГ».
GENERIC = {"исследован", "определен", "анализ", "уровн", "содержан", "оценк", "проведен", "выполнен",
           "метод", "методик", "показател", "концентрац", "изучен", "контрол", "диагностик", "проб",
           "тест", "обследован", "медицинск", "услуг", "лабораторн", "клиническ",
           "сыворотк", "плазм", "случа", "показан", "необходимост", "наличи", "налич",
           # verbs in questions: «можно ли определить / сделать / сдать …»
           "определит", "провест", "сделат", "сдат", "выполнит", "назначит", "исследоват", "измерит"}

# Words that name a method — a list member without one inherits the method of the first
# member: «КТ органов шеи, средостения» → «КТ средостения».
METHOD = {"исследован", "анализ", "определен", "тест", "проб", "рентгенограф", "рентгеноскоп", "томограф",
          "сцинтиграф", "ультразвуков", "биопс", "мониторирован", "электрокардиограм", "электрокардиограф",
          "эхокардиограф", "денситометр", "рентгеноденситометр", "прием", "консультац", "осмотр", "эндоскоп",
          "колоноскоп", "гастроскоп", "пункц", "посев", "допплерограф", "ангиограф", "урограф", "периметр",
          "офтальмоскоп", "спирометр", "полисомнограф", "эзофагогастродуоденоскоп", "фиброгастродуоденоскоп",
          "подсч", "измерен", "мазок", "цитологическ", "гистологическ", "микроскоп", "электроэнцефалограф",
          "электромиограф", "электронейромиограф", "магнитно", "компьютерн", "радиоизотопн", "маммограф",
          "холтеровск", "суточн", "исследовани", "скрининг", "осмотр", "оценк", "расчет", "выявлен", "типирован"}


@lru_cache(maxsize=None)
def stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    if len(w) <= 3 or not re.fullmatch(r"[а-я]+", w):
        return w
    for e in ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            w = w[: -len(e)]
            break
    if len(w) >= 5 and w.endswith("ок"):  # fleeting vowel: «белок» / «белка» → «белк»
        w = w[:-2] + "к"
    return w


def load_synonyms() -> dict:
    """Word stem → stems of its canonical phrase (data/synonyms.json)."""
    data = json.loads((DATA_DIR / "synonyms.json").read_text(encoding="utf-8"))
    out = {}
    for k, v in data.items():
        if not k.startswith("_"):
            out[stem(k)] = [stem(w) for w in WORD_RE.findall(v)]
    return out


def load_abbreviations() -> dict:
    data = json.loads((DATA_DIR / "abbreviations.json").read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


class Normalizer:
    """Expands abbreviations (curated dictionary first, then a document's glossary) and
    turns text into stems."""

    def __init__(self, curated: dict | None = None):
        self.curated = curated if curated is not None else load_abbreviations()
        self.synonyms = load_synonyms()
        self._lower = {}
        for k, v in self.curated.items():
            self._lower.setdefault(k.lower().replace("ё", "е"), v)

    def expand_token(self, tok: str, glossary: dict | None = None) -> str | None:
        if tok in self.curated:
            return self.curated[tok]
        if glossary and tok in glossary and len(tok) >= 2 and tok[:1].isupper():
            return glossary[tok]
        # «ттг» typed in lower case; only for tokens that look like abbreviations
        low = tok.lower().replace("ё", "е")
        if len(tok) <= 6 and low in self._lower and (tok.isupper() or tok.islower()):
            return self._lower[low]
        return None

    def expand(self, text: str, glossary: dict | None = None, keep_abbr: bool = False) -> str:
        """«ОАК; БАК: АЛТ» → «общий клинический анализ крови; биохимический анализ крови: …»
        (keep_abbr=True keeps the abbreviation in front of its expansion)."""
        def repl(m):
            tok = m.group(0)
            full = self.expand_token(tok, glossary)
            if full is None and "-" in tok:  # «ХМ-ЭКГ» not known as a whole: expand parts
                parts = [self.expand_token(p, glossary) for p in tok.split("-")]
                if any(parts):
                    full = " ".join(p or q for p, q in zip(parts, tok.split("-")))
            if full is None and tok.lower().startswith("св.") and len(tok) > 3:
                inner = self.expand_token(tok[3:], glossary)
                full = f"свободный {inner}" if inner else None
            if not full:
                return tok
            return f"{tok} {full}" if keep_abbr else full
        return WORD_RE.sub(repl, text)

    def stems(self, text: str, glossary: dict | None = None) -> list[str]:
        text = re.sub(r"(?<![А-Яа-яЁё])св\.\s*", "свободный ", text)  # «св. Т4», «св.Т4»
        out = []
        for w in WORD_RE.findall(self.expand(text, glossary)):
            for part in re.split(r"[-.]", w) if not re.search(r"\d", w) else [w]:
                s = stem(part)
                if s and s not in STOP and not (len(s) == 1 and not s.isdigit()):
                    out.extend(self.synonyms.get(s, [s]))
        return out


def close(a: str, b: str) -> bool:
    """Equal stems, or one typo in stems of 6+ letters: a wrong, missing or extra letter, or two
    neighbours swapped — «ничипоренк» ~ «нечипоренк», «тереоид» ~ «тиреоид», «посриаз» ~ «псориаз»."""
    if a == b:
        return True
    if min(len(a), len(b)) < 6 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    if len(a) > len(b):
        a, b = b, a
    for i in range(len(b)):
        if b[:i] + b[i + 1:] == a:
            return True
    return False


def prefix_match(a: str, b: str) -> bool:
    """«щитовидн» vs «щитовид», «гормон» vs «гормональн»: same root, different suffix."""
    n = min(len(a), len(b))
    if n >= 5 and a[:n] == b[:n]:
        return True
    # «сифилис» vs «сифилитическ»: a long common root covering most of the shorter stem
    common = 0
    while common < n and a[common] == b[common]:
        common += 1
    return common >= 6 and common >= 0.75 * n


def contains(haystack: list[str], needle: str) -> bool:
    return any(close(needle, h) or prefix_match(needle, h) for h in haystack)


def trigram_sim(a: str, b: str) -> float:
    """Character trigram Jaccard on normalized strings — for diagnosis names with typos."""
    def grams(s):
        s = " " + re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip() + " "
        return {s[i:i + 3] for i in range(len(s) - 2)}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0
