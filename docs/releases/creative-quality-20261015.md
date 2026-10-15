# Creative quality production deployment

- Source PR: #604
- Source commit: 0536e190b97155652a7fb0fbe749aed4ead1c34d
- Scope: Russian visual direction prompts, optional LLM art direction, QA-ranked image variants, gradient overlay, and animated banner classification.
- Production mechanism: .github/workflows/production-deploy-recovery.yml, using a verified Git bundle and the dedicated production deployment script.
- Rollout prerequisite: required branch protection checks; do not bypass.
- Validation: inspect recovery job and production health/ready checks after merge.
