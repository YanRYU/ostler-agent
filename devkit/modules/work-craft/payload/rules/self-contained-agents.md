# Self-Contained Agents

**Always-on. Applies when your repo builds or ships agent configuration.**

---

1. **Agents ship self-contained.** A repo you install into must work without the
   machine or checkout it was installed from: the agent must not read memory, skills, or
   scripts from a user's home config at run time.
   If an agent needs knowledge, the knowledge moves into the agent's repo.

2. **Copy-on-build, never symlink, never runtime-reference.** Capability flows
   downstream by versioned copy. After install, the target works with the repo it was
   installed from deleted.

3. **One constraint, one layer.** State each rule once, in the file best able to
   enforce it; other files only point to it.

   | Layer | File | Job |
   |---|---|---|
   | Permissions | `settings.json` | Block or gate dangerous actions |
   | Hooks | `hooks/` | Detect and flag at run time |
   | Rules | `rules/` | Instruct the agent |
   | Entry file | `CLAUDE.md` | Point to the above |
   | Audit | a script or review | Verify the others agree |

4. **Prefer generated inventories.** Counts and registries (how many rules, which files
   exist) drift when typed by hand: generate them with a script, or omit them. If you do
   hand-write one, keep it next to a check that fails when it drifts. Pointers and
   decisions do not drift.

5. **Build a module only for a current need.** Add one when a concrete user or repo needs it now, not for hypothetical future demand (YAGNI).
