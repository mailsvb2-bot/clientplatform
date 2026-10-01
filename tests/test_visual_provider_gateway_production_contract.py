from __future__ import annotations

import unittest
from pathlib import Path


class VisualProviderGatewayProductionContractTests(unittest.TestCase):
    def test_canonical_compose_owns_provider_gateway_and_preserves_migration_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        compose = (root / "deploy/clientplatform/compose.production.yml").read_text(encoding="utf-8")

        self.assertIn("  visual-provider-gateway:", compose)
        self.assertIn("dockerfile: visual_provider_gateway/Dockerfile", compose)
        self.assertIn("VCS_REF: ${CLIENTPLATFORM_BUILD_VCS_REF:-unknown}", compose)
        self.assertIn("pull_policy: build", compose)
        self.assertIn("VISUAL_GATEWAY_UPSTREAM_URL: http://visual-provider-gateway:8097", compose)
        self.assertIn("visual-provider-gateway:\n        condition: service_healthy", compose)
        self.assertIn(
            "${CLIENTPLATFORM_VISUAL_PROVIDER_DATA_DIR:-/opt/visual-creative-gateway/data}:/data",
            compose,
        )
        self.assertIn(
            "${CLIENTPLATFORM_YANDEX_BILLING_SECRET_HOST_DIR:-/var/lib/clientplatform/yandex-billing-secrets}:/run/secrets/clientplatform-yandex-billing:ro",
            compose,
        )
        self.assertEqual(
            compose.count(
                "YANDEX_BILLING_AUTHORIZED_KEY_FILE: ${YANDEX_BILLING_AUTHORIZED_KEY_FILE:-/run/secrets/clientplatform-yandex-billing/authorized-key.json}"
            ),
            2,
        )
        self.assertNotIn("VISUAL_GATEWAY_UPSTREAM_URL: http://visual-creative-gateway:8097", compose)
        provider_section = compose.split("  visual-provider-gateway:", 1)[1].split("\n  visual-gateway:", 1)[0]
        self.assertIn('expose: ["8097"]', provider_section)
        self.assertNotIn("ports:", provider_section)
        self.assertIn('security_opt: ["no-new-privileges:true"]', provider_section)
        self.assertIn("bool(payload.get('configured_image'))", provider_section)
        self.assertIn("bool(payload.get('configured_video'))", provider_section)
        self.assertIn("VISUAL_CREATIVE_OUTPUT_DIR: /tmp/visual-output", provider_section)
        self.assertIn('VISUAL_TRANSIENT_OUTPUT_REQUIRED: "1"', provider_section)
        self.assertIn("VISUAL_TRANSIENT_ASSET_TTL_SECONDS:", provider_section)
        self.assertIn("VISUAL_TRANSIENT_ASSET_CLEANUP_LIMIT:", provider_section)
        self.assertIn("/tmp:size=256m,mode=1777", provider_section)
        self.assertNotIn("VISUAL_CREATIVE_OUTPUT_DIR: /data/output", provider_section)

    def test_canonical_deploy_owner_recreates_wrapper_with_healthy_provider_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        entrypoint = (root / "deploy.sh").read_text(encoding="utf-8")
        deploy = (root / "scripts/clientplatform_production_deploy.py").read_text(encoding="utf-8")

        self.assertIn(
            'export CLIENTPLATFORM_BUILD_VCS_REF="$(git -C "$ROOT" rev-parse HEAD)"',
            entrypoint,
        )
        self.assertIn(
            'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"',
            entrypoint,
        )
        self.assertNotIn("clientplatform_visual_provider_rollout.py", entrypoint)
        self.assertIn(
            '_run([*compose, "up", "-d", "--force-recreate", "visual-gateway"])',
            deploy,
        )
        self.assertNotIn(
            '_run([*compose, "up", "-d", "--no-deps", "--force-recreate", "visual-gateway"])',
            deploy,
        )

    def test_provider_image_has_commit_provenance_and_uses_distinct_package_namespace(self) -> None:
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "visual_provider_gateway/Dockerfile").read_text(encoding="utf-8")
        run_module = (root / "visual_provider_gateway/run.py").read_text(encoding="utf-8")

        self.assertIn("ARG VCS_REF=unknown", dockerfile)
        self.assertIn('org.opencontainers.image.revision="$VCS_REF"', dockerfile)
        self.assertIn("apt-get install -y --no-install-recommends ffmpeg openssl", dockerfile)
        self.assertIn("COPY services/yandex_iam_token.py /app/services/yandex_iam_token.py", dockerfile)
        self.assertIn("COPY visual_provider_gateway /app/visual_provider_gateway", dockerfile)
        self.assertIn('"visual_provider_gateway.app:app"', run_module)
        self.assertNotIn('"visual_gateway.app:app"', run_module)

    def test_production_visual_diagnostic_fails_closed_when_provider_is_unavailable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/production-visual-provider-diagnostic.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('and safe["configured_video"]', workflow)
        self.assertIn('and not safe["runtime_image_error"]', workflow)
        self.assertIn('and not safe["runtime_video_error"]', workflow)
        self.assertIn('if readiness != "ready":', workflow)
        self.assertIn("raise SystemExit(28)", workflow)
        self.assertIn("YANDEX_ART_AUTHORIZED_KEY_FILE", workflow)
        self.assertIn("YANDEX_BILLING_AUTHORIZED_KEY_FILE", workflow)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_VISUAL_AUTH_PROBE", workflow)
        self.assertIn("get_yandex_art_iam_token", workflow)
        self.assertIn("accepted = {400, 422}", workflow)
        self.assertIn("raise SystemExit(29)", workflow)
        self.assertIn("authorized_key_file", workflow)
        self.assertIn("json_valid_object", workflow)
        self.assertIn("permission_probe", workflow)
        self.assertIn("clientplatform-permission-probe-model-does-not-exist", workflow)
        self.assertNotIn("print(raw_key", workflow)


if __name__ == "__main__":
    unittest.main()
