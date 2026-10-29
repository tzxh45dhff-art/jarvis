"""
The agent loop: observe -> reason -> act -> verify.

Complexity is routed, not fixed. A factual question does one forward pass with
no tools. A research-and-write task plans, executes many tool calls, and then
verifies its own output before reporting.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..config import CONFIG
from ..memory.store import get_store
from ..tools.registry import REGISTRY, ToolResult
from . import prompts
from .llm import get_backend
from .protocol import ToolCall

log = logging.getLogger("jarvis.agent")

# Requests that clearly need multi-step work / deeper reasoning.
_COMPLEX = re.compile(
    r"\b(research|compare|analy[sz]e|investigate|write (a|an|me)?\s*(paper|report|"
    r"review|essay|doc)|literature|build|implement|refactor|debug|fix|organi[sz]e|"
    r"plan|design|migrate|audit|benchmark|summari[sz]e .{0,30}(sources|papers)|"
    r"step by step|figure out|work out why|troubleshoot)\b", re.I)
# Requests to *do* something, as opposed to ask something. These are the ones a
# model can "complete" with a plausible substitute - a file on the Desktop for
# "solve it on LeetCode" - so their answer is held until it has been checked.
_ACTION = re.compile(
    r"\b(solve|submit|send|post|publish|book|buy|order|fill|upload|download|"
    r"install|create|make|write|edit|change|fix|delete|remove|move|rename|"
    r"play|schedule|add|reply|apply|sign ?up|register|log ?in|run|type|click|"
    r"save|close|quit|turn (on|off)|set|enable|disable|open .{1,40} and)\b", re.I)
_TRIVIAL = re.compile(
    r"^\s*(hi|hey|hello|yo|thanks|thank you|ok|okay|cool|nice|got it|"
    r"good (morning|evening|night)|bye)\b[\s!.?]*$", re.I)
# Observing the world again is how you detect a change, so these tools are
# allowed to repeat — but only while the answer keeps changing. Two identical
# results in a row means nothing moved, and a third call will not move it.
_RECHECK_TOOLS = {
    "see_screen", "screenshot", "find_on_screen", "screen_info", "check_job",
    "system_status", "now_playing", "read_clipboard", "list_apps",
    "browser_read", "browser_find", "inspect_ui", "ui_elements", "file_info", "list_dir",
}
MAX_IDENTICAL_OBSERVATIONS = 2
# A job that has made this many tool calls without a single confirmed result is
# going in circles: measured, a "resume the episode" request made 32 calls over
# three minutes, reopening the same page and re-running the same search because
# it could not confirm playback. Nudge once, then stop and report.
# Tools filed as reads that still change what is on screen; the final check
# must not use them.
_NOT_LOOKS = {"browser_open", "open_url", "open_file", "show_steps", "find_tools",
              "web_answer", "web_search", "fetch_page", "fetch_many", "news_search",
              "youtube_tool", "directions", "wikipedia"}
STALL_NUDGE = 12
STALL_STOP = 20
STALL_MESSAGE = (
    "You have made {n} tool calls without confirming progress. Do not repeat an "
    "approach that already ran, and do not reload or reopen a page you have "
    "already opened. Either try one genuinely different method, or stop: say "
    "plainly what you did, what you could not confirm and why, and what you need "
    "from me."
)

_ARITHMETIC = re.compile(r"^[\s\d+\-*/^%.()x×÷,]+\??$")
# "what is 25 times 8" is arithmetic too; spawning a Python subprocess for it
# costs more than the answer is worth.
_MATH_WORDS = (r"times|plus|minus|multiplied by|divided by|over|squared|cubed|"
               r"percent of")
_NL_ARITHMETIC = re.compile(
    r"^\s*(?:what(?:'s| is)|whats|calculate|compute)?\s*"
    r"[\d.,]+\s*(?:" + _MATH_WORDS + r"|[+\-*/^%x×÷])\s*[\d.,]+"
    r"(?:\s*(?:" + _MATH_WORDS + r"|[+\-*/^%x×÷])\s*[\d.,]+)*\s*\??\s*$", re.I)



def _for_model(obj, limit: int = 12000) -> str:
    """
    A tool result as JSON that fits the budget *and is still valid JSON*.

    Results used to be cut with [:12000] after serialising, which chopped a long
    page or listing off mid-string: the model got a broken fragment and no hint
    that anything was missing. Now the longest strings and lists are trimmed
    first, each with a marker saying how much was dropped, so the model knows
    to page for more.
    """
    s = json.dumps(obj, default=str, ensure_ascii=False)
    if len(s) <= limit:
        return s
    import copy
    data = copy.deepcopy(obj)

    def shrink(node, budget):
        if isinstance(node, str) and len(node) > budget:
            return node[:budget] + f" …[+{len(node) - budget} chars cut]"
        if isinstance(node, list) and len(node) > 8 and budget < 4000:
            keep = node[:8]
            return [shrink(x, budget) for x in keep] + [f"…[+{len(node) - 8} more items cut]"]
        if isinstance(node, list):
            return [shrink(x, budget) for x in node]
        if isinstance(node, dict):
            return {k: shrink(v, budget) for k, v in node.items()}
        return node

    for budget in (6000, 3000, 1500, 600, 250):
        data = shrink(data, budget)
        s = json.dumps(data, default=str, ensure_ascii=False)
        if len(s) <= limit:
            return s
    return json.dumps({"truncated": True, "start": s[:limit - 200]}, ensure_ascii=False)

def _plan_lines(text: str) -> list[str]:
    """Numbered plan lines at the start of a reply ("1. ...", "2) ...")."""
    lines = [l.strip() for l in (text or "").splitlines() if re.match(r"\s*\d+[.)]\s+\S", l)]
    return lines[:7]


def _blender_model() -> str:
    """The deployment for Blender work: `blender_deployment` in ~/.jarvis/azure.json
    (or JARVIS_AZURE_BLENDER_DEPLOYMENT). Unset means Blender jobs stay on the
    default model. The owner points it at a stronger, pricier model (GPT-6 Astra)
    because character modelling is where small models fall apart."""
    try:
        from .azure import settings
        return settings().get("blender_deployment") or ""
    except Exception:
        return ""
_3D_WORDS = re.compile(r"\b(3d|model|modell?ing|render|scene|mesh|object|material|texture|shader|"
                       r"animat\w*|rig|keyframe|modifier|sculpt|cube|sphere|camera|light(ing)?|"
                       r"geometry nodes?|uv|bevel|extrude)\b", re.I)


def _is_blender_job(text: str) -> bool:
    """The request names Blender, or Blender is in front and the request is 3D work."""
    if re.search(r"\bblender\b", text or "", re.I):
        return True
    if not _3D_WORDS.search(text or ""):
        return False
    try:
        from ..context import front_window
        return front_window()[0] == "Blender"
    except Exception:
        return False


# Tools whose verified result fully answers a one-action request.
_SELF_REPLY_TOOLS = {"play_music", "control_playback", "set_volume"}


@dataclass
class Event:
    """Everything the UI needs to render progress."""
    type: str                       # text|thought|tool_start|tool_end|plan|status|done|error
    text: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    result: Any = None
    ok: bool = True
    meta: dict = field(default_factory=dict)


@dataclass
class TurnStats:
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    tps: float = 0.0
    ttft: float = 0.0
    elapsed: float = 0.0
    thought_used: bool = False


class Agent:
    def __init__(self, backend=None, session_id: str = ""):
        self.llm = backend or get_backend()
        self.messages: list[dict] = []
        self.session_id = session_id or f"s{int(time.time())}"
        REGISTRY.session_id = self.session_id
        self.stop_event = threading.Event()
        self.last_stats = TurnStats()
        self._explicit_memory_this_turn = False
        self._failed_calls: dict[str, dict] = {}
        self._done_calls: dict[str, str] = {}
        self._repeat_counts: dict[str, int] = {}
        self._consecutive_failures = 0
        self._short_circuits = 0
        self._turn_index = 0
        # The user message of the request in progress: trimming never drops it.
        self._turn_msg: dict | None = None
        # Conversation: a light follow-up is offered now and then, never in a row.
        self.follow_ups = os.environ.get("JARVIS_FOLLOWUPS", "1") != "0"
        self._last_followup_turn = -99
        from ..memory.profile import Learner
        self._learner = Learner(self.llm)

    # -- context -----------------------------------------------------------
    def _relevant_memories(self, text: str) -> str:
        try:
            hits = get_store().search(text, k=CONFIG.memory.top_k)
        except Exception as e:
            log.warning("memory retrieval failed: %s", e)
            return ""
        return "\n".join(f"- {m.line()}" for m in hits)

    @staticmethod
    def _approx_tokens(messages: list[dict]) -> int:
        """Cheap size estimate; avoids tokenising the whole history each turn."""
        n = 0
        for m in messages:
            n += len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
        return int(n / 3.6)

    def _trim(self) -> None:
        """
        Drop the oldest exchanges, but only when the history genuinely exceeds
        the budget - and then drop a large chunk at once.

        Trimming rewrites the message list, which invalidates the cached KV
        prefix. Trimming a little on every turn would therefore force a full
        re-prefill every turn; measured, that took first-token latency from
        0.12 s back up to 8.6 s. So this fires rarely and cuts deep.
        """
        budget = CONFIG.llm.context_budget
        if not self.messages or self._approx_tokens(self.messages) <= budget:
            return
        head = self.messages[:1] if self.messages[0]["role"] == "system" else []
        body = self.messages[len(head):]
        # cut back to roughly half the budget so this does not re-fire next turn
        target = budget // 2
        keep_from = len(body)
        running = 0
        for i in range(len(body) - 1, -1, -1):
            running += self._approx_tokens([body[i]])
            keep_from = i
            if running >= target:
                break
        # never start the retained window with an orphaned tool result, and never
        # split an assistant tool-call from the results that answer it
        while keep_from < len(body) and body[keep_from].get("role") == "tool":
            keep_from += 1
        if keep_from >= len(body):
            # The newest step alone is over the target (a batch of big tool
            # results): keep it whole, from the message that asked for it. The
            # old fallback kept the last two messages, which could be two tool
            # results with their call cut off.
            keep_from = next((i for i in range(len(body) - 1, -1, -1)
                              if body[i].get("role") != "tool"), 0)
        tail = body[keep_from:]
        # The request being worked on is never trimmed away mid-job. Trimming
        # runs after every step; a job with large tool results used to lose the
        # user's own request and carry on without knowing what it was asked.
        req = self._turn_msg
        pinned = [req] if req is not None and any(m is req for m in body[:keep_from]) else []
        dropped = len(body) - len(tail) - len(pinned)
        if dropped <= 0:
            return
        # The cache is being rebuilt anyway, so this is the moment to shrink
        # what earlier requests left behind: whole pages and listings that were
        # read for a question already answered. Every later step re-reads them.
        cur = next((i for i, m in enumerate(tail) if m is req), None)
        for i, m in enumerate(tail):
            if cur is not None and i >= cur:
                break
            c = m.get("content")
            if m.get("role") == "tool" and isinstance(c, str) and len(c) > 1800:
                tail[i] = dict(m, content=c[:1500] + f" …[+{len(c) - 1500} chars from an "
                                                       "earlier request cut]")
        self.messages = head + [{
            "role": "user",
            "content": f"[{dropped} earlier messages from this session were "
                       f"dropped to stay within context. Ask if you need detail.]",
        }] + pinned + tail
        log.info("trimmed %d messages (~%d tokens remain)", dropped,
                 self._approx_tokens(self.messages))

    @staticmethod
    def _sig_of(name: str, args: dict) -> str:
        import hashlib
        raw = name + "\x00" + json.dumps(args, sort_keys=True, default=str)
        return hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()

    @staticmethod
    def _should_think(text: str) -> bool:
        if _TRIVIAL.match(text) or _ARITHMETIC.match(text.strip()):
            return False
        if _COMPLEX.search(text):
            return True
        return len(text.split()) > 28

    def _wants_plan(self, text: str) -> bool:
        """Several things to do, or one clearly involved job - and a model for
        which the extra round trip is cheap (the local model's KV cache would
        pay a full re-prefill for it)."""
        from .llm import MLXGemmaBackend
        if isinstance(self.llm, MLXGemmaBackend) or not self._needs_tools(text):
            return False
        verbs = {m.group(0).lower() for m in _ACTION.finditer(text)}
        chained = re.search(r"\b(then|after that|and also|afterwards|finally|"
                            r"once (that|it)'?s? done|while)\b|;", text, re.I)
        return len(verbs) >= 2 or bool(chained) or bool(_COMPLEX.search(text))

    @staticmethod
    def _needs_tools(text: str) -> bool:
        t = text.strip()
        if _TRIVIAL.match(t):
            return False
        if _ARITHMETIC.match(t) or _NL_ARITHMETIC.match(t):
            return False
        return True

    def _wants_followup(self, text: str) -> bool:
        """
        Whether this reply may carry a friendly follow-up.

        Decided here, in code, rather than left to the model: told only "be
        curious", a model asks on every reply, which is the interrogation the
        user asked us not to do. At most once per few turns, only for light
        conversation, never in the middle of a job.
        """
        if not self.follow_ups:
            return False
        if self._turn_index - self._last_followup_turn < 5:
            return False
        t = text.strip()
        if not t or len(t.split()) > 30 or _ACTION.search(t):
            return False
        if re.search(r"\b(error|fail|broken|urgent|asap|hurry|quick|stuck|wrong|"
                     r"not working|bug|crash)\b", t, re.I):
            return False
        return random.random() < 0.5

    # -- main loop ---------------------------------------------------------
    def run(self, user_input: str, voice: bool = False,
            max_steps: int | None = None) -> Iterator[Event]:
        t_start = time.time()
        self.stop_event.clear()
        stats = TurnStats()
        max_steps = max_steps or CONFIG.max_agent_steps
        self._turn_index += 1
        self._turn_msg = None
        # Read before this turn adds anything: a bare "continue" / "next" just
        # after a question or a stopped job answers that, not the music player.
        awaiting = self._awaiting_reply()

        # The system prompt must stay byte-identical across turns: it is the
        # cached KV prefix. Anything dynamic (memories, voice mode) rides along
        # with the user turn instead.
        if not self.messages or self.messages[0]["role"] != "system":
            self.messages.insert(0, {"role": "system",
                                     "content": prompts.system_prompt()})

        # A few exact questions the Mac answers in milliseconds - no model.
        if os.environ.get("JARVIS_FASTPATH", "1") != "0":
            from . import fastpath
            fp = fastpath.answer(user_input)
            if fp:
                text, kind = fp
                self.messages.append({"role": "user", "content": user_input})
                self.messages.append({"role": "assistant", "content": text})
                self._trim()
                yield Event("text", text=text)
                yield Event("done", text=text, meta={"fastpath": kind, "steps": 0,
                                                     "elapsed": round(time.time() - t_start, 3)})
                return

        # A saved routine named outright ("study mode") expands into its
        # steps, run in order as one request.
        try:
            from ..memory.profile import match_routine
            hit = match_routine(user_input)
        except Exception:
            hit = None
        if hit:
            name, steps = hit
            from . import fastpath as _fp
            quick = [(_fp.answer(st), _fp.action(st)) for st in steps]
            if all(a or b for a, b in quick):
                # Every step is one the Mac answers or does directly: no model.
                replies = []
                for (ans, act) in quick:
                    if ans:
                        replies.append(ans[0])
                        continue
                    tname, targs = act
                    yield Event("tool_start", tool=tname, args=targs)
                    r = REGISTRY.execute(tname, targs)
                    log.info("tool %s %s -> %s [fastpath]", tname,
                             json.dumps(targs, default=str)[:200],
                             r.display if r.ok else "FAILED: " + (r.error or ""))
                    yield Event("tool_end", tool=tname, args=targs, result=r, ok=r.ok,
                                text=r.display or (r.error or ""))
                    replies.append(_fp.reply(tname, targs, r.content) if r.ok
                                   else f"Couldn't: {r.error}")
                text = " ".join(replies)
                self.messages.append({"role": "user", "content": user_input})
                self.messages.append({"role": "assistant", "content": text})
                self._trim()
                yield Event("text", text=text)
                yield Event("done", text=text, meta={"routine": name, "steps": 0,
                                                     "elapsed": round(time.time() - t_start, 3)})
                return
            # Just the steps, as if said in one breath. Naming it a "routine"
            # made the model go and look the routine up (and save it again),
            # and "in order, then..." cost a planning round trip.
            user_input = "; ".join(steps) + "."

        # Unambiguous media commands ("play", "pause", "next", "volume up",
        # "play X on Spotify") run their tool directly - same safety checks,
        # no model round trip. A failure falls through to the model below with
        # a note, so the fast path never leaves a request worse off.
        fast_note = ""
        if os.environ.get("JARVIS_FASTPATH", "1") != "0":
            from . import fastpath
            act = fastpath.action(user_input)
            if act and awaiting and act[0] in ("play_music", "control_playback") \
                    and not act[1].get("query"):
                # "continue" after "shall I continue?" (or after a stopped
                # job) means the job, not the paused song: the model decides.
                act = None
            if act:
                name, args = act
                yield Event("tool_start", tool=name, args=args)
                r = REGISTRY.execute(name, args)
                log.info("tool %s %s -> %s [fastpath]", name,
                         json.dumps(args, default=str)[:200],
                         r.display if r.ok else "FAILED: " + (r.error or ""))
                yield Event("tool_end", tool=name, args=args, result=r, ok=r.ok,
                            text=r.display or (r.error or ""))
                if r.ok:
                    self._observe(name, args, r.content)
                    text = fastpath.reply(name, args, r.content)
                    self.messages.append({"role": "user", "content": user_input})
                    self.messages.append({"role": "assistant", "content": text})
                    self._trim()
                    yield Event("text", text=text)
                    yield Event("done", text=text, meta={
                        "fastpath": name, "steps": 0, "tool_calls": 1,
                        "elapsed": round(time.time() - t_start, 3)})
                    return
                fast_note = (f"[Already tried {name}({json.dumps(args)}) directly and it "
                             f"failed: {(r.error or '')[:300]}. Do not repeat that exact "
                             f"call; try another way.]")

        # Cheap model for short single-step requests (Azure only); it hands
        # over to the strong model at the first sign of trouble, below.
        self._fast_turn = False
        if hasattr(self.llm, "turn_model") and os.environ.get("JARVIS_ROUTER", "1") != "0":
            try:
                from . import router
                from .azure import settings as _az_settings
                cfg = _az_settings()
                fast = cfg.get("fast_deployment")
                # Plain web lookups run on the cheap model when `web_deployment`
                # is set in azure.json / JARVIS_AZURE_WEB_DEPLOYMENT.
                web = cfg.get("web_deployment")
                if fast and router.pick(user_input) == "fast":
                    self.llm.turn_model = fast
                    self._fast_turn = True
                elif web and router.pick_web(user_input):
                    self.llm.turn_model = web
                    self._fast_turn = True
                else:
                    self.llm.turn_model = None
            except Exception:
                self.llm.turn_model = None

        # Blender jobs go to GPT-6 Astra (the owner's choice, 19 Oct 2026).
        blender_job = _is_blender_job(user_input)
        if hasattr(self.llm, "turn_model") and blender_job and _blender_model():
            self.llm.turn_model = _blender_model()
            yield Event("status", text=f"using {self.llm.turn_model} for this Blender job")
        # Tools the request plainly needs are loaded before the first step.
        try:
            from ..tools.toolsearch import preload_for
            preload_for(user_input + (" blender" if blender_job else ""))
        except Exception:
            pass

        # What is on screen and what memory says are independent lookups
        # (~0.3 s each): run them side by side.
        import threading as _th
        _ctx: dict = {}
        _ct = _th.Thread(target=lambda: _ctx.__setitem__("v", prompts.turn_context()),
                         daemon=True)
        _ct.start()
        mem = self._relevant_memories(user_input)
        _ct.join(4.0)
        turn = user_input
        preamble = [_ctx.get("v") or f"[Current time: {time.strftime('%H:%M on %A %d %B %Y')}]"]
        if mem:
            preamble.append("[Relevant long-term memory:\n" + mem + "\n]")
        if voice:
            preamble.append("[" + prompts.VOICE_NOTE.strip() + "]")
        if self._wants_followup(user_input):
            preamble.append(prompts.FOLLOWUP_HINT)
        if fast_note:
            preamble.append(fast_note)
        # Text the user has selected, when the request is about "this".
        try:
            from .. import selection as _sel
            if _sel.wanted(user_input):
                sel_app, sel_text = _sel.get()
                if sel_text.strip():
                    preamble.append(
                        f"[Text the user has selected in {sel_app or 'the front app'} - "
                        f"'this' / 'it' means this text. To change it where it is, call "
                        f"replace_selection with only the new text:\n«{sel_text[:6000]}»]")
        except Exception:
            pass
        try:
            from ..tools.playbook import coach_on, for_request
            pb = for_request(user_input) if self._needs_tools(user_input) else None
            if pb is None and self._needs_tools(user_input):
                # The site in front counts too: "give me a hint" with a
                # LeetCode problem open is a LeetCode request.
                from ..context import front_window
                pb = for_request(front_window()[1] or "")
            if pb and pb[0] == "leetcode" and coach_on():
                preamble.append("[LeetCode coach mode is ON: hints and reviews, no solutions.]")
        except Exception:
            pb = None
        if pb:
            preamble.append(f"[The {pb[0]} playbook, already loaded - no need to "
                            f"call playbook for it:\n{pb[1].strip()}\n]")
        if preamble:
            turn = "\n".join(preamble) + "\n\n" + user_input
        self.messages.append({"role": "user", "content": turn})
        self._turn_msg = self.messages[-1]
        self._trim()

        self._explicit_memory_this_turn = False
        # Per-turn, not per-session: a call that was a pointless repeat while
        # answering one question is a perfectly reasonable first call for the
        # next one.
        self._failed_calls = {}
        self._done_calls = {}
        self._repeat_counts = {}
        self._reruns: dict[str, int] = {}
        self._consecutive_failures = 0
        self._short_circuits = 0
        self._trace: list[str] = []
        self._verified: list[bool] = []
        self._nudged = False
        self._last_trouble = False
        self._last_step_verified = False
        self._only_looked = True        # every call so far only read or searched
        action_req = bool(_ACTION.search(user_input)) and \
            self._needs_tools(user_input)
        # Tools are always offered. Dropping them for trivial requests used to
        # look like a saving, but the cached prefix *contains* the tool schemas,
        # so a tools-free prompt is a cache miss and costs ~3 s of re-prefill -
        # far more than the tool call it avoids. The system prompt is what keeps
        # simple questions from reaching for tools.
        use_tools = True
        think = self._should_think(user_input) or self._wants_plan(user_input)
        from .llm import is_remote
        self._remote = is_remote(self.llm)
        stats.thought_used = think
        tools = REGISTRY.schemas()
        if mem:
            yield Event("status", text=f"recalled {mem.count(chr(10)) + 1} memory(ies)")

        # Multi-step jobs get a plan before the first action. Measured failure
        # mode without one: the model starts acting on the first half of the
        # request, loses the second half, and reports done. The plan rides
        # along in the user turn, so every later step can see it.
        #
        # The plan is written in the same reply that starts the work. It used
        # to be a separate model call that only planned (3-5 s on every
        # multi-step job, with the whole prompt sent twice); the plan is just
        # as present in the conversation this way, and every later step still
        # sees it.
        inline_plan = self._wants_plan(user_input)
        if inline_plan:
            self.messages[-1]["content"] += "\n\n" + prompts.INLINE_PLAN

        did_mutate = False
        final_text = ""
        for step in range(max_steps):
            if self.stop_event.is_set():
                yield Event("status", text="interrupted")
                break
            stats.steps = step + 1
            if self._fast_turn and (self._last_trouble or step >= 4 or self._nudged
                                    or self._consecutive_failures):
                # Not as simple as it looked: the strong model takes over.
                self._fast_turn = False
                self.llm.turn_model = None
                yield Event("status", text="switching to the stronger model")
            calls: list[ToolCall] = []
            text_parts: list[str] = []
            t0 = time.time()
            first_tok = None

            try:
                # The local model can only afford reasoning on the first step
                # (it invalidates the KV cache); remote models keep it on for
                # every step of a hard task, which is where accuracy is lost.
                # Reasoning is spent where it pays: the first step, and any step
                # after something went wrong or came back unconfirmed. Plain
                # navigation steps (open, read, click) do not need 3-5 s of
                # thought each - a 30-step job used to spend most of its time
                # thinking about steps that were obvious.
                for d in self.llm.stream(self.messages, tools=REGISTRY.schemas(),
                                         think=think and (step == 0 or (
                                             self._remote and self._last_trouble)),
                                         stop_event=self.stop_event):
                    if d.kind == "text":
                        if first_tok is None:
                            first_tok = time.time() - t0
                        text_parts.append(d.text)
                        if not action_req:
                            yield Event("text", text=d.text)
                    elif d.kind == "thought":
                        yield Event("thought", text=d.text)
                    elif d.kind == "tool_call":
                        calls.append(d.tool_call)
                    elif d.kind == "done":
                        stats.tokens += d.n_tokens
                        stats.tps = d.tps or stats.tps
            except Exception as e:
                log.exception("generation failed")
                if hasattr(self.llm, "turn_model"):
                    self.llm.turn_model = None
                self._fast_turn = False
                stats.elapsed = time.time() - t_start
                self.last_stats = stats
                # "error" ends the turn: both clients stop reading at it. It
                # used to be followed by a "done" that nobody read - which the
                # next request on the same connection then took as its answer.
                yield Event("error", text=f"Model error: {e}")
                return

            if stats.ttft == 0.0 and first_tok:
                stats.ttft = first_tok
            assistant_text = "".join(text_parts).strip()

            plan_lines = _plan_lines(assistant_text) if (inline_plan and step == 0) else []
            if plan_lines:
                plan = "\n".join(plan_lines)
                yield Event("plan", text=plan)
                yield Event("status", text=f"planned {len(plan_lines)} steps")

            if not calls:
                if plan_lines and len(plan_lines) >= 2 and step == 0:
                    # Planned but did not start: never hand a plan back as the
                    # answer. Carry on with it.
                    self.messages.append({"role": "assistant", "content": assistant_text})
                    self.messages.append({"role": "user", "content":
                                          "Good plan. Now carry it out with your tools."})
                    continue
                final_text = assistant_text
                self.messages.append({"role": "assistant", "content": assistant_text})
                break

            # record the assistant's tool calls, then execute them
            self.messages.append({
                "role": "assistant",
                "content": assistant_text,
                "tool_calls": [c.to_openai() for c in calls],
            })
            stats.tool_calls += len(calls)

            # A model that has just seen a tool fail will often reissue the
            # identical call, and without a circuit breaker it does so until the
            # step budget runs out — observed: the same `ls | grep` 25 times and
            # the same failing `inspect_ui` 38 times, burning 144 s to learn
            # nothing. Repeating a call that already failed is never useful, so
            # it is answered from the record instead of being run again.
            pairs: list[tuple[str, dict]] = []
            short_circuit: dict[int, ToolResult] = {}
            for i, c in enumerate(calls):
                sig = self._sig_of(c.name, c.arguments)
                # A call that already *succeeded* with the same arguments is
                # just as much a loop as one that failed — observed:
                # browser_read called 40 times, returning the same page each
                # time. Re-running it cannot produce new information unless
                # something changed, so hand back what was already learned.
                done = self._done_calls.get(sig)
                if done is not None:
                    repeats = self._repeat_counts.get(sig, 0)
                    observer = c.name in _RECHECK_TOOLS
                    if not observer or repeats >= MAX_IDENTICAL_OBSERVATIONS:
                        short_circuit[i] = ToolResult(
                            False,
                            error=(
                                f"You already ran this exact call this turn and "
                                f"got: {done[:350]}. "
                                + (f"It has returned the same thing {repeats + 1} "
                                   f"times — nothing is changing. Take a different "
                                   f"action, or tell the user what is blocking you."
                                   if observer else
                                   "Running it again returns the same thing. Use "
                                   "that result, do something different, or answer "
                                   "the user.")),
                            display="repeat of a call already answered")
                        self._short_circuits += 1
                        continue
                prior = self._failed_calls.get(sig)
                if prior:
                    short_circuit[i] = ToolResult(
                        False,
                        error=(f"Already tried this exact call {prior['n']}x this "
                               f"turn and it failed: {prior['err'][:200]}. Do not "
                               f"repeat it. Either change the arguments, use a "
                               f"different tool, or tell the user what is blocking "
                               f"you."),
                        display="repeat of a call that already failed")
                    self._failed_calls[sig]["n"] += 1
                    self._short_circuits += 1
                else:
                    pairs.append((c.name, c.arguments))

            # A job that turns out to be Blender work moves to Astra mid-way.
            bm = _blender_model()
            if bm and hasattr(self.llm, "turn_model") and self.llm.turn_model != bm and any(
                    c.name == "blender_script" or (c.name == "playbook" and
                                                   str((c.arguments or {}).get("name", "")).lower() == "blender")
                    for c in calls):
                self.llm.turn_model = bm
                yield Event("status", text=f"using {bm} for this Blender job")

            for c in calls:
                if c.name in ("remember", "forget", "remember_about_me", "forget_about_me"):
                    self._explicit_memory_this_turn = True
                yield Event("tool_start", tool=c.name, args=c.arguments)

            if pairs:
                if CONFIG.parallel_tools and len(pairs) > 1:
                    ran = REGISTRY.execute_many(pairs, stop=self.stop_event)
                else:
                    # Checked before each call: once the user has stopped this
                    # request, nothing further in the batch may run.
                    ran = [ToolResult(False, error="not run: the user stopped this request")
                           if self.stop_event.is_set() else REGISTRY.execute_guarded(n, a)
                           for n, a in pairs]
            else:
                ran = []
            ran_iter = iter(ran)
            results = [short_circuit.get(i) or next(ran_iter)
                       for i in range(len(calls))]

            # Record failures so the next identical call is caught above.
            for c, r in zip(calls, results):
                if r.ok and r.display != "repeat of a call already answered":
                    sig_ok = self._sig_of(c.name, c.arguments)
                    body = json.dumps(r.for_model(), default=str)[:600]
                    if self._done_calls.get(sig_ok) == body:
                        self._repeat_counts[sig_ok] = \
                            self._repeat_counts.get(sig_ok, 0) + 1
                    else:
                        self._repeat_counts[sig_ok] = 0
                    self._done_calls[sig_ok] = body
                if not r.ok:
                    sig = self._sig_of(c.name, c.arguments)
                    rec = self._failed_calls.setdefault(
                        sig, {"n": 0, "err": r.error or "failed", "tool": c.name})
                    rec["n"] += 1
                    rec["err"] = r.error or rec["err"]
                    self._consecutive_failures += 1
                else:
                    self._consecutive_failures = 0

            # Did anything this step go wrong or come back unconfirmed? The
            # next step then gets the model's full reasoning.
            trouble = False
            for r in results:
                raw_ = getattr(r, "content", None)
                c_ = raw_ if isinstance(raw_, dict) else {}
                if (not r.ok) or c_.get("warning") or c_.get("verified") is False \
                        or c_.get("still_open"):
                    trouble = True
            self._last_trouble = trouble
            for c, r in zip(calls, results):
                t_ = REGISTRY.get(c.name)
                if (not r.ok) or t_ is None or t_.category not in ("read", "search", "compute"):
                    self._only_looked = False
            # The end state a tool itself confirmed (a video playing, a setting
            # read back): the job is done and needs no second opinion.
            self._last_step_verified = any(
                r.ok and isinstance(getattr(r, "content", None), dict)
                and r.content.get("verified") is True for r in results)

            no_progress = not any(self._verified)
            if (stats.tool_calls >= STALL_NUDGE and no_progress
                    and not self._nudged and not self.stop_event.is_set()):
                self._nudged = True
                yield Event("status", text="going in circles - changing approach")
                self.messages.append({"role": "user",
                                      "content": STALL_MESSAGE.format(n=stats.tool_calls)})
            stalled = stats.tool_calls >= STALL_STOP and no_progress

            # Being told twice that a call is a repeat and doing it anyway
            # means the model is not going to find its way out on its own.
            if self._consecutive_failures >= 6 or self._short_circuits >= 3 or stalled:
                yield Event("status",
                            text=("stopping: no confirmed progress after "
                                  f"{stats.tool_calls} steps" if stalled else
                                  "too many failing tool calls — stopping to "
                                  "avoid spinning"))
                self.messages.append({
                    "role": "user",
                    "content": ("Stop calling tools. They are failing or "
                                "repeating without making progress. "
                                "Tell me plainly what you were trying to do, "
                                "what blocked it, and what you need from me."),
                })
                try:
                    # Same tool list as every other step, so the prompt cache
                    # holds; a tool call it still makes is ignored, and if it
                    # gave no words at all it is asked again without tools.
                    for tl in (REGISTRY.schemas(), None):
                        for d in self.llm.stream(self.messages, tools=tl, think=False,
                                                 max_tokens=500,
                                                 stop_event=self.stop_event):
                            if d.kind == "text":
                                final_text += d.text
                                if not action_req:      # action replies are emitted below
                                    yield Event("text", text=d.text)
                        if final_text.strip() or self.stop_event.is_set():
                            break
                except Exception as e:              # the model is what is failing
                    log.warning("stall summary failed: %s", e)
                if not final_text.strip():
                    final_text = ("I stopped: my last steps kept failing or repeating "
                                  "without progress. What I tried:\n"
                                  + "\n".join(self._trace[-6:]))
                    if not action_req:
                        yield Event("text", text=final_text)
                self.messages.append({"role": "assistant", "content": final_text})
                break

            # A successful action changes the world, so a *look* that failed
            # before it (ui_elements on an app that was not open yet) is worth
            # trying again. Without this the loop guard refused the retry as a
            # "repeat of a call that already failed" - right after open_app.
            acted = any(r.ok and REGISTRY.get(c.name) and REGISTRY.get(c.name).category
                        not in ("read", "search", "compute")
                        for c, r in zip(calls, results))
            if acted:
                for sig in [k for k, v in self._failed_calls.items()
                            if v.get("tool") in _RECHECK_TOOLS]:
                    self._failed_calls.pop(sig, None)
                # The same action is a loop only if nothing changed in between.
                # After a different action succeeded (e.g. switching LeetCode's
                # language), re-running an earlier one (pasting the code again)
                # is legitimate - allowed up to twice per call, so a real
                # back-and-forth loop is still stopped.
                now_sigs = {self._sig_of(c.name, c.arguments) for c in calls}
                for sig in [k for k in self._done_calls if k not in now_sigs]:
                    if self._reruns.get(sig, 0) < 2:
                        self._reruns[sig] = self._reruns.get(sig, 0) + 1
                        self._done_calls.pop(sig, None)
                        self._repeat_counts.pop(sig, None)
            for c, r in zip(calls, results):
                self._trace.append(self._trace_line(c, r))
                # Only a tool that checked its own effect counts as self-verified.
                content = getattr(r, "content", None)
                self._verified.append(bool(r.ok and isinstance(content, dict)
                                           and content.get("verified") is True))
                # What ran and how it went, in the daemon log - so a report of
                # "it set brightness to 0" can be traced to the exact call.
                log.info("tool %s", self._trace_line(c, r)[2:])
                if r.ok and REGISTRY.get(c.name) and \
                        REGISTRY.get(c.name).category in ("write_new", "overwrite", "delete"):
                    did_mutate = True
                if r.ok:
                    self._observe(c.name, c.arguments, r.content)
                yield Event("tool_end", tool=c.name, args=c.arguments,
                            result=r, ok=r.ok,
                            text=r.display or (r.error if not r.ok else ""))
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": c.id,
                    "name": c.name,
                    "content": _for_model(r.for_model(), 12000),
                })
            self._trim()

            # One simple request, one action that confirmed its own effect
            # ("play blinding lights" -> play_music, verified): the answer is
            # already known. A second model call only to say "Playing X" cost
            # ~3 s on every such request.
            if (step == 0 and len(calls) == 1 and results[0].ok
                    and calls[0].name in _SELF_REPLY_TOOLS
                    and isinstance(results[0].content, dict)
                    and results[0].content.get("verified") is True
                    and len(user_input.split()) <= 14
                    and not self._wants_plan(user_input)):
                from . import fastpath
                final_text = fastpath.reply(calls[0].name, calls[0].arguments,
                                            results[0].content)
                self.messages.append({"role": "assistant", "content": final_text})
                if not action_req:          # action replies are emitted below
                    yield Event("text", text=final_text)
                break
        else:
            yield Event("status", text=f"step budget ({max_steps}) reached")

        # -- verification pass for consequential work ----------------------
        # A single action whose own tool confirmed it (brightness read back, the
        # app verified closed) needs no second opinion - and with free-tier
        # limits as low as 5 requests a minute, the extra call is not free.
        # Multi-step jobs, answers with no tool behind them, and anything that
        # failed are exactly where a model claims what it did not do.
        self_checked = (stats.tool_calls == 1 and len(self._verified) == 1
                        and self._verified[0])
        should_verify = (use_tools and final_text and not self.stop_event.is_set()
                         and ((did_mutate and stats.tool_calls >= 2) or action_req)
                         and not self_checked and not self._last_step_verified
                         # Only looked things up and all of it worked: the
                         # answer is the tool output; a second opinion adds a
                         # model call and nothing else.
                         and not (self._only_looked and stats.tool_calls > 0))
        if getattr(self, "_fast_turn", False):
            self.llm.turn_model = None          # the check is the strong model's job
            self._fast_turn = False
        if should_verify:
            out = {}
            for ev in self._verify(user_input, final_text, tools, out):
                yield ev
            if out.get("text"):
                final_text = out["text"]
        if action_req and final_text:
            # Held back until checked: the user never sees a claim that the
            # check then has to retract.
            yield Event("text", text=final_text)

        stats.elapsed = time.time() - t_start
        self.last_stats = stats
        pref = self._capture_default_app(user_input)
        if pref:
            yield Event("status", text=f"default set: {pref}")
        # What the user said feeds the learned profile (a few turns at a time,
        # in the background). It replaced saving raw sentences as "preferences".
        if not self._explicit_memory_this_turn:
            try:
                self._learner.note(user_input, final_text)
            except Exception as e:          # learning must never cost the answer
                log.warning("profile learner failed: %s", e)
        if final_text.rstrip().endswith("?"):
            self._last_followup_turn = self._turn_index      # asked: leave a gap
        if hasattr(self.llm, "turn_model"):
            self.llm.turn_model = None
        yield Event("done", text=final_text, meta={
            "steps": stats.steps, "tools": stats.tool_calls,
            "elapsed": round(stats.elapsed, 2), "tps": round(stats.tps, 1),
            "ttft": round(stats.ttft, 2), "thought": stats.thought_used,
        })

    @staticmethod
    def _trace_line(c, r) -> str:
        args = json.dumps(c.arguments, default=str, ensure_ascii=False)
        args = args if len(args) <= 140 else args[:137] + "..."
        res = (r.display or "") if r.ok else f"FAILED: {r.error or ''}"
        dur = getattr(r, "duration", 0) or 0
        return f"- {c.name} {args} -> {res[:160]}" + (f" [{dur:.1f}s]" if dur else "")

    def _verify(self, task: str, summary: str, tools,
                out: dict) -> Iterator[Event]:
        """
        One honest look at the work before the user sees the answer.

        The checker sees the tool calls that actually ran, not just the draft -
        a draft saying "solved on LeetCode" next to a trace of web_search,
        python_eval and write_file is visibly not a LeetCode submission. It
        either confirms the draft, finishes the job, or rewrites the answer to
        say what was and was not done. `out["text"]` carries the final answer.
        """
        yield Event("status", text="checking the result")
        trace = "\n".join(self._trace[-25:]) or "- (no tools were run)"
        self.messages.append({
            "role": "user",
            "content": prompts.VERIFY_PROMPT.format(task=task, summary=summary,
                                                    trace=trace),
        })
        draft = summary
        # The check may only LOOK. It used to be able to act for up to four
        # rounds, which is how a finished job ("the film is playing") turned
        # into reopening and refreshing the page to make sure.
        look_only = [n for n in REGISTRY.names()
                     if REGISTRY.get(n).category in ("read", "search")
                     and n not in _NOT_LOOKS]
        for _round in range(2):              # one look, then the answer
            parts: list[str] = []
            calls: list[ToolCall] = []
            try:
                # The full, unchanged tool list (so the prompt cache holds);
                # look-only is enforced below, when the calls are executed.
                for d in self.llm.stream(self.messages, tools=REGISTRY.schemas(),
                                         think=False, max_tokens=1500,
                                         stop_event=self.stop_event):
                    if d.kind == "text":
                        parts.append(d.text)
                    elif d.kind == "tool_call":
                        calls.append(d.tool_call)
            except Exception as e:
                # The work is done and the draft stands. A failed second look
                # used to end the turn with an "error" - and the user never saw
                # the answer, which is only shown after this check.
                log.warning("verification failed: %s", e)
                self.messages.append({"role": "assistant", "content": draft})
                out["text"] = draft
                yield Event("status", text="could not double-check the result")
                return
            text = "".join(parts).strip()
            confirmed = not text or text.upper().rstrip(".") == "CONFIRMED"
            if calls and _round == 1:
                # A second round may only answer; its calls are never run, so
                # they are not recorded either - a call with no result after it
                # is rejected by the API on every later request.
                self.messages.append({"role": "assistant",
                                      "content": draft if confirmed else text})
                out["text"] = draft if confirmed else text
                return
            self.messages.append({
                "role": "assistant", "content": text,
                **({"tool_calls": [c.to_openai() for c in calls]} if calls else {})})
            if not calls:
                if confirmed:
                    out["text"] = draft
                    yield Event("status", text="verified")
                else:
                    out["text"] = text
                    yield Event("status", text="corrected after checking")
                return
            for c in calls:
                yield Event("tool_start", tool=c.name, args=c.arguments)
            allowed = [(c.name, c.arguments) for c in calls if c.name in look_only]
            ran = iter(REGISTRY.execute_many(allowed)) if allowed else iter(())
            results = [next(ran) if c.name in look_only else ToolResult(
                False, error=("Not during the check: only look (read-only tools). "
                              "Do not redo, reopen, reload or click; say what is "
                              "not done instead."),
                display="refused during check") for c in calls]
            # A successful action changes the world, so a *look* that failed
            # before it (ui_elements on an app that was not open yet) is worth
            # trying again. Without this the loop guard refused the retry as a
            # "repeat of a call that already failed" - right after open_app.
            acted = any(r.ok and REGISTRY.get(c.name) and REGISTRY.get(c.name).category
                        not in ("read", "search", "compute")
                        for c, r in zip(calls, results))
            if acted:
                for sig in [k for k, v in self._failed_calls.items()
                            if v.get("tool") in _RECHECK_TOOLS]:
                    self._failed_calls.pop(sig, None)
                # The same action is a loop only if nothing changed in between.
                # After a different action succeeded (e.g. switching LeetCode's
                # language), re-running an earlier one (pasting the code again)
                # is legitimate - allowed up to twice per call, so a real
                # back-and-forth loop is still stopped.
                now_sigs = {self._sig_of(c.name, c.arguments) for c in calls}
                for sig in [k for k in self._done_calls if k not in now_sigs]:
                    if self._reruns.get(sig, 0) < 2:
                        self._reruns[sig] = self._reruns.get(sig, 0) + 1
                        self._done_calls.pop(sig, None)
                        self._repeat_counts.pop(sig, None)
            for c, r in zip(calls, results):
                self._trace.append(self._trace_line(c, r))
                # Only a tool that checked its own effect counts as self-verified.
                content = getattr(r, "content", None)
                self._verified.append(bool(r.ok and isinstance(content, dict)
                                           and content.get("verified") is True))
                # What ran and how it went, in the daemon log - so a report of
                # "it set brightness to 0" can be traced to the exact call.
                log.info("tool %s", self._trace_line(c, r)[2:])
                yield Event("tool_end", tool=c.name, args=c.arguments, result=r,
                            ok=r.ok, text=r.display or r.error)
                self.messages.append({"role": "tool", "tool_call_id": c.id,
                                      "name": c.name,
                                      "content": _for_model(r.for_model(), 8000)})
            if self.stop_event.is_set():
                return
        out["text"] = draft + ("\n\n(I could not fully confirm this.)")

    # -- passive memory ----------------------------------------------------
    @staticmethod
    def _capture_default_app(user_input: str) -> str:
        """
        "use Spotify from now on" must become a setting, not a note. Semantic
        memory would find it only sometimes; the preference store always does.
        """
        try:
            from ..memory.prefs import infer_default_from, set_default
            got = infer_default_from(user_input)
            if not got:
                return ""
            slot, app = got
            ok, res = set_default(slot, app)
            return f"{slot}={res}" if ok else ""
        except Exception:
            return ""

    @staticmethod
    def _observe(name: str, args: dict, content) -> None:
        """Feed a confirmed result to the learned profile. Never raises: it
        runs between a tool call and the record of its result, and an
        exception there left the call unanswered in the history."""
        try:
            from ..memory import profile as _profile
            _profile.observe_tool(name, args, content)
        except Exception as e:                       # noqa: BLE001
            log.warning("profile observe failed for %s: %s", name, e)

    def _awaiting_reply(self) -> bool:
        """Whether the conversation's last word was a question to the user,
        or a job the user stopped half-way."""
        for m in reversed(self.messages):
            role = m.get("role")
            if role == "assistant":
                c = str(m.get("content") or "").strip()
                return c.endswith("?") or c.startswith("[Stopped")
            if role in ("user", "system"):
                return False
        return False

    def interrupt(self) -> None:
        self.stop_event.set()

    def reset(self) -> None:
        self.messages = []
