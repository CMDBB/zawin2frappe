"""Normalising the free-text shift vocabulary in TAGPLANTERMIN.Beschreibung.

Shift assignment at this practice is manual: a staff member types a label into
a calendar entry. There is no controlled vocabulary, so ~11k distinct strings
encode what is really a small taxonomy. 96.4% of the 382,995 uncategorised
patient-less rows carry a non-blank label.

Observed conventions, in priority order (first match wins — the order matters,
e.g. "PAUSE + COLLOQUE" is a break, not a meeting):

  ABS...            absence            174,429 rows
  PRES.../PRESENCE  present at work     ~40,000 rows
  TRANSITION        chair changeover     22,859
  PAUSE             break                12,655
  RECEPTION         reception duty       ~15,000
  COLLOQUE          staff meeting         3,926
  NE RIEN METTRE    do-not-book marker    ~2,100
  TELEPHONIE / BACK OFFICE / ADMIN / POLYVALENCE   non-chairside duty
  GARDE             on-call
  COURS             training

Trailing initials are an audit trail, not part of the label: "/ HK", ", mmr",
"Agenda vérifié kd", "CL ok vb". They are stripped before matching.
"""
from __future__ import annotations

import re

import pandas as pd

from . import settings

#: Editor-initials / verification chatter appended to labels.
_AUDIT_SUFFIX = re.compile(
    r"(?:[\r\n]+.*)"                       # anything after a newline
    r"|(?:\s*[/,]\s*[A-Za-z]{2,4}\s*$)"    # "/ HK", ", mmr"
    r"|(?:\s+(?:CL\s+)?ok\s+\w+\s*$)",     # "CL ok vb"
    re.IGNORECASE | re.DOTALL,
)

_RULE_CACHE: dict[int, list] = {}


def rules() -> list[tuple]:
    """Compiled (pattern, kind, counts_as_worked), first match wins.

    The vocabulary is practice-specific — these are the French conventions this
    practice types by hand — so it comes from the profile, not from code.
    Cached per profile object.
    """
    prof = settings.get()
    key = id(prof)
    if key not in _RULE_CACHE:
        _RULE_CACHE[key] = [
            (re.compile(pat, re.IGNORECASE), kind, bool(worked))
            for pat, kind, worked in prof.label_rules
        ]
    return _RULE_CACHE[key]

#: Discipline hints that appear inside PRES labels.
_DISCIPLINE = re.compile(r"\b(ortho|hd|poly|rec|admin|dp|sv)\b", re.IGNORECASE)


def clean_label(raw: str | None) -> str:
    """Strip audit chatter and normalise whitespace."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    text = str(raw)
    # repeatedly strip trailing audit fragments
    for _ in range(3):
        new = _AUDIT_SUFFIX.sub("", text).strip()
        if new == text:
            break
        text = new
    return re.sub(r"\s+", " ", text).strip()


def classify(raw: str | None) -> tuple[str, bool]:
    """Map a raw Beschreibung to (kind, counts_as_worked)."""
    label = clean_label(raw)
    if not label:
        return "unlabelled", False
    for pattern, kind, worked in rules():
        if pattern.search(label):
            return kind, worked
    return "other", False


def discipline_hint(raw: str | None) -> str | None:
    """Extract the discipline token from labels like 'PRES ORTHO CFO'."""
    label = clean_label(raw)
    m = _DISCIPLINE.search(label)
    return m.group(1).lower() if m else None


#: Shift windows in minutes since midnight. VonZeit/BisZeit are minutes from
#: midnight on a 15-minute grid (TAGPLAN.Zeitraster = 15). The day bounds and
#: the AM/PM boundary differ by practice, so they come from the profile.


def shift_window(von: int | None, bis: int | None) -> str:
    """Bucket a time range into autoshift's atomic AM/PM shifts."""
    if von is None or bis is None or pd.isna(von) or pd.isna(bis):
        return "unknown"
    prof = settings.get()
    von, bis = int(von), int(bis)
    if bis - von <= 0:
        return "empty"
    # A 15-minute marker is not a shift; TRANSITION entries look like this.
    substantial = int(prof.threshold("substantial_minutes", 120))
    if bis - von < substantial:
        return "short"
    am_minutes = max(0, min(bis, prof.midday) - max(von, prof.day_start))
    pm_minutes = max(0, min(bis, prof.day_end) - max(von, prof.midday))
    if am_minutes >= substantial and pm_minutes >= substantial:
        return "full_day"
    if am_minutes >= pm_minutes:
        return "am"
    return "pm"


def annotate(df: pd.DataFrame, label_col: str = "Beschreibung") -> pd.DataFrame:
    """Add kind / worked / window columns to an agenda DataFrame."""
    out = df.copy()
    classified = out[label_col].map(classify)
    out["shift_kind"] = [c[0] for c in classified]
    out["counts_as_worked"] = [c[1] for c in classified]
    out["label_clean"] = out[label_col].map(clean_label)
    out["discipline_hint"] = out[label_col].map(discipline_hint)
    out["shift_window"] = [
        shift_window(v, b) for v, b in zip(out["VonZeit"], out["BisZeit"], strict=False)
    ]
    return out


# ---------------------------------------------------------------------------
# Bookkeeping style
# ---------------------------------------------------------------------------
#
# The practice records agendas in two opposite ways, and which one applies
# depends on whether the employee sees patients. Verified over 2024:
#
#   absence-marked (46 staff)   median 1,329 patient appointments/year
#                               only 9% have none
#       The agenda records when the person is *away* (ABS blocks). Working
#       time is the practice day MINUS those blocks; the patient bookings
#       fill the gaps. Reading PRES rows for these people finds almost
#       nothing.
#
#   presence-marked (40 staff)  98% have ZERO patient appointments
#                               reception, admin, assistants
#       The agenda records when the person IS there (PRES / RECEPTION /
#       ADMIN blocks), because they have no patient bookings to imply it.
#
# Extracting shifts with a single rule therefore loses roughly half the
# workforce. Use classify_style() to pick per employee.

STYLE_ABSENCE = "absence_marked"
STYLE_PRESENCE = "presence_marked"
STYLE_MIXED = "mixed"

#: kinds that positively assert the employee was at work
PRESENCE_KINDS = frozenset({"present", "reception", "admin", "remote", "on_call"})


def classify_style(annotated: pd.DataFrame, min_rows: int = 50) -> pd.DataFrame:
    """Per-employee bookkeeping style, from an annotate()d agenda frame.

    Returns one row per FK_Behandler with absence/presence counts and a
    `style` of absence_marked / presence_marked / mixed.
    """
    counts = pd.crosstab(annotated["FK_Behandler"], annotated["shift_kind"])
    absence = counts.get("absence", pd.Series(0, index=counts.index))
    presence = sum(
        (counts.get(k, pd.Series(0, index=counts.index)) for k in PRESENCE_KINDS),
        start=pd.Series(0, index=counts.index),
    )
    out = pd.DataFrame({"absence_rows": absence, "presence_rows": presence})
    out = out[(out.absence_rows + out.presence_rows) >= min_rows]

    def style(r):
        if r.absence_rows > 3 * r.presence_rows:
            return STYLE_ABSENCE
        if r.presence_rows > 3 * r.absence_rows:
            return STYLE_PRESENCE
        return STYLE_MIXED

    out["style"] = out.apply(style, axis=1)
    return out.reset_index()
