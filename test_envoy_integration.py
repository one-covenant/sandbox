#!/usr/bin/env python3
"""
Basilica Integration Tests - Direct Envoy Gateway Routing

This test validates production-like routing where WebSocket connections
go directly through Envoy Gateway to the sandbox exec-agents.

Flow:
  1. Create sandbox via API (http://localhost:9080)
  2. Sync HTTPRoute for the sandbox (via kubectl)
  3. Connect WebSocket through Envoy (http://localhost:31900)
  4. Execute commands and verify results
  5. Cleanup

Setup:
    cd ../basilica-backend && sudo ./scripts/sandbox/local-container-e2e.sh setup
    
    # Ensure routes are synced
    sudo ./scripts/sandbox/sync-sandbox-routes.sh

Usage:
    python test_envoy_integration.py                    # Quick test
    python test_envoy_integration.py --full             # Full test
    python test_envoy_integration.py --concurrent -n 3  # Concurrent test
"""

import os
import sys
import json
import time
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import click
import requests
import websocket

# Configuration
API_URL = os.environ.get("BASILICA_API_URL", "http://localhost:9080")
ENVOY_URL = os.environ.get("ENVOY_URL", "http://localhost:31900")
API_TOKEN = os.environ.get("BASILICA_API_TOKEN", "dev-token")

ok = lambda m: print(f"  ✓ {m}")
fail = lambda m: print(f"  ✗ {m}")
section = lambda t: print(f"\n{'─'*60}\n  {t}\n{'─'*60}")

progress_lock = threading.Lock()


def create_sandbox(language: str = "python") -> dict:
    """Create a sandbox via API."""
    resp = requests.post(
        f"{API_URL}/api/v1/sandboxes",
        json={"language": language},
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    # Normalize key names (API uses camelCase)
    return {
        "sandbox_id": data.get("sandboxId") or data.get("sandbox_id"),
        "state": data.get("state"),
        "websocket_url": data.get("websocketUrl") or data.get("websocket_url"),
    }


def delete_sandbox(sandbox_id: str):
    """Delete a sandbox via API."""
    try:
        requests.delete(
            f"{API_URL}/api/v1/sandboxes/{sandbox_id}",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=30,
        )
    except Exception as e:
        print(f"    Warning: Failed to delete sandbox {sandbox_id}: {e}")


def create_httproute(sandbox_id: str, namespace: str = "basilica-warm-pools"):
    """Create an HTTPRoute alias for the sandbox to enable Envoy routing.
    
    For warm pool sandboxes, the sandbox_id is a CRD name (e.g., python-pool-warm-xxx)
    but services use short UUIDs. This function creates an alias route.
    """
    # First, get the short sandbox ID from the CRD status
    try:
        result = subprocess.run(
            ["sudo", "kubectl", "get", "basilicasandbox", sandbox_id, "-n", namespace,
             "-o", "jsonpath={.status.sandboxId}"],
            capture_output=True, text=True, timeout=10,
        )
        full_uuid = result.stdout.strip()
        short_id = full_uuid.split("-")[0] if full_uuid else None
    except Exception:
        short_id = None
    
    if short_id:
        # Use alias script to create route from CRD name to actual service
        alias_script = os.path.join(
            os.path.dirname(__file__),
            "../basilica-backend/scripts/sandbox/create-sandbox-route-alias.sh"
        )
        try:
            subprocess.run(
                ["sudo", "bash", alias_script, sandbox_id, short_id, namespace],
                check=True, capture_output=True, timeout=10,
            )
        except subprocess.CalledProcessError as e:
            print(f"    Warning: Failed to create HTTPRoute alias: {e.stderr.decode()}")
    else:
        # Fallback to regular route (for non-warm-pool sandboxes)
        script_path = os.path.join(
            os.path.dirname(__file__),
            "../basilica-backend/scripts/sandbox/create-sandbox-route.sh"
        )
        try:
            subprocess.run(
                ["sudo", "bash", script_path, sandbox_id, namespace],
                check=True, capture_output=True, timeout=10,
            )
        except subprocess.CalledProcessError as e:
            print(f"    Warning: Failed to create HTTPRoute: {e.stderr.decode()}")


def wait_for_route(sandbox_id: str, timeout: int = 30):
    """Wait for HTTPRoute (or alias) to be accepted by Envoy."""
    start = time.time()
    # Check both regular and alias routes
    route_names = [f"sandbox-{sandbox_id}", f"sandbox-alias-{sandbox_id}"]
    
    while time.time() - start < timeout:
        for route_name in route_names:
            try:
                result = subprocess.run(
                    ["sudo", "kubectl", "get", "httproute", route_name,
                     "-n", "basilica-system", "-o", 
                     "jsonpath={.status.parents[0].conditions[?(@.type=='ResolvedRefs')].status}"],
                    capture_output=True, text=True, timeout=5,
                )
                # Check that ResolvedRefs is True (meaning backend service was found)
                if result.stdout.strip() == "True":
                    return True
            except Exception:
                pass
        time.sleep(1)
    return False


def test_websocket_via_envoy(sandbox_id: str) -> tuple[bool, str]:
    """Test WebSocket connection through Envoy Gateway."""
    ws_url = f"ws://localhost:31900/api/v1/sandboxes/{sandbox_id}/ws?api_key={API_TOKEN}"
    
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        
        # Send a simple command
        ws.send(json.dumps({
            "type": "exec",
            "command": ["echo", "hello from envoy"]
        }))
        
        # Wait for response
        output = ""
        start = time.time()
        while time.time() - start < 10:
            try:
                msg = ws.recv()
                data = json.loads(msg)
                # exec-agent uses 'stdout' type, not 'output'
                if data.get("type") in ("output", "stdout"):
                    output += data.get("data", "")
                elif data.get("type") == "exit":
                    break
            except websocket.WebSocketTimeoutException:
                break
        
        ws.close()
        
        if "hello from envoy" in output:
            return True, output.strip()
        return False, f"Unexpected output: {output}"
        
    except Exception as e:
        return False, str(e)


def test_file_ops_via_envoy(sandbox_id: str) -> tuple[bool, str]:
    """Test file operations through Envoy Gateway using shell commands."""
    ws_url = f"ws://localhost:31900/api/v1/sandboxes/{sandbox_id}/ws?api_key={API_TOKEN}"
    
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        
        # Write a file using echo
        ws.send(json.dumps({
            "type": "exec",
            "command": ["bash", "-c", "echo 'Written via Envoy Gateway' > /workspace/envoy_test.txt"]
        }))
        
        # Wait for write to complete
        for _ in range(5):
            msg = ws.recv()
            data = json.loads(msg)
            if data.get("type") == "exit":
                break
        
        # Read it back using cat
        ws.send(json.dumps({
            "type": "exec",
            "command": ["cat", "/workspace/envoy_test.txt"]
        }))
        
        content = ""
        for _ in range(10):
            try:
                msg = ws.recv()
                data = json.loads(msg)
                if data.get("type") in ("output", "stdout"):
                    content += data.get("data", "")
                elif data.get("type") == "exit":
                    break
            except websocket.WebSocketTimeoutException:
                break
        
        ws.close()
        
        if "Written via Envoy Gateway" in content:
            return True, content.strip()
        return False, f"Content mismatch: {content}"
        
    except Exception as e:
        return False, str(e)


@dataclass
class TestResult:
    """Result from a test."""
    test_name: str
    sandbox_id: Optional[str] = None
    success: bool = False
    message: str = ""
    duration: float = 0.0


def run_basic_test() -> TestResult:
    """Run basic WebSocket test through Envoy."""
    result = TestResult(test_name="Basic WebSocket via Envoy")
    start = time.time()
    
    try:
        # 1. Create sandbox
        sandbox = create_sandbox("python")
        result.sandbox_id = sandbox.get("sandbox_id")
        ok(f"Created sandbox: {result.sandbox_id}")
        
        # 2. Wait for sandbox to be ready
        time.sleep(5)  # Give warm pool sandbox time to initialize
        
        # 3. Create HTTPRoute
        create_httproute(result.sandbox_id)
        ok("Created HTTPRoute")
        
        # 4. Wait for route to be accepted
        if not wait_for_route(result.sandbox_id):
            fail("HTTPRoute not accepted in time")
            result.message = "HTTPRoute not accepted"
            return result
        ok("HTTPRoute accepted by Envoy")
        
        # 5. Test WebSocket through Envoy
        success, output = test_websocket_via_envoy(result.sandbox_id)
        if success:
            ok(f"WebSocket via Envoy: {output}")
            result.success = True
            result.message = output
        else:
            fail(f"WebSocket failed: {output}")
            result.message = output
            
    except Exception as e:
        fail(str(e))
        result.message = str(e)
    finally:
        if result.sandbox_id:
            delete_sandbox(result.sandbox_id)
        result.duration = time.time() - start
    
    return result


def run_full_test() -> TestResult:
    """Run full test including file operations."""
    result = TestResult(test_name="Full Test via Envoy")
    start = time.time()
    
    try:
        # Create sandbox
        sandbox = create_sandbox("python")
        result.sandbox_id = sandbox.get("sandbox_id")
        ok(f"Created sandbox: {result.sandbox_id}")
        
        time.sleep(5)
        
        # Create HTTPRoute
        create_httproute(result.sandbox_id)
        ok("Created HTTPRoute")
        
        if not wait_for_route(result.sandbox_id):
            fail("HTTPRoute not accepted in time")
            result.message = "HTTPRoute not accepted"
            return result
        ok("HTTPRoute accepted by Envoy")
        
        # Test WebSocket
        success, output = test_websocket_via_envoy(result.sandbox_id)
        if success:
            ok(f"WebSocket exec: {output}")
        else:
            fail(f"WebSocket failed: {output}")
            result.message = output
            return result
        
        # Test file operations
        success, content = test_file_ops_via_envoy(result.sandbox_id)
        if success:
            ok(f"File operations: {content}")
            result.success = True
            result.message = "All tests passed"
        else:
            fail(f"File ops failed: {content}")
            result.message = content
            
    except Exception as e:
        fail(str(e))
        result.message = str(e)
    finally:
        if result.sandbox_id:
            delete_sandbox(result.sandbox_id)
        result.duration = time.time() - start
    
    return result


def run_concurrent_test(agent_id: int) -> TestResult:
    """Run a single agent test for concurrent testing."""
    result = TestResult(test_name=f"Agent {agent_id}")
    start = time.time()
    
    try:
        with progress_lock:
            print(f"  🚀 Agent {agent_id}: Creating sandbox...")
        
        sandbox = create_sandbox("python")
        result.sandbox_id = sandbox.get("sandbox_id")
        
        with progress_lock:
            print(f"  📦 Agent {agent_id}: {result.sandbox_id[:12]}...")
        
        time.sleep(3)
        
        # Create HTTPRoute
        create_httproute(result.sandbox_id)
        
        if not wait_for_route(result.sandbox_id, timeout=20):
            with progress_lock:
                print(f"  ⚠️  Agent {agent_id}: Route not ready, continuing anyway...")
        
        # Test WebSocket through Envoy
        success, output = test_websocket_via_envoy(result.sandbox_id)
        
        with progress_lock:
            if success:
                print(f"  ✅ Agent {agent_id}: Success - {output[:30]}")
                result.success = True
                result.message = output
            else:
                print(f"  ❌ Agent {agent_id}: Failed - {output[:50]}")
                result.message = output
                
    except Exception as e:
        with progress_lock:
            print(f"  ❌ Agent {agent_id}: Error - {e}")
        result.message = str(e)
    finally:
        if result.sandbox_id:
            delete_sandbox(result.sandbox_id)
        result.duration = time.time() - start
    
    return result


@click.command()
@click.option("--full", is_flag=True, help="Run full test including file operations")
@click.option("--concurrent", is_flag=True, help="Run concurrent agents test")
@click.option("-n", "--num-agents", default=3, help="Number of concurrent agents")
def main(full, concurrent, num_agents):
    """Basilica Integration Tests - Envoy Gateway Routing."""
    
    print("\n" + "=" * 60)
    print("  Basilica Integration Tests - Envoy Gateway Routing")
    print("=" * 60)
    print(f"  API URL:   {API_URL}")
    print(f"  Envoy URL: {ENVOY_URL}")
    print(f"  Token:     {'*' * 8 if API_TOKEN else 'NOT SET'}")
    
    if not API_TOKEN:
        fail("BASILICA_API_TOKEN not set")
        sys.exit(1)
    
    if concurrent:
        section(f"Concurrent Test ({num_agents} agents)")
        print(f"\n  Starting {num_agents} agents...\n")
        
        start = time.time()
        results = []
        
        with ThreadPoolExecutor(max_workers=num_agents) as executor:
            futures = {executor.submit(run_concurrent_test, i): i for i in range(num_agents)}
            for future in as_completed(futures):
                results.append(future.result())
        
        total_time = time.time() - start
        
        # Summary
        print(f"\n{'─'*60}")
        print("  SUMMARY")
        print(f"{'─'*60}")
        
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        
        print(f"\n  Total:      {num_agents}")
        print(f"  Successful: {len(successful)} ✅")
        print(f"  Failed:     {len(failed)} {'❌' if failed else ''}")
        print(f"  Time:       {total_time:.2f}s")
        
        # Verify unique sandboxes
        sandbox_ids = [r.sandbox_id for r in results if r.sandbox_id]
        unique = len(set(sandbox_ids))
        
        print(f"\n  Unique sandboxes: {unique}/{num_agents}")
        
        if unique == num_agents:
            ok("Each agent got unique sandbox")
        else:
            fail(f"Expected {num_agents} unique sandboxes, got {unique}")
        
        all_passed = len(successful) == num_agents
        print(f"\n{'─'*60}")
        print(f"  {'✅ ALL PASSED' if all_passed else '❌ SOME FAILED'}")
        print(f"{'─'*60}\n")
        
        sys.exit(0 if all_passed else 1)
    
    if full:
        section("Full Test via Envoy")
        result = run_full_test()
    else:
        section("Basic Test via Envoy")
        result = run_basic_test()
    
    print(f"\n{'─'*60}")
    print(f"  {'✅ PASSED' if result.success else '❌ FAILED'}: {result.test_name}")
    print(f"  Duration: {result.duration:.2f}s")
    print(f"{'─'*60}\n")
    
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()

