#!/usr/bin/env python3
"""Desk: a four-column board over the cards collect.py produces.

Columns left to right: YOUR MOVE, WORKING, WAITING ON OTHERS, LANDED → REAP.
Left/right moves between columns, up/down between cards. Actions act on the
focused card and shell out to `herdr`, `gh`, `wt` and the configured hooks; the board never
mutates state itself. Data refreshes in a background thread every 60s or on `r`,
so gh latency never blocks the UI.
"""
import glob
import json
import re
import os
import subprocess
import sys
import threading
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
COLUMNS = [("todo", "TODO", "bright_magenta"), ("your_move", "YOUR MOVE", "bright_red"), ("working", "WORKING", "bright_green"),
           ("waiting", "WAITING ON OTHERS", "bright_yellow"), ("landed", "LANDED → REAP", "bright_blue")]
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


def has_claude_history(path):
    """Whether Claude Code saved a conversation for this directory; `claude --continue` exits without one.

    Claude keeps a directory's transcripts under projects/<the path with every non-alphanumeric as ->.
    """
    if config.AGENT_KIND != "claude":
        return False
    root = os.environ.get("CLAUDE_CONFIG_DIR") or f"{config.HOME}/.claude"
    return bool(glob.glob(f"{root}/projects/{re.sub(r'[^A-Za-z0-9]', '-', path)}/*.jsonl"))


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
    Card:focus { border: heavy ansi_bright_yellow; }
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
        prio = {1: "[ansi_bright_red]!![/ansi_bright_red] ", 2: "[ansi_bright_yellow]![/ansi_bright_yellow] "}.get(issue["priority"], "") if issue else ""
        title = (issue and issue["title"]) or (pr and pr["title"]) or next((a["title"] for a in agents if a.get("title")), "") \
            or (c["branch"].replace("detached@", "") if c["branch"] else os.path.basename(c["path"] or "~"))
        lines.append(f"{prio}[b ansi_bright_white]{escape(fit(title, 2 * w - 4))}[/b ansi_bright_white]")

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

        # 3. Status. The words carry the colour; the reason line below is dropped when it would repeat them.
        said = set()
        if pr:
            if pr["merged"]:
                state = "[ansi_bright_blue]merged[/ansi_bright_blue]"; said.add("merged")
            elif pr["draft"]:
                state = "[dim]draft[/dim]"; said.add("draft")
            else:
                state = {"CHANGES_REQUESTED": "[ansi_bright_red]changes requested[/ansi_bright_red]", "APPROVED": "[ansi_bright_green]approved[/ansi_bright_green]",
                         "REVIEW_REQUIRED": "[ansi_bright_yellow]review requested[/ansi_bright_yellow]"}.get(pr["review"], "open")
                if pr["review"] == "CHANGES_REQUESTED": said.add("changes requested")
            chk = {"fail": "[ansi_bright_red]✗ CI[/ansi_bright_red]", "pass": "[ansi_bright_green]✓ CI[/ansi_bright_green]", "pending": "[ansi_bright_yellow]… CI[/ansi_bright_yellow]"}.get(pr["checks"], "")
            if pr["checks"] == "fail": said.add("checks failing")
            conflict = "[ansi_bright_red]⚡ conflicts[/ansi_bright_red]" if pr.get("conflicts") and not pr["merged"] else ""
            if conflict: said.add("merge conflicts")
            age_d = pr["updated_days"] or 0
            age_col = "bright_red" if age_d > 5 and not pr["merged"] else ("bright_yellow" if age_d > 2 and not pr["merged"] else "dim")
            parts = [state, chk, conflict, f"[{age_col}]{days(pr['updated_days'])}[/{age_col}]"]
            if pr["reviewers"] and not pr["merged"]:
                parts.append(f"[dim]→ {escape(fit(', '.join(pr['reviewers']), max(10, w - 34)))}[/dim]")
            lines.append("  ".join(x for x in parts if x))

        # 4. Sessions.
        for a in agents:
            g = STATUS_GLYPH.get(a["status"], "?")
            col = {"working": "bright_green", "blocked": "bright_red", "done": "bright_cyan"}.get(a["status"], "dim")
            extra = f"  [dim]{escape(fit(a['title'], w - 22))}[/dim]" if a.get("title") and a["title"] != title and len(agents) > 1 else ""
            lines.append(f"[{col}]{g} {a['kind']} {a['status']}[/{col}] [dim]{days(a['since_days'])}[/dim]{extra}")

        # 4b. Unfinished work sitting in the worktree.
        if c.get("dirty") or c.get("unpushed"):
            bits = []
            if c.get("dirty"):
                bits.append(f"{c['dirty']} uncommitted")
            if c.get("unpushed"):
                bits.append(f"{c['unpushed']} unpushed")
            lines.append(f"[ansi_bright_magenta]✎ {escape(', '.join(bits))}[/ansi_bright_magenta]")

        # 5. Reason, only when the status line has not already said it.
        reason = c.get("reason")
        if reason and "uncommitted" in reason and (c.get("dirty") or c.get("unpushed")):
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
    """


class Board(HorizontalScroll, inherit_bindings=False):
    """The row of columns, scrolled sideways by focus and the mouse only; see Lane."""


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
    Screen { layout: vertical; background: ansi_default; color: ansi_default; }
    #board { height: 1fr; background: ansi_default; scrollbar-color: ansi_bright_black; scrollbar-background: ansi_default; scrollbar-size-horizontal: 1; }
    #filter { display: none; height: 3; background: ansi_default; color: ansi_bright_white; border: round ansi_bright_yellow; }
    #filter.shown { display: block; }
    Header { background: ansi_default; color: ansi_bright_white; }
    Footer { background: ansi_default; }
    Footer > .footer--key, FooterKey { background: ansi_default; color: ansi_bright_yellow; }
    FooterKey .footer-key--key { background: ansi_default; color: ansi_bright_yellow; }
    FooterKey .footer-key--description { background: ansi_default; color: ansi_default; }
    Toast { background: ansi_default; color: ansi_bright_white; border: round ansi_bright_black; }
    """
    TITLE = "Desk"
    BINDINGS = [
        Binding("enter", "focus_ws", "focus workspace"),
        Binding("o", "open_pr", "open PR"),
        Binding("i", "open_issue", "open issue"),
        Binding("n", "nudge", "copy nudge"),
        Binding("v", "review", "review PR"),
        Binding("a", "launch", "launch agent"),
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
        Binding("5", "goto_col(4)", show=False),
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

    def on_mount(self):
        self.load(from_disk=True)
        # 5 minutes: GitHub's GraphQL budget is shared with every other tool and agent on this account.
        self.set_interval(300, self.load)

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
        focused = self.focused.data["path"] if isinstance(self.focused, Card) else None
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
        cards = list(self.query(Card))
        target = next((c for c in cards if c.data["path"] == focused), cards[0] if cards else None)
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
        if c and c.get("workspace_id"):
            sh("herdr", "workspace", "focus", c["workspace_id"])
        elif c and c.get("path") and c.get("branch"):
            self.notify(f"reopening {os.path.basename(c['path'])} …", timeout=20)
            self._launch(dict(c), resume=True)
        elif c:
            self.notify("no worktree here — press a to launch one", severity="warning")

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
        self.notify(f"preparing a review of #{pr['number']} …", timeout=20)
        self._review(m.group(1), m.group(2))

    @work(thread=True, group="review")
    def _review(self, slug, num):
        r = sh(*config.REVIEW_COMMAND, slug, num)
        if r.returncode != 0:
            msg = (r.stderr.strip().splitlines() or ["review failed"])[-1]
            self.call_from_thread(self.notify, msg, severity="error", timeout=12)

    def action_launch(self):
        c = self.card()
        if not c:
            return
        if not c["branch"]:
            self.notify("no branch on this card to launch", severity="warning"); return
        if c.get("orphan_issue"):
            def chosen(repo):
                if repo:
                    c2 = dict(c, repo=repo, create=True)
                    self.notify(f"creating {c['branch']} in {repo} …", timeout=30)
                    self._launch(c2)
            self.push_screen(RepoPick(), chosen); return
        self.notify(f"launching {config.AGENT_KIND} on {c['branch']} …", timeout=20)
        self._launch(dict(c))

    @work(thread=True, group="launch")
    def _launch(self, c, resume=False):
        """Open the card's worktree in Herdr and start an agent there. With `resume`, the
        worktree already exists and the agent continues its last conversation when it has one."""
        notify = lambda *a, **k: self.call_from_thread(self.notify, *a, **k)
        repo_root = os.path.join(config.REPOS_DIR, c["repo"])
        if resume:
            path = c["path"]
        else:
            args = ["wt", "-C", repo_root, "switch", "--no-cd", "-y", "--format=json"] + (["-c"] if c.get("create") else []) + [c["branch"]]
            r = sh(*args)
            try:
                path = json.loads(r.stdout).get("path") or c["path"]
            except Exception:
                path = c["path"]
        if not path:
            notify("wt switch failed", severity="error"); return
        issue = c.get("issue")
        # A Linear branch name is long; the issue id is what you scan the sidebar for.
        label = issue["id"] if issue else c["branch"]
        o = sh("herdr", "worktree", "open", "--cwd", repo_root, "--path", path, "--label", label, "--focus", "--json")
        try:
            pane = json.loads(o.stdout)["result"]["root_pane"]["pane_id"]
        except Exception:
            notify("herdr worktree open failed", severity="error"); return
        name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (issue["id"] if issue else c["branch"]).lower()).strip("-")[:28]
        if not name[:1].isalpha():
            name = "t-" + name
        continuing = resume and has_claude_history(path)
        sh("herdr", "agent", "start", name, "--kind", config.AGENT_KIND, "--pane", pane, *(["--", "--continue"] if continuing else []))
        if issue and not continuing:
            notify(f"{issue['id']} worktree ready; the agent gets the issue once it is past the trust prompt", timeout=12)
            self._brief(name, issue, c["branch"])
        self.load()

    def _brief(self, name, issue, branch):
        """Hand a fresh session its Linear issue as the first prompt, from the `agent.brief` template.

        The agent cannot take the brief on its command line (Herdr refuses arguments
        with newlines), and a new worktree opens on the folder-trust dialog, which
        Herdr reports as `blocked`. The trust answer is yours, so this waits for
        the session to reach idle and only then submits the brief.
        """
        import time
        fields = {"id": issue["id"], "title": issue["title"], "url": issue["url"], "branch": branch}
        try:
            text = config.BRIEF.format(**fields)
        except (KeyError, IndexError, ValueError):
            # A template naming an unknown field would otherwise leave the session without its issue.
            text = config.DEFAULT_BRIEF.format(**fields)
        for _ in range(180):
            r = sh("herdr", "agent", "get", name)
            try:
                status = json.loads(r.stdout)["result"]["agent"]["agent_status"]
            except Exception:
                return
            if status in ("idle", "done"):
                sh("herdr", "agent", "prompt", name, text)
                return
            time.sleep(2)

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
                    self.notify(f"force-removing {os.path.basename(c['path'])} …", timeout=30)
                    self._reap(dict(c), force=True)
            self.push_screen(Confirm(f"Discard {len(dirty)} uncommitted change(s) and remove the worktree?", dirty[:12]), go)
            return
        self.notify(f"reaping {os.path.basename(c['path'])} …", timeout=30)
        self._reap(dict(c))

    def action_reap_force(self):
        self.action_reap(force=True)

    @work(thread=True, group="reap")
    def _reap(self, c, force=False):
        import time
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
            self.call_from_thread(self.notify, msg, severity="error", timeout=15)
            return
        # worktrunk can return before the directory is fully deleted; give it up to a minute.
        deadline = time.time() + 60
        while os.path.isdir(c["path"]) and time.time() < deadline:
            time.sleep(1)
        gone = not os.path.isdir(c["path"])
        self.call_from_thread(self.notify, f"reaped {os.path.basename(c['path'])}" if gone else "wt remove returned but the directory is still there",
                              severity="information" if gone else "error", timeout=10)
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
    Details > Static { width: 90; height: auto; max-height: 90%; border: round ansi_bright_yellow; padding: 1 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self, c):
        super().__init__(); self.c = c

    def compose(self) -> ComposeResult:
        c = self.c; pr = c.get("pr"); issue = c.get("issue"); L = []
        L.append(f"[b]{escape(c['repo'])}[/b] · {escape(c['branch'] or '')}")
        if c.get("path"): L.append(f"[dim]worktree[/dim]  {escape(c['path'])}")
        if c.get("reason"): L.append(f"[ansi_bright_red]▶ {escape(c['reason'])}[/ansi_bright_red]")
        if c.get("dirty") or c.get("unpushed"):
            L.append(f"[ansi_bright_magenta]✎ {c.get('dirty', 0)} uncommitted · {c.get('unpushed', 0)} unpushed[/ansi_bright_magenta]")
        if issue:
            L += ["", f"[b]{link(issue['id'], issue['url'])}[/b]  {escape(issue['state'])}  [dim]{escape(issue['project'])}[/dim]", escape(issue["title"])]
        if pr:
            L += ["", f"[b]{link('#' + str(pr['number']), pr['url'])}[/b]  {'merged' if pr['merged'] else ('draft' if pr['draft'] else pr['review'] or 'open')}  CI {pr['checks'] or '–'}  {'[ansi_bright_red]merge conflicts[/ansi_bright_red]  ' if pr.get('conflicts') else ''}{pr['size']}",
                  escape(pr["title"]), f"[dim]updated {days(pr['updated_days'])} ago · opened {days(pr['created_days'])} ago[/dim]"]
            if pr["reviewers"]: L.append(f"[dim]reviewers:[/dim] {escape(', '.join(pr['reviewers']))}")
        for a in c.get("agents", []):
            L += ["", f"{STATUS_GLYPH.get(a['status'], '?')} {a['kind']} {a['status']} for {days(a['since_days'])}  [dim]{a['workspace_id']} {a['pane_id']}[/dim]", f"  {escape(a['title'])}"]
            if a.get("session"): L.append(f"  [dim]resume:[/dim] claude --resume {a['session']}")
        L += ["", "[dim]Esc / Space to close[/dim]"]
        yield Static("\n".join(L))


class RepoPick(ModalScreen):
    """Choose the repo for a ticket that has no worktree yet. j/k or digits, Enter picks, Esc cancels."""
    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("enter", "pick", "pick"),
                Binding("j", "move(1)", show=False), Binding("k", "move(-1)", show=False),
                Binding("down", "move(1)", show=False), Binding("up", "move(-1)", show=False)]
    DEFAULT_CSS = """
    RepoPick { align: center middle; background: ansi_default 60%; }
    RepoPick > Static { width: 50; height: auto; border: round ansi_bright_yellow; padding: 1 2; background: ansi_default; color: ansi_default; }
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
    Nudge > Static { width: 74; height: auto; border: round ansi_bright_green; padding: 1 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self, text, copied=True):
        super().__init__(); self.text = text; self.copied = copied

    def compose(self) -> ComposeResult:
        body = "\n".join(f"[ansi_bright_white]{escape(l)}[/ansi_bright_white]" if l else "" for l in self.text.splitlines())
        head = "Copied to the clipboard" if self.copied else "No clipboard tool: select the text to copy it"
        yield Static(f"[b ansi_bright_green]{head}[/b ansi_bright_green]\n\n{body}\n\n[dim]Esc to close[/dim]")


class Confirm(ModalScreen):
    """Yes/no with the evidence in view. y or Enter confirms, anything else cancels."""
    BINDINGS = [Binding("y", "yes", "yes"), Binding("enter", "yes", show=False), Binding("n", "no", "no"), Binding("escape", "no", show=False)]
    DEFAULT_CSS = """
    Confirm { align: center middle; background: ansi_default 60%; }
    Confirm > Static { width: 80; height: auto; border: round ansi_bright_red; padding: 1 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self, question, items):
        super().__init__(); self.question = question; self.items = items

    def compose(self) -> ComposeResult:
        body = [f"[b ansi_bright_red]{escape(self.question)}[/b ansi_bright_red]", ""] + [f"  [dim]{escape(i)}[/dim]" for i in self.items] + ["", "[dim]y / Enter to confirm · n / Esc to cancel[/dim]"]
        yield Static("\n".join(body))

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class Help(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "close"), Binding("question_mark", "dismiss", show=False), Binding("q", "dismiss", show=False)]
    DEFAULT_CSS = """
    Help { align: center middle; background: ansi_default 60%; }
    Help > Static { width: 78; height: auto; border: round ansi_bright_yellow; padding: 1 3; background: ansi_default; color: ansi_default; }
    """
    KEYS = [
        ("Move", None),
        ("j  k  ↑  ↓", "previous / next card"),
        ("h  l  ←  →", "previous / next column (board scrolls sideways)"),
        ("shift+wheel", "scroll the board sideways"),
        ("1  2  3  4  5", "jump to a column"),
        ("Look", None),
        ("Space", "details of the focused card"),
        ("/", "filter all columns as you type · Esc clears"),
        ("D", "show or hide drafts older than a week"),
        ("r", "refresh now"),
        ("T", "close stale sessions with the configured tidy command (asks first)"),
        ("Act", None),
        ("Enter", "focus the card's Herdr workspace · reopen a closed one, resuming its session"),
        ("o", "open the PR, or the Linear issue if there is no PR"),
        ("i", "open the Linear issue"),
        ("n", "copy a Slack nudge for this PR's reviewer"),
        ("v", "review this PR with the configured review command"),
        ("a", "launch an agent here · on a TODO card: pick a repo, create the worktree"),
        ("c", "close the session (the agent's resume brings it back)"),
        ("x", "remove the worktree, close its session (refuses when dirty)"),
        ("X", "force-remove a dirty worktree after confirming what is discarded"),
        ("Mouse", None),
        ("click · double-click", "focus card · focus its workspace"),
        ("wheel · click on an id", "scroll · open PR or issue"),
        ("Columns", None),
        ("TODO", "assigned Linear issues with nothing local, by priority"),
        ("YOUR MOVE", "changes requested · CI red · merge conflicts · agent blocked or done"),
        ("", "In Progress in Linear with nothing local · no PR after 2 idle days"),
        ("", "drafts older than a day, folded after a week"),
        ("WORKING", "an agent is running"),
        ("WAITING ON OTHERS", "review outstanding · ⚠ after 2 days, red after 5"),
        ("LANDED → REAP", "PR merged, worktree or session still here"),
    ]

    def compose(self) -> ComposeResult:
        lines = ["[b]Desk[/b]"]
        for key, desc in self.KEYS:
            if desc is None:
                lines += ["", f"[b ansi_bright_yellow]{key}[/b ansi_bright_yellow]"]
            else:
                lines.append(f"  [ansi_bright_white]{escape(key):<24}[/ansi_bright_white]{escape(desc)}")
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
    Desk().run()
