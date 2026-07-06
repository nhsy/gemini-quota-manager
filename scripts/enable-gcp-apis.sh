#!/bin/bash
# Enable Google Cloud APIs required by gemini_quota_manager.py
set -euo pipefail

APIS=(
  cloudquotas.googleapis.com
  generativelanguage.googleapis.com
)

PROJECT=""
PROJECT_SOURCE=""

usage() {
  echo "ERROR: No GCP project specified." >&2
  echo "" >&2
  echo "Use one of:" >&2
  echo "  task setup -- your-project-id" >&2
  echo "  GCP_PROJECT=your-project-id task setup" >&2
  echo "  gcloud config set project your-project-id   # then: task setup" >&2
  echo "  cp .env.template .env   # set GCP_PROJECT, then: task setup" >&2
  exit 1
}

resolve_project() {
  local arg="${1:-}"

  if [[ -n "${arg}" ]]; then
    PROJECT="${arg}"
    PROJECT_SOURCE="argument"
    return 0
  fi
  if [[ -n "${GCP_PROJECT:-}" ]]; then
    PROJECT="${GCP_PROJECT}"
    PROJECT_SOURCE="GCP_PROJECT"
    return 0
  fi
  if [[ -n "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
    PROJECT="${GOOGLE_CLOUD_PROJECT}"
    PROJECT_SOURCE="GOOGLE_CLOUD_PROJECT"
    return 0
  fi
  if command -v gcloud >/dev/null 2>&1; then
    PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
    PROJECT_SOURCE="gcloud config"
  fi
}

confirm_gcloud_config_project() {
  if [[ "${PROJECT_SOURCE}" != "gcloud config" ]]; then
    return 0
  fi

  local account
  account="$(gcloud config get-value account 2>/dev/null || echo "unknown")"

  echo ""
  echo "No project was passed explicitly. Using gcloud active configuration:"
  echo "  project: ${PROJECT}"
  echo "  account: ${account}"
  echo ""
  echo "APIs to enable on this project:"
  printf '  - %s\n' "${APIS[@]}"
  echo ""

  if [[ "${GCP_SETUP_SKIP_CONFIRM:-}" == "1" ]]; then
    echo "GCP_SETUP_SKIP_CONFIRM=1 set — proceeding without prompt."
    return 0
  fi

  if [[ ! -t 0 ]]; then
    echo "ERROR: Refusing to use gcloud config project without confirmation in non-interactive mode." >&2
    echo "Pass a project explicitly (task setup -- PROJECT) or set GCP_SETUP_SKIP_CONFIRM=1." >&2
    exit 1
  fi

  local reply
  read -r -p "Enable APIs on gcloud config project '${PROJECT}'? [y/N] " reply
  if [[ ! "${reply}" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
  fi
}

offer_set_gcloud_config() {
  local current
  current="$(gcloud config get-value project 2>/dev/null || true)"

  if [[ "${current}" == "${PROJECT}" ]]; then
    echo ""
    echo "gcloud config project is already '${PROJECT}'."
    return 0
  fi

  if [[ ! -t 0 ]]; then
    return 0
  fi

  echo ""
  if [[ -n "${current}" && "${current}" != "(unset)" ]]; then
    echo "gcloud config project is currently '${current}'."
  else
    echo "gcloud config project is not set."
  fi
  read -r -p "Set gcloud config project to '${PROJECT}'? [y/N] " reply
  if [[ "${reply}" =~ ^[Yy]$ ]]; then
    gcloud config set project "${PROJECT}"
    echo "gcloud config project set to '${PROJECT}'."
  else
    echo "Left gcloud config project unchanged."
  fi
}

if ! command -v gcloud >/dev/null 2>&1; then
  echo "ERROR: gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi

resolve_project "${1:-}"

if [[ -z "${PROJECT}" || "${PROJECT}" == "(unset)" ]]; then
  usage
fi

confirm_gcloud_config_project

echo "Project: ${PROJECT} (source: ${PROJECT_SOURCE})"
echo "Enabling APIs:"
printf '  - %s\n' "${APIS[@]}"

gcloud services enable "${APIS[@]}" --project="${PROJECT}"

echo ""
echo "Enabled. Waiting for propagation (15s)..."
sleep 15

echo "Verifying enabled services:"
gcloud services list --enabled --project="${PROJECT}" \
  --filter="name:(cloudquotas.googleapis.com OR generativelanguage.googleapis.com)" \
  --format="table(name,title)"

offer_set_gcloud_config

echo ""
echo "Next steps:"
echo "  gcloud auth application-default login"
echo "  ./gemini_quota_manager.py --project ${PROJECT} list"
