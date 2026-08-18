from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import web

from clientplatform.application.growth_cockpit import get_growth_cockpit
from clientplatform.application.tenancy import list_accessible_businesses, resolve_tenant_context
from clientplatform.domain.growth_cockpit import GrowthCockpitMoney, GrowthCockpitSnapshot
from clientplatform.domain.tenancy import PlatformRole, TenantAccessDenied, TenantPermissionDenied
from clientplatform.runtime.control_bot import CONTROL_BOT_CREDENTIAL_ENV, control_bot_enabled
from clientplatform.runtime.telegram_webapp_auth import (
    TelegramWebAppAuthError,
    validate_telegram_webapp_init_data,
)

log = logging.getLogger(__name__)

_DASHBOARD_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
    }
)

_GROWTH_COCKPIT_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>ClientPlatform — Growth Cockpit</title>
  <script src="https://telegram.org/js/telegram-web-app.js?63"></script>
  <style>
    :root { color-scheme: light dark; font-family: Inter, system-ui, -apple-system, sans-serif; }
    body { margin:0; background:var(--tg-theme-bg-color,#f4f5f7); color:var(--tg-theme-text-color,#161616); }
    main { max-width:760px; margin:0 auto; padding:18px 16px calc(28px + env(safe-area-inset-bottom)); }
    h1 { font-size:24px; margin:0 0 4px; }
    .muted { color:var(--tg-theme-hint-color,#777); font-size:13px; }
    .toolbar { display:flex; gap:8px; margin:16px 0; flex-wrap:wrap; }
    button, select { border:0; border-radius:12px; padding:10px 13px; font:inherit; }
    button { background:var(--tg-theme-button-color,#2481cc); color:var(--tg-theme-button-text-color,#fff); cursor:pointer; }
    button.secondary, select { background:var(--tg-theme-secondary-bg-color,#e9edf1); color:var(--tg-theme-text-color,#161616); }
    button.active { outline:2px solid var(--tg-theme-accent-text-color,#2481cc); }
    section { background:var(--tg-theme-section-bg-color,var(--tg-theme-secondary-bg-color,#fff)); border-radius:16px; padding:15px; margin:12px 0; }
    section h2 { font-size:16px; margin:0 0 10px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
    .metric { background:var(--tg-theme-bg-color,#f7f7f7); border-radius:13px; padding:12px; }
    .metric strong { display:block; font-size:22px; margin-bottom:3px; }
    .metric span { font-size:12px; color:var(--tg-theme-hint-color,#777); }
    .money { font-size:22px; font-weight:700; margin:4px 0; }
    .notice { border-left:3px solid var(--tg-theme-accent-text-color,#2481cc); padding-left:11px; }
    .warning { border-left-color:#d98500; }
    .error { border-left-color:#c53b3b; }
    details { margin-top:8px; }
    details p { font-size:12px; color:var(--tg-theme-hint-color,#777); }
    #status { min-height:18px; }
    @media (min-width:620px) { .grid { grid-template-columns:repeat(3,minmax(0,1fr)); } }
  </style>
</head>
<body>
<main>
  <h1>Что происходит с бизнесом</h1>
  <div class="muted" id="subtitle">ClientPlatform собирает только подтверждённые факты.</div>
  <div class="toolbar">
    <select id="business" hidden aria-label="Бизнес"></select>
    <button id="p7" type="button">7 дней</button>
    <button id="p30" class="secondary" type="button">30 дней</button>
  </div>
  <div id="status" class="muted">Загрузка…</div>
  <div id="content"></div>
</main>
<script>
(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const content = document.getElementById('content');
  const status = document.getElementById('status');
  const businessSelect = document.getElementById('business');
  let periodDays = 7;
  let selectedBusiness = new URLSearchParams(window.location.search).get('business') || null;

  const text = (tag, value, cls) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    node.textContent = value;
    return node;
  };
  const section = title => {
    const node = document.createElement('section');
    node.appendChild(text('h2', title));
    return node;
  };
  const metricMap = payload => Object.fromEntries(payload.metrics.map(item => [item.key, item]));
  const metricCard = item => {
    const node = text('div', '', 'metric');
    node.appendChild(text('strong', String(item.value)));
    node.appendChild(text('span', item.label));
    const details = document.createElement('details');
    details.appendChild(text('summary', 'Откуда эта цифра'));
    details.appendChild(text('p', item.meaning + ' Источник: ' + item.source));
    node.appendChild(details);
    return node;
  };
  const moneyLine = items => items.length ? items.map(item => item.display).join(' · ') : 'Пока нет подтверждённых денежных событий';

  function render(payload) {
    content.replaceChildren();
    const m = metricMap(payload);
    const today = section('Сегодня');
    const todayGrid = text('div', '', 'grid');
    ['today_leads','today_bookings','today_paid_customers'].forEach(key => todayGrid.appendChild(metricCard(m[key])));
    today.appendChild(todayGrid);
    today.appendChild(text('div', moneyLine(payload.today_revenue), 'money'));
    content.appendChild(today);

    const period = section('За ' + payload.period_days + ' дней');
    const periodGrid = text('div', '', 'grid');
    ['period_leads','period_bookings','period_paid_customers'].forEach(key => periodGrid.appendChild(metricCard(m[key])));
    period.appendChild(periodGrid);
    period.appendChild(text('div', moneyLine(payload.period_revenue), 'money'));
    content.appendChild(period);

    const sales = section('Кому нужно ответить');
    const salesGrid = text('div', '', 'grid');
    ['sales_open_work','sales_needs_reply','sales_handoffs'].forEach(key => salesGrid.appendChild(metricCard(m[key])));
    sales.appendChild(salesGrid);
    content.appendChild(sales);

    if (m.advertising_accounts) {
      const ads = section('Реклама');
      const adsGrid = text('div', '', 'grid');
      ['advertising_accounts','advertising_clicks','advertising_attributed_leads','advertising_attributed_bookings']
        .forEach(key => adsGrid.appendChild(metricCard(m[key])));
      ads.appendChild(adsGrid);
      content.appendChild(ads);
    }

    const worked = section('Что сработало');
    worked.appendChild(text('div', payload.what_worked, 'notice'));
    content.appendChild(worked);

    const decision = section('Что требует решения');
    decision.appendChild(text('strong', payload.requires_decision.title));
    decision.appendChild(text('p', payload.requires_decision.detail));
    if (payload.requires_decision.kind !== 'none') {
      const btn = text('button', 'Перейти к следующему шагу');
      btn.type = 'button';
      btn.addEventListener('click', () => tg.close());
      decision.appendChild(btn);
      decision.appendChild(text('p', 'Вернёмся в чат: под сообщением Growth Cockpit уже есть кнопка нужного канонического действия.', 'muted'));
    }
    content.appendChild(decision);

    const next = section('Что ClientPlatform сделает дальше');
    next.appendChild(text('strong', payload.next_action.title));
    next.appendChild(text('p', payload.next_action.detail));
    content.appendChild(next);

    if (payload.limitations.length) {
      const limitations = section('Ограничения данных');
      limitations.classList.add('warning');
      payload.limitations.forEach(item => limitations.appendChild(text('p', item.message)));
      content.appendChild(limitations);
    }
    status.textContent = 'Обновлено: ' + new Date(payload.generated_at).toLocaleString();
  }

  function renderBusinessSelector(businesses) {
    businessSelect.replaceChildren();
    businesses.forEach(item => {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = item.name;
      businessSelect.appendChild(option);
    });
    businessSelect.hidden = businesses.length <= 1;
    if (selectedBusiness && businesses.some(item => item.id === selectedBusiness)) {
      businessSelect.value = selectedBusiness;
    } else if (businesses.length) {
      businessSelect.value = businesses[0].id;
      selectedBusiness = businesses[0].id;
    }
  }

  async function load() {
    if (!tg || !tg.initData) {
      status.textContent = 'Откройте Growth Cockpit кнопкой из ClientPlatform в Telegram.';
      return;
    }
    status.textContent = 'Обновляем подтверждённые данные…';
    try {
      const response = await fetch('/api/clientplatform/growth-cockpit', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Telegram-Init-Data': tg.initData,
        },
        body: JSON.stringify({business_id: selectedBusiness, period_days: periodDays}),
        credentials: 'omit',
        cache: 'no-store',
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || 'Не удалось загрузить Growth Cockpit');
      renderBusinessSelector(payload.businesses || []);
      if (payload.selection_required) {
        await load();
        return;
      }
      selectedBusiness = payload.business_id;
      render(payload);
    } catch (error) {
      content.replaceChildren();
      const node = section('Не удалось загрузить данные');
      node.classList.add('error');
      node.appendChild(text('p', error && error.message ? error.message : 'Попробуйте ещё раз из ClientPlatform.'));
      content.appendChild(node);
      status.textContent = '';
    }
  }

  function setPeriod(days) {
    periodDays = days;
    document.getElementById('p7').className = days === 7 ? 'active' : 'secondary';
    document.getElementById('p30').className = days === 30 ? 'active' : 'secondary';
    load();
  }

  document.getElementById('p7').addEventListener('click', () => setPeriod(7));
  document.getElementById('p30').addEventListener('click', () => setPeriod(30));
  businessSelect.addEventListener('change', () => { selectedBusiness = businessSelect.value; load(); });
  if (tg) { tg.ready(); tg.expand(); }
  setPeriod(7);
})();
</script>
</body>
</html>
"""

_LIMITATION_COPY = {
    "attribution_incomplete": "Часть оплат пока нельзя надёжно связать с источником; вывод «что сработало» неполный.",
    "revenue_mixed_currency": "Деньги пришли в разных валютах, поэтому ClientPlatform не складывает их в одну ложную сумму.",
    "advertising_data_unavailable": "Рекламный провайдер сейчас не дал надёжную статистику; внутренние бизнес-результаты продолжают показываться.",
    "advertising_spend_currency_unverified": "Рекламный расход не превращён в деньги, пока валюта рекламного аккаунта не подтверждена надёжным источником.",
    "advertising_spend_unavailable": "Подтверждённый рекламный расход для этого экрана пока недоступен.",
}


def growth_cockpit_http_enabled() -> bool:
    return control_bot_enabled() and bool((os.getenv(CONTROL_BOT_CREDENTIAL_ENV) or "").strip())


def _max_age_seconds() -> int:
    raw = (os.getenv("CLIENTPLATFORM_GROWTH_COCKPIT_INIT_DATA_MAX_AGE_SEC") or "300").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("growth_cockpit_init_data_max_age_invalid") from exc
    if not 60 <= value <= 3600:
        raise RuntimeError("growth_cockpit_init_data_max_age_invalid")
    return value


def _money_display(item: GrowthCockpitMoney) -> str:
    # OutcomeMoney stores ISO minor units.  ClientPlatform's commercial contour
    # currently settles in RUB; common 2-decimal currencies are rendered too.
    if item.currency in {"RUB", "USD", "EUR", "GBP", "CNY", "CHF"}:
        sign = "-" if item.amount_minor < 0 else ""
        value = abs(item.amount_minor)
        return f"{sign}{value // 100:,}.{value % 100:02d} {item.currency}".replace(",", " ")
    if item.currency in {"JPY", "KRW"}:
        return f"{item.amount_minor:,} {item.currency}".replace(",", " ")
    return f"{item.amount_minor:,} минимальных единиц {item.currency}".replace(",", " ")


def _money_payload(item: GrowthCockpitMoney) -> dict[str, Any]:
    return {
        "currency": item.currency,
        "amount_minor": item.amount_minor,
        "display": _money_display(item),
        "source": item.source,
        "meaning": item.meaning,
    }


def _snapshot_payload(snapshot: GrowthCockpitSnapshot) -> dict[str, Any]:
    return {
        "ok": True,
        "business_id": snapshot.business_id,
        "timezone": snapshot.timezone,
        "period_days": snapshot.period_days,
        "generated_at": snapshot.generated_at.isoformat(),
        "metrics": [
            {
                "key": item.key,
                "label": item.label,
                "value": item.value,
                "unit": item.unit,
                "source": item.source,
                "meaning": item.meaning,
            }
            for item in snapshot.metrics
        ],
        "today_revenue": [_money_payload(item) for item in snapshot.today_revenue],
        "period_revenue": [_money_payload(item) for item in snapshot.period_revenue],
        "what_worked": snapshot.what_worked,
        "requires_decision": {
            "kind": snapshot.requires_decision.kind,
            "title": snapshot.requires_decision.title,
            "detail": snapshot.requires_decision.detail,
        },
        "next_action": {
            "kind": snapshot.next_action.kind,
            "title": snapshot.next_action.title,
            "detail": snapshot.next_action.detail,
        },
        "limitations": [
            {"code": code, "message": _LIMITATION_COPY.get(code, code)}
            for code in snapshot.limitations
        ],
    }


def _load_for_user(*, user_id: int, requested_business_id: str | None, period_days: int) -> dict[str, Any]:
    access = [
        item
        for item in list_accessible_businesses(user_id=user_id)
        if item.membership.role in _DASHBOARD_ROLES
    ]
    businesses = [
        {"id": item.business.id, "name": item.business.name}
        for item in access
    ]
    allowed_ids = {item.business.id for item in access}
    if not businesses:
        raise TenantPermissionDenied("growth cockpit is not available for this account")

    requested = str(requested_business_id or "").strip() or None
    if requested is not None and requested not in allowed_ids:
        raise TenantAccessDenied("requested business is not accessible")
    if requested is None:
        if len(businesses) != 1:
            return {
                "ok": True,
                "selection_required": True,
                "businesses": businesses,
            }
        requested = businesses[0]["id"]

    actor = resolve_tenant_context(user_id=user_id, business_id=requested)
    snapshot = get_growth_cockpit(actor=actor, period_days=period_days)
    payload = _snapshot_payload(snapshot)
    payload["selection_required"] = False
    payload["businesses"] = businesses
    return payload


async def growth_cockpit_page(request: web.Request) -> web.Response:
    del request
    return web.Response(
        text=_GROWTH_COCKPIT_HTML,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


async def growth_cockpit_api(request: web.Request) -> web.Response:
    init_data = (request.headers.get("X-Telegram-Init-Data") or "").strip()
    token = (os.getenv(CONTROL_BOT_CREDENTIAL_ENV) or "").strip()
    try:
        principal = validate_telegram_webapp_init_data(
            init_data=init_data,
            bot_token=token,
            max_age_seconds=_max_age_seconds(),
        )
    except (TelegramWebAppAuthError, RuntimeError):
        return web.json_response(
            {"ok": False, "message": "Откройте Growth Cockpit заново из ClientPlatform в Telegram."},
            status=401,
            headers={"Cache-Control": "no-store"},
        )

    try:
        body = await request.json(loads=json.loads)
    except (json.JSONDecodeError, ValueError, TypeError):
        return web.json_response(
            {"ok": False, "message": "Некорректный запрос."},
            status=400,
            headers={"Cache-Control": "no-store"},
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "message": "Некорректный запрос."},
            status=400,
            headers={"Cache-Control": "no-store"},
        )
    try:
        period_days = int(body.get("period_days", 7))
    except (TypeError, ValueError):
        period_days = 0
    if period_days not in {7, 30}:
        return web.json_response(
            {"ok": False, "message": "Доступны периоды 7 или 30 дней."},
            status=400,
            headers={"Cache-Control": "no-store"},
        )
    requested_business = body.get("business_id")
    if requested_business is not None and not isinstance(requested_business, str):
        return web.json_response(
            {"ok": False, "message": "Некорректный бизнес-контекст."},
            status=400,
            headers={"Cache-Control": "no-store"},
        )

    try:
        payload = await asyncio.to_thread(
            _load_for_user,
            user_id=principal.user_id,
            requested_business_id=requested_business,
            period_days=period_days,
        )
    except (TenantAccessDenied, TenantPermissionDenied, ValueError):
        return web.json_response(
            {"ok": False, "message": "Growth Cockpit недоступен для выбранного бизнеса."},
            status=403,
            headers={"Cache-Control": "no-store"},
        )
    except Exception:  # validator: allow-wide-except
        log.exception(
            "Growth Cockpit request failed",
            extra={"clientplatform_user_id": principal.user_id},
        )
        return web.json_response(
            {"ok": False, "message": "Не удалось подготовить Growth Cockpit."},
            status=503,
            headers={"Cache-Control": "no-store"},
        )
    return web.json_response(
        payload,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def register_growth_cockpit_routes(app: web.Application) -> None:
    app.router.add_get("/dashboard/growth", growth_cockpit_page)
    app.router.add_post("/api/clientplatform/growth-cockpit", growth_cockpit_api)


__all__ = [
    "growth_cockpit_api",
    "growth_cockpit_http_enabled",
    "growth_cockpit_page",
    "register_growth_cockpit_routes",
]
