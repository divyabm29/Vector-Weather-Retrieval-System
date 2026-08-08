-- Setup script for weather_embeddings table in Lakebase
-- Run this SQL in your Lakebase Postgres database before running ingest_weather_embeddings.py

-- Enable pgvector extension (required for vector columns)
CREATE EXTENSION IF NOT EXISTS vector;

-- Create weather_embeddings table with vector column
CREATE TABLE IF NOT EXISTS weather_embeddings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(384),  -- sentence-transformers/all-MiniLM-L6-v2 outputs 384-dim vectors
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(document_id, chunk_index)
);

-- Create index on document_id for FK lookups
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id
ON weather_embeddings (document_id);

-- Create index on source_type for filtering
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_model_name
ON weather_embeddings (model_name);

-- Create HNSW index for vector similarity search (cosine distance)
-- This index enables fast nearest-neighbor queries for retrieval
-- Note: HNSW index creation can be slow on large tables
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding_hnsw
ON weather_embeddings USING hnsw (embedding vector_cosine_ops);

-- Alternative: IVFFlat index (faster to build, slightly less accurate)
-- Uncomment if you prefer IVFFlat over HNSW:
-- CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding_ivfflat
-- ON weather_embeddings USING ivfflat (embedding vector_cosine_ops)
-- WITH (lists = 100);

-- Verify table structure
\d weather_embeddings

-- Sample query to test vector similarity search (after embeddings are inserted)
-- SELECT 
--     document_id, 
--     chunk_text, 
--     1 - (embedding <=> '[0.1, 0.2, ...]'::vector) AS similarity
-- FROM weather_embeddings
-- ORDER BY embedding <=> '[0.1, 0.2, ...]'::vector
-- LIMIT 5;
