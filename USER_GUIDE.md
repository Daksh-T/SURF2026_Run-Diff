# Run·Diff user guide

This guide covers everything needed to install, configure, and use Run·Diff as a student or
instructor. You do not need programming experience for the desktop-app path.

Developers and administrators can find every environment variable, model name, storage path, and
build option in [CONFIGURATION.md](CONFIGURATION.md). The [README](README.md) explains the system's
architecture and repository layout.

## 1. Choose how to run Run·Diff

### Install a desktop release

This is the normal option for students and instructors. Obtain the installer from your instructor
or the project's Releases page:

- **macOS:** install the `.dmg` build. It uses the system WKWebView.
- **Windows:** install the `.msi` or setup `.exe`. It uses WebView2.
- **Linux:** make the `.AppImage` executable and run it. It uses WebKitGTK.

The builds are currently unsigned. Your operating system may display a first-launch warning. Use
only an installer obtained from a source you trust. On macOS, right-click the app and choose
**Open** if Gatekeeper blocks a normal double-click. On Windows, review the SmartScreen details
before choosing to run it.

Each desktop app starts its own local backend and opens a single Run·Diff window. Keep the app open
while working or while hosting a live class.

### Run from source

Use this path for development or a custom server. Install [uv](https://docs.astral.sh/uv/) and
[Bun](https://bun.sh/), then open two terminals at the repository root.

Terminal 1:

```bash
cd webapp/backend
uv sync
uv run uvicorn app:app --host 127.0.0.1 --port 8077
```

Terminal 2:

```bash
cd webapp/frontend
bun install
bun run dev
```

Open `http://127.0.0.1:5180`. Instructor authoring also needs a Groq key by default; see
[Set up instructor authoring](#set-up-instructor-authoring).

## 2. First-run hint setup

Run·Diff can grade all work without a language model. For model-written hints, it uses Ollama on
the same computer by default.

If the app shows **Live hints need the local model**, select **Set up** and follow three steps:

1. Install [Ollama](https://ollama.com/).
2. Start the Ollama app, or run `ollama serve` in a terminal.
3. Download the model offered by Run·Diff. The default is `qwen2.5-coder:7b`, shown in the app as
   an approximately 4.7 GB download.

You can continue without it. Grading, result tables, classroom logging, and deterministic hint
evidence still work; model-written rungs fall back to built-in wording. Return to `/setup` at any
time to check the model.

The default hint model runs locally. Instructor authoring is separate and uses a cloud model by
default.

## 3. Student guide

### Join a classroom

The Practice page starts with **Join your class**. Which fields you use depends on what your
instructor gave you.

#### Join a class already installed on this device

1. Enter the shared three-word **Class code**.
2. Enter your name.
3. Leave **Class server address** blank.
4. Select **Join**.

This is common after loading an assignment file. In a roster class, spelling is matched without
regard to capitalization and then normalized to the instructor's roster spelling.

#### Connect to an instructor over the network

1. Connect to the same Wi-Fi or LAN as the instructor.
2. Enter the class code and your name.
3. Enter the complete class-server address, including `http://` and port `8077`, for example
   `http://192.168.1.5:8077`.
4. Select **Connect**.

The app downloads the assignment to your device, joins the classroom, and configures attempt
sync. If the connection fails, check the address, network, firewall, and whether the instructor
has **Host on this network** turned on.

#### Join with a personal passcode

For a passcode classroom, enter your personal three-word passcode in the **Class code** field and
leave your name blank. Run·Diff identifies you from the code. A shared class code cannot be used to
enter a passcode classroom.

#### Load an assignment file

1. Enter your name unless you were given a personal passcode.
2. Under **…or load an assignment file**, choose the instructor's `.json` file.
3. If prompted to join afterward, use the class code or personal passcode provided by the
   instructor.

The file installs the classroom and every assigned set locally. If it contains an instructor URL,
attempts can also sync when that address is reachable. The file remains usable offline.

### Work through an assignment

After joining, the left side lists the problems. If the classroom contains multiple sets, use the
set selector above the list.

For each problem:

1. Read the prompt, difficulty, and table definitions. Some statement problems intentionally
   begin with an empty database.
2. Write SQL in the editor.
3. Select **Run & check**, or press Command–Enter where supported.
4. Review the verdict and the number of generated databases passed.

Run·Diff evaluates the submission on multiple database variants. Passing one visible-looking case
is not enough; a correct answer must pass every grading database.

For normal SELECT problems, the app may report that the query failed, that the rows are correct but
ordered incorrectly, or that an instructor-required result header is wrong. It shows your own
result, never the expected SQL.

For statement problems—such as `CREATE`, `INSERT`, `UPDATE`, `DELETE`, or `DROP`—the app compares
the database state after the statement. It can check rows, table structure, required constraints,
generated columns, views, explicit indexes, and triggers. Equivalent type spelling and irrelevant
column-order differences are normalized where appropriate.

### Use the adaptive hint ladder

After an incorrect run, choose **Ask for a hint**. Up to three rungs are revealed in order, and the
order depends on the detected kind of error.

Possible rungs are:

- **Result evidence:** a deterministic view of how your output differs. For membership errors this
  can show missing and extra rows; for ordering errors it explains that the row order differs.
- **A question to consider:** a Socratic question that directs your attention without naming the
  fix.
- **Concept to revisit:** a short conceptual nudge.
- **Where to make the change:** a more specific prose direction naming the relevant operation or
  clause, without runnable answer SQL.
- **Database error:** the database error produced by a statement that did not run.

The ladder is deliberately not a fixed “concept, clause, rows” sequence. Row-membership and
ordering errors usually begin with deterministic evidence; structural errors usually begin with a
question; SQL errors begin with the database message. State-changing problems use a parallel plan
and do not reveal missing expected rows.

Hints are tied to the current run. Submitting again resets the visible ladder so the evidence and
guidance match the new SQL. Hint requests are recorded in classroom insights.

### Understand classroom controls

An instructor can change the session while you are working:

- **Paused:** submissions and hints are temporarily disabled.
- **Ended:** you may submit only the problem already open; you cannot switch problems or ask for
  more hints.
- **Reopened/Running:** normal work resumes.

A classroom can also be scheduled, closed, archived, or have you removed from its roster. The app
shows the relevant status and preserves the option to export attempts when appropriate. Contact the
instructor if a roster removal is unexpected.

### Send attempts to your instructor

Every grade and hint request is logged locally. Submitted SQL is part of the attempt record.

- **Automatic live sync:** when the assignment has a reachable instructor URL, new events are sent
  in the background.
- **Sync:** sends the full local log. It is safe to repeat because the instructor app ignores
  duplicate event IDs.
- **Export attempts:** downloads a JSON file for email, a shared drive, or other offline delivery.

Use **Export attempts** before leaving a closed/deleted classroom or moving to a new computer. The
**×** in the signed-in bar leaves the classroom on this device; make sure the instructor has your
work first.

## 4. Instructor guide

### Set up instructor authoring

The default authoring model is Groq's `qwen/qwen3.6-27b`. You need internet access and a Groq API
key to infer schemas and generate robust data generators.

For a source checkout, create `.env` in the repository root:

```dotenv
groq_api_key=gsk_...
```

Restart the backend after adding the key. A packaged build must inherit the same environment
variable from the process that starts it, or be used with a separately configured backend.

This key is not needed for student grading, published-set practice, classroom management, or local
hints. Advanced local/cloud model changes are in
[CONFIGURATION.md](CONFIGURATION.md#model-registry).

### Protect the Author area

Open **Author**. If authoring is open, use **Set password** in the Author toolbar. Once set, the
password protects Sets, Classes, and Insights on this installation.

- **Lock now** forgets the current browser session's unlocked state.
- **Change** requires the current password and sets a new one.
- **Remove** requires the current password and opens authoring again.

The app stores only a SHA-256 digest. There is no password-reset workflow in the UI, so back up the
data directory and keep the password safely. The password protects the app's local author surface;
it is not a substitute for operating-system account security.

### Author one problem

Go to **Author → Sets**, choose **Single problem**, and enter:

- a short title;
- the student-facing prompt;
- the expected/gold SQL;
- easy, medium, or hard difficulty;
- optionally, **Force label match** for a SELECT result (shown as **Require exact column names**
  when editing the saved problem); and
- optionally, difficulty prediction.

Start authoring. Run·Diff then:

1. detects whether the SQL returns rows or changes database state;
2. infers or validates the starting schema;
3. identifies the SQL concepts being tested;
4. generates a seeded data-population function;
5. validates initial datasets and stress-tests 60 more seeds; and
6. shows the schema, robustness result, generated tables, and expected preview.

For SELECT questions, **Worth confirming** may ask about edge cases such as ties, empty groups, or
boundary values. Mark **Yes** only when that case should be guaranteed in the practice data, then
choose **Re-author with confirmations**. A No answer records no new guarantee.

When the preview is satisfactory, add the problem to a set. Authoring a problem does not publish
it to students.

#### Statement problems

Write the expected `CREATE`, `INSERT`, `UPDATE`, `DELETE`, or `DROP` statement as the gold SQL. The
app detects state mode automatically. Instead of result rows, its preview shows the database after
the expected statement. Deterministic coverage gates make sure, for example, that a conditional
update affects some rows and leaves others unchanged.

#### Difficulty prediction

The optional predictor runs a small simulated student against the problem, first unaided and then
with the tutor. It records solved-unaided rate and average maximum hint level. This can take several
extra minutes and requires the fixed local Ollama models used by the predictor. The prediction is
private and later appears beside actual classroom performance in Insights.

### Author a whole assignment

Choose **Whole assignment** when several questions share schemas.

1. Give the assignment a title.
2. Add one or more sections. Each section has a table/schema description.
3. Add questions within each section, each with its own title, prompt, gold SQL, difficulty, and
   optional exact-column-name requirement.
4. Choose **Author the assignment**.

The app infers one shared schema per section and authors each question against it. Expand each
result to review and add it to the set. If the inferred schema needs correction, select **Edit
schema** and **Re-author section with this schema**. Individual edge-case confirmations can also be
re-authored without discarding the other questions.

### Manage and publish sets

The Sets rail lists private source sets. Within a set you can:

- rename the set without changing its stable ID;
- add more problems;
- edit a problem's title, prompt, difficulty, schema, and exact-column-name rule;
- reorder or remove problems;
- export/import an instructor-private set JSON; and
- delete the set when no classroom still assigns it.

Editing a schema rebuilds the practice data against the new schema. Editing a published set marks
it **edited since last publish**. Students do not receive those changes until you select
**Re-publish set**. A pure set-title rename is synchronized without requiring a republish.

Publishing creates a student-safe bundle. It runs the expected SQL over the grading seeds, stores
the expected outcomes, and excludes the gold SQL. Publish before creating a classroom. Re-publish
after changing any student-visible problem behavior.

Set export and assignment export are different:

- **Export JSON** on a set is an instructor backup and includes gold SQL and generator source. Do
  not give it to students.
- **Assignment file** on a classroom is the sealed, student-facing package of published bundles.

### Create and configure a classroom

Go to **Author → Classes**. A classroom needs a title and at least one published set. Select the
sets in the order students should receive them, then choose an entry mode:

| Mode | Student credential | Best for |
| --- | --- | --- |
| Open | shared class code + any non-empty name | informal practice |
| Roster | shared class code + a name on the roster | named class groups |
| Passcodes | one personal three-word code; no name entry | individual credentials |

In roster and passcode modes, enter one student name per line. Personal codes appear on the class
card after creation. Give each student only their own code. Adding a roster name later creates a
new personal code; unchanged names retain theirs.

Select **Edit** on a class to change its title, assigned sets, mode, roster, or optional open/close
schedule. Times are entered in the computer's local time and stored as UTC. Before the opening time
the class is scheduled; after the closing time it is closed. Leave both blank for an unscheduled
class.

### Run a live session

Class controls are available on the Classes card and in the live Insights view:

- **Pause** freezes submissions and hints for everyone.
- **Resume** restores normal activity.
- **End test** lets an already-active student submit only their current question, while preventing
  switching and new hints.
- **Reopen** returns an ended test to normal.

These controls are separate from the class schedule and archive status.

- **Archive** disables the join code and new participation but keeps the class and insights. It is
  reversible with **Reactivate**.
- **Delete** removes the active classroom and hides its insights. Its attempt log is archived on
  disk, but restoration is not available in the UI.

The **Students** panel shows participants with attempt/solve counts. Open a name to see submitted
SQL and the full event timeline. Deleting a single attempt or deleting all attempts for a student
is permanent.

### Distribute assignments and collect work

#### File workflow

This is the simplest and most reliable method across different networks.

1. Select **Assignment file** on the classroom card.
2. Give the downloaded JSON file and the class code/personal passcodes to students.
3. Students load the file and work locally.
4. Students select **Export attempts** and return their files.
5. Select **Import attempts** on the matching classroom card.

Repeated imports are safe; duplicates are ignored.

#### Live LAN workflow

1. In the network-sync card, select the correct detected address and **Host on this network**, or
   use **Enter URL manually**.
2. Allow incoming connections if the operating system firewall asks.
3. Give students the displayed URL/QR value plus their code.
4. Keep Run·Diff running while students connect and sync.

Students must normally be on the same LAN. Campus guest networks often block device-to-device
traffic. Port `8077` must be reachable. The address may change after reconnecting to Wi-Fi; share
the updated address when necessary.

Selecting **Turn off** disables remote assignment fetch and attempt ingest. Existing student copies
continue grading locally and can export attempts later. For more detail, see the
[Network sync guide](webapp/network_sync_guide.md).

### Use Insights

Open **Author → Insights** and select a classroom. The overview includes:

- total activity and completion summaries;
- solve rate and hint pressure by problem;
- per-student performance;
- filters/drill-downs by assigned set and problem;
- predicted-versus-actual hint demand where a prediction exists; and
- CSV export for the selected class data.

Open a problem to see hint-level use and per-student outcomes. Open a student's name to inspect
their progression and attempt timeline, including submitted SQL.

The **Live** view shows a student-by-problem status grid, active-now indicators, summary counts,
and recent grade/hint activity. It can scope activity to all time, today, the last hour, or the
current viewing session. The grid refreshes automatically. The recent-activity ticker does not
show SQL, although detailed instructor timelines do.

Imported or delayed sync events appear in the same analytics as live events.

## 5. Back up or move an installation

Close Run·Diff, then copy its entire data directory. It contains authoring sources, published
bundles, classrooms, settings, and attempt logs. The source-checkout default is `webapp/data/`;
desktop locations are listed in [CONFIGURATION.md](CONFIGURATION.md#data-directory-and-backups).

This backup is sensitive:

- private set files contain gold SQL;
- classroom files contain join codes and rosters; and
- attempt logs contain student names and submitted SQL.

For a lightweight transfer, instructor set export preserves authoring material, while classroom
assignment/attempt exports preserve distribution and student work. Those separate exports are not
a complete replacement for a full data-directory backup.

## 6. Troubleshooting

| Problem | What to check |
| --- | --- |
| The desktop window never loads | Another process may be using port `8077`, or the bundled backend failed to start. Close other Run·Diff instances and retry. Developers can request `/api/health` on port `8077`. |
| Practice says there are no sets | Students must join a class whose assigned sets have been published. Authors should publish a non-empty set and assign it to a classroom. |
| Authoring fails immediately | Confirm internet access, restart after setting `groq_api_key`, and verify the selected author model. Student practice does not diagnose authoring credentials. |
| Model-written hints are unavailable | Open Setup; confirm Ollama is installed, running at the configured host, and has the displayed model. Built-in hints remain available. |
| A student cannot join | Check the code, class schedule/archive state, exact roster membership, or whether a personal passcode is required. For network connect, also verify the server address. |
| A student connects but attempts do not arrive | Confirm hosting remains on, the instructor URL is current, the class is active, and the student is still on the roster. Have the student select Sync or export an attempts file. |
| LAN connection is refused | Put both devices on the same non-isolated network, allow the firewall prompt, and verify port `8077`. Use assignment files on guest/campus networks that isolate clients. |
| Published edits are missing | Re-publish the set. Problem edits do not update the published bundle automatically. |
| Exact column names are unexpectedly rejected | The problem has **Require exact column names** enabled. Match aliases/header spelling, or disable it and republish. |
| A state problem looks right but fails | Check the complete final database: row data, constraints, generated expressions/modes, views, indexes, and triggers can all matter. |
| A class was deleted | The active class and its UI insights are gone. The old attempt log may remain as a `.deleted-<timestamp>.jsonl` file in the data directory; recovery is manual and requires a suitable class record/backup. |

When network synchronization is unreliable, the assignment-file plus attempts-file workflow is the
recommended fallback: it keeps grading local and does not depend on a continuous connection.
