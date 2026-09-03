from __future__ import annotations

from typing import Any

from core.time_utils import utc_now
from services.accounts.identity import link_channel_to_account, resolve_canonical_user_id
from services.db import db, tx

from services.messenger.platforms import MessengerPlatform, normalize_platform, parse_platform


def _iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def record_channel_identity(
    user_id: int,
    platform: str,
    external_user_id: str | None,
    *,
    username: str | None = None,
    display_name: str | None = None,
) -> None:
    norm = parse_platform(platform)
    if norm is None:
        raise ValueError('invalid platform')
    ext = (external_user_id or '').strip() or None
    uname = (username or '').strip() or None
    dname = (display_name or '').strip() or None

    # Account identity is the canonical layer. The legacy user_channel_* tables
    # below are kept as a compatibility mirror while older delivery services are
    # migrated. Do this first so an identity conflict cannot silently mutate the
    # legacy mirror into a different account: re-linking an already-claimed
    # external identity to another account must raise AccountIdentityConflict
    # (paid entitlements must never migrate between accounts implicitly).
    canonical_user_id = link_channel_to_account(
        int(user_id),
        norm,
        ext,
        username=uname,
        display_name=dname,
        link_source='legacy_mirror',
    )

    now = _iso_now()
    with db() as conn:
        with tx(conn):
            if ext is not None:
                conn.execute(
                    '''
                    DELETE FROM user_channel_identities
                    WHERE platform=? AND external_user_id=? AND user_id<>?
                    '''.strip(),
                    (norm, ext, int(canonical_user_id)),
                )
            conn.execute(
                '''
                INSERT INTO user_channel_identities(
                    user_id, platform, external_user_id, username, display_name, first_seen_at, last_seen_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user_id, platform) DO UPDATE SET
                    external_user_id=COALESCE(excluded.external_user_id, user_channel_identities.external_user_id),
                    username=COALESCE(excluded.username, user_channel_identities.username),
                    display_name=COALESCE(excluded.display_name, user_channel_identities.display_name),
                    last_seen_at=excluded.last_seen_at
                '''.strip(),
                (int(canonical_user_id), norm, ext, uname, dname, now, now),
            )

            conn.execute(
                '''
                INSERT INTO user_channel_preferences(user_id, preferred_platform, last_seen_platform, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen_platform=excluded.last_seen_platform,
                    updated_at=excluded.updated_at
                '''.strip(),
                (int(canonical_user_id), norm, norm, now),
            )


def record_channel_touch(user_id: int, platform: str) -> None:
    norm = parse_platform(platform)
    if norm is None:
        raise ValueError('invalid platform')
    canonical_user_id = resolve_canonical_user_id(int(user_id))
    now = _iso_now()
    with db() as conn:
        with tx(conn):
            conn.execute(
                '''
                INSERT INTO user_channel_preferences(user_id, preferred_platform, last_seen_platform, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen_platform=excluded.last_seen_platform,
                    updated_at=excluded.updated_at
                '''.strip(),
                (int(canonical_user_id), norm, norm, now),
            )


def set_preferred_platform(user_id: int, platform: str) -> None:
    norm = parse_platform(platform)
    if norm is None:
        raise ValueError('invalid platform')
    canonical_user_id = resolve_canonical_user_id(int(user_id))
    now = _iso_now()
    with db() as conn:
        with tx(conn):
            conn.execute(
                '''
                INSERT INTO user_channel_preferences(user_id, preferred_platform, last_seen_platform, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    preferred_platform=excluded.preferred_platform,
                    updated_at=excluded.updated_at
                '''.strip(),
                (int(canonical_user_id), norm, norm, now),
            )


def get_preferred_platform(user_id: int) -> str:
    canonical_user_id = resolve_canonical_user_id(int(user_id))
    with db() as conn:
        row = conn.execute(
            'SELECT preferred_platform, last_seen_platform FROM user_channel_preferences WHERE user_id=?',
            (int(canonical_user_id),),
        ).fetchone()
    if not row:
        return MessengerPlatform.TELEGRAM.value
    return normalize_platform(row['preferred_platform'] or row['last_seen_platform'])


def get_available_platforms(user_id: int) -> list[str]:
    canonical_user_id = resolve_canonical_user_id(int(user_id))
    with db() as conn:
        rows = conn.execute(
            'SELECT platform FROM user_channel_identities WHERE user_id=? ORDER BY last_seen_at DESC',
            (int(canonical_user_id),),
        ).fetchall()
    out: list[str] = []
    for row in rows:
        platform = normalize_platform(row['platform'])
        if platform not in out:
            out.append(platform)
    return out


def resolve_delivery_platform(user_id: int, *, fallback: str = MessengerPlatform.TELEGRAM.value) -> str:
    preferred = get_preferred_platform(int(user_id))
    available = get_available_platforms(int(user_id))
    if preferred in available:
        return preferred
    if available:
        return available[0]
    return normalize_platform(fallback)


def get_channel_snapshot(user_id: int) -> dict[str, Any]:
    canonical_user_id = resolve_canonical_user_id(int(user_id))
    with db() as conn:
        pref = conn.execute(
            'SELECT preferred_platform, last_seen_platform, updated_at FROM user_channel_preferences WHERE user_id=?',
            (int(canonical_user_id),),
        ).fetchone()
        ids = conn.execute(
            '''
            SELECT platform, external_user_id, username, display_name, first_seen_at, last_seen_at
            FROM user_channel_identities
            WHERE user_id=?
            ORDER BY last_seen_at DESC
            '''.strip(),
            (int(canonical_user_id),),
        ).fetchall()
    return {
        'user_id': int(canonical_user_id),
        'preferred_platform': normalize_platform(pref['preferred_platform']) if pref else MessengerPlatform.TELEGRAM.value,
        'last_seen_platform': normalize_platform(pref['last_seen_platform']) if pref else MessengerPlatform.TELEGRAM.value,
        'updated_at': pref['updated_at'] if pref else None,
        'identities': [dict(row) for row in ids],
    }


def prefer_current_platform(user_id: int, platform: str) -> None:
    norm = parse_platform(platform)
    if norm is None:
        raise ValueError('invalid platform')
    canonical_user_id = resolve_canonical_user_id(int(user_id))
    now = _iso_now()
    with db() as conn:
        with tx(conn):
            conn.execute(
                '''
                INSERT INTO user_channel_preferences(user_id, preferred_platform, last_seen_platform, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    preferred_platform=excluded.preferred_platform,
                    last_seen_platform=excluded.last_seen_platform,
                    updated_at=excluded.updated_at
                '''.strip(),
                (int(canonical_user_id), norm, norm, now),
            )
