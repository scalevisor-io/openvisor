# @openvisor/shared-ui

Framework-light React components and types shared between the **Openvisor spoke SPA** (`app/`) and the **Scalevisor hub customer console** (§hub pass-through), so the customer project experience is authored once.

## How it's consumed

There is **no build step**. Both apps compile the TypeScript source directly through a path alias (`@shared-ui` → this folder's `src`):

- **Spoke** (`app/`): the alias points at the sibling `../shared-ui/src` in-repo.
- **Hub** (closed source): vendors this folder at a pinned spoke commit (`app/SHARED_UI_REF`) via `scripts/vendor-shared-ui.sh` into `app/vendor/shared-ui`, then aliases `@shared-ui` there.

## Contract

- Every component is **pure/presentational** - props in, JSX out, no data-fetching, no app-specific `lib/` imports. Data access is abstracted behind the `ProjectApi` interface (`types.ts`), which each app implements against its own transport (the spoke's own REST, the hub's `/api/customer/me/*` proxy).
- **Security invariant (do not weaken):** `MessageBody` linkifies URLs with an `http`/`https`-only scheme allowlist and never uses `dangerouslySetInnerHTML`. A hub renders spoke-attested and customer-authored text through this same code, so loosening the allowlist would be a stored-XSS vector.

## License

Apache-2.0 (see `LICENSE`) - permissive with an explicit patent grant, pinned before the spoke's open-source release so neither the spoke's future license nor the hub's closed source constrains the other.
