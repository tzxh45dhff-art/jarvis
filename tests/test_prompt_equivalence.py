"""
The prefix cache is only safe if incremental rendering produces exactly the wire
text the reference chat template would. This checks that invariant across a full
tool-using conversation, which is where the two could silently diverge.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import mlx_lm  # noqa: F401
except ImportError:
    print("skipped: this test checks the on-device model's prompt renderer (pip install -r requirements-local.txt)")
    sys.exit(0)
from mlx_lm import load
from mlx_lm.tokenizer_utils import TokenizerWrapper

from jarvis.core import protocol as P
from jarvis.core.llm import MLXGemmaBackend
from jarvis.tools import REGISTRY

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


THOUGHT = re.compile(re.escape(P.CHANNEL_OPEN) + r".*?" + re.escape(P.CHANNEL_CLOSE),
                     re.DOTALL)


def norm(s: str) -> str:
    """Thought channels live in the cache but are stripped from history."""
    return THOUGHT.sub("", s)


_, tok = load("mlx-community/gemma-4-26b-a4b-it-4bit", lazy=True)
tokw = TokenizerWrapper(tok._tokenizer if hasattr(tok, "_tokenizer") else tok,
                        eos_token_ids=P.EOS_TOKEN_IDS)

TOOLS = REGISTRY.schemas()[:3]          # a few real schemas is enough
SYS = "You are JARVIS."

be = MLXGemmaBackend.__new__(MLXGemmaBackend)
be._tokenizer = tokw
be._cache_tail = "block"
be._folded = 1
be._sig = []


def reference(messages, think=False):
    return tokw.apply_chat_template(messages, add_generation_prompt=True,
                                    tokenize=False, enable_thinking=think,
                                    tools=TOOLS)


base = tokw.apply_chat_template([{"role": "system", "content": SYS}],
                                add_generation_prompt=False, tokenize=False,
                                enable_thinking=False, tools=TOOLS)

# ---------------------------------------------------------------- turn 1
msgs = [{"role": "system", "content": SYS},
        {"role": "user", "content": "What is 25 times 8?"}]
be._folded, be._cache_tail = 1, "block"
built = base + be._render_delta(msgs, think=False)
check("turn 1: user message", norm(built) == norm(reference(msgs)),
      f"built={built[-120:]!r}\n        ref  ={reference(msgs)[-120:]!r}")

# model generates a plain answer, ending on <turn|>
gen1 = "25 times 8 is 200." + P.TURN_CLOSE
cache_text = built + gen1
be._cache_tail = "turn_open"
be._folded = len(msgs) + 1

# ---------------------------------------------------------------- turn 2
msgs2 = msgs + [{"role": "assistant", "content": "25 times 8 is 200."},
                {"role": "user", "content": "And divided by 4?"}]
built2 = cache_text + be._render_delta(msgs2, think=False)
check("turn 2: appended user turn", norm(built2) == norm(reference(msgs2)),
      f"built={built2[-150:]!r}\n        ref  ={reference(msgs2)[-150:]!r}")

# ---------------------------------------------------------------- tool call
gen2 = ("50." + P.TURN_CLOSE)
cache_text2 = built2 + gen2
be._cache_tail = "turn_open"
be._folded = len(msgs2) + 1

msgs3 = msgs2 + [{"role": "assistant", "content": "50."},
                 {"role": "user", "content": "Check my disk space."}]
built3 = cache_text2 + be._render_delta(msgs3, think=False)
check("turn 3: second append", norm(built3) == norm(reference(msgs3)),
      f"built={built3[-150:]!r}\n        ref  ={reference(msgs3)[-150:]!r}")

# model emits a tool call and stops on <|tool_response>
tc = {"id": "call_0", "type": "function",
      "function": {"name": "read_file", "arguments": {"path": "/tmp/x"}}}
gen3 = (f'{P.TOOL_CALL_OPEN}call:read_file{{path:{P.STR}/tmp/x{P.STR}}}'
        f'{P.TOOL_CALL_CLOSE}{P.TOOL_RESP_OPEN}')
cache_text3 = built3 + gen3
be._cache_tail = "tool_open"
be._folded = len(msgs3) + 1

msgs4 = msgs3 + [{"role": "assistant", "content": "", "tool_calls": [tc]},
                 {"role": "tool", "tool_call_id": "call_0", "name": "read_file",
                  "content": '{"result": "hello"}'}]
built4 = cache_text3 + be._render_delta(msgs4, think=False)
ref4 = reference(msgs4)
check("turn 4: tool result continues the open model turn",
      norm(built4) == norm(ref4),
      f"built={built4[-190:]!r}\n        ref  ={ref4[-190:]!r}")

# ---------------------------------------------------------------- rebuild path
# After a thinking turn the cache is dropped and rebuilt from the base prefix,
# so the whole conversation is re-rendered in one delta. That path renders the
# assistant tool-call turn itself and must emit the full <|tool_response>.
be._folded, be._cache_tail = 1, "block"
rebuilt = base + be._render_delta(msgs4, think=False)
check("rebuild from base reproduces the same prompt",
      norm(rebuilt) == norm(ref4),
      f"built={rebuilt[-190:]!r}\n        ref  ={ref4[-190:]!r}")

# ---------------------------------------------------------------- two tool calls
tc2 = [{"id": "call_0", "type": "function",
        "function": {"name": "read_file", "arguments": {"path": "/a"}}},
       {"id": "call_1", "type": "function",
        "function": {"name": "read_file", "arguments": {"path": "/b"}}}]
msgs5 = msgs3 + [{"role": "assistant", "content": "", "tool_calls": tc2},
                 {"role": "tool", "tool_call_id": "call_0", "name": "read_file",
                  "content": '{"result": "A"}'},
                 {"role": "tool", "tool_call_id": "call_1", "name": "read_file",
                  "content": '{"result": "B"}'}]
be._folded, be._cache_tail = 1, "block"
rebuilt5 = base + be._render_delta(msgs5, think=False)
check("parallel tool results render correctly",
      norm(rebuilt5) == norm(reference(msgs5)),
      f"built={rebuilt5[-230:]!r}\n        ref  ={reference(msgs5)[-230:]!r}")

# ---------------------------------------------------------------- tokenisation
# encode(prefix) + encode(delta) must equal encode(prefix + delta), or the
# cache and the fed tokens would drift apart.
pre = base
delta = be._render_delta(msgs4, think=False)
a = tokw.encode(pre) + tokw.encode(delta, add_special_tokens=False)
b = tokw.encode(pre + delta)
check("tokenisation is compositional at the join", a == b,
      f"split={len(a)} joint={len(b)}")

print(f"\n{sum(R)}/{len(R)} prompt-equivalence tests passed")
sys.exit(0 if all(R) else 1)
