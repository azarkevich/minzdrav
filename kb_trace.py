"""Trace of one question through the expert system: which branch parsed it, what the rules
extracted, which diagnosis candidates were considered, every LLM call (prompt and answer),
problems and timings. Engine.ask puts it in the answer as `debug`; the website shows it in a
collapsed «Отладка» block, `kb.py ask --debug` prints it.

The current trace lives in a ContextVar, so kb_llm.call() can record into it without the
trace being passed through every function.
"""
import contextlib
import contextvars
import time

CURRENT: contextvars.ContextVar = contextvars.ContextVar("kb_trace", default=None)


class Trace:
    def __init__(self, question=None):
        self.t0 = time.time()
        self.data = {"question": question, "route": [], "parse": [], "diagnosis": {}, "service": {},
                     "llm": [], "llm_status": "", "problems": [], "timing": {}}
        self.cache = {}  # per-question results shared between steps (one LLM parse per question)
        self.calls = {}  # purpose → number of LLM calls

    # -- recording
    def route(self, text: str):
        self.data["route"].append(text)

    def parse(self, text: str):
        self.data["parse"].append(text)

    def problem(self, text: str):
        if text not in self.data["problems"]:
            self.data["problems"].append(text)

    def set(self, section: str, **kw):
        self.data[section].update(kw)

    def llm_call(self, rec: dict):
        self.data["llm"].append(rec)
        self.calls[rec.get("purpose", "")] = self.calls.get(rec.get("purpose", ""), 0) + 1

    def llm_note(self, text: str):
        """Why the LLM was (not) used — shown when no call was made."""
        self.data["llm_status"] = text

    @contextlib.contextmanager
    def timer(self, name: str):
        t = time.time()
        try:
            yield
        finally:
            self.data["timing"][name] = round(self.data["timing"].get(name, 0) + (time.time() - t) * 1000)

    def finish(self) -> dict:
        self.data["timing"]["total"] = round((time.time() - self.t0) * 1000)
        return self.data


def current() -> "Trace":
    """The trace of the question being answered; outside Engine.ask a throwaway one, so callers
    never need to check."""
    return CURRENT.get() or Trace()
