# GraphRAG — Local-First Development Workflow
#
# Usage:
#   make export        — snapshot Delta tables to local DuckDB
#   make test          — quick single-question smoke test
#   make validate      — run full test suite with quality gates
#   make parity        — compare local vs Databricks responses
#   make deploy        — validate + log + smoke-test + deploy
#   make deploy-force  — skip validation, deploy immediately

.PHONY: export export-enron test test-unit test-app test-enron test-enron-integration \
       build-metadata validate parity \
       bundle-validate bundle-deploy local-all \
       deploy deploy-force deploy-all test-endpoint help \
       preflight preflight-full deploy-confident

PYTHON ?= python

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

export: ## Export Delta tables to local DuckDB (data/graphrag.duckdb)
	$(PYTHON) scripts/export_local_data.py

export-enron: ## Export Enron Delta tables to local DuckDB
	$(PYTHON) scripts/export_local_data.py --corpus enron

test-enron: ## Run Enron agent unit tests (no DuckDB needed)
	$(PYTHON) -m pytest tests/test_enron_agent.py -m "not integration" -v

test-enron-integration: ## Run Enron agent integration tests (needs DuckDB)
	$(PYTHON) -m pytest tests/test_enron_agent.py -m integration -v

build-metadata: ## Build metadata tables locally via DuckDB scripts
	$(PYTHON) scripts/build_extraction_provenance.py
	$(PYTHON) scripts/build_email_classification.py
	$(PYTHON) scripts/build_data_quality.py
	$(PYTHON) scripts/build_person_identity.py

test: ## Quick single-question agent test (local backend)
	$(PYTHON) scripts/test_local.py "Who is Abraham?"

test-unit: ## Run pytest Layer 1 (graph engine, no LLM)
	$(PYTHON) -m pytest tests/test_graph_engine.py -m "not integration and not baseline" -v

test-app: ## Start local Dash app and run Playwright E2E (mock mode)
	USE_MOCK_BACKEND=true $(PYTHON) tests/test_local_app.py

validate: ## Run full test suite locally with quality gates
	$(PYTHON) scripts/validate_local.py

parity: ## Compare local vs Databricks backend responses
	$(PYTHON) scripts/validate_parity.py

bundle-validate: ## Validate Databricks bundle config (no deploy)
	databricks bundle validate --target dev

local-all: test-unit validate test-app ## Full local validation pipeline
	@echo ""
	@echo "  ALL LOCAL VALIDATIONS PASSED — safe to deploy"
	@echo ""

deploy: ## Validate locally, then log + deploy to Model Serving
	$(PYTHON) scripts/redeploy_agent.py --validate

deploy-force: ## Deploy without local validation
	$(PYTHON) scripts/redeploy_agent.py --no-validate

bundle-deploy: validate bundle-validate ## Validate locally + bundle validate, then deploy bundle
	databricks bundle deploy --target dev

deploy-all: local-all bundle-validate ## Full local validation + Model Serving deploy + bundle deploy
	$(PYTHON) scripts/redeploy_agent.py --no-validate
	databricks bundle deploy --target dev

preflight: ## Full deployment confidence check (all 8 layers)
	$(PYTHON) scripts/preflight.py

preflight-full: ## Full preflight including parity check (all 9 layers)
	$(PYTHON) scripts/preflight.py --parity

deploy-confident: preflight bundle-validate ## Preflight + Model Serving deploy + bundle deploy
	$(PYTHON) scripts/redeploy_agent.py --no-validate
	databricks bundle deploy --target dev

test-endpoint: ## Test the deployed endpoint
	$(PYTHON) scripts/test_endpoint.py
