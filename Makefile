SHELL := /bin/bash
# Per-working-copy deploy identity (gitignored) - same file scripts/deploy-helm.sh
# sources; here it feeds NAMESPACE and friends into the helm targets.
-include .env.deploy
COMPOSE_DEV  := docker compose -f compose.base.yml -f compose.dev.yml
COMPOSE_PROD := docker compose -f compose.base.yml -f compose.prod.yml

# K8s namespace of the release, and the deployments running a rebuilt platform
# image (so a `latest`-tag rebuild is actually picked up). Override NAMESPACE to
# match a non-default `deploy-helm.sh deploy <release> <namespace>`.
NAMESPACE     ?= openvisor
IMAGE_DEPLOYS := api worker beat mcp deployer app landing
# amd64 by default so `make prod-build` images run on the amd64 K8s nodes (and,
# emulated, on an arm64 dev machine) - one artifact for compose and Kubernetes.
IMAGE_PLATFORM ?= linux/amd64
# services whose image we build+push (postgres/redis/... are upstream images)
PROD_IMAGES   := api mcp donsetch-mcp deployer app landing runner

.PHONY: dev prod prod-build prod-push stop down logs ps migrate makemigration seed psql test build-dev build-runner helm-deploy helm-uninstall helm-build-push helm-bpdr

dev: build-runner ## Build and start the full platform in local mode
	$(COMPOSE_DEV) up -d --build

build-dev:
	$(COMPOSE_DEV) build

build-runner: ## Build the sandboxed dev-runner image (deployer /dev/run needs it; not a compose service)
	@set -a; [ -f .env ] && . .env; set +a; \
	docker build -t "$${COMPOSE_PROJECT_NAME:-openvisor}-runner" ./runner

prod: prod-build ## Build and start the platform in production mode
	$(COMPOSE_PROD) up -d

prod-build: ## Build all platform images with compose.prod.yml (registry) names for IMAGE_PLATFORM
	DOCKER_DEFAULT_PLATFORM=$(IMAGE_PLATFORM) $(COMPOSE_PROD) --profile build-only build

prod-push: ## Push the prod-built platform images to the registry (docker login first)
	$(COMPOSE_PROD) push $(PROD_IMAGES)

prod-down: ## Build and start the platform in production mode
	$(COMPOSE_PROD) down

stop:
	$(COMPOSE_DEV) stop

down:
	$(COMPOSE_DEV) down

logs:
	$(COMPOSE_DEV) logs -f --tail=100 $(S)

ps:
	$(COMPOSE_DEV) ps

migrate: ## Apply alembic migrations
	$(COMPOSE_DEV) exec api alembic upgrade head

makemigration: ## Autogenerate a migration: make makemigration M="message"
	$(COMPOSE_DEV) exec api alembic revision --autogenerate -m "$(M)"

seed: ## Seed admin user + org from env
	$(COMPOSE_DEV) exec api python -m app.seed

psql:
	$(COMPOSE_DEV) exec postgres psql -U openvisor openvisor

test:
	$(COMPOSE_DEV) exec api pytest -q

helm-build-push: ## Build + push all platform images for the cluster (IMAGE_REGISTRY/IMAGE_TAG/IMAGE_PLATFORM/DEPLOY_DOMAIN)
	./scripts/deploy-helm.sh build-push

helm-deploy: ## Deploy/upgrade the Helm release (see scripts/deploy-helm.sh header for vars)
	./scripts/deploy-helm.sh deploy

helm-uninstall: ## Uninstall the Helm release (namespace and CRDs kept)
	./scripts/deploy-helm.sh uninstall

helm-bpdr: ## Full cluster redeploy: build+push images, deploy (runs migrations), rollout-restart to pull new :latest
	./scripts/deploy-helm.sh build-push
	./scripts/deploy-helm.sh deploy
	kubectl -n $(NAMESPACE) rollout restart deployment $(IMAGE_DEPLOYS)
	@for d in $(IMAGE_DEPLOYS); do kubectl -n $(NAMESPACE) rollout status deployment/$$d --timeout=300s; done
