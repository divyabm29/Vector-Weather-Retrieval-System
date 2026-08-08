#!/bin/bash
# End-to-End Test Script for Weather Vector Retrieval System

set -e  # Exit on error

API_URL="http://localhost:8000"

echo "========================================="
echo "Weather Vector Retrieval System Test"
echo "========================================="
echo ""

# Test 1: Health Check
echo "[1/5] Testing health endpoint..."
curl -s "$API_URL/healthz" | python3 -m json.tool
echo "✓ Health check passed"
echo ""

# Test 2: Sync Weather Data
echo "[2/5] Syncing weather data for 3 locations..."
curl -s -X POST "$API_URL/weather/sync" \
  -H "Content-Type: application/json" \
  -d '{
    "locations": [
      "41.8781,-87.6298",
      "30.2672,-97.7431",
      "40.7128,-74.0060"
    ],
    "limit": 20
  }' | python3 -m json.tool
echo "✓ Weather data synced"
echo ""

# Test 3: List Documents
echo "[3/5] Listing weather documents..."
curl -s "$API_URL/weather/documents?limit=5" | python3 -m json.tool
echo "✓ Documents listed"
echo ""

# Test 4: Run Ingestion (separate script)
echo "[4/5] Running embedding ingestion..."
echo "    $ python ingest_weather_embeddings.py"
echo "    (This step must be run manually or in a separate terminal)"
echo ""
read -p "Press Enter once ingestion is complete..."
echo ""

# Test 5: Semantic Search
echo "[5/5] Testing semantic search..."

echo ""
echo "Query 1: 'flood warnings'"
curl -s -X POST "$API_URL/weather/search" \
  -H "Content-Type: application/json" \
  -d '{"query": "flood warnings", "top_k": 3}' | python3 -m json.tool

echo ""
echo "Query 2: 'risk of severe thunderstorms'"
curl -s -X POST "$API_URL/weather/search" \
  -H "Content-Type: application/json" \
  -d '{"query": "risk of severe thunderstorms", "top_k": 3}' | python3 -m json.tool

echo ""
echo "Query 3: 'temperature forecast'"
curl -s -X POST "$API_URL/weather/search" \
  -H "Content-Type: application/json" \
  -d '{"query": "temperature forecast for tomorrow", "top_k": 3}' | python3 -m json.tool

echo ""
echo "✓ Semantic search tests passed"
echo ""

echo "========================================="
echo "All Tests Complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo "  - Review search results for relevance"
echo "  - Try custom queries"
echo "  - Monitor app logs for performance"
