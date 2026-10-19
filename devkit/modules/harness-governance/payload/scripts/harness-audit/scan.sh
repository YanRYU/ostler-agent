#!/usr/bin/env bash
# .claude/scripts/harness-audit/scan.sh: extraction scanner of the harness-governance module.
# Enforces: .claude/rules/harness-governance.md (the inventory is computed, never hand-written)
#
# Reads one repo's control surfaces and prints them as JSON: the entries (one per control
# artifact) and the mechanical findings. READ-ONLY against the target.
#
# Usage: scan.sh [TARGET_DIR] [--out FILE]        (TARGET_DIR defaults to the current directory)
#
# Reads, all under the target: .claude/settings.json and .claude/settings.local.json (permission
# patterns and hook wiring), .claude/hooks/, .claude/rules/, .claude/skills/, .claude/agents/,
# .claude/scripts/, and the pointer-table rows of CLAUDE.md. Needs bash, jq and standard tools;
# no Python, no network, nothing outside the target.
# Mechanism is proposed from behavior: deny -> BLOCK, ask -> ASK, a hook with a non-comment
# `exit 2` or a decision:"block" payload -> BLOCK, any other hook -> DETECT, rule/skill/agent ->
# INSTRUCT, scripts -> VERIFY, CLAUDE.md pointer rows -> ROUTE.
# A settings file that is not valid JSON is reported as a `settings-unreadable` finding (its permissions
# and hook wiring cannot be read, so they are NOT treated as empty-and-fine).
# Not scanned: the audit script itself (.claude/devkit-audit.py) and tooling an add-on module installs
# under .claude outside hooks/, rules/, skills/, agents/ and scripts/ (for example a folder an add-on module creates for its own tooling).
# Output (stdout, or --out FILE): {"scan": {...}, "entries": [...], "findings": [...]}.
set -euo pipefail
export LC_ALL=C

TARGET="$PWD"; OUT=""
# Invalid arguments exit before anything is touched. Once the arguments are valid the run has
# started, and from then on any failure must not leave an older report behind to be read as current.
usage() { echo "$*" >&2; exit 1; }
bail() { # $1=exit code, rest=message
  local code="$1"; shift
  [ -z "$OUT" ] || rm -f "$OUT"
  echo "$*" >&2
  exit "$code"
}
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || usage "scan.sh: --out needs a file"; OUT="$2"; shift 2 ;;
    -*) usage "scan.sh: unknown flag: $1 (usage: scan.sh [TARGET_DIR] [--out FILE])" ;;
    *) TARGET="$1"; shift ;;
  esac
done
[ -z "$OUT" ] || rm -f "$OUT"
command -v jq >/dev/null 2>&1 || bail 2 "scan.sh: jq is required (install jq, then run again)"
[ -d "$TARGET" ] || bail 1 "scan.sh: target is not a directory: $TARGET"
TARGET="$(cd "$TARGET" && pwd -P)"

CDIR="$TARGET/.claude"
SETTINGS_FILES=()
[ -f "$CDIR/settings.json" ] && SETTINGS_FILES+=("$CDIR/settings.json")
[ -f "$CDIR/settings.local.json" ] && SETTINGS_FILES+=("$CDIR/settings.local.json")
HOOKS_DIR="$CDIR/hooks"; RULES_DIR="$CDIR/rules"; SKILLS_DIR="$CDIR/skills"
AGENTS_DIR="$CDIR/agents"; SCRIPTS_DIR="$CDIR/scripts"
CLAUDE_MD="$TARGET/CLAUDE.md"; [ -f "$CLAUDE_MD" ] || CLAUDE_MD="$CDIR/CLAUDE.md"

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
ENTRIES="$WORK/entries.jsonl"; FINDINGS="$WORK/findings.jsonl"; : >"$ENTRIES"; : >"$FINDINGS"

finding() { # $1=check $2=artifact $3=detail
  jq -nc --arg c "$1" --arg a "$2" --arg d "$3" '{check:$c, artifact:$a, detail:$d}' >>"$FINDINGS"
}

for f in "${SETTINGS_FILES[@]:-}"; do
  [ -n "$f" ] || continue
  jq -e . "$f" >/dev/null 2>&1 || \
    finding "settings-unreadable" "$(basename "$f")" "not valid JSON: permissions and hook wiring in this file could not be read"
done

# entry ID NAME TYPE MECHANISM ENFORCES WIRED STATUS HATCH [LIST_KEY LIST_VALUES(newline-separated)]
# An empty ENFORCES or HATCH leaves the field out.
entry() {
  local list_json="null"
  if [ -n "${9:-}" ]; then
    list_json=$(printf '%s\n' "${10}" | jq -R . | jq -sc .)
  fi
  jq -nc --arg id "$1" --arg name "$2" --arg t "$3" --arg m "$4" --arg e "$5" --arg w "$6" \
        --arg s "$7" --arg h "$8" --arg lk "${9:-}" --argjson lv "$list_json" \
    '{id:$id, name:$name, artifact_type:$t, mechanism:$m}
     + (if $e != "" then {enforces:$e} else {} end)
     + {wired_at:$w, status:$s}
     + (if $h != "" then {escape_hatch:$h} else {} end)
     + (if $lk != "" then {($lk):$lv} else {} end)' >>"$ENTRIES"
}

join_sep() { awk -v sep="$1" 'NR>1{printf "%s", sep} {printf "%s", $0} END{if(NR)print ""}'; }

perm_list() { # $1 = deny|ask
  local f
  for f in "${SETTINGS_FILES[@]:-}"; do
    [ -n "$f" ] || continue
    jq -r --arg k "$1" '(.permissions[$k] // [])[]' "$f" 2>/dev/null || true
  done
}

emit_perm_group() { # $1=deny|ask $2=mechanism
  local pats n; pats="$(perm_list "$1")"
  [ -n "$pats" ] || return 0
  n=$(printf '%s\n' "$pats" | wc -l | tr -d ' ')
  entry "settings-$1" "settings.json $1 list ($n patterns)" "$1-rule" "$2" "" "permissions" "active" "" \
        patterns "$pats"
}

# TAB-separated wiring: EVENT<TAB>MATCHER<TAB>COMMAND (an empty matcher reads as "*")
wiring_lines() {
  local f
  for f in "${SETTINGS_FILES[@]:-}"; do
    [ -n "$f" ] || continue
    jq -r '(.hooks // {}) | to_entries[] as $e
           | $e.value[] as $grp
           | (($grp.matcher // "*") | if . == "" then "*" else . end) as $m
           | $grp.hooks[]? | select(.type == "command")
           | [$e.key, $m, .command] | @tsv' "$f" 2>/dev/null || true
  done
}

hook_mechanism() { # $1=file; comment lines are stripped before the exit-2 search
  if grep -Ev '^[[:space:]]*#' "$1" | grep -q 'exit 2'; then echo BLOCK
  elif grep -Eq 'decision"?[[:space:]]*:[[:space:]]*"?block' "$1"; then echo BLOCK
  else echo DETECT; fi
}

# Escape hatches are read from code, not prose: a hook that only mentions another guard's variable
# in a comment has no hatch of its own. Known limit: a trailing comment, a heredoc body or a quoted
# help string can still produce a phantom hatch; that is judged the safer error (it shows up when
# reviewed) than silently dropping a real hatch that shares a line with a `#`.
hook_escape_hatch() { # $1=file -> comma-joined hatch variables, empty when none
  grep -Ev '^[[:space:]]*#' "$1" \
    | grep -ohE '[A-Z][A-Z0-9_]*_(DISABLED|OVERRIDE)' | sort -u | join_sep ', ' || true
}

cmd_mechanism() { # $1=command string -> BLOCK|DETECT
  if printf '%s' "$1" | grep -q 'exit 2'; then echo BLOCK
  elif printf '%s' "$1" | grep -Eq 'decision"?[[:space:]]*:[[:space:]]*"?block'; then echo BLOCK
  else echo DETECT; fi
}

# A hook counts as manually wired only when a rule or CLAUDE.md line INVOKES it (`bash path/<hook>`),
# never when prose merely names it. The basename must sit on a path boundary, so `a.sh` is not
# matched by a line that runs `extra.sh`. An invocation split across lines, or written without an
# interpreter word, is missed: that fails toward UNWIRED (loud), never toward active (silent).
manual_invocation() { # $1=hook basename
  local esc=${1//./\\.}
  local re="(bash|sh|zsh|python3?)([[:space:]]+-[^[:space:]]+)*[[:space:]]+([^[:space:]]*/)?${esc}([^A-Za-z0-9_.-]|$)"
  { [ -d "$RULES_DIR" ] && grep -rqE "$re" "$RULES_DIR" 2>/dev/null; } ||
  { [ -f "$CLAUDE_MD" ] && grep -qE "$re" "$CLAUDE_MD" 2>/dev/null; }
}

emit_hooks() {
  [ -d "$HOOKS_DIR" ] || return 0
  local wiring f base id enforces wired mech hatch status tgt
  wiring="$(wiring_lines)"
  for f in "$HOOKS_DIR"/*; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    id="hook-$(printf '%s' "${base%.*}" | tr '[:upper:]_.' '[:lower:]--')"
    enforces=$(grep -m1 '^# Enforces:' "$f" | sed 's/^# Enforces:[[:space:]]*//' || true)
    wired=$(printf '%s\n' "$wiring" | awk -F'\t' -v b="$base" 'index($3, b) {print $1 ":" $2}' | sort -u | join_sep '; ')
    mech=$(hook_mechanism "$f")
    hatch=$(hook_escape_hatch "$f")
    if [ -n "$wired" ]; then status=active
    elif manual_invocation "$base"; then status=active; wired="manual (invoked from rules/ or CLAUDE.md)"
    else status=unwired; wired="UNWIRED"; fi

    [ "$status" = "unwired" ] && finding "unwired-file" "hooks/$base" "present in hooks/ but wired nowhere in settings hooks and referenced by no rule or CLAUDE.md line"
    [ -z "$enforces" ] && finding "missing-enforces" "hooks/$base" "no '# Enforces:' header line"
    if [ -n "$enforces" ]; then
      tgt=$(printf '%s' "$enforces" | grep -oE '(~|/|[A-Za-z0-9_.-]+/)[^ )]*' | head -1 || true)
      if [ -n "$tgt" ]; then
        [ -f "$tgt" ] || [ -f "$CDIR/$tgt" ] || [ -f "$TARGET/$tgt" ] || \
          finding "dangling-enforces" "hooks/$base" "Enforces target not found: $tgt"
      fi
    fi
    [ "$mech" = "BLOCK" ] && [ -z "$hatch" ] && finding "block-no-escape-hatch" "hooks/$base" "BLOCK-behavior hook with no *_DISABLED/*_OVERRIDE escape hatch (advisory)"

    entry "$id" "$base" "hook" "$mech" "${enforces:-MISSING}" "$wired" "$status" "${hatch:-none}"
  done
}

# Settings hook commands that run no file under .claude/hooks: one entry per derived id, so two
# commands sharing a command word collapse into one entry (wired_at = sorted union of event:matcher).
emit_external_hooks() {
  local wiring; wiring="$(wiring_lines)"
  [ -n "$wiring" ] || return 0
  local rows
  rows=$(printf '%s\n' "$wiring" | while IFS=$'\t' read -r ev m cmd; do
    hit=0
    for hf in "$HOOKS_DIR"/*; do
      [ -f "$hf" ] || continue
      # [[ == ]] glob rather than `case`: a `)` in a case pattern breaks bash 3.2 inside $(...)
      if [[ "$cmd" == *"$(basename -- "$hf")"* ]]; then hit=1; break; fi
    done
    [ "$hit" -eq 0 ] || continue
    word=$(printf '%s' "$cmd" | awk '{print ($1=="bash"||$1=="python3"||$1=="sh")?$2:$1}')
    word=$(basename -- "$word")
    if [ -z "$word" ] || [[ "$word" == -* ]]; then id="__INLINE__"
    else id="ext-$(printf '%s' "${word%.*}" | tr -cd 'a-zA-Z0-9_-' | tr '[:upper:]_' '[:lower:]-')"; fi
    printf '%s\t%s:%s\t%s\n' "$id" "$ev" "$m" "$cmd"
  done)
  [ -n "$rows" ] || return 0
  # inline commands (`bash -c '...'`) get ext-inline-<n>, n = index in the sorted set of commands
  local inline_cmds
  inline_cmds=$(printf '%s\n' "$rows" | awk -F'\t' '$1=="__INLINE__" {print $3}' | sort -u)
  if [ -n "$inline_cmds" ]; then
    rows=$(printf '%s\n' "$rows" | while IFS=$'\t' read -r rid wm cmd; do
      if [ "$rid" = "__INLINE__" ]; then
        rid="ext-inline-$(printf '%s\n' "$inline_cmds" | grep -nxF -- "$cmd" | head -1 | cut -d: -f1)"
      fi
      printf '%s\t%s\t%s\n' "$rid" "$wm" "$cmd"
    done)
  fi
  local id wired name mech hatch
  for id in $(printf '%s\n' "$rows" | cut -f1 | sort -u); do
    wired=$(printf '%s\n' "$rows" | awk -F'\t' -v i="$id" '$1==i {print $2}' | sort -u | join_sep '; ')
    name=$(printf '%s\n' "$rows" | awk -F'\t' -v i="$id" '$1==i {print $3}' | sort -u | join_sep '; ')
    mech=$(cmd_mechanism "$name")
    hatch=$(printf '%s' "$name" | grep -oE '[A-Z][A-Z0-9_]*_(DISABLED|OVERRIDE)' | sort -u | join_sep ', ' || true)
    entry "$id" "$name" "hook" "$mech" "EXTERNAL - source not scanned" "$wired" "active" "${hatch:-none}"
  done
}

emit_rules() {
  [ -d "$RULES_DIR" ] || return 0
  local f base
  for f in "$RULES_DIR"/*.md; do
    [ -f "$f" ] || continue
    base=$(basename "$f" .md)
    entry "rule-$base" "rules/$base.md" "rule-file" "INSTRUCT" "" "always-on (rules/)" "active" ""
  done
}

emit_claude_md() {
  [ -f "$CLAUDE_MD" ] || return 0
  local rows
  rows=$(grep -E '^\|' "$CLAUDE_MD" | grep -vE '^\|[-: ]+\|' | sed 's/^|//' | awk -F'|' '{gsub(/^ +| +$/, "", $1); if ($1 != "") print $1}' || true)
  [ -n "$rows" ] || return 0
  # no absolute path in wired_at: the field is compared with the registry, and a machine-specific
  # prefix would read as drift on every other checkout
  entry "claude-md-pointers" "CLAUDE.md pointer/constraint table rows" "pointer" "ROUTE" "" \
        "always-on (CLAUDE.md)" "active" "" rows "$rows"
}

emit_skills_agents_scripts() {
  local d f base
  if [ -d "$SKILLS_DIR" ]; then
    for d in "$SKILLS_DIR"/*/; do
      [ -d "$d" ] || continue
      base=$(basename "$d")
      # A skill is its SKILL.md: a directory without one cannot be invoked. A gitignored one (a
      # vendor cache, say) is not an authoring mistake and is skipped; any other is reported.
      if [ ! -f "$d/SKILL.md" ]; then
        git -C "$TARGET" check-ignore -q "$d" 2>/dev/null || \
          finding "skill-without-manifest" "skills/$base" "directory in skills/ has no SKILL.md and is not gitignored: not invocable by the Skill tool"
        continue
      fi
      entry "skill-$base" "skills/$base" "skill" "INSTRUCT" "" "Skill tool (on-demand)" "active" ""
    done
  fi
  if [ -d "$AGENTS_DIR" ]; then
    for f in "$AGENTS_DIR"/*.md; do
      [ -f "$f" ] || continue
      base=$(basename "$f" .md)
      entry "agent-$base" "agents/$base.md" "agent" "INSTRUCT" "" "Agent tool (on-demand)" "active" ""
    done
  fi
  if [ -d "$SCRIPTS_DIR" ]; then
    for d in "$SCRIPTS_DIR"/*/; do
      [ -d "$d" ] || continue
      base=$(basename "$d")
      case "$base" in tests|__pycache__) continue ;; esac
      entry "script-$base" "scripts/$base/" "script" "VERIFY" "" "manual" "active" ""
    done
  fi
}

main() {
  emit_perm_group deny BLOCK
  emit_perm_group ask ASK
  emit_hooks
  emit_external_hooks
  emit_rules
  emit_claude_md
  emit_skills_agents_scripts
  local sf='[]'
  if [ "${#SETTINGS_FILES[@]}" -gt 0 ]; then
    sf=$(printf '%s\n' "${SETTINGS_FILES[@]}" | jq -R . | jq -sc .)
  fi
  jq -n --arg target "$TARGET" --argjson sf "$sf" \
        --slurpfile entries "$ENTRIES" --slurpfile findings "$FINDINGS" \
    '{scan: {target: $target, generated_by: ".claude/scripts/harness-audit/scan.sh", settings_files: $sf},
      entries: $entries, findings: $findings}'
}

if [ -n "$OUT" ]; then main >"$WORK/out.json"; mv "$WORK/out.json" "$OUT"; else main; fi
