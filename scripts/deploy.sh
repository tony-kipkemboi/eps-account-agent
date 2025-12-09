#!/bin/bash
# Deploy EPS Agent to Databricks using Asset Bundles
# Usage: ./scripts/deploy.sh [dev|staging|prod]

set -e

TARGET=${1:-dev}

echo "🚀 Deploying EPS Agent to: $TARGET"
echo "================================================"

# Validate first
echo "📋 Validating bundle..."
databricks bundle validate --target "$TARGET"

# Deploy
echo "📦 Deploying..."
databricks bundle deploy --target "$TARGET"

# Show summary
echo ""
echo "✅ Deployment complete!"
databricks bundle summary --target "$TARGET"

