from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the first complete clientplatform program, enrollment and delivery contour."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS programs(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            archived_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('draft', 'active', 'archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS lessons(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            title TEXT NOT NULL,
            content_kind TEXT NOT NULL,
            content_ref TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(id, business_id, program_id),
            UNIQUE(business_id, program_id, position),
            FOREIGN KEY(program_id, business_id)
                REFERENCES programs(id, business_id) ON DELETE CASCADE,
            CHECK(position > 0),
            CHECK(content_kind IN (
                'audio', 'video', 'text', 'document', 'image',
                'link', 'task', 'mixed'
            )),
            CHECK(status IN ('active', 'archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS enrollments(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            paused_at TEXT,
            cancelled_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(id, business_id, program_id),
            UNIQUE(business_id, program_id, customer_id),
            FOREIGN KEY(program_id, business_id)
                REFERENCES programs(id, business_id),
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id),
            CHECK(status IN ('active', 'paused', 'completed', 'cancelled'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_deliveries(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            enrollment_id TEXT NOT NULL,
            lesson_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            scheduled_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT,
            failed_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, enrollment_id, lesson_id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(enrollment_id, business_id, program_id)
                REFERENCES enrollments(id, business_id, program_id) ON DELETE CASCADE,
            FOREIGN KEY(lesson_id, business_id, program_id)
                REFERENCES lessons(id, business_id, program_id),
            CHECK(attempts >= 0),
            CHECK(status IN ('pending', 'sent', 'failed', 'cancelled'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_progress(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            program_id TEXT NOT NULL,
            enrollment_id TEXT NOT NULL,
            lesson_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            delivered_at TEXT,
            opened_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, enrollment_id, lesson_id),
            FOREIGN KEY(enrollment_id, business_id, program_id)
                REFERENCES enrollments(id, business_id, program_id) ON DELETE CASCADE,
            FOREIGN KEY(lesson_id, business_id, program_id)
                REFERENCES lessons(id, business_id, program_id),
            CHECK(status IN ('pending', 'delivered', 'opened', 'completed', 'skipped'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_programs_business_status
        ON programs(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lessons_program_position
        ON lessons(business_id, program_id, status, position)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_enrollments_customer_status
        ON enrollments(business_id, customer_id, status, started_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lesson_deliveries_due
        ON lesson_deliveries(business_id, status, scheduled_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lesson_progress_enrollment
        ON lesson_progress(business_id, enrollment_id, status)
        """
    )
