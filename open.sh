#!/usr/bin/env bash
# The board lives in one workspace labelled "Desk" and runs as an ordinary
# terminal pane (not a plugin pane) so Herdr forwards mouse input to it the same
# way it does for any full-screen app. Pressing the key focuses the workspace
# when it exists and creates it otherwise.
herdr=${HERDR_BIN_PATH:-herdr}
root=${HERDR_PLUGIN_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}
ws=$("$herdr" workspace list | jq -r '.result.workspaces[] | select(.label=="Desk") | .workspace_id' | head -1)
if [[ -n $ws ]]; then
  exec "$herdr" workspace focus "$ws"
fi
created=$("$herdr" workspace create --cwd "$HOME" --label Desk --focus)
ws=$(jq -r '.result.root_pane.workspace_id' <<<"$created")
pane=$(jq -r '.result.root_pane.pane_id' <<<"$created")
"$herdr" pane rename "$pane" Desk >/dev/null 2>&1
# `exec` replaces the shell so quitting the board closes the pane, and the shell
# does not swallow the first frames before the app takes over.
"$herdr" pane run "$pane" "exec '$root/desk'" >/dev/null
exec "$herdr" workspace focus "$ws"
