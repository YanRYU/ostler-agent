# Git Hygiene — Parallel Sessions, Branches, Stashes

**Always-on. Applies in every repo. Surfaced by the
`hooks-core` hooks `branch-similarity-check.sh` (PreToolUse) and `stash-leak-check.sh` (PostToolUse)
once you wire them into `settings.json`.**

---

## Multi-session branch safety

If you run several agent sessions in parallel on the same repo, give each its
own feature branch. **Before any commit, edit, or stash operation that mutates repo
state, run `git branch --show-current` and verify the branch is yours.**
If on another session's branch:

    git stash push -m "<your-work-tag>" --include-untracked
    git checkout <trunk> && git pull <remote> <trunk>
    git checkout -b <your-new-branch>
    git stash pop

Failure mode prevented: committing onto another session's PR, polluting their
diff, or losing work to a wrong-branch commit.

## Stash recovery discipline

**Every `git stash push` the agent creates MUST be resolved by session end.**
Valid dispositions:
1. **Pop** back onto the branch the work belongs to.
2. **Drop with named reason** (superseded, abandoned, merged elsewhere).
3. **Explicit handoff**: tell the user which stash you created, why, and that they need to decide what to do with it.

Forbidden: walking away with agent-created stashes in `git stash list`.
A stash the agent pushed is always the agent's to resolve.

Trigger points — run `git stash list` and resolve agent-created entries:
after any `git push`; after any commit on the stash's branch; before session close.

Pre-existing stashes (present at session start) belong to past sessions or the
user — do not pop or drop without explicit instruction. If you can't tell
which is which: ask, don't guess.
