# Basilica Integration Handoff Document

## Task Overview

Integrate Basilica sandboxes into the Bitsec subnet project (`/ephemeral/ubuntu/basilica-dir/sandbox/`) as an alternative backend to Docker, following the same pattern used in ridges.

## Implementation Status: COMPLETE

All code changes have been implemented:

### Files Created

1. **`/sandbox/validator/basilica_executor.py`** - Adapter class implementing the same interface as `AgentExecutor` using Basilica SDK
2. **`/sandbox/validator/executor_factory.py`** - Factory for selecting executor backend based on `SANDBOX_BACKEND` env var
3. **`/sandbox/test_basilica_integration.py`** - Integration tests for Basilica backend

### Files Modified

1. **`/sandbox/config.py`** - Added Basilica configuration settings:
   - `sandbox_backend` - "docker" (default) or "basilica"
   - `basilica_api_url`, `basilica_api_token` - SDK configuration
   - `basilica_image_prefix`, `basilica_cpu`, `basilica_memory`, `basilica_timeout` - Resource settings

2. **`/sandbox/validator/manager.py`** - Changed import to use `create_executor` factory

3. **`/sandbox/requirements.txt`** - Added SDK installation comment

## E2E Testing Status: BLOCKED

### What Works

1. **Executor Factory Test** - PASSES
   - Correctly selects Docker backend when `SANDBOX_BACKEND=docker`
   - Correctly selects Basilica backend when `SANDBOX_BACKEND=basilica`

2. **Sandbox Creation** - WORKS
   - SDK successfully creates sandboxes from warm pool

3. **Code Execution** - WORKS (after RBAC fix)
   - `sb.process.run()` successfully executes Python code in sandboxes

### What's Broken

1. **File Operations** - FAILS
   - `sb.files.write()` returns 500 errors
   - `sb.files.read()` likely also fails (not tested after run started working)

## Root Cause Analysis

### The Core Issue: NodePort vs K8s Service Proxy

The Basilica API has two methods to communicate with exec-agents in sandbox pods:

1. **NodePort** (default when `internal_endpoint` has 3 parts like `sandbox-xxx:9999:31425`)
   - Uses `EXEC_AGENT_HOST:NodePort` for direct HTTP access
   - Designed for production with SSH tunnels

2. **K8s Service Proxy** (fallback when `internal_endpoint` has 2 parts like `sandbox-xxx:9999`)
   - Uses K8s API server proxy: `/api/v1/namespaces/{ns}/services/{svc}:{port}/proxy/exec`
   - Works within cluster without external access

### Why NodePort Fails in k3d

The API pod runs in the k3d cluster with IP `10.42.x.x`. The k3d node's Docker IP is `172.18.0.3`. These are different networks and pods **cannot reach the Docker network** where NodePorts are exposed.

```
API Pod (10.42.0.x) --X--> k3d Node Docker IP (172.18.0.3:NodePort)
                           ^-- Not routable from pod network
```

### Why K8s Service Proxy Initially Failed

Missing RBAC permissions. Fixed by adding:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: basilica-api-service-proxy
  namespace: basilica-warm-pools
rules:
- apiGroups: [""]
  resources: ["services/proxy"]
  verbs: ["create", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: basilica-api-service-proxy
  namespace: basilica-warm-pools
subjects:
- kind: ServiceAccount
  name: basilica-api
  namespace: basilica-system
roleRef:
  kind: Role
  name: basilica-api-service-proxy
  apiGroup: rbac.authorization.k8s.io
```

### Current Blocker

After RBAC fix, code execution works BUT file operations still fail because:

1. The operator creates sandboxes with NodePort in `internal_endpoint`
2. The API tries NodePort first (which fails silently or with 500)
3. Only if NodePort is absent does it fall back to working service proxy

**The `internal_endpoint` format determines which path is used:**
- `sandbox-xxx:9999:31425` → NodePort (broken in k3d)
- `sandbox-xxx:9999` → Service Proxy (works after RBAC fix)

## Solutions (Not Yet Implemented)

### Option 1: Patch Sandboxes After Creation (Workaround)

Manually patch each sandbox to remove NodePort from `internal_endpoint`:

```bash
kubectl patch basilicasandbox <name> -n basilica-warm-pools \
  --type='merge' \
  -p '{"status":{"internalEndpoint":"<pod_name>:9999"}}' \
  --subresource=status
```

**Problem:** Must be done after each sandbox creation, warm pool constantly creates new ones.

### Option 2: Modify Operator (Requires Code Change)

Add environment variable to operator to disable NodePort in `internal_endpoint`:

```rust
// In sandbox_controller.rs
if std::env::var("DISABLE_NODEPORT_ENDPOINT").is_ok() {
    status.internal_endpoint = Some(format!("{}:{}", pod_name, EXEC_AGENT_PORT));
} else if let Some(np) = node_port {
    status.internal_endpoint = Some(format!("{}:{}:{}", pod_name, EXEC_AGENT_PORT, np));
}
```

### Option 3: Modify API (Requires Code Change)

Add fallback in API when NodePort fails:

```rust
// In client.rs exec_sandbox()
if let Some(np) = node_port {
    // Try NodePort first
    match nodeport_request().await {
        Ok(resp) => return Ok(resp),
        Err(e) => {
            tracing::warn!("NodePort failed, falling back to service proxy: {}", e);
            // Fall through to service proxy
        }
    }
}
// Service proxy path
```

### Option 4: Make k3d NodePorts Accessible (Infrastructure Change)

Configure k3d to expose NodePort range to host network, then set `EXEC_AGENT_HOST` appropriately. This is complex and may not work due to dynamic NodePort allocation.

## Recommended Next Steps

1. **For Quick Fix:** Implement Option 2 in the operator - add `DISABLE_NODEPORT_ENDPOINT` env var

2. **For Proper Fix:** Implement Option 3 in the API - add fallback when NodePort fails

3. **After Fix:** Re-run tests:
   ```bash
   cd /ephemeral/ubuntu/basilica-dir/sandbox
   source .venv/bin/activate
   export BASILICA_API_URL=http://localhost:9080
   export BASILICA_API_TOKEN=dev-token
   python test_basilica_integration.py --full
   ```

## Environment Setup Commands

```bash
# Start k3d cluster with Basilica
cd /ephemeral/ubuntu/basilica-dir/basilica-backend
sudo ./scripts/sandbox/local-container-e2e.sh setup

# Apply RBAC fix (required for service proxy)
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: basilica-api-service-proxy
  namespace: basilica-warm-pools
rules:
- apiGroups: [""]
  resources: ["services/proxy"]
  verbs: ["create", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: basilica-api-service-proxy
  namespace: basilica-warm-pools
subjects:
- kind: ServiceAccount
  name: basilica-api
  namespace: basilica-system
roleRef:
  kind: Role
  name: basilica-api-service-proxy
  apiGroup: rbac.authorization.k8s.io
EOF

# Install SDK (if not already done)
cd /ephemeral/ubuntu/basilica-dir/sandbox
source .venv/bin/activate
cd ../basilica/crates/basilica-sdk-python && maturin develop && cd -
```

## Key Files Reference

| File | Purpose |
|------|---------|
| `/sandbox/validator/basilica_executor.py` | Basilica backend implementation |
| `/sandbox/validator/executor_factory.py` | Backend selection factory |
| `/sandbox/test_basilica_integration.py` | Integration tests |
| `/sandbox/config.py` | Configuration with Basilica settings |
| `/basilica-backend/crates/basilica-api/src/k8s/client.rs:1956-2079` | `exec_sandbox()` - NodePort vs service proxy logic |
| `/basilica-backend/crates/basilica-operator/src/controllers/sandbox_controller.rs:309-316` | `internal_endpoint` construction |

## Test Results Summary

| Test | Status | Notes |
|------|--------|-------|
| Executor Factory | PASS | Backend selection works |
| Sandbox Creation | PASS | Claims from warm pool |
| Code Execution (`sb.process.run()`) | PASS | After RBAC fix |
| File Write (`sb.files.write()`) | FAIL | NodePort issue |
| File Read (`sb.files.read()`) | FAIL | NodePort issue |
| Agent Execution | FAIL | Depends on file write |
