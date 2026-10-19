# {maintainer or team} — {agent-name}

This agent {one-line: what it builds/maintains/decides}. This file names the agent and points to the files that hold
its rules, so each rule is written down once and this file only points to it.

---

## 1. About this agent

- Maintainer(s): {who maintains this agent and how to reach them}. Scope: {what this agent covers}.
- {how this agent should respond: tone, length}
- {optional: any working conventions for this agent, e.g. how it checks its own output; else delete}

## 2. Hard rules

Each rule is one line; the detail lives in whatever enforces it.

- Do not run destructive shell or git commands, or touch system and secret-store paths — enforced by the `settings.json` deny list
- {Optional: commands that need your confirmation first, e.g. sudo or network downloads; else delete this bullet} — enforced by the `ask` list in `settings.json`, if you configure one
- Never commit .env files, credentials or keys — enforced by this file
- {Optional: who approves settings.json changes, and how; else delete this bullet} — enforced by this file
- {Optional: extra git rule; see `.claude/rules/git-hygiene.md`; else delete this bullet} — enforced by `.claude/rules/git-hygiene.md`
- {domain-specific hard rule, if any; else delete this bullet} — enforced by {a hook | settings.json | this file}

## 3. Where the rules live

- Git hygiene (branches, stashes, parallel sessions) → `.claude/rules/git-hygiene.md`
- {Optional: how plans are written, if you install the plan-writing rule; else delete this bullet} → `.claude/rules/plan-writing.md`
- {Optional: if this repo builds other agents, how they are packaged (e.g. copied in rather than referenced at runtime); else delete this bullet} → `.claude/rules/self-contained-agents.md`
- Privacy: no personal absolute paths in shared artifacts → `.claude/rules/privacy.md`

## 4. Which skill to load

| When the request is about… | Load |
|---|---|
| {a request type that needs a specific skill or plugin} | {skill or plugin name} |

