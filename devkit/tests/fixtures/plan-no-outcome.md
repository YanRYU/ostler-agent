# Plan: rename the config keys

1. Rename the keys in config.yaml from snake_case to kebab-case.
2. Update the loader to read the new names.
3. Rollback: revert the commit.
4. Verify: run the end-to-end test suite and read the summary line.
