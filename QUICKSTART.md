# Quick Start Guide: Weather Vector Retrieval System

Get up and running with the weather vector retrieval system in 5 steps.

## Prerequisites

- Databricks workspace with Lakebase (Postgres) enabled
- Lakebase connection URL stored in secret scope `database` with key `lakebase-url`
- Python 3.10+ environment

## Step 1: Set Up Lakebase Tables

Run the SQL setup script in your Lakebase Postgres database:

```bash
# Connect to your Lakebase instance
psql -h <your-lakebase-host> -U <username> -d databricks_postgres

# Run the setup script
\i sql_setup_embeddings_table.sql
```

This creates:
- `weather_documents` table (if running app.py will auto-create)
- `weather_embeddings` table with pgvector extension
- HNSW index for vector similarity search

## Step 2: Sync Weather Data

Start the Flask app and sync some weather data:

```bash
# Run the Flask app
python app.py

# In another terminal, sync weather for some locations
curl -X POST http://localhost:8080/weather/sync \
  -H "Content-Type: application/json" \
  -d '{
    "locations": [
      "41.8781,-87.6298",
      "30.2672,-97.7431",
      "40.7128,-74.0060"
    ],
    "limit": 50
  }'
```

Response:
```json
{
  "synced": 42,
  "locations": ["41.8781,-87.6298", "30.2672,-97.7431", "40.7128,-74.0060"]
}
```

## Step 3: Generate Vector Embeddings

Run the ingestion script to compute embeddings:

```bash
python ingest_weather_embeddings.py
```

Output:
```
================================================================================
Weather Embeddings Ingestion Pipeline
================================================================================
Configuration:
  Source table: weather_documents
  Destination table: weather_embeddings
  Embedding model: sentence-transformers/all-MiniLM-L6-v2
  Embedding dimension: 384
  Chunk size: 800 chars
  Chunk overlap: 100 chars
================================================================================

Ensuring weather_embeddings table exists...
✅ Table weather_embeddings is ready

Loading unembedded documents from weather_documents...
Loaded 42 unembedded documents

Chunking narrative text from 42 documents...
Created 58 chunks from 42 documents
  Average chunks per document: 1.4

Loading embedding model sentence-transformers/all-MiniLM-L6-v2...
Computing embeddings for 58 chunks (batch_size=100)...
✅ Computed 58 embeddings (dimension: 384)

Inserting 58 embeddings into weather_embeddings...
✅ Successfully inserted 58 new embeddings
   (Duplicates were skipped via ON CONFLICT DO NOTHING)

================================================================================
✅ Pipeline complete! Inserted 58 new embeddings.
================================================================================
```

## Step 4: Query Vector Embeddings

### Python Example

```python
import psycopg2
from sentence_transformers import SentenceTransformer
import sys
sys.path.insert(0, '..')
import lakebase

# Load the embedding model
model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')

# Your search query
query = "What are the flood warnings in Chicago?"

# Generate query embedding
query_embedding = model.encode(query).tolist()

# Search for similar weather documents
with lakebase.get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                we.document_id,
                wd.headline,
                wd.event,
                wd.location,
                wd.source_type,
                we.chunk_text,
                1 - (we.embedding <=> %s::vector) AS similarity
            FROM weather_embeddings we
            JOIN weather_documents wd ON we.document_id = wd.id
            ORDER BY we.embedding <=> %s::vector
            LIMIT 5
        """, (str(query_embedding), str(query_embedding)))
        
        results = cur.fetchall()
        
        for row in results:
            print(f"\nLocation: {row['location']}")
            print(f"Event: {row['event']}")
            print(f"Headline: {row['headline']}")
            print(f"Similarity: {row['similarity']:.4f}")
            print(f"Text: {row['chunk_text'][:200]}...")
```

### SQL Example

```sql
-- First, get your query embedding from Python/app code
-- Then run this query:

SELECT 
    we.document_id,
    wd.headline,
    wd.event,
    wd.location,
    wd.source_type,
    we.chunk_text,
    1 - (we.embedding <=> '[0.1, 0.2, ...]'::vector) AS similarity
FROM weather_embeddings we
JOIN weather_documents wd ON we.document_id = wd.id
ORDER BY we.embedding <=> '[0.1, 0.2, ...]'::vector
LIMIT 5;
```

## Step 5: Verify Results

Check the synced documents:

```bash
curl http://localhost:8080/weather/documents?limit=10
```

Check embeddings in database:

```sql
-- Count embeddings by source type
SELECT 
    wd.source_type,
    COUNT(*) as embedding_count,
    COUNT(DISTINCT wd.id) as document_count
FROM weather_embeddings we
JOIN weather_documents wd ON we.document_id = wd.id
GROUP BY wd.source_type;
```

## Common Location Coordinates

For testing, here are some major US city coordinates:

| City | Coordinates |
|------|-------------|
| Chicago, IL | `41.8781,-87.6298` |
| Austin, TX | `30.2672,-97.7431` |
| New York, NY | `40.7128,-74.0060` |
| San Francisco, CA | `37.7749,-122.4194` |
| Miami, FL | `25.7617,-80.1918` |
| Seattle, WA | `47.6062,-122.3321` |
| Denver, CO | `39.7392,-104.9903` |
| Boston, MA | `42.3601,-71.0589` |

## Next Steps

- **Schedule ingestion**: Set up a Databricks Job to run `ingest_weather_embeddings.py` on a schedule
- **Add retrieval endpoint**: Extend `app.py` with a `/weather/search` endpoint that accepts natural language queries
- **Integrate with RAG**: Use the embeddings for context retrieval in a RAG pipeline
- **Monitor data freshness**: Track `synced_at` and `created_at` timestamps to ensure data is current

## Troubleshooting

### "No Lakebase endpoint found matching host"

Make sure your Lakebase connection URL is correctly stored in the secret scope:

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
secret = w.secrets.get_secret(scope="database", key="lakebase-url")
print(base64.b64decode(secret.value).decode("utf-8"))
```

### "relation 'weather_embeddings' does not exist"

Run the SQL setup script first:
```bash
psql -h <host> -U <user> -d databricks_postgres -f sql_setup_embeddings_table.sql
```

### "No unembedded documents found"

Sync some weather data first using `POST /weather/sync`.

## Support

For issues or questions, check the main [README.md](README.md) for detailed documentation.
