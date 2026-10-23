"""Questions and ratings of the website → a Google Sheet (Apps Script web app, see web/sheet_logger.gs),
with personal data masked before anything is stored.

    KB_SHEET_URL    the web app URL (https://script.google.com/macros/s/…/exec); unset = off
    KB_SHEET_TOKEN  shared secret, the same as TOKEN in the script

Sending is done by one background thread so a slow Google never delays an answer; a row that
fails three times is printed as `SHEET_FAIL {json}` (stdout is kept by the host's logs).
"""
import json
import os
import queue
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

MINSK = timezone(timedelta(hours=3))

# Order matters: the specific numbers first, then whatever long digit run is left.
MASKS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    # personal number of a Belarusian passport: 3010180A001PB4
    (re.compile(r"\b\d{7}[A-ZА-Я]\d{3}[A-ZА-Я]{2}\d\b", re.I), "[личный номер]"),
    # passport series + number: MP1234567, КН 1234567
    (re.compile(r"\b[A-ZА-Я]{2}\s?\d{7}\b"), "[паспорт]"),
    (re.compile(r"(?<!\w)(?:\+?375|8\s?0)[\s(-]*\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), "[телефон]"),
    (re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}\b"), "[дата]"),
    # «полис № 123-456», «договор 12345/2024», «карта №7788»
    (re.compile(r"(?i)\b(полис\w*|договор\w*|карт\w*|страхов\w*\s+свидетельств\w*|номер\w*|№)\s*[:№#]?\s*[\w/-]*\d[\w/-]*"), r"\1 [номер]"),
    # surname + initials: «Иванов И.И.», «И. И. Иванов», «Иванова И.»
    (re.compile(r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?(?:[А-ЯЁ]\b\.?)?"), "[ФИО]"),
    (re.compile(r"\b[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.\s?)?[А-ЯЁ][а-яё]+"), "[ФИО]"),
    # a patronymic with the name (and surname) before it: «Иван Петрович», «Иванов Иван Петрович»
    (re.compile(r"\b(?:[А-ЯЁ][а-яё]+\s+){1,2}[А-ЯЁ][а-яё]+(?:ович|евич|ич|овна|евна|ична|инична)\b"), "[ФИО]"),
    # «пациент Иванов», «застрахованная Петрова»: a capitalised word after who-words
    (re.compile(r"(?i)\b(пациент\w*|застрахованн\w*|больн(?:ой|ая|ого|ую)|клиент\w*|гр\.|гражданин\w*)\s+(?-i:[А-ЯЁ][а-яё]+\b)(?:\s+(?-i:[А-ЯЁ][а-яё]+\b))?(?!\s+[а-яё])"), r"\1 [ФИО]"),
    (re.compile(r"\d{6,}"), "[число]"),
]


def mask(text) -> str:
    """Personal data → placeholders. Medical words stay: eponyms («болезнь Аддисона», «по Нечипоренко»)
    have no initials or patronymic, МКБ-10 codes and doses are short."""
    if not text:
        return text or ""
    s = str(text)
    for rx, repl in MASKS:
        s = rx.sub(repl, s)
    return s


class SheetLog:
    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = url if url is not None else os.environ.get("KB_SHEET_URL")
        self.token = token if token is not None else os.environ.get("KB_SHEET_TOKEN", "")
        self.q: queue.Queue = queue.Queue(maxsize=1000)
        if self.url:
            threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def now() -> str:
        return datetime.now(MINSK).strftime("%Y-%m-%d %H:%M:%S")

    def send(self, sheet: str, row: dict):
        """`row`: {column title: value}; the script adds missing columns to the sheet's header."""
        if self.url:
            # a cell starting with = + - @ would be taken by Sheets as a formula
            row = {k: "'" + v if isinstance(v, str) and v[:1] and v[0] in "=+-@" else v for k, v in row.items()}
            try:
                self.q.put_nowait({"token": self.token, "sheet": sheet, "row": row})
            except queue.Full:
                print("SHEET_FAIL " + json.dumps(row, ensure_ascii=False), flush=True)

    def _run(self):
        while True:
            item = self.q.get()
            data = json.dumps(item, ensure_ascii=False).encode("utf-8")
            for attempt in range(3):
                try:
                    req = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=20) as r:  # Apps Script answers via a 302 redirect
                        body = r.read().decode("utf-8", "replace")
                    if '"ok"' in body:
                        break
                    raise RuntimeError(body[:200])
                except Exception as ex:  # noqa: BLE001
                    if attempt == 2:
                        print(f"SHEET_FAIL {type(ex).__name__}: {ex} " + json.dumps(item["row"], ensure_ascii=False), flush=True)
                    else:
                        time.sleep(2 * (attempt + 1))
