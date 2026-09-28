# Deploying a private GCC instance

One server runs everything in Docker: PostGIS with OpenStreetMap data for the six GCC states, the search API, and the web app behind Caddy with automatic HTTPS.

```
browser ──HTTPS──▶ web (Caddy: static app + /api proxy) ──▶ api (Flask/gunicorn) ──▶ db (PostGIS)
                                                                  ▲
                              importer (one-off: Geofabrik extract → osm2pgsql) ─┘
```

## 1. Server

- Ubuntu 24.04, **4 vCPU / 8 GB RAM / 80 GB SSD** is comfortable for the full GCC extract. UAE-only (see `OSM_BBOX`) fits in less.
- Open ports 80 and 443. Nothing else needs to be public.
- Install Docker: `curl -fsSL https://get.docker.com | sh`

## 2. Domain

Create an `A` record (e.g. `search.yourdomain.com`) pointing at the server's IP. Caddy obtains the certificate on first start.

## 3. Firebase (sign-in, saved presets, search log)

1. [console.firebase.google.com](https://console.firebase.google.com) → **Add project**.
2. **Authentication → Sign-in method**: enable **Google** and **Email/Password** with *Email link* turned on.
3. **Authentication → Settings → Authorized domains**: add your domain.
4. **Firestore Database → Create database** (production mode). Paste `deploy/firestore.rules` into **Rules** and publish.
5. **Project settings → General → Your apps → Web app**: register one; copy the six config values into `.env` (`FIREBASE_*`).
6. **Project settings → Service accounts → Generate new private key**: save the file as `secrets/service_account.json` on the server.

Anyone with a Google account can sign in to Firebase. Set `ALLOWED_EMAILS` in `.env` (emails and/or `@domains`) so only your people can actually search.

## 4. Mapbox

[account.mapbox.com](https://account.mapbox.com) → create a public token, restrict it to your domain (URL restriction), put it in `MAPBOX_TOKEN`. It powers the place-name search box and the satellite layer. Coordinate search (`25.2, 55.27`) works without it.

## 5. Install and start

```bash
git clone https://github.com/bvgtdavantage-del/osm-search.git
cd osm-search
cp .env.example .env              # fill in DOMAIN, PG_PASSWORD, FIREBASE_*, MAPBOX_TOKEN, ALLOWED_EMAILS
# upload secrets/service_account.json
docker compose up -d --build
```

## 6. Load the map data

```bash
docker compose --profile import run --rm importer
```

Downloads `gcc-states-latest.osm.pbf` from Geofabrik, imports it, strips unused columns, builds indexes, and prints row counts and database size. Expect roughly 20–60 minutes depending on the server. Then check:

```bash
curl https://search.yourdomain.com/api/health     # {"status":"ok"}
```

UAE only: set `OSM_BBOX=51.5,22.6,56.4,26.1` in `.env` before importing.

## 7. Keep data fresh

Geofabrik updates daily. Weekly refresh via cron (search is unavailable while it runs):

```bash
crontab -e
0 3 * * 5 cd /root/osm-search && docker compose --profile import run --rm importer >> /var/log/osm-import.log 2>&1
```

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f api` |
| Restart after `.env` change (API) | `docker compose up -d api` |
| Rebuild after frontend `.env` change (title, Firebase, Mapbox, map centre) | `docker compose up -d --build web` |
| Update code | `git pull && docker compose up -d --build` |
| Database shell | `docker compose exec db psql -U osm osm` |

No database backup is needed: everything in it is re-importable. User presets and the search log live in Firestore.

## Settings reference

All in `.env` — see `.env.example` for comments. The ones that change behaviour:

- `AUTH_REQUIRED=false` opens search to anyone who reaches the site. Only for testing.
- `LOG_SEARCHES=true` writes every search (user, area, filters, results) to Firestore `searches`.
- `MAX_AREA_KM2`, `MAX_RESULTS`, `STATEMENT_TIMEOUT` bound query cost.
