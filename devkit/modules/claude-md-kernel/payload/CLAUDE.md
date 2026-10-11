# {maintainer or team} — {agent-name}

This agent {one-line: what it builds/maintains/decides}. This file names the agent and points to the files that hold
its rules, so each rule is written down once (see `.claude/rules/self-contained-agents.md`).

---

## 1. About this agent

- Maintainer(s): {who maintains this agent and how to reach them}. Scope: {what this agent covers}.
- {how this agent should respond: tone, length}
- {optional: any working conventions for this agent, e.g. how it checks its own output; else delete}

## 2. Hard rules

Each rule is one line; the detail lives in whatever enforces it.

- Do not run destructive shell or git commands, or touch system and secret-store paths — enforced by the `settings.json` deny list
- Ask before sudo, curl/wget, git push, or publishing packages — enforced by `settings.json` ask gates
- Never commit .env files, credentials or keys — enforced by this file
- {Optional: who approves settings.json changes, and how; else delete this bullet} — enforced by this file
- {Optional: extra git rule; see `.claude/rules/git-hygiene.md`; else delete this bullet} — enforced by `.claude/rules/git-hygiene.md`
- {domain-specific hard rule, if any; else delete this bullet} — enforced by {a hook | settings.json | this file}

## 3. Where the rules live

- Git hygiene (branches, stashes, parallel sessions) → `.claude/rules/git-hygiene.md`
- Plan format + complexity-scaled gates → `.claude/rules/plan-writing.md`
- Agents ship self-contained; copy-on-build → `.claude/rules/self-contained-agents.md`
- Privacy: no personal absolute paths in shared artifacts → `.claude/rules/privacy.md`

## 4. Which skill to load

| When the request is about… | Load |
|---|---|
| {a request type that needs a specific skill or plugin} | {skill or plugin name} |

