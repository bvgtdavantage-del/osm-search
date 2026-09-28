from psycopg2 import sql
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import json
from functools import wraps
import os
from loguru import logger
from datetime import datetime
import math
import time


def env_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# --- Configuration -----------------------------------------------------------
# AUTH_REQUIRED   verify Firebase ID tokens on /intersection (default: true)
# ALLOWED_EMAILS  comma-separated emails and/or @domains allowed to search (default: any signed-in user)
# LOG_SEARCHES    write every search to Firestore collection "searches" (default: true)
# FIREBASE_CREDENTIALS  path to the service-account JSON (default: service_account.json)
# CORS_ORIGINS    comma-separated allowed origins (default: *)
# MAX_AREA_KM2    reject searches over a larger bounding box (default: 4,000,000)
# MAX_RESULTS     row limit per search (default: 100)
# STATEMENT_TIMEOUT  Postgres statement timeout (default: 100s)
AUTH_REQUIRED = env_flag("AUTH_REQUIRED", True)
LOG_SEARCHES = env_flag("LOG_SEARCHES", True)
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "service_account.json")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")
MAX_AREA_KM2 = float(os.environ.get("MAX_AREA_KM2", "4e6"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "100"))
STATEMENT_TIMEOUT = os.environ.get("STATEMENT_TIMEOUT", "100s")
ALLOWED_EMAILS = [
    e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
]


def email_allowed(idinfo):
    if not ALLOWED_EMAILS:
        return True
    email = (idinfo.get("email") or "").lower()
    if not email or not idinfo.get("email_verified", False):
        return False
    domain = "@" + email.split("@")[-1]
    return email in ALLOWED_EMAILS or domain in ALLOWED_EMAILS

auth = None
db = None
firestore = None

if AUTH_REQUIRED or LOG_SEARCHES:
    if os.path.exists(FIREBASE_CREDENTIALS):
        import firebase_admin
        from firebase_admin import credentials
        from firebase_admin import auth as firebase_auth
        from firebase_admin import firestore as firebase_firestore

        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_CREDENTIALS))
        auth = firebase_auth
        if LOG_SEARCHES:
            firestore = firebase_firestore
            db = firebase_firestore.client()
    elif AUTH_REQUIRED:
        raise RuntimeError(
            f"AUTH_REQUIRED is on but {FIREBASE_CREDENTIALS} was not found. "
            "Provide the Firebase service account file or set AUTH_REQUIRED=false."
        )
    else:
        logger.warning(f"{FIREBASE_CREDENTIALS} not found; search logging disabled.")
        LOG_SEARCHES = False

if not AUTH_REQUIRED:
    logger.warning("AUTH_REQUIRED=false: /intersection is open to anyone who can reach it.")

app = Flask(__name__)

CORS(app, origins=[o.strip() for o in CORS_ORIGINS.split(",")] if CORS_ORIGINS != "*" else "*")

ALLOWED_COMPARISONS = [
    "=",
    "!=",
    ">",
    "<",
    ">=",
    "<=",
    "starts with",
    "ends with",
    "contains",
    "does not contain",
    "is null",
    "is not null",
]

ALLOWED_METHODS = ["OR", "AND"]

ALLOWED_CASTS = ["integer", "float", "cast_to_int", "cast_to_float"]

TABLES = {
    "point": "planet_osm_point",
    "line": "planet_osm_line",
    "polygon": "planet_osm_polygon",
    "any": "planet_osm",
}


class BadRequest(Exception):
    pass


class QueryTimeout(Exception):
    pass


@app.errorhandler(BadRequest)
def handle_bad_request(e):
    logger.warning(f"Bad request: {e}")
    return {"message": str(e), "data": None, "error": "Bad Request"}, 400


def get_db_connection():
    return psycopg2.connect(
        database=os.environ.get("PG_DB"),
        host=os.environ.get("PG_HOST"),
        port=os.environ.get("PG_PORT"),
        user=os.environ.get("PG_USER"),
        password=os.environ.get("PG_PASSWORD"),
    )


def query_with_timing(conn, query):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(STATEMENT_TIMEOUT)))

    t1 = datetime.now()
    try:
        cur.execute(query)
    except psycopg2.errors.QueryCanceled:
        raise QueryTimeout()
    data = cur.fetchall()
    cur.close()
    t2 = datetime.now()

    for d in data:
        d.pop("point_geom", None)

    return (data, t2 - t1)


def get_token():
    header = request.headers.get("Authorization", "")
    parts = header.split(" ")
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] not in ("", "false", "null"):
        return parts[1]
    return None


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AUTH_REQUIRED:
            request.user = None
            return f(*args, **kwargs)

        token = get_token()
        if not token:
            logger.info("Request with missing authentication token.")
            return {
                "message": "Authentication Token is missing!",
                "data": None,
                "error": "Unauthorized",
            }, 401
        try:
            idinfo = auth.verify_id_token(token)
        except Exception as e:
            logger.warning(f"Token verification failed: {e}")
            return {
                "message": "Invalid Authentication token!",
                "data": None,
                "error": "Unauthorized",
            }, 403

        if not email_allowed(idinfo):
            logger.warning(f"Rejected search from non-allowlisted user {idinfo.get('email')}")
            return {
                "message": "This account is not authorised to use this service.",
                "data": None,
                "error": "Forbidden",
            }, 403

        request.user = idinfo
        logger.info(f"Authenticated request by {idinfo.get('email')}")
        return f(*args, **kwargs)

    return decorated


def make_filter_query(filter):
    subfilters = filter.get("filters") or []
    if not subfilters:
        raise BadRequest("Each feature needs at least one filter.")

    method = filter.get("method", "AND")
    if method not in ALLOWED_METHODS:
        raise BadRequest(f"Invalid method {method!r}")

    parts = []

    for subfilter in subfilters:
        comparison = subfilter.get("comparison")
        if comparison not in ALLOWED_COMPARISONS:
            raise BadRequest(f"Invalid comparison {comparison!r}")

        key = str(subfilter.get("parameter", ""))
        value = subfilter.get("value")
        value = "" if value is None else str(value)

        if comparison == "=":
            part = sql.SQL("(tags @> hstore({key}, {value}))").format(
                key=sql.Literal(key), value=sql.Literal(value)
            )
        elif comparison == "!=":
            part = sql.SQL("NOT(tags @> hstore({key}, {value}))").format(
                key=sql.Literal(key), value=sql.Literal(value)
            )
        elif comparison == "is null":
            part = sql.SQL("NOT(tags ? {key})").format(key=sql.Literal(key))
        elif comparison == "is not null":
            part = sql.SQL("(tags ? {key})").format(key=sql.Literal(key))
        else:
            parameter = sql.SQL("tags->{key}").format(key=sql.Literal(key))

            cast = subfilter.get("cast")
            if cast:
                if cast not in ALLOWED_CASTS:
                    raise BadRequest(f"Invalid cast {cast!r}")
                if cast == "cast_to_float":
                    parameter = sql.SQL("cast_to_float({p}, 0.0)").format(p=parameter)
                elif cast == "cast_to_int":
                    parameter = sql.SQL("cast_to_int({p}, 0)").format(p=parameter)
                else:
                    parameter = sql.SQL("CAST({p} AS {cast})").format(
                        p=parameter, cast=sql.SQL(cast)
                    )

            op = comparison
            if comparison == "starts with":
                value, op = f"{value}%", "ILIKE"
            elif comparison == "ends with":
                value, op = f"%{value}", "ILIKE"
            elif comparison == "contains":
                value, op = f"%{value}%", "ILIKE"
            elif comparison == "does not contain":
                value, op = f"%{value}%", "NOT ILIKE"

            part = sql.SQL("({p} {op} {value})").format(
                p=parameter, op=sql.SQL(op), value=sql.Literal(value)
            )

        parts.append(part)

    return sql.SQL(f" {method} ").join(parts)


def feature_table(feature):
    table = TABLES.get(feature.get("type"))
    if table is None:
        raise BadRequest(f"Invalid feature type {feature.get('type')!r}")
    return sql.Identifier(table)


def parse_args(args):
    try:
        buffer = int(args.get("buffer"))
        filters = json.loads(args.get("filters"))
        l = float(args.get("l"))
        b = float(args.get("b"))
        r = float(args.get("r"))
        t = float(args.get("t"))
    except (TypeError, ValueError) as e:
        raise BadRequest(f"Missing or malformed parameter: {e}")

    if not isinstance(filters, list) or not filters:
        raise BadRequest("filters must be a non-empty list")
    if buffer < 0 or buffer > 100000:
        raise BadRequest("buffer out of range")

    return buffer, filters, [l, b, r, t]


@app.route("/intersection")
@token_required
def get_intersection():
    buffer, filters, bbox = parse_args(request.args)
    l, b, r, t = bbox

    area = (
        math.pow(6371, 2)
        * math.pi
        * abs(math.sin(math.radians(t)) - math.sin(math.radians(b)))
        * abs(r - l)
        / 180
    )

    # reject queries that are too large
    if area > MAX_AREA_KM2:
        return Response(status=400)

    bbox_filter = sql.SQL(
        "AND (way && ST_Transform(ST_MakeEnvelope({left}, {bottom}, {right}, {top}, 4326), 3857))"
    ).format(
        left=sql.Literal(l),
        bottom=sql.Literal(b),
        right=sql.Literal(r),
        top=sql.Literal(t),
    )

    first = filters[0]
    first_assembled = sql.SQL(
        "SELECT tags->'name' AS name, ST_Centroid(way) AS point_geom, way AS geom FROM {table} WHERE ({filter}) {bbox}"
    ).format(table=feature_table(first), filter=make_filter_query(first), bbox=bbox_filter)

    logger.info(f"Buffer: {buffer}\tFilters: {filters}\tBbox: {bbox}")

    join_query = sql.SQL(
        "SELECT DISTINCT point_geom, name, ST_Y(ST_Transform(point_geom, 4326)) AS lat, ST_X(ST_Transform(point_geom, 4326)) as lng FROM ({point}) point "
    ).format(point=first_assembled)

    for i, f in enumerate(filters[1:]):
        subquery = sql.SQL("SELECT way AS geom FROM {table} WHERE ({filter}) {bbox}").format(
            table=feature_table(f), filter=make_filter_query(f), bbox=bbox_filter
        )
        join_query = sql.SQL(
            "{join_query} JOIN ({q}) {alias} ON ST_DWithin(point.geom, {alias}.geom, {buffer})"
        ).format(
            join_query=join_query,
            q=subquery,
            alias=sql.Identifier(f"subquery{i}"),
            buffer=sql.Literal(buffer),
        )

    join_query = sql.SQL("{join_query} LIMIT {limit}").format(
        join_query=join_query, limit=sql.Literal(MAX_RESULTS)
    )

    conn = get_db_connection()
    try:
        query_text = join_query.as_string(conn)
        logger.info(f"Executing query: {query_text}")
        try:
            data, tdiff = query_with_timing(conn, join_query)
        except QueryTimeout:
            logger.warning("Request timed out")
            return Response(status=400)
    finally:
        conn.close()

    user = getattr(request, "user", None) or {}

    if LOG_SEARCHES and db is not None:
        try:
            db.collection("searches").document(str(time.time_ns())).set(
                {
                    "timestamp": firestore.SERVER_TIMESTAMP,
                    "query": query_text,
                    "user_uid": user.get("uid"),
                    "user_email": user.get("email"),
                    "bbox": bbox,
                    "filters": filters,
                    "query_time": tdiff.total_seconds(),
                    "query_nresults": len(data),
                    "query_results": data,
                }
            )
        except Exception as e:
            logger.warning(f"Failed to log search to Firestore: {e}")

    logger.info(f"Found {len(data)} results in {tdiff} seconds")

    return jsonify(data)


@app.route("/health")
def health():
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('planet_osm') IS NOT NULL")
            ready = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        return {"status": "db_unreachable", "error": str(e)}, 503
    if not ready:
        return {"status": "no_data", "error": "planet_osm view missing; run the importer"}, 503
    return {"status": "ok"}


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /", mimetype="text/plain")


def start():
    app.run(port=5050)


if __name__ == "__main__":
    start()
