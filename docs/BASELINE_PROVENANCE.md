# ClientPlatform baseline provenance

- Source repository: `mailsvb2-bot/metrotherapy-bot-telegram`
- Imported baseline commit: `b4ac43c2961fb581078aedc25efeffd2ab4ecb34`
- New repository: `mailsvb2-bot/clientplatform`
- Product working name: **clientplatform**
- Runtime status at baseline: imported Metrotherapy code; not yet transformed into clientplatform.
- Repository visibility: public by owner decision to support GitHub Actions without private-repository billing.

## Non-negotiable isolation

ClientPlatform must never use Metrotherapy production:

- bot tokens;
- PostgreSQL database, dumps or backups;
- YooKassa or Telegram Stars credentials;
- webhook URLs or secrets;
- domains and TLS keys;
- object storage;
- systemd units and server paths;
- real user data;
- monitoring credentials;
- deployment SSH keys.

The imported commit records provenance only. It does not authorize deployment of the copied runtime.

Before any clientplatform staging or production launch, create independent bots, credentials, databases, storage, domains and deployment units.
