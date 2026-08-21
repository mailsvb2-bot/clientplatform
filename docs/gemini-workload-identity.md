# Gemini Workload Identity Federation

This document describes the production authentication path for Gemini CI review.

Flow:

GitHub Actions -> OIDC -> Google Workload Identity Federation -> Service Account -> Gemini API

Required GitHub secrets:

- GOOGLE_WORKLOAD_IDENTITY_PROVIDER
- GOOGLE_SERVICE_ACCOUNT

No long-lived Gemini API key is stored in GitHub.
