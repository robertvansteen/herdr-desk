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

# `a` and `f` pick the brief from the card: new work on an issue with no PR yet gets
# DEFAULT_BRIEF; a PR with something to act on gets the fix frame with one task per problem;
# any other open PR gets DEFAULT_RESUME_BRIEF, which orients the agent and leaves the next
# step to you.
DEFAULT_BRIEF = (
    "Start work on Linear issue {id}: {title}\n{url}\n\n"
    "This worktree's branch, {branch}, is the one Linear generated for the issue, so a PR "
    "from it links back automatically. Read the issue and its comments before anything "
    "else: it is the spec for this branch."
)

# The fix frame: one brief around one task per problem, so a PR with conflicts, red CI and
# review comments reads as one job. Fields in the frame and every task: {number} {url}
# {branch} {base} {repo} (owner/name) {reviewers} {issue} (a "Linear issue: …" line, or "");
# the frame also gets {tasks}, the task texts, numbered when there is more than one.
DEFAULT_FIX_BRIEF = (
    "PR #{number} on branch {branch} needs work before it can merge: {url}{issue}\n\n"
    "{tasks}\n\n"
    "Then run the tests and push. Stop and ask when a fix needs a decision about behaviour "
    "rather than code."
)
DEFAULT_FIX_CONFLICTS = (
    "It has merge conflicts with {base}. Bring {base} in the way the repo does it (rebase or "
    "merge) and resolve every conflict so both sides' intent survives."
)
DEFAULT_FIX_CHECKS = (
    "CI is failing. Find the failing checks with `gh pr checks {number}` and read their logs with "
    "`gh run view <run-id> --log-failed`. Fix the cause, not the symptom: change a test only when "
    "the test itself is wrong, and run the failing checks locally where you can."
)
DEFAULT_FIX_REVIEWS = (
    "{reviewers} reviewed it and left comments. Read the review threads with `gh pr view "
    "{number} --comments` and the line comments with `gh api repos/{repo}/pulls/{number}/comments`. "
    "Make each change you agree with. Where you disagree, or a comment needs my call, draft a "
    "reply instead and list the drafts for me when you are done. Do not post replies or "
    "resolve threads."
)
# For an open PR with nothing to act on. {state} says where it stands, such as "waiting for
# review by erik"; {issue} is as in the fix frame.
DEFAULT_RESUME_BRIEF = (
    "You are picking up PR #{number} on branch {branch}: {url}{issue}\n"
    "It is {state}.\n\n"
    "Read its description, its discussion and its diff against {base}, and the issue if it has "
    "one, so you know where the work stands. Summarise that in a few lines, then wait for "
    "instructions."
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
_github = _raw.get("github", {})


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

# Review requests made to a team you are on, not only to you by name.
TEAM_REVIEW_REQUESTS = bool(_github.get("team_review_requests", False))

AGENT_KIND = _agent.get("kind", "claude")
BRIEF = _agent.get("brief", DEFAULT_BRIEF)
FIX_BRIEF = _agent.get("fix_brief", DEFAULT_FIX_BRIEF)
FIX_CONFLICTS = _agent.get("fix_conflicts", DEFAULT_FIX_CONFLICTS)
FIX_CHECKS = _agent.get("fix_checks", DEFAULT_FIX_CHECKS)
FIX_REVIEWS = _agent.get("fix_reviews", DEFAULT_FIX_REVIEWS)
RESUME_BRIEF = _agent.get("resume_brief", DEFAULT_RESUME_BRIEF)

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
