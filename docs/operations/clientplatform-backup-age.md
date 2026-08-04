# ClientPlatform encrypted PostgreSQL backups

Production backups must use an X25519 `age` recipient. The private identity is kept outside the application environment; only its public recipient is written to `deploy/clientplatform/clientplatform.env`.

On the production host, after updating the repository, run as root:

```sh
sh deploy/clientplatform/configure-backup-age.sh
```

The configurator is idempotent. It:

1. creates `/root/.config/clientplatform/backup-age-identity.txt` when no identity exists;
2. enforces root ownership and mode `0600` for the identity;
3. derives and validates the public recipient;
4. atomically updates `CLIENTPLATFORM_BACKUP_AGE_RECIPIENT` without printing other environment values;
5. creates a real encrypted PostgreSQL backup through the production Compose service;
6. verifies the ciphertext, checksum and metadata and confirms that the matching plaintext bundle was removed.

The identity file is required for disaster recovery. Copy it to a separate secure password manager or offline encrypted storage. Do not place it in Git, the application environment, or the same backup bucket as the ciphertext.
