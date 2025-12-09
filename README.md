# EPS Account Intelligence Agent

An AI-powered conversational agent that helps Account Managers retrieve and synthesize account intelligence across enterprise data sources.

## Overview

The EPS Account Intelligence Agent searches across Salesforce, Google Drive, Gong, Gmail, Slack, and Looker via Glean's enterprise search API to answer questions about:

- **Renewals & Contracts** — When is an account renewing? What's the deal status?
- **Account Contacts** — Who are the key stakeholders at an account?
- **Meeting Notes & Calls** — What was discussed in the last QBR?
- **Strategy Docs** — What's the account plan for this quarter?
- **Communications** — Any recent escalations or concerns?
- **Metrics & Dashboards** — What's the account health score?

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Chat UI       │────▶│  Databricks      │────▶│   Glean API     │
│   (Next.js)     │     │  Model Serving   │     │   (Search)      │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │                         │
                               ▼                         ▼
                        ┌──────────────┐         ┌──────────────┐
                        │   MLflow     │         │ Data Sources │
                        │   Tracing    │         │ SF/GD/Gong/  │
                        └──────────────┘         │ Gmail/Slack  │
                                                 └──────────────┘
```

## Quick Start

### Prerequisites

- Databricks workspace with Model Serving enabled
- Unity Catalog access (`eps_intelligence.agents` schema)
- Glean API credentials
- Python 3.10+

### 1. Set Up Secrets

```bash
# Create secret scope
databricks secrets create-scope eps_agent

# Add Glean credentials
databricks secrets put-secret eps_agent GLEAN_API_TOKEN
databricks secrets put-secret eps_agent GLEAN_INSTANCE
```

### 2. Deploy the Agent

**Option A: Via Databricks Notebook**
1. Upload `agent/eps_agent.py` and `agent/deploy_notebook.py` to your Databricks workspace
2. Run the deployment notebook

**Option B: Via CLI**
```bash
./scripts/deploy.sh dev    # Deploy to dev
./scripts/deploy.sh prod   # Deploy to prod
```

### 3. Test in AI Playground

1. Go to **Serving** in Databricks
2. Find your endpoint: `agents_eps_intelligence-agents-eps_account_agent`
3. Click **AI Playground** to chat with the agent

## Project Structure

```
eps-account-agent/
├── agent/
│   ├── eps_agent.py          # Main agent implementation
│   └── deploy_notebook.py    # Databricks deployment notebook
├── scripts/
│   └── deploy.sh             # CLI deployment helper
├── docs/
│   ├── ARCHITECTURE.md       # Detailed architecture
│   └── DEPLOYMENT.md         # Deployment guide
├── .github/workflows/
│   └── deploy-agent.yml      # CI/CD pipeline
├── databricks.yml            # Asset Bundle config
└── requirements.txt          # Python dependencies
```

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `GLEAN_API_TOKEN` | Glean API bearer token | Yes |
| `GLEAN_INSTANCE` | Glean instance (e.g., `guild`) | Yes |
| `LLM_ENDPOINT` | Databricks LLM endpoint | No (default: `databricks-gpt-5-mini`) |

### Unity Catalog

The agent is registered to:
- **Catalog**: `eps_intelligence`
- **Schema**: `agents`
- **Model**: `eps_account_agent`

## Features

### 🔍 Multi-Source Search
Searches across 6 enterprise data sources via Glean's unified API.

### 🎯 Smart Routing
Automatically routes questions to the right data source based on intent.

### 📊 Structured Output
Returns formatted tables, hyperlinked sources, and sentiment indicators.

### 🔐 Permission-Aware
Respects user permissions via Glean OAuth passthrough.

### ⏰ Time-Aware Queries
Understands "last week", "past month", "last 30 days" in queries.

### 🏢 Account Alias Expansion
Recognizes common abbreviations (JPMC → JPMorgan Chase, AH → AdventHealth).

## Example Queries

```
"When is AdventHealth renewing?"
"Who are the key contacts at Tesla?"
"Summarize the last JPMC QBR"
"Any recent escalations for Walmart?"
"What's the deal status for Humana?"
"Prep me for my AdventHealth call tomorrow"
```

## Development

### Local Testing

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export GLEAN_API_TOKEN="your-token"
export GLEAN_INSTANCE="your-instance"
```

### CI/CD

The GitHub Actions workflow automatically deploys:
- `develop` branch → **staging** environment
- `main` branch → **production** environment

## Sharing with Stakeholders

After deployment, share the **Review App URL** with stakeholders:

```python
from databricks import agents

deployment = agents.get_deployments("eps_intelligence.agents.eps_account_agent")
print(f"Review App: {deployment[0].review_app_url}")
```

Stakeholders need **CAN QUERY** permission on the serving endpoint.

## Monitoring

- **MLflow Tracing**: View traces in the MLflow UI
- **Model Serving Metrics**: Monitor latency and throughput in Databricks
- **Review App Feedback**: Collect 👍/👎 feedback from users

## License

Internal use only. © Guild Education.

## Support

For issues or questions, contact the Data & AI team or open an issue in this repo.

