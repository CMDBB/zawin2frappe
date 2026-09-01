### Zawin2Frappe

Migrate practice data out of the legacy ZaWin dental practice system into Frappe HR

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app zawin2frappe
```

Requires [`frappe/hrms`](https://github.com/frappe/hrms) and
[`autoshift`](https://github.com/CMDBB/autoshift); bench installs both automatically.
autoshift owns the scheduling fields this import writes (`Employee.custom_fte`,
`Shift Location.custom_branch`) and the discipline split the extraction produces, so
without it those values have nowhere to land. The reverse is not true — autoshift is a
general-purpose optimiser and knows nothing about ZaWin.

This app adds three Custom Fields of its own: `Shift Assignment.custom_zawin_key`,
the import's idempotency key; the same field on `Shift Schedule Assignment`, which
identifies the settled weekly pattern a schedule was built from; and
`Employee.custom_initials`, carrying `BEHANDLER.Initialen` — the short code a practice
actually calls someone by on a paper roster. Anyone with no agenda column has none, so
that field stays editable.

### Tests

`tests/` is a pure-Python suite — no ZaWin, no Frappe, no site, no practice
data. `core/pipeline/binding.py` reads only the collapsed person-level frame and
the profile, which is what makes the whole settled-schedule measure testable
against hand-written schedules.

```bash
uv run pytest tests/
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/zawin2frappe
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.

### License

gpl-3.0

## Configuration

Nothing practice-specific lives in the code. A **profile** supplies the payroll
service numbering, disciplines, branches, shift windows, agenda ids and the
free-text vocabulary staff type into the agenda — all of which differ between
installs.

```bash
cp profiles/example.json profiles/mine.json   # then edit
export ZAWIN_PROFILE=profiles/mine.json
```

Inside a Frappe site, set `zawin_profile` in `site_config.json` instead. ZaWin
credentials come from `zawin_mssql` in the site config, or `MSSQL_*` in the
environment for standalone use.

`profiles/example.json` documents every field and points at the exploration
command that discovers the right value for your database.

### Disciplines

autoshift sizes room coverage per **discipline**, so which one a person belongs
to decides how much capacity the optimiser thinks the practice has. Payroll
rarely says: it files whole pools — every assistant under one service code,
every apprentice under another — because it has no use for the distinction.

Where the service map answers, it wins. Where it does not, the discipline is
read off the free-text agenda label, one presence row at a time:

| evidence | example | comes from |
|---|---|---|
| the label names a discipline | `PRES ORTHO` | `discipline_labels` in the profile |
| the label names a practitioner | `PRES CFO` | `BEHANDLER.Initialen` — no configuration |
| the label names nothing | `PRES` | `default_discipline` in the profile |

A person works in whichever discipline holds most of their rows. The second
row is the one worth knowing about: it needs no setup at all, because whoever
the initials belong to has already been placed by accounting, so their
discipline transfers to the person assisting them. A practice that configures
nothing still gets a usable split, and the build logs the commonest label words
nothing claimed — anything there that names a discipline is one
`discipline_labels` entry away from being counted properly.

Two things sit outside the label. A `role_color_rules` entry marked `primary`
places the people it marks directly, for work that leaves no trace in what
staff type (sterilization columns read a bare `PRES` like every other), and a
`staff_scope` override beats everything.

Departments are emitted for every discipline the profile names, filled or not,
so a Scheduling Role or a Discipline Branch Config always has something to link
to. Keeping a pool out of a clinical discipline is therefore just a matter of
giving it one of its own — set the service's `discipline` and apprentices stop
counting as omnipractice capacity.

### Apprenticeships that have ended

Payroll is quick to file an apprentice and slow to stop: one qualified
assistant here was still filed under `Apprent` six years after finishing, and
so sat in a department of her own instead of counting as the ortho capacity she
is. Length cannot correct that — an apprenticeship runs three years, four
elsewhere, longer again for anyone held back — but school attendance can. An
apprentice keeps going back to school, those days are already classified
`training` by `label_rules`, and when the apprenticeship ends they stop:

| | last school day | still working | verdict |
|---|---|---|---|
| current apprentice | 2027-06-29 | 2027-07-01 | in training |
| finished apprentice | 2025-09-26 | 2026-12-18 | qualified |

Give the apprenticeship service a `graduates_to` naming the service its
apprentices become, and anyone still working `apprenticeship_grace_months`
after their last school day is re-filed under it — designation, discipline and
clinical status all following the new code, so a graduate whose service leaves
the discipline open is placed by the same agenda signal as everyone else in the
pool. Leave `graduates_to` unset and nothing happens: accounting's filing
stands, as it does for any apprentice whose agenda records no school days at
all.

### Schedules the practice does not set

At some practices a group of staff decide their own working week and everyone
else is scheduled around them. autoshift models that as
`Scheduling Role.assignments_binding`: a bound holder keeps exactly the Shift
Assignments already on the books, and the optimiser may not add, move or drop
any of them.

Two separate questions, answered from two separate places.

**Which jobs may bind** is a fact about one practice's power structure, so it is
profile data — `"assignments_binding": true` on a service — and nothing here
infers it. Default false, which is the whole feature off.

**Whose week has actually settled** is a measurement, and it can only ever take
binding away again. A practitioner who has just arrived, or is mid-change, must
not be frozen to a pattern that does not exist yet, so they get
`Employee Scheduling Role.binding_override = "Not Binding"` and are scheduled
normally. Nobody is ever marked binding whom the profile did not.

The measure is the weighted overlap between each week someone worked and their
usual week, over a recency-weighted year — 1.0 is the same days every week.
Weeks they worked nothing are dropped rather than scored, because a fortnight's
holiday would otherwise read exactly like an unstable schedule.

Leave inside a week they did work is handled too, and it matters more than it
sounds. This practice records booked leave and sick days **in the slot the shift
would otherwise have occupied**, which makes them evidence about someone's
pattern rather than against it: a week with Thursday marked as holiday says
nothing about Thursday. Those days are therefore excused from both the vote that
decides the usual week and the comparison against it. The distinction is by
agenda *category*, never by the free-text label — the two booked categories here
land on a weekday the person never works 0.2% and 0.0% of the time, while the
generic "away" category does so 8.3% of the time and can carry over a thousand
full-day rows for one person. That one is a background banner, and excusing it
would blank out most of the calendar. Which categories are which is
`categories.excused` in the profile.

On this practice's data, excusing leave moved holdout agreement in the top score
band from 0.88 to 0.92 (and its floor from 0.60 to 0.75) at unchanged
correlation, and moved two more people over the settled threshold.

A week is not the only cycle a practice runs, and assuming it was got this
badly wrong first time: this practice's orthodontists are on rotas longer than a
week — one works four days, then three, then two weeks off — and a weekly
reading rejected all five as unsettled when theirs are among the most regular
schedules in the building. The commonest case is smaller, and comes straight off
the practice's wall chart, which has a "Friday, odd week / Friday, even week"
column. So the pattern is fitted per phase of a cycle up to
`binding_max_cycle_weeks`, each extra phase priced at `binding_cycle_penalty` so
a longer period has to earn its parameters.

Price that penalty against a **control group** rather than by feel: staff the
practice schedules itself should essentially never read as being on a rota, so
they measure the false-positive rate directly. Sweeping it here, 0.03 is the
lowest value at which none of the thirty-five controls flips, and it finds seven
of the thirty-two self-scheduling staff; by 0.025 the first control goes.

The score distribution is continuous — there is no natural break to read a
threshold off — so `binding_settled_min` is a judgement. Calibrate it:

```bash
zawin binding                  # everyone, ranked, with the largest gaps
zawin binding --eligible-only  # just the services marked binding
```

Each build re-runs the check and writes `data/derived/binding_review.csv`
saying where everyone landed and why.

### Handing a settled week back to HR

Once a week is known to repeat, it stops being several hundred rows and becomes
a *rule* — and stock HR already has somewhere to put a rule. Each bound
practitioner gets a `Shift Schedule` (a shift type, a frequency, the weekdays it
falls on) and a `Shift Schedule Assignment` joining it to them; HRMS's own
nightly job creates the `Shift Assignment` records from there. An administrator
reviews one rule instead of auditing a year of rows.

A `Shift Schedule` names exactly one shift type, so someone working mornings on
Monday and afternoons on Thursday gets two — a fair description of the practice
rather than a workaround. Schedules are named after their own content, so
everyone on the same pattern shares one record.

Everything is emitted **disabled** (`enabled = 0`). Nothing is generated until
somebody approves it, and `create_shifts_after` is set to the build's own
`date_to` so the import owns history and the schedule owns the future. They have
to meet exactly rather than overlap: HRMS throws on an active assignment that
collides with an existing one.

#### Only weekly schedules can actually be run

`Every N Weeks` is **not** an N-week rota. It repeats *the same* weekday set,
working one week in every N — there is no way to say "week one is Monday to
Wednesday, week two is Thursday and Friday".

It is also unsound for N > 1. `create_shifts` takes its week boundary from
`create_shifts_after`, and `create_individual_assignment` overwrites that with
the last *shift's* end date rather than the end of a week. One long call is
correct; the nightly job resumes mid-pattern, the boundary re-anchors, and the
cycle collapses. Measured against hrms 16.8.0 over twelve weeks in thirty-day
chunks, `Every 4 Weeks` produced weeks 0, 4, 4, 5, 8, 9, 10, 11, 12 — weekly by
the third month. `Every Week` is immune: `gap` is 0 and the branch that moves
the boundary never runs.

So a rota is still emitted — it is real, and someone should be able to see it —
but as `Inactive`, `enabled = 0`, tagged **DO NOT ENABLE**, and carrying a
comment saying why. One assignment per phase, anchored a week apart, is the
faithful shape and would work as-is the day `create_shifts` anchors its weeks
properly. Until then those people keep their imported `Shift Assignment` rows,
which describe them correctly. The build names them in its warnings.

### Curated overrides

Some links cannot be inferred — a member of staff recorded under a former
surname, someone whose real job differs from their payroll filing, or a
practitioner whose new working week is final even though the agenda is still a
quarter away from saying so. A profile may point at CSVs of human-confirmed
corrections via its `overrides` block.

**These name identifiable people. Keep them out of public repositories**, along
with the profile itself: service numbering, branch names and agenda layout
together fingerprint a practice.

## Standalone use

`zawin2frappe.core` imports no Frappe, so schema forensics and CSV builds run
against a restored backup with no site and no bench:

```bash
zawin tables --candidates          # domain-matching tables with row counts
zawin columns TAGPLANTERMIN        # column metadata
zawin values BEHANDLER Funktion    # value frequencies — how enums get decoded
zawin fkscan TAGPLANTERMIN         # infer foreign keys (ZaWin declares none)
zawin binding                      # how settled each person's working week is
zawin build all --out data/import  # Frappe Data Import CSVs
```

## Refreshing the database

Every extract is only as current as whichever nightly backup is loaded, so this
is routine rather than setup. `zawin restore` does the whole thing: pick a
backup off the share, unpack it, replace the local database, report how far the
new agenda reaches, and optionally re-run the build.

```bash
zawin restore --list                      # what is on the share, newest first
zawin restore                             # choose one, confirm, restore
zawin restore --latest --yes --then-build # unattended refresh
```

**This one command runs on the host, not in bench**, which is the only place it
can: the backups sit on a Windows drive mapped into WSL, the `.bak` has to land
somewhere the SQL Server *container* can read, and the Frappe container can
reach neither. So the rebuild afterwards is a command it shells out to
(`ZAWIN_BUILD_COMMAND`) rather than a function it calls.

Everything else is worked out rather than configured. The SQL Server container
is identified from `MSSQL_HOST`, or failing that from whichever container
publishes `MSSQL_PORT`; the staging directory is read off that container's bind
mount; and because the configured address is the one the *Frappe* container
uses, the restore falls back to the same container's published port when that
address does not resolve here. `.env.example` lists the overrides for when a
guess is wrong.

Two things worth knowing:

- The share is not in `/etc/fstab`, so after a WSL restart it is simply absent.
  The command says so, with the `mount` line to fix it, rather than reporting
  no backups.
- Unpacking turns 1.3 GiB into 7.8 GiB. The `.bak` is deleted once the restore
  lands unless you pass `--keep-bak`, and disk space is checked before
  unpacking.

The restore replaces the local copy outright and closes any open connection to
it first (`SINGLE_USER WITH ROLLBACK IMMEDIATE`). That is safe here and only
here: this database is a read-only copy of the practice's and nothing writes to
it. It is never the practice's live server.
