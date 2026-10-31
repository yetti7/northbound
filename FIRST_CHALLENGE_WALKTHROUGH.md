# Northbound — First Challenge Training Walkthrough

Theme guidance includes the percentage-bonus update; pre-existing Themes retain legacy fixed-page rules.

A hypothetical November Reading Challenge for Northbound v1.0.1.

**This is a training scenario, not an instruction to create application data during this investigation.** No users, Groups, Challenges, submissions or database records were created. Read through the scenario first. Any later hands-on exercise belongs on a separately authorized disposable installation, with provider writes disabled unless explicitly being tested.

Use the [Administrator Guide](ADMIN_GUIDE.md) for all permission/settings details and the [Reader's Guide](READER_GUIDE.md) when rehearsing member instructions. Names, book titles and scores below are invented examples, not application records.

## Contents

1. [Preparation](#1-preparation)
2. [Create the community Group](#2-create-the-community-group)
3. [Assign staff](#3-assign-staff)
4. [Create the Challenge](#4-create-the-challenge)
5. [Configure registration](#5-configure-registration)
6. [Create teams](#6-create-teams)
7. [Register Readers](#7-register-readers)
8. [Start the Challenge](#8-start-the-challenge)
9. [Submit and review books](#9-submit-and-review-books)
10. [Track progress](#10-track-progress)
11. [Complete the Challenge](#11-complete-the-challenge)
12. [Additional formats](#12-additional-formats)
13. [Practice checklist](#13-practice-checklist)

## 1. Preparation

Imagine a Discord community, **Northbound Reading Circle**, preparing **November Reading Challenge** for November 2026. Choose `America/New_York` as the Group timezone. November crosses a daylight-saving change; always read local timestamps in the configured timezone rather than assuming a constant UTC offset.

### People and accounts

| Person | Account / planned role | Responsibility |
| --- | --- | --- |
| Morgan | Platform Owner account, separate from normal organizer account | Installation policies, backups, optional OAuth, exceptional recovery. |
| Alex | Normal account, Group Owner and appointed Challenge Host | Community access and rules; Challenge configuration, rosters, review and final results. |
| Sam | Normal account, Moderator | Group announcements and Challenge TBR assistance by default; other Group powers must be deliberately delegated. |
| Riley | Normal account, Reader then Cedar Team Leader | Compete and review the Cedar team's books/claims. |
| Jordan | Normal account, Reader then Birch Team Leader | Compete and review the Birch team's books/claims. |
| Casey | Normal account, Reader | Register, read, submit and follow progress. |
| Taylor | Optional normal account, Floater | Non-competing Challenge-wide review support; no active enrollment/team. |

Platform Owner access is not a participation identity. Alex can be both Group Owner and Host, but the Host assignment must actually exist. Team Leaders must first be enrolled and placed on their teams.

### What to agree beforehand

Prepare the Group name, timezone, private invitation method, staff roster, registration questions, dates, review expectations and whether scores should be visible. For this first event:

- Start with ordinary book submissions and two teams.
- Keep BOTM, Personal TBR and Games off for the core rehearsal; practice them separately in section 12.
- Set Reader Scores and Team Standings to Everybody if community-wide competition visibility is desired. Their defaults are Hosts only.
- Use manual completion after final review.
- Use a fixed checkpoint target because this new Group has no Completed Challenge history.
- Explain that Readers cannot normally edit submitted book entries.

The deployment operator separately prepares private first setup, public HTTPS origin, persistent storage, secrets and backups. See [initial configuration](ADMIN_GUIDE.md#3-initial-platform-configuration). No deployment commands are part of this rehearsal.

## 2. Create the community Group

**Act as Alex, normal organizer account. Required:** Allow Normal Accounts to Create Groups enabled, or arrange creation through a Platform Owner.

1. On **Sign In**, use **Create an Account** if permitted. Enter username/email/password, then **Create Account**. On an initialized account, just Sign In.
2. Home → **Create Group**.
3. Enter **Name:** Northbound Reading Circle; **Timezone:** America/New_York.
4. Check **Enable Group Announcement** and enter: “Welcome! Join November Reading Challenge when registration opens. Ask Alex about account or team questions.”
5. Leave **Hardcover API Token** blank for a manual-only start. If shared catalog search will be tested later, a Group-owned key is configured separately by someone with Group settings authority.
6. Select **Create Group**. Alex becomes Group Owner; the Group receives its code.
7. Open **Access Code**, privately distribute **Current Code**, and retain the default **Group owners only** visibility unless a broader invitation policy is intended.
8. Have staff with existing accounts use Home → **Join With Code → Access Code → Save**.

**Expected:** members appear in **Members**, without automatic Challenge enrollment or teams. For someone without an account, Alex can use **Members → Add Member**, a unique Username/Display Name, Role and Temporary Password of at least 12 characters → **Save**. Do not create a second account for an existing Reader.

**If a Platform Owner created the Group:** their account did not become its Group Owner member. Establish Alex's normal membership/Owner role explicitly through the supported membership administration instead of expecting Morgan to appear as a Reader/Host candidate.

## 3. Assign staff

### Group Owner and Moderator

Alex is Owner because the normal account created the Group. To appoint Sam:

1. Group → **Members → Permissions** on Sam's row.
2. Set **Persistent Group Role:** Moderator.
3. Leave **Delegated Group Permissions** at **Use Role Default** for this example.
4. **Save Group Permissions**.

**Expected:** Sam can manage Group announcements, but does not become a Host, reviewer, member manager or Challenge creator. If Sam needs one of those Group tasks, deliberately Allow that capability. Do not grant Platform Owner just to help with books.

To share Group ownership, an authorized person can set another member to Owner through Permissions. Alex cannot change their own role here; an ownership handoff needs another authorized account.

### Challenge Host and Team Leaders

The Host is chosen during Challenge creation below; additional Hosts can be added later under **Challenge Settings → Hosts → Assign Host** by Group Challenge-management authority. Alex should be selected, even though already Group Owner.

Riley/Jordan cannot yet be appointed Team Leaders: the Challenge, teams and their participation do not exist. First create/configure the event, let them register or add them as participants, place them, then use **Manage Team Leaders**. This ordering prevents an important first-time mistake.

Taylor can later be appointed under **Challenge Settings → Floaters → Assign Floater** if not an active Reader/team member. A Floater reviews across teams but does not run setup or compete.

## 4. Create the Challenge

**Act as Alex. Required:** Group Challenge-management capability (Owner default).

1. Group → **Create Challenge**.
2. **Challenge Title:** November Reading Challenge.
3. In **Host**, choose Alex → **Add Host**. Check **Selected Hosts**.
4. **Save**.

**Expected:** Draft, registration Closed, dates unset, Alex appointed Host but not Reader. Ordinary members cannot yet see the Draft.

### General and announcement

Open **Challenge Settings → General**. Keep the title and enter a Description explaining monthly reading, team scoring, manual review, submission cutoff and who to contact → **Save General Settings**.

Open **Challenge Announcement**. Enter “Register by October 31. Submit completed November books while the Challenge is Active. Contact Alex about mistakes.” → **Save Challenge Announcement**. This saves a custom Challenge message. Blank here means no Challenge announcement; there is no visible inherit-mode selector in this editor.

### Schedule: every field

Open **Challenge Schedule**. All times below are Group-local; these are hypothetical future configuration values, not dates applied by this documentation task.

| Field | Training value | Reason |
| --- | --- | --- |
| **Registration Opens** | October 30, 2026, 09:00 | Gives members a pre-start sign-up period. |
| **Open registration automatically** | Checked | Also publishes Draft to Upcoming when due. Finish all setup before the opening time. |
| **Registration Closes** | October 31, 2026, 23:59 | Closes self-registration separately from reading. |
| **Close registration automatically** | Checked | Prevents new self-registration after the chosen time. |
| **Challenge Starts** | November 1, 2026, 00:00 | Begins the intended monthly event. |
| **Move Challenge to Active automatically** | Checked | Upcoming → Active when due. |
| **Challenge Ends** | December 1, 2026, 00:00 | Operational cutoff immediately after November; see date-range caveat below. |
| **Move Challenge to Finalizing automatically** | Checked | Stops new submissions at cutoff while review continues. |
| **Final Announcement** | December 3, 2026, 18:00 | Staff's planned announcement time. Does not post to Discord. |
| **Mark Challenge Completed at this time** | Unchecked | Alex will complete only after checking review. |

Select **Save Schedule**. The first four timestamps are required on initial configuration. Check opening ≤ closing and start ≤ end. Entering a timestamp alone does not enable an unchecked automatic action.

**Monthly date-range caveat:** Northbound derives inclusive reading dates from schedule timestamps. An end at December 1 midnight means the completion-date range includes December 1, while the scheduled lifecycle action stops submission at that instant if the scheduler runs promptly. If the community needs the data validation range strictly November 1–30, use November 30, 23:59 instead and explicitly announce that slightly earlier cutoff. There is no separate editable grace-period or inclusive-date control in the current form. Check the deployed date/time display before announcing a deadline; do not promise a minute-exact scheduler response or backdated late submissions after Finalizing.

For a later practice deployment, shift the entire scenario's dates together. These historical example values are not suitable for creating a fresh live Challenge after the example month; overdue enabled actions can catch up over successive scheduler passes.

### Other settings encountered

Before any enrollment:

- **Signup Questions:** configure in section 5.
- **Progress Checkpoints:** add one at November 15, 12:00, **Threshold Percentage 50**, **Progress Basis Base Pages**, **Target Basis Fixed Target**, **Fixed Target Pages 600** → **Save Checkpoints**. No scores are affected by adding it.
- **Competition Visibility:** Reader Scores Everybody; Team Standings Everybody → **Save Visibility**. This shares competition information with Group members, not public visitors or registration answers.
- **Games:** leave **Enable Games** unchecked; **Save Games Setting** if changed.
- **BOTM Settings:** leave Enable Book of the Month unchecked and Full BOTM Completion Bonus Pages 0; **Save Settings** if changed.
- **Personal TBR** under Signup Questions: disabled, both bonuses 0; **Save Personal TBR Settings** if changed.
- **Themes:** none required. For the optional claim exercise below, add a visible active Theme after the schedule is saved: Name “New-to-you author”, Description explaining eligibility, Starts On November 1, Ends On November 30, Theme Bonus (%) 25, Prompt “Why does this book fit?”, Allow Stacking checked → **Save**.
- **Hosts/Floaters/Teams:** staffing/structure as described in sections 3 and 6.
- **Challenge Lifecycle:** leave Draft while preparing. If publishing manually later, Move Forward to Upcoming does not open registration by itself.

## 5. Configure registration

**Act as Alex, Host/Group Challenge manager. Do this before adding anyone to the Challenge.** Staff-created participation also locks the question schema.

1. **Challenge Settings → Signup Questions → Add Question**.
2. **Question:** “What would help you enjoy this challenge?” **Type:** Short Text. **Required:** unchecked.
3. Add a second: “Preferred reading pace?” **Type:** Single Choice. **Choices:** one per line: Relaxed, Moderate, Ambitious. **Required:** checked.
4. Use **Move Up**, **Move Down**, **Remove Question** as needed.
5. Set **Reader Answer Editing:** Editing allowed for a set period; **Editing Duration (Hours):** 24.
6. **Save Registration Settings**.

**Expected:** Readers must answer the pace question. They may edit answers for 24 hours from their original enrollment; the TBR lock is separate. No staff registration-approval queue is involved. Short Text, Number, Single Choice and Multiple Choice are supported; maximum ten questions and 2–20 unique choices for each choice question.

Check the schedule's opening/closing times and automatic boxes again. On opening, verify the Challenge displays **Upcoming** and registration **Open** before sharing the registration link. If it remains Draft/Closed, inspect schedule and background processing rather than inventing an Open Registration button.

## 6. Create teams

**Act as Alex, Host.** Creating teams can happen while Draft, before registration. Appoint leaders only after enrollment/placement.

1. Challenge → **Teams → Add Team**. Name **Cedar**, choose a color → **Save**.
2. Repeat for **Birch**, with a different color.
3. After Riley and Jordan register (section 7), open **Challenge Participants → Edit Team** beside Riley → Cedar → **Save**; place Jordan on Birch in the same way.
4. Teams → Cedar → **Manage Team Leaders** → select Riley → **Assign Team Leader**.
5. Teams → Birch → Manage Team Leaders → select Jordan → Assign Team Leader.
6. Check both **Current Roster** views. Leaders appear before other Readers.

**Expected:** each leader reviews only their current team's books/claims, not permanent Group membership or all Challenge settings. They remain competing Readers. Moving Riley later to Birch ends Cedar leadership; appoint leadership on Birch separately if intended.

To change team name/color use **Edit Team → Save Team**. Use Archive Team / View Archive / Restore Team for preserved history, not Delete Team after it has been used. Do not call an unassigned Reader a Floater.

## 7. Register Readers

### Reader view: Casey

1. Normal Sign In → Home → Northbound Reading Circle → November Reading Challenge.
2. Check Upcoming/registration Open → **Register**.
3. Answer the required pace question and any optional response; provide optional Discord username if requested.
4. **Review Registration**, inspect **Registration Summary**, then **Confirm Registration**.
5. Note that registration is complete but team may be unassigned.
6. If a response was wrong, use **Edit Registration → Save Registration** within the configured 24 hours.

**Expected:** active participation without separate staff approval. No team is required to submit once Active. Since TBR is off, no planned-book list is required. Questions lock for staff after the first enrollment.

Have Riley and Jordan follow the same process before Team Leader appointment.

### Host view: Alex

Open **Challenge Participants**. Find Casey, Riley and Jordan. Inspect Registration, Team, Discord Username and planning columns. **Avg Pages** and **Last Challenge** may show N/A because the Group has no eligible Completed history; this is not a zero-reading forecast.

Use **Edit Team** to place Casey on Cedar and confirm Riley/Jordan's placement. Select a Reader for Participant Detail and registration details. These responses are private planning data, not a public team announcement.

For an eligible late administrative addition while configurable, **Add Participant**, select the Group member and optional team → Save. It creates participation without answering questions or confirming a TBR for that person. If someone withdraws, history remains; if staff removes them, only staff can Reactivate. Reactivation does not restore old team/leadership.

Sam's Moderator title alone does not grant the Host roster controls. Riley's leadership does not grant access to Casey's private registration answers just because both are on Cedar.

## 8. Start the Challenge

At November 1, 00:00, the scheduled start moves **Upcoming → Active**, provided the scheduler is running and the automatic-start box is checked. Alex confirms the displayed state and announcements. The registration gate remains governed by its own close setting.

Manual alternative: **Challenge Settings → Challenge Lifecycle → Move Forward to Active**, complete the displayed action from Upcoming. Lifecycle only moves adjacent stages; Draft cannot jump directly to Active. Check schedule automation when using manual changes.

**Reader actions:** actively enrolled Readers now see Submit Book; they can read Themes, and enabled activities, and see competition reporting under the selected visibility. Future book completions are recorded through completed-book submissions, not daily page increments.

**Host responsibilities:** watch Review Queue/Needs Attention, answer mistakes promptly, verify teams and use consistent manual page evidence. A Host who wants to compete must register separately; the appointment alone did not add Alex to the roster.

## 9. Submit and review books

### Exercise A: successful catalog submission

This requires a separately configured working Group catalog key. Without it, describe the steps and perform only the manual exercise on a later authorized test installation.

As Casey, **Submit Book → Find on Hardcover → Title, Author, or ISBN → Search**, choose **View Editions** on the correct real catalog result, then **Use Edition**, and check Book Details. Enter an actual in-range Completed On date and optional start/notes. Submit Book.

**Expected:** usable selected/scoring metadata gives automatic Approved pages. Audio may use a print/ebook scoring equivalent. If no usable page-bearing edition exists, follow the manual path instead. The hypothetical 300-page score below is an illustration, not a promise of what a real catalog result returns.

If Casey claims New-to-you author, answer its prompt. Catalog approval does **not** approve that Theme claim. As Alex or Riley (Cedar leader), Review Queue → open entry → inspect claim → set appropriate claim decision → **Save Review**. Catalog approved pages remain locked.

### Exercise B: manual book

As Casey, Submit Book → **Clear and Enter Manually**:

| Field | Hypothetical value |
| --- | --- |
| Title | The Lantern Road |
| Author | Avery Example |
| Book Format | Paperback |
| Started On | November 2, 2026 |
| Completed On | November 8, 2026 |
| Submitted Pages | 300 |
| Reference Link | Leave blank in this paper rehearsal; in real use, optionally provide valid exact-edition evidence. |
| Notes | “Manual edition; please verify pages.” |
| Theme Claims | New-to-you author, if genuinely eligible; answer “Why does this book fit?” |

**Submit Book** produces Pending review, contributing no approved base pages yet.

As Alex (or Cedar leader Riley), open **Review Queue**, inspect Submitted Book/Reader Notes. In Decision enter **Approved Pages 300**, Status Approved, optional Verification Link and clear Review Notes. Approve the valid Theme claim separately → **Save Review**.

**Expected:** 300 Base, 75 Modifier, 375 Total for this example. Without claim approval, the book can count as 300 while the bonus remains pending. Do not enter 375 into Approved Pages.

The 25% Theme rate locks once the first claim exists. Its 300 × 25% award is
75 pages. If authorized review intentionally corrects Approved Pages to 400,
the award becomes 100 and Total becomes 500; legacy fixed-page rewards would
remain fixed. Repeated review and catalog updates do not recalculate it.

### Exercise C: rejection or correction

Imagine Casey manually submitted a book that does not qualify for the event.

- Reviewer opens it, chooses **Rejected**, explains in **Review Notes**, then **Save Review**. It contributes no approved base and claims are rejected.
- If the manual review decision itself was mistaken, reopen the existing entry from Participant Detail → Submissions, change the decision/approved pages while Active/Finalizing, and Save Review.
- If the **title/date/book entry** was wrong, Reader reports it to Alex. There is no normal Reader edit or Host metadata-edit control on review. While Active, Alex can use submission **Remove**, inspect confirmation/reason, and arrange any appropriate replacement without leaving a duplicate counting.
- Wrong catalog-verified book: do not promise a normal review override of its locked pages/status. Use supported removal or Platform Owner recovery as appropriate.
- In Finalizing, normal review remains available but Host soft-removal does not. Escalate justified exceptional corrections rather than pretending the Remove button should still exist.

Do not deliberately enter a duplicate to test an assumed duplicate blocker. Normal submissions have no universal uniqueness constraint; staff judgment distinguishes rereads and accidents. BOTM/TBR matching has separate anti-duplicate qualification rules.

## 10. Track progress

### Reader check

Casey opens the Challenge and their available Participant Detail, then My Stats. Confirm book status, Base/bonus/Final and approved book count. My Stats' summary Verified Pages can include modifiers, so its total need not equal the sum of base-only book rows.

### Host/team check

Alex opens Teams → Cedar. Check Current Roster, Base/Modifier/Total, **From**, **To**, **Show Bonuses** and available Reader filters. Choose **Reset Filters** before comparing with Team Comparison.

If Casey has 300 Base +75 Theme and Riley has 250 Base with no modifiers, Cedar's current-roster unfiltered Total is 625 (550 Base +75 Modifier). With Show Bonuses off, the displayed base-only figure is 550. If Casey moves to Birch, the team totals change with current placement; past team assignment history remains. This is not a permanent per-book team allocation.

### Checkpoint check

On November 15 at 12:00, the configured 50% of 600 Base target requires 300 approved base pages. Casey's 300 Base meets it. A Reader with 250 Base +100 bonus is below on **Base Pages**, despite 350 Total.

Alex looks for below-target warnings in participant planning / Needs Attention. It is a staff follow-up aid, not an automatic removal or penalty. Results capture processing-time totals and do not change when another book is approved later. A pending checkpoint first processed after Active is skipped.

Do not expect a Reader checkpoint dashboard or an independently edited personal goal. Explain the community target in its announcement.

## 11. Complete the Challenge

1. **Registration closure:** it happened October 31 under its own schedule. Confirm it; closed registration did not stop November submissions.
2. **Before submission cutoff:** tell Readers to submit before the announced Challenge Ends. Finalizing has no new-book submission grace period or normal late-entry extension field.
3. **At end:** automatic ending moves Active → Finalizing. Verify state rather than assuming the calendar alone changed it.
4. **Final review:** Hosts/Floaters/team-scoped leaders finish pending manual books and Theme claims. Check BOTM/TBR matches if enabled. Verify score filters and reward history; Games award/void operations have their own Active/Finalizing rules.
5. **Completion:** Alex opens Challenge Settings → Challenge Lifecycle → **Move Forward to Completed** and completes the action after review/results are ready. We left automatic completion off so an unfinished queue would not be silently passed over.
6. **Announcement:** staff publish the agreed results to Discord themselves. Final Announcement is a schedule value, not an implemented Discord-posting bot.
7. **Archive later:** after considering history/planning consequences, **Move Forward to Archived**. It retains records but has no normal backward lifecycle action. Current historical-average calculations select Completed, so archiving can change later baselines.

Completed stops ordinary review. If a correction needs reopening, Completed → Finalizing requires the explicit reversal/recovery confirmation, including **Confirm Recovery to Finalizing**. Coordinate automation so it does not immediately complete again. Do not describe an imaginary Closed lifecycle state: use registration Closed and Challenge Finalizing correctly.

## 12. Additional formats

For a later authorized hands-on practice, use a fresh prepared Challenge before enrollment so the main exercise's locked registration does not become an obstacle. These are feature configurations, not separate format selections.

### BOTM exercise

**Act as Host.**

1. Challenge Settings → BOTM Settings: Enable Book of the Month; Full BOTM Completion Bonus Pages **100** → **Save Settings**.
2. Books → Add Book: prepare two books in Positions 1 and 2, each with Title/Author/positive Page Count and Per-book Bonus Pages **25** → **Save Book**. Use Find on Hardcover → Find Book if shared catalog is configured; otherwise manual entry.
3. Readers open Book of the Month; submit either selected book normally during Active.
4. Approve manual pages; inspect **Match History** and Confirm/Reject uncertain candidate matches using actual evidence. Synchronize means local match reconciliation, not external library export.
5. First qualifying completion earns 25 bonus pages. Both qualifying books earn 50 per-book +100 full-set =150 modifier pages, in addition to approved base.
6. Check Completion Awards/current qualification and Reader progress. Retiring a book preserves history; editing later does not simply rewrite frozen award amounts/sets.

**Teach:** all-books bonus follows the configured active set (1–9), not a mandatory nine-book list. Readers do not use a separate BOTM submission form. A book can have approved pages before its uncertain match is resolved.

### Personal TBR exercise

**Act as Host/Group Challenge manager before readers confirm registration.**

1. Signup Questions → Personal TBR: Enable Personal TBR; Per-book TBR Bonus Pages **25**; Full 9-Book TBR Completion Bonus Pages **200** → Save Personal TBR Settings.
2. As Reader, Register → Personal TBR → Enter manually, supply Title/Author; optional Page Count and HTTP/HTTPS Cover URL. Check a cover preview and a blank-cover placeholder. Search results also show covers; selecting an edition retains its artwork when saved. Add and organize four planned books, then Review Registration.
3. Read the warning: a four-book list can earn per-book rewards but never the full nine-book bonus. Back to Edit if nine books were intended; otherwise Confirm Registration knowingly locks four.
4. Read/submit a listed book during Active. Staff approve manual reading and inspect **Review Queue → Personal TBR Matches** if necessary. Confirm only supported identities.
5. One qualifying book earns 25 modifier pages. All four earn 100; the 200 full-nine reward still does not apply.
6. In a separate hypothetical nine-book registration, all nine qualifying completions earn 225 per-book +200 full-list =425 modifiers. Eight of nine does not earn the 200.
7. Demonstrate that Edit Registration only changes answers within policy, not the locked list. An empty confirmed list also stays locked. Authorized staff can instead use **Manage TBR** for protected corrections; books with match or award history must keep their selection and position.

8. As Group Owner/Moderator, Host or Floater, open Challenge Participants → **Manage TBR**. A Team Leader may do this only for Readers currently on their own team. Try **Save TBR** for an incomplete list, then **Save & Complete TBR** with nine valid selections. Neither action completes registration or bypasses required answers. Check the last-updated attribution and a blocked change to a book with match/award history.

**Search distinction:** Reader self-service TBR uses the Reader's personal Hardcover connection; submissions/BOTM use the Group key. Staff **Manage TBR** search prefers the acting staff member's connection, then the Group catalog connection, then manual entry. Staff never use the Reader's private credentials. TBR changes never queue reading sync or send activity to staff or Group Hardcover accounts. The Reader's own consented forward-only sync remains separate.

### Optional Games and personal sync follow-ups

To explain Games, Host enables Games, adds an activity, then Active/Finalizing → Apply Reward → Amount 20 → Apply To Team Cedar → Reason / Note → review recipients → Confirm and Apply Reward. Every eligible listed recipient gets 20; the award is not a pool. Void Reward removes its effect with retained history. Never perform this on live data merely for training.

For personal Hardcover, rehearse My Account → Connect with Hardcover **with consent OFF**. Connection alone should not export reading. A later explicitly authorized provider test enables consent before one new eligible approval; verifies its intended Read/date result; and checks Sync Now does not backfill old books. Live OAuth/provider writes were not tested during this documentation task.

## 13. Practice checklist

Before introducing Northbound to Discord, be comfortable explaining or, in an authorized test deployment, demonstrating:

- [ ] Separate Platform Owner and normal account sign-ins; no default owner password.
- [ ] Create/configure a Group, confirm timezone, privately distribute/regenerate code.
- [ ] Change another member's Group role and explain delegation/defaults.
- [ ] Explain why Owner/Moderator does not automatically mean Host/reviewer.
- [ ] Create Draft Challenge and explicitly select at least one normal Host.
- [ ] Set every schedule timestamp/checkbox and distinguish date validation from scheduler cutoff.
- [ ] Configure questions and editing policy before any enrollment locks them.
- [ ] Explain registration Open/Closed separately from lifecycle.
- [ ] Create teams, register/place Readers, appoint Team Leaders in that order.
- [ ] Explain Floaters and unassigned competing Readers correctly.
- [ ] Read registration answers only with appropriate authority.
- [ ] Show manual submission, pending review, approval/rejection and separate Theme decisions.
- [ ] Explain catalog auto-approval, audio page equivalents and locked pages.
- [ ] Handle mistakes without promising a Reader edit button or creating duplicates.
- [ ] Compare Base/Modifier/Total and filtered/unfiltered current-team totals.
- [ ] Explain N/A historical averages and fixed-target checkpoint snapshots.
- [ ] Configure BOTM set rewards and resolve supported matches.
- [ ] Explain TBR confirmation lock and exact-nine full-list requirement.
- [ ] Explain Games recipients, per-recipient amount and void history.
- [ ] Explain optional Hardcover, Group/personal separation, explicit consent and no backfill.
- [ ] Finish review in Finalizing, then Complete and consider archive consequences.
- [ ] Know when to ask a Platform Owner for recovery rather than change unrelated settings.
- [ ] Complete deployment/browser/scheduler/provider/backup acceptance checks from the Administrator Guide before launch.

The scenario is source-verified training material. Actual dynamic controls, account-role visibility, deployed timing, live provider authorization/sync and restore behavior remain manual acceptance work on an appropriately isolated installation.
