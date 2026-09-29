"""Column rules of collect.place. Run: python3 test_collect.py"""
from collect import incomplete, place


def card(pr=None, agents=(), **extra):
    return {"repo": "api", "branch": "eng-1-x", "path": "/wt", "pr": pr, "agents": list(agents), **extra}


def pr(**over):
    return {"merged": False, "draft": False, "review": "REVIEW_REQUIRED", "checks": "pass", "conflicts": False,
            "created_days": 0.5, "mergeable": False, **over}


def agent(status, since_days=0.1):
    return {"status": status, "since_days": since_days}


assert place(card(agents=[agent("idle")])) == ("your_move", "waiting for prompt")
assert place(card(agents=[agent("idle", 3)])) == ("your_move", "waiting for prompt, idle 3d")
assert place(card(agents=[agent("idle"), agent("working")]))[0] == "working"
assert place(card(pr(), [agent("idle")])) == ("waiting", None)

assert place(card(pr(review="APPROVED", mergeable=True))) == ("mergeable", None)
assert place(card(pr(review="APPROVED", mergeable=True), [agent("working")]))[0] == "working"
assert place(card(pr(review="APPROVED", mergeable=True), [agent("blocked")])) == ("your_move", "agent needs you")
assert place(card(pr(review="APPROVED", checks="fail"))) == ("your_move", "checks failing")
assert place(card(pr(review="APPROVED"))) == ("waiting", None)

assert place(card(pr(conflicts=True))) == ("your_move", "merge conflicts")
assert place(card(pr(draft=True, created_days=3))) == ("your_move", "draft > 1d")
assert place(card(pr(draft=True, created_days=3, conflicts=True, checks="fail"))) == ("your_move", "draft > 1d · conflicts · CI red")
assert place(card(pr(draft=True, conflicts=True))) == ("waiting", None)
assert place(card(pr(draft=True, conflicts=True, created_days=3), [agent("blocked")])) == ("your_move", "agent needs you")

assert place(card(pr(author="alice"), path=None, review_request=True)) == ("your_move", "review requested by alice")

review = lambda by="erik", current=True: {"by": by, "state": "COMMENTED", "current": current}
assert place(card(pr(reviewers=[], reviews=[]))) == ("your_move", "no reviewer requested")
assert place(card(pr(reviewers=[], reviews=[review(), review("juliana")]))) == ("your_move", "reviewed by erik, juliana: reply to comments")
assert place(card(pr(reviewers=[], reviews=[review(current=False)]))) == ("waiting", None)
assert place(card(pr(reviewers=["erik"], reviews=[]))) == ("waiting", None)
assert place(card(pr(reviewers=[], reviews=[], review="APPROVED"))) == ("waiting", None)
assert place(card(pr(reviewers=[], reviews=[], draft=True))) == ("waiting", None)
assert place(card(pr(reviewers=[]))) == ("waiting", None)   # no reviews collected: no evidence

assert place(card(pr(merged=True))) == ("landed", None)
assert place(card(pr(merged=True), path=None)) == (None, None)
assert place(card()) == (None, None)
assert place(card(todo=True)) == ("todo", None)


def search(nodes, count, errors=None):
    return {"data": {"search": {"issueCount": count, "nodes": nodes}}, **({"errors": errors} if errors else {})}


assert incomplete(search([{}, {}], 2)) == ""
assert incomplete(search([{}] * 100, 140)) == ""   # past the page size: a limit, not a partial answer
assert incomplete(search([{}], 3)) == "partial result: 1 of 3 PRs"
assert incomplete(search([{}, None], 2)) == "partial result: 1 PR(s) did not resolve"
assert incomplete(search([{}], 1, [{"message": "timeout"}])) == "partial result: timeout"
print("ok")
