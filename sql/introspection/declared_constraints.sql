-- Declared referential integrity in ZaWin.
--
-- The foreign-key query below returns ZERO rows. That is the single most
-- important structural fact about this database: all referential integrity
-- was enforced in application code. Every relationship used downstream is
-- inferred by value-set containment — see src/zawin/fkscan.py.
--
-- This file exists so the claim stays verifiable after a re-restore rather
-- than becoming folklore.

-- Declared foreign keys (expected: 0 rows)
SELECT
    fk.name AS fk_name,
    tp.name AS parent_table,
    cp.name AS parent_column,
    tr.name AS referenced_table,
    cr.name AS referenced_column
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
JOIN sys.tables tp ON fkc.parent_object_id = tp.object_id
JOIN sys.columns cp ON fkc.parent_object_id = cp.object_id
                   AND fkc.parent_column_id = cp.column_id
JOIN sys.tables tr ON fkc.referenced_object_id = tr.object_id
JOIN sys.columns cr ON fkc.referenced_object_id = cr.object_id
                   AND fkc.referenced_column_id = cr.column_id
ORDER BY tp.name;

-- Primary keys, which DO exist (e.g. PK__TAGPLANT__5E5AE7420262E179 on
-- TAGPLANTERMIN.Zähler). These are the parent-key candidates for fkscan.
SELECT
    tc.TABLE_NAME AS table_name,
    kcu.COLUMN_NAME AS column_name,
    tc.CONSTRAINT_NAME AS constraint_name
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
ORDER BY tc.TABLE_NAME, kcu.ORDINAL_POSITION;
