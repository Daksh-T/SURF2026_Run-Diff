# Run·Diff — user guide

This is for people *using* the tutor — students practicing SQL and instructors authoring
problems. For how the app is built, see `README.md`.

---

## For students

### Get the app

Either:
- **Desktop app** — a single-window app, no terminal. Ask your instructor for the build, or
  see `desktop/README.md` if you're building it yourself.
- **Run from source** — if your instructor hasn't shared a packaged build, follow the dev
  setup in `README.md` ("Run it") and open the printed local URL.

### First run

The first time you open the app, you'll land on a setup screen that checks for **Ollama**,
the program that runs the tutor's hint model on your own machine (nothing about your code
leaves your computer for hints). If Ollama isn't installed, the screen links you to the
download. Once it's running, the screen offers to pull the tutor model
(`qwen2.5-coder:7b`, about **4.7 GB** — a one-time download that can take a while depending on
your connection).

**You don't have to wait for this.** Until the model is downloaded, hints are built-in,
template-based hints instead of model-written ones — grading, the result table, and the
error message all work exactly the same. You can start practicing immediately and let the
download finish in the background.

### Join your class

You'll see a "Join your class" screen with two options:

1. **Join code + name** — your instructor gives you a three-word code (e.g.
   `maple-river-stone`). Enter it with your name and you're in. If your class uses a roster,
   your name is matched against it (case doesn't matter); if it's open, any name works.
   - **Personal passcode** — some classes hand each student their *own* three-word passcode
     instead of a shared class code. If you were given one, enter it in the same code box and
     leave the name field blank — your name is recognized from the passcode itself.
2. **Load an assignment file** — if your instructor sent you a file instead (for offline or
   no-shared-server setups), use "load an assignment file" and pick it. This installs the
   class and its problems locally — the join code in the file then works the same way.

### The practice loop

1. Pick a problem from the list. You'll see the prompt and the table schema.
2. Write your SQL in the editor.
3. Click **Run & check**.
4. Read the verdict:
   - **Correct** — your query matches the expected result on every hidden test database.
   - **Incorrect** — you'll see *your own result rows* (not the answer) and a short
     **error category** chip — one of: didn't run, ordering, wrong columns, extra rows
     (filter too loose?), missing rows (filter too strict?), or values differ (check
     calculations). This is computed directly from your result, with no extra wait.
5. If you're stuck, open the **hint ladder**. Each click reveals the next rung:
   - **L1 — what kind of thing is off** (a conceptual nudge, no SQL).
   - **L2 — the clause or operation to revisit** (still no SQL — points you at *where*,
     not *what*).
   - **L3 — your own diverging rows**, laid out concretely from the grader's diff. This is
     evidence from your own run, not a skeleton query — you still have to work out the fix.

   **Hints reset every time you run.** Each fresh "Run & check" clears the ladder, so you
   always start from L1 on your next attempt — there's no stockpiling hints from a previous
   try.

**Some problems ask you to *change* the database** instead of querying it — create a table,
update some rows, delete or drop something. "Run & check" still works the same way, but it
compares the database your statement *leaves behind* against the expected end state, so
you'll see your own tables after the statement instead of a result set. Column order and how
you spell a type (e.g. `INT` vs `INTEGER`) don't matter — only the structure and data do.

### Getting your work back to your instructor

Your attempts (grades and hint requests) are logged locally as you work. How they reach your
instructor depends on how the class is set up:

- **Automatic** — if your instructor configured a live sync URL for the class, your attempts
  are sent automatically in the background as you work. Nothing to do.
- **Manual sync** — if you see a **Sync** button in the join bar, click it to push your whole
  local log. Safe to click repeatedly — duplicates are ignored on the receiving end.
- **Export attempts** — for fully offline classes, use **Export attempts** to download a
  file, then send it to your instructor (email, shared drive, etc.) for them to import.

---

## For authors

### Unlock authoring

If the author area is locked, enter the password your administrator set. If you're setting up
the app for the first time and no password exists yet, you'll see a banner offering to set
one — do this if you want to keep students out of `/author` and `/insights` on a shared
machine. Without a password, authoring is open but unadvertised.

### Author a problem

You write only two things: the **question** (a plain-English prompt) and the **gold SQL**
(the correct query). The system does the rest:

1. Go to **Author**, fill in the title, prompt, gold SQL, and a difficulty (easy/medium/hard).
2. Click **Author**. The system:
   - Infers the table schema from your SQL.
   - Derives the target clauses (what the query is testing).
   - Builds a seeded data generator and stress-tests it (60 seeds).
   - Surfaces **nudges** — plain-English yes/no questions about edge cases it couldn't infer
     on its own (e.g. "should two rows tie on this sort column?").
3. **Answer the nudges honestly.** These aren't busywork — each "yes" becomes a guaranteed
   case in the generated data (the system writes the verifying SQL itself, using your gold
   query's own column names). A nudge answered "no" when the real answer is "yes" is exactly
   the kind of edge case that lets a subtly-wrong student query slip through ungraded. Click
   **Re-author with confirmations** after answering.
4. Preview the generated data and the problem as a student would see it, then **add it to a
   set**.

### Statement questions (CREATE / INSERT / UPDATE / DELETE / DROP)

If your gold SQL changes the database instead of querying it, the system detects this
automatically from the SQL you wrote — there's nothing to pick. These problems are graded by
**final database state** instead of a result set.

- Instead of nudges, your problem gets **deterministic edge-case gates**: an UPDATE or DELETE
  with a WHERE clause is guaranteed data where it affects some rows and leaves others
  untouched (so an unconditioned statement is caught automatically); an INSERT is guaranteed
  to grow the target table; a CREATE/DROP is checked against the table actually
  appearing/disappearing.
- **Difficulty prediction works the same as for queries** — see "Optional: difficulty
  prediction" below; the simulated student's statement is graded by comparing database state
  instead of result rows.
- See the **`ddl-dml-demo`** set for worked examples spanning CREATE, INSERT, UPDATE, DELETE,
  and DROP.

### Assignment mode (sections)

Instead of one problem at a time, switch to **whole assignment** mode to author several
questions against shared schemas at once:

- Each **section** gets its own table hint (its own inferred schema) and its own list of
  questions.
- Click **+ add question** within a section, or **+ add section** for a new schema entirely.
- Authoring runs per-question against its section's schema, with progress shown as it goes.
- If a nudge needs confirming for one question, re-authoring that question keeps the
  section's shared schema.

### Optional: difficulty prediction

Before publishing, you can check **Predict difficulty**. This runs a simulated weak student
through the problem (cold, then with the tutor's hints) and estimates how hard it actually
is — solved-unaided rate and average hint level needed. This works for both query problems and
statement problems (the simulated student's CREATE/INSERT/UPDATE/DELETE/DROP is graded by
comparing database state, same as a real student's). It takes roughly 1-2 extra minutes per
problem, is stored privately (never shown to students), and shows up later in **Insights** as
"predicted vs actual" once real students have attempted it.

### Publish

Click **Publish** on a set. This *bakes* the gold queries into per-seed result sets and
strips the gold SQL entirely from what gets served to students — from this point on, nothing
a student can reach contains the answer. Editing a published set's problems (title,
difficulty, prompt, or schema) marks it "edited since last publish"; publish again to push the
changes live.

### Create a class

A class wraps **one published set** behind a join code (three random words, e.g.
`maple-river-stone`):

- **Open mode** — any student name is accepted.
- **Roster mode** — student names are matched against a roster you provide
  (case-insensitive, canonicalized to the roster's spelling).

### Distribute

- **Shared server / LAN** — share the join code. Students enter it plus their name.
- **Fully offline** — use **Export assignment file** to download a sealed assignment file
  containing the class record and the gold-free bundle. Send it to students; they use "load an
  assignment file" and the join code works locally. The file is sealed (not human-readable) so
  curious students can't open it in a text editor and read the expected outputs — but for
  high-stakes grading, prefer a live class on your machine (LAN, join by passphrase) over
  exported files.

### Collect insights

- **Live sync** — if students can reach your machine (LAN or hosted), set `instructor_url`
  to your backend's address in `data/config.json` *before* exporting assignment files — it's
  embedded in the file, and student apps then forward attempts to you automatically as they
  work (plus a manual Sync button on their end).
- **Import attempts** — for offline classes, students export an attempts file; use **Import
  attempts** on the class to merge it in (idempotent — importing the same file twice is
  harmless).

### Read the Insights tab

- **Overview** — a summary strip per class, plus two charts: solve rate by problem (the
  worst-performing problem is flagged) and hint pressure by problem (how often each hint
  level gets used).
- **Per-problem drill-down** — click a problem's chip for stat boxes, a breakdown of hint
  levels used, a per-student table, and (if you ran difficulty prediction) **predicted vs
  actual** average hint level, with a checkmark when they're within 0.5 of each other.
- **CSV export** — download the underlying data for any class.

### Watch a live session

Open **Insights → your class → Live** to watch a session as it happens.

- A **student × problem grid** shows each student's status on each problem: solved, hinted, or
  currently trying — with an active-now indicator for students working right now.
- **Session scoping** lets you narrow the view to all time, today, the last hour, or since you
  opened the page — useful for watching just the current class period.
- The grid **polls every 4 seconds**, and an activity ticker shows a live feed of what's
  happening. The ticker never shows student SQL — only that an attempt happened, on which
  problem, by whom.
- Live mode works the same way for attempts that arrive later through sync or an imported
  attempts file — it isn't limited to students connected over LAN right now.

### Manage classes

In the class manager you can:
- **Rename**, change mode (open/roster), or edit the roster — the class id and join code
  never change.
- **Delete a class** — this archives its attempt log (renamed, not removed) and keeps it out
  of the active list. Attempt history is never destroyed by a delete.
- **Delete a whole set** — removes the set and its published bundle. Blocked (with an error)
  if any class still wraps it, so you can't accidentally orphan a class's content.
