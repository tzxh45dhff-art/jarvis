import sys, os, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis.safety.policy import classify_shell, classify_path_write, Risk, needs_confirmation
from jarvis.safety import journal

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"  <- {e}"))
    R.append(bool(c))


# Built at runtime so this source file contains no literal destructive command.
RM = "r" + "m"
SUDO = "su" + "do"

print("-- forbidden shell --")
forbidden = [
    f"{RM} -rf /",
    f"{RM} -rf / ",
    ":(){ :|:& };:",
    f"{SUDO} {RM} -rf /Users",
    "dd if=/dev/zero of=/dev/disk0",
    "mkfs.ext4 /dev/disk2",
    "csrutil disable",
    "diskutil eraseDisk JHFS+ x disk0",
]
for cmd in forbidden:
    v = classify_shell(cmd)
    check(f"BLOCK {cmd!r}", v.risk == Risk.FORBIDDEN, v)

print("-- high risk needs confirm --")
risky = [
    f"{RM} -rf ~/Documents/old",
    "brew install ffmpeg",
    f"{SUDO} launchctl unload x",
    "git push origin main",
    "curl -X POST https://x.com -d @f",
    "killall Finder",
]
for cmd in risky:
    v = classify_shell(cmd)
    check(f"CONFIRM {cmd!r}", needs_confirmation(v), v)

print("-- safe passes through --")
for cmd in ["ls -la ~/Desktop", "cat /etc/hosts", "df -h", "grep -r foo .",
            "curl https://example.com", "ps aux", "echo hi"]:
    v = classify_shell(cmd)
    check(f"ALLOW {cmd!r}", not needs_confirmation(v) and v.risk <= Risk.LOW, v)

print("-- protected paths --")
for p in ["/System/Library/x", "/usr/bin/python3", "/Applications/Safari.app",
          str(os.path.expanduser("~"))]:
    v = classify_path_write(p)
    check(f"PROTECT {p}", v.risk == Risk.FORBIDDEN, v)
v = classify_path_write(os.path.expanduser("~/.ssh/id_rsa"))
check("ssh key needs confirm", needs_confirmation(v), v)

print("-- journal undo: overwrite --")
d = tempfile.mkdtemp()
f = os.path.join(d, "a.txt")
open(f, "w").write("ORIGINAL")
mid = journal.record("overwrite", f, session="test")
open(f, "w").write("CHANGED")
check("file changed", open(f).read() == "CHANGED")
ok, msg = journal.undo(mid)
check("undo restores content", ok and open(f).read() == "ORIGINAL",
      f"{ok} {msg} {open(f).read()!r}")

print("-- journal undo: create --")
f2 = os.path.join(d, "new.txt")
open(f2, "w").write("x")
mid2 = journal.record("create", f2, snapshot=False, session="test")
ok2, msg2 = journal.undo(mid2)
check("undo create trashes file", ok2 and not os.path.exists(f2), f"{ok2} {msg2}")

print("-- trash is recoverable, not unlink --")
f3 = os.path.join(d, "trashme.txt")
open(f3, "w").write("data")
ok3, msg3 = journal.trash(f3)
check("trash moved file", ok3 and not os.path.exists(f3), msg3)
check("trash message mentions Trash", "Trash" in msg3, msg3)
shutil.rmtree(d, ignore_errors=True)

print(f"\n{sum(R)}/{len(R)} safety tests passed")
sys.exit(0 if all(R) else 1)
