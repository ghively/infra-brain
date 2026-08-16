---
name: lc-data
description: Use when building SQL agents, Pandas/DataFrame agents, CSV/Excel pipelines, OpenAPI agents, or any structured-data pattern with LangChain or LangGraph. Triggered by requests to query a database with natural language, build a text-to-SQL agent, analyze a CSV/Excel file, connect to a REST API, or mix multiple data sources. Teaches safety patterns (read-only connections, SQL injection prevention via AST parsing, sensitive column masking) as it scaffolds code.
---

# lc-data — SQL Agents, Pandas Agents & Structured Data Patterns

## Overview

This skill scaffolds every structured-data pattern in the LangChain/LangGraph ecosystem.
It teaches as it builds — every concept is explained inline on first use.
Default model is `claude-sonnet-4-6` via `langchain-anthropic`.

**Safety is non-negotiable.** Every SQL pattern includes AST-level validation (not string matching),
read-only connection strings, result-size limiting, and sensitive column masking.
These are explained in full — not just bolted on silently.

---

## Discovery Flow

Ask these three questions before generating any code. They determine the entire pattern.
Skip any question that the user's message already answers.

### Question 1 — Data Source

```
What's your data source?

  1. SQL database (PostgreSQL, SQLite, MySQL, etc.)
  2. CSV or Excel file
  3. JSON / REST API
  4. Pandas DataFrame (already in memory)
  5. Multiple sources (e.g., SQL + documents, CSV + API)

Enter 1-5 (or describe your source):
```

### Question 2 — Write Access

```
Do you need write access to the data?

  1. SELECT only — read queries, no modifications
  2. Full access — INSERT / UPDATE / DELETE also needed

⚠  This is a critical security decision. Write access means the LLM can
   modify or delete your data if it misinterprets a question. Read-only
   is strongly recommended unless your use case explicitly requires writes.
```

### Question 3 — Usage Pattern

```
How will this agent be used?

  1. Interactive exploration — a human chats with it in real time
  2. Automated pipeline — runs on a schedule or is called by another system

This affects error handling, retry logic, and whether to include
human-in-the-loop approval before executing queries.
```

Use the answers to select the right pattern below.

---

## Environment Setup

```bash
pip install langgraph langchain-anthropic langchain-community \
            sqlalchemy sqlglot pandas openpyxl requests \
            langchain-experimental python-dotenv
```

```bash
# .env
ANTHROPIC_API_KEY="sk-ant-..."
LANGSMITH_API_KEY="ls__..."
LANGSMITH_TRACING="true"
LANGSMITH_PROJECT="data-agent"
DATABASE_URL="postgresql+psycopg2://user:pass@host:5432/mydb"
```

```python
# Every file starts with this — loads .env before any LangChain import
from dotenv import load_dotenv
load_dotenv()
```

---

## Pattern 1 — Text-to-SQL with Validation Loop (LangGraph)

**Use when:** SQL database, any access level, interactive or automated.
This is the most requested LangChain pattern and the most dangerous if done naively.

### Why a validation loop?

A plain LLM-to-SQL pipeline will occasionally generate harmful queries.
The LLM might write `DROP TABLE` if asked "delete all my old records."
We intercept every query with AST parsing before it ever reaches the database.

**AST parsing vs string matching:** String matching (`if "DROP" in sql`) can be bypassed
with SQL comments, Unicode tricks, or mixed case. AST parsing (via `sqlglot`) understands
the query's intent regardless of formatting — it cannot be tricked.

### State Definition

```python
# sql_agent/state.py
from typing import TypedDict, Optional

class SQLAgentState(TypedDict):
    """
    TypedDict is the standard way to define LangGraph state.
    Every node receives this dict and returns a partial update.
    Fields not returned by a node are left unchanged.
    """
    question: str           # original natural language question
    sql: str                # generated SQL query
    result: str             # query result as formatted string
    error: Optional[str]    # validation or execution error message
    retry_count: int        # how many rewrite attempts have occurred
    final_answer: str       # natural language summary of result
```

### Graph Nodes

```python
# sql_agent/nodes.py
from dotenv import load_dotenv
load_dotenv()

import os
import sqlglot
import sqlglot.errors
from langchain_anthropic import ChatAnthropic
from langchain_community.utilities import SQLDatabase
from langchain_core.tools import ToolException
from langchain_core.prompts import ChatPromptTemplate

# --- Database connection ---
# include_tables limits the schema injected into prompts — never give the LLM
# your entire schema. Only include tables relevant to the queries you expect.
# sample_rows_in_table_info=2 adds two example rows per table so the LLM
# understands the data shape without exposing sensitive production data.
db = SQLDatabase.from_uri(
    os.getenv("DATABASE_URL"),
    include_tables=["orders", "customers", "products"],  # customize per project
    sample_rows_in_table_info=2,
)

# The model that generates and rewrites SQL
llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

# --- Dangerous operation blocklist ---
# These are SQL statement types that should never run from an LLM.
# sqlglot parses the AST — it returns the statement type as a class name.
BLOCKED_STATEMENT_TYPES = {
    "Drop", "Truncate", "Delete", "Update", "Insert",
    "Create", "Alter", "Grant", "Revoke",
    # SQL Server-specific dangerous procs — block by keyword in raw SQL too
}

BLOCKED_KEYWORDS = {"xp_cmdshell", "exec(", "execute(", "sp_executesql"}


def generate_sql(state: SQLAgentState) -> dict:
    """
    Node 1: Ask the LLM to write SQL for the user's question.

    We inject the schema with db.get_table_info() so the LLM knows column
    names and types. We also inject few-shot examples of good SQL for this
    specific database — this dramatically improves accuracy.
    """
    schema = db.get_table_info()

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a SQL expert. Write a single SQL SELECT query to answer the question.
Database schema:
{schema}

Rules:
- Only write SELECT queries. Never INSERT, UPDATE, DELETE, DROP, or TRUNCATE.
- Always include LIMIT 100 if the query could return many rows.
- Return ONLY the SQL query, no explanation, no markdown fences.

Examples of good queries for this database:
-- How many orders were placed last month?
SELECT COUNT(*) FROM orders WHERE created_at >= DATE_TRUNC('month', NOW() - INTERVAL '1 month');

-- Top 5 customers by total spend:
SELECT c.name, SUM(o.total) as total_spend
FROM customers c JOIN orders o ON c.id = o.customer_id
GROUP BY c.name ORDER BY total_spend DESC LIMIT 5;
"""),
        ("human", "{question}")
    ])

    chain = prompt | llm
    response = chain.invoke({"schema": schema, "question": state["question"]})

    # Strip markdown fences if the LLM included them despite instructions
    sql = response.content.strip().strip("```sql").strip("```").strip()

    return {"sql": sql, "error": None}


def validate_sql(state: SQLAgentState) -> dict:
    """
    Node 2: Parse and validate the SQL using sqlglot AST analysis.

    This is the security checkpoint. We parse the SQL into an abstract syntax
    tree (AST) and inspect the statement type. This catches injection attempts
    that would fool string matching.

    If validation fails, we set an error message. The router node will
    decide whether to retry or abort based on retry_count.
    """
    sql = state["sql"]

    # Check for dangerous keywords in raw SQL (belt-and-suspenders)
    sql_lower = sql.lower()
    for keyword in BLOCKED_KEYWORDS:
        if keyword in sql_lower:
            return {"error": f"Blocked: dangerous keyword '{keyword}' detected."}

    # Parse with sqlglot — if parsing fails, the SQL is malformed
    try:
        statements = sqlglot.parse(sql)
    except sqlglot.errors.ParseError as e:
        return {"error": f"SQL parse error: {e}"}

    if not statements:
        return {"error": "No valid SQL statement found."}

    if len(statements) > 1:
        return {"error": "Multiple statements detected. Only single queries are allowed."}

    statement = statements[0]

    # Check the AST node type — this is AST-level, not string-level
    statement_type = type(statement).__name__
    if statement_type in BLOCKED_STATEMENT_TYPES:
        return {"error": f"Blocked: {statement_type} statements are not allowed. Only SELECT."}

    # Ensure SELECT is the top-level statement
    if statement_type != "Select":
        return {"error": f"Expected SELECT, got {statement_type}."}

    return {"error": None}  # validation passed


def execute_sql(state: SQLAgentState) -> dict:
    """
    Node 3: Run the validated SQL and return results.

    We limit result size to prevent the LLM context from being flooded.
    We also mask sensitive columns — the LLM never sees raw SSNs or card numbers.
    """
    SENSITIVE_COLUMNS = {"ssn", "social_security", "credit_card", "card_number", "password", "password_hash"}
    MAX_RESULT_CHARS = 4000  # keep within LLM context limits

    try:
        result = db.run(state["sql"])

        # Mask sensitive columns in the result string (simple pattern-based mask)
        # In production, mask at the DB level with views instead
        for col in SENSITIVE_COLUMNS:
            import re
            result = re.sub(
                rf"(?i)({col}\s*:\s*)([^\n,}}]+)",
                r"\1[REDACTED]",
                result
            )

        # Truncate if result is too large
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + "\n... [result truncated at 4000 chars]"

        return {"result": result, "error": None}

    except Exception as e:
        return {"error": f"Execution error: {e}", "result": ""}


def summarize_result(state: SQLAgentState) -> dict:
    """
    Node 4: Convert raw SQL results into a natural language answer.

    The LLM sees the original question and the raw result, then writes
    a human-readable summary. This is where the UX value is delivered.
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You answer data questions clearly and concisely. "
                   "Summarize the SQL query result in plain English. "
                   "If the result is empty, say so. Include specific numbers."),
        ("human", "Question: {question}\n\nSQL result:\n{result}")
    ])

    chain = prompt | llm
    response = chain.invoke({
        "question": state["question"],
        "result": state["result"]
    })
    return {"final_answer": response.content}


def handle_error(state: SQLAgentState) -> dict:
    """
    Node 5: Rewrite the SQL after a validation or execution failure.

    We include the error in the prompt so the LLM understands what went wrong.
    This enables the retry loop to converge on valid SQL.
    """
    schema = db.get_table_info()

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a SQL expert fixing a broken query.
Database schema:
{schema}
Only write SELECT queries. Return ONLY the corrected SQL, no explanation."""),
        ("human", """Original question: {question}

Failed SQL:
{sql}

Error:
{error}

Write a corrected SQL query:""")
    ])

    chain = prompt | llm
    response = chain.invoke({
        "schema": schema,
        "question": state["question"],
        "sql": state["sql"],
        "error": state["error"]
    })

    sql = response.content.strip().strip("```sql").strip("```").strip()
    return {
        "sql": sql,
        "retry_count": state["retry_count"] + 1,
        "error": None
    }
```

### Graph Assembly

```python
# sql_agent/graph.py
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import StateGraph, END
from sql_agent.state import SQLAgentState
from sql_agent.nodes import (
    generate_sql, validate_sql, execute_sql,
    summarize_result, handle_error
)

MAX_RETRIES = 3  # prevent infinite loops on persistently bad SQL


def route_after_validation(state: SQLAgentState) -> str:
    """
    Conditional edge: decide where to go after validation.

    In LangGraph, conditional edges are functions that return a string
    matching one of the node names (or END). This is the retry logic hub.
    """
    if state.get("error"):
        if state["retry_count"] >= MAX_RETRIES:
            # Too many failures — route to summarize with error context
            return "summarize_result"
        return "handle_error"
    return "execute_sql"


def route_after_execution(state: SQLAgentState) -> str:
    """After execution, check for DB errors before summarizing."""
    if state.get("error"):
        if state["retry_count"] >= MAX_RETRIES:
            return "summarize_result"
        return "handle_error"
    return "summarize_result"


# Build the graph
builder = StateGraph(SQLAgentState)

# Add nodes — each is a plain Python function
builder.add_node("generate_sql", generate_sql)
builder.add_node("validate_sql", validate_sql)
builder.add_node("execute_sql", execute_sql)
builder.add_node("summarize_result", summarize_result)
builder.add_node("handle_error", handle_error)

# Define the happy path edges
builder.set_entry_point("generate_sql")
builder.add_edge("generate_sql", "validate_sql")

# Conditional edges use a router function instead of a fixed destination
builder.add_conditional_edges(
    "validate_sql",
    route_after_validation,
    {
        "execute_sql": "execute_sql",
        "handle_error": "handle_error",
        "summarize_result": "summarize_result",
    }
)

builder.add_conditional_edges(
    "execute_sql",
    route_after_execution,
    {
        "summarize_result": "summarize_result",
        "handle_error": "handle_error",
    }
)

# After error handling, re-validate the rewritten SQL
builder.add_edge("handle_error", "validate_sql")
builder.add_edge("summarize_result", END)

# recursion_limit prevents infinite loops if the routing logic has a bug
# 20 steps is generous for a 4-node graph with 3 retries
graph = builder.compile()
graph.config = {"recursion_limit": 20}
```

### Usage

```python
# sql_agent/main.py
from dotenv import load_dotenv
load_dotenv()

from sql_agent.graph import graph

result = graph.invoke({
    "question": "What were the top 5 products by revenue last quarter?",
    "sql": "",
    "result": "",
    "error": None,
    "retry_count": 0,
    "final_answer": "",
})

print(result["final_answer"])
# → "The top 5 products by revenue last quarter were: ..."
```

---

## Pattern 2 — Safe SQL Tool (for ReAct agents)

**Use when:** You want to give an existing agent SQL access as one tool among many,
rather than building a dedicated SQL graph.

```python
# tools/sql_tool.py
from dotenv import load_dotenv
load_dotenv()

import os
import re
import sqlglot
from langchain_community.utilities import SQLDatabase
from langchain_core.tools import tool, ToolException

# Read-only connection string pattern:
# PostgreSQL: add ?options=-c%20default_transaction_read_only%3Don
# MySQL: use a DB user with only SELECT privileges
# SQLite: use uri=True with ?mode=ro
READ_ONLY_URL = os.getenv("DATABASE_URL") + "?options=-c%20default_transaction_read_only%3Don"

db = SQLDatabase.from_uri(
    READ_ONLY_URL,
    include_tables=["orders", "customers", "products"],
    sample_rows_in_table_info=2,
)

BLOCKED_TYPES = {"Drop", "Truncate", "Delete", "Update", "Insert", "Create", "Alter"}
SENSITIVE_COLS = {"ssn", "credit_card", "password"}


def _validate_and_add_limit(sql: str) -> str:
    """Parse SQL with sqlglot, block dangerous statements, inject LIMIT if missing."""
    try:
        statements = sqlglot.parse(sql.strip())
    except Exception as e:
        raise ToolException(f"SQL parse failed: {e}")

    if not statements or len(statements) > 1:
        raise ToolException("Exactly one SELECT statement is required.")

    stmt = statements[0]
    if type(stmt).__name__ in BLOCKED_TYPES:
        raise ToolException(f"Only SELECT queries are allowed. Got: {type(stmt).__name__}")
    if type(stmt).__name__ != "Select":
        raise ToolException(f"Expected SELECT, got {type(stmt).__name__}")

    # Inject LIMIT 100 if no LIMIT clause is present
    # sqlglot can rewrite the AST — we add the limit programmatically
    if not stmt.args.get("limit"):
        stmt = stmt.limit(100)

    return stmt.sql(dialect="postgres")


@tool
def query_database(sql: str) -> str:
    """
    Execute a SQL SELECT query against the database and return results.

    Args:
        sql: A SQL SELECT query. Must be read-only. Will automatically
             add LIMIT 100 if not present.

    Returns:
        Query results as a formatted string, or an error message.

    Raises:
        ToolException: If the query is not a SELECT or is malformed.
    """
    # Validate and rewrite
    safe_sql = _validate_and_add_limit(sql)

    # Execute
    try:
        result = db.run(safe_sql)
    except Exception as e:
        raise ToolException(f"Database error: {e}")

    # Mask sensitive columns
    for col in SENSITIVE_COLS:
        result = re.sub(rf"(?i)({col}\s*[=:]\s*)([^\n,}}]+)", r"\1[REDACTED]", result)

    # Limit output size
    if len(result) > 3000:
        result = result[:3000] + "\n[truncated]"

    return result or "Query returned no results."


@tool
def get_database_schema() -> str:
    """
    Return the schema of available tables including column names and sample rows.
    Use this before writing queries to understand the data structure.
    """
    return db.get_table_info()
```

---

## Pattern 3 — Pandas / DataFrame Agent

**Use when:** Data is a CSV, Excel file, or already a DataFrame in memory.

### Safety Warning

> `create_pandas_dataframe_agent` requires `allow_dangerous_code=True`. This flag
> exists because the agent executes arbitrary Python via `exec()`. The LLM could
> theoretically write `import os; os.system("rm -rf /")` if instructed to.
>
> **Mitigation:** Use this only on trusted datasets in controlled environments.
> For production or user-facing applications, use the E2B sandboxed alternative
> shown below.

```python
# agents/pandas_agent.py
from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from langchain_anthropic import ChatAnthropic
from langchain_experimental.agents import create_pandas_dataframe_agent

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

# Load your data — let pandas infer types automatically
# parse_dates detects date columns; this helps the LLM write date filters
df = pd.read_csv("sales_data.csv", parse_dates=True, infer_datetime_format=True)

# Print describe() output so you can see what the LLM will see in its context
print("DataFrame shape:", df.shape)
print("\nColumn types:\n", df.dtypes)
print("\nSample statistics:\n", df.describe())

# Create the agent
# allow_dangerous_code=True is required — the agent generates Python code and executes it
# verbose=True shows the generated code — always enable during development
agent = create_pandas_dataframe_agent(
    llm,
    df,
    agent_type="tool-calling",   # newer approach: LLM uses tool_calls, not ReAct strings
    verbose=True,
    allow_dangerous_code=True,   # required; see safety warning above
    prefix="""You are a data analyst. You have access to a pandas DataFrame called `df`.
Always check df.dtypes and df.columns before writing code.
Do not modify the DataFrame — only read from it.
When showing results, include specific numbers."""
)

# Run a question
result = agent.invoke({"input": "What is the average order value by region?"})
print(result["output"])
```

### Safer Alternative — E2B Code Interpreter

For production or untrusted environments, run generated code in an isolated sandbox:

```python
# Install: pip install e2b-code-interpreter
# E2B runs code in a container — LLM cannot access your filesystem or network

from e2b_code_interpreter import Sandbox

def analyze_dataframe_safely(df: pd.DataFrame, question: str) -> str:
    """
    E2B pattern: serialize DataFrame to CSV, send to sandbox, run generated code.
    The sandbox is a fresh container — no access to your system.
    """
    from langchain_anthropic import ChatAnthropic
    from langchain_core.prompts import ChatPromptTemplate

    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

    # Describe the DataFrame so the LLM knows the schema
    schema_desc = f"Columns: {list(df.columns)}\nTypes:\n{df.dtypes}\nShape: {df.shape}"

    prompt = ChatPromptTemplate.from_messages([
        ("system", f"""Write Python code to answer the question using a pandas DataFrame `df`.
The DataFrame is already loaded. Schema:
{schema_desc}
Return ONLY the Python code. The last line must print the answer."""),
        ("human", "{question}")
    ])

    chain = prompt | llm
    code_response = chain.invoke({"question": question})
    code = code_response.content.strip().strip("```python").strip("```")

    # Run in isolated sandbox
    with Sandbox() as sandbox:
        csv_data = df.to_csv(index=False)
        # Upload data and run
        execution = sandbox.run_code(
            f"import pandas as pd\nimport io\ndf = pd.read_csv(io.StringIO({repr(csv_data)}))\n{code}"
        )
        return execution.text or str(execution.results)
```

### Excel Multi-Sheet Pattern

```python
# Load all sheets from an Excel file and create one agent per sheet
import pandas as pd

sheets = pd.read_excel("quarterly_report.xlsx", sheet_name=None)
# sheets is a dict: {"Q1": DataFrame, "Q2": DataFrame, ...}

# Log what we loaded
for name, sheet_df in sheets.items():
    print(f"Sheet '{name}': {sheet_df.shape[0]} rows, {sheet_df.shape[1]} columns")

# Create agent with multiple DataFrames
# create_pandas_dataframe_agent accepts a list — each becomes a named variable
agent = create_pandas_dataframe_agent(
    llm,
    list(sheets.values()),
    agent_type="tool-calling",
    verbose=True,
    allow_dangerous_code=True,
    prefix=f"""You have {len(sheets)} DataFrames: {list(sheets.keys())}.
They are named df1, df2, etc. in order.
Use pd.concat() to combine across sheets when needed."""
)
```

---

## Pattern 4 — OpenAPI / REST API Agent

**Use when:** You need to query a REST API using natural language.

```python
# agents/api_agent.py
from dotenv import load_dotenv
load_dotenv()

import os
import json
import yaml
from langchain_anthropic import ChatAnthropic
from langchain_community.agent_toolkits.openapi import planner
from langchain_community.agent_toolkits.openapi.spec import reduce_openapi_spec
from langchain_community.utilities import RequestsWrapper

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

# Load the OpenAPI spec — supports JSON or YAML
# reduce_openapi_spec trims the spec to what fits in context
with open("openapi_spec.yaml") as f:
    raw_spec = yaml.safe_load(f)

reduced_spec = reduce_openapi_spec(raw_spec)

# Auth headers — never hardcode; always read from environment
headers = {
    "Authorization": f"Bearer {os.getenv('API_TOKEN')}",
    "Content-Type": "application/json",
}

# RequestsWrapper handles rate limiting, headers, and response parsing
requests_wrapper = RequestsWrapper(headers=headers)

# Build the agent — it will plan multi-step API calls automatically
agent = planner.create_openapi_agent(
    reduced_spec,
    requests_wrapper,
    llm,
    verbose=True,
    # allow_dangerous_requests must be True because the agent calls real HTTP endpoints
    allow_dangerous_requests=True,
)

result = agent.invoke({
    "input": "Get all open support tickets assigned to user ID 42"
})
print(result["output"])
```

### Rate Limit Handling

```python
# Wrap any API tool with exponential backoff
import time
from functools import wraps
from langchain_core.tools import ToolException

def with_retry(max_retries: int = 3, backoff_seconds: float = 1.0):
    """Decorator that retries on rate limit errors with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "rate limit" in str(e).lower() or "429" in str(e):
                        wait = backoff_seconds * (2 ** attempt)
                        print(f"Rate limited. Waiting {wait}s before retry {attempt + 1}...")
                        time.sleep(wait)
                    else:
                        raise ToolException(f"API error: {e}") from e
            raise ToolException(f"Max retries ({max_retries}) exceeded.")
        return wrapper
    return decorator
```

---

## Pattern 5 — CSV / Excel Ingestion Pipeline

**Use when:** Loading files into a vector store or structured pipeline for retrieval.

```python
# pipelines/csv_ingestion.py
from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from langchain_community.document_loaders import CSVLoader
from langchain_core.documents import Document
from typing import List

def load_csv_with_metadata(filepath: str, metadata_columns: List[str]) -> List[Document]:
    """
    Load a CSV where some columns become document content and others become metadata.

    Why metadata columns? When you store documents in a vector store, metadata
    enables fast filtering without semantic search. For example: filter by
    region='APAC' before doing a similarity search on the content.

    Args:
        filepath: Path to the CSV file.
        metadata_columns: Column names to promote to document metadata.
                          These become filterable fields in the vector store.
    """
    df = pd.read_csv(filepath)

    # Schema validation — fail fast rather than loading corrupt data
    for col in metadata_columns:
        if col not in df.columns:
            raise ValueError(f"Metadata column '{col}' not found in CSV. "
                             f"Available columns: {list(df.columns)}")

    documents = []
    content_columns = [c for c in df.columns if c not in metadata_columns]

    for idx, row in df.iterrows():
        # Build the document content from non-metadata columns
        content_parts = [f"{col}: {row[col]}" for col in content_columns]
        content = "\n".join(content_parts)

        # Build metadata from designated metadata columns
        metadata = {col: str(row[col]) for col in metadata_columns}
        metadata["source"] = filepath
        metadata["row_index"] = idx

        documents.append(Document(page_content=content, metadata=metadata))

    print(f"Loaded {len(documents)} documents from {filepath}")
    return documents


def validate_schema(df: pd.DataFrame, required_columns: List[str]) -> None:
    """Check that all required columns exist and have no fully-null columns."""
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    null_columns = [c for c in required_columns if df[c].isna().all()]
    if null_columns:
        raise ValueError(f"Columns are entirely null: {null_columns}")


# Example usage:
# docs = load_csv_with_metadata(
#     "customers.csv",
#     metadata_columns=["region", "tier", "account_status"]
# )
```

---

## Pattern 6 — Multi-Source Agent (SQL + Documents)

**Use when:** Questions might be answered by either a database or a document store,
and you need the agent to decide which to use — or combine both.

```python
# agents/multi_source_agent.py
from dotenv import load_dotenv
load_dotenv()

import os
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool, ToolException
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_community.utilities import SQLDatabase
import sqlglot

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

# --- Tool 1: SQL ---
db = SQLDatabase.from_uri(
    os.getenv("DATABASE_URL"),
    include_tables=["orders", "customers"],
    sample_rows_in_table_info=2,
)

@tool
def query_structured_data(sql: str) -> str:
    """
    Query the orders and customers database with SQL.
    Use for: order counts, revenue figures, customer stats, date ranges.
    Always use SELECT only. LIMIT is added automatically.

    Args:
        sql: A SQL SELECT query.
    """
    try:
        statements = sqlglot.parse(sql.strip())
        if not statements or type(statements[0]).__name__ != "Select":
            raise ToolException("Only SELECT queries are allowed.")
        # Add limit if missing
        stmt = statements[0]
        if not stmt.args.get("limit"):
            stmt = stmt.limit(100)
        result = db.run(stmt.sql(dialect="postgres"))
        return result or "No results."
    except ToolException:
        raise
    except Exception as e:
        raise ToolException(f"Database error: {e}")


# --- Tool 2: Document search (placeholder — swap in your vector store) ---
@tool
def search_documents(query: str) -> str:
    """
    Search internal documents, policies, and knowledge base articles.
    Use for: policy questions, how-to guides, product descriptions,
    anything that requires understanding context rather than exact numbers.

    Args:
        query: A natural language search query.
    """
    # Replace this with your actual retriever
    # Example: vectorstore.similarity_search(query, k=4)
    raise ToolException("Document store not configured. Set up a vector store retriever.")


# --- Router prompt ---
# The system message teaches the agent when to use each tool.
# Without this guidance, agents over-rely on whichever tool they tried first.
system_prompt = """You are a data analyst with access to two sources:

1. **Structured database** (query_structured_data): Use for exact numbers,
   counts, revenue, dates, customer records. Requires precise SQL.

2. **Document search** (search_documents): Use for policies, procedures,
   explanations, product descriptions. Returns text passages.

Decision rule:
- "How many...", "What is the total...", "Show me all..." → database
- "What is our policy on...", "How do I...", "Explain..." → documents
- Questions needing both → call both tools, then synthesize

Always cite which source(s) you used in your final answer."""

prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    MessagesPlaceholder(variable_name="chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),
])

tools = [query_structured_data, search_documents]

# create_tool_calling_agent uses the model's native tool_calls feature
# This is more reliable than ReAct string parsing
agent = create_tool_calling_agent(llm, tools, prompt)

executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    handle_parsing_errors=True,  # recover gracefully from malformed tool calls
    max_iterations=6,            # prevent infinite loops
)

# Example usage
result = executor.invoke({
    "input": "How many orders came from enterprise customers last month, "
             "and what is our refund policy for enterprise accounts?"
})
print(result["output"])
```

---

## Concept Reference

| Concept | What it is | When it matters |
|---|---|---|
| `SQLDatabase.from_uri()` | Wrapper that connects to a DB and extracts schema | Every SQL pattern — it provides `get_table_info()` and `run()` |
| `include_tables` | Limits which tables appear in the LLM's schema context | Always set this — never expose your full schema |
| `sqlglot.parse()` | AST parser that understands SQL structure | Security — use instead of string matching |
| `ToolException` | Exception type that LangChain agents handle gracefully | Raise this in tools to trigger agent retry logic |
| `StateGraph` | LangGraph graph builder with typed state | Any multi-step workflow with branching logic |
| Conditional edges | Router functions that return the next node name | Retry loops, error recovery, branching |
| `recursion_limit` | Max steps before LangGraph raises an error | Prevents infinite loops in retry graphs |
| `allow_dangerous_code` | Enables `exec()` in Pandas agent | Required for Pandas agent — understand the risk |
| `sample_rows_in_table_info` | Adds N example rows to schema for LLM context | Improves SQL accuracy, set to 2-3 |
| `reduce_openapi_spec` | Trims large OpenAPI specs to fit LLM context | Required for large API specs |

---

## Common Mistakes

| Mistake | What goes wrong | Fix |
|---|---|---|
| String matching for SQL safety (`"DROP" in sql`) | Can be bypassed with comments, mixed case, Unicode | Use `sqlglot.parse()` AST analysis — checks statement type |
| Injecting full DB schema into prompt | Context overflow, LLM confused by irrelevant tables | Use `include_tables=[...]` to limit to relevant tables only |
| No `LIMIT` on SQL queries | LLM writes `SELECT * FROM large_table`, returns millions of rows | Auto-inject `LIMIT 100` via sqlglot AST rewrite |
| `allow_dangerous_code` in production | LLM executes arbitrary Python on your server | Use E2B sandbox pattern for untrusted environments |
| No `recursion_limit` on LangGraph | A bug in routing causes infinite loops, burns API tokens | Always set `recursion_limit=20` or lower |
| Hardcoded connection strings | Credentials in source code | Always use `os.getenv()` and `.env` via `load_dotenv()` |
| Exposing sensitive columns in results | SSN, card numbers appear in LLM context and logs | Mask at query level or use DB views with columns omitted |
| Single statement check omitted | LLM writes `SELECT 1; DROP TABLE users` (two statements) | Check `len(sqlglot.parse(sql)) == 1` before executing |

---

## Security Checklist

Before deploying any SQL agent to production, verify:

- [ ] Connection string is read-only (PostgreSQL `default_transaction_read_only`, or read-only DB user)
- [ ] `include_tables` is set — LLM cannot see tables it should not know about
- [ ] All queries pass `sqlglot` AST validation before execution
- [ ] Single-statement check: `len(statements) == 1`
- [ ] `LIMIT` is injected if missing
- [ ] Sensitive columns are masked in results
- [ ] `recursion_limit` is set on all LangGraph graphs
- [ ] LangSmith tracing is on — you can audit every query the LLM generated

---

## Transitions

After scaffolding a data agent, offer:

- `/lc-agent` — Add memory, checkpointing, or human-in-the-loop approval before queries execute
- `/lc-trace` — Set up LangSmith to audit every SQL query the LLM generates
- `/lc-test` — Write tests for the validation loop and tool safety checks
- `/lc-deploy` — Deploy the agent as an API endpoint
- `/rag` — Add document search alongside SQL (multi-source pattern)
