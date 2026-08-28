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

This app adds two Custom Fields of its own: `Shift Assignment.custom_zawin_key`, the
import's idempotency key, and `Employee.custom_initials`, carrying `BEHANDLER.Initialen`
— the short code a practice actually calls someone by on a paper roster. Anyone with no
agenda column has none, so the field stays editable.

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

### Curated overrides

Some links cannot be inferred — a member of staff recorded under a former
surname, or someone whose real job differs from their payroll filing. A profile
may point at CSVs of human-confirmed corrections via its `overrides` block.

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
