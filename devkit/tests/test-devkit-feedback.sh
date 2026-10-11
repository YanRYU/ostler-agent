#!/usr/bin/env bash
# Tests the devkit minimal upstream feedback loop (patch ledger):
# devkit/modules/audit/payload/devkit-audit.py ledger validation,
# classification (incl. mode:merge rejection, age display), the printed upstream
# command, and devkit/install/_devkit.py's `patch` subcommand + the
# refused-file record carry-forward.
#
# Two groups of test:
#   - Baseline (1-3): behavior of the audit on targets without a patch ledger.
#   - Patch ledger (4-9): the ledger, the feedback-log command and the
#     refused-file record carry-forward.
#
# Usage: bash devkit/tests/test-devkit-feedback.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUDIT="$REPO/devkit/modules/audit/payload/devkit-audit.py"
INSTALL_SH="$REPO/devkit/install/devkit-install.sh"

pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'PASS  %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL  %s\n      %s\n' "$1" "$2"; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

sha256_of() { python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

# Build a scratch target dir with one managed file "rule.md" and a manifest
# recording it. $1 = target dir. $2 = file content (recorded). Prints nothing.
mk_basic_target() {
    local t="$1" content="$2"
    mkdir -p "$t"
    printf '%s' "$content" > "$t/rule.md"
    local sha; sha=$(sha256_of "$t/rule.md")
    python3 -c "
import json, sys
manifest = {
    'schema_version': 1, 'source_commit': 'deadbeef',
    'installs': [{'module': 'rules-core', 'version': '1.0.0',
                  'files': [{'path': 'rule.md', 'sha256': sys.argv[2], 'mode': 'overwrite'}]}],
}
open(sys.argv[1] + '/.devkit-manifest.json', 'w').write(json.dumps(manifest, indent=2) + '\n')
" "$t" "$sha"
    echo "$sha"
}

add_ledger_entry_py() {
    # $1 = target, then a python snippet body operating on `manifest` dict, writing it back.
    local t="$1"; shift
    python3 -c "
import json
p = '$t/.devkit-manifest.json'
manifest = json.load(open(p))
$1
open(p, 'w').write(json.dumps(manifest, indent=2) + '\n')
"
}

run_audit() { # $1 = target
    out=$(python3 -S "$AUDIT" "$1" 2>"$WORK/err"); rc=$?
    err=$(cat "$WORK/err" 2>/dev/null || true)
}

# Same, under an explicit environment: the printed upstream command resolves the
# feedback log when the AUDIT runs, so the environment that matters is the
# audit's own. `env -u` clears any ambient feedback-log env vars first.
# $1 = target, rest = VAR=value assignments.
run_audit_env() {
    local t="$1"; shift
    out=$(env -u DEVKIT_HOME -u DEVKIT_FEEDBACK_DIR "$@" python3 -S "$AUDIT" "$t" 2>"$WORK/err"); rc=$?
    err=$(cat "$WORK/err" 2>/dev/null || true)
}

# A printed command (upstream/refresh) is shlex-quoted, never JSON-escaped for
# the refresh case, so a hostile reason with an embedded literal
# newline legitimately makes it span multiple printed lines. Extract from the
# marker to (not including) the final "[audit] summary:" line, preserving
# real newlines rather than assuming one grep line.
extract_command() { # $1 = full audit stdout, $2 = marker (e.g. "refresh:" / "upstream:")
    printf '%s\n' "$1" | awk -v marker="$2" '
        found { if ($0 ~ /^\[audit\] summary:/) exit; print; next }
        i = index($0, marker) { found=1; print substr($0, i + length(marker) + 1); next }
    '
}

echo "devkit feedback-loop — patch ledger suite"

# =================================================================
# Characterization (1-3): baseline behavior that must stay green.
# =================================================================

# --- 1: clean manifest, no ledger -> exit 0, unchanged output shape
T1="$WORK/t1"
mk_basic_target "$T1" "hello world" >/dev/null
run_audit "$T1"
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "clean" && printf '%s' "$out" | grep -q "rule.md"; then
    ok "1 · clean manifest, no ledger -> exit 0, rule.md classified clean"
else
    bad "1 · clean manifest, no ledger" "rc=$rc out=${out:0:300}"
fi

# --- 2: one modified file, no entry -> exit 2, file named
T2="$WORK/t2"
mk_basic_target "$T2" "hello world" >/dev/null
printf 'changed content' > "$T2/rule.md"
run_audit "$T2"
if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -q "modified-downstream" && printf '%s' "$out" | grep -q "rule.md"; then
    ok "2 · modified file, no entry -> exit 2, named"
else
    bad "2 · modified file, no entry" "rc=$rc out=${out:0:300}"
fi

# --- 3: explicit target arg, python3 -S -> exit 0, named managed file classified (stdlib-only proxy)
T3="$WORK/t3"
mk_basic_target "$T3" "stable content" >/dev/null
run_audit "$T3"
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "rule.md"; then
    ok "3 · python3 -S with explicit target -> exit 0, managed file classified, stdlib-only holds"
else
    bad "3 · python3 -S explicit target" "rc=$rc out=${out:0:300}"
fi

# =================================================================
# Patch-ledger tests (4-9).
# =================================================================

# --- 4: modified file + valid matching entry -> exit 0, pending-upstream line,
#        printed append command actually writes a matching JSONL line.
T4="$WORK/t4"
mk_basic_target "$T4" "original" >/dev/null
printf 'patched fix' > "$T4/rule.md"
newsha=$(sha256_of "$T4/rule.md")
add_ledger_entry_py "$T4" "
manifest['patched_pending_upstream'] = [
    {'path': 'rule.md', 'sha256': '$newsha', 'reason': 'tightened wording', 'date': '2026-09-01'}
]
"
HOME_T4="$WORK/t4-home"
mkdir -p "$HOME_T4"
run_audit_env "$T4" HOME="$HOME_T4"
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "pending-upstream"; then
    cmd=$(extract_command "$out" "upstream:")
    HOME="$HOME_T4" bash -c "$cmd"
    jsonl=$(find "$HOME_T4/.devkit/devkit-feedback" -name 'feedback.jsonl' 2>/dev/null | head -1)
    if [ -n "$jsonl" ] && python3 -c "
import json, sys
d = json.loads(open(sys.argv[1]).read().splitlines()[0])
assert d['reason'] == 'tightened wording'
assert d['path'] == 'rule.md'
assert set(d) == {'ts', 'reason', 'path'}
assert d['ts'].endswith('Z')
" "$jsonl" 2>"$WORK/jsonl_err"; then
        ok "4 · modified + valid entry -> pending-upstream, append command writes valid log line"
    else
        bad "4 · append command JSONL" "jsonl=$jsonl err=$(cat "$WORK/jsonl_err" 2>/dev/null)"
    fi
else
    bad "4 · modified + valid entry -> pending-upstream" "rc=$rc out=${out:0:400}"
fi

# --- 5: ledger-validation table, each -> exit 2, each naming the offending entry
T5="$WORK/t5"

check_ledger_case() { # $1 = label, $2 = python snippet setting manifest['patched_pending_upstream'], $3 = grep needle
    local label="$1" snippet="$2" needle="$3"
    local t="$WORK/t5-$(echo "$label" | tr -c 'a-zA-Z0-9' '_')"
    mk_basic_target "$t" "stable" >/dev/null
    add_ledger_entry_py "$t" "$snippet"
    run_audit "$t"
    if [ "$rc" -eq 2 ] && printf '%s\n%s' "$out" "$err" | grep -qi "$needle"; then
        ok "5 · $label -> exit 2, entry named"
    else
        bad "5 · $label" "rc=$rc out=${out:0:200} err=${err:0:300}"
    fi
}

check_ledger_case "orphan path" \
  "manifest['patched_pending_upstream'] = [{'path': 'nope.md', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'}]" \
  "nope.md"

check_ledger_case "absolute path" \
  "manifest['patched_pending_upstream'] = [{'path': '/etc/passwd', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'}]" \
  "etc/passwd"

check_ledger_case "duplicate path" \
  "manifest['patched_pending_upstream'] = [
      {'path': 'rule.md', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'},
      {'path': 'rule.md', 'sha256': 'b'*64, 'reason': 'y', 'date': '2026-09-02'},
  ]" \
  "duplicate"

check_ledger_case "missing sha256" \
  "manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'reason': 'x', 'date': '2026-09-01'}]" \
  "rule.md"

check_ledger_case "malformed sha256" \
  "manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'not-hex', 'reason': 'x', 'date': '2026-09-01'}]" \
  "rule.md"

# sha256 + trailing newline is 65 characters, not "exactly 64 hex characters"
# (`^...$` matches before a trailing "\n").
check_ledger_case "sha256 with trailing newline" \
  "manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'a'*64 + chr(10), 'reason': 'x', 'date': '2026-09-01'}]" \
  "rule.md"

# uppercase hex is accepted by a naive [0-9a-fA-F] class but would always
# mismatch hashlib's lowercase hexdigest().
check_ledger_case "uppercase sha256" \
  "manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'A'*64, 'reason': 'x', 'date': '2026-09-01'}]" \
  "rule.md"

check_ledger_case "empty reason" \
  "manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'a'*64, 'reason': '', 'date': '2026-09-01'}]" \
  "rule.md"

# entry on a mode:merge record
T5M="$WORK/t5-merge"
mk_basic_target "$T5M" "stable" >/dev/null
add_ledger_entry_py "$T5M" "
manifest['installs'][0]['files'][0]['mode'] = 'merge'
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'}]
"
run_audit "$T5M"
if [ "$rc" -eq 2 ] && printf '%s\n%s' "$out" "$err" | grep -qi "rule.md"; then
    ok "5 · entry on mode:merge record -> exit 2, entry named"
else
    bad "5 · entry on mode:merge record" "rc=$rc out=${out:0:200} err=${err:0:300}"
fi

# ledger present but not an array at all (e.g. a JSON object) -> exit 2
T5_NOTARRAY="$WORK/t5-notarray"
mk_basic_target "$T5_NOTARRAY" "stable" >/dev/null
add_ledger_entry_py "$T5_NOTARRAY" "
manifest['patched_pending_upstream'] = {'path': 'rule.md', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'}
"
run_audit "$T5_NOTARRAY"
if [ "$rc" -eq 2 ] && printf '%s\n%s' "$out" "$err" | grep -qi "not a json array"; then
    ok "5 · ledger is not a JSON array (object) -> exit 2, malformed named"
else
    bad "5 · ledger not an array" "rc=$rc out=${out:0:200} err=${err:0:300}"
fi

# ledger present but null -> must NOT silently degrade to an empty ledger (absent key and null value are different cases:
# only an absent key means "empty ledger"; a null must be reported as malformed)
T5_NULL="$WORK/t5-null"
mk_basic_target "$T5_NULL" "stable" >/dev/null
add_ledger_entry_py "$T5_NULL" "
manifest['patched_pending_upstream'] = None
"
run_audit "$T5_NULL"
if [ "$rc" -eq 2 ] && printf '%s\n%s' "$out" "$err" | grep -qi "not a json array"; then
    ok "5 · ledger present but null -> exit 2 (not silently treated as absent/empty)"
else
    bad "5 · ledger null" "rc=$rc out=${out:0:200} err=${err:0:300}"
fi

# an entry that is not an object at all (e.g. a bare string) -> exit 2
T5_ENTRYNOTOBJ="$WORK/t5-entrynotobj"
mk_basic_target "$T5_ENTRYNOTOBJ" "stable" >/dev/null
add_ledger_entry_py "$T5_ENTRYNOTOBJ" "
manifest['patched_pending_upstream'] = ['rule.md']
"
run_audit "$T5_ENTRYNOTOBJ"
if [ "$rc" -eq 2 ] && printf '%s\n%s' "$out" "$err" | grep -qi "not an object"; then
    ok "5 · entry is not an object -> exit 2, entry named"
else
    bad "5 · entry not object" "rc=$rc out=${out:0:200} err=${err:0:300}"
fi

# --- 5b: valid well-formed entry on a non-modified file (clean / forked / missing)
T5B_CLEAN="$WORK/t5b-clean"
sha=$(mk_basic_target "$T5B_CLEAN" "unchanged")
add_ledger_entry_py "$T5B_CLEAN" "
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': '$sha', 'reason': 'x', 'date': '2026-09-01'}]
"
run_audit "$T5B_CLEAN"
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qi "obsolete"; then
    ok "5b · clean file + valid entry -> exit 0, obsolete-entry warning"
else
    bad "5b · clean + entry" "rc=$rc out=${out:0:300}"
fi

T5B_FORKED="$WORK/t5b-forked"
mk_basic_target "$T5B_FORKED" "unchanged" >/dev/null
printf 'unchanged\ndevkit:forked\n' > "$T5B_FORKED/rule.md"
add_ledger_entry_py "$T5B_FORKED" "
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'}]
"
run_audit "$T5B_FORKED"
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qi "forked" && printf '%s' "$out" | grep -qi "contradict"; then
    ok "5b · forked file + valid entry -> exit 0, contradiction warning"
else
    bad "5b · forked + entry" "rc=$rc out=${out:0:300}"
fi

T5B_MISSING="$WORK/t5b-missing"
mk_basic_target "$T5B_MISSING" "unchanged" >/dev/null
rm "$T5B_MISSING/rule.md"
add_ledger_entry_py "$T5B_MISSING" "
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'}]
"
run_audit "$T5B_MISSING"
if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -qi "missing"; then
    ok "5b · missing file + valid entry -> exit 2"
else
    bad "5b · missing + entry" "rc=$rc out=${out:0:300}"
fi

# --- 6: end-to-end: install rules-core + audit into a scratch git repo, edit a
#        rule, patch it, verify drift through the CONSUMER's own installed audit.
T6="$WORK/t6-scratch"
mkdir -p "$T6"
(cd "$T6" && git init -q)
"$INSTALL_SH" install "$T6" rules-core audit >"$WORK/t6-install.log" 2>&1
installed_rule="$T6/.claude/rules/git-hygiene.md"
installed_audit="$T6/.claude/devkit-audit.py"
if [ -f "$installed_rule" ] && [ -f "$installed_audit" ]; then
    printf '\n<!-- patch edit -->\n' >> "$installed_rule"
    "$INSTALL_SH" patch "$T6" ".claude/rules/git-hygiene.md" --reason "edit" >"$WORK/t6-patch.log" 2>&1
    patch_rc=$?
    out=$(python3 -S "$installed_audit" "$T6" 2>"$WORK/t6-check.err"); rc=$?
    if [ "$patch_rc" -eq 0 ] && [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "pending-upstream"; then
        ok "6 · end-to-end: installed consumer audit reports pending-upstream after patch"
    else
        bad "6 · end-to-end" "patch_rc=$patch_rc rc=$rc out=${out:0:400} install_log=$(cat "$WORK/t6-install.log" | tail -5) patch_log=$(cat "$WORK/t6-patch.log")"
    fi

    # The defining case: REINSTALL over a pending-upstream file. A refused file's
    # manifest record must be kept, or its ledger entry is orphaned on the next check.
    "$INSTALL_SH" install "$T6" rules-core audit >"$WORK/t6-reinstall.log" 2>&1
    reinstall_rc=$?
    out=$(python3 -S "$installed_audit" "$T6" 2>"$WORK/t6-recheck.err"); rc=$?
    if grep -qi "REFUSE modified-downstream .*git-hygiene.md" "$WORK/t6-reinstall.log" \
       && [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "pending-upstream"; then
        ok "6 · end-to-end reinstall — refused patched file's ledger entry survives (no orphan)"
    else
        bad "6 · end-to-end reinstall" "reinstall_rc=$reinstall_rc rc=$rc out=${out:0:400} reinstall_log=$(cat "$WORK/t6-reinstall.log")"
    fi

    # Forked files: a forked file's manifest record must also survive reinstall.
    installed_plan="$T6/.claude/rules/plan-writing.md"
    printf '\n<!-- forked by consumer -->\ndevkit:forked\n' >> "$installed_plan"
    "$INSTALL_SH" install "$T6" rules-core audit >"$WORK/t6-fork-reinstall.log" 2>&1
    still_recorded=$(python3 -c "
import json
m = json.load(open('$T6/.devkit-manifest.json'))
paths = [f.get('path') for i in m.get('installs', []) for f in i.get('files', [])]
print('yes' if '.claude/rules/plan-writing.md' in paths else 'no')
")
    if [ "$still_recorded" = "yes" ]; then
        ok "6 · forked file's manifest record survives reinstall (skip carries forward)"
    else
        bad "6 · forked carry-forward" "still_recorded=$still_recorded log=$(cat "$WORK/t6-fork-reinstall.log")"
    fi
else
    bad "6 · end-to-end install" "installed_rule_exists=$([ -f "$installed_rule" ] && echo yes || echo no) installed_audit_exists=$([ -f "$installed_audit" ] && echo yes || echo no) log=$(cat "$WORK/t6-install.log" | tail -10)"
fi

# --- 7: re-edit a file that already has a valid entry -> exit 2 + refresh
#        command; pasting that command -> exit 0.
T7="$WORK/t7"
mk_basic_target "$T7" "v1" >/dev/null
printf 'v2 patched' > "$T7/rule.md"
v2sha=$(sha256_of "$T7/rule.md")
add_ledger_entry_py "$T7" "
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': '$v2sha', 'reason': 'first fix', 'date': '2026-08-01'}]
"
printf 'v3 re-edited' > "$T7/rule.md"
run_audit "$T7"
if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -qi "STALE"; then
    cmd=$(extract_command "$out" "refresh:")
    cmd="${cmd/devkit-install.sh/$INSTALL_SH}"
    eval "$cmd" >"$WORK/t7-refresh.log" 2>&1
    refresh_rc=$?
    run_audit "$T7"
    if [ "$refresh_rc" -eq 0 ] && [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "pending-upstream"; then
        ok "7 · stale re-edit -> exit 2 + refresh command; pasting it -> exit 0"
    else
        bad "7 · refresh paste" "refresh_rc=$refresh_rc rc=$rc out=${out:0:300} log=$(cat "$WORK/t7-refresh.log")"
    fi
else
    bad "7 · stale re-edit detection" "rc=$rc out=${out:0:300}"
fi

# --- 8: hostile reason against both printed commands -> no side effect,
#        JSON decodes to the exact original reason.
HOSTILE=$'quote\'"$(touch '"$WORK"$'/devkit-test-pwned)`backtick`\nnewline\ttab\rcr\x1besc\x01ctrl'
rm -f $WORK/devkit-test-pwned

# 8a: append command (pending-upstream, fresh, valid entry)
T8A="$WORK/t8a"
mk_basic_target "$T8A" "orig" >/dev/null
printf 'fixed' > "$T8A/rule.md"
sha8a=$(sha256_of "$T8A/rule.md")
python3 -c "
import json, sys
p, sha, reason = sys.argv[1], sys.argv[2], sys.argv[3]
manifest = json.load(open(p))
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': sha, 'reason': reason, 'date': '2026-09-01'}]
open(p, 'w').write(json.dumps(manifest, indent=2) + '\n')
" "$T8A/.devkit-manifest.json" "$sha8a" "$HOSTILE"
HOME_T8A="$WORK/t8a-home"
mkdir -p "$HOME_T8A"
run_audit_env "$T8A" HOME="$HOME_T8A"
side_effect_before=$([ -e $WORK/devkit-test-pwned ] && echo yes || echo no)
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "pending-upstream"; then
    cmd=$(extract_command "$out" "upstream:")
    HOME="$HOME_T8A" bash -c "$cmd" 2>"$WORK/t8a-err"
    jsonl=$(find "$HOME_T8A/.devkit/devkit-feedback" -name 'feedback.jsonl' 2>/dev/null | head -1)
    side_effect_after=$([ -e $WORK/devkit-test-pwned ] && echo yes || echo no)
    if [ "$side_effect_after" = "no" ] && [ -n "$jsonl" ] && python3 -c "
import json, sys
d = json.loads(open(sys.argv[1]).read().splitlines()[0])
assert d['reason'] == sys.argv[2], (d['reason'], sys.argv[2])
" "$jsonl" "$HOSTILE" 2>"$WORK/t8a-decode-err"; then
        ok "8a · hostile reason, append command -> no side effect, exact decode"
    else
        bad "8a · hostile reason append" "side_effect=$side_effect_after jsonl=$jsonl decode_err=$(cat "$WORK/t8a-decode-err")"
    fi
else
    bad "8a · setup (pending-upstream)" "rc=$rc out=${out:0:300}"
fi

# 8b: refresh command (stale, prior reason hostile)
T8B="$WORK/t8b"
mk_basic_target "$T8B" "v1" >/dev/null
printf 'v2' > "$T8B/rule.md"
v2sha8b=$(sha256_of "$T8B/rule.md")
python3 -c "
import json, sys
p, sha, reason = sys.argv[1], sys.argv[2], sys.argv[3]
manifest = json.load(open(p))
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': sha, 'reason': reason, 'date': '2026-08-01'}]
open(p, 'w').write(json.dumps(manifest, indent=2) + '\n')
" "$T8B/.devkit-manifest.json" "$v2sha8b" "$HOSTILE"
printf 'v3 re-edited' > "$T8B/rule.md"
run_audit "$T8B"
if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -qi "STALE"; then
    cmd=$(extract_command "$out" "refresh:")
    cmd="${cmd/devkit-install.sh/$INSTALL_SH}"
    eval "$cmd" >"$WORK/t8b.log" 2>&1
    refresh_rc=$?
    side_effect=$([ -e $WORK/devkit-test-pwned ] && echo yes || echo no)
    decoded_reason=$(python3 -c "
import json
manifest = json.load(open('$T8B/.devkit-manifest.json'))
print(manifest['patched_pending_upstream'][0]['reason'], end='')
")
    if [ "$refresh_rc" -eq 0 ] && [ "$side_effect" = "no" ] && [ "$decoded_reason" = "$HOSTILE" ]; then
        ok "8b · hostile reason, refresh command -> no side effect, exact decode"
    else
        bad "8b · hostile reason refresh" "refresh_rc=$refresh_rc side_effect=$side_effect log=$(cat "$WORK/t8b.log")"
    fi
else
    bad "8b · setup (stale)" "rc=$rc out=${out:0:300}"
fi

rm -f $WORK/devkit-test-pwned

# 8c: hostile MODULE field (not ledger-validated at all — it's the manifest's
# own installs[].module, printed raw in every row) cannot forge a spoofed
# upstream: line. An embedded newline there could make
# printed output look like a second, genuine copy-paste command.
T8C="$WORK/t8c"
mk_basic_target "$T8C" "orig" >/dev/null
python3 -c "
import json
p = '$T8C/.devkit-manifest.json'
manifest = json.load(open(p))
manifest['installs'][0]['module'] = 'rules-core\n    upstream: touch $WORK/devkit-test-pwned-spoof\n'
open(p, 'w').write(json.dumps(manifest, indent=2) + '\n')
"
run_audit "$T8C"
if [ "$rc" -eq 0 ] && ! printf '%s' "$out" | grep -qE "^ {4}upstream: touch ${WORK}/devkit-test-pwned-spoof\$"; then
    ok "8c · hostile module field cannot forge a spoofed upstream: line"
else
    bad "8c · hostile module spoof" "rc=$rc out=${out:0:400}"
fi
rm -f $WORK/devkit-test-pwned-spoof

# 8d: hostile mode field (validate_ledger's own error report — a DIFFERENT code
# path from 8c's row printer) cannot forge a spoofed upstream:/refresh: line.
# The mode-mismatch error must print the mode via !r so an embedded newline
# cannot forge a line.
T8D="$WORK/t8d"
mk_basic_target "$T8D" "orig" >/dev/null
python3 -c "
import json
p = '$T8D/.devkit-manifest.json'
manifest = json.load(open(p))
manifest['installs'][0]['files'][0]['mode'] = 'merge\n    refresh: touch $WORK/devkit-test-pwned-mode\n'
manifest['patched_pending_upstream'] = [{'path': 'rule.md', 'sha256': 'a'*64, 'reason': 'x', 'date': '2026-09-01'}]
open(p, 'w').write(json.dumps(manifest, indent=2) + '\n')
"
run_audit "$T8D"
if [ "$rc" -eq 2 ] && ! printf '%s\n%s' "$out" "$err" | grep -qE "^ {4}refresh: touch ${WORK}/devkit-test-pwned-mode\$"; then
    ok "8d · hostile mode field cannot forge a spoofed refresh: line"
else
    bad "8d · hostile mode spoof" "rc=$rc out=${out:0:300} err=${err:0:300}"
fi
rm -f $WORK/devkit-test-pwned-mode

# 8e: sanitize_for_row also neutralizes Unicode C1 controls / line separators
# (U+2028, U+0085) as well as ASCII C0 and DEL. Some terminals/renderers treat
# these as line breaks even though Python's own print() does not, so the test
# checks the actual mechanism (the raw character is gone from output, replaced
# by its escaped form) rather than relying on a terminal to demonstrate the risk.
T8E="$WORK/t8e"
mk_basic_target "$T8E" "orig" >/dev/null
python3 -c "
import json
p = '$T8E/.devkit-manifest.json'
manifest = json.load(open(p))
manifest['installs'][0]['module'] = 'rules-core spoof'
open(p, 'w').write(json.dumps(manifest, indent=2) + '\n')
"
run_audit "$T8E"
check_8e=$(printf '%s' "$out" | python3 -c "
import sys
out = sys.stdin.read()
raw_present = (' ' in out) or ('' in out)
escaped_present = '\\\\u2028\\\\u0085' in out
print('ok' if (not raw_present and escaped_present) else 'bad')
")
if [ "$rc" -eq 0 ] && [ "$check_8e" = "ok" ]; then
    ok "8e · U+2028/U+0085 in module field are escaped, not printed raw"
else
    bad "8e · U+2028/U+0085 spoof" "rc=$rc check=$check_8e out=${out:0:400}"
fi

# =================================================================
# Feedback-log location (9): the printed upstream command must write to the
# feedback-log directory the AUDIT resolves (DEVKIT_FEEDBACK_DIR > <home>/devkit-feedback,
# <home> being the devkit home), as an ABSOLUTE, shell-quoted path, never a literal `~`.
# The log is ONE file, feedback.jsonl, in that directory.
# The payload stays self-contained (no imports beyond the Python standard library).
# =================================================================

# A target with one patched-and-ledgered file, so the audit prints an
# `upstream:` command. $1 = target dir.
mk_pending_target() {
    local t="$1" newsha
    mk_basic_target "$t" "original" >/dev/null
    printf 'patched fix' > "$t/rule.md"
    newsha=$(sha256_of "$t/rule.md")
    add_ledger_entry_py "$t" "
manifest['patched_pending_upstream'] = [
    {'path': 'rule.md', 'sha256': '$newsha', 'reason': 'tightened wording', 'date': '2026-09-01'}
]
"
}

# Run the printed upstream command under a DECOY read-only-ish HOME and report
# where the log line landed. $1 = decoy dir. Sets $jsonl (first feedback.jsonl
# found under $2).
run_printed_command() { # $1 = decoy HOME, $2 = dir to search
    mkdir -p "$1"
    cmd=$(extract_command "$out" "upstream:")
    (cd "$WORK" && env -u DEVKIT_HOME -u DEVKIT_FEEDBACK_DIR HOME="$1" bash -c "$cmd") 2>"$WORK/9-cmd-err"
    jsonl=$(find "$2" -name 'feedback.jsonl' 2>/dev/null | head -1)
}

# --- 9a: DEVKIT_FEEDBACK_DIR wins; nothing lands under the decoy HOME
T9A="$WORK/t9a"; mk_pending_target "$T9A"
S9A="$WORK/t9a-feedback"; H9A="$WORK/t9a-home"
run_audit_env "$T9A" HOME="$H9A" DEVKIT_FEEDBACK_DIR="$S9A"
run_printed_command "$H9A-run" "$S9A"
if [ "$rc" -eq 0 ] && [ -n "$jsonl" ] && [ "$jsonl" = "$S9A/$(basename "$jsonl")" ] \
   && [ -z "$(find "$H9A-run" -mindepth 1 2>/dev/null | head -1)" ] && ! printf '%s' "$out" | grep -q 'upstream: .*~/'; then
    ok "9a · DEVKIT_FEEDBACK_DIR -> log in that dir, absolute path, decoy HOME untouched"
else
    bad "9a · DEVKIT_FEEDBACK_DIR" "rc=$rc jsonl=$jsonl out=${out:0:300}"
fi

# --- 9b: DEVKIT_HOME relocates the default log directory (<DEVKIT_HOME>/devkit-feedback)
T9B="$WORK/t9b"; mk_pending_target "$T9B"
H9B="$WORK/t9b-home"; mkdir -p "$H9B"
run_audit_env "$T9B" HOME="$WORK/t9b-realhome" DEVKIT_HOME="$H9B"
run_printed_command "$WORK/t9b-run" "$H9B/devkit-feedback"
if [ "$rc" -eq 0 ] && [ -n "$jsonl" ] && [ "$jsonl" = "$H9B/devkit-feedback/feedback.jsonl" ] ; then
    ok "9b · DEVKIT_HOME -> <home>/devkit-feedback/feedback.jsonl"
else
    bad "9b · DEVKIT_HOME" "rc=$rc jsonl=$jsonl out=${out:0:300}"
fi

# --- 9c: a log path with a space is ONE shell word (a split would mkdir a
#         stray relative `dir/` next to the cwd, which the runner pins to $WORK)
T9C="$WORK/t9c"; mk_pending_target "$T9C"
S9C="$WORK/t9c-log dir"
run_audit_env "$T9C" HOME="$WORK/t9c-home" DEVKIT_FEEDBACK_DIR="$S9C"
run_printed_command "$WORK/t9c-run" "$S9C"
if [ "$rc" -eq 0 ] && [ -n "$jsonl" ] && [ "$jsonl" = "$S9C/$(basename "$jsonl")" ] \
   && [ ! -e "$WORK/dir" ] && [ ! -e "$WORK/t9c-log" ]; then
    ok "9c · space in the log path -> quoted as one word, line lands in the right dir"
else
    bad "9c · space in path" "rc=$rc jsonl=$jsonl out=${out:0:300}"
fi

# --- 9d: no env vars -> <HOME>/.devkit/devkit-feedback, spelled absolute
T9D="$WORK/t9d"; mk_pending_target "$T9D"
H9D="$WORK/t9d-home"; mkdir -p "$H9D"
run_audit_env "$T9D" HOME="$H9D"
run_printed_command "$WORK/t9d-run" "$H9D/.devkit/devkit-feedback"
if [ "$rc" -eq 0 ] && [ -n "$jsonl" ] && [ "$jsonl" = "$H9D/.devkit/devkit-feedback/$(basename "$jsonl")" ] \
   && ! printf '%s' "$out" | grep -q 'upstream: .*~/'; then
    ok "9d · default -> <HOME>/.devkit/devkit-feedback (absolute in the printed command)"
else
    bad "9d · default location" "rc=$rc jsonl=$jsonl out=${out:0:300}"
fi

# --- 9e: an invalid (relative) DEVKIT_FEEDBACK_DIR is ignored, as the contract says
T9E="$WORK/t9e"; mk_pending_target "$T9E"
H9E="$WORK/t9e-home"; mkdir -p "$H9E"
run_audit_env "$T9E" HOME="$H9E" DEVKIT_FEEDBACK_DIR="relative/logs"
run_printed_command "$WORK/t9e-run" "$H9E/.devkit/devkit-feedback"
if [ "$rc" -eq 0 ] && [ -n "$jsonl" ] && [ "$jsonl" = "$H9E/.devkit/devkit-feedback/$(basename "$jsonl")" ]; then
    ok "9e · relative DEVKIT_FEEDBACK_DIR ignored -> default location"
else
    bad "9e · invalid env value" "rc=$rc jsonl=$jsonl out=${out:0:300}"
fi

# =================================================================
# Default target (10): with no argument the audit checks the repo root it is run
# from (the directory holding .devkit-manifest.json), also when the script is
# installed as <repo>/.claude/devkit-audit.py.
# =================================================================
T10="$WORK/t10"
mk_basic_target "$T10" "original" >/dev/null
mkdir -p "$T10/.claude"
cp "$AUDIT" "$T10/.claude/devkit-audit.py"
out=$(cd "$T10" && python3 -S .claude/devkit-audit.py 2>"$WORK/err"); rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "rule.md" && ! printf '%s' "$out" | grep -q "nothing devkit-managed"; then
    ok "10a · no argument, run from the repo root -> finds the manifest"
else
    bad "10a · no-argument default" "rc=$rc out=${out:0:300}"
fi
out=$(cd "$WORK" && python3 -S "$T10/.claude/devkit-audit.py" 2>"$WORK/err"); rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "rule.md"; then
    ok "10b · no argument, run from elsewhere -> falls back to the repo root above .claude/"
else
    bad "10b · no-argument fallback" "rc=$rc out=${out:0:300}"
fi

echo "----"
echo "$pass/$((pass+fail)) passed"
[ "$fail" -eq 0 ]
