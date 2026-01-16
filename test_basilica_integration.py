#!/usr/bin/env python3
"""
Basilica Integration Tests for Bitsec Sandbox

Setup:
    # Start k3d cluster
    cd ../basilica-backend && sudo ./scripts/sandbox/local-container-e2e.sh setup

    # Install dependencies with uv
    uv venv && source .venv/bin/activate
    uv pip install -r requirements.txt
    uv pip install maturin
    cd ../basilica/crates/basilica-sdk-python && maturin develop && cd -

    # Set environment
    export BASILICA_API_URL=http://localhost:9080 BASILICA_API_TOKEN=dev-token

Usage:
    python test_basilica_integration.py                    # Quick check
    python test_basilica_integration.py --full             # Full test with agent execution
    python test_basilica_integration.py --concurrent       # Run concurrent agents test
    python test_basilica_integration.py --concurrent -n 5  # Run 5 concurrent agents
"""

import os
import sys
import json
import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

import click
import basilica
from basilica import Sandbox

basilica.configure(
    api_url=os.environ.get("BASILICA_API_URL", "http://localhost:9080"),
    api_key=os.environ.get("BASILICA_API_TOKEN", ""),
)

ok = lambda m: print(f"  ✓ {m}")
fail = lambda m: print(f"  ✗ {m}")
section = lambda t: print(f"\n{'─'*60}\n  {t}\n{'─'*60}")

# Thread-safe progress tracking
progress_lock = threading.Lock()


@dataclass
class AgentResult:
    """Result from a single agent execution."""
    agent_id: int
    sandbox_id: Optional[str] = None
    status: str = "pending"
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    exit_code: Optional[int] = None
    output: str = ""
    report: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0

    @property
    def success(self) -> bool:
        return self.status == "completed" and self.exit_code == 0 and self.report.get("success", False)


def test_sdk_connection():
    """Test SDK connection and basic sandbox operations."""
    section("SDK Connection")
    try:
        with Sandbox.create(language="python", runtime="container") as sb:
            ok(f"Created: {sb.sandbox_id}")

            result = sb.process.run("print('Hello from Basilica')")
            assert result.exit_code == 0
            ok("Code execution")

            sb.files.write("/workspace/test.txt", "test content")
            content = sb.files.read("/workspace/test.txt")
            assert content == "test content"
            ok("File I/O")
        ok("Cleanup")
        return True
    except Exception as e:
        fail(str(e))
        traceback.print_exc()
        return False


def test_agent_execution():
    """Test agent-style execution (mimics BasilicaAgentExecutor)."""
    section("Agent Execution")

    agent_code = '''
import json
result = {"success": True, "message": "Agent executed successfully"}
with open("/workspace/report.json", "w") as f:
    json.dump(result, f)
print("Agent completed")
'''

    try:
        with Sandbox.create(language="python", runtime="container") as sb:
            ok(f"Sandbox: {sb.sandbox_id}")

            sb.files.write("/workspace/agent.py", agent_code)
            ok("Agent uploaded")

            result = sb.process.exec(["python3", "/workspace/agent.py"])
            ok(f"Execution (exit_code={result.exit_code})")

            report = json.loads(sb.files.read("/workspace/report.json"))
            assert report["success"] is True
            ok(f"Report: {report['message']}")
        return True
    except Exception as e:
        fail(str(e))
        traceback.print_exc()
        return False


def test_executor_factory():
    """Test that executor factory correctly selects backends."""
    section("Executor Factory")
    try:
        from config import settings
        from validator.executor_factory import get_executor_class

        # Test docker backend (default)
        original = settings.sandbox_backend
        settings.sandbox_backend = "docker"
        cls = get_executor_class()
        assert cls.__name__ == "AgentExecutor"
        ok("Docker backend selected")

        # Test basilica backend
        settings.sandbox_backend = "basilica"
        cls = get_executor_class()
        assert cls.__name__ == "BasilicaAgentExecutor"
        ok("Basilica backend selected")

        # Restore original
        settings.sandbox_backend = original
        return True
    except Exception as e:
        fail(str(e))
        traceback.print_exc()
        return False


def run_single_agent(agent_id: int, task_description: str) -> AgentResult:
    """Run a single agent in its own sandbox with a specific task."""
    result = AgentResult(agent_id=agent_id)
    result.start_time = time.time()
    
    # Agent code that performs a computational task and writes report
    agent_code = f'''
import json
import time
import random
import hashlib

# Task: {task_description}
agent_id = {agent_id}
start = time.time()

# Simulate some computational work
results = []
for i in range(10):
    # Calculate hashes (simulating agent work)
    data = f"agent-{{agent_id}}-iteration-{{i}}-{{time.time()}}"
    hash_val = hashlib.sha256(data.encode()).hexdigest()[:16]
    results.append(hash_val)
    time.sleep(0.1)  # Simulate processing time

# Compute summary
elapsed = time.time() - start
report = {{
    "success": True,
    "agent_id": agent_id,
    "task": "{task_description}",
    "results_count": len(results),
    "sample_results": results[:3],
    "elapsed_seconds": round(elapsed, 3),
    "message": f"Agent {{agent_id}} completed task successfully"
}}

with open("/workspace/report.json", "w") as f:
    json.dump(report, f, indent=2)

print(f"Agent {{agent_id}} completed: {{report['message']}}")
print(f"Processed {{len(results)}} items in {{elapsed:.2f}}s")
'''

    try:
        with progress_lock:
            print(f"  🚀 Agent {agent_id}: Starting sandbox...")
        
        with Sandbox.create(language="python", runtime="container") as sb:
            result.sandbox_id = sb.sandbox_id
            result.status = "running"
            
            with progress_lock:
                print(f"  📦 Agent {agent_id}: Sandbox {sb.sandbox_id[:8]}... created")
            
            # Upload and run agent code
            sb.files.write("/workspace/agent.py", agent_code)
            
            with progress_lock:
                print(f"  ⚙️  Agent {agent_id}: Executing task...")
            
            exec_result = sb.process.exec(["python3", "/workspace/agent.py"])
            result.exit_code = exec_result.exit_code
            result.output = exec_result.stdout or ""
            
            # Read report
            try:
                report_content = sb.files.read("/workspace/report.json")
                result.report = json.loads(report_content)
            except Exception as e:
                result.report = {"success": False, "error": str(e)}
            
            result.status = "completed"
            with progress_lock:
                status_icon = "✅" if result.success else "❌"
                print(f"  {status_icon} Agent {agent_id}: {result.report.get('message', 'Done')}")
                
    except Exception as e:
        result.status = "failed"
        result.error = str(e)
        with progress_lock:
            print(f"  ❌ Agent {agent_id}: Failed - {e}")
    
    result.end_time = time.time()
    return result


def test_concurrent_agents(num_agents: int = 3):
    """Run multiple agents concurrently and verify their results."""
    section(f"Concurrent Agents Test ({num_agents} agents)")
    
    tasks = [
        "Analyze security vulnerabilities",
        "Process data transformations",
        "Generate code refactoring suggestions",
        "Validate API responses",
        "Run compliance checks",
        "Perform load testing simulation",
        "Execute data migration tasks",
        "Generate test coverage report",
        "Analyze performance metrics",
        "Process batch operations",
    ]
    
    print(f"\n  Starting {num_agents} agents concurrently...\n")
    start_time = time.time()
    results: list[AgentResult] = []
    
    # Run agents concurrently
    with ThreadPoolExecutor(max_workers=num_agents) as executor:
        futures = {
            executor.submit(run_single_agent, i, tasks[i % len(tasks)]): i
            for i in range(num_agents)
        }
        
        for future in as_completed(futures):
            agent_id = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append(AgentResult(
                    agent_id=agent_id,
                    status="error",
                    error=str(e)
                ))
    
    total_time = time.time() - start_time
    
    # Sort results by agent_id for display
    results.sort(key=lambda r: r.agent_id)
    
    # Print summary
    print(f"\n{'─'*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'─'*60}")
    
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    
    print(f"\n  Total agents:     {num_agents}")
    print(f"  Successful:       {len(successful)} ✅")
    print(f"  Failed:           {len(failed)} {'❌' if failed else ''}")
    print(f"  Total time:       {total_time:.2f}s")
    print(f"  Avg time/agent:   {total_time/num_agents:.2f}s")
    
    if successful:
        avg_exec = sum(r.duration for r in successful) / len(successful)
        print(f"  Avg exec time:    {avg_exec:.2f}s")
    
    # Detailed results table
    print(f"\n{'─'*60}")
    print(f"  {'Agent':<8} {'Status':<12} {'Duration':<10} {'Sandbox':<14} {'Results':<8}")
    print(f"{'─'*60}")
    
    for r in results:
        status = "✅ OK" if r.success else "❌ FAIL"
        duration = f"{r.duration:.2f}s" if r.duration else "N/A"
        sandbox = r.sandbox_id[:12] + "..." if r.sandbox_id else "N/A"
        results_count = str(r.report.get("results_count", "-"))
        print(f"  {r.agent_id:<8} {status:<12} {duration:<10} {sandbox:<14} {results_count:<8}")
    
    # Verify results
    print(f"\n{'─'*60}")
    print(f"  VERIFICATION")
    print(f"{'─'*60}")
    
    all_passed = True
    
    # Check all agents completed
    if len(successful) == num_agents:
        ok(f"All {num_agents} agents completed successfully")
    else:
        fail(f"Only {len(successful)}/{num_agents} agents completed")
        all_passed = False
        for r in failed:
            print(f"      Agent {r.agent_id}: {r.error or r.report.get('error', 'Unknown error')}")
    
    # Check all agents produced valid reports
    valid_reports = [r for r in results if r.report.get("success") and r.report.get("results_count", 0) > 0]
    if len(valid_reports) == num_agents:
        ok(f"All agents produced valid reports with results")
    else:
        fail(f"Only {len(valid_reports)}/{num_agents} agents produced valid reports")
        all_passed = False
    
    # Check unique sandbox IDs (each agent got its own sandbox)
    sandbox_ids = [r.sandbox_id for r in results if r.sandbox_id]
    unique_sandboxes = len(set(sandbox_ids))
    if unique_sandboxes == num_agents:
        ok(f"Each agent ran in its own sandbox ({unique_sandboxes} unique sandboxes)")
    else:
        fail(f"Expected {num_agents} unique sandboxes, got {unique_sandboxes}")
        all_passed = False
    
    # Check reasonable execution times (includes sandbox creation ~30s + execution ~2s)
    reasonable_times = [r for r in successful if 1.0 <= r.duration <= 120.0]
    if len(reasonable_times) == len(successful):
        ok(f"All execution times within expected range (1-120s)")
    else:
        fail(f"Some execution times outside expected range")
        all_passed = False
    
    print(f"\n{'─'*60}")
    final_status = "✅ ALL TESTS PASSED" if all_passed else "❌ SOME TESTS FAILED"
    print(f"  {final_status}")
    print(f"{'─'*60}\n")
    
    return all_passed


@click.command()
@click.option("--full", is_flag=True, help="Run full test suite including agent execution")
@click.option("--concurrent", is_flag=True, help="Run concurrent agents test")
@click.option("-n", "--num-agents", default=3, help="Number of concurrent agents (default: 3)")
@click.option("--api-url", envvar="BASILICA_API_URL", default="http://localhost:9080", help="Basilica API URL")
@click.option("--api-token", envvar="BASILICA_API_TOKEN", default="", help="Basilica API token")
def main(full, concurrent, num_agents, api_url, api_token):
    """Basilica Integration Tests for Bitsec Sandbox."""
    
    # Reconfigure with CLI options
    if api_url or api_token:
        basilica.configure(
            api_url=api_url,
            api_key=api_token,
        )
    
    print("\n" + "=" * 60 + "\n  Basilica Integration Tests\n" + "=" * 60)
    print(f"  API URL: {api_url}")
    print(f"  Token:   {'*' * 8 if api_token else 'NOT SET'}")
    
    if not api_token:
        fail("BASILICA_API_TOKEN not set (use --api-token or env var)")
        sys.exit(1)

    # Run concurrent agents test if requested
    if concurrent:
        print(f"\n  Mode: Concurrent Agents Test ({num_agents} agents)")
        success = test_concurrent_agents(num_agents)
        sys.exit(0 if success else 1)

    # Otherwise run standard tests
    tests = [
        ("Executor Factory", test_executor_factory),
        ("SDK Connection", test_sdk_connection),
    ]
    if full:
        tests.append(("Agent Execution", test_agent_execution))

    results = [(n, f()) for n, f in tests]

    print("\n" + "=" * 60)
    for n, p in results:
        print(f"  {'✓' if p else '✗'} {n}")
    print("=" * 60)

    passed = all(p for _, p in results)
    print(f"  {'✓ All passed' if passed else '✗ Some failed'}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
