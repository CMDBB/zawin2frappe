"""When an apprenticeship ended, and what the person is once it has.

Accounting files apprentices under their own service code, and that filing is
not always retired when the apprenticeship is. One person here finished in
2020, has worked as an ortho assistant ever since, and is still filed under
`Apprent` six years later. The cost is not cosmetic: an apprentice service maps
to its own discipline on purpose — apprentices rotate through every discipline,
so counting them as capacity in a clinical one over-states it — and a graduate
left in that department is a chairside assistant the optimiser cannot see.

Length does not settle it. An apprenticeship runs three years here, four
elsewhere, and longer again for anyone held back, so no cutoff separates a
current apprentice from a former one. What does separate them is that an
apprentice keeps going back to school. Their agenda carries the school days
(`COURS`, `Formation`) that `label_rules` already classify as `training`, the
practice books them for the whole apprenticeship in advance, and when the
apprenticeship ends they stop for good:

    ALA   school to 2027-06-29, worked to 2027-07-01     2 working days after
    MBU   school to 2027-06-28, worked to 2027-07-02     4
    FRC   school to 2020-06-25, worked to 2026-12-18   656   <- finished

Within an apprenticeship the longest break in school months observed across the
ten the agenda covers is two — the summer — so normal work continuing more than
`apprenticeship_grace_months` past the last school day is the end of the
apprenticeship, not a holiday in the middle of one.

`training` covers every course the practice books, though, so the rows alone do
not say which of them were schooling: FRC took a radiology day in 2025, five
years after qualifying, and read naively that would be her last school day. So
school days are grouped into spells — same grace period — and a spell too short
to be a school year is a course (see `schooling_end`).

Where the profile marks the apprentice service with `graduates_to`, a finished
apprentice is re-filed under that service and is thereafter an ordinary
employee of it: designation, discipline and clinical status all follow from the
new code, so someone whose graduate service leaves the discipline open (the
assistant pool does) is placed by the same agenda signal as everyone else in it
— which is how FRC lands in orthodontics rather than in a department of her
own.

Nothing is inferred without evidence. An apprentice whose agenda holds no
training rows at all keeps the filing accounting gave them; if they have been
working far longer than any apprenticeship lasts, that is reported for a human
to look at rather than acted on.
"""

from __future__ import annotations

import logging

import pandas as pd

from .. import settings, shifts

log = logging.getLogger(__name__)

#: Kinds that positively place someone at work — a "normal assignment", as
#: against the school days that mark the apprenticeship still running.
WORKED_KINDS = tuple(shifts.PRESENCE_KINDS)

TRAINING_KIND = "training"

#: Months of ordinary work after the last school day before the apprenticeship
#: is called finished. Two is the longest mid-apprenticeship break in the data
#: (the summer), so three is the first value that cannot be one.
#: Overridable as threshold("apprenticeship_grace_months").
DEFAULT_GRACE_MONTHS = 3

#: An apprenticeship nobody has heard of runs this long. Only used to report
#: apprentices whose agenda carries no training rows to judge them on.
#: Overridable as threshold("apprenticeship_max_years").
DEFAULT_MAX_YEARS = 5

#: School days a spell of `training` must hold to be schooling rather than a
#: course. A school year is thirty-odd days here and the shortest spell in the
#: data is forty-five; a radiology refresher is one.
#: Overridable as threshold("apprenticeship_min_school_days").
DEFAULT_MIN_SCHOOL_DAYS = 20


def schooling_end(days: pd.Series, max_gap_days: int, min_days: int) -> pd.Timestamp:
	"""The last day of someone's last spell of schooling, or NaT if they had none.

	`training` covers every course the practice books, so the rows alone do not
	say which were an apprenticeship: the one person here who has finished took
	a radiology day five years afterwards, and reading her last training row
	would date the apprenticeship to that. Days are therefore grouped into
	spells — a gap of more than `max_gap_days` starts a new one — and a spell
	shorter than `min_days` is a course, not a school year.
	"""
	unique = pd.Series(sorted(set(pd.to_datetime(days).dt.normalize())))
	if unique.empty:
		return pd.NaT
	spells = unique.groupby(unique.diff().dt.days.gt(max_gap_days).cumsum())
	ends = [spell.max() for _, spell in spells if len(spell) >= min_days]
	return max(ends) if ends else pd.NaT


def milestones(annotated: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""Per person: end of schooling, last working day, and the work since.

	One row per person who appears in the agenda at all. `worked_after` counts
	only the rows that follow the schooling, which is the evidence it is over
	rather than merely paused for the summer.
	"""
	out_columns = ["personnel_no", "first_seen", "schooling_ended", "last_worked", "worked_after"]
	link = columns.dropna(subset=["personnel_no"])[["behandler_id", "personnel_no"]]
	rows = annotated.merge(link, left_on="FK_Behandler", right_on="behandler_id", how="inner")
	if rows.empty:
		return pd.DataFrame(columns=out_columns)

	prof = settings.get()
	# In days, because a spell is a run of dates: the grace period is the same
	# quantity either way, and a month is only ever an approximation of it.
	max_gap = int(prof.threshold("apprenticeship_grace_months", DEFAULT_GRACE_MONTHS) * 31)
	min_days = int(prof.threshold("apprenticeship_min_school_days", DEFAULT_MIN_SCHOOL_DAYS))

	rows = rows.assign(date=pd.to_datetime(rows["Datum"]))
	worked = rows[rows["shift_kind"].isin(WORKED_KINDS)]
	training = rows[rows["shift_kind"].eq(TRAINING_KIND)]

	out = rows.groupby("personnel_no", as_index=False)["date"].min().rename(columns={"date": "first_seen"})
	out["schooling_ended"] = out["personnel_no"].map(
		training.groupby("personnel_no")["date"].apply(schooling_end, max_gap_days=max_gap, min_days=min_days)
	)
	out["last_worked"] = out["personnel_no"].map(worked.groupby("personnel_no")["date"].max())

	after = worked.merge(out[["personnel_no", "schooling_ended"]], on="personnel_no", how="left")
	after = after[after["date"] > after["schooling_ended"]]
	out["worked_after"] = out["personnel_no"].map(after.groupby("personnel_no").size()).fillna(0).astype(int)
	return out[out_columns]


def finished(spine: pd.DataFrame, annotated: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""The apprentices whose apprenticeship the agenda says is over.

	One row per person to re-file, with `graduates_to` — the service they are
	now — and the evidence for saying so. Empty unless the profile marks at
	least one service with `graduates_to`.

	Two conditions, both on the work done since the schooling ended: it has to
	have run more than `apprenticeship_grace_months` past the last school day,
	longer than any break within an apprenticeship, and there has to be enough
	of it to be a job rather than a stray row — `min_worked_rows`, the same
	floor `pipeline.scope` puts under an agenda signal.
	"""
	out_columns = ["personnel_no", "service_no", "graduates_to", "finished_on", "worked_after"]
	prof = settings.get()
	graduate_service = prof.apprenticeship_services
	if not graduate_service:
		return pd.DataFrame(columns=out_columns)

	apprentices = spine[spine["service_no"].isin(graduate_service)][["personnel_no", "service_no"]]
	if apprentices.empty:
		return pd.DataFrame(columns=out_columns)

	seen = milestones(annotated, columns).set_index("personnel_no")
	df = apprentices.join(seen, on="personnel_no")
	df["graduates_to"] = df["service_no"].map(graduate_service)

	grace = int(prof.threshold("apprenticeship_grace_months", DEFAULT_GRACE_MONTHS))
	min_worked = prof.threshold("min_worked_rows", 30)
	over = df["last_worked"] > df["schooling_ended"] + pd.DateOffset(months=grace)
	done = over & df["worked_after"].ge(min_worked)

	_report_unjudged(df[~done & df["schooling_ended"].isna()])

	out = df[done].rename(columns={"schooling_ended": "finished_on"})
	return out[out_columns].sort_values("personnel_no").reset_index(drop=True)


def _report_unjudged(unjudged: pd.DataFrame) -> None:
	"""Apprentices with no school days in the agenda to judge them on.

	Their filing stands — an absent signal is not evidence against it. Worth a
	line in the log all the same for anyone working longer than an
	apprenticeship can run, because that is where a stale filing hides.
	"""
	if unjudged.empty:
		return
	years = settings.get().threshold("apprenticeship_max_years", DEFAULT_MAX_YEARS)
	tenure = (unjudged["last_worked"] - unjudged["first_seen"]).dt.days / 365.25
	stale = unjudged[tenure > years]
	log.info("%d apprentice(s) have no schooling in the agenda to date", len(unjudged))
	if not stale.empty:
		log.warning(
			"%d person(s) filed as apprentices have worked more than %g years and have no "
			"school days to date the apprenticeship by; check their service code: %s",
			len(stale),
			years,
			", ".join(stale["personnel_no"].astype(str)),
		)


def apply(spine: pd.DataFrame, annotated: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""Re-file finished apprentices under the service they graduated into.

	Rewrites `service_no` rather than any one downstream field, because every
	consumer of it — designation, discipline, clinical status, whether the
	agenda signal is allowed to place them — must agree about who the person is
	now. `service_no_filed` keeps what accounting said, and
	`apprenticeship_ended` the last school day, so the change is visible in the
	spine rather than only in the log.

	Must run before `pipeline.discipline`: an apprentice service carries a
	discipline of its own, and accounting's discipline wins there over anything
	the agenda says.
	"""
	df = spine.copy()
	df["service_no_filed"] = df["service_no"]
	df["apprenticeship_ended"] = pd.NaT

	done = finished(df, annotated, columns)
	if done.empty:
		return df

	prof = settings.get()
	by_person = done.set_index("personnel_no")
	hit = df["personnel_no"].isin(by_person.index)
	df.loc[hit, "service_no"] = df.loc[hit, "personnel_no"].map(by_person["graduates_to"])
	df.loc[hit, "apprenticeship_ended"] = df.loc[hit, "personnel_no"].map(by_person["finished_on"])
	# `roster` derived both of these from the service code, and `discipline` is
	# read back as accounting's answer by `pipeline.discipline` — where it would
	# otherwise keep the graduate in the apprenticeship department whatever the
	# new code says. They move with the code they came from.
	df.loc[hit, "discipline"] = df.loc[hit, "service_no"].map(lambda c: prof.service(c).discipline)
	df.loc[hit, "is_clinical"] = df.loc[hit, "service_no"].isin(prof.clinical_services)

	log.info(
		"%d apprenticeship(s) finished; re-filed from the service accounting still has them under: %s",
		len(done),
		", ".join(
			f"{r.personnel_no} {r.service_no}->{r.graduates_to} "
			f"(last school day {r.finished_on:%Y-%m-%d}, {r.worked_after} working days since)"
			for r in done.itertuples()
		),
	)
	return df
