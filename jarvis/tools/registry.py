"""
Tool registry: declaration, schema generation, safety gating, execution.

Tools declare their own risk category. The registry — not the model — decides
whether a call runs, needs confirmation, or is refused.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import inspect
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from ..safety.policy import Risk, Verdict, needs_confirmation

log = logging.getLogger("jarvis.tools")

# Which request a tool call belongs to. A request the user stopped can still
# have a tool finishing in the background; its confirmation prompts must not
# reach the screen of whatever request runs next. Carried into worker threads
# via copy_context() below.
CURRENT_TURN: contextvars.ContextVar[int] = contextvars.ContextVar("jarvis_turn", default=0)



# ------------------------------------------------------------------ arguments
#
# Models get tool calls *almost* right far more often than completely wrong:
# `app_name` for `app`, the number 50 sent as "50", "Safari" for an enum that
# says "safari", one URL where a list was expected. The old behaviour silently
# dropped any argument it did not recognise - so a misnamed `app` meant the
# action quietly went to the wrong app - and crashed on wrong types. Every call
# is now checked against the tool's own schema: unambiguous mistakes are
# repaired, everything else is refused with a message that says exactly how to
# fix it, *before* anything runs or the user is asked to confirm.

# Names models reach for, mapped to what JARVIS tools actually call them.
_ARG_ALIASES = {
    "app": ("app_name", "application", "application_name", "appname", "program"),
    "path": ("file", "filepath", "file_path", "filename", "location", "dir",
             "directory", "folder"),
    "url": ("link", "href", "uri", "address", "website", "page"),
    "text": ("content", "message", "body", "string", "value", "input"),
    "query": ("q", "search", "search_query", "term", "keywords", "prompt",
              "song", "track", "music", "song_name", "track_name", "artist"),
    "keys": ("key", "shortcut", "hotkey", "combo", "keystroke"),
    "level": ("value", "amount", "percent", "percentage", "brightness", "volume"),
    "element": ("button", "control", "target", "name", "label", "item"),
    "name": ("app", "title", "label"),
    "x": ("x_coord", "left"), "y": ("y_coord", "top"),
}


def _closest(word: str, options) -> str | None:
    import difflib
    hit = difflib.get_close_matches(word, list(options), n=1, cutoff=0.72)
    return hit[0] if hit else None


def _coerce(value, spec: dict, where: str):
    """Make `value` fit a JSON-schema type, or raise ValueError saying why."""
    import json as _json
    t = spec.get("type")
    if "enum" in spec:
        opts = spec["enum"]
        if value in opts:
            return value
        low = {str(o).lower(): o for o in opts}
        if str(value).lower() in low:
            return low[str(value).lower()]
        raise ValueError(f"{where} must be one of {opts}, not {value!r}")
    if t == "integer":
        if isinstance(value, bool):
            raise ValueError(f"{where} must be a whole number")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("+-").replace(".", "", 1).isdigit():
            f = float(value)
            if f.is_integer():
                return int(f)
        raise ValueError(f"{where} must be a whole number, not {value!r}")
    if t == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        try:
            return float(str(value).strip())
        except ValueError:
            raise ValueError(f"{where} must be a number, not {value!r}") from None
    if t == "boolean":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "yes", "1", "on"):
            return True
        if s in ("false", "no", "0", "off", ""):
            return False
        raise ValueError(f"{where} must be true or false, not {value!r}")
    if t == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        raise ValueError(f"{where} must be text, not {type(value).__name__}")
    if t == "array":
        if isinstance(value, list):
            items = value
        elif isinstance(value, str):
            v = value.strip()
            if v.startswith("["):
                try:
                    items = _json.loads(v)
                except ValueError:
                    items = [value]
            else:
                items = [value]
        else:
            items = [value]
        ispec = spec.get("items") or {}
        return [_coerce(x, ispec, f"{where}[{i}]") if ispec else x
                for i, x in enumerate(items)]
    if t == "object":
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                v = _json.loads(value)
                if isinstance(v, dict):
                    return v
            except ValueError:
                pass
        raise ValueError(f"{where} must be an object")
    return value


def normalize_args(tool: "Tool", args) -> tuple[dict, list[str]]:
    """
    Check a call against the tool's schema.

    Returns (clean_args, notes). Raises ValueError with a fix-it message when
    the call cannot be repaired safely.
    """
    import json as _json
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = _json.loads(args) if args.strip() else {}
        except ValueError:
            raise ValueError("arguments must be a JSON object") from None
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")
    props = (tool.parameters or {}).get("properties") or {}
    required = list((tool.parameters or {}).get("required") or [])
    try:
        accepted = set(inspect.signature(tool.fn).parameters)
    except (TypeError, ValueError):
        accepted = set(props)
    known = set(props) | accepted
    out: dict = {}
    notes: list[str] = []
    for k, v in args.items():
        if k in known:
            out[k] = v
            continue
        target = next((p for p, al in _ARG_ALIASES.items()
                       if p in known and k.lower() in al and p not in args), None)
        target = target or _closest(k, [p for p in known if p not in args])
        if target and target not in out:
            out[target] = v
            notes.append(f"treated '{k}' as '{target}'")
        else:
            raise ValueError(f"'{tool.name}' has no argument '{k}'. Its arguments "
                             f"are: {', '.join(sorted(props)) or 'none'}")
    for k in list(out):
        if k in props:
            out[k] = _coerce(out[k], props[k], f"'{k}'")
    missing = [r for r in required if r not in out
               or (isinstance(out[r], str) and not out[r].strip()
                   and props.get(r, {}).get("type") == "string")]
    if missing:
        raise ValueError(f"missing required argument(s) for '{tool.name}': "
                         f"{', '.join(missing)}")
    return out, notes

@dataclass
class ToolResult:
    ok: bool
    content: Any = ""
    error: str = ""
    duration: float = 0.0
    display: str = ""            # short line for the UI
    meta: dict = field(default_factory=dict)

    def for_model(self) -> Any:
        """What the model sees. Keep it compact - context is latency."""
        if not self.ok:
            # Details a failing tool attached (what it tried, what it saw) are
            # what the model needs to fix its approach on its own - not only
            # the one-line error.
            out = {"error": self.error or "failed"}
            if self.content not in (None, "", {}, []):
                import json as _json
                details = _json.dumps(self.content, default=str, ensure_ascii=False)
                out["details"] = self.content if len(details) <= 1500 else details[:1500] + "…"
            return out
        if isinstance(self.content, (dict, list)):
            return self.content
        return {"result": str(self.content)}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable
    risk: Risk = Risk.SAFE
    category: str = "generic"
    # Dynamic risk assessment based on actual arguments
    assess: Callable[[dict], Verdict] | None = None
    # Safe to run concurrently with other tools?
    parallel_safe: bool = True
    # Long-running: the UI shows a live spinner
    slow: bool = False
    # Not sent with every request: found on demand with find_tools. Providers
    # cap a request at 128 tools, and a shorter list is also easier to choose
    # from; rarely needed tools wait here until something asks for them.
    deferred: bool = False

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.confirm_hook: Callable[[str, dict, Verdict], bool] | None = None
        self.session_id: str = ""
        # categories the user granted for this whole session
        self.granted: set[str] = set()
        # deferred tools that find_tools has switched on (name -> last used)
        self._enabled: dict[str, float] = {}

    # -- registration ------------------------------------------------------
    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, parameters: dict,
             risk: Risk = Risk.SAFE, category: str = "generic",
             assess: Callable | None = None, parallel_safe: bool = True,
             slow: bool = False):
        def deco(fn):
            self.register(Tool(name, description, parameters, fn, risk,
                               category, assess, parallel_safe, slow))
            return fn
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    MAX_ENABLED = 12

    def schemas(self, only: list[str] | None = None,
                include_deferred: bool = False) -> list[dict]:
        """The tools to offer the model: everything except deferred tools that
        nothing has asked for yet. Sorted, so the list is stable between
        requests and the provider's prompt cache keeps working."""
        ts = []
        for n, t in sorted(self._tools.items()):
            if only is not None:
                if n in only:
                    ts.append(t)
            elif not t.deferred or include_deferred or n in self._enabled:
                ts.append(t)
        return [t.schema() for t in ts]

    def defer(self, names) -> None:
        for n in names:
            if n in self._tools:
                self._tools[n].deferred = True

    def enable(self, names) -> list[str]:
        """Switch deferred tools on (oldest evicted past the cap)."""
        now = time.time()
        done = []
        for n in names:
            t = self._tools.get(n)
            if t is None:
                continue
            if t.deferred:
                self._enabled[n] = now
            done.append(n)
        while len(self._enabled) > self.MAX_ENABLED:
            self._enabled.pop(min(self._enabled, key=self._enabled.get))
        return done

    def deferred_catalog(self) -> list[tuple[str, str]]:
        """(name, one-line purpose) of every deferred tool, for the prompt."""
        out = []
        for n, t in sorted(self._tools.items()):
            if t.deferred:
                first = t.description.strip().split(". ")[0].split("\n")[0]
                out.append((n, first[:110].rstrip(" .") + "."))
        return out

    # -- execution ---------------------------------------------------------
    def assess(self, name: str, args: dict) -> Verdict:
        t = self.get(name)
        if t is None:
            return Verdict(Risk.FORBIDDEN, f"unknown tool '{name}'")
        if t.assess:
            try:
                return t.assess(args)
            except Exception as e:            # a broken assessor must not open a hole
                return Verdict(Risk.HIGH, f"risk assessment failed: {e}",
                               category=t.category, needs_confirm=True)
        return Verdict(t.risk, t.description[:80], category=t.category,
                       needs_confirm=t.risk >= Risk.HIGH)

    def execute(self, name: str, args: dict, *, confirmed: bool = False) -> ToolResult:
        t0 = time.time()
        t = self.get(name)
        if t is None:
            import difflib
            near = difflib.get_close_matches(name, self.names(), n=4, cutoff=0.5)
            return ToolResult(False, error=(
                f"No tool named '{name}'."
                + (f" Did you mean: {', '.join(near)}?" if near else
                   " Check the tool list - do not invent tool names.")))
        try:
            args, notes = normalize_args(t, args)
        except Exception as e:          # ValueError, or a schema it cannot read
            return ToolResult(False, error=f"Invalid call to {name}: {e}. Nothing "
                                           f"was done - fix the call and retry.")
        verdict = self.assess(name, args)
        if verdict.risk >= Risk.FORBIDDEN:
            return ToolResult(False, error=f"REFUSED (safety): {verdict.reason}",
                              meta={"risk": "forbidden"})
        if not confirmed and verdict.category not in self.granted and needs_confirmation(verdict):
            hook = self.confirm_hook
            if hook is None:
                return ToolResult(False,
                                  error=f"Requires confirmation ({verdict.reason}) "
                                        f"but no confirmation channel is available.",
                                  meta={"risk": "needs_confirm"})
            try:
                approved = hook(name, args, verdict)
            except Exception as e:      # a broken channel is a "no", never a crash
                log.warning("confirmation for %s failed: %s", name, e)
                approved = False
            if not approved:
                return ToolResult(False, error="Declined by user.",
                                  meta={"risk": "declined"})
        # filter kwargs to the signature
        try:
            sig = inspect.signature(t.fn)
            kwargs = {k: v for k, v in args.items() if k in sig.parameters}
            missing = [
                p.name for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                and p.name not in kwargs
            ]
            if missing:
                return ToolResult(False,
                                  error=f"Missing required argument(s): {', '.join(missing)}")
            out = t.fn(**kwargs)
        except Exception as e:
            if type(e).__name__ == "InputBlocked":
                # An expected, explained condition - not a crash worth a trace.
                log.warning("tool %s blocked: no Accessibility permission", name)
                return ToolResult(False, error=str(e),
                                  duration=time.time() - t0)
            log.exception("tool %s failed", name)
            return ToolResult(False, error=f"{type(e).__name__}: {e}",
                              duration=time.time() - t0,
                              meta={"trace": traceback.format_exc()[-1500:]})
        if not isinstance(out, ToolResult):
            out = ToolResult(True, content=out)
        out.duration = time.time() - t0
        if notes:
            out.meta["arg_repairs"] = notes
            if isinstance(out.content, dict):
                out.content.setdefault("note", "; ".join(notes))
        return out

    # Read-only tools get a time limit: a lookup that hangs (seen once: a
    # batch of three read/search calls blocked a turn for 18 minutes) must not
    # freeze the whole turn. Tools that change things never get one -
    # abandoning a send or a write and letting the model retry could do it
    # twice.
    _DEADLINES = {"read": 90.0, "search": 120.0, "compute": 120.0}

    def execute_guarded(self, name: str, args: dict) -> ToolResult:
        t = self.get(name)
        limit = self._DEADLINES.get(t.category) if t else None
        if not limit:
            return self.execute(name, args)
        if t.slow:
            limit *= 2
        ex = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        fut = ex.submit(contextvars.copy_context().run, self.execute, name, args)
        try:
            return fut.result(timeout=limit)
        except cf.TimeoutError:
            import faulthandler
            import logging
            import sys
            logging.getLogger("jarvis.tools").error(
                "%s exceeded %.0fs and was abandoned; thread stacks follow", name, limit)
            try:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            except Exception:
                pass
            return ToolResult(False, error=(
                f"{name} took longer than {limit:.0f}s and was abandoned. Try a "
                f"different way, or tell the user it is not responding."))
        finally:
            ex.shutdown(wait=False)

    def execute_many(self, calls: list[tuple[str, dict]],
                     max_workers: int = 4, stop=None) -> list[ToolResult]:
        """
        Run independent tool calls concurrently. Only tools marked
        `parallel_safe` are batched; anything else runs in order to keep
        side effects deterministic.
        """
        if len(calls) == 1:
            return [self.execute_guarded(*calls[0])]
        all_safe = all((self.get(n) and self.get(n).parallel_safe) for n, _ in calls)
        if not all_safe:
            return [_STOPPED if (stop is not None and stop.is_set())
                    else self.execute_guarded(n, a) for n, a in calls]
        ex = cf.ThreadPoolExecutor(max_workers=min(max_workers, len(calls)))
        try:
            futs = [ex.submit(contextvars.copy_context().run, self.execute_guarded, n, a)
                    for n, a in calls]
            return [f.result() for f in futs]
        finally:
            ex.shutdown(wait=False)


_STOPPED = ToolResult(False, error="not run: the user stopped this request")

REGISTRY = ToolRegistry()
