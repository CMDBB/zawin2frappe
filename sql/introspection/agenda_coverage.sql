-- Establishes what the agenda tables actually cover.
-- Results are recorded in docs/findings.md; re-run after any re-restore.

-- Live table: 2018-01-01 .. 2028-12-31, 954,239 rows.
-- The far future bound is real forward bookings, not junk data.
SELECT MIN(Datum) AS min_datum, MAX(Datum) AS max_datum, COUNT(*) AS rows
FROM TAGPLANTERMIN;

-- Archive: 2009-01-05 .. 2017-12-31, 380,351 rows. Disjoint from the live
-- table, so the two can be UNION ALL'd without deduplication.
SELECT MIN(Datum) AS min_datum, MAX(Datum) AS max_datum, COUNT(*) AS rows
FROM TAGPLANTERMINARCHIV;

-- The split that matters: over half of TAGPLANTERMIN has no patient at all.
-- Those rows are the staff agenda (working blocks, absence, training).
SELECT
    CASE WHEN FK_Patient IS NULL OR FK_Patient = 0
         THEN 'no_patient' ELSE 'has_patient' END AS kind,
    COUNT(*) AS n
FROM TAGPLANTERMIN
GROUP BY CASE WHEN FK_Patient IS NULL OR FK_Patient = 0
              THEN 'no_patient' ELSE 'has_patient' END;

-- Patient-less rows broken down by category. Note the join is on the
-- bracket-quoted umlaut column [Zähler].
SELECT ISNULL(k.Bezeichnung_1, '(no category)') AS category, COUNT(*) AS n
FROM TAGPLANTERMIN t
LEFT JOIN TAGPLANTERMINKATEGORIE k
       ON k.[Zähler] = t.FK_TagPlanTerminKategorie
WHERE t.FK_Patient IS NULL OR t.FK_Patient = 0
GROUP BY k.Bezeichnung_1
ORDER BY COUNT(*) DESC;
