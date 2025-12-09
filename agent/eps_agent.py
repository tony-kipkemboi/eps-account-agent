"""
EPS Account Intelligence Agent - Databricks Deployment

This agent uses MLflow's ResponsesAgent interface for deployment on Databricks Model Serving.
It searches Glean enterprise search to provide account intelligence for Account Managers.

Key integrations:
- Glean API for enterprise search across Salesforce, Google Drive, Gong, Gmail, Slack
- Databricks-hosted LLM via WorkspaceClient
- MLflow tracing for observability
"""

import json
import os
import re
import warnings
from datetime import datetime, timedelta
from typing import Any, Generator, Optional
from uuid import uuid4

import backoff
import httpx
import mlflow
import openai
from databricks.sdk import WorkspaceClient
from mlflow.entities import SpanType
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    output_to_responses_items_stream,
    to_chat_completions_input,
)

# Environment variables are injected by Databricks Model Serving from secrets
LLM_ENDPOINT_NAME = os.environ.get("LLM_ENDPOINT", "databricks-gpt-5-mini")
GLEAN_API_TOKEN = os.environ.get("GLEAN_API_TOKEN")
GLEAN_INSTANCE = os.environ.get("GLEAN_INSTANCE")

SYSTEM_PROMPT = """
# EPS Account Intelligence Agent

You help Account Managers retrieve and synthesize account intelligence across Salesforce, Google Drive, Gong, Gmail, and Slack.

## CRITICAL RULES

1. NEVER say "I'll search" or "I'm searching" — just call the tool immediately
2. NEVER ask permission to search — just do it
3. If you need information, call a tool NOW — don't announce it first
4. Each tool call must be a SEPARATE function call with its own arguments

## SCOPE & GUARDRAILS

You ONLY answer questions about:
- Account renewals, contracts, deals (Salesforce)
- Account contacts and stakeholders
- Meeting notes, call recordings, sentiment (Gong)
- Account plans, QBRs, strategy docs (Google Drive)
- Communications history (Slack, Gmail)
- Metrics and dashboards (Looker)

For OFF-TOPIC questions (weather, general knowledge, coding, personal advice, etc.), respond:
"I'm the EPS Account Intelligence assistant. I help with account information like renewals, contacts, call notes, and strategy docs. What account can I help you with?"

## ACCOUNT NAME HANDLING

The agent automatically expands known EP aliases (JPMC, AH, BBW, etc.) when searching.

For accounts NOT in our alias list, use your knowledge of common company abbreviations:
- Include both the full name AND common abbreviations in your search
- Example: For "Bank of America", also consider "BofA", "BAC"
- Example: For "Johnson & Johnson", also consider "J&J", "JNJ"

When a user uses an abbreviation you don't recognize, ask them to clarify which company they mean.

## DATA SOURCE ROUTING

| Question Type | Tool | What to Include |
|---------------|------|-----------------|
| Renewal dates, contracts, deals | search_salesforce_opportunities | Dates, amounts, stage, risks |
| Account overview, company info | search_salesforce_accounts | Industry, segment, tier |
| CLIENT contacts at accounts | search_salesforce_contacts | Role, last contact, decision power |
| Metrics, dashboards, spend | search_metrics_and_dashboards | Trends, YoY changes |
| QBRs, account plans, strategy | search_strategy_docs | Goals, blockers, action items |
| Calls, emails, sentiment | search_communications | Tone, key topics, escalations |

## COMMON USE CASES

### Customer Status Summary
When asked for account status/overview, include:
- **Overall sentiment** (positive/neutral/at-risk based on recent communications)
- **Key dates** (renewal, last QBR, upcoming meetings)
- **Open issues** (support tickets, escalations, blockers)
- **Recent activity** (last call, last email, last meeting)

### Deal Progression
When asked about deal/opportunity progress:
- **Stage and timeline** (where are we, what's next)
- **Blockers** (what's slowing things down)
- **Key stakeholders** (who's involved, who decides)
- **Next actions** (from recent calls/emails)

### Meeting Prep
When preparing for a customer call:
- **Last conversation summary** (from Gong)
- **Open action items** (from previous meetings)
- **Current opportunities/renewals** (from Salesforce)
- **Recent Slack/email threads** (any escalations or concerns)

### Risk Identification
When assessing account health or churn risk:
- **Renewal timeline** (flag if <90 days out)
- **Sentiment trend** (improving or declining)
- **Engagement level** (frequency of touchpoints)
- **Open issues** (unresolved support items)

## QUERY CONSTRUCTION

Place account name FIRST: "AdventHealth renewal" (not "renewal AdventHealth")

## OUTPUT FORMAT

### Structure
1. **Lead with the answer** — the key fact first
2. **Use tables** for comparing items or listing multiple results
3. **Bold key info** — dates, names, amounts, status
4. **Hyperlink sources** — `[Title](URL)` format
5. **Include sentiment** when analyzing communications (positive/neutral/concerned)
6. **End with one insight** if relevant (one sentence max)

### For Status Summaries
Use this format:
- Header with account name
- Sentiment indicator (🟢 Positive / 🟡 Neutral / 🔴 At-Risk)
- Table with Area, Status, Details columns
- Key insight at the end

### For Multiple Results
Use a **table format**:
- Prioritize by date (soonest first)
- Include status/type if available
- Keep it scannable

### Do NOT Include
- "I'll search now" or "Let me search"
- "What I could not find" sections
- Speculation about permission limits
- "Next steps I can take" sections
- Process explanations ("Step 1...", "I searched...")
- Your thinking/reasoning (keep internal)

Only report what you actually found. If results are empty, say so briefly.

## EXAMPLES

✅ GOOD (Status Summary):
"## AdventHealth Summary

**Overall Sentiment:** 🟡 Neutral — recent calls show engagement but some concerns about rollout timing.

| Area | Status | Details |
|------|--------|---------|
| Renewal | **Aug 2026** | In Progress |
| Last Call | Dec 3, 2025 | QBR with VP |
| Open Issues | 2 | Rollout timing |

[View Renewal Opportunity](url) · [Last QBR Notes](url)

**Key insight:** Lifetime Caps rollout timeline is the main risk factor for renewal sentiment."

✅ GOOD (Meeting Prep):
"## Prep for AdventHealth Call

**Last Meeting (Dec 3):** Discussed Q1 goals and Lifetime Caps rollout. Action: Send implementation timeline by Dec 15.

**Open Items:**
- Rollout timeline TBD (they're waiting on us)
- Budget approval pending their CFO sign-off

**Current Opportunity:** [AdventHealth Renewal](url) — $2.4M, closing Aug 2026

**Recent Slack:** Thread in #adventhealth about integration questions — [view thread](url)"

❌ BAD:
"I searched Salesforce and found some results. Next, I'll search Google Drive for more information. Would you like me to also check Gong calls?"

## PERMISSION-AWARE RESPONSES

When a tool returns "No accessible results":
- The user may not have permission to view those records
- Acknowledge briefly: "I couldn't find accessible records for X."
- Don't speculate about what restricted data might contain
"""


def _get_glean_api_url() -> str:
    """Construct Glean API URL from instance name."""
    if not GLEAN_INSTANCE:
        raise RuntimeError("GLEAN_INSTANCE not set")
    
    clean = GLEAN_INSTANCE.replace("https://", "").replace("http://", "").rstrip("/")
    # Support short form: "guild" → "guild-be.glean.com"
    if "." not in clean:
        clean = f"{clean}-be.glean.com"
    
    return f"https://{clean}/rest/api/v1/search"


def glean_search(
    query: str, 
    datasources: Optional[list[str]] = None, 
    num_results: int = 10,
    facet_filters: Optional[list[dict]] = None
) -> list[dict]:
    """
    Search Glean via REST API.
    
    Note: Glean silently filters results based on user permissions (OAuth passthrough).
    Empty results may indicate no matches OR no permission to view matches.
    """
    if not GLEAN_API_TOKEN:
        raise RuntimeError("GLEAN_API_TOKEN not set")
    
    headers = {"Authorization": f"Bearer {GLEAN_API_TOKEN}"}
    
    request_options = {
        "facetBucketSize": 100,
        "returnLlmContentOverSnippets": True,  # Prefer richer content for LLM
    }
    
    if datasources:
        request_options["datasourcesFilter"] = datasources
    
    if facet_filters:
        request_options["facetFilters"] = facet_filters
    
    payload = {
        "query": query,
        "pageSize": num_results,
        "maxSnippetSize": 4000,
        "requestOptions": request_options
    }
    
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(_get_glean_api_url(), headers=headers, json=payload)
            response.raise_for_status()
            
            data = response.json()
            results = data.get("results", [])
            
            formatted = []
            for r in results:
                doc = r.get("document", {})
                content = r.get("llmContent") or r.get("snippets", [])
                
                formatted.append({
                    "title": doc.get("title", "Untitled"),
                    "url": doc.get("url", ""),
                    "content": content,
                    "datasource": doc.get("datasource", ""),
                    "author": doc.get("author", {}).get("name", "Unknown"),
                    "updatedAt": doc.get("updateTime", "")
                })
            return formatted
            
    except httpx.HTTPStatusError as e:
        # Glean uses non-standard error codes - see support.glean.com/hc/en-us/articles/30458821065883
        if e.response.status_code == 400:
            return [{"error": "There was an issue with the search query. Please try rephrasing your question."}]
        elif e.response.status_code == 401:
            return [{"error": "Your session has expired. Please log in again to continue."}]
        elif e.response.status_code == 404:
            # Glean uses 404 for "Missing Permissions" (not standard 403)
            return [{"error": "You don't have permission to access this search feature. Contact your administrator if you believe this is an error."}]
        elif e.response.status_code == 405:
            return [{"error": "There's a configuration issue with the search system. Please contact support."}]
        elif e.response.status_code == 408:
            return [{"error": "The search took too long. Please try a more specific query."}]
        elif e.response.status_code == 429:
            return [{"error": "Too many searches in a short time. Please wait a moment and try again."}]
        elif e.response.status_code >= 500:
            return [{"error": "The search system is temporarily unavailable. Please try again shortly."}]
        else:
            return [{"error": f"Search error ({e.response.status_code}). Please try again or contact support."}]
    except httpx.TimeoutException:
        return [{"error": "The search took too long. Please try a more specific query."}]
    except Exception:
        return [{"error": "Something went wrong with this search. Please try again."}]


def format_results(results: list[dict], source_name: str, query: str = "") -> str:
    """Format search results for LLM consumption."""
    if not results:
        return (
            f"No accessible results found in {source_name}.\n\n"
            "This could mean:\n"
            "• No matching records exist for this query\n"
            "• You may not have permission to view matching records in this source\n\n"
            "Try a different source or rephrase your query."
        )
    
    if results[0].get("error"):
        return results[0]["error"]
    
    formatted = []
    for i, r in enumerate(results[:5], 1):
        title = r.get('title', 'Untitled')
        url = r.get('url', '')
        datasource = r.get('datasource', 'Unknown')
        
        content = r.get('content', [])
        content_text = ''
        if content:
            if isinstance(content, str):
                content_text = content
            elif isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict):
                    content_text = first.get('text', first.get('snippet', ''))
                elif isinstance(first, str):
                    content_text = first
        
        entry = f"**[{i}] {title}**\n"
        entry += f"- **Datasource: {datasource}**\n"
        entry += f"- Content: {content_text[:500]}...\n" if len(content_text) > 500 else f"- Content: {content_text}\n"
        entry += f"- URL: {url}\n"
        formatted.append(entry)
    
    header = f"Found {len(results)} result(s) from {source_name}\n\n"
    return header + "\n".join(formatted)


def quote_account_name(query: str) -> str:
    """
    Quote account name to improve search precision.
    
    Without quoting, "JPMorgan Chase renewal" may return results for other companies.
    Quoting ensures Glean treats multi-word names as a single entity.
    """
    if query.startswith('"'):
        return query
    
    action_words = [
        'renewal', 'renew', 'contract', 'opportunity', 'deal',
        'contact', 'contacts', 'stakeholder', 'decision',
        'account', 'company', 'info', 'overview',
        'call', 'calls', 'meeting', 'email', 'slack',
        'qbr', 'ebr', 'plan', 'strategy', 'doc',
        'metric', 'metrics', 'dashboard', 'spend', 'funding',
        'key', 'recent', 'last', 'latest', 'upcoming'
    ]
    
    words = query.split()
    account_words = []
    rest_words = []
    found_action = False
    
    for word in words:
        if not found_action and word.lower() not in action_words:
            account_words.append(word)
        else:
            found_action = True
            rest_words.append(word)
    
    if account_words:
        account_name = ' '.join(account_words)
        rest = ' '.join(rest_words)
        return f'"{account_name}" {rest}'.strip()
    
    return query


# EP account aliases - source: Guild EP Acronym list
ACCOUNT_ALIASES = {
    "JPMorgan Chase": ["JPMC", "JPM", "JP Morgan", "Chase"],
    "USAA": [],
    "PNC": [],
    "Fidelity": [],
    "Discover": [],
    "Allstate": [],
    "Regions": [],
    "Rocket": [],
    "Zurich": [],
    "AdventHealth": ["AH", "Advent Health", "Advent"],
    "Baylor Scott & White Health": ["BSWH", "Baylor Scott White", "BSW"],
    "Bon Secours Mercy Health": ["BSMH", "Bon Secours"],
    "UCHealth": ["UCH", "UC Health"],
    "Main Line Health": ["MLH"],
    "Sentara Health": ["Sentara"],
    "Humana": [],
    "Baptist Health": [],
    "CHRISTUS Health": ["CHRISTUS"],
    "Cincinnati Children's": ["Cinci Children's"],
    "IU Health": ["Indiana University Health"],
    "Johns Hopkins Health System": ["JHHS", "Johns Hopkins"],
    "Trinity Health": [],
    "Providence": [],
    "Sutter Health": ["Sutter"],
    "Wellstar": [],
    "Sharp HealthCare": ["Sharp"],
    "The Cigna Group": ["Cigna"],
    "Walgreens": [],
    "Walmart": ["WMT", "Wal-Mart"],
    "Target": ["TGT"],
    "Bath & Body Works": ["BBW", "Bath and Body Works"],
    "Kohl's": ["Kohls"],
    "Lowe's": ["Lowes"],
    "Macy's, Inc.": ["Macys", "Macy's"],
    "Sherwin-Williams": ["Sherwin Williams"],
    "Whole Foods Market": ["WFM", "Whole Foods"],
    "H-E-B": ["HEB"],
    "Hy-Vee and Affiliates": ["Hy-Vee", "HyVee"],
    "Giant Eagle": [],
    "Meijer": [],
    "Disney": ["Walt Disney", "WDW"],
    "Hilton": [],
    "Herschend": [],
    "Tesla": [],
    "Ford": [],
    "PepsiCo": ["Pepsi"],
    "Tyson": ["Tyson Foods"],
    "Smithfield": [],
    "Hershey": ["The Hershey Company"],
    "Lennox": [],
    "Chipotle": [],
    "Five Guys": [],
    "MOD Pizza": ["MOD"],
    "Din Tai Fung": ["DTF"],
    "Guild for Guilders": ["G4G"],
    "Charter": [],
    "Sunrun": [],
    "Lennar": [],
    "Pitney Bowes": [],
}

ALIAS_TO_CANONICAL = {}
for canonical, aliases in ACCOUNT_ALIASES.items():
    ALIAS_TO_CANONICAL[canonical.lower()] = canonical
    for alias in aliases:
        ALIAS_TO_CANONICAL[alias.lower()] = canonical


def expand_account_aliases(query: str) -> str:
    """
    Expand account name to include common aliases using OR operator.
    
    Example: "JPMC calls" → '("JPMorgan Chase" OR "JPMC" OR "JPM") calls'
    
    This ensures we find results regardless of which name variation people used.
    """
    query_lower = query.lower()
    
    for alias_lower, canonical in ALIAS_TO_CANONICAL.items():
        if alias_lower in query_lower:
            all_names = [canonical] + ACCOUNT_ALIASES.get(canonical, [])
            seen = set()
            unique_names = []
            for name in all_names:
                if name.lower() not in seen:
                    seen.add(name.lower())
                    unique_names.append(name)
            
            or_clause = " OR ".join([f'"{name}"' for name in unique_names])
            or_clause = f"({or_clause})"
            
            pattern = re.compile(re.escape(alias_lower), re.IGNORECASE)
            match = pattern.search(query)
            if match:
                matched_text = match.group()
                expanded = query.replace(matched_text, or_clause, 1)
                return expanded
    
    return query


def parse_time_expression(query: str) -> tuple[str, Optional[list[dict]]]:
    """
    Parse time expressions from query and return Glean date filter.
    
    Returns: (cleaned_query, date_filter_or_none)
    
    Supported expressions:
    - "last week", "past week" → past_week
    - "last month", "past month" → past_month  
    - "last N days" → GT date filter
    - "recent", "recently" → past_week
    - "today" → today
    - "yesterday" → yesterday
    """
    query_lower = query.lower()
    date_filter = None
    cleaned_query = query
    
    keyword_map = {
        r'\b(last|past)\s+week\b': 'past_week',
        r'\b(last|past)\s+month\b': 'past_month',
        r'\b(last|past)\s+day\b': 'past_day',
        r'\btoday\b': 'today',
        r'\byesterday\b': 'yesterday',
        r'\brecent(ly)?\b': 'past_week',
    }
    
    for pattern, glean_keyword in keyword_map.items():
        if re.search(pattern, query_lower):
            date_filter = [
                {"fieldName": "last_updated_at", "values": [{"relationType": "EQUALS", "value": glean_keyword}]}
            ]
            cleaned_query = re.sub(pattern, '', query, flags=re.IGNORECASE).strip()
            break
    
    days_match = re.search(r'\b(last|past)\s+(\d+)\s+days?\b', query_lower)
    if days_match and not date_filter:
        days = int(days_match.group(2))
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        date_filter = [
            {"fieldName": "last_updated_at", "values": [{"relationType": "GT", "value": start_date}]}
        ]
        cleaned_query = re.sub(r'\b(last|past)\s+\d+\s+days?\b', '', query, flags=re.IGNORECASE).strip()
    
    cleaned_query = ' '.join(cleaned_query.split())
    
    return cleaned_query, date_filter


def merge_facet_filters(existing: Optional[list[dict]], new: Optional[list[dict]]) -> Optional[list[dict]]:
    """Merge two sets of facet filters."""
    if not existing and not new:
        return None
    if not existing:
        return new
    if not new:
        return existing
    return existing + new


@mlflow.trace(span_type=SpanType.TOOL)
def search_salesforce_opportunities(query: str) -> str:
    """Search Salesforce for opportunities (renewals, contracts, deals). Supports time filters."""
    cleaned_query, date_filter = parse_time_expression(query)
    optimized_query = quote_account_name(cleaned_query)
    type_filter = [
        {"fieldName": "type", "values": [{"value": "opportunity", "relationType": "EQUALS"}]}
    ]
    facet_filters = merge_facet_filters(type_filter, date_filter)
    results = glean_search(optimized_query, datasources=["salescloud"], num_results=5, facet_filters=facet_filters)
    return format_results(results, "Salesforce Opportunities")


@mlflow.trace(span_type=SpanType.TOOL)
def search_salesforce_accounts(query: str) -> str:
    """Search Salesforce for account records (company info)."""
    optimized_query = quote_account_name(query)
    facet_filters = [
        {"fieldName": "type", "values": [{"value": "account", "relationType": "EQUALS"}]}
    ]
    results = glean_search(optimized_query, datasources=["salescloud"], num_results=5, facet_filters=facet_filters)
    return format_results(results, "Salesforce Accounts")


@mlflow.trace(span_type=SpanType.TOOL)
def search_salesforce_contacts(query: str) -> str:
    """Search Salesforce for CLIENT contacts at partner companies (not Guild employees)."""
    optimized_query = quote_account_name(query)
    facet_filters = [
        {"fieldName": "type", "values": [{"value": "contact", "relationType": "EQUALS"}]}
    ]
    results = glean_search(optimized_query, datasources=["salescloud"], num_results=5, facet_filters=facet_filters)
    return format_results(results, "Salesforce Contacts")


@mlflow.trace(span_type=SpanType.TOOL)
def search_metrics_and_dashboards(query: str) -> str:
    """Search Salesforce and Looker for metrics, dashboards, funding."""
    optimized_query = quote_account_name(query)
    results = glean_search(optimized_query, datasources=["salescloud", "looker"], num_results=6)
    return format_results(results, "Metrics (Salesforce + Looker)")


@mlflow.trace(span_type=SpanType.TOOL)
def search_strategy_docs(query: str) -> str:
    """Search Google Drive for QBRs, Account Plans, strategy docs. Supports time filters and account aliases."""
    cleaned_query, date_filter = parse_time_expression(query)
    expanded_query = expand_account_aliases(cleaned_query)
    results = glean_search(expanded_query, datasources=["gdrive"], num_results=5, facet_filters=date_filter)
    return format_results(results, "Google Drive")


@mlflow.trace(span_type=SpanType.TOOL)
def search_communications(query: str) -> str:
    """Search Gong, Slack, Gmail for calls, messages, communications. Supports time filters and account aliases."""
    cleaned_query, date_filter = parse_time_expression(query)
    expanded_query = expand_account_aliases(cleaned_query)
    results = glean_search(expanded_query, datasources=["gong", "slack", "gmail"], num_results=9, facet_filters=date_filter)
    return format_results(results, "Communications (Gong/Slack/Gmail)")


@mlflow.trace(span_type=SpanType.TOOL)
def search_general_fallback(query: str) -> str:
    """Search ALL sources. Use only when user approves."""
    optimized_query = quote_account_name(query)
    results = glean_search(optimized_query, datasources=None, num_results=10)
    return format_results(results, "All Sources")


TOOLS = {
    "search_salesforce_opportunities": search_salesforce_opportunities,
    "search_salesforce_accounts": search_salesforce_accounts,
    "search_salesforce_contacts": search_salesforce_contacts,
    "search_metrics_and_dashboards": search_metrics_and_dashboards,
    "search_strategy_docs": search_strategy_docs,
    "search_communications": search_communications,
    "search_general_fallback": search_general_fallback,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_salesforce_opportunities",
            "description": "Search Salesforce OPPORTUNITIES for renewals, contracts, deals. Query MUST start with account name.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Query starting with account name"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_salesforce_accounts",
            "description": "Search Salesforce ACCOUNT records for company info. Query MUST start with account name.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Query starting with account name"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_salesforce_contacts",
            "description": "Search Salesforce for CLIENT contacts at partner companies. Use for 'who are the contacts at [Account]' questions. NOT for Guild employees.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Account name + contacts (e.g., 'Tesla contacts')"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_metrics_and_dashboards",
            "description": "Search Salesforce/Looker for metrics, dashboards, funding. Query should include account name.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Query with account name"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_strategy_docs",
            "description": "Search Google Drive for QBRs, Account Plans, strategy docs. Query should include account name.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Query with account name"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_communications",
            "description": "Search Gong/Slack/Gmail for calls, sentiment, messages. Query should include account name.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Query with account name"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_general_fallback",
            "description": "Search ALL sources. Only use when user approves after other tools fail.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"]
            }
        }
    },
]


class EPSAccountAgent(ResponsesAgent):
    """
    EPS Account Intelligence Agent using MLflow ResponsesAgent interface.
    
    This class implements the Databricks agent pattern for Model Serving deployment.
    Uses WorkspaceClient to get an OpenAI-compatible client for Databricks-hosted LLMs.
    """
    
    def __init__(self):
        self.workspace_client = WorkspaceClient()
        self.client = self.workspace_client.serving_endpoints.get_open_ai_client()
        self.llm_endpoint = LLM_ENDPOINT_NAME
    
    @mlflow.trace(span_type=SpanType.TOOL)
    def execute_tool(self, tool_name: str, args: dict) -> str:
        """Execute a tool by name."""
        if tool_name not in TOOLS:
            return "I don't have access to that information source. Let me try a different approach."
        try:
            return TOOLS[tool_name](**args)
        except Exception:
            return "I ran into an issue searching for that information. Please try rephrasing your question."
    
    @backoff.on_exception(backoff.expo, openai.RateLimitError)
    @mlflow.trace(span_type=SpanType.LLM)
    def call_llm(self, messages: list[dict], stream: bool = False) -> Any:
        """Call the LLM with messages and tools."""
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="PydanticSerializationUnexpectedValue")
            return self.client.chat.completions.create(
                model=self.llm_endpoint,
                messages=messages,
                tools=TOOL_SPECS,
                stream=stream,
            )
    
    @backoff.on_exception(backoff.expo, openai.RateLimitError)
    @mlflow.trace(span_type=SpanType.LLM)
    def call_llm_stream(self, messages: list[dict]) -> Generator[dict, None, None]:
        """Call the LLM with streaming for real-time responses."""
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="PydanticSerializationUnexpectedValue")
            for chunk in self.client.chat.completions.create(
                model=self.llm_endpoint,
                messages=to_chat_completions_input(messages),
                tools=TOOL_SPECS,
                stream=True,
            ):
                yield chunk.to_dict()
    
    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        """Process a request and return a response (non-streaming entry point)."""
        outputs = [
            event.item
            for event in self.predict_stream(request)
            if event.type == "response.output_item.done"
        ]
        return ResponsesAgentResponse(output=outputs)
    
    def _handle_tool_call(
        self, tool_call: dict, messages: list[dict]
    ) -> ResponsesAgentStreamEvent:
        """Execute a tool call and append result to message history."""
        args = json.loads(tool_call["arguments"])
        result = self.execute_tool(tool_call["name"], args)
        
        tool_output = self.create_function_call_output_item(
            call_id=tool_call["call_id"],
            output=result
        )
        messages.append(tool_output)
        
        return ResponsesAgentStreamEvent(
            type="response.output_item.done",
            item=tool_output
        )
    
    def _call_and_run_tools(
        self, messages: list[dict], max_iter: int = 10
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """
        Agentic loop: call LLM → execute tools → repeat until done.
        
        Uses output_to_responses_items_stream to convert OpenAI streaming chunks
        to ResponsesAgent events for AI Playground compatibility.
        """
        for _ in range(max_iter):
            last_msg = messages[-1] if messages else {}
            
            if last_msg.get("role") == "assistant" and not last_msg.get("tool_calls"):
                return
            
            if last_msg.get("type") == "function_call":
                yield self._handle_tool_call(last_msg, messages)
            else:
                yield from output_to_responses_items_stream(
                    chunks=self.call_llm_stream(messages),
                    aggregator=messages
                )
        
        yield ResponsesAgentStreamEvent(
            type="response.output_item.done",
            item=self.create_text_output_item(
                text="Max iterations reached. Please try a more specific question.",
                id=str(uuid4())
            )
        )
    
    def predict_stream(
        self, request: ResponsesAgentRequest
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Stream responses for real-time feedback in AI Playground."""
        try:
            # MLflow user/session tracking: https://mlflow.org/docs/latest/genai/tracing/track-users-sessions/
            context = getattr(request, 'context', None) or {}
            user_id = context.get('user_id') if isinstance(context, dict) else getattr(context, 'user_id', None)
            session_id = context.get('conversation_id') if isinstance(context, dict) else getattr(context, 'conversation_id', None)
            
            if user_id or session_id:
                mlflow.update_current_trace(
                    metadata={
                        **({"mlflow.trace.user": user_id} if user_id else {}),
                        **({"mlflow.trace.session": session_id} if session_id else {}),
                    }
                )
            
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages.extend([msg.model_dump() for msg in request.input])
            yield from self._call_and_run_tools(messages)
        except Exception:
            yield ResponsesAgentStreamEvent(
                type="response.output_item.done",
                item=self.create_text_output_item(
                    text="I'm having trouble processing your request right now. Please try again or rephrase your question.",
                    id=str(uuid4())
                )
            )


mlflow.openai.autolog()
AGENT = EPSAccountAgent()
mlflow.models.set_model(AGENT)
