# Reader qualification selection (Phase 7A.1)

The existing Challenge book picker is expanded on Submit Book. Native BOTM and
Personal TBR lists and enhanced cover cards share one selection and the existing
actor-bound, entry-bound, expiring signature. Native lists work without JavaScript;
No selection removes intent. Check Qualifications renders feedback without saving
a submission. Request Review selects a target; the Reader must still Submit Book.
Original title/author and Hardcover edition evidence are preserved.

Suggestions are presentation only. Automatic qualification still uses the unchanged
live identity evaluator: trusted Hardcover work or exact normalized title/author,
plus existing eligibility and uniqueness checks. Suggestions may identify:

- Exact normalized titles of at least two words, with nonempty author names of at
  least five characters and a deterministic SequenceMatcher ratio of at least .8.
- Same-author, colon-delimited subtitle variations sharing a base of at least two
  words, or subtitle/edition ambiguity already supported by the live evaluator.

Catalog conflicts exclude suggestions. Volume, adaptation and abridgement markers
must agree, including one-sided markers. Only the current eligible Reader's active
Challenge BOTM and confirmed Personal TBR are queried. Qualified, pending and
previously rejected entries are excluded from new suggestions. At most three
suggestions per domain are offered; larger sets are suppressed. Punctuation alone
that normalizes identically remains subject to normal exact matching.

Request Review uses immutable Reader intent captured on new submission and existing
pending qualification relationships. A spelling/subtitle suggestion earns no credit
on its own; ordinary book approval does not confirm an ambiguous qualification.
Authorized qualification review and existing eligibility checks remain necessary.
There is no post-submission request endpoint or new persistent request model.

JavaScript Dismiss hides that suggestion for the current form's identity during the
page session. Without JavaScript, close the suggestion's Dismiss / show disclosure
and continue without selecting. Dismissal does not save anything or reject a match;
reloading/checking again can offer it again. Native controls permit changing or
removing a selection. Submission confirmation messages use actual saved match state.

The default read-only `report_missing_personal_tbr_matches` now calls `assess_entries`
and separates strong, ambiguous and ineligible candidates, with evidence reasons.
It does not run synchronizers or write scoring/relationships. Bonus projections
remain hypothetical; they do not reconstruct frozen historical decisions.

Historical reconciliation still calls this report with the internal
`legacy_candidates=True` compatibility option, preserving its prior candidate set
and apply behavior. Updating historical repair policy is a separate decision; do
not treat this diagnostic improvement as authorization to repair records.
