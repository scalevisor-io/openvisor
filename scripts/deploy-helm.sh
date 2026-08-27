#!/bin/sh
# OCPA-style Helm deployment script (OCPA convention: one compose/Helm layout per
# repo, secrets only via env vars, crash on missing required vars):
# renders k8s/values.example.yaml with envsubst into the gitignored
# k8s/values.generated.yaml, then installs/upgrades the release.
#
# Usage: ./scripts/deploy-helm.sh <deploy|uninstall|build-push> [release] [namespace]
#
# deploy vars (environment, NOT .env - the .env stays the compose identity and is
# shipped whole to the cluster as the backend env Secret):
#   DEPLOY_DOMAIN        public domain (default openvisor.example.com; use <LB-IP>.nip.io on test clusters)
#   DEPLOY_ENV           production|local (default production)
#   IMAGE_REGISTRY       registry prefix for the platform images (default registry.example.com/openvisor)
#   IMAGE_TAG            image tag (default latest)
#   GATEWAY_TLS_ENABLED  true|false (default false); GATEWAY_TLS_SECRET (default openvisor-wildcard-tls)
#   MAILPIT_ENABLED      true|false (default false) - dev mail sink + mail.<domain> route
#   STRIPE_RELAY_ENABLED true|false (default false) - run the stripe-cli webhook
#     relay (outbound `stripe listen`); lands top-up credits without an inbound
#     dashboard webhook, so it works behind a firewalled ingress. Needs a real sk_ key.
#   POSTGRES_USER/PASSWORD/DB (default openvisor), STORAGE_CLASS (default cluster default)
#   DEMO_CPU_LIMIT (1), DEMO_MEM_LIMIT_K8S (2Gi), DEMO_RUNTIME ("" = privileged DinD)
#   DEMO_HEALTHCHECK_TIMEOUT (60) - seconds the demo app gets to answer HTTP after
#     compose up before the start fails (0 disables the readiness gate)
#   REGISTRY_SERVER/REGISTRY_USER/REGISTRY_PASSWORD  pull-secret creds for private registries
#   FORCE_INSECURE=1  allow DEPLOY_ENV=production over plain HTTP (normally refused)
# build-push extra vars: IMAGE_PLATFORM (linux/amd64), APP_URL (landing build arg,
#   default <scheme>://app.<DEPLOY_DOMAIN>), SITE_URL (landing canonical origin,
#   default <scheme>://<DEPLOY_DOMAIN>). The landing's BRAND_NAME/CONSULTANT_NAME/
#   BRAND_COLOR_* build args are read from PROJECT_DIR/.env by compose.
#
# .env note: it is shipped verbatim into the openvisor-env Secret via
# `kubectl create secret --from-env-file`, which takes everything after `=`
# literally - keep values UNQUOTED with no trailing inline comments (unlike
# docker compose, which strips surrounding quotes and ` # comments`). The env
# Secret must carry MEILI_MASTER_KEY: the in-cluster meilisearch StatefulSet pulls
# that key from it, so a missing key fails the deploy loudly (OCPA). Run a one-time
# KB reindex after the first deploy (POST /api/admin/knowledge/reindex).
# A gitignored .env.k8s next to .env overrides individual keys for the cluster
# (e.g. GIT_EXTRA_HOST pointing at the Tailscale egress Service instead of the
# raw tailnet IP - see k8s/tailscale-egress.example.yaml).

set -e

ACTION="${1:?ERROR: Action required (deploy|uninstall|build-push)}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# .env.deploy: gitignored per-working-copy deploy identity (domain, TLS, registry,
# postgres, release/namespace...). Sourced BEFORE the defaults so a working copy
# pins its cluster identity durably instead of relying on shell exports - the
# GATEWAY_TLS_ENABLED-forgotten-in-the-shell class of mistake. Values in the file
# win over the defaults below; explicit CLI args ($2/$3) still win over the file.
if [ -f "$PROJECT_DIR/.env.deploy" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJECT_DIR/.env.deploy"
  set +a
fi

RELEASE_NAME="${2:-${RELEASE_NAME:-openvisor}}"
NAMESPACE="${3:-${NAMESPACE:-openvisor}}"

CHART_DIR="$PROJECT_DIR/k8s"
VALUES_EXAMPLE="$CHART_DIR/values.example.yaml"
VALUES_FILE="$CHART_DIR/values.generated.yaml"

GATEWAY_API_CRDS="https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.0/standard-install.yaml"
TRAEFIK_CRDS="https://raw.githubusercontent.com/traefik/traefik/v3.6/docs/content/reference/dynamic-configuration/kubernetes-crd-definition-v1.yml"

# deploy-var defaults (environment overrides win)
DEPLOY_DOMAIN="${DEPLOY_DOMAIN:-openvisor.example.com}"
DEPLOY_ENV="${DEPLOY_ENV:-production}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-registry.example.com/openvisor}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
GATEWAY_TLS_ENABLED="${GATEWAY_TLS_ENABLED:-false}"
GATEWAY_TLS_SECRET="${GATEWAY_TLS_SECRET:-openvisor-wildcard-tls}"
MAILPIT_ENABLED="${MAILPIT_ENABLED:-false}"
STRIPE_RELAY_ENABLED="${STRIPE_RELAY_ENABLED:-false}"
POSTGRES_USER="${POSTGRES_USER:-openvisor}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-openvisor}"
POSTGRES_DB="${POSTGRES_DB:-openvisor}"
STORAGE_CLASS="${STORAGE_CLASS:-}"
DEMO_CPU_LIMIT="${DEMO_CPU_LIMIT:-1}"
DEMO_MEM_LIMIT_K8S="${DEMO_MEM_LIMIT_K8S:-2Gi}"
DEMO_RUNTIME="${DEMO_RUNTIME:-}"
DEMO_HEALTHCHECK_TIMEOUT="${DEMO_HEALTHCHECK_TIMEOUT:-60}"
if [ "$GATEWAY_TLS_ENABLED" = "true" ]; then HTTP_SCHEME="${HTTP_SCHEME:-https}"; else HTTP_SCHEME="${HTTP_SCHEME:-http}"; fi
export DEPLOY_DOMAIN DEPLOY_ENV IMAGE_REGISTRY IMAGE_TAG GATEWAY_TLS_ENABLED GATEWAY_TLS_SECRET \
    MAILPIT_ENABLED STRIPE_RELAY_ENABLED POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB STORAGE_CLASS \
    DEMO_CPU_LIMIT DEMO_MEM_LIMIT_K8S DEMO_RUNTIME DEMO_HEALTHCHECK_TIMEOUT HTTP_SCHEME

substitute() {
    # envsubst when available (Linux); perl fallback (macOS ships no gettext)
    if command -v envsubst >/dev/null 2>&1; then
        envsubst
    else
        perl -pe 's/\$\{([A-Za-z0-9_]+)\}/defined $ENV{$1} ? $ENV{$1} : ""/ge'
    fi
}

case "$ACTION" in
    deploy)
        [ -f "$VALUES_EXAMPLE" ] || { echo "ERROR: $VALUES_EXAMPLE not found"; exit 1; }
        [ -f "$PROJECT_DIR/.env" ] || { echo "ERROR: $PROJECT_DIR/.env not found (backend env Secret source)"; exit 1; }

        # production over plain HTTP = secure session cookies never sent -> nobody
        # can log in, and https:// demo links are dead. Refuse unless forced.
        if [ "$DEPLOY_ENV" = "production" ] && [ "$HTTP_SCHEME" = "http" ] && [ "${FORCE_INSECURE:-0}" != "1" ]; then
            echo "ERROR: DEPLOY_ENV=production with HTTP (GATEWAY_TLS_ENABLED=false) is not loginnable."
            echo "       Set GATEWAY_TLS_ENABLED=true (paste a wildcard cert), or DEPLOY_ENV=local for HTTP tests,"
            echo "       or FORCE_INSECURE=1 to override."
            exit 1
        fi

        echo "INFO: Rendering $VALUES_FILE (domain: $DEPLOY_DOMAIN, scheme: $HTTP_SCHEME, env: $DEPLOY_ENV)"
        substitute < "$VALUES_EXAMPLE" > "$VALUES_FILE"

        echo "INFO: Installing Gateway API + Traefik CRDs (idempotent)"
        # --force-conflicts: adopt fields another controller may already manage
        # (e.g. a cluster where the GKE/Cilium operator installed the CRDs)
        kubectl apply --server-side --force-conflicts -f "$GATEWAY_API_CRDS"
        kubectl apply --server-side --force-conflicts -f "$TRAEFIK_CRDS"

        kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

        echo "INFO: Syncing backend env Secret from .env"
        # Optional gitignored .env.k8s overlay: cluster-specific overrides for
        # env vars whose compose value is wrong in-cluster (e.g. GIT_EXTRA_HOST
        # must point at the Tailscale egress Service, not the raw tailnet IP).
        # Same key wins from the later file; comments/blank lines are dropped
        # (kubectl --from-env-file skips them anyway).
        ENV_FILE="$PROJECT_DIR/.env"
        ENV_TMP=""
        if [ -f "$PROJECT_DIR/.env.k8s" ]; then
            echo "INFO: applying overlay $PROJECT_DIR/.env.k8s"
            ENV_TMP=$(mktemp)
            awk -F= '/^[A-Za-z_][A-Za-z0-9_]*=/{if (!($1 in v)) order[++n]=$1; v[$1]=$0} END{for (i=1;i<=n;i++) print v[order[i]]}' \
                "$PROJECT_DIR/.env" "$PROJECT_DIR/.env.k8s" > "$ENV_TMP"
            ENV_FILE="$ENV_TMP"
        fi
        kubectl -n "$NAMESPACE" create secret generic openvisor-env \
            --from-env-file="$ENV_FILE" --dry-run=client -o yaml | kubectl apply -f -
        if [ -n "$ENV_TMP" ]; then rm -f "$ENV_TMP"; fi

        if [ -n "${REGISTRY_PASSWORD:-}" ]; then
            echo "INFO: Syncing registry pull Secret (${REGISTRY_SERVER:-registry.example.com})"
            # build the dockerconfigjson in a temp file so the password never
            # appears in the process list (ps) as a kubectl argument
            REG_SRV="${REGISTRY_SERVER:-registry.example.com}"
            REG_USR="${REGISTRY_USER:-nologin}"
            REG_AUTH=$(printf '%s:%s' "$REG_USR" "$REGISTRY_PASSWORD" | base64 | tr -d '\n')
            REG_TMP=$(mktemp)
            trap 'rm -f "$REG_TMP"' EXIT INT TERM
            printf '{"auths":{"%s":{"username":"%s","password":"%s","auth":"%s"}}}' \
                "$REG_SRV" "$REG_USR" "$REGISTRY_PASSWORD" "$REG_AUTH" > "$REG_TMP"
            kubectl -n "$NAMESPACE" create secret generic openvisor-registry \
                --type=kubernetes.io/dockerconfigjson \
                --from-file=.dockerconfigjson="$REG_TMP" \
                --dry-run=client -o yaml | kubectl apply -f -
            rm -f "$REG_TMP"; trap - EXIT INT TERM
        elif ! kubectl -n "$NAMESPACE" get secret openvisor-registry >/dev/null 2>&1; then
            echo "WARN: no openvisor-registry pull secret and REGISTRY_PASSWORD unset - private image pulls will fail"
        fi

        # Optional gitignored operator overlay for values that don't fit env vars
        # (e.g. gateway.extraListeners / crossNamespaceRoutes for a multi-tenant
        # gateway). Applied after the generated values, so it wins.
        LOCAL_VALUES="$CHART_DIR/values.local.yaml"
        EXTRA_VALUES=""
        [ -f "$LOCAL_VALUES" ] && EXTRA_VALUES="-f $LOCAL_VALUES" && echo "INFO: applying overlay $LOCAL_VALUES"

        echo "INFO: helm upgrade --install $RELEASE_NAME ($NAMESPACE)"
        helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
            -n "$NAMESPACE" -f "$VALUES_FILE" $EXTRA_VALUES --wait --timeout 10m

        ADDR="$(kubectl -n "$NAMESPACE" get gateway -o jsonpath='{.items[0].status.addresses[0].value}' 2>/dev/null || true)"
        echo "SUCCESS: deployed. Gateway address: ${ADDR:-<pending>}"
        case "$DEPLOY_DOMAIN" in
            *nip.io|openvisor.example.com) ;;
            *) [ -n "$ADDR" ] && echo "HINT: for a test cluster redeploy with DEPLOY_DOMAIN=$ADDR.nip.io" ;;
        esac
        ;;

    uninstall)
        echo "INFO: Uninstalling $RELEASE_NAME from $NAMESPACE"
        helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" --wait || echo "WARN: release not found"
        # demo/dev-run resources are created at runtime by the deployer, not Helm
        kubectl -n "$NAMESPACE" delete pods,services,secrets,persistentvolumeclaims,jobs \
            -l 'openvisor/demo=true' --ignore-not-found 2>/dev/null || true
        kubectl -n "$NAMESPACE" delete jobs,secrets -l 'openvisor/dev-run=true' --ignore-not-found 2>/dev/null || true
        kubectl -n "$NAMESPACE" delete middlewares.traefik.io,httproutes.gateway.networking.k8s.io \
            -l 'openvisor/demo=true' --ignore-not-found 2>/dev/null || true
        rm -f "$VALUES_FILE"
        echo "SUCCESS: uninstalled (namespace and CRDs kept)"
        ;;

    build-push)
        # Build + push via compose.prod.yml so the pushed images are the exact
        # artifacts `make prod-build` produces and `make prod` can run locally -
        # one build path for compose and Kubernetes (parity by construction).
        IMAGE_PLATFORM="${IMAGE_PLATFORM:-linux/amd64}"
        APP_URL="${APP_URL:-$HTTP_SCHEME://app.$DEPLOY_DOMAIN}"
        # SITE_URL is the landing's canonical origin; derive it from the deploy
        # domain like APP_URL. The landing's other brand build args (BRAND_NAME,
        # CONSULTANT_NAME, BRAND_COLOR_*) are read from PROJECT_DIR/.env by
        # compose, so they need no explicit pass here.
        SITE_URL="${SITE_URL:-$HTTP_SCHEME://$DEPLOY_DOMAIN}"
        echo "INFO: building via compose.prod ($IMAGE_PLATFORM -> $IMAGE_REGISTRY, tag $IMAGE_TAG, landing APP_URL=$APP_URL SITE_URL=$SITE_URL)"
        cd "$PROJECT_DIR"
        DOCKER_DEFAULT_PLATFORM="$IMAGE_PLATFORM" APP_URL="$APP_URL" SITE_URL="$SITE_URL" \
            docker compose -f compose.base.yml -f compose.prod.yml --profile build-only build
        APP_URL="$APP_URL" SITE_URL="$SITE_URL" docker compose -f compose.base.yml -f compose.prod.yml \
            push api mcp donsetch-mcp deployer app landing runner
        echo "SUCCESS: images built + pushed"
        ;;

    *)
        echo "ERROR: Unknown action '$ACTION'"
        echo "Usage: $0 <deploy|uninstall|build-push> [release_name] [namespace]"
        exit 1
        ;;
esac
