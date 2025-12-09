# Databricks notebook source
# MAGIC %md
# MAGIC # EPS Account Intelligence Agent - Deployment
# MAGIC 
# MAGIC This notebook deploys the EPS Agent to Unity Catalog and Model Serving.
# MAGIC 
# MAGIC **Prerequisites:**
# MAGIC 1. Secrets created in scope `eps_agent` (GLEAN_API_TOKEN, GLEAN_INSTANCE)
# MAGIC 2. Schema exists: `eps_intelligence.agents`
# MAGIC 3. `eps_agent.py` uploaded to same folder as this notebook

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: Install Dependencies

# COMMAND ----------

# MAGIC %pip install -U -qqqq databricks-agents mlflow databricks-openai httpx backoff

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: Configuration

# COMMAND ----------

# Unity Catalog model path
CATALOG = "eps_intelligence"
SCHEMA = "agents"
MODEL_NAME = "eps_account_agent"
UC_MODEL_NAME = f"{CATALOG}.{SCHEMA}.{MODEL_NAME}"

# Secret scope name
SECRET_SCOPE = "eps_agent"

# LLM endpoint (Databricks-hosted)
LLM_ENDPOINT = "databricks-gpt-5-mini"

print(f"Model will be registered to: {UC_MODEL_NAME}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Verify Secrets

# COMMAND ----------

try:
    glean_token = dbutils.secrets.get(SECRET_SCOPE, "GLEAN_API_TOKEN")
    glean_instance = dbutils.secrets.get(SECRET_SCOPE, "GLEAN_INSTANCE")
    print(f"✓ Secrets found in scope '{SECRET_SCOPE}'")
    print(f"  - GLEAN_INSTANCE: {glean_instance}")
except Exception as e:
    print(f"✗ Error: {e}")
    print(f"\nCreate secrets with Databricks CLI:")
    print(f"  databricks secrets create-scope {SECRET_SCOPE}")
    print(f"  databricks secrets put-secret {SECRET_SCOPE} GLEAN_API_TOKEN")
    print(f"  databricks secrets put-secret {SECRET_SCOPE} GLEAN_INSTANCE")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4: Set Environment Variables

# COMMAND ----------

import os

# Load secrets into environment for agent initialization
os.environ["GLEAN_API_TOKEN"] = dbutils.secrets.get(SECRET_SCOPE, "GLEAN_API_TOKEN")
os.environ["GLEAN_INSTANCE"] = dbutils.secrets.get(SECRET_SCOPE, "GLEAN_INSTANCE")
os.environ["LLM_ENDPOINT"] = LLM_ENDPOINT

print("✓ Environment variables set")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5: Test Agent Locally (Optional)

# COMMAND ----------

# Uncomment to test the agent before deployment
# from eps_agent import AGENT
# 
# result = AGENT.predict({
#     "input": [{"role": "user", "content": "When is AdventHealth's renewal date?"}]
# })
# print(result.model_dump(exclude_none=True))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: Log Agent to MLflow

# COMMAND ----------

import mlflow

# Set Unity Catalog as the model registry
mlflow.set_registry_uri("databricks-uc")

# Get the path to the agent file (same folder as this notebook)
import os
notebook_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
folder_path = "/".join(notebook_path.split("/")[:-1])
agent_file_path = f"/Workspace{folder_path}/eps_agent.py"

print(f"Agent file: {agent_file_path}")

# COMMAND ----------

with mlflow.start_run(run_name="eps_account_agent_v1") as run:
    logged_agent_info = mlflow.pyfunc.log_model(
        artifact_path="agent",
        python_model=agent_file_path,
        pip_requirements=[
            "databricks-openai",
            "httpx>=0.25.0",
            "mlflow>=3.1.0",
            "backoff>=2.2.0",
        ],
    )
    
    run_id = run.info.run_id
    print(f"✓ Agent logged!")
    print(f"  Run ID: {run_id}")
    print(f"  Model URI: {logged_agent_info.model_uri}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7: Validate Model (Pre-deployment Check)

# COMMAND ----------

# Test the logged model before deployment
mlflow.models.predict(
    model_uri=f"runs:/{run_id}/agent",
    input_data={"input": [{"role": "user", "content": "Hello, what can you help me with?"}]},
    env_manager="uv",
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8: Register to Unity Catalog

# COMMAND ----------

uc_registered_model_info = mlflow.register_model(
    model_uri=logged_agent_info.model_uri, 
    name=UC_MODEL_NAME
)

print(f"✓ Model registered to Unity Catalog!")
print(f"  Model: {UC_MODEL_NAME}")
print(f"  Version: {uc_registered_model_info.version}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9: Deploy to Model Serving

# COMMAND ----------

from databricks import agents

deployment = agents.deploy(
    model_name=UC_MODEL_NAME,
    model_version=uc_registered_model_info.version,
    environment_vars={
        "GLEAN_API_TOKEN": f"{{{{secrets/{SECRET_SCOPE}/GLEAN_API_TOKEN}}}}",
        "GLEAN_INSTANCE": f"{{{{secrets/{SECRET_SCOPE}/GLEAN_INSTANCE}}}}",
        "LLM_ENDPOINT": LLM_ENDPOINT,
    },
    tags={"project": "eps_intelligence", "version": "1.0"},
)

print(f"✓ Agent deployed!")
print(f"  Endpoint: {deployment.endpoint_name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 10: Test Deployed Endpoint

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

# Wait a moment for endpoint to be ready
import time
print("Waiting for endpoint to be ready...")
time.sleep(30)

# Query the deployed agent
response = w.serving_endpoints.query(
    name=deployment.endpoint_name,
    input={"input": [{"role": "user", "content": "When is AdventHealth's renewal?"}]}
)

print("Response:")
print(response)

# COMMAND ----------

# MAGIC %md
# MAGIC ## ✅ Deployment Complete!
# MAGIC 
# MAGIC **Next Steps:**
# MAGIC 1. Go to **AI Playground** and select your endpoint to chat with the agent
# MAGIC 2. Share the endpoint with your team
# MAGIC 3. Monitor traces in MLflow
# MAGIC 
# MAGIC **Endpoint URL:** Check Model Serving UI for the REST API endpoint

