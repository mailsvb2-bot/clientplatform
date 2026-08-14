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
        self.assertNotIn("VISUAL_GATEWAY_UPSTREAM_URL: http://visual-creative-gateway:8097", compose)
        provider_section = compose.split("  visual-provider-gateway:", 1)[1].split("\n  visual-gateway:", 1)[0]
        self.assertIn('expose: ["8097"]', provider_section)
        self.assertNotIn("ports:", provider_section)
        self.assertIn('security_opt: ["no-new-privileges:true"]', provider_section)
        self.assertIn("bool(payload.get('configured_image'))", provider_section)

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
        self.assertIn("COPY visual_provider_gateway /app/visual_provider_gateway", dockerfile)
        self.assertIn('"visual_provider_gateway.app:app"', run_module)
        self.assertNotIn('"visual_gateway.app:app"', run_module)


if __name__ == "__main__":
    unittest.main()
