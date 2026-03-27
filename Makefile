# GraphRAG — Local-First Development Workflow
#
# Usage:
#   make export        — snapshot Delta tables to local DuckDB
#   make test          — quick single-question smoke test
#   make validate      — run full test suite with quality gates
#   make parity        — compare local vs Databricks responses
#   make deploy        — validate + log + smoke-test + deploy
#   make deploy-force  — skip validation, deploy immediately

.PHONY: export test validate parity deploy deploy-force test-endpoint help

PYTHON ?= python

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

export: ## Export Delta tables to local DuckDB (data/graphrag.duckdb)
	$(PYTHON) scripts/export_local_data.py

test: ## Quick single-question agent test (local backend)
	$(PYTHON) scripts/test_local.py "Who is Abraham?"

validate: ## Run full test suite locally with quality gates
	$(PYTHON) scripts/validate_local.py

parity: ## Compare local vs Databricks backend responses
	$(PYTHON) scripts/validate_parity.py

deploy: ## Validate locally, then log + deploy to Model Serving
	$(PYTHON) scripts/redeploy_agent.py --validate

deploy-force: ## Deploy without local validation
	$(PYTHON) scripts/redeploy_agent.py --no-validate

test-endpoint: ## Test the deployed endpoint
	$(PYTHON) scripts/test_endpoint.py
