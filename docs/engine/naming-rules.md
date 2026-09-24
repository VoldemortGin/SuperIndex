# PageIndex naming rules v1

> **Reference only.** This is the upstream PageIndex naming contract, kept for
> reference. superindex implements only the local allocation in
> `superindex/engine/naming.py` (used by the local store and Markdown
> ingestion). Upstream's Chat, Compute, cloud SDK, folders, `upload-url` and
> rollout do not exist here; the shared `naming-v1.json` fixtures (run in
> upstream's Vitest and pytest suites) are not part of this repository.

Names preserve case. Existing duplicate-name scopes and comparison behavior
are unchanged.

## New names

- Apply Unicode NFKC, then normalize quote variants as in Chat (`‘`, `’`,
  `ʼ` to an apostrophe; `“`, `”` to a double quote). Collapse whitespace using
  the JavaScript whitespace set and trim leading/trailing whitespace.
  This order is idempotent, including characters that expand into quotes.
- Allow Unicode text, emoji (including joined emoji), and interior spaces.
- Disallow `/ \ : * ? " < > |`, C0/C1 controls including DEL, and Unicode
  line/paragraph separators. Check controls before whitespace normalization.
- Disallow dot-only names, trailing periods, and case-insensitive Windows
  device names CON, PRN, AUX, NUL, COM0–9 and LPT0–9, also with extensions.
- Before resolving duplicates, names including extensions and truncation
  hashes must fit **180 UTF-8 bytes**. Automatically added short numeric
  collision suffixes may exceed this budget by the suffix length. Do not
  split a Unicode code point when truncating.

## Upload allocation

Replace disallowed characters with `_`, remove trailing periods, prefix
reserved device names with `_` (preserving the extension), and use `untitled`
when nothing remains. Replace unpaired surrogates with U+FFFD.

Shorten overlong names with `_` plus the first eight hexadecimal characters
of the MD5 of the cleaned name. Preserve the extension where possible; if the
extension itself exceeds the budget, shorten it too. MD5 is only a stable
label here, not a security mechanism. A duplicate adds `_1`, `_2`, etc.
before the extension. Allocators may shorten again to stay within the
180-byte budget; a short numeric suffix exceeding that budget is also allowed.
Each backend keeps its existing collision-attempt limit.

In superindex the local store is its own allocator and records the final
name with the document ID; everything runs locally through
`superindex/engine/naming.py`. The engine's PDF client (`local_api.py`)
sanitizes with `sanitize_filename` and adds `_1` … `_99` with
`truncate_filename`. `superindex index` (`md_ingest.py`) sanitizes the same
way, but re-indexing a file of the same name replaces the earlier copy
instead of adding a suffix.

(Upstream's folder creation/rename rules are omitted: superindex has no
folders.)

## Reading

Read assigned names literally. Apply no new cleaning, truncation or case
folding to persisted names or to a final upload name submitted for processing.
At the processing boundary, still reject path separators, controls and `.`/`..`
so the name remains one path component. Existing names are not migrated.
