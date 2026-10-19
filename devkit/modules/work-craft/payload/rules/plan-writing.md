# Plan Writing Rules

**Always-on. Applies to every plan written from this agent.**

---

## Complexity-Scaled Gates

| Change Size | Indicator | Gates Required |
|-------------|-----------|----------------|
| Trivial | One file, no downstream impact, pattern exists | None |
| Small | 2–5 files, single skill/rule | Self-review before presenting |
| Medium | A file other repos or people depend on, OR a change to a documented contract | A short note listing what else is affected + self-review |
| Large | New capability category, new system, architectural | Research step (a written research note: what was found, sources, open questions) + a short note listing what else is affected + self-review |

---

## Before Writing Any Plan

1. **Read the target files yourself** before planning. A search tool or
   subagent may find the paths; you read the content.
2. **Search for prior art** — before proposing a new artifact, grep for existing
   skills/rules that solve the same problem. Cite the search and results.
3. **Read the CLAUDE.md and settings.json** of any repo the plan will change
   (just-in-time, before touching it).
4. **For any rule** referenced in the plan — cite the source file. Never write rules
   from intuition.

---

## Required Plan Format

### Files Read (mandatory)
Every file actually read before writing this plan:
- `path/relative/to/repo` (lines X–Y) — what was learned

If a section references behavior not verified by a file in this list, mark it INFERRED.

### Commands Run (mandatory when plan claims runtime behavior)
Any claim about hook/validator/script behavior cites actual command output — not the
script's apparent intent. Reading tells what it SHOULD do; running tells what it DOES.

### Design Decisions (table)
| Decision | Rationale | Alternative Considered |

### What Is NOT in Scope (explicit boundary)

### Implementation Sequence (ordered, with dependencies)

---

## Do NOT

1. Do NOT write a plan section describing behavior not verified by a direct file read.
2. Do NOT rely on a subagent's summary of a file in place of opening the file you cite.
3. Do NOT propose a new artifact without searching for existing prior art.
4. Do NOT ship a Large-class change without a research step producing a written research note first.
5. Do NOT claim hook/script behavior without a Commands Run entry verifying it.
