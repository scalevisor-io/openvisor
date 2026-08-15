# @openvisor/shared-ui

Framework-light React components and types shared between the **Openvisor spoke SPA** (`app/`) and the **Scalevisor hub customer console** (§hub pass-through), so the customer project experience is authored once.

## How it's consumed

There is **no build step**. Both apps compile the TypeScript source directly through a path alias (`@shared-ui` → this folder's `src`):

- **Spoke** (`app/`): the alias points at the sibling `../shared-ui/src` in-repo.
- **Hub** (closed source): vendors this folder at a pinned spoke commit (`app/SHARED_UI_REF`) via `scripts/vendor-shared-ui.sh` into `app/vendor/shared-ui`, then aliases `@shared-ui` there.

## Contract

- Every component is **pure/presentational** - props in, JSX out, no data-fetching, no app-specific `lib/` imports. Data access is abstracted behind the `ProjectApi` interface (`types.ts`), which each app implements against its own transport (the spoke's own REST, the hub's `/api/customer/me/*` proxy).
- **Security invariant (do not weaken):** `MessageBody` renders message text as markdown through `react-markdown` (+ `remark-gfm`, `remark-breaks` - peer dependencies the hub must install when it vendors this folder). No raw HTML ever renders (react-markdown builds a React element tree; no `rehype-raw`, no `dangerouslySetInnerHTML`), link hrefs are allowlisted to `http`/`https` only (any other scheme renders as plain text), and images never render - their alt text does. A hub renders spoke-attested and customer-authored text through this same code, so loosening the scheme allowlist is a stored-XSS vector and letting `<img>` through makes a stored message a tracking pixel against every reader.

## License

Apache-2.0 (see `LICENSE`) - permissive with an explicit patent grant, pinned before the spoke's open-source release so neither the spoke's future license nor the hub's closed source constrains the other.
