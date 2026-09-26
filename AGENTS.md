# r2-sync Project Operations

## Global Agent Harness

This Project uses the `development` Harness Profile. Universal safety, secret protection, Main Agent ownership, context boundaries, subagent use, model escalation, approval, and auditability follow the user-level Global Invariants and cannot be overridden by this Project.

The Main Agent continuously owns the normal loop: `understand → investigate → plan → implement → deterministic verification → diagnose/fix → self-review → risk assessment`. Use subagents only for large-scale exploration, independent specification research, non-dependent parallel work, or independent review; do not hand off task ownership.

Project-specific verification consists of `npm test`, `npm run build` when needed, public-content checks, and dry-run result review. Treat `npm run sync:apply`, `npm run sync:full`, R2 changes, Vault writes, deletions, and publication as external side effects; execute them only after confirming the target, scope, and impact and receiving explicit approval. Treat `--allow-delete` as especially irreversible.

Do not copy common safety policy or the Harness Core into this repository. The Project Policy below keeps only the differences for public-repository constraints, R2/Vault synchronization, dry-runs, and prohibitions on personal information and secrets.

This is a public repository. Do not bring personal environment information or operations that cannot be generalized for public users into the repository.

## Placement Boundaries

- Store requirements in `REQUIREMENTS.md`, shared design in `DESIGN.md`, and implementation-specific design in `DESIGN_NODE.md` and `DESIGN_PYTHON.md`.
- Keep the Node.js source in `src/` and the iOS/a-Shell Python source in `py/`.
- Generated bundle locations inside a user's Vault are not canonical sources.
- Users manage their own Vault paths, shortcuts, logs, and settings.

## Public Repository Prohibitions

- Do not record or commit personal names, personal absolute paths, Vault names, device-specific IDs, account IDs, real bucket names, or real endpoints.
- Do not record or commit API keys, passwords, cookies, tokens, authenticated responses, or raw logs.
- Do not put a personal Vault's folder structure or user-specific integration procedures in requirements, design documents, or the README.
- Use placeholders such as `<project-root>`, `<vault-path>`, `<account-id>`, and `<bucket-name>` in examples.
- Store user-specific investigation results and environment constraints outside the public repository.

## Pre-Publication Harness Checks

Run public-content checks first as part of this harness's workflow. GitHub Actions automation is not required; add it only when separately decided.

For normal commits, also run the version-controlled `.githooks/pre-commit`. After cloning, enable it with `npm run setup-hooks`. The hook checks only the staged diff locally and performs no external communication.

Before committing, verify the following:

1. Only intended files are changed.
2. No personal paths, Vault names, device-specific information, or real bucket information is present.
3. No API keys, passwords, cookies, tokens, authenticated responses, or raw logs are present.
4. No populated `config.json`, sync-state file, or personal configuration is staged.
5. Documentation examples use placeholders.
6. Requirements, design, and code changes are generalizable to public users.

The pre-commit hook checks the mechanically decidable items above and runs `npm test`. The harness must inspect the full public content instead of relying only on the hook result. Do not bypass it with `--no-verify`.

If any item cannot be judged, stop the commit and investigate.

## Change Procedure

1. Update `REQUIREMENTS.md` first for requirement changes.
2. Update `DESIGN.md` for structural changes.
3. Implement in `src/` or `py/`.
4. Run `npm test` for the Node.js version.
5. Generate a bundle with `npm run build` only when distribution is required.
6. Before publication, check for personal information, secrets, environment-specific paths, and uncurated logs.

## Security

- Do not read or commit populated `config.json`, access keys, passwords, or personal absolute paths.
- Put placeholders only in `config.example.json`.
- Do not store iOS state files, logs, or credentials in the Vault.
- Probing production R2 is forbidden; use a test bucket and a dedicated key.

## iOS Operations

- The initial version is iOS Shortcuts → a-Shell `In App` → Python pull-only.
- Deploy iOS `py/sync.py` and `py/r2sync/` together from this Project to the execution area on the iPhone, and reference them through a Shortcuts bookmark.
- Start iOS in the current compatibility mode that scans the entire Vault and enumerates all R2 objects.
- Measure the five-second target through completion of direct file replacement; measure Obsidian refresh time separately.
