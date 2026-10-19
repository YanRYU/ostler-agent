# Privacy in shared artifacts

Do not write personal absolute paths such as `/Users/<name>/...` or `/home/<name>/...` into files shared through Git. Use a repository-relative path, `$HOME`, or a documented project environment variable.

Local shell configuration, ignored secrets, and user-level Codex configuration are outside this shared-artifact rule. The optional Codex hook checks supported local edit calls; it cannot observe every write path.
