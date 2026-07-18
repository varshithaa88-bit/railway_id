#!/usr/bin/env python3
"""
Comprehensive endpoint test script for the ID card generation system.
Tests all major endpoints for students and employees.
"""

import sys
import requests
import json
from pathlib import Path

# Configuration
BASE_URL = "http://localhost:5000"
API_BASE = f"{BASE_URL}/api"

def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def test_endpoint(name, url, method="GET", data=None, expected_status=200):
    """Test a single endpoint."""
    print(f"\n[{name}]")
    print(f"  URL: {url}")
    print(f"  Method: {method}")
    
    try:
        if method == "GET":
            response = requests.get(url, timeout=10)
        elif method == "POST":
            response = requests.post(url, json=data, timeout=10)
        else:
            print(f"  ❌ Unsupported method: {method}")
            return False
        
        print(f"  Status: {response.status_code}")
        
        if response.status_code == expected_status:
            print(f"  ✅ PASS")
            try:
                if response.headers.get('content-type', '').startswith('application/json'):
                    print(f"  Response: {json.dumps(response.json(), indent=2)[:200]}...")
                else:
                    print(f"  Content-Type: {response.headers.get('content-type')}")
                    print(f"  Content-Length: {len(response.content)} bytes")
            except:
                pass
            return True
        else:
            print(f"  ❌ FAIL - Expected {expected_status}, got {response.status_code}")
            try:
                print(f"  Response: {response.text[:500]}")
            except:
                pass
            return False
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        return False

def main():
    print_section("ID Card Generation System - Endpoint Tests")
    print(f"Base URL: {BASE_URL}")
    
    results = {}
    
    # Test 1: Health check
    print_section("1. Health Check")
    results['health'] = test_endpoint(
        "Health Check",
        f"{BASE_URL}/",
        "GET"
    )
    
    # Test 2: Templates list
    print_section("2. Templates")
    results['templates'] = test_endpoint(
        "Get Templates",
        f"{API_BASE}/templates",
        "GET"
    )
    
    # Test 3: Student endpoints (without data)
    print_section("3. Student Endpoints (No Data)")
    results['student_preview_all'] = test_endpoint(
        "Student Preview All",
        f"{API_BASE}/preview/all?template=redeemer",
        "GET",
        expected_status=400  # No students loaded
    )
    
    results['student_download_all'] = test_endpoint(
        "Student Download All",
        f"{API_BASE}/download/all?template=redeemer",
        "GET",
        expected_status=400  # No students loaded
    )
    
    results['student_preview_one'] = test_endpoint(
        "Student Preview One",
        f"{API_BASE}/preview/student?template=redeemer&class=10A&name=Test",
        "GET",
        expected_status=400  # No students loaded
    )
    
    results['student_download_one'] = test_endpoint(
        "Student Download One",
        f"{API_BASE}/download/student?template=redeemer&class=10A&name=Test",
        "GET",
        expected_status=400  # No students loaded
    )
    
    results['student_download_one_png'] = test_endpoint(
        "Student Download One (PNG)",
        f"{API_BASE}/download/student?template=redeemer&class=10A&name=Test&format=png",
        "GET",
        expected_status=400  # No students loaded
    )
    
    results['student_zip'] = test_endpoint(
        "Student ZIP Download",
        f"{API_BASE}/download/zip?template=redeemer",
        "GET",
        expected_status=400  # No students loaded
    )
    
    results['student_zip_job'] = test_endpoint(
        "Student ZIP Job Start",
        f"{API_BASE}/jobs/start-zip?template=redeemer",
        "POST",
        expected_status=400  # No students loaded
    )
    
    # Test 4: Employee endpoints (without data)
    print_section("4. Employee Endpoints (No Data)")
    results['emp_preview_all'] = test_endpoint(
        "Employee Preview All",
        f"{API_BASE}/employees/preview/all?template=redeemer_emp",
        "GET",
        expected_status=400  # No employees loaded
    )
    
    results['emp_download_all'] = test_endpoint(
        "Employee Download All",
        f"{API_BASE}/employees/download/all?template=redeemer_emp",
        "GET",
        expected_status=400  # No employees loaded
    )
    
    results['emp_preview_one'] = test_endpoint(
        "Employee Preview One",
        f"{API_BASE}/employees/preview/student?template=redeemer_emp&class=Teacher&name=Test",
        "GET",
        expected_status=400  # No employees loaded
    )
    
    results['emp_download_one'] = test_endpoint(
        "Employee Download One",
        f"{API_BASE}/employees/download/student?template=redeemer_emp&class=Teacher&name=Test",
        "GET",
        expected_status=400  # No employees loaded
    )
    
    results['emp_download_one_png'] = test_endpoint(
        "Employee Download One (PNG)",
        f"{API_BASE}/employees/download/student?template=redeemer_emp&class=Teacher&name=Test&format=png",
        "GET",
        expected_status=400  # No employees loaded
    )
    
    results['emp_zip'] = test_endpoint(
        "Employee ZIP Download",
        f"{API_BASE}/employees/download/zip?template=redeemer_emp",
        "GET",
        expected_status=400  # No employees loaded
    )
    
    results['emp_zip_job'] = test_endpoint(
        "Employee ZIP Job Start",
        f"{API_BASE}/employees/jobs/start-zip?template=redeemer_emp",
        "POST",
        expected_status=400  # No employees loaded
    )
    
    results['emp_zip_job_png'] = test_endpoint(
        "Employee ZIP Job Start (PNG)",
        f"{API_BASE}/employees/jobs/start-zip?template=redeemer_emp&format=jpeg",
        "POST",
        expected_status=400  # No employees loaded
    )
    
    # Test 5: Job endpoints
    print_section("5. Job Management Endpoints")
    results['job_progress'] = test_endpoint(
        "Job Progress",
        f"{API_BASE}/jobs/test123/progress",
        "GET",
        expected_status=404  # Job doesn't exist
    )
    
    # Summary
    print_section("Test Summary")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    print(f"\nTotal Tests: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")
    print(f"Success Rate: {passed/total*100:.1f}%")
    
    print("\nDetailed Results:")
    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")
    
    return passed == total

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTests interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
