# Personal TBR custom covers (Phase 7A.3)

Before this phase, PersonalTBRBook stored cover_url_snapshot and display_cover_url,
which may contain edition-specific Hardcover or manual image URLs. It had no
uploaded artwork override. Provider display refresh remains unchanged.

Migration core.0082 adds only nullable custom_cover to PersonalTBRBook. Keeping the
column nullable permits old-source inserts during a source rollback; no data
backfill, selection changes, new model or permission expansion is required.

Readers can use Upload Cover / Change Cover on each saved Personal TBR card,
including locked cards. A separate small multipart form uploads/replaces artwork
or removes the override. Both saved unlocked and confirmed lists are supported;
unsaved registration selections still use their existing cover URL previews.

The route and service independently require the active owning Reader, active
membership/enrollment, correct Group/Challenge context and existing Challenge
visibility. Current artwork lifecycle restrictions are retained: Draft (only when
visible), Upcoming and Active. Finalizing, Completed, Archived and inactive
participation remain read-only. Existing administrator/provider metadata refresh
permissions are untouched; this feature grants no cross-Reader staff authority.

Uploads reuse branding.image_asset and the existing storage backend: PNG/JPEG/WebP,
5 MB maximum, at most 6000 pixels per side and 16 million source pixels. The helper
verifies actual image content, applies EXIF orientation, strips metadata and
normalizes to PNG. Cover thumbnails fit within 800 x 1200 without changing aspect
ratio. Names are generated UUIDs under tbr-covers/<tbr-id>/; supplied filenames or
remote URLs never become stored artwork paths. No dependencies or media settings
change.

The update service owns its transaction, locks Challenge/enrollment/TBR/entry,
rechecks authority and verifies a 30-minute actor/entry/identity/current-cover
signature. It updates only custom_cover using an explicit column whitelist. The
ordinary PersonalTBRBook save/validation lock is unchanged. Confirmation, all book
snapshots/order/associations, progress, matches, submissions and awards are never
saved or synchronized by cover operations.

Priority is custom artwork (when the stored file exists), then the existing
provider/manual display-or-snapshot cover, then the shared placeholder. All uses of
the Personal TBR effective cover property inherit this priority; BOTM/submission
cover behavior is unchanged. The shared URL filter additionally accepts only the
strict generated local custom-cover namespace, not arbitrary local media paths.

Replacement/removal follows existing artwork conventions by retaining prior files.
No shared/provider asset is deleted. If storage succeeds but the database/audit
write fails, the service rolls back and deletes its own newly saved UUID file when
it is unreferenced. Cleanup/storage failures can leave an unreferenced orphan;
existing references are kept safe. Caller-owned outer transactions are rejected so
a later outer rollback cannot turn a successful service return into a leaked file.
No media pruning is introduced.

AuditEvent records personal_tbr.cover_uploaded / cover_replaced / cover_removed
against PersonalTBRBook with artwork_only=true and before/after storage references.
These are separate from selection, submission-correction and qualification audits.
No matching, scoring, reconciliation or Discord producers are called.

No historical write expansion, identity unlocking, staff permission redesign,
submission management redesign or Discord activation belongs to this phase.
