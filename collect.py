#!/usr/bin/env python3
"""Desk collector: joins Herdr agents, git worktrees and GitHub PRs into one
card per branch, then sorts the cards into four columns.

The board is a view over three sources that know nothing of each other:
  * Herdr   — which agent runs where and whether it is working, idle, blocked
              or done (finished and not yet looked at). Herdr has no timestamps,
              so this collector keeps its own `state.json` of when each pane
              last changed status; "idle 3d" comes from there.
  * git     — every worktree of every primary repo under `repos_dir`, keyed by
              branch, including colocated jujutsu repos (read-only here).
  * GitHub  — the user's open PRs and PRs merged in the last 7 days, per repo,
              with review decision, check status and requested reviewers.
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
  your_move — PR has changes requested, failing checks, merge conflicts, or is a draft older than
              a day; or an agent is `blocked` (needs an approval) or `done`
              (finished, unseen); or an agent idles > 2 days with no PR at all;
              or a Linear issue is In Progress with nothing local.
  working   — any agent in `working`.
  waiting   — PR open, review outstanding. Age since the last push is what the
              card shows, because that is how long a reviewer has had it.
  landed    — PR merged but a worktree or agent still exists: reaper territory.

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
import time

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


def worktrees(repo):
    out, _ = sh(["git", "worktree", "list", "--porcelain"], repo)
    for block in out.split("\n\n"):
        d = dict(line.partition(" ")[::2] for line in block.splitlines() if line)
        if d.get("worktree") and d["worktree"] != repo and "prunable" not in d:
            yield d["worktree"], d.get("branch", "").replace("refs/heads/", "")


PR_FIELDS = """number title url headRefName isDraft updatedAt createdAt mergedAt additions deletions
  repository { nameWithOwner }"""
# Review state, mergeability and checks are what makes the search slow enough for GitHub to
# answer 504, so only open PRs, where they decide the column, ask for them.
OPEN_FIELDS = PR_FIELDS + """ reviewDecision mergeable mergeStateStatus
  reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"""
SEARCH = 'query($q: String!) { search(query: $q, type: ISSUE, first: 100) { nodes { ... on PullRequest { %s } } } }'
GITHUB_ERROR = f"{STATE_DIR}/github-error.txt"


def search_prs(q, fields):
    """(nodes, error) for one PR search; error is "" on success, else gh's own message.

    Tried twice: the failures seen in practice (HTTP 504 on the heavy search, TCP dial
    timeouts) are transient, and a second attempt usually succeeds.
    """
    error = ""
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
            return json.loads(r.stdout)["data"]["search"]["nodes"], ""
        except Exception:
            lines = [l.strip() for l in (r.stderr or r.stdout).splitlines() if l.strip()]
            error = (lines[-1] if lines else f"gh exited {r.returncode}").removeprefix("gh: ")
    return [], error


def all_prs():
    """All of the user's open PRs plus PRs merged in the last 7 days, in two GraphQL calls
    total rather than two per repo. Returns (prs_by_repo_slug, error); error is "" on success,
    else the reason GitHub refused, and the caller then keeps the previous snapshot. The
    reason is also written to github-error.txt with the time it happened."""
    since = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    by_slug, errors = {}, []
    for q, fields in (("is:pr author:@me is:open archived:false", OPEN_FIELDS),
                      (f"is:pr author:@me is:merged merged:>={since} archived:false", PR_FIELDS)):
        nodes, error = search_prs(q, fields)
        if error:
            errors.append(error)
            with open(GITHUB_ERROR, "a") as f:
                f.write(f"{dt.datetime.now():%FT%T} {q}: {error}\n")
            continue
        for n in nodes:
            if not n:
                continue
            roll = ((n.get("commits") or {}).get("nodes") or [{}])[0].get("commit", {}).get("statusCheckRollup") or {}
            n["statusCheckRollup"] = [{"state": roll.get("state", "")}] if roll else []
            n["reviewRequests"] = [r.get("requestedReviewer") or {} for r in (n.get("reviewRequests") or {}).get("nodes", [])]
            by_slug.setdefault(n["repository"]["nameWithOwner"].lower(), []).append(n)
    return by_slug, "; ".join(errors)


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
    return (now() - t) / 86400


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


def collect():
    os.makedirs(STATE_DIR, exist_ok=True)
    state = load_state()
    agents = herdr_agents(state)
    json.dump(state, open(STATE, "w"))

    # Linear first: its team keys decide which strings on branches and PR titles are tickets.
    issues, teams, workspace = linear_issues()
    pattern = ticket_pattern(config.TICKET_PREFIXES or teams)

    def ticket(text):
        m = pattern.search(text or "") if pattern else None
        return m[1] if m else None

    repos = list(primaries())
    cards = {}

    def card(repo_name, branch):
        return cards.setdefault((repo_name, branch), {
            "repo": repo_name, "branch": branch, "path": None, "pr": None, "agents": [], "workspace_id": None,
            "ticket": ticket(branch),
        })

    for p, _ in repos:
        name = os.path.basename(p)
        for path, branch in worktrees(p):
            c = card(name, branch or f"detached@{os.path.basename(path)}")
            c["path"] = path
            dirty, _ = sh(["git", "status", "--porcelain"], path)
            ahead, rc = sh(["git", "rev-list", "--count", "@{u}..HEAD"], path)
            c["dirty"] = len(dirty.splitlines()) if dirty else 0
            c["unpushed"] = int(ahead) if rc == 0 and ahead.isdigit() else 0

    previous_conflicts = {}
    try:
        for col in json.load(open(BOARD))["columns"].values():
            for c in col:
                if c.get("pr"):
                    previous_conflicts[(c["repo"], c["branch"])] = c["pr"].get("conflicts")
    except Exception:
        pass
    prs_by_slug, github_error = all_prs()
    if github_error:
        # GitHub refused (a 504 on the search, a network timeout, or the rate limit). Reuse the
        # PRs from the last board so cards do not vanish; the board shows the reason as stale data.
        try:
            prev = json.load(open(BOARD))
            for col in prev["columns"].values():
                for c in col:
                    if c.get("pr"):
                        card(c["repo"], c["branch"])["pr"] = c["pr"]
        except Exception:
            pass
    if True:
        for p, slugs in repos:
            name = os.path.basename(p)
            for pr in [pr for sl in dict.fromkeys(x.lower() for x in slugs) for pr in prs_by_slug.get(sl, [])]:
                c = card(name, pr["headRefName"])
                c["pr"] = {
                    "number": pr["number"], "title": pr["title"], "url": pr["url"], "draft": pr["isDraft"],
                    "review": pr.get("reviewDecision") or "", "checks": checks(pr),
                    "reviewers": [r.get("login") or r.get("name") for r in pr.get("reviewRequests") or []],
                    "updated_days": iso_age_days(pr.get("updatedAt")), "created_days": iso_age_days(pr.get("createdAt")),
                    "merged": bool(pr.get("mergedAt")), "size": f'+{pr.get("additions",0)} -{pr.get("deletions",0)}',
                    # GitHub computes mergeability lazily: UNKNOWN means "not yet", not "clean",
                    # so the previous snapshot's answer is kept until GitHub commits to one.
                    "conflicts": (previous_conflicts.get((name, pr["headRefName"])) if pr.get("mergeable") == "UNKNOWN"
                                  else pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "DIRTY"),
                }
                if not c["ticket"]:
                    c["ticket"] = ticket(pr["title"])

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
            "kind": a["agent"], "status": a["agent_status"], "since_days": (now() - a["since"]) / 86400,
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
    for k, i in linear_lookup(sorted(missing)).items():
        for c in cards.values():
            if (c.get("ticket") or "").upper() == k:
                c["issue"] = i; c["ticket"] = i["id"]
    for k, i in issues.items():
        if k not in claimed:
            # Linear-only card: In Progress ones land in your_move, Todo ones in the todo column.
            cards[("linear", k)] = {"repo": i["project"] or "linear", "branch": i["branch"], "path": None, "pr": None,
                                    "agents": [], "workspace_id": None, "ticket": i["id"], "issue": i, "orphan_issue": True,
                                    "todo": i["state_type"] != "started"}

    columns = {"todo": [], "your_move": [], "working": [], "waiting": [], "landed": []}
    for c in cards.values():
        pr, ag = c["pr"], c["agents"]
        statuses = {a["status"] for a in ag}
        idle_days = max((a["since_days"] for a in ag if a["status"] == "idle"), default=0)
        reason = None
        if c.get("todo"):
            columns["todo"].append(c); continue
        if pr and not pr["merged"] and (pr["review"] == "CHANGES_REQUESTED"):
            reason = "changes requested"
        elif pr and not pr["merged"] and pr["checks"] == "fail":
            reason = "checks failing"
        elif pr and not pr["merged"] and pr.get("conflicts"):
            reason = "merge conflicts"
        elif "blocked" in statuses:
            reason = "agent needs you"
        elif "done" in statuses:
            reason = "agent finished"
        elif pr and not pr["merged"] and pr["draft"] and (pr["created_days"] or 0) > 1:
            reason = "draft > 1d"
        elif not pr and ag and idle_days > 2 and "working" not in statuses:
            reason = f"no PR, idle {idle_days:.0f}d"
        elif c.get("dirty", 0) >= 3 and (ag or pr) and "working" not in statuses:
            # Uncommitted work only counts as yours to move when a session or PR makes it live.
            # A bare worktree with stray files is wt-reap's review list, not a card.
            reason = f"{c['dirty']} uncommitted file(s)"
        elif c.get("orphan_issue"):
            reason = f"{c['issue']['state']} in Linear, nothing local"
        if reason:
            c["reason"] = reason
            columns["your_move"].append(c)
        elif "working" in statuses:
            columns["working"].append(c)
        elif pr and not pr["merged"]:
            columns["waiting"].append(c)
        elif pr and pr["merged"] and (c["path"] or ag):
            columns["landed"].append(c)
        elif ag:
            columns["waiting"].append(c)  # idle agent, no PR yet, under 2 days
        # else: bare worktree with nothing attached, not shown

    URGENCY = ["changes requested", "checks failing", "merge conflicts", "agent needs you", "uncommitted", "agent finished", "in linear", "no pr", "draft"]

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
             "counts": {k: len(v) for k, v in columns.items()}, "agents": len(agents)}
    json.dump(board, open(BOARD, "w"))
    return board


if __name__ == "__main__":
    b = collect()
    if "--print" in sys.argv:
        print(json.dumps(b, indent=1))
    else:
        print(json.dumps(b["counts"]))
