{{- define "nb-register.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nb-register.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "nb-register.componentFullname" -}}
{{- printf "%s-%s" (include "nb-register.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nb-register.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nb-register.labels" -}}
helm.sh/chart: {{ include "nb-register.chart" . }}
app.kubernetes.io/name: {{ include "nb-register.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "nb-register.componentLabels" -}}
{{ include "nb-register.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "nb-register.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nb-register.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "nb-register.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "nb-register.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "nb-register.configName" -}}
{{- printf "%s-config" (include "nb-register.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nb-register.appSecretName" -}}
{{- default (printf "%s-secret" (include "nb-register.fullname" .)) .Values.secrets.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nb-register.gopayConfigSecretName" -}}
{{- default (include "nb-register.appSecretName" .) .Values.gopayPaymentConfig.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nb-register.image" -}}
{{- $registry := default "" .root.Values.global.imageRegistry -}}
{{- $repository := required "image.repository is required" .image.repository -}}
{{- $tag := default "latest" .image.tag -}}
{{- $firstSegment := first (splitList "/" $repository) -}}
{{- $hasRegistry := or (contains "." $firstSegment) (contains ":" $firstSegment) (eq $firstSegment "localhost") -}}
{{- if and $registry (not $hasRegistry) -}}
{{- printf "%s/%s:%s" (trimSuffix "/" $registry) $repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "nb-register.postgresHost" -}}
{{- if .Values.postgres.enabled -}}
{{- include "nb-register.componentFullname" (dict "root" . "component" "postgres") -}}
{{- else -}}
{{- required "postgres.external.host is required when postgres.enabled=false" .Values.postgres.external.host -}}
{{- end -}}
{{- end -}}

{{- define "nb-register.postgresPort" -}}
{{- if .Values.postgres.enabled -}}
{{- .Values.postgres.service.port -}}
{{- else -}}
{{- .Values.postgres.external.port -}}
{{- end -}}
{{- end -}}

{{- define "nb-register.pgDsn" -}}
{{- printf "host=%s user=$(POSTGRES_USER) password=$(POSTGRES_PASSWORD) dbname=$(POSTGRES_DB) port=%v sslmode=%s" (include "nb-register.postgresHost" .) (include "nb-register.postgresPort" .) .Values.postgres.sslMode -}}
{{- end -}}
