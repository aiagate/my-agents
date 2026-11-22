#!/usr/bin/env sh
# Pipe-drive helper for the verify-my-agents skill.
#
# Usage: drive.sh <run-id> <name> <commands-file> [cli-flags...]
#
# Feeds each line of <commands-file> to the my-agents REPL as typed input,
# with an isolated MEMORY_PATH and --log-file under artifacts/verify/<run-id>/tmp/
# so runs never touch ./memory/ or the default state log. Saves the transcript
# (commands, stdout+stderr, exit code), the core.md snapshot, and the JSONL log
# under artifacts/verify/<run-id>/.
#
# Env: MY_AGENTS_BIN overrides the launch command (default: uv run my-agents).
#     Use the .venv/bin/my-agents absolute path when a sandbox blocks uv's
#     ~/.cache/uv writes.
set -eu

run_id=$1
name=$2
commands_file=$3
shift 3

my_agents=${MY_AGENTS_BIN:-uv run my-agents}

out_dir="artifacts/verify/$run_id"
tmp_dir="$out_dir/tmp"
memory_dir="$tmp_dir/$name-memory"
chats_db=${VERIFY_CHATS_DB_PATH:-$memory_dir/chats.db}
log_file="$tmp_dir/$name-log.jsonl"
transcript="$out_dir/$name.txt"

mkdir -p "$memory_dir"

{
    printf 'my-agents: %s\n' "$my_agents"
    printf 'cli-flags: %s\n' "$*"
    printf -- '--- commands ---\n'
    cat "$commands_file"
    printf '\n--- stdout/stderr ---\n'
} > "$transcript"

set +e
MEMORY_PATH="$memory_dir" CHATS_DB_PATH="$chats_db" $my_agents --log-file "$log_file" "$@" < "$commands_file" >> "$transcript" 2>&1
status=$?
set -e

printf '\nexit=%s\n' "$status" >> "$transcript"
cp "$memory_dir/core.md" "$out_dir/$name-core.md" 2>/dev/null || true
cp "$log_file" "$out_dir/$name-log.jsonl" 2>/dev/null || true
printf 'saved %s (exit %s)\n' "$transcript" "$status"
