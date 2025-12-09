# Architecture

This document describes the technical architecture of the EPS Account Intelligence Agent.

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              USERS                                       │
│                    Account Managers (Okta SSO)                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            FRONTEND                                      │
│  ┌─────────────────────┐    ┌─────────────────────────────────────┐    │
│  │   Chat UI           │    │   Lakebase                          │    │
│  │   (Next.js)         │───▶│   (Conversation Memory)             │    │
│  └─────────────────────┘    └─────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          DATABRICKS                                      │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    Model Serving Endpoint                        │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐ │   │
│  │  │ EPS Agent   │  │ LLM         │  │ Search Tools            │ │   │
│  │  │ (Python)    │──│ (GPT-5-mini)│──│ (7 Glean API wrappers)  │ │   │
│  │  └─────────────┘  └─────────────┘  └─────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                    │                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────────┐  │
│  │ Unity        │  │ MLflow       │  │ Secrets Manager              │  │
│  │ Catalog      │  │ Tracing      │  │ (GLEAN_API_TOKEN, etc.)      │  │
│  └──────────────┘  └──────────────┘  └──────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           GLEAN API                                      │
│                    Enterprise Search Gateway                             │
│                    (OAuth 2.1 Passthrough)                              │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         DATA SOURCES                                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │Salesforce│ │  Gong    │ │ Google   │ │  Slack   │ │  Gmail   │     │
│  │          │ │          │ │ Drive    │ │          │ │          │     │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘     │
│  ┌──────────┐                                                          │
│  │  Looker  │                                                          │
│  └──────────┘                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Agent Core (`eps_agent.py`)

The main agent implementation using MLflow's `ResponsesAgent` interface.

**Key Classes:**
- `EPSAccountAgent` - Main agent class implementing `predict()` and `predict_stream()`

**Key Functions:**
- `glean_search()` - Core search function calling Glean REST API
- `format_results()` - Formats search results for LLM consumption
- `expand_account_aliases()` - Expands account abbreviations (JPMC → JPMorgan Chase)
- `parse_time_expression()` - Parses "last week", "past 30 days", etc.

### 2. Search Tools

Seven specialized search tools, each targeting specific data sources:

| Tool | Data Source | Use Case |
|------|-------------|----------|
| `search_salesforce_opportunities` | Salesforce | Renewals, contracts, deals |
| `search_salesforce_accounts` | Salesforce | Account overview, company info |
| `search_salesforce_contacts` | Salesforce | Client contacts at accounts |
| `search_metrics_and_dashboards` | Salesforce + Looker | Metrics, dashboards, funding |
| `search_strategy_docs` | Google Drive | QBRs, account plans, strategy |
| `search_communications` | Gong + Slack + Gmail | Calls, messages, emails |
| `search_general_fallback` | All sources | When user approves broad search |

### 3. LLM Integration

Uses Databricks-hosted LLM via `WorkspaceClient`:

```python
self.workspace_client = WorkspaceClient()
self.client = self.workspace_client.serving_endpoints.get_open_ai_client()
```

**Model:** `databricks-gpt-5-mini` (configurable via `LLM_ENDPOINT` env var)

### 4. Agentic Loop

The agent follows a ReAct (Reason + Act) pattern:

```
┌──────────────────┐
│ User Message     │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌──────────────────┐
│ Call LLM         │────▶│ LLM returns      │
│ (with tools)     │     │ tool_call?       │
└──────────────────┘     └────────┬─────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
                    ▼                           ▼
           ┌──────────────┐           ┌──────────────┐
           │ Yes: Execute │           │ No: Return   │
           │ Tool         │           │ Response     │
           └──────┬───────┘           └──────────────┘
                  │
                  ▼
         ┌──────────────────┐
         │ Append result    │
         │ to messages      │
         └────────┬─────────┘
                  │
                  ▼
         ┌──────────────────┐
         │ Loop (max 10)    │
         └──────────────────┘
```

## Data Flow

### Query Processing

1. **User Input** → Chat UI sends message to Model Serving endpoint
2. **Agent Receives** → `predict_stream()` called with user message
3. **LLM Decides** → LLM chooses which tool(s) to call
4. **Tool Execution** → Agent calls Glean API with optimized query
5. **Results Formatted** → Search results formatted for LLM
6. **LLM Synthesizes** → LLM generates final response
7. **Streaming Response** → Response streamed back to UI

### Search Optimization

Before calling Glean, queries are optimized:

```python
# 1. Parse time expressions
"JPMC calls last week" → "JPMC calls" + date_filter: past_week

# 2. Expand aliases
"JPMC calls" → '("JPMorgan Chase" OR "JPMC" OR "JPM") calls'

# 3. Quote account names
"AdventHealth renewal" → '"AdventHealth" renewal'
```

## Security

### Authentication Flow

```
User (Okta SSO) → Chat UI → Databricks (PAT/OAuth) → Glean (API Token)
```

### Permission Model

- **Databricks**: Users need `CAN QUERY` on serving endpoint
- **Glean**: Results filtered by user's source system permissions
- **Secrets**: Stored in Databricks Secrets Manager

### Data Privacy

- No PII stored in logs (MLflow traces redacted)
- Glean silently filters results user can't access
- No conversation data stored in agent (UI handles persistence)

## Observability

### MLflow Tracing

Every request is traced with:
- User ID and session ID (from request context)
- Tool calls and their arguments
- LLM calls and token usage
- Latency breakdown

### Monitoring

| Metric | Source |
|--------|--------|
| Request latency | Model Serving metrics |
| Error rate | Model Serving metrics |
| Tool call distribution | MLflow traces |
| LLM token usage | MLflow traces |
| User feedback | Review App |

## Scalability

### Model Serving

- **Auto-scaling**: Endpoint scales based on traffic
- **Scale to zero**: Optional for non-prod environments
- **Concurrency**: Multiple requests handled in parallel

### Glean API

- **Rate limiting**: Backoff retry on 429 errors
- **Timeout**: 30s per request
- **Connection pooling**: Via `httpx.Client`

## Future Enhancements

### Planned

- [ ] Guardrails for prompt injection detection
- [ ] Iceberg table integration for structured data
- [ ] Feedback loop for alias suggestions
- [ ] Cached responses for common queries

### Potential

- Vector search for semantic similarity
- Multi-turn conversation memory in agent
- Proactive alerts for renewal risks

