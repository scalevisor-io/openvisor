{{/* Common labels */}}
{{- define "openvisor.labels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/* Image reference for a locally-built component: backend, mcp, app, landing, deployer, runner */}}
{{- define "openvisor.image" -}}
{{ .root.Values.imageRegistry }}/{{ .name }}:{{ .root.Values.imageTag }}
{{- end }}

{{/* Scheme-aware public origin helpers */}}
{{- define "openvisor.appBaseUrl" -}}
{{ .Values.scheme }}://app.{{ .Values.domain }}
{{- end }}
{{- define "openvisor.landingBaseUrl" -}}
{{ .Values.scheme }}://{{ .Values.domain }}
{{- end }}

{{/* DATABASE_URL rendered from chart values when the in-cluster postgres is enabled */}}
{{- define "openvisor.databaseUrl" -}}
postgresql+asyncpg://{{ .Values.postgres.user }}:{{ .Values.postgres.password }}@postgres:5432/{{ .Values.postgres.db }}
{{- end }}

{{/*
Backend env overrides layered over the .env secret (explicit env beats envFrom).
Everything hostname- or domain-derived is pinned here so the same .env works under
compose and Kubernetes. .Values.backendEnv wins last.
*/}}
{{- define "openvisor.backendEnv" -}}
{{- $env := dict
  "DEPLOY_ENV" .Values.deployEnv
  "DEPLOY_DOMAIN" .Values.domain
  "APP_BASE_URL" (include "openvisor.appBaseUrl" .)
  "LANDING_BASE_URL" (include "openvisor.landingBaseUrl" .)
  "TRAEFIK_HTTP_PORT" (ternary "443" "80" (eq .Values.scheme "https"))
  "DEPLOYER_URL" "http://deployer:8500"
  "CONTEXT7_MCP_URL" "http://context7:3000/mcp"
  "CHROME_CDP_URL" "http://chrome:9222"
  "WORKSPACES_DIR" "/workspaces"
  "DEMO_CPU_LIMIT" (.Values.demo.cpuLimit | toString)
  "DEMO_MEM_LIMIT" (.Values.demo.memLimit | toString)
-}}
{{- if .Values.postgres.enabled }}{{- $_ := set $env "DATABASE_URL" (include "openvisor.databaseUrl" .) }}{{- end }}
{{- if .Values.redis.enabled }}{{- $_ := set $env "REDIS_URL" "redis://redis:6379/0" }}{{- end }}
{{- if .Values.meilisearch.enabled }}{{- $_ := set $env "MEILI_URL" "http://meilisearch:7700" }}{{- end }}
{{- if .Values.mailpit.enabled }}
{{- $_ := set $env "SMTP_HOST" "mailpit" }}
{{- $_ := set $env "SMTP_PORT" "1025" }}
{{- end }}
{{- range $k, $v := .Values.backendEnv }}{{- $_ := set $env $k ($v | toString) }}{{- end }}
{{- range $key := $env | keys | sortAlpha }}
- name: {{ $key }}
  value: {{ get $env $key | quote }}
{{- end }}
{{- end }}

{{/*
Co-schedule every RWO workspaces-PVC consumer onto ONE node. Symmetric anchor:
each consumer requires being on the same host as any pod carrying the shared
openvisor/workspaces-consumer label (which every consumer sets). The first pod
schedules freely; the rest - and any later reschedule, e.g. api under Recreate -
follow it, so the RWO volume never has to attach to two nodes (no Multi-Attach
deadlock). Disabled when workspaces.rwx is true (RWX can bind many nodes).
*/}}
{{- define "openvisor.workspacesConsumerLabel" -}}
openvisor/workspaces-consumer: "true"
{{- end }}
{{- define "openvisor.workspacesAffinity" -}}
{{- if not .Values.workspaces.rwx }}
affinity:
  podAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            openvisor/workspaces-consumer: "true"
        topologyKey: kubernetes.io/hostname
{{- end }}
{{- end }}

{{/* Section name demo/app HTTPRoutes attach to (TLS on -> websecure, else web) */}}
{{- define "openvisor.routeSection" -}}
{{- if .Values.gateway.tls.enabled }}websecure{{- else }}web{{- end }}
{{- end }}

{{/* imagePullSecrets block */}}
{{- define "openvisor.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}
