"""The employee identity spine.

Up to three sources describe the same people, and typically none is complete:

    accounting export   identity, FTE, service, entry date — authoritative
    payroll export      a short name or initials, plus recent leavers
    ZaWin BEHANDLER     the agenda link (FK_Behandler)

The two HR exports are rarely cut on the same day, so each holds people the
other lacks: recent hires missing from the older one, leavers already dropped
from the newer. Agenda data goes back years, so historical shift assignments
need the **union**, not the current roster.

Where an accounting export carries no short name, matching it straight to
BEHANDLER scores materially worse than the payroll export does, because the
short name is `crosswalk`'s fourth fallback. The spine therefore routes through
payroll to pick that up:

    accounting ──(personnel no.)──► payroll ──(short name ≈ Initialen)──► BEHANDLER

Both HR exports are optional: with neither, the spine degrades to BEHANDLER
alone, which costs the FTE and service columns but still links agenda columns
to people.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from . import crosswalk, settings
from .db import REFERENCE_DIR

log = logging.getLogger(__name__)

#: Human-confirmed identity links the matcher cannot infer (name changes).
#: IDs only, no names — safe to commit. See data/overrides/README.md.
#: Curated identity links live with the profile, not in this package.

#: Accounting export. "Liste collaborateurs", one sheet, one row per employee.
DEFAULT_ACCOUNTING = REFERENCE_DIR / "collaborateurs_2026-08-13.xlsx"

#: Accounting column headers → internal names. The FTE column header is long
#: and prone to drift ("Taux d'occupation actuel entreprise fin de mois"), so
#: it is matched by prefix rather than equality — see load_accounting().
ACCOUNTING_COLUMNS = {
    "N° P": "personnel_no",
    "Nom": "surname",
    "Prénom": "forename",
    "Date de naissance": "date_of_birth",
    "Numéro du service": "service_no",
    "Service": "service",
    "Dernière date d'entrée": "joined_on",
    "Sexe AS Suisse": "sex",
}

#: Service -> discipline, and which services are clinical, both come from the
#: profile: service numbering is the practice's payroll convention, not ZaWin's.


def load_accounting(path: Path | None = None) -> pd.DataFrame:
    """Read the accounting roster: identity, FTE, service, entry date."""
    path = path or DEFAULT_ACCOUNTING
    df = pd.read_excel(path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    fte_cols = [c for c in df.columns if c.lower().startswith("taux d'occupation")]
    if not fte_cols:
        raise ValueError(f"no 'Taux d'occupation' column in {path.name}: {list(df.columns)}")

    df = df.rename(columns={**ACCOUNTING_COLUMNS, fte_cols[0]: "fte_pct"})
    missing = set(ACCOUNTING_COLUMNS.values()) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing expected columns: {sorted(missing)}")

    df["personnel_no"] = df["personnel_no"].astype(str).str.strip()
    df["service_no"] = df["service_no"].astype(str).str.strip()
    df["fte_pct"] = pd.to_numeric(df["fte_pct"], errors="coerce")
    for col in ("date_of_birth", "joined_on"):
        df[col] = pd.to_datetime(df[col], errors="coerce")

    prof = settings.get()
    df["discipline"] = df["service_no"].map(lambda c: prof.service(c).discipline)
    df["is_clinical"] = df["service_no"].isin(prof.clinical_services)

    if df["fte_pct"].isna().any():
        log.warning("%d accounting rows have no FTE", int(df["fte_pct"].isna().sum()))
    return df[
        [
            "personnel_no", "surname", "forename", "date_of_birth", "service_no",
            "service", "joined_on", "sex", "fte_pct", "discipline", "is_clinical",
        ]
    ]


def merge_rosters(
    accounting_path: Path | None = None,
    personnel_path: Path | None = None,
) -> pd.DataFrame:
    """Union of the two rosters, keyed on personnel number.

    `source` records provenance: both / accounting_only (recent hires with no
    payroll row yet) / payroll_only (leavers, and non-person placeholders).
    """
    acc = load_accounting(accounting_path)
    pay = crosswalk.load_personnel(personnel_path)
    pay = pay.copy()
    pay["personnel_no"] = pay["personnel_no"].astype(str).str.strip()

    pay_cols = ["personnel_no", "short_name", "key_short", "service_no"]
    merged = acc.merge(
        pay[pay_cols].rename(columns={"service_no": "payroll_service_no"}),
        on="personnel_no",
        how="outer",
        indicator=True,
    )
    merged["source"] = merged["_merge"].map(
        {"both": "both", "left_only": "accounting_only", "right_only": "payroll_only"}
    )
    merged = merged.drop(columns="_merge")

    # Payroll-only rows carry no accounting identity; recover name from payroll.
    payroll_only = merged["source"] == "payroll_only"
    if payroll_only.any():
        extra = pay.set_index("personnel_no")
        for col, src in (("surname", "surname"), ("forename", "forename")):
            merged.loc[payroll_only, col] = (
                merged.loc[payroll_only, "personnel_no"].map(extra[src])
            )
        merged.loc[payroll_only, "service_no"] = (
            merged.loc[payroll_only, "payroll_service_no"]
        )

    merged = crosswalk_keys(merged)
    log.info(
        "roster union: %d people (%s)",
        len(merged),
        ", ".join(f"{k}={v}" for k, v in merged["source"].value_counts().items()),
    )
    return merged


def crosswalk_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Add the normalised match keys `crosswalk.build()` expects."""
    out = df.copy()
    for col in ("surname", "forename", "short_name"):
        out[col] = out[col].fillna("")
    out["key_surname"] = out["surname"].map(crosswalk.normalise)
    out["key_forename"] = out["forename"].map(crosswalk.normalise)
    out["key_first"] = out["key_forename"].str.split().str[0].fillna("")
    out["key_name"] = (out["key_surname"] + " " + out["key_forename"]).str.strip()
    out["key_core"] = (out["key_surname"] + " " + out["key_first"]).str.strip()
    out["key_short"] = out["short_name"].map(crosswalk.normalise).str.replace(" ", "")
    return out


def build_spine(
    accounting_path: Path | None = None,
    personnel_path: Path | None = None,
) -> pd.DataFrame:
    """One row per person, with the ZaWin `behandler_id` resolved where possible.

    Rows with a null `behandler_id` have no agenda in ZaWin and therefore no
    shifts — expected for very recent hires and for payroll placeholders.
    """
    roster = merge_rosters(accounting_path, personnel_path)
    matches = crosswalk.build(people=roster)

    spine = roster.merge(
        matches[["personnel_no", "behandler_id", "behandler_initials",
                 "behandler_active", "match_method", "confidence"]],
        on="personnel_no",
        how="left",
    )
    unresolved = spine["behandler_id"].isna().sum()
    log.info("spine: %d people, %d without a BEHANDLER match", len(spine), unresolved)
    return spine


# ---------------------------------------------------------------------------
# BEHANDLER columns → people
# ---------------------------------------------------------------------------
#
# FK_Behandler is an agenda *column*, not a person. One person can hold several
# columns, and there are exactly three reasons for it — each verified against
# 2024 data and each meaning something different downstream:
#
#   ortho fan-out   CFO / A-CFO / CFO2, JA / JA2 / JA3, VV / A-VV, CGU / A-CGU
#       Orthodontists supervise up to three chairs at once (ortho assistants are
#       qualified enough to work semi-independently), so they get one column per
#       chair. Verified: CFO and A-CFO hold 3,335 *simultaneous* slots in 2024,
#       both with their own patient bookings — not one person's duplicate rows.
#       Matches autoshift's `max_rooms_for_employee_type` = 3 for Orthodontics.
#
#   branch split    MDP / MDPB, PR / PRB, SG / SGB  ("B" = Blandonnet)
#       One column per site for staff who work both. Verified against
#       FK_BehOrt: PR is 99.8% BehOrt 12 (Balexert), PRB 98.5% BehOrt 13
#       (Blandonnet).
#
#   discipline split  PN / PNPRO, DVE / DVPRO  ("PRO" = Prophylaxie)
#       Verified via TAGPLANBEHANDLER: DVE sits in the Assistantes agenda,
#       DVPRO in Prophylaxie / Hygiénistes / Zone HD.
#
# All three collapse to one Employee. The distinction is kept because branch
# drives `shift_location` and discipline drives `department`.

COLUMN_ORTHO_PARALLEL = "ortho_parallel"
COLUMN_BRANCH_BLANDONNET = "branch_blandonnet"
COLUMN_PROPHYLAXIS = "prophylaxis"
COLUMN_PRIMARY = "primary"

#: BEHANDLER.Funktion for prophylaxis columns. This is the reliable signal:
#: matching initials for a "PRO" suffix caught DVPRO and PNPRO but missed
#: JPLPR, and would miss any future spelling. Verified against all four
#: Funktion=5 columns (SG, DVPRO, PNPRO, JPLPR).
def funktion_prophylaxis() -> int | None:
    """Which BEHANDLER.Funktion means prophylaxis, if any.

    The *technique* is general and worth keeping: Funktion beats matching on
    an initials suffix, which misses any spelling the practice did not adopt.
    Which code it is, is profile data.
    """
    return settings.get().zawin.get("funktion_prophylaxis")

#: BEHORT ids that denote a site rather than a chair.
#: BEHORT ids that denote a site rather than a chair: settings.get().site_behort


def classify_column(
    initials: str, base_initials: str | None = None, funktion: int | None = None
) -> str:
    """Why this agenda column exists, relative to the person's base column.

    The naming scheme is undocumented and irregular; these are the three
    patterns observed across every multi-column person with 2024 activity.
    `base_initials` is the person's undecorated column — the suffix rules are
    only meaningful against it.
    """
    ini = str(initials or "").strip().upper()
    base = str(base_initials).strip().upper() if base_initials else ""
    if not ini or ini == base:
        return COLUMN_PRIMARY
    fp = funktion_prophylaxis()
    if fp is not None and funktion is not None and not pd.isna(funktion) and int(funktion) == fp:
        return COLUMN_PROPHYLAXIS
    if ini.endswith("PRO"):
        return COLUMN_PROPHYLAXIS
    if base and ini == base + "B":
        return COLUMN_BRANCH_BLANDONNET
    if ini.startswith(("A-", "A_")) or (len(ini) > 1 and ini[-1].isdigit()):
        return COLUMN_ORTHO_PARALLEL
    return COLUMN_PRIMARY


def _base_initials(initials: list[str]) -> str:
    """The undecorated column among a person's columns.

    Chosen as the shortest that the others are built from, not the busiest: a
    secondary column can carry several times more rows than the column it
    derives from, so row count is the wrong signal.
    """
    clean = [str(i).strip().upper() for i in initials if str(i).strip()]
    if not clean:
        return ""
    derived = {i for i in clean if i.endswith(("PRO", "PR")) or i.startswith(("A-", "A_"))
               or (len(i) > 1 and i[-1].isdigit())}
    candidates = [i for i in clean if i not in derived] or clean
    return min(candidates, key=len)


def behandler_columns(year: int | None = None) -> pd.DataFrame:
    """Every BEHANDLER row with its agenda activity, for column resolution."""
    from .db import query

    where = ""
    if year:
        where = f"WHERE Datum >= '{year}-01-01' AND Datum < '{year + 1}-01-01'"
    cols = query(
        f"""
        SELECT b.[Zähler] AS behandler_id, b.Name AS name_raw,
               b.Vorname AS vorname_raw, b.Initialen AS initials,
               b.Funktion AS funktion_code, b.Gruppe AS is_group,
               b.Austritts_Datum AS left_on, b.Geburtsdatum AS date_of_birth,
               a.rows, a.patient_rows, a.first_seen, a.last_seen
        FROM BEHANDLER b
        LEFT JOIN (
            SELECT FK_Behandler,
                   COUNT(*) AS rows,
                   SUM(CASE WHEN FK_Patient > 0 THEN 1 ELSE 0 END) AS patient_rows,
                   MIN(Datum) AS first_seen, MAX(Datum) AS last_seen
            FROM TAGPLANTERMIN {where}
            GROUP BY FK_Behandler
        ) a ON a.FK_Behandler = b.[Zähler]
        """
    )
    cols["rows"] = cols["rows"].fillna(0).astype(int)
    cols["patient_rows"] = cols["patient_rows"].fillna(0).astype(int)
    return cols


def fold_umlauts(text: str) -> str:
    """Collapse German umlaut transliterations, so "MUELLER" == "MULLER".

    Swiss records write ü as either "u" (accent dropped) or "ue" (transliterated)
    depending on the system, and the two rosters disagree. `crosswalk.normalise`
    already strips combining accents, which turns "ü" into "u"; this folds the
    "ue" spelling onto the same form. Applied only as a fallback, never as the
    primary key, since it also merges genuinely distinct strings.
    """
    out = text
    for double, single in (("UE", "U"), ("OE", "O"), ("AE", "A")):
        out = out.replace(double, single)
    return out


def load_overrides(path: Path | None = None) -> pd.DataFrame:
    """Human-confirmed personnel_no -> behandler_id links.

    Empty when the active profile ships none, which is the normal case for a
    fresh practice: the automatic matcher handles everything except renames.
    """
    path = path or settings.get().override_path("identity_links")
    if path is None or not Path(path).is_file():
        return pd.DataFrame(columns=["personnel_no", "behandler_id", "reason", "confirmed_on"])
    df = pd.read_csv(path, dtype={"personnel_no": str})
    df["behandler_id"] = pd.to_numeric(df["behandler_id"], errors="coerce").astype("Int64")
    return df.dropna(subset=["behandler_id"])


def person_key(name_raw, vorname_raw, initials) -> str:
    """Normalised person key, tolerating BEHANDLER's two name conventions.

    `Name` sometimes holds the initials with the real name in `Vorname`
    so any token equal to the initials is dropped.
    """
    ini = crosswalk.normalise(initials or "").replace(" ", "")
    tokens = crosswalk.normalise(f"{name_raw or ''} {vorname_raw or ''}").split()
    return " ".join(t for t in tokens if t != ini)


def resolve_columns(spine: pd.DataFrame, year: int | None = None) -> pd.DataFrame:
    """Map every active agenda column to a person in the spine.

    Returns one row per BEHANDLER column with `personnel_no` attached where a
    person could be identified, plus the `column_role` explaining multi-column
    people. Columns with no roster match are kept with a null `personnel_no` —
    they are leavers and historical staff, and their agenda rows still matter
    for reconstruction.

    `year` defaults to None (all available data) and should normally stay that
    way. Restricting it silently drops staff: with year=2024 only 55 people
    resolve, against 77 over the full span, because the 2025-26 intake has no
    2024 activity at all.
    """
    cols = behandler_columns(year)
    cols = cols[cols["rows"] > 0].copy()
    cols["person_key"] = [
        person_key(n, v, i)
        for n, v, i in zip(cols["name_raw"], cols["vorname_raw"], cols["initials"], strict=False)
    ]

    people = spine.copy()
    people["person_key"] = people.apply(
        lambda r: crosswalk.normalise(f"{r['surname']} {r['forename']}"), axis=1
    )

    # Surname-token overlap plus first-forename agreement. This is what recovers
    # the compound/married-name cases where ZaWin keeps only one component:
    # a compound or married surname reduced to one of its components.
    # Surname-token overlap plus agreement on *any* forename token. This is
    # what recovers the compound/married-name cases where ZaWin keeps only one
    # component, and the cases where ZaWin uses a later given name than
    # payroll does (payroll "SURNAME First Second", ZaWin "SURNAME Second").
    # Requiring a surname match keeps it tight: forename alone collides badly,
    # since several unrelated columns share a forename.
    lookup: dict[str, list] = {}
    for _, p in people.iterrows():
        surnames = set(p["key_surname"].split())
        forenames = set(p["key_forename"].split())
        for token in surnames:
            lookup.setdefault(token, []).append((forenames, surnames, p["personnel_no"]))

    folded: dict[str, list] = {}
    for token, entries in lookup.items():
        folded.setdefault(fold_umlauts(token), []).extend(entries)

    def _best(tokens: set[str], table: dict[str, list]) -> tuple[int, str] | None:
        best = None
        for token in tokens:
            for forenames, surnames, pno in table.get(token, []):
                if (forenames & tokens) and (surnames & tokens):
                    score = len((surnames | forenames) & tokens)
                    if best is None or score > best[0]:
                        best = (score, pno)
        return best

    def match(key: str) -> str | None:
        tokens = set(key.split())
        best = _best(tokens, lookup)
        if best is None:
            # Fallback: umlaut-folded surnames.
            folded_tokens = {fold_umlauts(t) for t in tokens}
            best = _best(folded_tokens, folded)
        return best[1] if best else None

    cols["personnel_no"] = cols["person_key"].map(match)

    # Curated links win over inference: they exist precisely because the name
    # no longer matches, so any inferred value for them would be wrong.
    overrides = load_overrides()
    if not overrides.empty:
        by_behandler = overrides.set_index("behandler_id")["personnel_no"]
        hit = cols["behandler_id"].isin(by_behandler.index)
        cols.loc[hit, "personnel_no"] = cols.loc[hit, "behandler_id"].map(by_behandler)
        log.info("applied %d curated identity links (%d columns)",
                 len(overrides), int(hit.sum()))

    # Classify each column relative to its person's base column.
    cols["initials"] = cols["initials"].fillna("").astype(str).str.strip()
    base = (
        cols.dropna(subset=["personnel_no"])
        .groupby("personnel_no")["initials"]
        .apply(lambda s: _base_initials(list(s)))
    )
    cols["base_initials"] = cols["personnel_no"].map(base)
    cols["column_role"] = [
        classify_column(i, b, f)
        for i, b, f in zip(
            cols["initials"], cols["base_initials"], cols["funktion_code"], strict=False
        )
    ]

    log.info(
        "columns: %d active, %d linked to a person, %d multi-column people",
        len(cols),
        int(cols["personnel_no"].notna().sum()),
        int((cols.dropna(subset=["personnel_no"]).groupby("personnel_no").size() > 1).sum()),
    )
    return cols


def identity_review(spine: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
    """Roster people with no agenda column, and their nearest candidates.

    Two different causes, and they need different handling:

      recent hire   joined after the agenda snapshot, so genuinely has no
                    history yet — nothing to review, no shifts to import
      name change   ZaWin still knows the person under a former surname.
                    Surname matching cannot bridge this, and forename alone is
                    far too weak to link automatically — unrelated columns
                    share forenames routinely.

    Candidates are suggested on forename agreement only and must be confirmed
    by a human before use. Nothing here is auto-linked.
    """
    linked = set(columns.dropna(subset=["personnel_no"])["personnel_no"])
    missing = spine[~spine["personnel_no"].isin(linked)].copy()

    unlinked_cols = columns[columns["personnel_no"].isna()]
    rows = []
    for _, person in missing.iterrows():
        forenames = set(person["key_forename"].split())
        cands = [
            f"{c.initials or '?'} ({c.behandler_id}): {c.name_raw} {c.vorname_raw}".strip()
            for c in unlinked_cols.itertuples()
            if forenames & set(person_key(c.name_raw, c.vorname_raw, c.initials).split())
        ]
        rows.append(
            {
                "personnel_no": person["personnel_no"],
                "surname": person["surname"],
                "forename": person["forename"],
                "service": person.get("service"),
                "joined_on": person.get("joined_on"),
                "source": person["source"],
                "candidates": " | ".join(cands[:5]) or "(none)",
                "n_candidates": len(cands),
            }
        )
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["n_candidates", "personnel_no"], ascending=[False, True])
    return out
