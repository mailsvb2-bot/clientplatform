# M7-009 production release carrier

This file is a no-code production release carrier for the already merged and verified M7-009 Native Automation / Approvals Cockpit slice.

Product code source before this carrier: `2c1cb636bf5cb480007160b12f4faef25867ca5c`.

Release requirements:
- deploy only through the canonical dedicated ClientPlatform production recovery workflow;
- pin deployment to the resulting protected `main` merge SHA;
- target only the dedicated ClientPlatform production checkout at `/opt/clientplatform`;
- require the existing production backup, readiness, runtime-marker, HTTPS and automatic rollback gates;
- do not modify production data or infrastructure outside the canonical deploy pipeline.

The carrier intentionally contains no application, runtime, schema or configuration change.
