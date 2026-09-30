#!/usr/bin/env python3
"""Desk: a board over the cards collect.py produces.

Columns left to right: TODO, YOUR MOVE, WORKING, WAITING ON OTHERS, MERGEABLE, LANDED → REAP.
Left/right moves between columns, up/down between cards. Actions act on the
focused card and shell out to `herdr`, `gh`, `wt` and the configured hooks; the board never
mutates state itself. Data refreshes in a background thread every 60s or on `r`,
so gh latency never blocks the UI.
"""
import json
import re
import os
import subprocess
import sys
import threading
import time
import webbrowser

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static

import config
from config import BOARD, STATE_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")
COLUMNS = [("todo", "TODO", "$magenta"), ("your_move", "YOUR MOVE", "$red"), ("working", "WORKING", "$green"),
           ("waiting", "WAITING ON OTHERS", "$yellow"), ("mergeable", "MERGEABLE", "$cyan"),
           ("landed", "LANDED → REAP", "$blue")]
HUES = ("red", "green", "yellow", "blue", "magenta", "cyan")
STATUS_GLYPH = {"working": "●", "idle": "◌", "blocked": "◆", "done": "✔", "unknown": "?"}


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True)


def days(d):
    if d is None:
        return ""
    return f"{d*24:.0f}h" if d < 1 else f"{d:.0f}d"


def link(text, url):
    """Clickable text: a Textual @click action opens the URL, so it works wherever mouse events reach the app."""
    return f"[@click=app.open_url('{url}') u]{escape(text)}[/]" if url else escape(text)


def ticket_url(card, board):
    """The Linear URL for a card's ticket: the fetched issue's own, else one built from the workspace slug."""
    if card.get("issue"):
        return card["issue"]["url"]
    workspace = (board or {}).get("linear_workspace")
    if card.get("ticket") and workspace:
        return f"https://linear.app/{workspace}/issue/{card['ticket'].upper()}"
    return None


def elapsed(since):
    """Seconds, then minutes: a job's age, where the board's day/hour ages would all read 0h."""
    secs = int(time.time() - since)
    return f"{secs}s" if secs < 60 else f"{secs // 60}m"


def cause(output):
    """The line of a failed command's output that says why, for a one-line card.

    wt ends its errors with a hint (a "↳ To switch …, run …" line) and pre-switch hooks
    print their own warnings first, so neither the first nor the last line is the reason;
    the line marked ✗, or an error/fatal line, is.
    """
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    for l in lines:
        if l.startswith("✗"):
            return l.lstrip("✗ ").strip()
    for l in lines:
        if l.lower().startswith(("error:", "fatal:")):
            return l
    return lines[-1] if lines else "no output"


def card_id(card):
    """A card's identity across refreshes, which rebuild every card and may move it to
    another column: its repo, branch and worktree path."""
    return (card.get("repo"), card.get("branch"), card.get("path"))


def job_key(card):
    """What a job is filed under: the branch, which a TODO card shares with the worktree card
    that replaces it once `a` creates the worktree; the path for a card with no branch."""
    return card.get("branch") or card.get("path") or ""


def on_board():
    """Whether the Desk workspace has focus in Herdr; True when that cannot be told."""
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    if not ws:
        return True
    try:
        spaces = json.loads(sh("herdr", "workspace", "list").stdout)["result"]["workspaces"]
    except Exception:
        return True
    return any(w["workspace_id"] == ws and w.get("focused") for w in spaces)


def alert(title, body, sound="done"):
    """A Herdr notification, for a job that ends while you are in another workspace. On the
    board the card already says it, and a notification there only asks you to look twice.
    `request` is the sound for something that needs you."""
    if not on_board():
        sh("herdr", "notification", "show", title, "--body", body, "--sound", sound)


def fill(template, default, fields):
    """A brief from a configured template, or from its default when the template names a field
    that does not exist: a broken template must not leave a new session without its task."""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        return default.format(**fields)


def pr_brief(card, fixes_only=False):
    """(brief, what) for an agent on a card with an open PR of yours, or (None, why not).

    The tasks are the PR's problems, in the order that saves work: conflicts first, since
    resolving them changes the code CI runs; then red CI; then review comments on the
    current commit, or changes requested. With none, the resume brief orients the agent
    and asks for instructions; fixes_only (for `f`) refuses instead, and leaves reviews out,
    since answering them is judgement rather than mechanics.
    """
    pr, issue = card.get("pr"), card.get("issue")
    if not pr or pr.get("merged") or card.get("review_request"):
        return None, "no open PR of yours on this card"
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/", pr["url"])
    reviewed = sorted({r["by"] for r in pr.get("reviews") or [] if r["current"]})
    fields = {"number": pr["number"], "url": pr["url"], "branch": card["branch"], "base": pr.get("base") or "the base branch",
              "repo": m.group(1) if m else "", "issue": f"\nLinear issue: {issue['id']}, {issue['title']} ({issue['url']})" if issue else "",
              "reviewers": ", ".join(reviewed) or "Reviewers"}
    tasks = []
    if pr.get("conflicts"):
        tasks.append(fill(config.FIX_CONFLICTS, config.DEFAULT_FIX_CONFLICTS, fields))
    if pr.get("checks") == "fail":
        tasks.append(fill(config.FIX_CHECKS, config.DEFAULT_FIX_CHECKS, fields))
    if not fixes_only and (reviewed or pr.get("review") == "CHANGES_REQUESTED"):
        tasks.append(fill(config.FIX_REVIEWS, config.DEFAULT_FIX_REVIEWS, fields))
    if tasks:
        what = "fix" if fixes_only or not (reviewed or pr.get("review") == "CHANGES_REQUESTED") else "work on"
        if len(tasks) > 1:
            tasks = [f"{i}. {t}" for i, t in enumerate(tasks, 1)]
            if pr.get("conflicts"):
                tasks.append("Resolve the conflicts first: everything after them runs on the merged code.")
        return fill(config.FIX_BRIEF, config.DEFAULT_FIX_BRIEF, {**fields, "tasks": "\n\n".join(tasks)}), what
    if fixes_only:
        return None, "nothing mechanical to fix: no merge conflicts and CI is not red"
    if pr["draft"]:
        state = "a draft"
    elif pr.get("review") == "APPROVED":
        state = "approved"
    elif pr.get("reviewers"):
        state = f"waiting for review by {', '.join(pr['reviewers'])}"
    elif pr.get("reviews"):
        state = "reviewed before your last push, and waiting for those reviewers to look again"
    else:
        state = "open, with no reviewer requested yet"
    return fill(config.RESUME_BRIEF, config.DEFAULT_RESUME_BRIEF, {**fields, "state": state}), "pick up"


def fit(text, width):
    """One line, hard-truncated with an ellipsis; wrapping inside a narrow card destroys scanability."""
    text = text or ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"



def nudge_text(card):
    """A short Slack message asking for a review, addressed to nobody.

    Written for a direct message: second person, no greeting and no name, so the
    same text goes to whoever ends up reviewing. It says what the PR does, how
    big it is and how long it has waited, and stops there.
    """
    pr, issue = card.get("pr"), card.get("issue")
    if not pr or pr["merged"]:
        return None, "no open PR on this card"
    if card.get("review_request"):
        return None, "someone else's PR: the review is yours to give"
    days_open = pr["updated_days"] or 0
    waited = "opened this morning" if days_open < 1 else f"open for {days_open:.0f} day{'s' if days_open >= 2 else ''}"
    lines = ["Could you take a review when you have a moment?", ""]
    lines.append(f"*{pr['title']}*")
    lines.append(pr["url"])
    if issue:
        lines.append(f"<{issue['url']}|{issue['id']}>")
    facts = [pr["size"]]
    if pr["checks"] == "pass":
        facts.append("CI green")
    facts.append(waited)
    lines.append("_" + " · ".join(facts) + "_")
    return "\n".join(lines), None


class Card(Static):
    DEFAULT_CSS = """
    Card { border: round ansi_bright_black; padding: 0 1; margin: 0 0 1 0; height: auto; background: ansi_default; color: ansi_default; }
    Card:focus { border: heavy $yellow; }
    """
    can_focus = True

    def __init__(self, data, accent):
        super().__init__()
        self.data = data
        self.accent = accent

    def on_resize(self):
        self.refresh()

    def on_click(self, event):
        self.focus()
        if event.chain == 2:
            self.app.action_focus_ws()

    def render(self):
        c = self.data
        w = max(20, (self.content_size.width or 44))
        pr, issue, agents = c.get("pr"), c.get("issue"), c.get("agents", [])
        lines = []

        # 1. Title: issue > PR > what the session calls itself > branch. Two lines at most.
        prio = {1: "[$red]!![/$red] ", 2: "[$yellow]![/$yellow] "}.get(issue["priority"], "") if issue else ""
        title = (issue and issue["title"]) or (pr and pr["title"]) or next((a["title"] for a in agents if a.get("title")), "") \
            or (c["branch"].replace("detached@", "") if c["branch"] else os.path.basename(c["path"] or "~"))
        lines.append(f"{prio}[b]{escape(fit(title, 2 * w - 4))}[/b]")

        # 2. Identifiers: ticket, PR, repo. The branch is in details; it rarely says more than the title.
        ids, used = [], 0   # `used` counts visible characters; the markup in `ids` is much longer than what renders
        if issue:
            ids.append(f"{link(issue['id'], issue['url'])} [dim]{escape(issue['state'])}[/dim]"); used += len(issue["id"]) + 1 + len(issue["state"])
        elif c.get("ticket"):
            ids.append(link(c["ticket"], ticket_url(c, self.app.board))); used += len(c["ticket"])
        if pr:
            ids.append(link("#" + str(pr["number"]), pr["url"])); used += len(str(pr["number"])) + 1
        tag = issue.get("project") if c.get("orphan_issue") and issue else c["repo"]
        # A session in the home directory is keyed by the home folder's name, which says nothing.
        if tag and tag != os.path.basename(config.HOME):
            ids.append(f"[dim]{escape(fit(tag, max(10, w - used - 2 * len(ids))))}[/dim]")
        if ids:
            lines.append("  ".join(ids))

        # 2b. A job this board started on the card: progress while it runs, the reason when it failed.
        job = self.app.jobs.get(job_key(c))
        if job:
            if job["state"] == "failed":
                lines.append(f"[b $red]✗ {escape(fit(job['text'], w - 2))}[/b $red]")
            elif job["state"] == "done":
                lines.append(f"[b $green]✓ {escape(fit(job['text'], w - 2))}[/b $green]")
            else:
                lines.append(f"[b $yellow]⟳ {escape(fit(job['text'], w - 8))} {elapsed(job['since'])}[/b $yellow]")

        # 3. Status. The words carry the colour; the reason line below is dropped when it would repeat them.
        said = set()
        if pr:
            if pr["merged"]:
                state = "[$blue]merged[/$blue]"; said.add("merged")
            elif pr["draft"]:
                state = "[dim]draft[/dim]"; said.add("draft")
            else:
                state = {"CHANGES_REQUESTED": "[$red]changes requested[/$red]", "APPROVED": "[$green]approved[/$green]",
                         "REVIEW_REQUIRED": "[$yellow]review required[/$yellow]"}.get(pr["review"], "open")
                if pr["review"] == "CHANGES_REQUESTED": said.add("changes requested")
            chk = {"fail": "[$red]✗ CI[/$red]", "pass": "[$green]✓ CI[/$green]", "pending": "[$yellow]… CI[/$yellow]"}.get(pr["checks"], "")
            if pr["checks"] == "fail": said.add("checks failing")
            conflict = "[$red]⚡ conflicts[/$red]" if pr.get("conflicts") and not pr["merged"] else ""
            if conflict: said.add("merge conflicts")
            age_d = pr["updated_days"] or 0
            age_col = "$red" if age_d > 5 and not pr["merged"] else ("$yellow" if age_d > 2 and not pr["merged"] else "dim")
            parts = [state, chk, conflict, f"[{age_col}]{days(pr['updated_days'])}[/{age_col}]"]
            if pr["reviewers"] and not pr["merged"]:
                parts.append(f"[dim]→ {escape(fit(', '.join(pr['reviewers']), max(10, w - 34)))}[/dim]")
            lines.append("  ".join(x for x in parts if x))

        # 4. Sessions.
        for a in agents:
            g = STATUS_GLYPH.get(a["status"], "?")
            col = {"working": "$green", "blocked": "$red", "done": "$cyan"}.get(a["status"], "dim")
            extra = f"  [dim]{escape(fit(a['title'], w - 22))}[/dim]" if a.get("title") and a["title"] != title and len(agents) > 1 else ""
            lines.append(f"[{col}]{g} {a['kind']} {a['status']}[/{col}] [dim]{days(a['since_days'])}[/dim]{extra}")

        # 4b. Unfinished work sitting in the worktree.
        if c.get("dirty") or c.get("unpushed"):
            bits = []
            if c.get("dirty"):
                bits.append(f"{c['dirty']} uncommitted")
            if c.get("unpushed"):
                bits.append(f"{c['unpushed']} unpushed")
            lines.append(f"[$magenta]✎ {escape(', '.join(bits))}[/$magenta]")

        # 5. Reason, only when the status line has not already said it.
        reason = c.get("reason")
        if reason and ("uncommitted" in reason or "not pushed" in reason) and (c.get("dirty") or c.get("unpushed")):
            reason = None   # the ✎ line above already says it
        if reason and reason.lower() not in said and not (reason.startswith("draft") and "draft" in said):
            lines.append(f"[b {self.accent}]▶ {escape(fit(reason, w - 2))}[/b {self.accent}]")
        return "\n".join(lines)


class FoldedDrafts(Static):
    """One card standing in for every draft PR older than a week; D unfolds them."""
    DEFAULT_CSS = "FoldedDrafts { border: dashed ansi_bright_black; padding: 0 1; margin: 0 0 1 0; height: auto; color: ansi_bright_black; }"

    def __init__(self, n):
        super().__init__(f"▸ {n} draft PRs older than a week  [dim](D to show)[/dim]")


class Lane(VerticalScroll, inherit_bindings=False):
    """A scroll area without key bindings: arrows move between cards, and focus keeps the card in view.

    VerticalScroll and HorizontalScroll bind the arrow keys to scrolling, and as ancestors of the
    focused card they would take the arrows before the app's navigation bindings see them.
    Nor does it take focus: a scroll container is focusable by default, and when a refresh
    removes the focused card, focus would fall to it and leave no card focused.
    """
    can_focus = False


class Board(HorizontalScroll, inherit_bindings=False):
    """The row of columns, scrolled sideways by focus and the mouse only; see Lane."""
    can_focus = False


class Column(Vertical):
    DEFAULT_CSS = """
    Column { width: 64; border: round ansi_bright_black; padding: 0; background: ansi_default; }
    Column > .title { text-style: bold; padding: 0 1; height: 1; background: ansi_default; }
    Column > Lane { height: 1fr; padding: 0 1; background: ansi_default; scrollbar-color: ansi_bright_black; scrollbar-background: ansi_default; }
    """

    def __init__(self, key, title, color):
        super().__init__()
        self.key, self.title_text, self.color = key, title, color

    def compose(self) -> ComposeResult:
        yield Static("", classes="title")
        yield Lane()

    def fill(self, cards, needle="", show_drafts=False):
        # Lanes share one fixed width (see Column CSS); the board scrolls sideways when they do not fit.
        if needle:
            def haystack(c):
                pr, issue = c.get("pr") or {}, c.get("issue") or {}
                parts = [c.get("repo"), c.get("branch"), c.get("ticket"), c.get("reason"),
                         pr.get("title"), issue.get("title"), issue.get("id")]
                if pr.get("number"):
                    parts += [str(pr["number"]), f"#{pr['number']}"]
                return " ".join(str(x) for x in parts if x).lower()
            cards = [c for c in cards if needle in haystack(c)]
        folded = [c for c in cards if c.get("stale_draft")] if not show_drafts and not needle else []
        shown = [c for c in cards if c not in folded]
        self.query_one(".title", Static).update(f"[{self.color}]{self.title_text} ({len(cards)})[/{self.color}]")
        scroll = self.query_one(Lane)
        scroll.remove_children()
        for c in shown:
            scroll.mount(Card(c, self.color))
        if folded:
            scroll.mount(FoldedDrafts(len(folded)))


class Desk(App):
    # Terminal palette, not Textual's theme: colours and background follow the terminal's own theme.
    ansi_color = True
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    * { link-color: ansi_default; link-color-hover: ansi_default; link-background-hover: ansi_default; }
    Screen { layout: vertical; background: ansi_default; color: ansi_default; }
    #board { height: 1fr; background: ansi_default; scrollbar-color: ansi_bright_black; scrollbar-background: ansi_default; scrollbar-size-horizontal: 1; }
    #filter { display: none; height: 3; background: ansi_default; color: ansi_default; border: round $yellow; }
    #filter.shown { display: block; }
    Header { background: ansi_default; color: ansi_default; }
    Footer { background: ansi_default; }
    Footer > .footer--key, FooterKey { background: ansi_default; color: $yellow; }
    FooterKey .footer-key--key { background: ansi_default; color: $yellow; }
    FooterKey .footer-key--description { background: ansi_default; color: ansi_default; }
    Toast { background: ansi_default; color: ansi_default; border: round ansi_bright_black; }
    """
    TITLE = "Desk"
    BINDINGS = [
        Binding("enter", "focus_ws", "focus workspace"),
        Binding("o", "open_pr", "open PR"),
        Binding("i", "open_issue", "open issue"),
        Binding("n", "nudge", "copy nudge"),
        Binding("v", "review", "review PR"),
        Binding("a", "launch", "launch agent"),
        Binding("f", "fix", "fix it"),
        Binding("c", "close_ws", "close session"),
        Binding("x", "reap", "reap"),
        Binding("X", "reap_force", "force reap", show=False),
        Binding("r", "refresh", "refresh"),
        Binding("T", "tidy", "tidy sessions"),
        Binding("space", "details", "details"),
        Binding("slash", "filter", "filter"),
        Binding("D", "toggle_drafts", "old drafts", show=False),
        Binding("question_mark", "help", "help"),
        Binding("escape", "clear_filter", show=False),
        Binding("1", "goto_col(0)", show=False), Binding("2", "goto_col(1)", show=False),
        Binding("3", "goto_col(2)", show=False), Binding("4", "goto_col(3)", show=False),
        Binding("5", "goto_col(4)", show=False), Binding("6", "goto_col(5)", show=False),
        Binding("left", "col(-1)", show=False), Binding("right", "col(1)", show=False),
        Binding("h", "col(-1)", show=False), Binding("l", "col(1)", show=False),
        Binding("up", "row(-1)", show=False), Binding("down", "row(1)", show=False),
        Binding("k", "row(-1)", show=False), Binding("j", "row(1)", show=False),
        Binding("q", "quit", "quit"),
    ]


    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="")
        # Hidden, the box must not be focusable: as the first focusable widget it would take
        # focus at startup, before any card exists, and swallow the "/" meant to open it.
        filter_box = Input(placeholder="filter: repo, branch, ticket, title…  (Esc clears)", id="filter")
        filter_box.can_focus = False
        yield filter_box
        with Board(id="board"):
            for key, title, color in COLUMNS:
                yield Column(key, title, color)
        yield Footer()

    board = None
    needle = ""
    show_drafts = False
    dark = False

    def get_theme_variable_defaults(self):
        # Bright hues pop on a dark background but wash out on a light one.
        return {h: f"ansi_bright_{h}" if self.dark else f"ansi_{h}" for h in HUES}

    def set_dark(self, dark):
        if dark != self.dark:
            self.dark = dark
            self.refresh_css()
            self.render_board()

    jobs = {}

    def set_job(self, card, text, state="running", workspace=None, detail=""):
        """Show a job's progress on its card, or clear it with state=None. Main thread only;
        workers go through `job()`. A failed job stays until the card's next job replaces it."""
        key = job_key(card)
        if state is None:
            self.jobs.pop(key, None)
        else:
            since = self.jobs[key]["since"] if key in self.jobs and self.jobs[key]["state"] == "running" == state else time.time()
            workspace = workspace or (self.jobs.get(key) or {}).get("workspace")
            self.jobs[key] = {"text": text, "state": state, "since": since, "workspace": workspace, "detail": detail}
        for w in self.query(Card):
            if job_key(w.data) == key:
                w.refresh(layout=True)

    def job(self, card, text, state="running", workspace=None, detail=""):
        self.call_from_thread(self.set_job, card, text, state, workspace, detail)

    def _tick(self):
        # Elapsed time on running jobs; nothing to redraw when none runs.
        running = {k for k, j in self.jobs.items() if j["state"] == "running"}
        for w in self.query(Card):
            if job_key(w.data) in running:
                w.refresh()

    def on_mount(self):
        # Ask to be told the terminal's dark/light scheme now and whenever it changes (mode 2031).
        self._driver.write("\x1b[?2031h\x1b[?996n")
        self.load(from_disk=True)
        # 5 minutes: GitHub's GraphQL budget is shared with every other tool and agent on this account.
        self.set_interval(300, self.load)
        self.set_interval(1, self._tick)

    def on_unmount(self):
        self._driver.write("\x1b[?2031l")

    @work(thread=True, exclusive=True)
    def load(self, from_disk=False):
        if not from_disk or not os.path.exists(BOARD):
            subprocess.run([sys.executable, f"{HERE}/collect.py"], capture_output=True)
        try:
            board = json.load(open(BOARD))
        except Exception:
            return
        self.call_from_thread(self.render_board, board)

    def render_board(self, board=None):
        board = board or self.board
        if not board:
            return
        self.board = board
        focused = card_id(self.focused.data) if isinstance(self.focused, Card) else getattr(self, "_last_card_id", None)
        for col in self.query(Column):
            col.fill(board["columns"].get(col.key, []), self.needle, self.show_drafts)
        import time as _t
        self.sub_title = (f"{board['agents']} agents · {sum(board['counts'].values())} cards · data {_t.strftime('%H:%M', _t.localtime(board['generated']))}"
                          + ("" if board.get("linear") else " · Linear: no key")
                          + ("" if board.get("github_ok", True) else
                             f" · ⚠ GitHub unavailable ({fit(board.get('github_error') or 'unknown error', 60)}), PR data stale"))
        # Refocus a card only when a card had focus. While the filter box is being typed
        # into, it keeps focus: moving it to a card mid-word would hand the remaining
        # keystrokes to the board, where letters such as l, c, x and v are actions.
        if isinstance(self.focused, Input):
            return
        # The columns mount their new cards on the next refresh; focusing before then would
        # pick a card that is being removed.
        self.call_after_refresh(self._refocus, focused)

    def _refocus(self, key):
        """Focus the card with this identity wherever it now sits, else the first card."""
        if isinstance(self.focused, Input):
            return
        cards = list(self.query(Card))
        target = next((c for c in cards if card_id(c.data) == key), cards[0] if cards else None)
        if target:
            target.focus()

    # ---- navigation
    def _grid(self):
        return [list(col.query(Card)) for col in self.query(Column)]

    def _pos(self):
        for ci, col in enumerate(self._grid()):
            for ri, card in enumerate(col):
                if card is self.focused:
                    return ci, ri
        return 0, 0

    def _enter_col(self, grid, ci, ri):
        """Focus the card last focused in column ci, else the one at row ri (clamped).

        The card is remembered by (repo, branch), not by row: a refresh rebuilds and
        re-sorts the columns, and the same row may then hold a different card.
        """
        col = grid[ci]
        key = getattr(self, "_col_cards", {}).get(ci)
        target = next((c for c in col if (c.data["repo"], c.data["branch"]) == key), None) or col[min(ri, len(col) - 1)]
        target.focus(); list(self.query(Column))[ci].scroll_visible()

    def on_descendant_focus(self, event):
        if isinstance(event.widget, Card):
            self._last_card_id = card_id(event.widget.data)
            ci, _ = self._pos()
            self._col_cards = {**getattr(self, "_col_cards", {}), ci: (event.widget.data["repo"], event.widget.data["branch"])}

    def action_col(self, d):
        grid = self._grid(); ci, ri = self._pos()
        for step in range(1, 6):
            nc = ci + d * step
            if 0 <= nc < len(grid) and grid[nc]:
                self._enter_col(grid, nc, ri); return

    def action_row(self, d):
        grid = self._grid(); ci, ri = self._pos()
        col = grid[ci] if grid else []
        if col:
            col[max(0, min(len(col) - 1, ri + d))].focus()

    # ---- actions
    def card(self):
        """The focused card, refocusing the last one when a modal handed focus back to the screen."""
        if isinstance(self.focused, Card):
            self._last_card = self.focused
            return self.focused.data
        last = getattr(self, "_last_card", None)
        if last is not None and last.is_attached:
            last.focus()
            return last.data
        cards = list(self.query(Card))
        if cards:
            cards[0].focus()
            return cards[0].data
        return None

    def action_focus_ws(self):
        c = self.card()
        if not c:
            return
        # A workspace this board just opened is known before the collector next sees it.
        job = self.jobs.get(job_key(c)) or {}
        ws = c.get("workspace_id") or job.get("workspace")
        if ws:
            sh("herdr", "workspace", "focus", ws)
            if job.get("state") == "done":
                self.set_job(c, None, None)
        else:
            self.notify("no open workspace — press a to launch one", severity="warning")

    def action_open_url(self, url):
        webbrowser.open(url)

    def action_open_pr(self):
        c = self.card()
        if c and c.get("pr"):
            webbrowser.open(c["pr"]["url"])
        elif c and c.get("issue"):
            webbrowser.open(c["issue"]["url"])

    def action_open_issue(self):
        c = self.card()
        if not c:
            return
        url = ticket_url(c, self.board)
        if url:
            webbrowser.open(url)
        else:
            self.notify("no Linear issue on this card", severity="warning")

    def action_nudge(self):
        c = self.card()
        if not c:
            return
        pr = c.get("pr")
        if pr and pr.get("checks") == "fail":
            self.notify("CI is red — a reviewer would bounce it. Fix the build first.", severity="warning", timeout=8); return
        if pr and pr.get("review") == "APPROVED":
            self.notify("already approved — this one is yours to merge", severity="warning"); return
        text, err = nudge_text(c)
        if err:
            self.notify(err, severity="warning"); return
        clip = config.clipboard_command()
        if clip:
            subprocess.run(clip, input=text, text=True)
            self.notify(f"copied: {text.splitlines()[0]}", timeout=8)
        else:
            self.notify("no clipboard tool found (pbcopy, wl-copy, xclip or xsel); copy it from the dialog", severity="warning", timeout=8)
        self.push_screen(Nudge(text, copied=bool(clip)))

    def action_review(self):
        """Hand the card's PR to the configured review command as `<owner/repo> <number>`."""
        if not config.REVIEW_COMMAND:
            self.notify(f"no review command configured: set commands.review in {config.PATH}", severity="warning", timeout=8); return
        c = self.card()
        pr = (c or {}).get("pr")
        if not pr or pr.get("merged"):
            self.notify("no open PR on this card", severity="warning"); return
        m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr["url"])
        if not m:
            self.notify(f"cannot read a repo from {pr['url']}", severity="error"); return
        self.set_job(c, f"preparing a review of #{pr['number']}")
        self._review(dict(c), m.group(1), m.group(2))

    @work(thread=True, group="review")
    def _review(self, c, slug, num):
        r = sh(*config.REVIEW_COMMAND, slug, num)
        if r.returncode != 0:
            msg = (r.stderr.strip().splitlines() or ["review failed"])[-1]
            self.job(c, f"review failed: {msg}", "failed")
            alert(f"Review of #{num} failed", msg, "request")
            return
        self.job(c, None, None)
        alert(f"Review of #{num} is ready", f"{slug}#{num}")

    def action_fix(self):
        """Hand a PR's merge conflicts or red CI to an agent: the card's idle agent when it has
        one, else a new one in the card's worktree. Both problems at once go in one brief."""
        c = self.card()
        if not c:
            return
        if c.get("review_request"):
            self.notify("someone else's PR: v reviews it", severity="warning"); return
        text, why = pr_brief(c, fixes_only=True)
        if not text:
            self.notify(why, severity="warning"); return
        pr = c["pr"]
        agents = c.get("agents", [])
        free = next((a for a in agents if a["status"] in ("idle", "done")), None)
        if free:
            sh("herdr", "agent", "prompt", free["pane_id"], text)
            self.notify(f"sent the fix for #{pr['number']} to the {free['kind']} session here", timeout=10)
            self.load(); return
        if agents:
            self.notify(f"the session here is {agents[0]['status']}; press f again once it is idle", severity="warning"); return
        self.set_job(c, f"launching {config.AGENT_KIND} to fix #{pr['number']}")
        self._launch(dict(c), brief=text)

    def action_launch(self):
        c = self.card()
        if not c:
            return
        if c.get("review_request"):
            self.notify("someone else's PR: v reviews it", severity="warning"); return
        if not c["branch"]:
            self.notify("no branch on this card to launch", severity="warning"); return
        if c.get("orphan_issue"):
            def chosen(repo):
                if repo:
                    c2 = dict(c, repo=repo, create=True)
                    self.set_job(c2, f"creating {c['branch']} in {repo}")
                    self._launch(c2)
            self.push_screen(RepoPick(), chosen); return
        # A card with an open PR is follow-up work: the brief comes from the PR's state, not
        # from the issue, which the new-work brief treats as the start of the branch.
        brief, what = pr_brief(c) if c.get("pr") and not c["pr"].get("merged") else (None, None)
        self.set_job(c, f"launching {config.AGENT_KIND}" + (f" to {what} #{c['pr']['number']}" if brief else ""))
        self._launch(dict(c), brief=brief)

    @work(thread=True, group="launch")
    def _launch(self, c, brief=None):
        """Worktree, workspace, agent, brief, all in the background.

        The workspace opens without focus: `wt switch` can take minutes (its hooks install
        dependencies), and taking focus at the end would land your typing, in whatever you
        moved on to meanwhile, in the new session. Progress shows on the card; the end, or
        a trust prompt only you can answer, comes as a Herdr notification.
        """
        issue = c.get("issue")
        # A Linear branch name is long; the issue id is what you scan the sidebar for.
        label = issue["id"] if issue else c["branch"]

        def failed(msg, output=""):
            self.job(c, msg, "failed", detail=output)
            alert(f"{label}: launch failed", msg, "request")

        repo_root = os.path.join(config.REPOS_DIR, c["repo"])
        path = c["path"] if not c.get("create") and c.get("path") and os.path.isdir(c["path"]) else None
        # An existing worktree needs nothing from wt: its switch hooks (a fetch, for one) are
        # for arriving in a shell, and cost seconds, or minutes on a stalling network.
        if not path:
            self.job(c, "creating the worktree" if c.get("create") else "switching to the worktree")
            args = ["wt", "-C", repo_root, "switch", "--no-cd", "-y", "--format=json"] + (["-c"] if c.get("create") else []) + [c["branch"]]
            r = sh(*args)
            try:
                path = json.loads(r.stdout).get("path")
            except Exception:
                path = None
            if not path:
                return failed("wt switch failed: " + cause(r.stderr or r.stdout), (r.stderr or r.stdout).strip())
        self.job(c, "opening the workspace")
        o = sh("herdr", "worktree", "open", "--cwd", repo_root, "--path", path, "--label", label, "--no-focus", "--json")
        try:
            opened = json.loads(o.stdout)["result"]
            pane = opened["root_pane"]["pane_id"]
        except Exception:
            return failed("herdr worktree open failed: " + ((o.stderr or o.stdout).strip().splitlines() or ["no output"])[-1][:140])
        # From here Enter on the card goes to the workspace, whatever the job's state.
        self.job(c, "opening the workspace", workspace=opened["root_pane"]["workspace_id"])
        name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (issue["id"] if issue else c["branch"]).lower()).strip("-")[:28]
        if not name[:1].isalpha():
            name = "t-" + name
        self.job(c, f"starting {config.AGENT_KIND}")
        start = lambda p: sh("herdr", "agent", "start", name, "--kind", config.AGENT_KIND, "--pane", p)
        a = None
        if opened.get("already_open"):
            # A live workspace's first pane can be anything, such as a review pane opened
            # beside a session that has since closed. Its idle shells are tried once each,
            # since they are up already; with none free the agent gets a pane of its own.
            pane = None
            for p in self._shell_panes(opened["root_pane"]["workspace_id"]):
                if (a := start(p)).returncode == 0:
                    pane = p
                    break
            if pane is None:
                pane = self._split_for_agent(opened["root_pane"]["pane_id"], path)
                if not pane:
                    return failed("no free pane for the agent, and herdr pane split failed")
        if a is None or a.returncode != 0:
            # A new pane refuses an agent (agent_pane_busy) until its shell is up, a few
            # seconds after it opens; one attempt straight away would leave it without one.
            for _ in range(60):
                if (a := start(pane)).returncode == 0:
                    break
                time.sleep(0.5)
            else:
                return failed(f"{config.AGENT_KIND} did not start: " + ((a.stdout or a.stderr).strip().splitlines() or ["no output"])[-1][:140])
        # The new-work brief is for a branch with no PR yet; a merged one needs no brief at all.
        if not brief and issue and not c.get("pr"):
            fields = {"id": issue["id"], "title": issue["title"], "url": issue["url"], "branch": c["branch"]}
            brief = fill(config.BRIEF, config.DEFAULT_BRIEF, fields)
        if brief and not self._brief(c, label, name, brief):
            return
        doing = "working on its brief" if brief else "waiting for you"
        self.job(c, f"{config.AGENT_KIND} is {doing} · Enter opens it", "done")
        alert(f"{label} is ready", f"{config.AGENT_KIND} is {doing} in the {label} workspace.")
        self.load()

    def _shell_panes(self, ws):
        """The panes in a workspace that hold no agent, in layout order."""
        try:
            panes = json.loads(sh("herdr", "pane", "list").stdout)["result"]["panes"]
        except Exception:
            return []
        return [p["pane_id"] for p in panes if p["workspace_id"] == ws and not p.get("agent")]

    def _split_for_agent(self, beside, cwd):
        """A new pane for the agent, left of `beside` where the agent sits in a fresh
        workspace; None when the split fails. Herdr splits only right or down, so the new
        pane is swapped into place, best effort: a failed swap leaves it on the right."""
        r = sh("herdr", "pane", "split", beside, "--direction", "right", "--cwd", cwd, "--no-focus")
        try:
            new = json.loads(r.stdout)["result"]["pane"]["pane_id"]
        except Exception:
            return None
        sh("herdr", "pane", "swap", "--source-pane", new, "--target-pane", beside)
        return new

    def _brief(self, c, label, name, text):
        """Hand a fresh session its first prompt; False when it never became ready for one.

        The agent cannot take the brief on its command line (Herdr refuses arguments
        with newlines), and a new worktree opens on the folder-trust dialog, which
        Herdr reports as `blocked`. The trust answer is yours, so this asks for it once
        and submits the brief when the session reaches idle.
        """
        asked = False
        for _ in range(180):
            r = sh("herdr", "agent", "get", name)
            try:
                status = json.loads(r.stdout)["result"]["agent"]["agent_status"]
            except Exception:
                self.job(c, "the agent exited before it got its brief", "failed")
                return False
            if status in ("idle", "done"):
                sh("herdr", "agent", "prompt", name, text)
                return True
            if status == "blocked" and not asked:
                asked = True
                self.job(c, f"waiting for you: answer the trust prompt in {label}")
                alert(f"{label} needs you", "Answer the trust prompt so the agent can take its brief.", "request")
            time.sleep(2)
        self.job(c, "the agent was not ready within 6 minutes; the brief was not sent", "failed")
        alert(f"{label}: brief not sent", "The agent was not ready within 6 minutes.", "request")
        return False

    def action_close_ws(self):
        c = self.card()
        if c and not c.get("workspace_id"):
            self.notify("no open session on this card", severity="warning"); return
        if c and c.get("workspace_id"):
            sh("herdr", "workspace", "close", c["workspace_id"])
            self.notify(f"closed {c['workspace_id']}; sessions resume with claude --resume")
            self.load()

    def action_reap(self, force=False):
        c = self.card()
        if not c:
            self.notify("no card focused", severity="warning"); return
        if not c["path"]:
            self.notify("nothing to reap: this card has no worktree" + (" (close the session with c)" if c.get("workspace_id") else ""), severity="warning")
            return
        if force:
            dirty = sh("git", "-C", c["path"], "status", "--porcelain").stdout.splitlines()
            def go(yes):
                if yes:
                    self.set_job(c, f"force-removing {os.path.basename(c['path'])}")
                    self._reap(dict(c), force=True)
            self.push_screen(Confirm(f"Discard {len(dirty)} uncommitted change(s) and remove the worktree?", dirty[:12]), go)
            return
        self.set_job(c, f"removing {os.path.basename(c['path'])}")
        self._reap(dict(c))

    def action_reap_force(self):
        self.action_reap(force=True)

    @work(thread=True, group="reap")
    def _reap(self, c, force=False):
        repo_root = os.path.join(config.REPOS_DIR, c["repo"])
        if c.get("workspace_id"):
            sh("herdr", "workspace", "close", c["workspace_id"])
        args = ["wt", "-C", repo_root, "remove", "--foreground", "-y"] + (["--force"] if force else []) + [c["path"]]
        r = sh(*args)
        if r.returncode != 0:
            text = (r.stderr or r.stdout).strip()
            dirty = [l.strip() for l in text.splitlines() if l.strip()[:2] in ("??", " M", "M ", "A ", "D ", "MM")]
            if "uncommitted" in text and not force:
                msg = f"{os.path.basename(c['path'])} has {len(dirty)} uncommitted file(s): {', '.join(d.split()[-1] for d in dirty[:3])}{'…' if len(dirty) > 3 else ''}  —  X to force-remove"
            else:
                msg = f"wt remove failed: {text.splitlines()[0][:140] if text else 'unknown error'}"
            self.job(c, msg, "failed")
            return
        # worktrunk can return before the directory is fully deleted; give it up to a minute.
        deadline = time.time() + 60
        while os.path.isdir(c["path"]) and time.time() < deadline:
            time.sleep(1)
        if os.path.isdir(c["path"]):
            self.job(c, "wt remove returned but the directory is still there", "failed")
            return
        self.job(c, None, None)
        self.load()

    def action_tidy(self):
        """Preview the tidy command with `--dry-run`, then run it on confirmation.

        The command prints one `close <workspace>` line per workspace it closes (or would close).
        """
        if not config.TIDY_COMMAND:
            self.notify(f"no tidy command configured: set commands.tidy in {config.PATH}", severity="warning", timeout=8); return
        self.notify("checking which sessions are stale …", timeout=20)
        self._tidy_preview()

    @work(thread=True, group="tidy")
    def _tidy_preview(self):
        r = sh(*config.TIDY_COMMAND, "--dry-run")
        lines = [l for l in r.stdout.splitlines() if l.startswith("close ")]
        if not lines:
            self.call_from_thread(self.notify, "nothing stale to close", severity="information"); return
        items = [l[8:].strip() for l in lines]

        def go(yes):
            if yes:
                self.notify(f"closing {len(items)} workspaces …", timeout=20)
                self._tidy_run()
        self.call_from_thread(self.push_screen, Confirm(f"Close {len(items)} idle workspaces? Every session stays resumable.", items[:14]), go)

    @work(thread=True, group="tidy")
    def _tidy_run(self):
        r = sh(*config.TIDY_COMMAND)
        n = len([l for l in r.stdout.splitlines() if l.startswith("close ")])
        self.call_from_thread(self.notify, f"closed {n} workspaces" + (f" · resume lines in {config.TIDY_LOG}" if config.TIDY_LOG else ""), timeout=12)
        self.load()

    def action_goto_col(self, i):
        grid = self._grid()
        cols = list(self.query(Column))
        if i < len(cols):
            cols[i].scroll_visible()
        if i < len(grid) and grid[i]:
            self._enter_col(grid, i, 0)

    def action_toggle_drafts(self):
        self.show_drafts = not self.show_drafts
        self.render_board()

    def action_filter(self):
        inp = self.query_one("#filter", Input)
        inp.add_class("shown")
        inp.can_focus = True
        inp.focus()

    def action_clear_filter(self):
        inp = self.query_one("#filter", Input)
        inp.value = ""
        inp.remove_class("shown")
        inp.can_focus = False
        self.needle = ""
        self.render_board()
        # The columns were just rebuilt; the new cards can take focus only once mounted.
        self.call_after_refresh(lambda: (list(self.query(Card)) or [None])[0] and list(self.query(Card))[0].focus())

    def on_input_changed(self, event):
        # The "/" that opens the box also lands in it, at whatever moment the key event
        # reaches the newly focused input. Stripping it here catches it whenever it arrives;
        # no branch, ticket or title starts with a slash, so nothing real is lost.
        if event.value.startswith("/"):
            event.input.value = event.value.lstrip("/")
            return
        self.needle = event.value.strip().lower()
        self.render_board()

    def on_input_submitted(self, event):
        cards = list(self.query(Card))
        if cards:
            cards[0].focus()

    def action_details(self):
        c = self.card()
        if c:
            self.push_screen(Details(c))

    def action_help(self):
        self.push_screen(Help())

    def action_refresh(self):
        self.notify("refreshing…")
        self.load()


class Details(ModalScreen):
    """Everything the card knows, untruncated."""
    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("space", "dismiss", show=False), Binding("q", "dismiss", show=False)]
    DEFAULT_CSS = """
    Details { align: center middle; background: ansi_default 60%; }
    Details > Static { width: 90; height: auto; max-height: 90%; border: round $yellow; padding: 1 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self, c):
        super().__init__(); self.c = c

    def compose(self) -> ComposeResult:
        c = self.c; pr = c.get("pr"); issue = c.get("issue"); L = []
        L.append(f"[b]{escape(c['repo'])}[/b] · {escape(c['branch'] or '')}")
        if c.get("path"): L.append(f"[dim]worktree[/dim]  {escape(c['path'])}")
        if c.get("reason"): L.append(f"[$red]▶ {escape(c['reason'])}[/$red]")
        if c.get("dirty") or c.get("unpushed"):
            L.append(f"[$magenta]✎ {c.get('dirty', 0)} uncommitted · {c.get('unpushed', 0)} unpushed[/$magenta]")
        if issue:
            L += ["", f"[b]{link(issue['id'], issue['url'])}[/b]  {escape(issue['state'])}  [dim]{escape(issue['project'])}[/dim]", escape(issue["title"])]
        if pr:
            L += ["", f"[b]{link('#' + str(pr['number']), pr['url'])}[/b]  {'merged' if pr['merged'] else ('draft' if pr['draft'] else pr['review'] or 'open')}  CI {pr['checks'] or '–'}  {'[$red]merge conflicts[/$red]  ' if pr.get('conflicts') else ''}{pr['size']}",
                  escape(pr["title"]), f"[dim]updated {days(pr['updated_days'])} ago · opened {days(pr['created_days'])} ago[/dim]"]
            if pr["reviewers"]: L.append(f"[dim]reviewers:[/dim] {escape(', '.join(pr['reviewers']))}")
        for a in c.get("agents", []):
            L += ["", f"{STATUS_GLYPH.get(a['status'], '?')} {a['kind']} {a['status']} for {days(a['since_days'])}  [dim]{a['workspace_id']} {a['pane_id']}[/dim]", f"  {escape(a['title'])}"]
            if a.get("session"): L.append(f"  [dim]resume:[/dim] claude --resume {a['session']}")
        job = self.app.jobs.get(job_key(c)) or {}
        if job.get("state") == "failed":
            L += ["", f"[$red]✗ {escape(job['text'])}[/$red]"]
            # The command's own output, without the hook scripts it echoes as it runs them.
            L += [f"  [dim]{escape(l)}[/dim]" for l in (job.get("detail") or "").splitlines()
                  if l.strip() and not l.startswith("  ")][-12:]
        L += ["", "[dim]Esc / Space to close[/dim]"]
        yield Static("\n".join(L))


class RepoPick(ModalScreen):
    """Choose the repo for a ticket that has no worktree yet. j/k or digits, Enter picks, Esc cancels."""
    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("enter", "pick", "pick"),
                Binding("j", "move(1)", show=False), Binding("k", "move(-1)", show=False),
                Binding("down", "move(1)", show=False), Binding("up", "move(-1)", show=False)]
    DEFAULT_CSS = """
    RepoPick { align: center middle; background: ansi_default 60%; }
    RepoPick > Static { width: 50; height: auto; border: round $yellow; padding: 1 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self):
        super().__init__()
        dev = config.REPOS_DIR
        self.repos = sorted(d for d in os.listdir(dev) if os.path.isdir(f"{dev}/{d}/.git") and not os.path.isdir(f"{dev}/{d}/.jj"))
        self.idx = self.repos.index(config.DEFAULT_REPO) if config.DEFAULT_REPO in self.repos else 0

    def compose(self) -> ComposeResult:
        yield Static(self.text())

    def text(self):
        rows = [f"{'▶' if i == self.idx else ' '} {escape(r)}" for i, r in enumerate(self.repos)]
        return "[b]Which repo?[/b]\n\n" + "\n".join(rows) + "\n\n[dim]j/k · Enter · Esc[/dim]"

    def action_move(self, d):
        self.idx = max(0, min(len(self.repos) - 1, self.idx + d)); self.query_one(Static).update(self.text())

    def action_pick(self):
        self.dismiss(self.repos[self.idx])

    def action_cancel(self):
        self.dismiss(None)


class Nudge(ModalScreen):
    """The copied message, so it can be read before it is pasted."""
    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("q", "dismiss", show=False), Binding("n", "dismiss", show=False)]
    DEFAULT_CSS = """
    Nudge { align: center middle; background: ansi_default 60%; }
    Nudge > Static { width: 74; height: auto; border: round $green; padding: 1 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self, text, copied=True):
        super().__init__(); self.text = text; self.copied = copied

    def compose(self) -> ComposeResult:
        body = "\n".join(escape(l) for l in self.text.splitlines())
        head = "Copied to the clipboard" if self.copied else "No clipboard tool: select the text to copy it"
        yield Static(f"[b $green]{head}[/b $green]\n\n{body}\n\n[dim]Esc to close[/dim]")


class Confirm(ModalScreen):
    """Yes/no with the evidence in view. y or Enter confirms, anything else cancels."""
    BINDINGS = [Binding("y", "yes", "yes"), Binding("enter", "yes", show=False), Binding("n", "no", "no"), Binding("escape", "no", show=False)]
    DEFAULT_CSS = """
    Confirm { align: center middle; background: ansi_default 60%; }
    Confirm > Static { width: 80; height: auto; border: round $red; padding: 1 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self, question, items):
        super().__init__(); self.question = question; self.items = items

    def compose(self) -> ComposeResult:
        body = [f"[b $red]{escape(self.question)}[/b $red]", ""] + [f"  [dim]{escape(i)}[/dim]" for i in self.items] + ["", "[dim]y / Enter to confirm · n / Esc to cancel[/dim]"]
        yield Static("\n".join(body))

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class Help(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("question_mark", "dismiss", show=False), Binding("q", "dismiss", show=False)]
    DEFAULT_CSS = """
    Help { align: center middle; background: ansi_default 60%; }
    Help > Static { width: 78; height: auto; border: round $yellow; padding: 1 3; background: ansi_default; color: ansi_default; }
    """
    KEYS = [
        ("Move", None),
        ("j  k  ↑  ↓", "previous / next card"),
        ("h  l  ←  →", "previous / next column (board scrolls sideways)"),
        ("shift+wheel", "scroll the board sideways"),
        ("1 … 6", "jump to a column"),
        ("Look", None),
        ("Space", "details of the focused card"),
        ("/", "filter all columns as you type · Esc clears"),
        ("D", "show or hide drafts older than a week"),
        ("r", "refresh now"),
        ("T", "close stale sessions with the configured tidy command (asks first)"),
        ("Act", None),
        ("Enter", "focus the card's Herdr workspace"),
        ("o", "open the PR, or the Linear issue if there is no PR"),
        ("i", "open the Linear issue"),
        ("n", "copy a Slack nudge for this PR's reviewer"),
        ("v", "review this PR with the configured review command"),
        ("a", "launch an agent, briefed by the card: its issue, or its PR's state"),
        ("", "on a TODO card: pick a repo, create the worktree"),
        ("f", "hand merge conflicts or red CI to the idle agent here, or a new one"),
        ("c", "close the session (the agent's resume brings it back)"),
        ("x", "remove the worktree, close its session (refuses when dirty)"),
        ("X", "force-remove a dirty worktree after confirming what is discarded"),
        ("Mouse", None),
        ("click · double-click", "focus card · focus its workspace"),
        ("wheel · click on an id", "scroll · open PR or issue"),
        ("Columns", None),
        ("TODO", "assigned Linear issues with nothing local, by priority"),
        ("YOUR MOVE", "review asked of you · changes requested · CI red · merge conflicts"),
        ("", "agent blocked or done"),
        ("", "In Progress in Linear with nothing local · idle agent with no PR"),
        ("", "drafts older than a day, folded after a week"),
        ("WORKING", "an agent is running"),
        ("WAITING ON OTHERS", "a reviewer is asked, or has to look again after your push"),
        ("", "⚠ after 2 days, red after 5 · nobody asked, or comments to answer: YOUR MOVE"),
        ("MERGEABLE", "approved and GitHub can merge it now"),
        ("LANDED → REAP", "PR merged, worktree or session still here"),
    ]

    def compose(self) -> ComposeResult:
        lines = ["[b]Desk[/b]"]
        for key, desc in self.KEYS:
            if desc is None:
                lines += ["", f"[b $yellow]{key}[/b $yellow]"]
            else:
                lines.append(f"  [b]{escape(key):<24}[/b]{escape(desc)}")
        lines += ["", "[dim]Esc to close[/dim]"]
        yield Static("\n".join(lines))


def _pin_mouse_to_cells():
    """Keep mouse coordinates in cells under Herdr.

    Herdr emits an in-band window-resize report (CSI 48;…t) when a click focuses
    the pane, without honouring SGR-pixel mouse mode (?1016). Textual treats that
    report as proof of pixel support and starts dividing mouse coordinates by the
    cell size, which pins every later click and wheel event near (0,0). Forcing
    the parser to stay in cell mode, and not asking for pixel mode at all, keeps
    the board clickable after the first click.
    """
    from textual import _xterm_parser as _xp
    from textual.drivers import linux_driver as _ld
    _xp.XTermParser.mouse_pixels = property(lambda self: False, lambda self, v: None)
    _ld.LinuxDriver._enable_mouse_pixels = lambda self: None


def _follow_color_scheme(app):
    """Hand the terminal's colour scheme reports (CSI ?997;1n dark, ?997;2n light) to the app.

    Textual does not know these reports and would replay them as key presses.
    """
    from textual import _xterm_parser as _xp
    report = re.compile(r"\x1b\[\?997;([12])n")
    orig = _xp.XTermParser.feed
    def feed(self, data):
        for m in report.finditer(data):
            app.call_from_thread(app.set_dark, m[1] == "1")
        rest = report.sub("", data)
        # An empty feed means end of input to Textual, so a chunk that was only a report feeds nothing.
        return orig(self, rest) if rest or not data else ()
    _xp.XTermParser.feed = feed


if __name__ == "__main__":
    _pin_mouse_to_cells()
    if os.environ.get("DESK_TRACE_RAW"):
        # Log the raw terminal input bytes the parser sees, to compare with what Herdr sends.
        from textual import _xterm_parser as _xp
        _orig = _xp.XTermParser.feed
        def _feed(self, data):
            with open(f"{STATE_DIR}/raw.log", "a") as f:
                f.write(repr(data) + "\n")
            return _orig(self, data)
        _xp.XTermParser.feed = _feed
    app = Desk()
    _follow_color_scheme(app)
    app.run()
    sys.exit(app.return_code)
