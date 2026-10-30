# Security and privacy

## Scope

This is a local Windows file-management utility. It makes no network requests, exposes no server, accepts no remote commands, stores no account credentials, and sends no telemetry. Run it as a normal user with trusted local settings and an updated Python/7-Zip installation.

The public source is the supported development baseline. The optional build script creates an unsigned Windows executable from that source. Older privately built executables may differ; rebuild to pick up source changes. No signed release or packaged installer is provided yet.

## Protections and limits

- Output paths are checked for containment. Traversal, Windows reserved names, symbolic links, and reparse points are rejected in the supported import paths.
- The app remembers a selected FAT32 volume by identity and checks it during work. That identity is a convenience against drive-letter mistakes, not cryptographic authentication of a device.
- Inputs are retained on failed verification. Copy publication and automatic source deletion require successful verification and saved records. Keep mode is the first-run default.
- Existing conflicting files are not intentionally overwritten. Removal/cleanup are limited to selected content or recognized work folders.
- The archive helper is launched with an argument list, without a shell. Default lookup uses Program Files; an explicit executable path is trusted user configuration.
- Game executables are treated as data, not launched by the converter.

These checks do not make the application a sandbox or malware scanner. Malformed content can still stress parsers, CPU, memory, and storage. 7-Zip is a security-sensitive external dependency: update it from its official source. Do not run with administrator privileges or deliberately point the app at hostile shared folders. Other processes changing folders concurrently, hardware faults, and power loss can defeat filesystem assumptions or interrupt a multi-file publication. Keep a recoverable copy until console testing succeeds.

Settings, queue history, and verification manifests are trusted local data. They are not signed. Do not replace them with files supplied by another person or put them in a folder writable by untrusted users.

## Public repository hygiene

Keep inputs, USB content, archives, logs, history, drive identifiers, private paths, access tokens, keys, and runtime/build outputs outside version control. Synthetic test fixtures are generated during tests; copyrighted game files are not fixtures.

`python scripts/check_public_tree.py` checks tracked/staged filenames, file sizes, common secret patterns, private-network addresses, literal home-directory paths, and commit email metadata. It is a lightweight prevention check, **not** an exhaustive secret scanner or a security certification. Review `git diff --cached` before every push. Use a GitHub no-reply email for public commits.

## Reporting a vulnerability

Use the repository's **Security → Advisories → Report a vulnerability** option if private reporting is enabled. If it is unavailable, open a public issue asking the maintainer to enable a private reporting channel, without including exploit details, credentials, logs, or personal information. Do not publish a sensitive report in a normal issue.

Provide the affected commit, Windows/Python/7-Zip versions, expected behavior, and a minimal synthetic reproduction. Do not include real game archives, private paths, volume identifiers, or original user logs.

## Maintainer settings

Enable GitHub private vulnerability reporting, secret scanning/push protection where available, and review repository access under Settings. These are GitHub settings, not features enabled by this file. The source workflow has read-only repository permissions, pins its action dependencies by commit, and does not use repository secrets. Dependabot is configured for action updates.

If a secret is published, revoke or rotate it first. Rewriting Git history does not revoke credentials or erase other people's clones or GitHub's cached commit views. Follow [GitHub's removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository) for any remaining exposure.
