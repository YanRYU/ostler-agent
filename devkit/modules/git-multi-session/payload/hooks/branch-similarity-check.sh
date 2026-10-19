#!/usr/bin/env bash
# Enforces: rules/git-hygiene.md (branch overlap)
# PreToolUse hook for Bash: before creating a new git branch, surface existing
# branches that might overlap — by exact name, token similarity, or squash-merged
# (which git's --merged check misses).
#
# Non-blocking: prints to stderr only. exit 0 always. Set BRANCH_CHECK_DISABLED=1
# to silence per-session.
#
# Trunk-name detection handles repos whose origin/HEAD points at a stale branch.
# Override the candidate trunk list with DEVKIT_TRUNK_CANDIDATES (space-separated).

set -u

[ "${BRANCH_CHECK_DISABLED:-0}" = "1" ] && exit 0

input=$(cat 2>/dev/null || echo "")
[ -z "$input" ] && exit 0

tool=$(echo "$input" | jq -r '.tool_name // ""' 2>/dev/null)
[ "$tool" = "Bash" ] || exit 0

cmd=$(echo "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)
[ -z "$cmd" ] && exit 0

# Extract proposed branch name from one of:
#   git checkout -b NAME [start]   |   git switch -c NAME [start]
#   git branch NAME [start]        (NAME must not start with -)
proposed=""
if [[ "$cmd" =~ git[[:space:]]+(checkout|switch)[[:space:]]+(-[bc]|--orphan)[[:space:]]+([^[:space:]\;\&\|]+) ]]; then
    proposed="${BASH_REMATCH[3]}"
elif [[ "$cmd" =~ git[[:space:]]+branch[[:space:]]+([^[:space:]\-][^[:space:]]*)([[:space:]]|$) ]]; then
    proposed="${BASH_REMATCH[1]}"
fi

[ -z "$proposed" ] && exit 0

repo=$(git rev-parse --show-toplevel 2>/dev/null || true)
[ -z "$repo" ] && exit 0

# Resolve default branch for squash-merge detection.
# `symbolic-ref refs/remotes/origin/HEAD` can point at a stale/legacy branch, so
# pick the candidate with the most-recent commit among known trunk names.
TRUNK_CANDIDATES="${DEVKIT_TRUNK_CANDIDATES:-main master trunk develop}"
default=""
default_ts=0
for c in $TRUNK_CANDIDATES; do
    if git -C "$repo" show-ref --verify --quiet "refs/remotes/origin/$c"; then
        ts=$(git -C "$repo" log -1 --format=%ct "origin/$c" 2>/dev/null || echo 0)
        if [ "${ts:-0}" -gt "${default_ts:-0}" ]; then
            default="$c"
            default_ts="$ts"
        fi
    fi
done
# Fall back to symbolic-ref if nothing matched
if [ -z "$default" ]; then
    ref=$(git -C "$repo" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null || true)
    [ -n "$ref" ] && default="${ref#refs/remotes/origin/}"
fi

# All branches (local + remote, deduped, sans origin/HEAD)
branches=$(git -C "$repo" for-each-ref --format='%(refname:short)' \
    refs/heads refs/remotes/origin 2>/dev/null \
    | grep -v '^origin/HEAD$' \
    | sed 's|^origin/||' \
    | sort -u)

[ -z "$branches" ] && exit 0

# ─── 1. Exact-match ──────────────────────────────────────────────────────
if echo "$branches" | grep -qFx "$proposed"; then
    last=$(git -C "$repo" log -1 --format='%cr by %an' "$proposed" 2>/dev/null \
        || git -C "$repo" log -1 --format='%cr by %an' "origin/$proposed" 2>/dev/null \
        || echo 'unknown')
    {
        echo ""
        echo "[branch-similarity-check] '$proposed' ALREADY EXISTS in $repo"
        echo "  Last activity: $last"
        echo "  Resume with:   git checkout $proposed"
        echo ""
    } >&2
fi

# ─── 2. Token-similarity (≥2 shared, after stopword filter) ──────────────
# Generic conventional-commit prefixes don't carry domain signal — strip them
# so `feat/foo` and `feat/bar` don't appear similar just for sharing `feat`.
STOPWORDS='^(feat|fix|chore|docs|refactor|test|perf|style|build|ci|revert|hotfix|bug|bugfix|wip|tmp|temp|draft|scratch|pr)$'
tokenize() {
    echo "$1" | tr '/_-' '\n' | grep -v '^$' | grep -viE "$STOPWORDS" | sort -u
}
ptoks=$(tokenize "$proposed")
nptoks=$(echo "$ptoks" | wc -l | tr -d ' ')

similar=""
if [ "$nptoks" -ge 2 ]; then
    while IFS= read -r b; do
        [ "$b" = "$proposed" ] && continue
        [ "$b" = "$default" ] && continue
        otoks=$(tokenize "$b")
        shared=$(comm -12 <(echo "$ptoks") <(echo "$otoks") 2>/dev/null | wc -l | tr -d ' ')
        if [ "${shared:-0}" -ge 2 ]; then
            last=$(git -C "$repo" log -1 --format='%cr' "$b" 2>/dev/null \
                || git -C "$repo" log -1 --format='%cr' "origin/$b" 2>/dev/null \
                || echo 'unknown')
            similar+="  - $b  ($last)
"
        fi
    done <<<"$branches"
fi

if [ -n "$similar" ]; then
    {
        echo ""
        echo "[branch-similarity-check] '$proposed' shares ≥2 tokens with:"
        printf "%s" "$similar"
        echo "  If overlapping, reuse one of these or name more specifically."
        echo ""
    } >&2
fi

# ─── 3. Squash-merged peers (same first-segment) ─────────────────────────
if [ -n "$default" ] && [[ "$proposed" == */* ]]; then
    seg="${proposed%%/*}"
    peers=$(echo "$branches" | grep "^${seg}/" | grep -v "^${proposed}$" || true)
    squash=""
    while IFS= read -r b; do
        [ -z "$b" ] && continue
        # Try local ref first, then origin/<b> — branch may exist only on remote
        cherry=$(git -C "$repo" cherry "$default" "$b" 2>/dev/null \
              || git -C "$repo" cherry "$default" "origin/$b" 2>/dev/null \
              || true)
        [ -z "$cherry" ] && continue
        plus=$(printf '%s\n' "$cherry" | grep -c '^+' || true)
        minus=$(printf '%s\n' "$cherry" | grep -c '^-' || true)
        plus="${plus:-0}"; minus="${minus:-0}"
        if [ "$plus" = "0" ] && [ "$minus" -gt "0" ]; then
            squash+="  - $b  (all commits already on $default via squash/rebase)
"
        fi
    done <<<"$peers"

    if [ -n "$squash" ]; then
        {
            echo ""
            echo "[branch-similarity-check] same-prefix branches whose work is ALREADY"
            echo "merged into $default (squash/rebase) but the branch still exists:"
            printf "%s" "$squash"
            echo "  Re-attempting their work? Check why they were superseded first."
            echo ""
        } >&2
    fi
fi

exit 0
