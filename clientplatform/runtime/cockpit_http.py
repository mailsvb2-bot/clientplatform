from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from clientplatform.application.cockpit import (
    resolve_cockpit_context,
    resolve_cockpit_section_start_payload,
)
from clientplatform.application.cockpit_home import (
    CockpitHomeUnavailable,
    resolve_cockpit_home,
)
from clientplatform.application.cockpit_customers import (
    CockpitCustomerActionUnavailable,
    resolve_cockpit_customer_action_route,
    resolve_cockpit_customer_detail,
    resolve_cockpit_customer_page,
)
from clientplatform.domain.customers import CustomerNotFound
from clientplatform.domain.tenancy import TenantAccessDenied, TenantPermissionDenied
from clientplatform.runtime.telegram_webapp_auth import (
    TelegramWebAppAuthError,
    verify_telegram_webapp_init_data,
)
from config.settings import settings
from core.runtime_env import env_int
from services.messenger.links import build_entry_targets
from services.messenger.platforms import MessengerPlatform

_COCKPIT_PREFIX = "/clientplatform/cockpit"
_COCKPIT_APP_KEY = web.AppKey("clientplatform_cockpit", bool)
_CUSTOMERS_SCRIPT = Path(__file__).with_name("cockpit_customers.js")

_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ClientPlatform · Кабинет</title>
<link rel="stylesheet" href="/clientplatform/cockpit/styles.css">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script defer src="/clientplatform/cockpit/app.js"></script>
<script defer src="/clientplatform/cockpit/customers.js"></script>
</head><body>
<main class="shell">
<header><div><p class="eyebrow">ClientPlatform</p><h1>Ваш бизнес</h1></div><span id="role" class="pill">Проверяем доступ…</span></header>
<section class="business"><label for="business-select">Какой бизнес открыт</label><select id="business-select" disabled><option>Загрузка…</option></select></section>
<section id="status" class="status"><span id="status-text">Проверяем безопасный вход через Telegram…</span><button id="status-action" class="secondary" type="button" hidden>Вернуться в бот</button></section>
<section id="navigation" class="grid" aria-live="polite"></section>
<section id="home-view" class="home-view" aria-live="polite" hidden><div class="view-toolbar"><button id="home-back" class="secondary" type="button">Все разделы</button><button id="home-refresh" class="secondary" type="button">Обновить</button></div><div class="home-heading"><p class="eyebrow">Главный экран</p><h2>Сегодня</h2><p id="home-meta"></p></div><div id="home-metrics" class="metrics"></div><div id="home-money" class="money"></div><section id="home-attention-block" class="home-block"><h3>Требует внимания</h3><div id="home-attention"></div></section><section id="home-actions-block" class="home-block"><h3>Что посмотреть дальше</h3><div id="home-actions"></div></section><p id="home-empty" class="muted"></p><p id="home-limitations" class="muted"></p></section>
<section id="customers-view" class="customers-view" aria-live="polite" hidden>
<div class="view-toolbar"><button id="customers-back" class="secondary" type="button">Все разделы</button><button id="customers-refresh" class="secondary" type="button">Обновить</button></div>
<div id="customer-list-panel"><div class="home-heading"><p class="eyebrow">CRM</p><h2>Клиенты</h2><p>Найдите человека и сразу увидьте историю и следующий шаг.</p></div>
<form id="customer-search-form" class="customer-search"><label for="customer-search">Имя, username, email или телефон</label><div><input id="customer-search" type="search" maxlength="100" autocomplete="off" placeholder="Например: Анна"><button type="submit">Найти</button></div></form>
<p id="customer-list-meta" class="muted"></p><div id="customer-list"></div><div class="pager"><button id="customer-prev" class="secondary" type="button" disabled>Назад</button><button id="customer-next" class="secondary" type="button" disabled>Дальше</button></div></div>
<section id="customer-detail" hidden><button id="customer-detail-back" class="secondary" type="button">К списку клиентов</button><div class="home-heading"><p class="eyebrow">Карточка клиента</p><h2 id="customer-detail-name">Клиент</h2><p id="customer-detail-meta"></p></div><section class="home-block"><h3>Контакты</h3><div id="customer-contacts"></div></section><section class="home-block"><h3>Следующий шаг</h3><div id="customer-action"></div></section><section class="home-block"><h3>История</h3><div id="customer-timeline"></div></section><p id="customer-limitations" class="muted"></p></section>
</section>
<section id="explanation" class="explanation" hidden><button id="close-explanation" class="secondary" type="button">К разделам</button><h2 id="explanation-title"></h2><p id="explanation-summary"></p><p id="explanation-when"></p><p id="explanation-reason"></p></section>
</main></body></html>"""

_CSS = """:root{--bg:var(--tg-theme-bg-color,#f4f6f8);--surface:var(--tg-theme-secondary-bg-color,#fff);--text:var(--tg-theme-text-color,#17202a);--hint:var(--tg-theme-hint-color,#66717d);--link:var(--tg-theme-link-color,#2678d9);--button:var(--tg-theme-button-color,#2678d9);--button-text:var(--tg-theme-button-text-color,#fff);--border:rgba(127,127,127,.24)}*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text);padding:0 0 env(safe-area-inset-bottom)}button,select{font:inherit;color:inherit}.shell{max-width:760px;margin:0 auto;padding:calc(18px + env(safe-area-inset-top)) 16px calc(40px + env(safe-area-inset-bottom))}header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:16px}.eyebrow{margin:0 0 4px;font-size:12px;font-weight:800;letter-spacing:.045em;color:var(--hint)}h1{margin:0;font-size:29px;line-height:1.1}h2,h3{color:var(--text)}.pill{font-size:12px;background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:8px 10px;max-width:46%;text-align:center}.business,.status,.explanation,.home-view,.customers-view{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:14px;margin-bottom:14px}.business label{display:block;font-size:13px;font-weight:750;margin-bottom:8px}select{width:100%;min-height:46px;border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:0 12px}.status{font-size:14px;line-height:1.4}.status .secondary{margin-top:10px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{min-height:126px;text-align:left;border:1px solid var(--border);border-radius:16px;background:var(--surface);padding:15px;position:relative}.card h2{font-size:17px;margin:0 0 7px;padding-right:56px}.card p{font-size:13px;line-height:1.38;margin:0;color:var(--hint)}.card.planned{border-style:dashed}.card.restricted{opacity:.72}.badge{position:absolute;right:10px;top:10px;font-size:10px;font-weight:800;border-radius:999px;padding:4px 7px;background:var(--bg);color:var(--hint)}.badge.available{background:var(--button);color:var(--button-text)}.explanation h2{margin:14px 0 8px}.explanation p{line-height:1.5}.secondary,.action-card{min-height:44px;border:1px solid var(--border);border-radius:12px;padding:0 14px;background:var(--bg);font-weight:700}.view-toolbar{display:flex;justify-content:space-between;gap:10px}.home-heading h2{margin:14px 0 4px}.home-heading p{margin:0 0 12px;color:var(--hint)}.metrics,.money{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}.metric,.money-card,.attention-card{border:1px solid var(--border);border-radius:14px;padding:12px}.metric strong,.money-card strong{display:block;font-size:24px;margin-top:4px}.metric span,.money-card span,.muted{font-size:12px;color:var(--hint);line-height:1.4}.home-block{margin-top:18px}.home-block h3{margin:0 0 9px;font-size:16px}.attention-card{margin-bottom:8px}.action-card{display:block;width:100%;text-align:left;margin-bottom:8px}.action-card small{display:block;font-weight:400;margin-top:4px;color:var(--hint);line-height:1.35}.customer-search label{display:block;font-size:13px;font-weight:750;margin-bottom:8px}.customer-search>div{display:flex;gap:8px}.customer-search input{min-width:0;flex:1;min-height:44px;border:1px solid var(--border);border-radius:12px;background:var(--surface);color:var(--text);padding:0 12px}.customer-search button{min-height:44px;border:0;border-radius:12px;background:var(--button);color:var(--button-text);padding:0 16px;font-weight:800}.customer-row{display:block;width:100%;text-align:left;border:1px solid var(--border);border-radius:14px;background:var(--surface);padding:13px;margin-bottom:8px}.customer-row strong{display:block}.customer-row small,.contact-card small,.timeline-card small{display:block;color:var(--hint);margin-top:4px}.pager{display:flex;justify-content:space-between;gap:10px;margin-top:12px}.contact-card,.timeline-card{border:1px solid var(--border);border-radius:14px;padding:12px;margin-bottom:8px}.busy{opacity:.66;pointer-events:none}@media(max-width:520px){.grid,.metrics,.money{grid-template-columns:1fr}.shell{padding-left:12px;padding-right:12px}h1{font-size:27px}.pill{max-width:52%}.card{min-height:auto}.view-toolbar{position:sticky;top:env(safe-area-inset-top);z-index:2;background:var(--surface);padding:2px 0 8px}}"""

_JS = r"""(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const statusText = document.getElementById('status-text');
  const statusAction = document.getElementById('status-action');
  const nav = document.getElementById('navigation');
  const select = document.getElementById('business-select');
  const role = document.getElementById('role');
  const explanation = document.getElementById('explanation');
  const title = document.getElementById('explanation-title');
  const summary = document.getElementById('explanation-summary');
  const when = document.getElementById('explanation-when');
  const reason = document.getElementById('explanation-reason');
  const close = document.getElementById('close-explanation');
  const home = document.getElementById('home-view');
  const homeBack = document.getElementById('home-back');
  const homeRefresh = document.getElementById('home-refresh');
  const homeMeta = document.getElementById('home-meta');
  const homeMetrics = document.getElementById('home-metrics');
  const homeMoney = document.getElementById('home-money');
  const homeAttentionBlock = document.getElementById('home-attention-block');
  const homeActionsBlock = document.getElementById('home-actions-block');
  const homeAttention = document.getElementById('home-attention');
  const homeActions = document.getElementById('home-actions');
  const homeEmpty = document.getElementById('home-empty');
  const homeLimitations = document.getElementById('home-limitations');
  const initData = tg && typeof tg.initData === 'string' ? tg.initData : '';
  const roleNames = {owner:'Владелец',administrator:'Администратор',manager:'Менеджер',marketer:'Маркетолог',analyst:'Аналитик',content_manager:'Контент-менеджер',support:'Поддержка',customer:'Клиент'};
  const periodNames = {'7d':'7 дней','30d':'30 дней','today':'сегодня'};
  let navigationItems = [];
  let currentView = 'home';
  let lastHomePayload = null;

  const text = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const screenStatus = (item) => {
    if (item.status === 'restricted') return 'restricted';
    if (item.status === 'available') return 'available';
    return 'planned';
  };
  const syncBackButton = () => {
    if (!tg || !tg.BackButton) return;
    if (currentView === 'home') tg.BackButton.hide(); else tg.BackButton.show();
  };
  const showNavigation = () => { currentView = 'navigation'; explanation.hidden = true; home.hidden = true; document.getElementById('customers-view').hidden = true; nav.hidden = false; syncBackButton(); };
  const showHomeView = () => { currentView = 'home'; explanation.hidden = true; document.getElementById('customers-view').hidden = true; nav.hidden = true; home.hidden = false; syncBackButton(); };
  const enterCustomers = () => { currentView = 'customers'; syncBackButton(); };
  window.ClientPlatformCockpitNavigation = Object.freeze({showNavigation, enterCustomers});
  const setHomeBusy = (busy) => { home.classList.toggle('busy', Boolean(busy)); homeRefresh.disabled = Boolean(busy); home.setAttribute('aria-busy', busy ? 'true' : 'false'); };
  const closeToBot = () => { if (tg && typeof tg.close === 'function') tg.close(); else window.history.back(); };
  statusAction.addEventListener('click', closeToBot);
  close.addEventListener('click', showNavigation);
  homeBack.addEventListener('click', showNavigation);

  const showExplanation = (item) => {
    const state = screenStatus(item);
    currentView = 'explanation';
    text(title, item.title); text(summary, item.summary); text(when, `Когда пригодится: ${item.when_to_use}`);
    if (state === 'planned') text(reason, 'Экран этого раздела ещё подключается. Пока используйте «Сегодня» и быстрые команды в боте.');
    else if (state === 'restricted') text(reason, item.reason || 'Для Вашей роли этот раздел недоступен. Если он нужен, попросите владельца бизнеса изменить доступ.');
    else text(reason, item.reason || 'Раздел доступен.');
    nav.hidden = true; home.hidden = true; document.getElementById('customers-view').hidden = true; explanation.hidden = false; syncBackButton();
  };

  const appendMetric = (container, label, value, note) => {
    const card = document.createElement('div'); card.className = container === homeMoney ? 'money-card' : 'metric';
    const caption = document.createElement('span'); const number = document.createElement('strong'); const meaning = document.createElement('span');
    text(caption, label); text(number, value); text(meaning, note); card.append(caption, number, meaning); container.appendChild(card);
  };

  const renderHome = (payload) => {
    lastHomePayload = payload;
    homeMetrics.replaceChildren(); homeMoney.replaceChildren(); homeAttention.replaceChildren(); homeActions.replaceChildren();
    text(homeMeta, `${payload.business_name} · данные на сегодня`);
    for (const item of payload.metrics || []) appendMetric(homeMetrics, item.title, item.value, item.meaning);
    for (const item of payload.money || []) appendMetric(homeMoney, `Подтверждённая выручка · ${periodNames[item.period] || item.period}`, item.display, item.meaning);
    for (const item of payload.attention || []) { const card = document.createElement('div'); card.className = 'attention-card'; text(card, item); homeAttention.appendChild(card); }
    homeAttentionBlock.hidden = !(payload.attention || []).length;
    for (const item of payload.actions || []) {
      const target = navigationItems.find((entry) => entry.id === item.section); const state = target ? screenStatus(target) : 'planned';
      const button = document.createElement('button'); button.type = 'button'; button.className = 'action-card';
      const label = document.createElement('span'); const detail = document.createElement('small'); const cleanTitle = String(item.title || '').replace(/^Открыть:\s*/, '');
      text(label, state === 'available' ? cleanTitle : `Подробнее: ${cleanTitle}`);
      text(detail, state === 'available' ? item.reason : `${item.reason} Экран раздела пока подключается.`);
      button.append(label, detail); button.addEventListener('click', () => { if (target) showItem(target); }); homeActions.appendChild(button);
    }
    homeActionsBlock.hidden = !(payload.actions || []).length;
    text(homeEmpty, payload.empty_message || '');
    text(homeLimitations, (payload.limitations || []).length ? 'Некоторые данные сейчас временно недоступны. Остальная информация показана без догадок.' : '');
    showHomeView();
  };

  const post = async (path, businessId, extra) => {
    const body = {init_data: initData, ...(extra || {})}; if (businessId) body.business_id = businessId;
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, credentials:'same-origin', cache:'no-store', body:JSON.stringify(body)});
    const payload = await response.json().catch(() => ({error:'invalid_response'})); if (!response.ok) throw new Error(payload.error || 'access_denied'); return payload;
  };
  const loadHome = async () => { showHomeView(); setHomeBusy(true); text(homeMeta, 'Обновляем данные…'); try { renderHome(await post('/clientplatform/cockpit/home', select.value)); } finally { setHomeBusy(false); } };
  homeRefresh.addEventListener('click', () => loadHome().catch(homeFail));
  const homeFail = (error) => {
    setHomeBusy(false);
    if (error && ['expired_init_data','business_access_denied','access_denied'].includes(error.message)) { fail(error); return; }
    homeMetrics.replaceChildren(); homeMoney.replaceChildren(); homeAttention.replaceChildren(); homeActions.replaceChildren(); homeAttentionBlock.hidden = true; homeActionsBlock.hidden = true;
    text(homeMeta, 'Не удалось обновить сводку'); text(homeEmpty, 'Сводка временно недоступна. Нажмите «Обновить» или откройте список разделов.'); text(homeLimitations, 'Ваши данные и права доступа не менялись.'); showHomeView();
  };
  const openSectionRoute = async (item) => {
    const payload = await post('/clientplatform/cockpit/section-route', select.value, {section:item.id});
    const url = String(payload.route_url || '');
    if (!url.startsWith('https://t.me/')) throw new Error('section_route_unavailable');
    if (tg && typeof tg.openTelegramLink === 'function') tg.openTelegramLink(url);
    else window.location.assign(url);
  };
  const showItem = (item) => {
    const state = screenStatus(item);
    if (state !== 'available') { showExplanation(item); return; }
    if (item.id === 'home') { loadHome().catch(homeFail); return; }
    if (item.id === 'customers' && window.ClientPlatformCustomers) { window.ClientPlatformCustomers.open(); return; }
    openSectionRoute(item).catch(() => {
      currentView = 'explanation';
      text(title, item.title); text(summary, item.summary); text(when, `Когда пригодится: ${item.when_to_use}`);
      text(reason, 'Не удалось открыть раздел в боте. Вернитесь к разделам и попробуйте ещё раз.');
      nav.hidden = true; home.hidden = true; document.getElementById('customers-view').hidden = true; explanation.hidden = false; syncBackButton();
    });
  };

  const render = (payload) => {
    nav.replaceChildren(); select.replaceChildren(); navigationItems = payload.navigation || [];
    text(role, payload.role ? `Роль: ${roleNames[payload.role] || payload.role}` : 'Нужен бизнес'); statusAction.hidden = true;
    if (payload.onboarding_required) { text(statusText, 'У Вас пока нет подключённого бизнеса. Вернитесь в бот и нажмите «Подключить мой бизнес».'); select.disabled = true; statusAction.hidden = false; showNavigation(); return; }
    for (const business of payload.businesses || []) { const option = document.createElement('option'); option.value = business.id; text(option, `${business.name} · ${roleNames[business.role] || business.role}`); option.selected = Boolean(business.selected); select.appendChild(option); }
    select.disabled = false; text(statusText, `Открыт бизнес «${payload.business_name}». Сначала показываем главное на сегодня.`);
    for (const item of navigationItems) {
      const state = screenStatus(item); const button = document.createElement('button'); button.type = 'button'; button.className = `card ${state}`;
      const heading = document.createElement('h2'); const copy = document.createElement('p'); const badge = document.createElement('span'); badge.className = `badge ${state}`;
      const nativeHere = ['home','customers'].includes(item.id);
      text(heading, item.title); text(copy, item.summary); text(badge, state === 'available' ? (nativeHere ? 'Работает' : 'Открыть') : state === 'planned' ? 'Скоро' : 'Нет доступа'); button.append(heading, copy, badge); button.addEventListener('click', () => showItem(item)); nav.appendChild(button);
    }
    loadHome().catch(homeFail);
  };
  const load = async (businessId) => { select.disabled = true; text(statusText, 'Проверяем доступ и загружаем бизнес…'); return render(await post('/clientplatform/cockpit/context', businessId)); };
  select.addEventListener('change', () => load(select.value).catch(fail));
  function fail(error) {
    nav.replaceChildren(); home.hidden = true; document.getElementById('customers-view').hidden = true; explanation.hidden = true; select.disabled = true; currentView = 'navigation'; syncBackButton(); text(role, 'Доступ не подтверждён'); statusAction.hidden = false;
    text(statusText, error && error.message === 'expired_init_data' ? 'Сессия Telegram устарела. Вернитесь в бот и откройте кабинет ещё раз.' : 'Не удалось подтвердить безопасный доступ. Вернитесь в бот и откройте кабинет ещё раз.');
  }
  if (tg && tg.BackButton && typeof tg.BackButton.onClick === 'function') tg.BackButton.onClick(() => {
    if (currentView === 'customers' && window.ClientPlatformCustomers) { window.ClientPlatformCustomers.back(); return; }
    if (currentView === 'explanation') { showNavigation(); return; }
    if (currentView === 'navigation') { if (lastHomePayload) renderHome(lastHomePayload); else loadHome().catch(homeFail); }
  });
  if (!initData) { fail(new Error('missing_init_data')); }
  else { if (tg) { tg.ready(); tg.expand(); } load(null).catch(fail); }
})();"""


def cockpit_http_enabled() -> bool:
    return bool(str(getattr(settings, "BOT_TOKEN", "") or "").strip())


def _base_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
    }


def _shell_headers() -> dict[str, str]:
    headers = _base_headers()
    headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; form-action 'none'; "
        "script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )
    return headers


async def cockpit_shell(_request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html", charset="utf-8", headers=_shell_headers())


async def cockpit_script(_request: web.Request) -> web.Response:
    return web.Response(text=_JS, content_type="application/javascript", charset="utf-8", headers=_base_headers())


async def cockpit_styles(_request: web.Request) -> web.Response:
    return web.Response(text=_CSS, content_type="text/css", charset="utf-8", headers=_base_headers())


async def cockpit_customers_script(_request: web.Request) -> web.Response:
    return web.Response(
        text=_CUSTOMERS_SCRIPT.read_text(encoding="utf-8"),
        content_type="application/javascript",
        charset="utf-8",
        headers=_base_headers(),
    )


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"ok": False, "error": code}, status=status, headers=_base_headers())


def _telegram_action_url(start_payload: str) -> str | None:
    target = next(
        (
            item
            for item in build_entry_targets(start_payload)
            if item.get("platform") == MessengerPlatform.TELEGRAM.value
        ),
        None,
    )
    if target is None:
        return None
    url = str(target.get("url") or "").strip()
    return url or None


async def _verified_payload_scope(
    request: web.Request,
) -> tuple[int, str | None, dict[str, Any]] | web.Response:
    try:
        raw = await request.read()
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _error(400, "invalid_json")
    except json.JSONDecodeError:
        return _error(400, "invalid_json")
    except OSError:
        return _error(400, "body_read_failed")
    except ValueError:
        return _error(400, "body_read_failed")
    if not isinstance(payload, dict):
        return _error(400, "invalid_json")
    init_data = payload.get("init_data")
    requested_business = payload.get("business_id")
    if not isinstance(init_data, str):
        return _error(400, "init_data_required")
    if requested_business is not None and not isinstance(requested_business, str):
        return _error(400, "invalid_business_id")

    max_age = env_int("CLIENTPLATFORM_COCKPIT_INIT_DATA_MAX_AGE_SEC", 300, minimum=60, maximum=3600)
    try:
        principal = verify_telegram_webapp_init_data(
            init_data,
            bot_token=str(getattr(settings, "BOT_TOKEN", "") or ""),
            max_age_seconds=max_age,
        )
    except TelegramWebAppAuthError as exc:
        code = "expired_init_data" if "expired" in str(exc) else "invalid_init_data"
        return _error(401, code)
    return principal.user_id, requested_business, payload


async def _verified_scope(request: web.Request) -> tuple[int, str | None] | web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, _payload = scope
    return user_id, requested_business


async def cockpit_context(request: web.Request) -> web.Response:
    scope = await _verified_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business = scope
    try:
        context = await asyncio.to_thread(
            resolve_cockpit_context,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except ValueError:
        return _error(400, "invalid_business_id")
    return web.json_response({"ok": True, **context.as_dict()}, headers=_base_headers())


async def cockpit_home(request: web.Request) -> web.Response:
    scope = await _verified_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business = scope
    try:
        home = await asyncio.to_thread(
            resolve_cockpit_home,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
        )
    except (TenantAccessDenied, TenantPermissionDenied):
        return _error(403, "business_access_denied")
    except ValueError:
        return _error(400, "invalid_business_id")
    except CockpitHomeUnavailable:
        return _error(503, "home_unavailable")
    return web.json_response({"ok": True, **home.as_dict()}, headers=_base_headers())


async def cockpit_customers(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        return _error(400, "invalid_customer_request")
    try:
        page = await asyncio.to_thread(
            resolve_cockpit_customer_page,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            query=query,
            limit=payload.get("limit", 20),
            offset=payload.get("offset", 0),
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "customer_access_denied")
    except ValueError:
        return _error(400, "invalid_customer_request")
    return web.json_response({"ok": True, **page.as_dict()}, headers=_base_headers())


async def cockpit_customer_detail(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    customer_id = payload.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id.strip():
        return _error(400, "customer_id_required")
    try:
        detail = await asyncio.to_thread(
            resolve_cockpit_customer_detail,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            customer_id=customer_id,
            timeline_limit=payload.get("timeline_limit", 20),
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "customer_access_denied")
    except CustomerNotFound:
        return _error(404, "customer_not_found")
    except ValueError:
        return _error(400, "invalid_customer_request")
    return web.json_response({"ok": True, **detail.as_dict()}, headers=_base_headers())


async def cockpit_section_route(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    section = payload.get("section")
    if not isinstance(section, str) or not section.strip() or len(section) > 40:
        return _error(400, "invalid_section")
    try:
        start_payload = await asyncio.to_thread(
            resolve_cockpit_section_start_payload,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            section=section,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "section_access_denied")
    except ValueError:
        return _error(400, "invalid_section")
    route_url = _telegram_action_url(start_payload)
    if route_url is None:
        return _error(503, "section_route_unavailable")
    return web.json_response({"ok": True, "route_url": route_url}, headers=_base_headers())


async def cockpit_customer_action_route(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    customer_id = payload.get("customer_id")
    expected_action_key = payload.get("expected_action_key")
    if not isinstance(customer_id, str) or not customer_id.strip():
        return _error(400, "customer_id_required")
    if (
        expected_action_key is not None
        and (
            not isinstance(expected_action_key, str)
            or not expected_action_key.strip()
            or len(expected_action_key) > 100
        )
    ):
        return _error(400, "invalid_customer_request")
    try:
        route = await asyncio.to_thread(
            resolve_cockpit_customer_action_route,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            customer_id=customer_id,
            expected_action_key=expected_action_key,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "customer_access_denied")
    except CustomerNotFound:
        return _error(404, "customer_not_found")
    except CockpitCustomerActionUnavailable:
        return _error(409, "customer_action_changed")
    except ValueError:
        return _error(400, "invalid_customer_request")
    route_url = _telegram_action_url(route.start_payload)
    if route_url is None:
        return _error(503, "customer_action_route_unavailable")
    return web.json_response(
        {
            "ok": True,
            "schema_version": route.schema_version,
            "route_url": route_url,
        },
        headers=_base_headers(),
    )


def register_cockpit_routes(app: web.Application) -> None:
    app.router.add_get(_COCKPIT_PREFIX, cockpit_shell)
    app.router.add_get(f"{_COCKPIT_PREFIX}/app.js", cockpit_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/styles.css", cockpit_styles)
    app.router.add_get(f"{_COCKPIT_PREFIX}/customers.js", cockpit_customers_script)
    app.router.add_post(f"{_COCKPIT_PREFIX}/context", cockpit_context)
    app.router.add_post(f"{_COCKPIT_PREFIX}/home", cockpit_home)
    app.router.add_post(f"{_COCKPIT_PREFIX}/section-route", cockpit_section_route)
    app.router.add_post(f"{_COCKPIT_PREFIX}/customers", cockpit_customers)
    app.router.add_post(
        f"{_COCKPIT_PREFIX}/customers/detail", cockpit_customer_detail
    )
    app.router.add_post(
        f"{_COCKPIT_PREFIX}/customers/action-route", cockpit_customer_action_route
    )
    app[_COCKPIT_APP_KEY] = True


__all__ = [
    "cockpit_context",
    "cockpit_customer_action_route",
    "cockpit_customer_detail",
    "cockpit_customers",
    "cockpit_customers_script",
    "cockpit_home",
    "cockpit_http_enabled",
    "cockpit_script",
    "cockpit_section_route",
    "cockpit_shell",
    "cockpit_styles",
    "register_cockpit_routes",
]
