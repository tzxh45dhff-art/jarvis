"""Central configuration. Everything tunable lives here."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
REPO_ROOT = Path(__file__).resolve().parent.parent


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file ('#' comments, optional quotes, 'export ')."""
    out: dict[str, str] = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:                         # trailing comment on an unquoted value
            v = v.split(" #", 1)[0].strip()
        if k:
            out[k] = v
    return out


def _load_dotenv(path: Path) -> None:
    """Settings (JARVIS_*) from a .env file into the environment. The real
    environment wins. API keys are deliberately NOT exported: every tool that
    runs a shell command would inherit them. They are read on demand by
    core.cloud.load_secrets / core.anthropic.api_key instead."""
    for k, v in read_env_file(path).items():
        if k.startswith("JARVIS_"):
            os.environ.setdefault(k, v)


_load_dotenv(REPO_ROOT / ".env")
JARVIS_HOME = Path(os.environ.get("JARVIS_HOME", HOME / ".jarvis"))
_load_dotenv(JARVIS_HOME / ".env")
LOG_DIR = JARVIS_HOME / "logs"
DATA_DIR = JARVIS_HOME / "data"
RUN_DIR = JARVIS_HOME / "run"
SOCKET_PATH = RUN_DIR / "jarvisd.sock"
PID_FILE = RUN_DIR / "jarvisd.pid"

for _d in (JARVIS_HOME, LOG_DIR, DATA_DIR, RUN_DIR):
    _d.mkdir(parents=True, exist_ok=True)


@dataclass
class LLMConfig:
    # Local MLX model, verified: 85 tok/s decode, 244 tok/s prefill, tools + vision, 262k ctx
    model: str = os.environ.get("JARVIS_MODEL", "mlx-community/gemma-4-26b-a4b-it-4bit")
    max_tokens: int = 4096
    temperature: float = 0.3
    # Deterministic tool selection matters more than variety
    tool_temperature: float = 0.0
    # Trim history when the prompt exceeds this many tokens
    context_budget: int = 24000
    # Keep this many recent turns verbatim before summarising
    keep_recent_turns: int = 12


@dataclass
class VoiceConfig:
    # Off for now: Whisper + Kokoro + the wake-word model cost ~1-2 GB of the
    # engine's memory. Turn back on with JARVIS_VOICE_ENABLED=1.
    enabled: bool = os.environ.get("JARVIS_VOICE_ENABLED", "0").lower() in (
        "1", "true", "yes", "on")
    tts_model: str = "mlx-community/Kokoro-82M-bf16"
    tts_voice: str = os.environ.get("JARVIS_VOICE", "af_heart")
    tts_speed: float = 1.05
    tts_lang: str = "a"          # 'a' = American English
    # small.en over large-v3-turbo: measured on command-style phrases it is
    # 4x faster (0.12s vs 0.48s standalone, and the gap widens to ~2s inside the
    # daemon where it competes with the resident LLM for the GPU) *and* slightly
    # more accurate (92% vs 89% keyword recall). Large only wins on long-form
    # prose, which is not what anyone says to an assistant.
    stt_model: str = os.environ.get(
        "JARVIS_STT", "mlx-community/whisper-small.en-mlx"
    )
    stt_fallback: str = "mlx-community/whisper-base.en-mlx"
    sample_rate: int = 16000
    tts_sample_rate: int = 24000
    wake_word: str = "hey_jarvis"
    wake_threshold: float = 0.5
    # Voice-activity detection
    vad_silence_ms: int = 700     # only used for hands-free capture, not push-to-talk
    vad_start_ms: int = 200       # speech must persist this long to count
    vad_energy_floor: float = 0.012
    max_utterance_s: float = 30.0
    barge_in: bool = True


@dataclass
class MemoryConfig:
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embed_dim: int = 384
    db_path: Path = Path(os.environ.get("JARVIS_MEMORY_DB") or DATA_DIR / "memory.sqlite3")
    vector_path: Path = DATA_DIR / "vectors"
    # Retrieval
    top_k: int = 6
    min_score: float = 0.14
    # Auto-capture salient facts from conversation
    auto_capture: bool = True


@dataclass
class SafetyConfig:
    # Categories auto-approved without prompting
    auto_approve: set[str] = field(
        default_factory=lambda: {"read", "search", "compute", "write_new",
                                 "media", "ui_input", "calendar_write"}
    )
    require_confirm: set[str] = field(
        default_factory=lambda: {"delete", "overwrite", "system", "network_write",
                                 "send", "purchase", "publish", "install", "control"}
    )
    # Never allowed, regardless of confirmation
    forbidden_patterns: tuple[str, ...] = (
        r"rm\s+-rf\s+/\s*$", r"rm\s+-rf\s+/(?:\s|$)", r":\(\)\{.*\};:",
        r"mkfs(\.|\s)", r"dd\s+if=.*of=/dev/(disk|rdisk)",
        r">\s*/dev/(disk|rdisk)", r"diskutil\s+(eraseDisk|eraseVolume|zeroDisk)",
        r"csrutil\s+disable", r"spctl\s+--master-disable",
        r"sudo\s+rm\s+-rf", r"chmod\s+-R\s+777\s+/\s*$",
    )
    # Paths that may never be modified
    protected_paths: tuple[str, ...] = (
        "/System", "/usr/bin", "/usr/sbin", "/bin", "/sbin", "/Library/LaunchDaemons",
        "/private/etc", "/Applications",
    )
    # Trash instead of unlink
    use_trash: bool = True
    max_delete_batch: int = 20


@dataclass
class UIConfig:
    accent: str = "#5eead4"
    dim: str = "#64748b"
    warn: str = "#fbbf24"
    err: str = "#f87171"
    ok: str = "#4ade80"
    user_color: str = "#93c5fd"
    show_thinking: bool = False


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    workspace: Path = Path(os.environ.get("JARVIS_WORKSPACE", str(HOME)))
    max_agent_steps: int = 40
    parallel_tools: bool = True


CONFIG = Config()
