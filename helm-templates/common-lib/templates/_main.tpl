{{/*
================================================================================
  common-lib/templates/main.yaml -> renamed to _main.tpl for Helm 4 compatibility
================================================================================
*/}}
{{- define "common-lib.main" -}}
{{- $supportedTypes := list "deployment" "job" "cronjob" -}}
{{- if not .Values.type }}
  {{- fail "ERROR: .Values.type is required. Supported values: deployment, job, cronjob" }}
{{- end }}
{{- if not (has .Values.type $supportedTypes) }}
  {{- fail (printf "ERROR: Unsupported .Values.type '%s'. Supported values: deployment, job, cronjob" .Values.type) }}
{{- end }}

{{- if and .Values.pvc .Values.pvc.enabled }}
{{ include "common-lib.pvc" . }}
---
{{- end }}

{{- if .Values.serviceAccount.create }}
{{ include "common-lib.serviceAccount" . }}
---
{{- end }}

{{- if eq .Values.type "deployment" }}
{{ include "common-lib.deployment" . }}
{{- else if eq .Values.type "job" }}
{{ include "common-lib.job" . }}
{{- else if eq .Values.type "cronjob" }}
{{ include "common-lib.cronjob" . }}
{{- end }}

{{- if .Values.service.enabled }}
---
{{ include "common-lib.service" . }}
{{- end }}

{{- if .Values.ingress.enabled }}
---
{{ include "common-lib.ingress" . }}
{{- end }}

{{- if and .Values.hpa.enabled (eq .Values.type "deployment") }}
---
{{ include "common-lib.hpa" . }}
{{- end }}
{{- end }}
