{{/*
================================================================================
  common-lib/_pod.tpl
  Shared template that renders the full pod spec (spec.template.spec).
  Reused by _deployment.yaml, _job.yaml, and _cronjob.yaml.

  Caller must ensure .Values.deployment is populated with the pod spec fields.
================================================================================
*/}}
{{- define "common-lib.podSpec" -}}
{{- with .Values.deployment.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
serviceAccountName: {{ include "common-lib.serviceAccountName" . }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountServiceAccountToken | default false }}
securityContext:
  {{- toYaml (.Values.deployment.podSecurityContext | default dict) | nindent 2 }}
{{- with .Values.deployment.initContainers }}
initContainers:
  {{- toYaml . | nindent 2 }}
{{- end }}
containers:
  - name: {{ include "common-lib.name" . }}
    {{- with .Values.deployment.securityContext }}
    securityContext:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
    imagePullPolicy: {{ .Values.image.pullPolicy | default "IfNotPresent" }}
    {{- with .Values.deployment.command }}
    command:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .Values.deployment.args }}
    args:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- /* PY-M1 FIX: Only render ports for Deployments.
       Jobs/CronJobs are batch workloads — container ports are unnecessary and misleading.
       The old condition accidentally rendered ports for all types when .deployment.ports was empty. */}}
    {{- if eq .Values.type "deployment" }}
    ports:
      {{- if .Values.deployment.ports }}
      {{- toYaml .Values.deployment.ports | nindent 6 }}
      {{- else }}
      - name: http
        containerPort: {{ .Values.deployment.containerPort | default 80 }}
        protocol: TCP
      {{- end }}
    {{- end }}
    {{- with .Values.deployment.env }}
    env:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .Values.deployment.envFrom }}
    envFrom:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- if .Values.deployment.livenessProbe }}
    livenessProbe:
      {{- toYaml .Values.deployment.livenessProbe | nindent 6 }}
    {{- end }}
    {{- if .Values.deployment.readinessProbe }}
    readinessProbe:
      {{- toYaml .Values.deployment.readinessProbe | nindent 6 }}
    {{- end }}
    {{- if .Values.deployment.startupProbe }}
    startupProbe:
      {{- toYaml .Values.deployment.startupProbe | nindent 6 }}
    {{- end }}
    resources:
      {{- toYaml (.Values.deployment.resources | default dict) | nindent 6 }}
    {{- with .Values.deployment.volumeMounts }}
    volumeMounts:
      {{- toYaml . | nindent 6 }}
    {{- end }}
  {{- with .Values.deployment.sidecars }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
{{- with .Values.deployment.volumes }}
volumes:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.deployment.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.deployment.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.deployment.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.deployment.k8sPod }}
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end }}
