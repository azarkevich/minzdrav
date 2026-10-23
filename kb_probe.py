"""Invariant probes of the expert system: thousands of inputs from the reference dictionaries, no labels, no LLM.

    python -I kb_probe.py [--kb kb_all/kb.idx.json] [--n 400] [--only icd_name,service_not_dx] [--seed 1]

Each probe checks something that must always hold, prints the pass rate and failing examples, and
writes all failures to kb/probe_report.json:

  service_not_dx   a service name from пост. №123 («Трахеоскопия») is not taken for a diagnosis
  icd_name         an МКБ-10 rubric name resolves to that rubric (protocol-covered rubrics reported apart)
  chapter_name     a protocol chapter title («ДИАГНОСТИКА И ЛЕЧЕНИЕ ПСОРИАЗА») resolves to a code of the chapter
  perturb          eval questions keep diagnosis and verdict with «?», «Пациент, …», a typo, a different case
"""
import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb  # noqa: E402

HERE = Path(__file__).resolve().parent
# МКБ names that only say «see another rubric»: they can't be recognized from the words alone
CROSS_REF_RE = re.compile(r"при болезнях,? классифицированных в других рубриках|не классифицированн\w+ в других рубриках", re.I)


def probe_service_not_dx(e, n, rnd):
    svc = [s for s in json.loads((e_ref(e) / "services.json").read_text(encoding="utf-8"))["services"]
           if s.get("diagnostic") and s.get("name") and not s.get("excluded")]
    fails = []
    for s in rnd.sample(svc, min(n, len(svc))):
        a = e.ask(s["name"])
        if a.get("diagnosis_only"):
            fails.append({"input": s["name"], "service": s["code"], "got": f"{a['diagnosis']['code']} {a['diagnosis']['name']}"})
    return min(n, len(svc)), fails


def resolved(e, text):
    dx = e.resolve_diagnosis(text)
    if not dx or dx[0]["score"] < kb.MIN_DX_SCORE:
        return None, dx
    return e._choose(dx), dx


def probe_icd_name(e, n, rnd):
    # names with a code inside («Мутилирующий артрит (L40.5+)») resolve to that code — by design
    codes = [c for c in e.icd if re.fullmatch(r"[A-T]\d{2}(\.\d)?", c) and not CROSS_REF_RE.search(e.icd[c])
             and not kb.ICD_CODE_RE.search(e.icd[c])]
    covered = [c for c in codes if c in e.covered]
    other = [c for c in codes if c not in e.covered]
    sample = rnd.sample(covered, min(n // 2, len(covered))) + rnd.sample(other, min(n - n // 2, len(other)))
    fails = []
    for c in sample:
        ch, dx = resolved(e, e.icd[c])
        got = ch["code"] if ch else None
        # the rubric itself or a code inside it / its parent (E10.9 for E10, E10 for E10.9)
        if not got or not (got == c or got.startswith(c + ".") or c.startswith(got + ".") or (len(c) == 3 and got[:3] == c)):
            fails.append({"input": e.icd[c], "expected": c, "covered": c in e.covered,
                          "got": f"{got} {e.icd.get(got, '')}" if got else None,
                          "candidates": [f"{d['code']} {d['score']:.2f}" for d in dx[:4]]})
    return len(sample), fails


def probe_chapter_name(e, n, rnd):
    seen, items = set(), []
    for s in e.kb["sections"]:
        t = s["chapter_title"] or ""
        if s["icd10_source"] in ("classification", "title") and s["icd10"] and len(s["icd10"]) <= 8 \
                and re.search(r"ДИАГНОСТИК|ЛЕЧЕНИ", t, re.I) and t not in seen:
            seen.add(t)
            dx = re.sub(r"(?i)^.*?(?:диагностик\w*|лечени\w*|медицинск\w+ наблюдени\w*)(?:\s+и\s+(?:диагностик\w*|лечени\w*))?\s+(?:пациентов\s+)?(?:с\s+|при\s+)?", "", t).strip(" .")
            if len(dx) > 3:
                items.append((dx, s["icd10"], t))
    fails = []
    sample = rnd.sample(items, min(n, len(items)))
    for dx_text, codes, title in sample:
        ch, cand = resolved(e, dx_text)
        got = ch["code"] if ch else None
        if not got or not any(got == c or got.startswith(c + ".") or c.startswith(got + ".") for c in codes):
            fails.append({"input": dx_text, "chapter": title, "expected": codes, "got": f"{got} {e.icd.get(got, '')}" if got else None})
    return len(sample), fails


def typo(word, rnd):
    i = rnd.randrange(1, len(word) - 2)
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


def probe_perturb(e, n, rnd):
    cases = []
    for f in sorted((HERE / "eval").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                cases.append(json.loads(line)["question"])
    cases = rnd.sample(cases, min(n, len(cases)))

    def key(a):
        return ((a.get("diagnosis") or {}).get("code"), a.get("mode"), a.get("verdict_level"),
                (a.get("insurance") or {}).get("verdict_level"))

    fails, total = [], 0
    for q in cases:
        base = key(e.ask(q))
        long_words = [w for w in re.findall(r"[А-Яа-яЁё]{7,}", q)]
        variants = [q.rstrip("?. ") + "?", "Пациент, " + q[0].lower() + q[1:], q.lower(), q.upper()]
        if long_words:
            w = rnd.choice(long_words)
            variants.append(q.replace(w, typo(w, rnd), 1))
        for v in variants:
            total += 1
            got = key(e.ask(v))
            if got != base:
                fails.append({"input": v, "original": q, "expected": base, "got": got})
    return total, fails


def e_ref(e):
    return e.ref


PROBES = {"service_not_dx": probe_service_not_dx, "icd_name": probe_icd_name,
          "chapter_name": probe_chapter_name, "perturb": probe_perturb}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=Path, default=Path("kb_all/kb.idx.json"))
    ap.add_argument("--reference", type=Path, default=Path("reference"))
    ap.add_argument("--n", type=int, default=400, help="inputs per probe")
    ap.add_argument("--only", help="comma-separated probe names")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--show", type=int, default=12, help="failing examples printed per probe")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    e = kb.Engine(args.kb, args.reference)
    e.ref = args.reference
    report = {}
    for name, fn in PROBES.items():
        if args.only and name not in args.only.split(","):
            continue
        t = time.time()
        total, fails = fn(e, args.n, random.Random(args.seed))
        report[name] = {"total": total, "failed": len(fails), "fails": fails}
        print(f"\n== {name}: {total - len(fails)}/{total} ok ({100 * (total - len(fails)) / max(total, 1):.0f}%), {time.time() - t:.0f} s")
        if name == "icd_name":
            cov = [f for f in fails if f["covered"]]
            print(f"   of them protocol-covered rubrics failing: {len(cov)}")
        for f in fails[:args.show]:
            print("   ", json.dumps({k: v for k, v in f.items() if k != "covered"}, ensure_ascii=False))
    out = HERE / "kb" / "probe_report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
