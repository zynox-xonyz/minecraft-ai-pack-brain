"""Pack-Brain agent v4 = v3 pipeline with passage retrieval (packbrain/passages.py), a secondary question type for mixed
questions and vanilla-first usage lists. Implemented as a variant of agent3 so that v3 stays unchanged for comparison."""
from . import agent3


def ask_stream(question, history=None, brevity="overlay", context=None, game_lang=None):
    return agent3.ask_stream(question, history=history, brevity=brevity, context=context, game_lang=game_lang, variant="v4")


def ask(question, model=None, history=None, context=None, game_lang=None):
    return agent3.ask(question, history=history, context=context, game_lang=game_lang, variant="v4")


explain_stream = agent3.explain_stream
