---
inclusion: fileMatch
fileMatchPattern: '*.ts|*.tsx'
---

# TypeScript Coding Guidelines: Browser App (Local API)

Apply these rules when writing or editing TypeScript in this project. They assume a
browser app that talks to a local (`127.0.0.1`) API. When a rule and the project's
framework conventions genuinely conflict, prefer the framework and note the exception.

## 1. Type Safety & Local API Contract
- **No `any`:** Never use `any`. Use `unknown` with type guards or runtime validation when an API response is dynamic.
- **Explicit API contracts:** Declare strict interfaces/types matching the exact payload schemas returned by the `127.0.0.1` endpoints. Keep these contracts in one place so they can be reused and updated together.
- **Explicit return types on the public surface:** Declare return types for data-fetching functions, event handlers, and exported utilities. Local, trivial helpers may rely on inference — don't add noise where the type is obvious.
- **Strict null checks:** Treat local API responses as possibly incomplete. Reach for `?.` and `??`, and handle `null` / `undefined` explicitly rather than assuming data is populated.

## 2. Browser Environment & Stylistic Rules
- **DOM types:** Use the correct browser/React event types (e.g. `React.ChangeEvent<HTMLInputElement>`, `MouseEvent`, `SubmitEvent`). Never cast events to `any`.
- **Interfaces vs. types:** Use `interface` for UI component props and API models. Use `type` for state unions, action definitions, and literal configurations.
- **No enums:** Prefer string literal unions (e.g. `type ViewMode = 'list' | 'detail'`) over `enum` for easier state management and debugging.
- **Named exports:** Prefer named exports for components, hooks, and utilities so auto-imports and naming stay consistent. Exception: use a default export only where the framework or tooling requires it (e.g. route/page modules).

## 3. Local Connectivity & Architecture
- **Environment-bound base URL:** Don't hardcode `http://127.0.0.1`. Define it once in a central config or environment variable, typed as a strict string literal.
- **Defensive fetch layer:** Wrap fetch/axios calls with strongly typed error handling. Explicitly handle local-service failures (connection refused, network errors) rather than letting them surface as untyped exceptions.
- **State immutability:** Treat UI state as read-only. Use `const` and pure array methods (`.map()`, `.filter()`) instead of mutating state in place.
