# ClientPlatform baseline provenance

- Imported technical baseline commit: `b4ac43c2961fb581078aedc25efeffd2ab4ecb34`
- Canonical repository: `mailsvb2-bot/clientplatform`
- Product name: **ClientPlatform**
- The original product identity and product-specific runtime contract have been intentionally removed from this repository.
- Repository visibility: public by owner decision to support GitHub Actions without private-repository billing.

## Non-negotiable isolation

ClientPlatform must use only ClientPlatform-owned production resources. It must never reuse credentials, databases, backups, webhook secrets, domains, object storage, runtime paths, deployment units, monitoring credentials, deployment keys, or real user data from any other product.

The imported commit records technical provenance only. It does not define the current product model and does not authorize deployment of copied runtime behavior.

Every ClientPlatform staging or production environment must have independent bots, credentials, databases, storage, domains and deployment units.
