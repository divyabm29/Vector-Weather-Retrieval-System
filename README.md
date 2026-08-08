# Weather Vector Retrieval System

A Flask application that fetches weather alerts and forecasts from the National Weather Service (NWS) API and stores them in Lakebase (Databricks-managed Postgres) for vector search and analysis.

## Architecture

* **weather_client.py**: NWS API client with methods to:
  - Resolve lat/lon to NWS gridpoint
  - Fetch active alerts
  - Fetch forecast periods
  - Fetch forecast discussions (AFD)
  - Normalize all data into a consistent document format

* **app.py**: Flask application with endpoints:
  - `POST /weather/sync`: Fetch and sync weather data for locations
  - `GET /weather/documents`: List stored weather documents
  - `GET /healthz`: Health check

* **Lakebase Table**: `weather_documents` with fields:
  - `id`: Stable dedup key
  - `location`: City/state or lat,lon
  - `source_type`: "alert", "forecast", or "discussion"
  - `headline`: Brief title
  - `event`: Event type (e.g., "Flash Flood Warning")
  - `narrative_text`: Free-text body for embedding
  - `issued_at`, `effective_at`: Timestamps
  - `payload`: Raw JSON for provenance
  - `synced_at`: Sync timestamp

## Usage

### Sync Weather Data

```bash
POST /weather/sync
Content-Type: application/json

{
  "locations": ["41.8781,-87.6298", "30.2672,-97.7431"],
  "limit": 50
}
```

**Note**: Locations must be in `lat,lon` format. City/state geocoding is not implemented.

Response:
```json
{
  "synced": 42,
  "locations": ["41.8781,-87.6298", "30.2672,-97.7431"]
}
```

### List Weather Documents

```bash
GET /weather/documents?limit=100&source_type=alert&location=41.8781,-87.6298
```

## Location Examples

* Chicago: `41.8781,-87.6298`
* Austin: `30.2672,-97.7431`
* New York: `40.7128,-74.0060`
* San Francisco: `37.7749,-122.4194`
* Miami: `25.7617,-80.1918`

## Running Locally

```bash
python app.py
```

The app will start on port 8080.

## Deploying as Databricks App

Create an `app.yaml` configuration file and use the Databricks CLI to deploy.

## Dependencies

* Flask
* requests
* psycopg2
* databricks-sdk
* ../lakebase.py (connection helper)

## NWS API

* No API key required
* User-Agent header is required by NWS terms
* Rate limits apply (unspecified, use reasonable request patterns)
* Documentation: https://www.weather.gov/documentation/services-web-api

## Future Enhancements

* [ ] Add geocoding support for city/state lookups
* [ ] Implement vector embeddings for narrative_text
* [ ] Add vector similarity search endpoint
* [ ] Support bulk location import from CSV/JSON
* [ ] Add time-range filtering for historical data
* [ ] Implement caching/deduplication strategies


## Vector Embeddings Pipeline

### Overview

The `ingest_weather_embeddings.py` script processes weather documents into vector embeddings for semantic search:

1. **Reads** unembedded documents from `weather_documents` table
2. **Chunks** `narrative_text` using sliding-window pattern:
   - Chunk size: 800 characters
   - Overlap: 100 characters
   - Ensures long NWS text (combined alerts + instructions) is split appropriately
3. **Embeds** each chunk using `sentence-transformers/all-MiniLM-L6-v2` (384-dim)
4. **Writes** to `weather_embeddings` table via psycopg2

### Setup

1. **Enable pgvector** and create embeddings table:
   ```bash
   # Run in your Lakebase Postgres database
   psql -h <host> -U <user> -d databricks_postgres -f sql_setup_embeddings_table.sql
   ```

2. **Install dependencies**:
   ```bash
   pip install sentence-transformers psycopg2-binary databricks-sdk pandas
   ```

### Running the Pipeline

```bash
python ingest_weather_embeddings.py
```

Or as a Databricks Job for scheduled execution.

### Configuration

Environment variables (optional):
- `WEATHER_TABLE_NAME`: Source table (default: `weather_documents`)
- `WEATHER_EMBEDDINGS_TABLE_NAME`: Destination table (default: `weather_embeddings`)
- `LAKEBASE_SECRET_SCOPE`: Secret scope (default: `database`)
- `LAKEBASE_SECRET_KEY`: Secret key (default: `lakebase-url`)

### Schema: weather_embeddings

```sql
CREATE TABLE weather_embeddings (
    id TEXT PRIMARY KEY,                -- chunk_id: "{document_id}:{chunk_index}"
    document_id TEXT NOT NULL,          -- FK to weather_documents.id
    chunk_index INTEGER NOT NULL,       -- 0-indexed chunk number
    chunk_text TEXT NOT NULL,           -- The chunked text
    embedding vector(384),              -- 384-dim vector from all-MiniLM-L6-v2
    model_name TEXT NOT NULL,           -- "sentence-transformers/all-MiniLM-L6-v2"
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(document_id, chunk_index)
);
```

### Indexes

- **HNSW index** on `embedding` for fast vector similarity search (cosine distance)
- **B-tree index** on `document_id` for FK lookups

### Vector Similarity Search

```sql
-- Find weather documents similar to a query embedding
SELECT 
    we.document_id,
    wd.headline,
    wd.event,
    wd.location,
    we.chunk_text,
    1 - (we.embedding <=> $1::vector) AS similarity
FROM weather_embeddings we
JOIN weather_documents wd ON we.document_id = wd.id
ORDER BY we.embedding <=> $1::vector
LIMIT 10;
```

### Performance Notes

- **Chunking**: Most NWS text is short (forecasts are 100-300 chars). Chunking primarily matters for combined alert descriptions + instructions (~500-2000 chars).
- **Batch processing**: Embeddings computed in batches of 100, inserted in batches of 50
- **Deduplication**: `ON CONFLICT (id) DO NOTHING` skips already-embedded chunks
- **Index**: HNSW index enables sub-linear search on large embedding tables

### Why Not Spark JDBC?

This pipeline uses **psycopg2** instead of `spark.write.jdbc` because:
- Lakebase OAuth tokens require dynamic generation (1-hour validity)
- Spark JDBC doesn't support pgvector's `::vector` casting in this environment
- psycopg2 with `execute_values` provides comparable throughput for this workload
- For distributed processing at scale, use Python's `concurrent.futures.ThreadPoolExecutor` instead of Spark
