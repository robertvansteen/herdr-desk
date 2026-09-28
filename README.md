# Desk

A [Herdr](https://herdr.dev) plugin that shows your agent sessions, git worktrees, GitHub PRs and (optionally) Linear issues on one board. It sorts each branch into the column that says who has to act next:

| Column | A card is here when |
|---|---|
| TODO | a Linear issue is assigned to you, in a Todo state, and nothing local exists for it |
| YOUR MOVE | changes requested, CI red, merge conflicts, an agent is blocked or finished, a draft is older than a day, or an agent has idled 2 days without a PR |
| WORKING | an agent is running |
| WAITING ON OTHERS | a PR is open and waits for review |
| LANDED → REAP | the PR merged but its worktree or session still exists |

A card is one branch. For example, the branch `eng-142-retry-webhooks` with a worktree, a Claude session that is `blocked` and an open PR with green CI shows up as one card in YOUR MOVE with the reason "agent needs you". Press Enter to jump to that session's workspace.

## Requirements

- Herdr 0.9.0 or newer
- Python 3.11 or newer
- `git`, `jq`, `curl`
- [`gh`](https://cli.github.com), logged in: PRs come from `gh api graphql`
- [`wt` (worktrunk)](https://github.com/max-sixty/worktrunk): launching an agent (`a`) and removing a worktree (`x`) use it
- Optional: a Linear personal API key for the TODO column and for issue titles on cards

macOS and Linux are supported. On Linux, copying a nudge needs `wl-copy`, `xclip` or `xsel`.

## Install

```sh
herdr plugin install robertvansteen/herdr-desk
```

The install runs `install.sh`, which creates `.venv` with the pinned Textual version. To work from a local checkout, run `./install.sh` and then `herdr plugin link <path>`: `link` skips build steps.

Bind the `desk.open` action to a key in your Herdr config. The action opens the board in a workspace labelled "Desk", or focuses that workspace if it already exists.

## Configuration

Settings live in `~/.config/desk/config.toml`, or in the file `$DESK_CONFIG` points to. Every key is optional; [`config.example.toml`](config.example.toml) lists them with their defaults.

```toml
repos_dir = "~/code"          # its direct children are the repos the board reads
default_repo = "api"          # preselected when `a` on a TODO card asks for a repo

[linear]
ticket_prefixes = ["ENG"]     # default: every team key in your Linear workspace

[commands]
review = "bash ~/bin/review-pr.sh"   # `v`, called as: review-pr.sh <owner/repo> <number>
```

**Linear.** The key comes from `$LINEAR_API_KEY`, else from the output of `linear.key_command`. On macOS, the default command reads the keychain item with service `linear-api`:

```sh
security add-generic-password -s linear-api -a "$USER" -w <key>
```

Or let [linear-cli](https://github.com/nesszer/linear-cli) send the queries with its own login (API key, OAuth or keyring), and no key is needed here:

```toml
[linear]
cli = "linear-cli"
```

The workspace slug and the team keys come from Linear, so the key is all you need. A ticket ID such as `ENG-142` in a branch name or PR title links the card to its issue. Without a key, the board works with no TODO column and no issue links.

**Hooks.** `v` (review a PR) and `T` (close stale sessions) run commands that you supply. When a hook is not configured, its key shows a message saying so. The tidy command runs with `--dry-run` first, and must print one `close <workspace>` line per workspace it closes.

## Keys

Press `?` on the board for the full list. The main keys:

| Key | Action |
|---|---|
| `h` `j` `k` `l` or arrows | move between columns and cards |
| `Enter` | focus the card's Herdr workspace |
| `o` / `i` | open the PR / the Linear issue |
| `a` | start an agent in the card's worktree (on a TODO card, create the worktree first) |
| `n` | copy a short review request for the PR |
| `x` / `X` | remove the worktree and close its session / force-remove a dirty one after confirmation |
| `/` | filter every column as you type |

## How it works

`collect.py` joins the sources on `(repo, branch)` and writes `~/.local/state/desk/board.json`. `desk.py` is the Textual UI. It reads that file and runs the collector again every 5 minutes, or when you press `r`. The UI never changes state itself. Every action calls `herdr`, `gh`, `wt` or a configured hook.

Herdr does not record when an agent's status changed, so the collector records it in `state.json`. Ages such as "idle 3d" come from that file.

## Caveats

- `desk.py` patches `textual._xterm_parser`, which is private Textual code, to keep mouse coordinates in cells under Herdr. Textual is pinned in `requirements.txt` for this reason. Test the board after you change that pin.
- PR search asks for your PRs across all of GitHub (`author:@me`). A card only appears for a PR whose repo is cloned under `repos_dir`.

## License

MIT
