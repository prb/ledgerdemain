---
inclusion: always
description: Build only what the current workflow uses; avoid speculative code paths.
---

# Less Is More

Apply these rules whenever adding or changing sources, adapters, config options, CLI flags, or abstractions. Build only what the current workflow uses.

- **Name the user.** Before adding a source path, file format, adapter, config option, strategy, or CLI flag, name how it is used today. If you can't, leave it out.
- **One supported way in per source.** Pick one path for each source (API, OFX, or CSV) and document it. Don't add alternate formats "for flexibility".
- **No speculative abstraction.** No registries, protocols, or strategy switches with one implementation or no caller.
- **Every option needs a test and a real config that selects it.** Otherwise omit it.
- **Delete, don't keep.** When you find unused code while working nearby, remove it in the same PR or open an issue for it.
- **Say what you skipped.** The PR description lists any new flag, option, or code path and why it is needed, plus anything you chose not to build.
