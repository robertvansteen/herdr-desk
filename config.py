"""Desk settings, read once from a TOML file shared by collect.py and desk.py.

The file is `$DESK_CONFIG`, else `$XDG_CONFIG_HOME/desk/config.toml` (usually
`~/.config/desk/config.toml`). Every key is optional; config.example.toml lists
them with their defaults. A missing file means all defaults.

Values that Linear can answer are not settings: the workspace slug for issue
links and the team keys that make up ticket identifiers (ENG-123) come from
the API unless the file overrides them.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tomllib

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or f"{HOME}/.local/state", "desk")
BOARD = f"{STATE_DIR}/board.json"
PATH = os.environ.get("DESK_CONFIG") or os.path.join(os.environ.get("XDG_CONFIG_HOME") or f"{HOME}/.config", "desk", "config.toml")

DEFAULT_BRIEF = (
    "Start work on Linear issue {id}: {title}\n{url}\n\n"
    "This worktree's branch, {branch}, is the one Linear generated for the issue, so a PR "
    "from it links back automatically. Read the issue and its comments before anything "
    "else: it is the spec for this branch."
)


def _load():
    try:
        with open(PATH, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        # A broken file must not take the board down; the defaults still give a usable board.
        print(f"desk: ignoring {PATH}: {e}", file=sys.stderr)
        return {}


_raw = _load()
_linear = _raw.get("linear", {})
_agent = _raw.get("agent", {})
_commands = _raw.get("commands", {})


def _command(value):
    """A command as an argv list, with `~` expanded; a string is split like a shell would."""
    if not value:
        return []
    argv = shlex.split(value) if isinstance(value, str) else list(value)
    return [os.path.expanduser(a) for a in argv]


REPOS_DIR = os.path.expanduser(_raw.get("repos_dir", "~/Developer"))
DEFAULT_REPO = _raw.get("default_repo", "")

LINEAR_WORKSPACE = _linear.get("workspace", "")
# nesszer/linear-cli to send Linear queries through; its own login replaces the key.
LINEAR_CLI = _command(_linear.get("cli"))
TICKET_PREFIXES = [p.upper() for p in _linear.get("ticket_prefixes", [])]
# The key never goes in the file: it comes from the environment or from a command that prints it.
LINEAR_KEY_COMMAND = _command(_linear.get("key_command")) or (
    ["security", "find-generic-password", "-s", "linear-api", "-w"] if sys.platform == "darwin" else [])

AGENT_KIND = _agent.get("kind", "claude")
BRIEF = _agent.get("brief", DEFAULT_BRIEF)

# Optional hooks. Without one, its key on the board reports that it is not configured.
REVIEW_COMMAND = _command(_commands.get("review"))
TIDY_COMMAND = _command(_commands.get("tidy"))
TIDY_LOG = os.path.expanduser(_commands.get("tidy_log", ""))


def linear_key():
    """The Linear API key, or "" when none is configured: `$LINEAR_API_KEY` first, then `key_command`."""
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if key or not LINEAR_KEY_COMMAND:
        return key
    try:
        r = subprocess.run(LINEAR_KEY_COMMAND, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def clipboard_command():
    """The first clipboard writer on PATH, as argv, or [] when there is none."""
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(argv[0]):
            return argv
    return []
