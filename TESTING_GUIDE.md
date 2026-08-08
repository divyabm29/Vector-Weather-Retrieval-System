# Weather Vector Search Endpoint - Testing Guide

## ✅ Search Endpoint Status

**Implementation:** COMPLETE  
**Location:** `app.py:192-289`  
**Route:** `@app.route("/weather/search", methods=["POST"])`  

### Verified Components

✓ Route decorator with POST method  
✓ Request validation (query presence, top_k type)  
✓ Top-k clamping to [1, 20] range  
✓ Module-level embedding model loading  
✓ pgvector cosine similarity query  
✓ Empty table detection (404 response)  
✓ Exception handling and logging  

## Prerequisites

1. **Lakebase Database Setup** - Tables and indexes created
2. **Dependencies Installed** - flask, sentence-transformers, psycopg2-binary
3. **Weather Data Synced** - `POST /weather/sync`
4. **Embeddings Generated** - `python ingest_weather_embeddings.py`

## Testing Methods

### Method 1: Automated Test Script

```bash
# Start Flask app
python app.py

# Run tests
python test_search_endpoint.py --url http://localhost:8080 -v
```

### Method 2: Manual curl Testing

**Basic Search:**
```bash
curl -X POST http://localhost:8080/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "risk of flooding near rivers", "top_k": 5}'
```

**Expected Response:**
```json
{
  "query": "risk of flooding near rivers",
  "top_k": 5,
  "total_embeddings": 58,
  "results": [
    {
      "document_id": "abc123...",
      "location": "41.8781,-87.6298",
      "source_type": "alert",
      "headline": "Flood Warning until 8:00 PM CST",
      "similarity": 0.8234
    }
  ]
}
```

## Edge Case Tests

```bash
# Empty query (400)
curl -X POST http://localhost:8080/weather/search \
  -d '{"query": "", "top_k": 5}'

# Top-k clamping
curl -X POST http://localhost:8080/weather/search \
  -d '{"query": "weather", "top_k": 0}'    # Clamps to 1

curl -X POST http://localhost:8080/weather/search \
  -d '{"query": "weather", "top_k": 100}'  # Clamps to 20
```

## Performance Benchmarks

Expected latency:
- Query embedding: 5-10ms
- Vector search: 10-50ms
- **Total: 20-75ms** (typical)

## Troubleshooting

**"Connection refused"** → Start Flask app: `python app.py`  
**"No embeddings found"** → Run: `python ingest_weather_embeddings.py`  
**"Module not found"** → Install: `pip install sentence-transformers`  

## Complete Test Example

```python
import requests

response = requests.post(
    "http://localhost:8080/weather/search",
    json={"query": "tornado warning", "top_k": 5}
)

print(f"Status: {response.status_code}")
data = response.json()
print(f"Found {len(data['results'])} results")

for result in data['results'][:3]:
    print(f"  {result['similarity']:.4f} - {result['headline']}")
```

---
See [README_WEATHER.md](README_WEATHER.md) for technical details.
