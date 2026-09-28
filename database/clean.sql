-- Drop every column osm2pgsql created except the three the API uses
-- (osm_id, tags, way). All tags remain available in the hstore "tags" column
-- because the import runs with --hstore-all. Works regardless of which
-- style file / osm2pgsql version produced the tables.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name IN ('planet_osm_point', 'planet_osm_line', 'planet_osm_polygon')
      AND column_name NOT IN ('osm_id', 'tags', 'way')
  LOOP
    EXECUTE format('ALTER TABLE %I DROP COLUMN %I', r.table_name, r.column_name);
  END LOOP;
END $$;
