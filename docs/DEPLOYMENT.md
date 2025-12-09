# Deployment Guide

This guide covers deploying the EPS Account Intelligence Agent to Databricks Model Serving.

## Prerequisites

Before deploying, ensure you have:

1. **Databricks Workspace Access**
   - Model Serving enabled
   - Unity Catalog access
   - Ability to create serving endpoints

2. **Unity Catalog Schema**
   ```sql
   CREATE CATALOG IF NOT EXISTS eps_intelligence;
   CREATE SCHEMA IF NOT EXISTS eps_intelligence.agents;
   ```

3. **Glean API Credentials**
   - API Token with search permissions
   - Instance name (e.g., `guild`)

## Step 1: Create Secrets

Create a secret scope and add your Glean credentials:

```bash
# Create the secret scope
databricks secrets create-scope eps_agent

# Add Glean API token
databricks secrets put-secret eps_agent GLEAN_API_TOKEN
# Enter your token when prompted

# Add Glean instance
databricks secrets put-secret eps_agent GLEAN_INSTANCE
# Enter your instance name (e.g., "guild")
```

Verify secrets are set:
```bash
databricks secrets list-secrets eps_agent
```

## Step 2: Deploy via Notebook

### Upload Files

1. In Databricks, navigate to your workspace folder
2. Upload `agent/eps_agent.py`
3. Upload `agent/deploy_notebook.py`

### Run Deployment

1. Open `deploy_notebook.py` in Databricks
2. Attach to a cluster with:
   - DBR 14.0+ ML Runtime
   - Single node is sufficient
3. Run all cells sequentially

### Deployment Steps (in notebook)

| Step | What It Does |
|------|--------------|
| 1 | Install dependencies |
| 2 | Set configuration |
| 3 | Verify secrets |
| 4 | Set environment variables |
| 5 | (Optional) Test locally |
| 6 | Log agent to MLflow |
| 7 | Validate model |
| 8 | Register to Unity Catalog |
| 9 | Deploy to Model Serving |
| 10 | Test deployed endpoint |

### Expected Output

After Step 9, you'll see:
```
✓ Agent deployed!
  Endpoint: agents_eps_intelligence-agents-eps_account_agent
  Review App: https://your-workspace.cloud.databricks.com/...
```

## Step 3: Verify Deployment

### Check Endpoint Status

1. Go to **Serving** in the Databricks sidebar
2. Find `agents_eps_intelligence-agents-eps_account_agent`
3. Status should be **Ready** (may take 5-15 minutes)

### Test in AI Playground

1. Click on your endpoint
2. Click **AI Playground**
3. Try a test query: "When is AdventHealth renewing?"

### Check MLflow Traces

1. Go to **Experiments** in Databricks
2. Find the experiment associated with your deployment
3. View traces to debug any issues

## Step 4: Share with Stakeholders

### Get the Review App URL

```python
from databricks import agents

deployments = agents.get_deployments("eps_intelligence.agents.eps_account_agent")
print(f"Review App URL: {deployments[0].review_app_url}")
```

### Grant Access

Users need **CAN QUERY** permission on the serving endpoint:

**Via UI:**
1. Go to **Serving** → your endpoint
2. Click **Permissions**
3. Add users/groups with **CAN QUERY**

**Via SDK:**
```python
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
w.serving_endpoints.update_permissions(
    serving_endpoint_id="agents_eps_intelligence-agents-eps_account_agent",
    access_control_list=[
        {"user_name": "user@company.com", "permission_level": "CAN_QUERY"}
    ]
)
```

## Updating the Agent

### Redeploy After Code Changes

1. Edit `eps_agent.py` with your changes
2. Re-run the deployment notebook from **Step 6** onwards
3. A new model version will be created and deployed

### Version Management

Each deployment creates a new model version in Unity Catalog. You can:
- View versions in **Catalog** → `eps_intelligence.agents.eps_account_agent`
- Roll back by deploying a previous version
- Compare traces across versions in MLflow

## CI/CD Deployment

For automated deployments, use Databricks Asset Bundles:

```bash
# Deploy to dev
databricks bundle deploy --target dev

# Deploy to prod
databricks bundle deploy --target prod
```

See `.github/workflows/deploy-agent.yml` for the CI/CD pipeline.

## Troubleshooting

### Endpoint Won't Start

1. Check secrets are correctly set
2. Verify Unity Catalog permissions
3. Check cluster logs in Model Serving

### Agent Returns Errors

1. Check MLflow traces for stack traces
2. Verify Glean API token is valid
3. Test Glean connectivity separately

### Slow Responses

1. Check Glean API latency in traces
2. Consider reducing `num_results` in search calls
3. Review LLM endpoint performance

## Environment Variables

| Variable | Source | Description |
|----------|--------|-------------|
| `GLEAN_API_TOKEN` | Secret | Glean API bearer token |
| `GLEAN_INSTANCE` | Secret | Glean instance name |
| `LLM_ENDPOINT` | Config | Databricks LLM endpoint |

## Costs

Model Serving costs depend on:
- Endpoint size (auto-scales by default)
- Number of queries
- LLM token usage

Enable **scale to zero** for non-production environments:
```python
agents.deploy(
    model_name=UC_MODEL_NAME,
    model_version=version,
    scale_to_zero_enabled=True,  # Reduces cost for dev/staging
    ...
)
```

