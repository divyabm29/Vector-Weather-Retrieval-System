#!/usr/bin/env python3
"""
Test script for Weather Vector Search Endpoint

Usage: python test_search_endpoint.py --url http://localhost:8080
"""

import argparse
import json
import sys
import time

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)


def test_search(base_url, verbose=False):
    print("Testing /weather/search endpoint...\n")
    
    tests = [
        {"name": "Flood query", "payload": {"query": "flooding near rivers", "top_k": 5}},
        {"name": "Tornado query", "payload": {"query": "tornado warning", "top_k": 3}},
        {"name": "Empty query", "payload": {"query": "", "top_k": 5}, "expect_error": True},
        {"name": "Top-k clamp (0)", "payload": {"query": "weather", "top_k": 0}},
        {"name": "Top-k clamp (100)", "payload": {"query": "weather", "top_k": 100}},
    ]
    
    passed = 0
    for test in tests:
        print(f"Test: {test['name']}")
        try:
            r = requests.post(f"{base_url}/weather/search", json=test["payload"], timeout=10)
            data = r.json()
            
            expect_error = test.get("expect_error", False)
            if expect_error:
                if r.status_code >= 400:
                    print(f"  ✓ Expected error: {r.status_code}")
                    passed += 1
                else:
                    print(f"  ✗ Should have failed but got: {r.status_code}")
            else:
                if r.status_code == 200:
                    print(f"  ✓ Status: {r.status_code}, Results: {len(data.get('results', []))}")
                    if verbose and data.get("results"):
                        print(f"    Top result: {data['results'][0].get('headline', 'N/A')}")
                    passed += 1
                else:
                    print(f"  ✗ Status: {r.status_code}, Error: {data.get('error', 'Unknown')}")
        except Exception as e:
            print(f"  ✗ Error: {e}")
        print()
    
    print(f"Passed: {passed}/{len(tests)}")
    return passed == len(tests)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    
    success = test_search(args.url, args.verbose)
    sys.exit(0 if success else 1)
