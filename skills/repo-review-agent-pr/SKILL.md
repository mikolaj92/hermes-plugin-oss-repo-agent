# Review agent PR workflow

Review generated pull requests without merging them.

Use `gh pr view`, `gh pr diff`, and `gh pr checks` to verify linkage, labels,
branch prefix, test evidence, and safety constraints. If executing code locally,
use an isolated checkout. Return one verdict: approve, request changes, or
blocked.

Do not close, replace, supersede, merge, or take over third-party pull requests.
