{{/* vim: set filetype=mustache: */}}
{{- define "nebulens.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nebulens.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "nebulens.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "nebulens.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "nebulens.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nebulens.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* "true" unless server.enabled is false. Agent-only installs (a remote cluster that sends to another console) turn the server off. */}}
{{- define "nebulens.serverEnabled" -}}
{{- $s := default (dict) .Values.server -}}
{{- if hasKey $s "enabled" -}}{{- if $s.enabled -}}true{{- end -}}{{- else -}}true{{- end -}}
{{- end }}

{{- define "nebulens.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "nebulens.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
