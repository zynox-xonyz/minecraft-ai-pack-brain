"""v5-lite: a switch in front of the two measured pipelines.

- v3 for simple single-thing questions (rule fast path: same accuracy as v1, faster) and for the features only v3 has
  (calculators, "what am I missing", projects/notes, structure lists);
- v1 (free tool calling) for everything else, i.e. open "how does X work / what is mod Y" questions, where v1 measured
  as good as v3 but faster (6.6 s vs 10.8 s median).
"""
import re

from . import agent3, streaming
from . import tools as T

V3_ONLY = re.compile(
    r"was fehlt|fehlen mir|hab(e)? ich genug|reicht|genug|what am i missing|enough|"                      # missing / stock
    r"schüsse|schuss|schläge|hits? to kill|shots?|wie (oft|viele (male|hits))|stress|\bsu\b|wasserr(a|ä)d|"  # calculators
    r"chance|wahrscheinlich|truhen|kisten|chests|"
    r"projekt|project|notiz|\bunser|\bunsere|\bwir\b|our |"                                                # team notes / projects
    r"struktur|structure|dungeon|tempel|temple|loot", re.I)


def use_v3(question, history=None):
    if V3_ONLY.search(question or ""):
        return True
    if history:
        return False
    return bool(agent3.fast_route(question, T.resolve_entities(question)))


def ask_stream(question, history=None, brevity="overlay", context=None, game_lang=None):
    if use_v3(question, history):
        yield from agent3.ask_stream(question, history=history, brevity=brevity, context=context, game_lang=game_lang)
    else:
        for ev in streaming.ask_stream(question, history=history, brevity=brevity, context=context):
            if ev["type"] == "final":
                ev = {**ev, "trace": [("v5lite", "v1")] + list(ev.get("trace") or [])}
            yield ev


def ask(question, model=None, history=None, context=None, game_lang=None):
    final = None
    for e in ask_stream(question, history=history, context=context, game_lang=game_lang):
        if e["type"] == "final":
            final = e
    return final
