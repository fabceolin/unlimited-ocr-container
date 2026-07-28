{{/*
Full image reference, defaulting the tag to the variant name (cpu/gpu).
*/}}
{{- define "unlimited-ocr.image" -}}
{{- $tag := .Values.image.tag | default .Values.image.variant -}}
{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}

{{/*
Job/app name for this release.
*/}}
{{- define "unlimited-ocr.jobName" -}}
{{- printf "%s-ocr" .Release.Name | trunc 52 | trimSuffix "-" -}}
{{- end -}}
