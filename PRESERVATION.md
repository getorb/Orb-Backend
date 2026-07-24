# Preservation & Archival Policy

**This applies to the entire Orb project — this backend repo, the app repo, the `getorb`
GitHub organization, and the legacy private monorepo.** Mirror this file into every
Orb repository (app, wiki, and any new ones).

## The rule: never delete anything. Archive instead.

There are **no destructive actions, ever**. Do not delete repositories, branches, commits, tags,
CI workflows, files, or git history — not on the app, not on the backend, not anywhere in the org.

A large amount of code and workflow is being **deprecated** as the project moves to clean
production code on the getorb org. That is fine and expected — but *deprecated is not deleted*.
The **working set** should be clean; **everything else is kept in an archive state**, fully
restorable, so nothing is ever lost.

## How to retire something — by archiving

| Thing to retire | Do this (archive) | Never do this |
|---|---|---|
| **Branch** | Keep the ref; if moving it off the working set, push it to `archive/<name>` first. | Delete the branch. |
| **CI workflow** (Xcode Cloud, etc.) | **Disable** it. | Delete it. |
| **Repository** | GitHub → **Archive repository** (read-only, restorable any time). | Delete the repo. |
| **File / code** | Move to an `archive/` path, or rely on git history. | Lossy `git push --force` that rewrites/orphans history. |
| **Commit / tag / history** | Keep an archive ref pointing at it before any rewrite. | Force-push that drops commits with nothing pointing at them. |

Already-established archive refs (examples of the pattern):
`archive/pre-migration-main-0721`.
The old Xcode Cloud **"Default" workflow stays disabled**, not deleted, as a fallback.

## Backend-specific: how snapshots publish

This public repo is refreshed by a curated snapshot pipeline that **stacks each snapshot
as a new commit on top of the existing history** (clone → replace worktree → commit →
normal push). It must never force-push or rewrite history. Files removed from the
working set in a snapshot remain fully recoverable in this repo's git history.

## The migration goal

Operate exclusively from the **getorb** org — backend repo, app repo, wiki, and any new repos —
with a clean production codebase. When the cutover is complete, the legacy private
monorepo will be **archived** (GitHub Archive = read-only, fully preserved), **not deleted**.

## For any contributor or automated session

If a task appears to require a delete, a force-push, or a history rewrite, **stop and archive
instead**, and confirm with the owner before doing anything irreversible. When in doubt,
preserve. Archiving is always the correct default; deletion is never on the table.
