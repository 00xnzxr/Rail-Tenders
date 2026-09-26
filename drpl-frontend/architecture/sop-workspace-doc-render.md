# SOP — Workspace Doc Render (HTML-from-DB)

## Goal

Render per-tender workspace documents. Documents are HTML strings persisted on the backend; the frontend's job is render + edit + save.

## Inputs

- **Backend producer.** Workspace document rows from `tender_documents` / `workspace_documents` tables.
- **Render path.** `src/components/workspace/`.
- **Migration boundary.** The 2026-04-06 `_migrate_workspace_content()` migration ([../../CLAUDE.md](../../CLAUDE.md) item 5) converted all markdown-format docs to HTML. **Going forward, all stored content is HTML.**

## Outputs

Rendered editable HTML in a rich-text editor (TipTap or equivalent). Save POSTs back to the backend.

## Render contract

```jsonc
{
  "id": 123,
  "tender_id": 456,
  "title": "Cost Statement",
  "content_html": "<p>...</p>",
  "version": 7,
  "updated_at": "ISO-8601"
}
```

## Determinism boundary

The frontend renders bytes. It does **not** transform markdown to HTML, sanitize content (the backend sanitizes on write), or merge edits with server state (use optimistic locking via the `version` field).

## Rules

1. **HTML only.** No markdown library imported in workspace-render paths. If a doc returns markdown (legacy bug), surface an error — do NOT silently render markdown-as-text.
2. **Version-aware saves.** Every save sends the `version` field. Backend rejects on mismatch (409); UI prompts the user to reload.
3. **No client-side templates.** Document templates are owned by the backend's format-template system. Frontend only consumes the rendered HTML.

## Edge cases

1. **Empty doc.** Render an empty editor — never default-fill from a template client-side.
2. **Doc references an image in R2.** Image URLs are presigned ([storage_service.py:192-208](../../drpl-backend/app/services/storage_service.py)) — they expire. Re-presign on each load.
3. **Concurrent edits.** Two users editing same doc → second save returns 409. UI must surface "Document changed, reload to see latest" — do not auto-merge.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Doc renders as `## Heading` text instead of HTML heading | Backend stored markdown (legacy doc that missed migration) | Backfill: re-run `_migrate_workspace_content` for that doc id |
| Image URLs expired mid-session | R2 presigned URL TTL exceeded | Re-fetch the doc; frontend should not cache URLs longer than 1h |
| Save returns 409 silently | Optimistic-locking error not surfaced | Add a toast — "Document changed, reload to see latest" |

## Verification

```bash
# 1. Open a tender's workspace
# /tenders/:id/workspace/cost_statement

# 2. Edit + save; confirm the save POST includes `version`
# (DevTools Network tab)

# 3. Verify backend stores HTML (not markdown)
psql -c "SELECT id, title, LEFT(content_html, 100) FROM workspace_documents LIMIT 5;"
# All rows should start with HTML tags (<p>, <h1>, etc.), not # or **
```

## Open work

None. The migration is complete; the contract is stable.
