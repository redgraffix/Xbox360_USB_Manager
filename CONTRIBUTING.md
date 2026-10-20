# Contributing

Keep the workflow simple: choose folders and a USB, queue files, verify the transfer, and report what happened. Preserve conservative deletion and duplicate checks when changing the interface.

Run the non-GUI tests listed in the README for conversion changes. Run the UI regression scripts for interface/worker changes. Use temporary directories and synthetic fixtures; tests must never discover and operate on a real user's game drive.

Before committing:

1. Review `git status` and `git diff --cached`.
2. Stage specific source/documentation files. Never force-add archives, game content, settings, history, diagnostics, or executable bundles.
3. Run `python scripts/check_public_tree.py` after staging.
4. Check that your Git author/committer identity uses your public alias and GitHub no-reply email.

Keep new dependencies limited and documented. Archive tools must come from their upstream publisher; do not commit downloaded binaries. Do not add download-site automation, account credentials, or telemetry to solve local file-management problems.

Use the security reporting guidance in SECURITY.md for vulnerabilities. Public bug reports should contain only redacted information and synthetic reproductions.
