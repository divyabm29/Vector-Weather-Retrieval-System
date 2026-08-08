"""
Weather Embeddings Ingestion Script

Reads unembedded weather documents from weather_documents table in Lakebase,
chunks narrative_text using sliding-window pattern, embeds each chunk using
sentence-transformers/all-MiniLM-L6-v2 (384-dim), and writes embeddings into
weather_embeddings table via psycopg2.

Run as a standalone Python script or as a Databricks notebook.
"""

import base64
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import execute_values
from sentence_transformers import SentenceTransformer

# Configuration
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 outputs 384-dimensional vectors

WEATHER_TABLE_NAME = os.environ.get("WEATHER_TABLE_NAME", "weather_documents")
EMBEDDINGS_TABLE_NAME = os.environ.get("WEATHER_EMBEDDINGS_TABLE_NAME", "weather_embeddings")
BATCH_SIZE = 100  # Batch size for embedding computation
INSERT_BATCH_SIZE = 50  # Batch size for database inserts

# Lakebase connection details
LAKEBASE_SECRET_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
LAKEBASE_SECRET_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")

w = WorkspaceClient()


def get_lakebase_url() -> str:
    """Fetch and decode the Lakebase connection URL from the Databricks secret scope."""
    secret = w.secrets.get_secret(scope=LAKEBASE_SECRET_SCOPE, key=LAKEBASE_SECRET_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


def get_lakebase_oauth_token() -> str:
    """Generate a Lakebase-scoped OAuth token (valid for 1 hour)."""
    # Parse the base URL to get the host for endpoint discovery
    parsed = urlparse(get_lakebase_url())
    host = parsed.hostname
    
    # Discover the endpoint name from the host
    for project in w.postgres.list_projects():
        for branch in w.postgres.list_branches(parent=project.name):
            for endpoint in w.postgres.list_endpoints(parent=branch.name):
                if endpoint.status and endpoint.status.hosts and endpoint.status.hosts.host == host:
                    # Generate OAuth token for this endpoint
                    creds = w.postgres.generate_database_credential(endpoint=endpoint.name)
                    return creds.token
    
    raise RuntimeError(f"No Lakebase endpoint found matching host: {host}")


def parse_lakebase_url():
    """Parse the Lakebase URL into connection parameters."""
    parsed = urlparse(get_lakebase_url())
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") or "databricks_postgres",
        "user": w.current_user.me().user_name,
    }


def get_connection():
    """Get a psycopg2 connection to Lakebase with OAuth token."""
    conn_params = parse_lakebase_url()
    oauth_token = get_lakebase_oauth_token()
    
    return psycopg2.connect(
        host=conn_params["host"],
        port=conn_params["port"],
        dbname=conn_params["dbname"],
        user=conn_params["user"],
        password=oauth_token,
        sslmode="require",
    )


def ensure_embeddings_table():
    """Create the weather_embeddings table with pgvector extension if it doesn't exist."""
    print(f"Ensuring {EMBEDDINGS_TABLE_NAME} table exists...")
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Enable pgvector extension
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        
        # Create embeddings table
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {EMBEDDINGS_TABLE_NAME} (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding vector({EMBEDDING_DIM}),
                model_name TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE(document_id, chunk_index)
            )
        """)
        
        # Create index on document_id for FK lookups
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{EMBEDDINGS_TABLE_NAME}_document_id
            ON {EMBEDDINGS_TABLE_NAME} (document_id)
        """)
        
        # Create HNSW index for vector similarity search (cosine distance)
        # This may take a while if there are already many rows
        try:
            cursor.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{EMBEDDINGS_TABLE_NAME}_embedding_hnsw
                ON {EMBEDDINGS_TABLE_NAME} USING hnsw (embedding vector_cosine_ops)
            """)
        except Exception as e:
            print(f"Warning: Could not create HNSW index (may already exist or require more data): {e}")
        
        conn.commit()
        print(f"✅ Table {EMBEDDINGS_TABLE_NAME} is ready")
    finally:
        cursor.close()
        conn.close()


def load_unembedded_documents():
    """Load weather documents that don't have embeddings yet."""
    print(f"Loading unembedded documents from {WEATHER_TABLE_NAME}...")
    
    conn = get_connection()
    try:
        query = f"""
            SELECT 
                wd.id,
                wd.location,
                wd.source_type,
                wd.headline,
                wd.event,
                wd.narrative_text,
                wd.issued_at
            FROM {WEATHER_TABLE_NAME} wd
            LEFT JOIN {EMBEDDINGS_TABLE_NAME} we ON wd.id = we.document_id
            WHERE we.document_id IS NULL
                AND wd.narrative_text IS NOT NULL
                AND TRIM(wd.narrative_text) != ''
        """
        
        df = pd.read_sql_query(query, conn)
        print(f"Loaded {len(df)} unembedded documents")
        return df
    finally:
        conn.close()


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks using sliding-window pattern."""
    if not text or len(text) <= chunk_size:
        return [text] if text else []
    
    chunks = []
    start = 0
    
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        
        if chunk:
            chunks.append(chunk)
        
        # Break if we've reached the end
        if end >= len(text):
            break
        
        # Move window forward (with overlap)
        start = end - overlap
    
    return chunks


def prepare_chunks_dataframe(documents_df: pd.DataFrame) -> pd.DataFrame:
    """Chunk narrative_text for each document and prepare for embedding."""
    print(f"Chunking narrative text from {len(documents_df)} documents...")
    
    rows = []
    for _, doc in documents_df.iterrows():
        doc_id = doc["id"]
        narrative = doc["narrative_text"] or ""
        
        # Skip empty narratives
        if not narrative.strip():
            continue
        
        # Chunk the narrative text
        chunks = chunk_text(narrative, CHUNK_SIZE, CHUNK_OVERLAP)
        
        for chunk_index, chunk_text in enumerate(chunks):
            # Generate unique ID for this chunk
            chunk_id = f"{doc_id}:{chunk_index}"
            
            rows.append({
                "id": chunk_id,
                "document_id": doc_id,
                "chunk_index": chunk_index,
                "chunk_text": chunk_text,
                "location": doc["location"],
                "source_type": doc["source_type"],
            })
    
    chunks_df = pd.DataFrame(rows)
    print(f"Created {len(chunks_df)} chunks from {len(documents_df)} documents")
    print(f"  Average chunks per document: {len(chunks_df) / len(documents_df):.1f}")
    
    return chunks_df


def compute_embeddings(chunks_df: pd.DataFrame) -> pd.DataFrame:
    """Compute embeddings for all chunks using sentence-transformers."""
    if len(chunks_df) == 0:
        print("No chunks to embed")
        return chunks_df
    
    print(f"Loading embedding model {EMBEDDING_MODEL_NAME}...")
    
    # Set up HuggingFace cache
    os.environ["HF_HOME"] = "/tmp/.cache/huggingface"
    os.environ["TRANSFORMERS_CACHE"] = "/tmp/.cache/huggingface"
    os.environ["HF_HUB_CACHE"] = "/tmp/.cache/huggingface"
    
    model = SentenceTransformer(EMBEDDING_MODEL_NAME, cache_folder="/tmp/.cache/huggingface")
    
    print(f"Computing embeddings for {len(chunks_df)} chunks (batch_size={BATCH_SIZE})...")
    
    # Compute embeddings in batches for memory efficiency
    all_embeddings = []
    for i in range(0, len(chunks_df), BATCH_SIZE):
        batch_texts = chunks_df["chunk_text"].iloc[i:i+BATCH_SIZE].tolist()
        batch_embeddings = model.encode(batch_texts, show_progress_bar=False)
        all_embeddings.extend(batch_embeddings)
        
        if (i + BATCH_SIZE) % 500 == 0:
            print(f"  Processed {min(i + BATCH_SIZE, len(chunks_df))}/{len(chunks_df)} chunks")
    
    # Convert embeddings to list format for psycopg2
    chunks_df["embedding"] = [emb.tolist() for emb in all_embeddings]
    chunks_df["model_name"] = EMBEDDING_MODEL_NAME
    chunks_df["created_at"] = datetime.now()
    
    print(f"✅ Computed {len(chunks_df)} embeddings (dimension: {EMBEDDING_DIM})")
    
    return chunks_df


def insert_embeddings(embeddings_df: pd.DataFrame):
    """Insert embeddings into Lakebase using psycopg2 execute_values for throughput."""
    if len(embeddings_df) == 0:
        print("No embeddings to insert")
        return 0
    
    print(f"Inserting {len(embeddings_df)} embeddings into {EMBEDDINGS_TABLE_NAME}...")
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Prepare data tuples
        insert_data = [
            (
                row["id"],
                row["document_id"],
                row["chunk_index"],
                row["chunk_text"],
                str(row["embedding"]),  # Convert list to string representation
                row["model_name"],
                row["created_at"],
            )
            for _, row in embeddings_df.iterrows()
        ]
        
        # Use execute_values for batch insert with ON CONFLICT
        inserted = 0
        for i in range(0, len(insert_data), INSERT_BATCH_SIZE):
            batch = insert_data[i:i+INSERT_BATCH_SIZE]
            
            execute_values(
                cursor,
                f"""
                INSERT INTO {EMBEDDINGS_TABLE_NAME} 
                    (id, document_id, chunk_index, chunk_text, embedding, model_name, created_at)
                VALUES %s
                ON CONFLICT (id) DO NOTHING
                """,
                batch,
                template="(%s, %s, %s, %s, %s::vector, %s, %s)",
            )
            
            inserted += cursor.rowcount
            
            if (i + INSERT_BATCH_SIZE) % 500 == 0:
                print(f"  Inserted {min(i + INSERT_BATCH_SIZE, len(insert_data))}/{len(insert_data)} embeddings")
        
        conn.commit()
        print(f"✅ Successfully inserted {inserted} new embeddings")
        print(f"   (Duplicates were skipped via ON CONFLICT DO NOTHING)")
        
        return inserted
    finally:
        cursor.close()
        conn.close()


def main():
    """Main ingestion pipeline."""
    print("="*80)
    print("Weather Embeddings Ingestion Pipeline")
    print("="*80)
    print(f"Configuration:")
    print(f"  Source table: {WEATHER_TABLE_NAME}")
    print(f"  Destination table: {EMBEDDINGS_TABLE_NAME}")
    print(f"  Embedding model: {EMBEDDING_MODEL_NAME}")
    print(f"  Embedding dimension: {EMBEDDING_DIM}")
    print(f"  Chunk size: {CHUNK_SIZE} chars")
    print(f"  Chunk overlap: {CHUNK_OVERLAP} chars")
    print("="*80)
    print()
    
    # Step 1: Ensure embeddings table exists
    ensure_embeddings_table()
    print()
    
    # Step 2: Load unembedded documents
    documents_df = load_unembedded_documents()
    if len(documents_df) == 0:
        print("✅ No new documents to embed. Exiting.")
        return
    print()
    
    # Step 3: Chunk documents
    chunks_df = prepare_chunks_dataframe(documents_df)
    if len(chunks_df) == 0:
        print("✅ No chunks created. Exiting.")
        return
    print()
    
    # Step 4: Compute embeddings
    embeddings_df = compute_embeddings(chunks_df)
    print()
    
    # Step 5: Insert embeddings
    inserted_count = insert_embeddings(embeddings_df)
    print()
    
    print("="*80)
    print(f"✅ Pipeline complete! Inserted {inserted_count} new embeddings.")
    print("="*80)


if __name__ == "__main__":
    main()
