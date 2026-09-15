# umt-shared.css — vendored

Byte-identical copy of the UMT shared visual language, so this app reads as part of the
same product family as the TAK UMT plugin UIs (Logs, User Management).

| | |
|---|---|
| Source | `umt-plugin/tak-ui-shared/styles.css` |
| Upstream commit | `a37eaeeb` (2026-09-01) |
| Vendored | 2026-09-15 |

## Why a verbatim copy

Kept unmodified so `diff` against upstream stays meaningful when re-syncing. Everything
this app needs to change about it — including two upstream bugs — lives in `styles.css`,
which loads second and overrides.

To re-sync:

    cp ../../../umt-plugin/tak-ui-shared/styles.css umt-shared.css
    # then re-check the two fixes at the top of styles.css are still needed

## Upstream bugs patched in styles.css

- **`--mono` is used but never defined.** `tak-ui-shared` references `var(--mono, monospace)`
  twice but only `umt-shell/styles.css` defines the token, so inside a sub-app it silently
  falls back to bare `monospace`. We define it.
- **`.pill.success` is unreadable.** It pairs `background:#dff4ea` (pale green) with
  `color:var(--good)` (`#65e1a0`, also pale green). The newer `.pill--good` modifier is the
  correct one. We fix the legacy variant rather than avoid it.

## Unused upstream CSS

Roughly 60% of this file is specific to the Logs and User Management apps (`.um-*`,
`.log-*`, `.disk-*`, `.federation-list`). It is kept rather than pruned so the file stays
diffable. Do not build on those classes here — they are not part of this app's contract.
