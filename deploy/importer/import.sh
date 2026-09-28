#!/usr/bin/env bash
# Downloads an OSM extract and loads it into PostGIS in the layout the API expects.
# Re-running replaces the existing data (search is unavailable while it runs).
set -euo pipefail

: "${PGHOST:=db}" "${PGPORT:=5432}" "${PGUSER:?}" "${PGPASSWORD:?}" "${PGDATABASE:?}"
export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE

OSM_EXTRACT_URL="${OSM_EXTRACT_URL:-https://download.geofabrik.de/asia/gcc-states-latest.osm.pbf}"
OSM_BBOX="${OSM_BBOX:-}"                 # optional clip, "minlon,minlat,maxlon,maxlat"
OSM2PGSQL_CACHE_MB="${OSM2PGSQL_CACHE_MB:-2000}"
OSM2PGSQL_PROCS="${OSM2PGSQL_PROCS:-4}"
PBF="/data/$(basename "$OSM_EXTRACT_URL")"

log() { echo "[import $(date -u +%H:%M:%S)] $*"; }

log "Waiting for database at $PGHOST:$PGPORT"
until pg_isready -q; do sleep 2; done

psql -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS hstore;
SQL

log "Downloading $OSM_EXTRACT_URL"
curl -fL --retry 3 -z "$PBF" -o "$PBF.part" "$OSM_EXTRACT_URL" && [ -s "$PBF.part" ] && mv "$PBF.part" "$PBF" || rm -f "$PBF.part"
[ -s "$PBF" ] || { log "No extract available at $PBF"; exit 1; }
ls -lh "$PBF"

# osm2pgsql --create drops and recreates the tables; the view depends on them.
psql -v ON_ERROR_STOP=1 -q -c "DROP VIEW IF EXISTS planet_osm;"

BBOX_ARGS=()
[ -n "$OSM_BBOX" ] && BBOX_ARGS=(--bbox "$OSM_BBOX")

log "Importing with osm2pgsql"
osm2pgsql --create --slim --drop --hstore-all \
  -d "$PGDATABASE" -U "$PGUSER" -H "$PGHOST" -P "$PGPORT" \
  --cache "$OSM2PGSQL_CACHE_MB" --number-processes "$OSM2PGSQL_PROCS" \
  "${BBOX_ARGS[@]}" "$PBF"

for f in functions clean index cluster analyze views; do
  log "Running $f.sql"
  psql -v ON_ERROR_STOP=1 -q -f "/sql/$f.sql"
done

psql -At -c "SELECT 'points: '||count(*) FROM planet_osm_point UNION ALL SELECT 'lines: '||count(*) FROM planet_osm_line UNION ALL SELECT 'polygons: '||count(*) FROM planet_osm_polygon;"
psql -At -c "SELECT 'database size: '||pg_size_pretty(pg_database_size(current_database()));"
log "Done"
