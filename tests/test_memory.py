import sys, os, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis.memory.store import MemoryStore, looks_sensitive

R=[]
def check(n,c,e=""):
    print(("  PASS " if c else "  FAIL ")+n+("" if c else f"  <- {e}")); R.append(bool(c))

db = os.path.join(tempfile.mkdtemp(), "m.sqlite3")
t0=time.time(); s = MemoryStore(path=db); print(f"  (store init {time.time()-t0:.2f}s)")

t0=time.time()
s.add("Jais prefers dark mode in every editor and terminal", kind="preference", subject="ui")
s.add("Jais is building a personal AI assistant called JARVIS on an M4 Pro MacBook", kind="project", subject="jarvis")
s.add("Jais uses VS Code as the main editor", kind="preference", subject="tools")
s.add("The user's favourite programming language is Python", kind="semantic")
s.add("Yesterday JARVIS organised the Downloads folder into 6 categories", kind="episodic")
s.add("Jais wants responses to be concise and to skip preamble", kind="preference")
print(f"  (6 adds in {time.time()-t0:.2f}s)")

print("-- semantic retrieval (not keyword) --")
t0=time.time(); hits = s.search("what editor should I use?"); dt=time.time()-t0
check("retrieval is fast", dt < 0.5, f"{dt:.3f}s")
check("finds VS Code without the word 'VS Code' in query",
      any("VS Code" in m.content for m in hits), [m.content for m in hits])

hits2 = s.search("what is the user working on right now?")
check("finds project memory", any("JARVIS" in m.content for m in hits2),
      [m.content for m in hits2])

hits3 = s.search("how should you format your answers?")
check("finds style preference", any("concise" in m.content for m in hits3),
      [m.content for m in hits3])

print("-- irrelevant query returns nothing --")
hits4 = s.search("the mating habits of emperor penguins in Antarctica", min_score=0.45)
check("no false positives", len(hits4) == 0, [m.content for m in hits4])

print("-- dedup / update --")
n_before = sum(s.count().values())
mid, msg = s.add("Jais uses VS Code as his primary code editor", kind="preference")
n_after = sum(s.count().values())
check("near-duplicate updates instead of duplicating", n_after == n_before, f"{n_before}->{n_after} ({msg})")

print("-- correction --")
mid2,_ = s.add("Jais lives in London", kind="semantic")
s.update(mid2, content="Jais lives in Toronto")
h = s.search("where does the user live")
check("correction reflected", any("Toronto" in m.content for m in h) and not any("London" in m.content for m in h),
      [m.content for m in h])

print("-- forget --")
s.forget(mid2)
h2 = s.search("where does the user live")
check("forgotten memory gone", not any("Toronto" in m.content for m in h2), [m.content for m in h2])

print("-- sensitive data refused --")
for bad in ["my password = hunter2supersecret", "sk-abc123def456ghi789jkl012mno",
            "AKIAIOSFODNN7EXAMPLE", "card 4111 1111 1111 1111"]:
    check(f"detect sensitive: {bad[:28]!r}", looks_sensitive(bad), bad)
mid3, msg3 = s.add("my api_key = sk-abcdefghij1234567890")
check("refuses to store credential", mid3 is None and "refused" in msg3, f"{mid3} {msg3}")

print("-- persistence across restart --")
del s
s2 = MemoryStore(path=db)
h3 = s2.search("what editor")
check("survives reopen", any("VS Code" in m.content for m in h3), [m.content for m in h3])

print(f"\n{sum(R)}/{len(R)} memory tests passed")
sys.exit(0 if all(R) else 1)
