---
inclusion: always
---

# Git Workflow

Apply these rules for all version-control work in this project. The default branch is
`main`; if a repo you're working against uses a different default (e.g. `master`),
adapt the branch names accordingly.

- **Open an issue before branching.** For any non-trivial work, create a GitHub issue first (`gh issue create`) that records the problem, the proposed change and the scope, so the intent is documented before there is code. Branch from it, and put `Closes #<issue>` in the pull request. Creating this issue is standing permission and needs no separate confirmation. A trivial change (a typo, a one-line fix) may skip the issue.
- **Branch before building.** Start non-trivial work on a feature branch off an up-to-date `main`, never commit directly to `main`. Name branches `<type>/<issue>-<short-description>`, where `<type>` is `feature`, `fix`, or `chore` (for non-code changes) and the issue number comes first (the main one, if the branch closes several). Work with no issue may use `<type>/<short-description>`. Examples: `feature/9-solo-401k-profit-sharing`, `fix/12-lot-key-collisions`.
- **Prefer chunkier pull requests.** Group related issues into one branch and one PR (`Closes #A`, `Closes #B`) instead of a PR per small issue, so a reviewer sees one coherent change and CI and approvals aren't repeated for each step. Split only when the pieces are independent, or too risky to review together.
- **Commit locally, push when asked.** Commit as you go in your worktree: scoped commits with clear messages. Keep the work local until it is ready for review. When asked, push the branch with upstream tracking (`git push -u`) and open a pull request for review rather than merging to `main` directly. The one exception is review follow-ups (next bullet).
- **Review follow-ups are pushed.** A request to address review feedback on a pull request covers pushing the fixes. Make the changes, run the checks, commit them as new commits on the PR branch (no `--amend`, no force-push), push, and update the PR description if it is now out of date. A push can dismiss earlier approvals, so say in chat that the PR needs another look.
- **Stage deliberately.** Add specific files by name. Avoid `git add -A` / `git add .` so unrelated in-progress work is not swept into a commit.
- **Don't commit secrets.** Never stage `.env` files, credentials, keys, or tokens. Flag them if you notice them staged or in the working tree.
- **Keep commits scoped.** One logical change per commit. Do not commit unrelated changes that happen to be present in the working tree.
- **Write clear messages.** Use a concise imperative subject under ~70 characters; use the body to explain the *why* when it is not obvious.
- **Prefer new commits over rewriting history.** Avoid `--amend`, `rebase`, force-push, `reset --hard`, and `clean -f` unless explicitly requested.
- **Preserve hooks.** Do not skip hooks (`--no-verify`) unless explicitly asked.
- **Post PR reviews on GitHub.** When asked to review a pull request, submit the review on the PR via `gh pr review` (approving, requesting changes, or commenting as appropriate) in addition to summarizing findings in chat.

### Worktrees
- **The primary checkout stays on `main`.** Update it only with `git fetch` followed by `git merge --ff-only origin/main`. Never commit, switch branches or edit files there.
- **Do all branch work in a worktree under `.worktrees/`.** Create one per branch from an up-to-date `origin/main`, named after the branch with `/` replaced by `-`, and use `--no-track` so the new branch does not inherit `origin/main` as its upstream (a bare `git push` could then target `main`): `git worktree add --no-track .worktrees/<type>-<short-description> -b <type>/<short-description> origin/main`. Run builds, tests and commits from inside the worktree.
- **Clean up after a merge.** Fast-forward the primary checkout, then remove the merged worktree (`git worktree remove .worktrees/<name>`) and delete its local branch.
- **Leave stray changes alone.** If uncommitted or staged changes appear in the primary checkout, do not commit or discard them; move them into a worktree or ask before removing them.
- **Check out other people's branches separately.** Review checkouts and branches owned by someone else go in their own worktree, added detached (`git worktree add --detach .worktrees/review-<pr> <sha>`), and are removed when done.
