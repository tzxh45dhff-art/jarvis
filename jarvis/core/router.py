"""
Which model handles a request: the cheap one or the strong one.

GPT-6 Luna costs about 1/20 of Sol (Azure price list, Oct 2026) and scored 18/20
against Sol's 20/20 on our hardest tool tasks. So simple, single-step requests
start on Luna and everything else goes to Sol - and a request that starts on
Luna moves to Sol the moment anything goes wrong (see Agent.run), so a simple
request that turns out not to be simple still gets the strong model.

Deliberately conservative: when in doubt, Sol.
"""
from __future__ import annotations

import re

# Anything that drives the screen, writes, sends, builds or chains steps.
_HEAVY = re.compile(
    r"\b(open|click|type|fill|submit|solve|leetcode|edit|code|program|script|write|"
    r"create|make|build|draw|design|presentation|deck|slides?|video|blender|resolve|"
    r"davinci|photoshop|figma|email|mail|message|text|send|reply|download|install|"
    r"book|order|buy|pay|delete|remove|move|rename|organi[sz]e|fix|debug|refactor|"
    r"research|compare|analy[sz]e|summari[sz]e|plan|schedule|remind|calendar|event|"
    r"play|pause|skip|browser|safari|chrome|tab|website|page|screen|window|app|"
    r"login|log in|sign in|upload|file|folder|pdf|document)\b", re.I)
_CHAIN = re.compile(r"\b(then|after that|and also|afterwards|finally|first|next)\b|;|\band\b.*\band\b", re.I)


def pick(text: str) -> str:
    """'fast' for short single-step questions, otherwise 'strong'."""
    t = (text or "").strip()
    words = t.split()
    if not t or len(words) > 16:
        return "strong"
    if _HEAVY.search(t) or _CHAIN.search(t):
        return "strong"
    # Romanised Hindi and non-Latin scripts stay on the main model: the cheap one
    # answered Hinglish in Devanagari (the voice cannot speak it) and read
    # "kal" (tomorrow/yesterday) as today.
    if not t.isascii() or _HINGLISH.search(t):
        return "strong"
    return "fast"


# ---------------------------------------------------------------- web lookups
#
# A plain "look this up" question is cheap to answer well: web_answer does the
# searching and citing (itself on Luna), and the turn around it only calls that
# one tool and repeats the result. GPT-6 Luna does that as well as Sol at about
# 1/20 of the price (Sol's two steps cost ~Rs 1 per lookup). Everything else
# stays on the main model; this is deliberately narrower than pick().
_WEB_LOOKUP = re.compile(
    r"^\s*(?:(?:hey |hi |ok |please |can you |could you |tell me |google |search (?:the web |online )?(?:for )?|"
    r"look up |find out |do you know )*)"
    r"(?:what(?:'s| is| are| was| were)|who(?:'s| is| was| are)|when (?:is|was|did|does|will)|"
    r"where (?:is|was|can i)|how (?:much|many|old|tall|far|long|big|high) (?:is|are|was|does|do|did)|"
    r"which|is there|are there|latest|current|price of|weather|score|news|"
    r"who won|who wins)\b", re.I)
_WEB_WORDS = re.compile(
    r"\b(price|cost|rate|weather|forecast|temperature|score|scores|news|latest|current|"
    r"today|tonight|tomorrow|yesterday|now|right now|released?|release date|version|"
    r"population|capital|ceo|founder|president|prime minister|stock|share price|"
    r"worth|net worth|age|height|distance|time in|exchange rate|result|results|winner|"
    r"trending|top|best)\b", re.I)
# Words the cheaper model has been seen to mishandle: Romanised Hindi (it answered
# in Devanagari, and read "kal" as today), and anything not plain ASCII.
_HINGLISH = re.compile(
    r"\b(kal|aaj|abhi|kitna|kitni|kitne|kya|kaun|kaha|kab|kaise|batao|bata|hai|hain|"
    r"ka|ki|ke|mein|me|mera|meri|mujhe|bhai|yaar|wala|wali)\b", re.I)
# Things about this Mac or the user, which a web lookup cannot answer.
_PERSONAL = re.compile(
    r"\b(my|mine|me|i|our|this mac|this computer|battery|wifi|screen|clipboard|calendar|"
    r"reminders?|contacts?|downloads|desktop|inbox)\b", re.I)


_SUM = re.compile(r"\d\s*(?:times|plus|minus|divided by|multiplied by|[x*/+^%×÷-])\s*\d", re.I)


def pick_web(text: str) -> bool:
    """True for a short, plain, English, factual lookup: the web_answer kind."""
    t = (text or "").strip()
    words = t.split()
    if not t or len(words) > 18 or len(words) < 2:
        return False
    if not t.isascii() or _HINGLISH.search(t):
        return False
    if _HEAVY.search(t) or _CHAIN.search(t) or _PERSONAL.search(t) or _SUM.search(t):
        return False
    if _WEB_LOOKUP.search(t) or _WEB_WORDS.search(t):
        return True
    return False
