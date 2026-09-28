#!/usr/bin/env python3
"""Desk collector: joins Herdr agents, git worktrees and GitHub PRs into one
card per branch, then sorts the cards into columns.

The board is a view over three sources that know nothing of each other:
  * Herdr   — which agent runs where and whether it is working, idle, blocked
              or done (finished and not yet looked at). Herdr has no timestamps,
              so this collector keeps its own `state.json` of when each pane
              last changed status; "idle 3d" comes from there.
  * git     — every worktree of every primary repo under `repos_dir`, keyed by
              branch, including colocated jujutsu repos (read-only here).
  * GitHub  — the user's open PRs and PRs merged in the last 7 days, per repo,
              with review decision, check status and requested reviewers; and
              other people's open PRs that request the user's review.
  * Linear  — issues assigned to the user that are started or in review, read
              with a personal API key (see config.py for where it comes from).
              Optional: with no key, cards simply carry no issue.

Linear issues join a card by identifier: the ENG-123 in a branch name or PR
title, or the issue's own `branchName`. An identifier is a team key, a dash and
a number; the team keys are the configured `ticket_prefixes`, else every team
in the Linear workspace, so without either no identifier is recognised. An In Progress issue with nothing local
(no worktree, session or PR) becomes its own card in your_move, because Linear
says you are working on it and nothing here shows it.

Join key is (repo, branch). A card exists when the branch has a worktree, an
agent, or an open/recently merged PR. Worktrees with neither agent nor PR are
left to wt-reap and do not appear.

Column rules, first match wins:
  todo      — an assigned Linear issue in a Todo state with nothing local. `a`
              on it asks for a repo and starts a worktree on Linear's branch name.
  your_move — someone asked you to review their PR; or a ready PR
              has changes requested, failing checks or merge conflicts;
              or an agent is `blocked` (needs an approval) or `done` (finished,
              unseen); or a PR is a draft older than a day; or an agent is idle
              with no PR at all, waiting for its next prompt; or a Linear issue
              is In Progress with nothing local. A draft's conflicts and red CI
              are named on its draft reason, not ranked with a ready PR's:
              nobody can merge a draft, so they block no one yet.
  working   — any agent in `working`.
  mergeable — PR approved and GitHub says it can merge now (mergeStateStatus CLEAN).
  waiting   — PR open, review outstanding. Age since the last push is what the
              card shows, because that is how long a reviewer has had it.
  landed    — PR merged but a worktree or agent still exists: reaper territory.

Each source refreshes on its own interval in a background thread (see the Source
instances below): Herdr on every call, worktrees every 5s, git status every 30s,
Linear every minute, GitHub every 2 minutes and not at all while its GraphQL budget
is nearly spent. A call returns at once with the latest answer from each source.

Output: board.json in the state directory (~/.local/state/desk), also printed to
stdout with --print.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

import config
from config import BOARD, STATE_DIR

DEV = config.REPOS_DIR
STATE = f"{STATE_DIR}/state.json"
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")


def ticket_pattern(keys):
    """A regex matching KEY-123 for any of the team keys, case-insensitively, or None without keys."""
    if not keys:
        return None
    alternatives = "|".join(re.escape(k) for k in sorted(set(keys), key=len, reverse=True))
    return re.compile(rf"\b((?:{alternatives})-\d+)\b", re.I)


def sh(args, cwd=None, timeout=60):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.returncode
    except Exception:
        return "", 1


def now():
    return time.time()


def primaries():
    """(path, slugs) per primary repo, where slugs is the owner/name of every remote.

    Every remote, not just origin: a clone made before its repo moved to another
    owner can keep origin on the old owner and carry the live repo as a second
    remote, and PRs are keyed by the live repo's slug. URLs are read through git's
    insteadOf rewrites (`ls-remote --get-url`), so a host alias such as
    github-work still yields <owner>/<name>.
    """
    for d in sorted(os.listdir(DEV)):
        p = f"{DEV}/{d}"
        # Colocated jujutsu repos are included: this only reads. The branch-deleting
        # reaper is what has to leave them alone.
        if not os.path.isdir(f"{p}/.git"):
            continue
        names, _ = sh(["git", "remote"], p)
        slugs = []
        for r in names.split():
            url, rc = sh(["git", "ls-remote", "--get-url", r], p)
            m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url.strip()) if rc == 0 else None
            if m:
                slugs.append(m.group(1))
        if slugs:
            yield p, slugs


def git_status(paths):
    """{worktree path: (uncommitted files, unpushed commits)}, eight worktrees at a time:
    one by one, a hundred worktrees take half a minute."""
    def one(path):
        dirty, _ = sh(["git", "status", "--porcelain"], path)
        ahead, rc = sh(["git", "rev-list", "--count", "@{u}..HEAD"], path)
        return path, (len(dirty.splitlines()), int(ahead) if rc == 0 and ahead.strip().isdigit() else 0)
    with cf.ThreadPoolExecutor(8) as pool:
        return dict(pool.map(one, paths))


def worktrees(repo):
    out, _ = sh(["git", "worktree", "list", "--porcelain"], repo)
    for block in out.split("\n\n"):
        d = dict(line.partition(" ")[::2] for line in block.splitlines() if line)
        if d.get("worktree") and d["worktree"] != repo and "prunable" not in d:
            yield d["worktree"], d.get("branch", "").replace("refs/heads/", "")


PR_FIELDS = """number title url headRefName baseRefName isDraft updatedAt createdAt mergedAt additions deletions
  repository { nameWithOwner }"""
# Review state, mergeability and checks are what makes the search slow enough for GitHub to
# answer 504, so only open PRs, where they decide the column, ask for them.
OPEN_FIELDS = PR_FIELDS + """ reviewDecision mergeable mergeStateStatus
  reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"""
# PRs others asked you to review: no review state or checks, since those decide nothing for you.
REVIEW_FIELDS = PR_FIELDS + " author { login }"
SEARCH_LIMIT = 100
SEARCH = ('query($q: String!) { rateLimit { remaining resetAt } '
          'search(query: $q, type: ISSUE, first: %d) { issueCount nodes { ... on PullRequest { %%s } } } }' % SEARCH_LIMIT)
GITHUB_ERROR = f"{STATE_DIR}/github-error.txt"
# GraphQL points left untouched for everything else on the account (5000 an hour in all).
GITHUB_RESERVE = 1000


def incomplete(response):
    """Why a search response that parsed is still short of PRs, or "" when it is whole.

    GitHub can answer 200 with a partial result: an `errors` list beside the data, null
    entries where a PR failed to resolve, or fewer nodes than `issueCount`. Taken as
    complete, every PR it left out would drop off the board until the next refresh.
    """
    search = response["data"]["search"]
    nodes = search["nodes"]
    if response.get("errors"):
        return "partial result: " + (response["errors"][0].get("message") or "unknown error")
    if any(n is None for n in nodes):
        return f"partial result: {sum(n is None for n in nodes)} PR(s) did not resolve"
    expected = min(search.get("issueCount") or 0, SEARCH_LIMIT)
    if len(nodes) < expected:
        return f"partial result: {len(nodes)} of {expected} PRs"
    return ""


def search_prs(q, fields):
    """(nodes, error) for one PR search; error is "" on success, else the reason it failed.

    Tried twice: the failures seen in practice (HTTP 504 on the heavy search, TCP dial
    timeouts, partial results) are transient, and a second attempt usually succeeds. After
    two partial results the PRs that did resolve are still returned, with the error.
    """
    error, partial = "", []
    for _ in range(2):
        try:
            r = subprocess.run(["gh", "api", "graphql", "-f", f"q={q}", "-f", f"query={SEARCH % fields}"],
                               capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            error = "gh timed out after 45s"
            continue
        except OSError as e:
            return [], f"gh could not run: {e}"
        try:
            response = json.loads(r.stdout)
            nodes = response["data"]["search"]["nodes"]
        except Exception:
            lines = [l.strip() for l in (r.stderr or r.stdout).splitlines() if l.strip()]
            error = (lines[-1] if lines else f"gh exited {r.returncode}").removeprefix("gh: ")
            continue
        limit = response["data"].get("rateLimit") or {}
        if limit.get("remaining", GITHUB_RESERVE) < GITHUB_RESERVE:
            GITHUB.hold = dt.datetime.fromisoformat(limit["resetAt"].replace("Z", "+00:00")).timestamp()
        error = incomplete(response)
        if not error:
            return nodes, ""
        partial = [n for n in nodes if n]
    return partial, error


def all_prs():
    """All of the user's open PRs plus PRs merged in the last 7 days, in two GraphQL calls
    total rather than two per repo, and the open PRs that ask the user for a review (by name,
    or also through a team with `github.team_review_requests`).
    Returns (prs_by_repo_slug, review_requests, error); error is "" on success,
    else the reason GitHub refused or answered short, and the caller then keeps the previous
    snapshot under whatever PRs did arrive. The reason is also written to github-error.txt
    with the time it happened."""
    since = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    by_slug, reviews, errors = {}, [], []
    # user-review-requested asks for requests to you by name; review-requested adds those to
    # every team you are on, which on a large team is most of its PRs.
    review_q = "review-requested" if config.TEAM_REVIEW_REQUESTS else "user-review-requested"
    for q, fields in (("is:pr author:@me is:open archived:false", OPEN_FIELDS),
                      (f"is:pr author:@me is:merged merged:>={since} archived:false", PR_FIELDS),
                      (f"is:pr {review_q}:@me is:open archived:false", REVIEW_FIELDS)):
        nodes, error = search_prs(q, fields)
        if error:
            errors.append(error)
            with open(GITHUB_ERROR, "a") as f:
                f.write(f"{dt.datetime.now():%FT%T} {q}: {error}\n")
        if fields is REVIEW_FIELDS:
            reviews += [n for n in nodes if n]
            continue
        for n in nodes:
            if not n:
                continue
            roll = ((n.get("commits") or {}).get("nodes") or [{}])[0].get("commit", {}).get("statusCheckRollup") or {}
            n["statusCheckRollup"] = [{"state": roll.get("state", "")}] if roll else []
            n["reviewRequests"] = [r.get("requestedReviewer") or {} for r in (n.get("reviewRequests") or {}).get("nodes", [])]
            by_slug.setdefault(n["repository"]["nameWithOwner"].lower(), []).append(n)
    return by_slug, reviews, "; ".join(errors)


def checks(pr):
    roll = pr.get("statusCheckRollup") or []
    states = {(c.get("conclusion") or c.get("state") or "").upper() for c in roll}
    if not states:
        return ""
    if states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
        return "fail"
    if states & {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", ""}:
        return "pending"
    return "pass"


def iso_age_days(s):
    if not s:
        return None
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    return round((now() - t) / 86400, 2)


LINEAR_QUERY = """
query { organization { urlKey } teams(first: 250) { nodes { key } }
  viewer { assignedIssues(first: 100, filter: { state: { type: { in: ["started", "unstarted"] } } }) {
  nodes { identifier title url branchName priority updatedAt
          state { name type } project { name } } } } }
"""


def linear_graphql(query, variables=None):
    """(stdout, returncode) of one Linear GraphQL call, or None when Linear is not configured.

    Through `linear.cli` when set (nesszer/linear-cli, which holds the credentials,
    OAuth included), else straight to the API with the key. Both print the API's
    own {"data": ...} response.
    """
    if config.LINEAR_CLI:
        var_args = [a for k, v in (variables or {}).items() for a in ("-v", f"{k}={json.dumps(v)}")]
        return sh([*config.LINEAR_CLI, "api", "query", "-o", "json", "-q", query, *var_args], timeout=30)
    key = config.linear_key()
    if not key:
        return None
    body = {"query": query, **({"variables": variables} if variables else {})}
    return sh(["curl", "-sS", "--max-time", "20", "https://api.linear.app/graphql", "-H", "Content-Type: application/json",
               "-H", f"Authorization: {key}", "-d", json.dumps(body)], timeout=30)


def linear_issues():
    """(issues, team_keys, workspace) from one Linear call.

    issues is {IDENTIFIER: issue} for the user's started/unstarted issues; team_keys
    and workspace (the URL slug) are what identifiers and issue links are built from.
    All empty without a key or on error.
    """
    r = linear_graphql(LINEAR_QUERY)
    if r is None:
        return {}, [], ""
    out, rc = r
    try:
        data = json.loads(out)["data"]
        nodes = data["viewer"]["assignedIssues"]["nodes"]
    except Exception:
        # The response (never the key) is kept for diagnosis; Linear's errors are explicit.
        with open(f"{STATE_DIR}/linear-error.txt", "w") as f:
            f.write(f"rc={rc}\n{out[:2000]}\n")
        return {}, [], ""
    teams = [t["key"] for t in (data.get("teams") or {}).get("nodes", []) if t.get("key")]
    workspace = (data.get("organization") or {}).get("urlKey") or ""
    return {n["identifier"].upper(): {
        "id": n["identifier"], "title": n["title"], "url": n["url"], "branch": n.get("branchName") or "",
        "state": n["state"]["name"], "state_type": n["state"]["type"], "priority": n.get("priority") or 0,
        "project": (n.get("project") or {}).get("name") or "",
    } for n in nodes}, teams, workspace


def linear_lookup(identifiers):
    """Fetch specific issues by identifier (any state, any assignee), for tickets named on
    branches that are not in the user's open assigned set: usually Done or someone else's."""
    if not identifiers:
        return {}
    clauses = []
    for ident in identifiers:
        team, _, num = ident.upper().partition("-")
        if num.isdigit():
            clauses.append({"and": [{"team": {"key": {"eq": team}}}, {"number": {"eq": int(num)}}]})
    q = """query($f: IssueFilter) { issues(first: 50, filter: $f) {
      nodes { identifier title url branchName priority state { name type } project { name } } } }"""
    r = linear_graphql(q, {"f": {"or": clauses}})
    if r is None:
        return {}
    out, _ = r
    try:
        nodes = json.loads(out)["data"]["issues"]["nodes"]
    except Exception:
        return {}
    return {n["identifier"].upper(): {
        "id": n["identifier"], "title": n["title"], "url": n["url"], "branch": n.get("branchName") or "",
        "state": n["state"]["name"], "state_type": n["state"]["type"], "priority": n.get("priority") or 0,
        "project": (n.get("project") or {}).get("name") or "",
    } for n in nodes}


_looked_up = {}


def linear_lookup_once(identifiers):
    """linear_lookup, asking Linear only about identifiers it has not been asked about yet."""
    new = [i for i in identifiers if i not in _looked_up]
    if new:
        _looked_up.update(dict.fromkeys(new))
        _looked_up.update(linear_lookup(new))
    return {i: _looked_up[i] for i in identifiers if _looked_up.get(i)}


class Source:
    """One input to the board, refreshed on its own schedule. A read returns the last value at
    once and, when that is `every` seconds old, refreshes it in a background thread, so a slow
    source never holds up a fast one. No refresh starts before `hold`, a rate limit's reset."""

    def __init__(self, fetch, every):
        self.fetch, self.every = fetch, every
        self.value, self.at, self.hold = None, 0.0, 0.0
        self._thread, self._lock = None, threading.Lock()

    def get(self, wait=False):
        """The latest value, None before the first; `wait` blocks for the first instead."""
        with self._lock:
            if not (self._thread and self._thread.is_alive()) and now() >= max(self.at + self.every, self.hold):
                self._thread = threading.Thread(target=self._refresh, daemon=True)
                self._thread.start()
        if wait and self.value is None and self._thread:
            self._thread.join()
        return self.value

    def _refresh(self):
        try:
            self.value = self.fetch()
        except Exception:
            # Not to stderr: under the board that is the screen. The last value stays.
            with open(f"{STATE_DIR}/collect-error.txt", "w") as f:
                traceback.print_exc(file=f)
        finally:
            self.at = now()


REPOS = Source(lambda: [(p, slugs, list(worktrees(p))) for p, slugs in primaries()], 5)
STATUS = Source(lambda: git_status([path for _, _, wts in REPOS.value for path, _ in wts]), 30)
GITHUB = Source(all_prs, 120)
LINEAR = Source(linear_issues, 60)


def refresh():
    """Make every source due now (`r` on the board); a rate-limit hold still applies."""
    for s in (REPOS, STATUS, GITHUB, LINEAR):
        s.at = 0
    _looked_up.clear()


def place(c):
    """(column, reason) for one card, by the rules in the module docstring; column is None for
    a card that is not shown, and reason is set only for your_move."""
    pr, ag = c["pr"], c["agents"]
    statuses = {a["status"] for a in ag}
    idle_days = max((a["since_days"] for a in ag if a["status"] == "idle"), default=0)
    reason = None
    if c.get("todo"):
        return "todo", None
    if c.get("review_request"):
        return "your_move", f"review requested by {c['pr']['author']}"
    ready = pr and not pr["merged"] and not pr["draft"]
    if ready and pr["review"] == "CHANGES_REQUESTED":
        reason = "changes requested"
    elif ready and pr["checks"] == "fail":
        reason = "checks failing"
    elif ready and pr.get("conflicts"):
        reason = "merge conflicts"
    elif "blocked" in statuses:
        reason = "agent needs you"
    elif "done" in statuses:
        reason = "agent finished"
    elif pr and not pr["merged"] and pr["draft"] and (pr["created_days"] or 0) > 1:
        # The suffixes avoid the URGENCY keys, so the card ranks as a draft.
        reason = " · ".join(["draft > 1d"] + (["conflicts"] if pr.get("conflicts") else [])
                            + (["CI red"] if pr["checks"] == "fail" else []))
    elif not pr and "idle" in statuses and "working" not in statuses:
        reason = "waiting for prompt" + (f", idle {idle_days:.0f}d" if idle_days >= 1 else "")
    elif c.get("dirty", 0) >= 3 and (ag or pr) and "working" not in statuses:
        # Uncommitted work only counts as yours to move when a session or PR makes it live.
        # A bare worktree with stray files is wt-reap's review list, not a card.
        reason = f"{c['dirty']} uncommitted file(s)"
    elif c.get("orphan_issue"):
        reason = f"{c['issue']['state']} in Linear, nothing local"
    if reason:
        return "your_move", reason
    if "working" in statuses:
        return "working", None
    if pr and not pr["merged"] and pr.get("mergeable"):
        return "mergeable", None
    if pr and not pr["merged"]:
        return "waiting", None
    if pr and pr["merged"] and (c["path"] or ag):
        return "landed", None
    if ag:
        return "waiting", None  # agent in a state Herdr cannot classify, no PR
    return None, None


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def herdr_agents(state):
    out, rc = sh(["herdr", "agent", "list"])
    agents = []
    if rc == 0 and out.strip():
        try:
            agents = json.loads(out)["result"]["agents"]
        except Exception:
            agents = []
    seen = set()
    for a in agents:
        pid = a["pane_id"]
        seen.add(pid)
        st = state.get(pid)
        if not st or st["status"] != a["agent_status"]:
            state[pid] = {"status": a["agent_status"], "since": now()}
        a["since"] = state[pid]["since"]
        a["session"] = (a.get("agent_session") or {}).get("value")
    for pid in list(state):
        if pid not in seen:
            del state[pid]
    return agents


def collect(wait=False):
    """The board, or None while a source has yet to answer; `wait` blocks until all have."""
    os.makedirs(STATE_DIR, exist_ok=True)
    repos = REPOS.get(wait)
    # git status runs over the worktrees REPOS found, so it waits for them.
    status = STATUS.get(wait) if repos is not None else None
    fetched = GITHUB.get(wait), LINEAR.get(wait)
    if None in (repos, status, *fetched):
        return None
    (prs_by_slug, review_prs, github_error), (issues, teams, workspace) = fetched

    state = load_state()
    agents = herdr_agents(state)
    json.dump(state, open(STATE, "w"))

    # Linear first: its team keys decide which strings on branches and PR titles are tickets.
    pattern = ticket_pattern(config.TICKET_PREFIXES or teams)

    def ticket(text):
        m = pattern.search(text or "") if pattern else None
        return m[1] if m else None

    cards = {}

    def card(repo_name, branch):
        return cards.setdefault((repo_name, branch), {
            "repo": repo_name, "branch": branch, "path": None, "pr": None, "agents": [], "workspace_id": None,
            "ticket": ticket(branch),
        })

    for p, _, wts in repos:
        name = os.path.basename(p)
        for path, branch in wts:
            c = card(name, branch or f"detached@{os.path.basename(path)}")
            c["path"] = path
            c["dirty"], c["unpushed"] = status.get(path, (0, 0))

    previous_conflicts = {}
    try:
        for col in json.load(open(BOARD))["columns"].values():
            for c in col:
                if c.get("pr"):
                    previous_conflicts[(c["repo"], c["branch"])] = c["pr"].get("conflicts")
    except Exception:
        pass
    if github_error:
        # GitHub refused or answered short (a 504, a network timeout, the rate limit, a partial
        # result). Reuse the PRs from the last board so cards do not vanish; the PRs that did
        # arrive overwrite them below, and the board shows the reason as stale data.
        try:
            prev = json.load(open(BOARD))
            for col in prev["columns"].values():
                for c in col:
                    if c.get("review_request"):
                        cards[("review", c["pr"]["url"])] = c
                    elif c.get("pr"):
                        card(c["repo"], c["branch"])["pr"] = c["pr"]
        except Exception:
            pass
    if True:
        for p, slugs, _ in repos:
            name = os.path.basename(p)
            for pr in [pr for sl in dict.fromkeys(x.lower() for x in slugs) for pr in prs_by_slug.get(sl, [])]:
                c = card(name, pr["headRefName"])
                c["pr"] = {
                    "number": pr["number"], "title": pr["title"], "url": pr["url"], "draft": pr["isDraft"],
                    "base": pr.get("baseRefName") or "", "review": pr.get("reviewDecision") or "", "checks": checks(pr),
                    "reviewers": [r.get("login") or r.get("name") for r in pr.get("reviewRequests") or []],
                    "updated_days": iso_age_days(pr.get("updatedAt")), "created_days": iso_age_days(pr.get("createdAt")),
                    "merged": bool(pr.get("mergedAt")), "size": f'+{pr.get("additions",0)} -{pr.get("deletions",0)}',
                    # GitHub computes mergeability lazily: UNKNOWN means "not yet", not "clean",
                    # so the previous snapshot's answer is kept until GitHub commits to one.
                    "conflicts": (previous_conflicts.get((name, pr["headRefName"])) if pr.get("mergeable") == "UNKNOWN"
                                  else pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "DIRTY"),
                    # GitHub's own verdict under branch protection, plus an approval: repos
                    # without required reviews report CLEAN for PRs nobody has looked at.
                    "mergeable": not pr["isDraft"] and pr.get("reviewDecision") == "APPROVED"
                                 and pr.get("mergeStateStatus") in ("CLEAN", "HAS_HOOKS"),
                }
                if not c["ticket"]:
                    c["ticket"] = ticket(pr["title"])

    # A review request is its own card: the branch is someone else's, so it never joins a
    # worktree or session of yours, and the repo needs no clone for the review command.
    local = {sl.lower(): os.path.basename(p) for p, slugs, _ in repos for sl in slugs}
    for pr in review_prs:
        slug = pr["repository"]["nameWithOwner"]
        cards[("review", pr["url"])] = {
            "repo": local.get(slug.lower(), slug.split("/")[-1]), "branch": pr["headRefName"], "path": None,
            "agents": [], "workspace_id": None, "ticket": ticket(pr["headRefName"]) or ticket(pr["title"]),
            "review_request": True,
            "pr": {"number": pr["number"], "title": pr["title"], "url": pr["url"], "draft": pr["isDraft"],
                   "base": pr.get("baseRefName") or "", "review": "", "checks": "", "reviewers": [], "conflicts": False,
                   "mergeable": False, "merged": False, "author": (pr.get("author") or {}).get("login") or "someone",
                   "updated_days": iso_age_days(pr.get("updatedAt")), "created_days": iso_age_days(pr.get("createdAt")),
                   "size": f'+{pr.get("additions",0)} -{pr.get("deletions",0)}'},
        }

    # Attach agents by cwd: the deepest worktree path that prefixes the agent's cwd.
    paths = sorted(((c["path"], k) for k, c in cards.items() if c["path"]), key=lambda x: -len(x[0]))
    for a in agents:
        home = next((k for path, k in paths if a["cwd"] == path or a["cwd"].startswith(path + "/")), None)
        if home is None:
            # Agent in a primary checkout or elsewhere: its own card, keyed by directory.
            home = (os.path.basename(a["cwd"]), "")
            cards.setdefault(home, {"repo": home[0], "branch": "", "path": a["cwd"], "pr": None, "agents": [], "workspace_id": None, "ticket": None})
        c = cards[home]
        c["agents"].append({
            "kind": a["agent"], "status": a["agent_status"], "since_days": round((now() - a["since"]) / 86400, 2),
            "pane_id": a["pane_id"], "workspace_id": a["workspace_id"], "title": a.get("terminal_title_stripped", ""),
            "session": a["session"],
        })
        c["workspace_id"] = c["workspace_id"] or a["workspace_id"]

    by_branch = {i["branch"]: k for k, i in issues.items() if i["branch"]}
    claimed = set()
    for c in cards.values():
        k = (c["ticket"] or "").upper() or by_branch.get(c["branch"] or "")
        if k in issues:
            c["issue"] = issues[k]; c["ticket"] = issues[k]["id"]; claimed.add(k)
    missing = {(c["ticket"] or "").upper() for c in cards.values() if c.get("ticket") and not c.get("issue")}
    for k, i in linear_lookup_once(sorted(missing)).items():
        for c in cards.values():
            if (c.get("ticket") or "").upper() == k:
                c["issue"] = i; c["ticket"] = i["id"]
    for k, i in issues.items():
        if k not in claimed:
            # Linear-only card: In Progress ones land in your_move, Todo ones in the todo column.
            cards[("linear", k)] = {"repo": i["project"] or "linear", "branch": i["branch"], "path": None, "pr": None,
                                    "agents": [], "workspace_id": None, "ticket": i["id"], "issue": i, "orphan_issue": True,
                                    "todo": i["state_type"] != "started"}

    columns = {"todo": [], "your_move": [], "working": [], "waiting": [], "mergeable": [], "landed": []}
    for c in cards.values():
        key, reason = place(c)
        if reason:
            c["reason"] = reason
        if key:
            columns[key].append(c)
        # else: bare worktree with nothing attached, not shown

    URGENCY = ["changes requested", "checks failing", "merge conflicts", "agent needs you", "review requested", "uncommitted", "agent finished", "in linear", "waiting for prompt", "draft"]

    def urgency(c):
        """Approved PRs first: one rebase or one CI fix from merging, they are the cheapest
        work on the board to finish. Then by reason, then the longest-waiting first."""
        r = (c.get("reason") or "").lower()
        rank = next((i for i, key in enumerate(URGENCY) if key in r), len(URGENCY))
        pr = c.get("pr") or {}
        approved = pr.get("review") == "APPROVED" and not pr.get("merged")
        return (0 if approved else 1, rank, -(pr.get("updated_days") or 0))

    def age(c):
        return -(c["pr"]["updated_days"] if c["pr"] and c["pr"]["updated_days"] is not None else 0)

    for k in columns:
        columns[k].sort(key=urgency if k == "your_move" else age)
    # Linear priority: 1 urgent … 4 low, 0 none; none sorts last.
    columns["todo"].sort(key=lambda c: (c["issue"]["priority"] or 9, c["issue"]["id"]))
    for c in columns["your_move"]:
        pr = c.get("pr")
        c["stale_draft"] = bool(pr and pr["draft"] and not pr["merged"] and (pr["created_days"] or 0) > 7 and c.get("reason", "").startswith("draft"))
    board = {"generated": now(), "columns": columns, "linear": bool(issues),
             "linear_workspace": config.LINEAR_WORKSPACE or workspace, "github_ok": not github_error, "github_error": github_error,
             "github_at": GITHUB.at, "github_hold": GITHUB.hold if GITHUB.hold > now() else 0,
             "counts": {k: len(v) for k, v in columns.items()}, "agents": len(agents)}
    json.dump(board, open(BOARD, "w"))
    return board


if __name__ == "__main__":
    b = collect(wait=True)
    if "--print" in sys.argv:
        print(json.dumps(b, indent=1))
    else:
        print(json.dumps(b["counts"]))
