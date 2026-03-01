{{/*
Expand the name of the chart.
*/}}
{{- define "sre-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "sre-agent.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "sre-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "sre-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sre-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
llamacpp selector labels
*/}}
{{- define "sre-agent.llamacpp.selectorLabels" -}}
app.kubernetes.io/name: llamacpp
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Resolve LLM base URL — replaces the Go template placeholder in values with the actual namespace.
*/}}
{{- define "sre-agent.llmBaseUrl" -}}
{{- if contains "{{ .Release.Namespace }}" .Values.llm.baseUrl -}}
{{- .Values.llm.baseUrl | replace "{{ .Release.Namespace }}" .Release.Namespace }}
{{- else -}}
{{- .Values.llm.baseUrl }}
{{- end }}
{{- end }}
