"""
Weather Vector Retrieval System
- Fetches weather alerts and forecasts from NWS API
- Stores documents in Lakebase (Databricks-managed Postgres)
- Provides vector search over weather narratives

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os
import sys

import requests
from flask import Flask, jsonify, request
from sentence_transformers import SentenceTransformer

import lakebase

from weather_client import (
    WeatherClient,
    parse_location,
    normalize_alert,
    normalize_forecast,
    normalize_discussion,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-app")

app = Flask(__name__)

WEATHER_TABLE_NAME = os.environ.get("WEATHER_TABLE_NAME", "weather_documents")
EMBEDDINGS_TABLE_NAME = os.environ.get("WEATHER_EMBEDDINGS_TABLE_NAME", "weather_embeddings")
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Load embedding model once at module level (not per-request)
logger.info(f"Loading embedding model {EMBEDDING_MODEL_NAME}...")
os.environ["HF_HOME"] = "/tmp/.cache/huggingface"
os.environ["TRANSFORMERS_CACHE"] = "/tmp/.cache/huggingface"
os.environ["HF_HUB_CACHE"] = "/tmp/.cache/huggingface"
embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME, cache_folder="/tmp/.cache/huggingface")
logger.info("Embedding model loaded successfully")


def ensure_weather_table():
    """
    Create the weather documents table in Lakebase if it doesn't exist yet.
    Stores weather alerts, forecasts, and forecast discussions from NWS.
    """
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {WEATHER_TABLE_NAME} (
            id TEXT PRIMARY KEY,
            location TEXT NOT NULL,
            source_type TEXT NOT NULL,
            headline TEXT,
            event TEXT,
            narrative_text TEXT,
            issued_at TIMESTAMPTZ,
            effective_at TIMESTAMPTZ,
            payload JSONB NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    lakebase.run_write(
        f"CREATE INDEX IF NOT EXISTS idx_{WEATHER_TABLE_NAME}_location "
        f"ON {WEATHER_TABLE_NAME} (location)"
    )
    lakebase.run_write(
        f"CREATE INDEX IF NOT EXISTS idx_{WEATHER_TABLE_NAME}_source_type "
        f"ON {WEATHER_TABLE_NAME} (source_type)"
    )


@app.route("/")
def index():
    """Landing page with API documentation."""
    return jsonify({
        "service": "Weather Vector Retrieval System",
        "version": "1.0.0",
        "description": "Semantic search over NWS weather alerts and forecasts using pgvector",
        "endpoints": {
            "GET /healthz": "Health check",
            "POST /weather/sync": "Sync weather data from NWS API (body: {location: 'latitude,longitude'})",
            "GET /weather/documents": "List all weather documents in database",
            "POST /weather/search": "Semantic vector search (body: {query: 'your search', top_k: 5})"
        },
        "status": "running"
    })


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/weather/sync", methods=["POST"])
def sync_weather_from_nws():
    """
    Fetch weather alerts and forecasts from the National Weather Service API
    for a set of locations (specified as lat,lon pairs) and upsert them into
    the weather_documents table in Lakebase.

    Body (JSON): {"locations": ["41.8781,-87.6298", "30.2672,-97.7431"], "limit": 50}
    Returns: {"synced": N, "locations": [...]}
    """
    ensure_weather_table()
    client = WeatherClient()

    body = request.json if request.is_json else {}
    locations = body.get("locations", [])
    limit = int(body.get("limit", 50))

    if not locations:
        return jsonify({
            "error": "No locations provided. Use lat,lon format (e.g. '41.8781,-87.6298')"
        }), 400

    total = 0
    processed_locations = []

    for location_str in locations:
        if not isinstance(location_str, str):
            continue

        coords = parse_location(location_str.strip())
        if not coords:
            logger.warning(f"Invalid location format: {location_str!r}")
            continue

        lat, lon = coords
        processed_locations.append(location_str)

        try:
            # Resolve gridpoint
            gridpoint = client.resolve_gridpoint(lat, lon)
            grid_id = gridpoint.get("gridId", "")
            forecast_url = gridpoint.get("forecast", "")

            # Fetch alerts
            alerts = client.get_active_alerts(lat, lon, limit=limit)
            for alert in alerts:
                doc = normalize_alert(alert, location_str)
                total += _upsert_weather_doc(doc)

            # Fetch forecast periods
            if forecast_url:
                periods = client.get_forecast(forecast_url)
                for period in periods[:limit]:
                    doc = normalize_forecast(period, location_str, gridpoint)
                    total += _upsert_weather_doc(doc)

            # Fetch forecast discussion
            if grid_id:
                discussion = client.get_forecast_discussion(grid_id)
                if discussion:
                    doc = normalize_discussion(discussion, location_str, grid_id)
                    if doc:
                        total += _upsert_weather_doc(doc)

        except requests.HTTPError as e:
            logger.warning(f"Failed to fetch weather for {location_str}: {e}")
            continue

    return jsonify({"synced": total, "locations": processed_locations})


@app.route("/weather/documents")
def list_weather_documents():
    """List weather documents from Lakebase."""
    limit = int(request.args.get("limit", 100))
    source_type = request.args.get("source_type")
    location = request.args.get("location")

    query = f"SELECT * FROM {WEATHER_TABLE_NAME} WHERE 1=1"
    params = []

    if source_type:
        query += " AND source_type = %s"
        params.append(source_type)

    if location:
        query += " AND location = %s"
        params.append(location)

    query += " ORDER BY synced_at DESC LIMIT %s"
    params.append(limit)

    rows = lakebase.run_query(query, tuple(params))
    return jsonify(rows)


@app.route("/weather/search", methods=["POST"])
def search_weather():
    """
    Semantic search over weather embeddings using vector similarity.
    
    Body (JSON): {"query": "risk of flooding near rivers", "top_k": 5}
    Returns: [{"location": ..., "headline": ..., "chunk_text": ..., "similarity": ...}, ...]
    """
    body = request.json if request.is_json else {}
    query_text = body.get("query", "").strip()
    top_k = body.get("top_k", 5)
    
    # Validation
    if not query_text:
        return jsonify({"error": "Missing or empty 'query' field"}), 400
    
    # Clamp top_k to reasonable bounds (1-20)
    if not isinstance(top_k, int):
        try:
            top_k = int(top_k)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid 'top_k' value, must be an integer"}), 400
    
    top_k = max(1, min(top_k, 20))
    
    try:
        # Embed the query using the same model as ingestion
        logger.info(f"Embedding query: {query_text[:50]}...")
        query_embedding = embedding_model.encode(query_text).tolist()
        query_embedding_str = str(query_embedding)
        
        # Run vector similarity search using psycopg2
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                # Check if embeddings table has any data
                cur.execute(f"SELECT COUNT(*) as cnt FROM {EMBEDDINGS_TABLE_NAME}")
                count_result = cur.fetchone()
                embedding_count = count_result[0] if count_result else 0
                
                if embedding_count == 0:
                    return jsonify({
                        "error": "No embeddings found. Run the ingestion pipeline first to populate weather_embeddings table.",
                        "results": []
                    }), 404
                
                # Vector similarity search with cosine distance
                cur.execute(
                    f"""
                    SELECT 
                        d.id,
                        d.location,
                        d.source_type,
                        d.headline,
                        d.event,
                        d.narrative_text,
                        e.chunk_text,
                        e.chunk_index,
                        1 - (e.embedding <=> %s::vector) AS similarity
                    FROM {EMBEDDINGS_TABLE_NAME} e
                    JOIN {WEATHER_TABLE_NAME} d ON d.id = e.document_id
                    ORDER BY e.embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (query_embedding_str, query_embedding_str, top_k)
                )
                
                rows = cur.fetchall()
                
                # Format results
                results = []
                for row in rows:
                    results.append({
                        "document_id": row[0],
                        "location": row[1],
                        "source_type": row[2],
                        "headline": row[3],
                        "event": row[4],
                        "narrative_text": row[5],
                        "chunk_text": row[6],
                        "chunk_index": row[7],
                        "similarity": float(row[8]) if row[8] is not None else 0.0,
                    })
                
                logger.info(f"Found {len(results)} results for query: {query_text[:50]}...")
                
                return jsonify({
                    "query": query_text,
                    "top_k": top_k,
                    "results": results,
                    "total_embeddings": embedding_count
                })
                
    except Exception as e:
        logger.exception(f"Error during vector search: {e}")
        return jsonify({"error": f"Search failed: {str(e)}"}), 500


def _upsert_weather_doc(doc: dict) -> int:
    """Upsert a single weather document into the weather_documents table.
    
    Returns 1 if successful, 0 otherwise.
    """
    import json as _json

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {WEATHER_TABLE_NAME} (
                    id, location, source_type, headline, event, narrative_text,
                    issued_at, effective_at, payload, synced_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE
                    SET location = EXCLUDED.location,
                        source_type = EXCLUDED.source_type,
                        headline = EXCLUDED.headline,
                        event = EXCLUDED.event,
                        narrative_text = EXCLUDED.narrative_text,
                        issued_at = EXCLUDED.issued_at,
                        effective_at = EXCLUDED.effective_at,
                        payload = EXCLUDED.payload,
                        synced_at = EXCLUDED.synced_at
                """,
                (
                    doc.get("id"),
                    doc.get("location"),
                    doc.get("source_type"),
                    doc.get("headline"),
                    doc.get("event"),
                    doc.get("narrative_text"),
                    doc.get("issued_at"),
                    doc.get("effective_at"),
                    _json.dumps(doc.get("payload", {})),
                ),
            )
            conn.commit()
            return 1


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8080))
    app.run(debug=True, host=host, port=port)
    print(f"Weather app running on http://{host}:{port}")
