from __future__ import annotations

EVENT_FOLLOWUP_SEGMENTS = (
    "no_show",
    "join_signal_unpaid",
    "attended_unpaid",
    "offer_clicked_unpaid",
)
EVENT_FOLLOWUP_CHANNELS = ("email", "max", "vk")


def classify_event_followup_segment(
    *,
    first_join_click_at: object | None,
    attendance_confirmed_at: object | None,
    offer_clicked_at: object | None,
) -> str:
    """Classify observed webinar behaviour without inferring attendance."""

    if offer_clicked_at:
        return "offer_clicked_unpaid"
    if attendance_confirmed_at:
        return "attended_unpaid"
    if first_join_click_at:
        return "join_signal_unpaid"
    return "no_show"


__all__ = [
    "EVENT_FOLLOWUP_CHANNELS",
    "EVENT_FOLLOWUP_SEGMENTS",
    "classify_event_followup_segment",
]
