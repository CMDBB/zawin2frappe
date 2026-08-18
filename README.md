### Zawin2Frappe

Migrate practice data out of the legacy ZaWin dental practice system into Frappe HR

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app zawin2frappe
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
