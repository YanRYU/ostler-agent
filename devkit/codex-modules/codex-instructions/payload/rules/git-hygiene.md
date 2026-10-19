# Git hygiene

Before a commit, edit, or stash that changes repository state, check the branch and working tree. Keep concurrent sessions on separate branches. If the current branch belongs to another session, stop and choose a branch for this work without discarding that session's changes.

Resolve each stash created in this session before finishing: pop it onto its intended branch, drop it with a recorded reason, or hand it off explicitly to the user. Do not change stashes that existed when the session began without authorization.

The optional Codex branch and stash hooks in `.codex/hooks/` surface relevant state. They are notices, not a complete Git enforcement boundary.
