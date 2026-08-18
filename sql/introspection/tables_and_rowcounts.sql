-- All base tables with row counts, largest first.
-- Equivalent: uv run zawin tables
--
-- sys.partitions is used rather than COUNT(*) because a COUNT over the larger
-- tables (BEHANDLERLOG 3.07M, ZUTRITTLOG 2.43M, LEISTUNG 2.36M) is needlessly
-- slow for an inventory. index_id IN (0, 1) selects the heap or clustered
-- index so rows are not double-counted across nonclustered indexes.
SELECT t.name AS table_name, SUM(p.rows) AS rows
FROM sys.tables t
JOIN sys.partitions p
  ON t.object_id = p.object_id AND p.index_id IN (0, 1)
GROUP BY t.name
ORDER BY SUM(p.rows) DESC;
