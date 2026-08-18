"""Reconciling ZaWin BEHANDLER against an HR personnel export.

Two identifier systems have to be joined:

  ZaWin BEHANDLER   the clinical system. Includes leavers, groups, rooms and
                    placeholder rows, so it is always larger than the payroll.
  personnel export  current staff only, from payroll. Often cp1252 and
                    semicolon-delimited rather than UTF-8 and comma.

A social-security number looks like the obvious key, and ZaWin has a column for
it, but in practice it is populated for only a handful of rows — check before
relying on it. The working key is `Initialen` against the payroll's short name,
backed by normalised surname/forename matching.

BEHANDLER name fields follow no single convention, and both of these occur:
    Name="Dr <Surname> <Forename>", Vorname=NULL   -> whole name in Name
    Name="<initials>", Vorname="<Surname> <Forename>" -> initials in Name
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pandas as pd

from .db import REFERENCE_DIR
from .extract import employees

#: Personnel export. Overridable; profiles that ship their own point here.
DEFAULT_PERSONNEL = REFERENCE_DIR / "personnel_2026-06-09.csv"

_TITLE = re.compile(r"^(dr|dre|prof|me|mme|mr|m)\.?\s+", re.IGNORECASE)


def normalise(text: str | None) -> str:
    """Uppercase, strip accents, collapse whitespace, drop titles."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    s = str(text).strip()
    s = _TITLE.sub("", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().upper()


def load_personnel(path: Path | None = None) -> pd.DataFrame:
    """Read the payroll export. cp1252 + semicolons — not UTF-8, not commas."""
    path = path or DEFAULT_PERSONNEL
    df = pd.read_csv(path, sep=";", encoding="cp1252", dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]
    rename = {
        "N° personnel": "personnel_no",
        "Nom": "surname",
        "Prénom": "forename",
        "Nom abrégé": "short_name",
        "Numéro AVS": "avs",
        "Numéro de service": "service_no",
        "Lieu": "town",
        "Code langue": "language",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    df["key_surname"] = df["surname"].map(normalise)
    df["key_forename"] = df["forename"].map(normalise)
    # Payroll lists every given name; ZaWin carries one. Match on the first.
    df["key_first"] = df["key_forename"].str.split().str[0].fillna("")
    df["key_name"] = (df["key_surname"] + " " + df["key_forename"]).str.strip()
    df["key_core"] = (df["key_surname"] + " " + df["key_first"]).str.strip()
    df["key_short"] = df["short_name"].map(normalise).str.replace(" ", "")
    return df


def prepare_behandler() -> pd.DataFrame:
    """BEHANDLER with the dirty name fields untangled into a match key."""
    df = employees()
    df["initials_norm"] = df["initials"].map(normalise).str.replace(" ", "")

    def full_name(row) -> str:
        name, vor = normalise(row["name_raw"]), normalise(row["vorname_raw"])
        # If Name is just the initials, the real name sits in Vorname.
        if name and name == row["initials_norm"]:
            return vor
        return f"{name} {vor}".strip()

    df["key_name"] = df.apply(full_name, axis=1)
    df["key_name_sorted"] = df["key_name"].map(
        lambda s: " ".join(sorted(s.split()))
    )
    df["key_tokens"] = df["key_name"].map(lambda s: frozenset(s.split()))
    df["is_active"] = df["left_on"].isna()
    return df


def build(
    personnel_path: Path | None = None,
    people: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Match personnel rows to BEHANDLER rows, reporting method + confidence.

    Returns one row per personnel record. `behandler_id` is NA where no match
    was found; those rows are the ones needing a human decision.

    Pass `people` to match an already-assembled roster (see `roster.build_spine`)
    instead of reading the payroll CSV directly; it must carry the normalised
    `key_*` columns that `roster.crosswalk_keys()` adds.
    """
    people = load_personnel(personnel_path) if people is None else people
    beh = prepare_behandler()

    # Name order differs between systems, so match on the sorted token set.
    beh_by_name = {}
    for _, r in beh.iterrows():
        beh_by_name.setdefault(r["key_name_sorted"], []).append(r)
    beh_by_core = {}
    for _, r in beh.iterrows():
        beh_by_core.setdefault(" ".join(sorted(r["key_name"].split())), []).append(r)

    def pick(cands, method, confidence):
        """Prefer an active BEHANDLER when several rows collide."""
        if not cands:
            return None, "unmatched", 0.0
        if len(cands) == 1:
            return cands[0], method, confidence
        active = [c for c in cands if c["is_active"]]
        return (active or cands)[0], method + "_ambiguous", round(confidence - 0.3, 2)

    rows = []
    for _, p in people.iterrows():
        match, method, confidence = None, "unmatched", 0.0

        # 1. exact: every token of the payroll name matches
        key_full = " ".join(sorted(p["key_name"].split()))
        match, method, confidence = pick(beh_by_name.get(key_full, []), "full_name", 1.0)

        # 2. surname + first forename only
        if match is None:
            key_core = " ".join(sorted(p["key_core"].split()))
            match, method, confidence = pick(beh_by_core.get(key_core, []), "core_name", 0.9)

        # 3. token subset: ZaWin name is a subset of the payroll name
        if match is None:
            want = frozenset(p["key_core"].split())
            cands = [
                r for _, r in beh.iterrows()
                if want and r["key_tokens"] and want <= r["key_tokens"]
            ] or [
                r for _, r in beh.iterrows()
                if r["key_tokens"] and r["key_tokens"] <= frozenset(p["key_name"].split())
                and p["key_surname"] in r["key_tokens"]
            ]
            match, method, confidence = pick(cands, "token_subset", 0.75)

        # 4. truncated "Nom abrégé" prefix against the ZaWin name
        if match is None and p["key_short"]:
            pref = p["key_short"][:10]
            cands = [
                r for _, r in beh.iterrows()
                if pref and r["key_name"].replace(" ", "").startswith(pref)
            ]
            match, method, confidence = pick(cands, "short_name_prefix", 0.6)

        rows.append(
            {
                "personnel_no": p["personnel_no"],
                "surname": p["surname"],
                "forename": p["forename"],
                "short_name": p["short_name"],
                "behandler_id": match["behandler_id"] if match is not None else pd.NA,
                "behandler_initials": match["initials"] if match is not None else pd.NA,
                "behandler_active": match["is_active"] if match is not None else pd.NA,
                "match_method": method,
                "confidence": confidence,
            }
        )
    return pd.DataFrame(rows)


def unmatched_behandler(crosswalk: pd.DataFrame) -> pd.DataFrame:
    """BEHANDLER rows with no personnel counterpart, classified by likely reason.

    Expected to be large: BEHANDLER carries years of leavers plus non-person
    entries, against a payroll export listing only current staff.
    """
    beh = prepare_behandler()
    matched = set(crosswalk["behandler_id"].dropna().astype(int))
    rest = beh[~beh["behandler_id"].isin(matched)].copy()

    def reason(row) -> str:
        if row["is_group"]:
            return "group"
        if not row["is_active"]:
            return "left"
        if not row["key_name"]:
            return "no_name"
        return "active_no_payroll_match"

    rest["reason"] = rest.apply(reason, axis=1)
    return rest[
        ["behandler_id", "name_raw", "vorname_raw", "initials",
         "funktion_code", "joined_on", "left_on", "reason"]
    ]
