from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from aiogram.exceptions import TelegramAPIError

from clientplatform.application.cockpit import (
    resolve_cockpit_context,
    resolve_cockpit_section_start_payload,
)
from clientplatform.application.cockpit_action_routing import (
    build_cockpit_section_start_payload,
    parse_cockpit_action_start_payload,
)
from clientplatform.application.cockpit_home import (
    CockpitHomeUnavailable,
    resolve_cockpit_home,
)
from clientplatform.application.cockpit_calendar import resolve_cockpit_calendar
from clientplatform.application.cockpit_calendar_management import (
    cancel_cockpit_calendar_slot,
    create_cockpit_calendar_slot,
    replace_cockpit_calendar_slot,
    resolve_cockpit_calendar_management,
)
from clientplatform.application.cockpit_connections import (
    issue_cockpit_messenger_setup,
    resolve_cockpit_connections,
)
from clientplatform.application.cockpit_sales import resolve_cockpit_sales
from clientplatform.application.cockpit_sales_management import (
    CockpitSalesLeadManagement,
    add_cockpit_sales_note,
    assign_cockpit_sales_lead,
    reopen_cockpit_sales_lead,
    resolve_cockpit_sales_lead,
    set_cockpit_sales_next_action,
    set_cockpit_sales_stage,
    unassign_cockpit_sales_lead,
)
from clientplatform.application.cockpit_settings import (
    resolve_cockpit_settings,
    update_cockpit_settings,
)
from clientplatform.application.cockpit_customers import (
    CockpitCustomerActionUnavailable,
    resolve_cockpit_customer_action_route,
    resolve_cockpit_customer_detail,
    resolve_cockpit_customer_page,
)
from clientplatform.domain.activity import ActivityInvariantViolation
from clientplatform.domain.bookings import BookingInvariantViolation, BookingNotFound
from clientplatform.domain.customers import CustomerNotFound
from clientplatform.domain.sales import SalesInvariantViolation, SalesLeadNotFound
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
_COCKPIT_BOT_APP_KEY = web.AppKey("clientplatform_cockpit_bot", object)
_COCKPIT_SECTION_SENDER_APP_KEY = web.AppKey("clientplatform_cockpit_section_sender", object)
_COCKPIT_ACTION_SENDER_APP_KEY = web.AppKey("clientplatform_cockpit_action_sender", object)
_CUSTOMERS_SCRIPT = Path(__file__).with_name("cockpit_customers.js")
_CALENDAR_SCRIPT = Path(__file__).with_name("cockpit_calendar.js")
_SALES_SCRIPT = Path(__file__).with_name("cockpit_sales.js")
_CONNECTIONS_SCRIPT = Path(__file__).with_name("cockpit_connections.js")
_SETTINGS_SCRIPT = Path(__file__).with_name("cockpit_settings.js")


class _BotMessageTarget:
    __slots__ = ("_bot", "_chat_id")

    def __init__(self, bot: Any, *, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = int(chat_id)

    async def answer(self, text: str, **kwargs: Any) -> Any:
        return await self._bot.send_message(chat_id=self._chat_id, text=text, **kwargs)

_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ClientPlatform · Кабинет</title>
<link rel="stylesheet" href="/clientplatform/cockpit/styles.css">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script defer src="/clientplatform/cockpit/app.js"></script>
<script defer src="/clientplatform/cockpit/customers.js"></script>
<script defer src="/clientplatform/cockpit/calendar.js"></script>
<script defer src="/clientplatform/cockpit/sales.js"></script>
<script defer src="/clientplatform/cockpit/connections.js"></script>
<script defer src="/clientplatform/cockpit/settings.js"></script>
</head><body>
<main class="shell">
<header><div><p class="eyebrow">ClientPlatform</p><h1>Ваш бизнес</h1></div><span id="role" class="pill">Проверяем доступ…</span></header>
<section class="business"><label for="business-select">Какой бизнес открыт</label><select id="business-select" disabled><option>Загрузка…</option></select></section>
<section id="status" class="status"><span id="status-text">Проверяем безопасный вход через Telegram…</span><button id="status-action" class="secondary" type="button" hidden>Вернуться в бот</button></section>
<section id="navigation" class="grid" aria-live="polite"></section>
<section id="home-view" class="workspace-view home-view" aria-live="polite" hidden>
<div class="view-toolbar"><button id="home-back" class="secondary" type="button">Все разделы</button><button id="home-refresh" class="secondary" type="button">Обновить</button></div>
<div class="home-heading"><p class="eyebrow">Главный экран</p><h2>Сегодня</h2><p id="home-meta"></p></div>
<section id="home-primary-block" class="home-block primary-block" hidden><p class="eyebrow">Главное действие</p><h3>Что сделать сейчас</h3><div id="home-primary-action"></div></section>
<div id="home-metrics" class="metrics"></div><div id="home-money" class="money"></div>
<section id="home-attention-block" class="home-block"><h3>Требует внимания</h3><div id="home-attention"></div></section>
<section id="home-actions-block" class="home-block"><h3>Что посмотреть дальше</h3><div id="home-actions"></div></section>
<p id="home-empty" class="muted"></p><p id="home-limitations" class="muted"></p>
</section>
<section id="customers-view" class="workspace-view customers-view" aria-live="polite" hidden>
<div class="view-toolbar"><button id="customers-back" class="secondary" type="button">Все разделы</button><button id="customers-refresh" class="secondary" type="button">Обновить</button></div>
<div id="customer-list-panel"><div class="home-heading"><p class="eyebrow">CRM</p><h2>Клиенты</h2><p>Найдите человека и сразу увидьте историю и следующий шаг.</p></div>
<form id="customer-search-form" class="customer-search"><label for="customer-search">Имя, username, email или телефон</label><div><input id="customer-search" type="search" maxlength="100" autocomplete="off" placeholder="Например: Анна"><button type="submit">Найти</button></div></form>
<p id="customer-list-meta" class="muted"></p><div id="customer-list"></div><div class="pager"><button id="customer-prev" class="secondary" type="button" disabled>Назад</button><button id="customer-next" class="secondary" type="button" disabled>Дальше</button></div></div>
<section id="customer-detail" hidden><button id="customer-detail-back" class="secondary" type="button">К списку клиентов</button><div class="home-heading"><p class="eyebrow">Карточка клиента</p><h2 id="customer-detail-name">Клиент</h2><p id="customer-detail-meta"></p></div><section class="home-block"><h3>Контакты</h3><div id="customer-contacts"></div></section><section class="home-block"><h3>Следующий шаг</h3><div id="customer-action"></div></section><section class="home-block"><h3>История</h3><div id="customer-timeline"></div></section><p id="customer-limitations" class="muted"></p></section>
</section>
<section id="calendar-view" class="workspace-view" aria-live="polite" hidden>
<div class="view-toolbar"><button id="calendar-more" class="secondary" type="button">Все разделы</button><button id="calendar-refresh" class="secondary" type="button">Обновить</button></div>
<div class="home-heading"><p class="eyebrow">Расписание</p><h2>Записи</h2><p id="calendar-meta"></p></div>
<div id="calendar-list"></div><p id="calendar-empty" class="muted"></p><p id="calendar-limitations" class="muted"></p>
<button id="calendar-manage" class="primary-cta" type="button" hidden>Добавить свободное время</button>
<section id="calendar-manage-panel" class="home-block" hidden><h3 id="calendar-form-title">Добавить свободное время</h3>
<form id="calendar-form" class="calendar-form"><label for="calendar-offering">Услуга</label><select id="calendar-offering" required></select><label for="calendar-start">Дата и время бизнеса</label><input id="calendar-start" type="datetime-local" required><label for="calendar-duration">Длительность, минут</label><input id="calendar-duration" type="number" min="15" max="1440" step="5" value="60" required><button id="calendar-save" class="primary-cta" type="submit">Опубликовать время</button><button id="calendar-form-cancel" class="secondary calendar-form-cancel" type="button" hidden>Отменить изменение</button></form>
<p id="calendar-manage-message" class="muted"></p></section>
<button id="calendar-advanced" class="secondary calendar-advanced" type="button">Дополнительные действия в боте</button>
</section>
<section id="sales-view" class="workspace-view" aria-live="polite" hidden>
<div class="view-toolbar"><button id="sales-more" class="secondary" type="button">Все разделы</button><button id="sales-refresh" class="secondary" type="button">Обновить</button></div>
<div id="sales-list-panel"><div class="home-heading"><p class="eyebrow">Работа с клиентами</p><h2>Продажи</h2><p id="sales-meta"></p></div>
<p id="sales-handoff" class="muted"></p><div id="sales-list"></div><p id="sales-empty" class="muted"></p><p id="sales-limitations" class="muted"></p></div>
<section id="sales-detail" hidden><button id="sales-detail-back" class="secondary" type="button">К очереди продаж</button><div class="home-heading"><p class="eyebrow">Сделка</p><h2 id="sales-detail-name">Клиент</h2><p id="sales-detail-meta"></p></div>
<div class="sales-detail-actions"><button id="sales-open-customer" class="secondary" type="button">Открыть карточку клиента</button><button id="sales-assignment" class="secondary" type="button"></button></div>
<section id="sales-stage-block" class="home-block"><h3>Этап продажи</h3><div id="sales-stage-actions" class="sales-stage-actions"></div></section>
<section id="sales-next-block" class="home-block"><h3>Следующий шаг</h3><form id="sales-next-form" class="sales-form"><label for="sales-next-action">Что сделать</label><input id="sales-next-action" maxlength="500" placeholder="Например: отправить предложение"><label for="sales-next-due">Срок по времени бизнеса</label><input id="sales-next-due" type="datetime-local"><button id="sales-next-save" class="primary-cta" type="submit">Сохранить следующий шаг</button></form></section>
<section class="home-block"><h3>Заметка</h3><form id="sales-note-form" class="sales-form"><textarea id="sales-note" maxlength="4000" rows="4" placeholder="Что важно помнить по клиенту" required></textarea><button id="sales-note-save" class="secondary sales-wide" type="submit">Добавить заметку</button></form><p id="sales-note-message" class="muted"></p></section>
<section id="sales-result-block" class="home-block"><h3>Результат</h3><label class="sales-result-label" for="sales-result-reason">Комментарий к результату</label><textarea id="sales-result-reason" maxlength="500" rows="3" placeholder="Например: оплатил счёт / выбрал другой вариант"></textarea><div class="sales-result-actions"><button id="sales-won" type="button">Клиент оплатил</button><button id="sales-lost" class="secondary" type="button">Не состоялось</button></div></section>
<button id="sales-reopen" class="primary-cta" type="button" hidden>Вернуть в работу</button><p id="sales-detail-message" class="muted"></p></section>
<button id="sales-manage" class="secondary sales-advanced" type="button">Дополнительные действия в боте</button>
</section>
<section id="connections-view" class="workspace-view" aria-live="polite" hidden>
<div class="view-toolbar"><button id="connections-more" class="secondary" type="button">Все разделы</button><button id="connections-refresh" class="secondary" type="button">Обновить</button></div>
<div class="home-heading"><p class="eyebrow">Каналы бизнеса</p><h2>Подключения</h2><p id="connections-meta"></p></div>
<p class="muted">Здесь видно реальное состояние Telegram, ВКонтакте и MAX. Новый токен вводится только на защищённой одноразовой HTTPS-странице.</p>
<div id="connections-list"></div><p id="connections-empty" class="muted"></p>
</section>
<section id="settings-view" class="workspace-view" aria-live="polite" hidden>
<div class="view-toolbar"><button id="settings-more" class="secondary" type="button">Все разделы</button><button id="settings-refresh" class="secondary" type="button">Обновить</button></div>
<div class="home-heading"><p class="eyebrow">Бизнес</p><h2>Настройки</h2><p id="settings-meta"></p></div>
<form id="settings-form" class="settings-form">
<label for="settings-business-name">Название бизнеса</label><input id="settings-business-name" maxlength="160" autocomplete="organization" required>
<label for="settings-activity">Чем занимается бизнес</label><textarea id="settings-activity" maxlength="2000" rows="5" required></textarea>
<label for="settings-timezone">Часовой пояс</label><input id="settings-timezone" maxlength="100" autocomplete="off" placeholder="Europe/Moscow" required>
<button id="settings-save" class="primary-cta" type="submit">Сохранить настройки</button>
</form><p id="settings-message" class="muted"></p>
</section>
<section id="explanation" class="explanation" hidden><button id="close-explanation" class="secondary" type="button">К разделам</button><h2 id="explanation-title"></h2><p id="explanation-summary"></p><p id="explanation-when"></p><p id="explanation-reason"></p></section>
</main>
<nav id="primary-nav" class="primary-nav" aria-label="Основная навигация" hidden>
<button type="button" data-primary="home"><span class="primary-icon">●</span><span>Сегодня</span></button>
<button type="button" data-primary="customers"><span class="primary-icon">●</span><span>Клиенты</span></button>
<button type="button" data-primary="calendar"><span class="primary-icon">●</span><span>Записи</span></button>
<button type="button" data-primary="sales"><span class="primary-icon">●</span><span>Продажи</span></button>
<button type="button" data-primary="more"><span class="primary-icon">•••</span><span>Ещё</span></button>
</nav>
</body></html>"""

_CSS = """:root{--bg:var(--tg-theme-bg-color,#f4f6f8);--surface:var(--tg-theme-secondary-bg-color,#fff);--text:var(--tg-theme-text-color,#17202a);--hint:var(--tg-theme-hint-color,#66717d);--link:var(--tg-theme-link-color,#2678d9);--button:var(--tg-theme-button-color,#2678d9);--button-text:var(--tg-theme-button-text-color,#fff);--border:rgba(127,127,127,.24)}*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text);padding:0}button,select,input,textarea{font:inherit;color:inherit}.shell{max-width:760px;margin:0 auto;padding:calc(18px + env(safe-area-inset-top)) 16px calc(104px + env(safe-area-inset-bottom))}header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:16px}.eyebrow{margin:0 0 4px;font-size:12px;font-weight:800;letter-spacing:.045em;color:var(--hint)}h1{margin:0;font-size:29px;line-height:1.1}h2,h3{color:var(--text)}.pill{font-size:12px;background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:8px 10px;max-width:46%;text-align:center}.business,.status,.explanation,.workspace-view{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:14px;margin-bottom:14px}.business label{display:block;font-size:13px;font-weight:750;margin-bottom:8px}select{width:100%;min-height:46px;border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:0 12px}.status{font-size:14px;line-height:1.4}.status .secondary{margin-top:10px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{min-height:126px;text-align:left;border:1px solid var(--border);border-radius:16px;background:var(--surface);padding:15px;position:relative;touch-action:manipulation;cursor:pointer}.card:active,.customer-row:active,.sales-card:active,.action-card:active{transform:scale(.995)}.card:disabled{opacity:.7}.card h2{font-size:17px;margin:0 0 7px;padding-right:56px}.card p{font-size:13px;line-height:1.38;margin:0;color:var(--hint)}.card.planned{border-style:dashed}.card.restricted{opacity:.72}.badge{position:absolute;right:10px;top:10px;font-size:10px;font-weight:800;border-radius:999px;padding:4px 7px;background:var(--bg);color:var(--hint)}.badge.available{background:var(--button);color:var(--button-text)}.explanation h2{margin:14px 0 8px}.explanation p{line-height:1.5}.secondary,.action-card{min-height:44px;border:1px solid var(--border);border-radius:12px;padding:0 14px;background:var(--bg);font-weight:700}.view-toolbar{display:flex;justify-content:space-between;gap:10px}.home-heading h2{margin:14px 0 4px}.home-heading p{margin:0 0 12px;color:var(--hint)}.metrics,.money{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}.metric,.money-card,.attention-card{border:1px solid var(--border);border-radius:14px;padding:12px}.metric strong,.money-card strong{display:block;font-size:24px;margin-top:4px}.metric span,.money-card span,.muted{font-size:12px;color:var(--hint);line-height:1.4}.home-block{margin-top:18px}.home-block h3{margin:0 0 9px;font-size:16px}.primary-block{border:1px solid var(--button);border-radius:16px;padding:14px;background:color-mix(in srgb,var(--button) 7%,var(--surface))}.primary-block h3{font-size:19px}.attention-card{margin-bottom:8px}.action-card{display:block;width:100%;text-align:left;margin-bottom:8px;touch-action:manipulation}.action-card.primary-action{min-height:76px;background:var(--button);color:var(--button-text);border-color:var(--button);font-size:16px}.action-card small{display:block;font-weight:400;margin-top:4px;color:var(--hint);line-height:1.35}.action-card.primary-action small{color:var(--button-text);opacity:.84}.customer-search label{display:block;font-size:13px;font-weight:750;margin-bottom:8px}.customer-search>div{display:flex;gap:8px}.customer-search input{min-width:0;flex:1;min-height:44px;border:1px solid var(--border);border-radius:12px;background:var(--surface);color:var(--text);padding:0 12px}.customer-search button,.primary-cta{min-height:46px;border:0;border-radius:12px;background:var(--button);color:var(--button-text);padding:0 16px;font-weight:800}.primary-cta{display:block;width:100%;margin-top:14px}.customer-row,.sales-card{display:block;width:100%;text-align:left;border:1px solid var(--border);border-radius:14px;background:var(--surface);padding:13px;margin-bottom:8px;touch-action:manipulation}.customer-row strong{display:block}.customer-row small,.contact-card small,.timeline-card small,.schedule-card small,.sales-card small{display:block;color:var(--hint);margin-top:4px}.pager{display:flex;justify-content:space-between;gap:10px;margin-top:12px}.contact-card,.timeline-card,.schedule-card{border:1px solid var(--border);border-radius:14px;padding:12px;margin-bottom:8px}.schedule-card-top,.sales-card-top{display:flex;justify-content:space-between;gap:12px;align-items:center}.schedule-card p,.sales-card p{margin:8px 0 0;line-height:1.35}.schedule-status,.sales-stage{font-size:11px;font-weight:800;border-radius:999px;padding:4px 8px;background:var(--bg);white-space:nowrap}.schedule-status.booked{background:var(--button);color:var(--button-text)}.sales-card.overdue{border-color:var(--button)}.connection-card{border:1px solid var(--border);border-radius:14px;padding:13px;margin-bottom:8px}.connection-card-top{display:flex;justify-content:space-between;gap:12px;align-items:center}.connection-card p{margin:8px 0;line-height:1.4}.connection-state{font-size:11px;font-weight:800;border-radius:999px;padding:4px 8px;background:var(--bg);text-align:right}.connection-card.active .connection-state{background:var(--button);color:var(--button-text)}.connection-connect{width:100%;margin-top:6px}.settings-form label{display:block;font-size:13px;font-weight:750;margin:14px 0 7px}.settings-form input,.settings-form textarea{width:100%;border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:11px 12px}.settings-form input{min-height:46px}.settings-form textarea{resize:vertical;line-height:1.4}.calendar-form label{display:block;font-size:13px;font-weight:750;margin:14px 0 7px}.calendar-form input,.calendar-form select{width:100%;min-height:46px;border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:0 12px}.calendar-form-cancel{width:100%;margin-top:8px}.calendar-advanced{width:100%;margin-top:10px}.schedule-actions{display:flex;gap:8px;margin-top:10px}.schedule-actions button{flex:1;min-height:40px;border:1px solid var(--border);border-radius:10px;background:var(--bg);font-weight:750}.sales-detail-actions,.sales-result-actions,.sales-stage-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.sales-detail-actions button,.sales-stage-actions button,.sales-result-actions button{flex:1;min-width:120px;min-height:42px;border:1px solid var(--border);border-radius:10px;background:var(--bg);font-weight:750;padding:0 10px}.sales-stage-actions button.active{background:var(--button);color:var(--button-text);border-color:var(--button)}.sales-result-actions #sales-won{background:var(--button);color:var(--button-text);border-color:var(--button)}.sales-form label,.sales-result-label{display:block;font-size:13px;font-weight:750;margin:12px 0 7px}.sales-form input,.sales-form textarea,#sales-result-reason{width:100%;border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:11px 12px}.sales-form input{min-height:46px}.sales-form textarea,#sales-result-reason{resize:vertical;line-height:1.4}.sales-wide,.sales-advanced{width:100%;margin-top:10px}.sales-card.assigned-to-me{border-color:var(--button)}.sales-recent-heading{margin:18px 0 4px}.sales-recent-hint{margin:0 0 10px}.primary-nav{position:fixed;left:50%;bottom:0;transform:translateX(-50%);width:min(760px,100%);z-index:30;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));padding:8px 8px calc(8px + env(safe-area-inset-bottom));background:var(--surface);border-top:1px solid var(--border);box-shadow:0 -8px 28px rgba(0,0,0,.08)}.primary-nav button{min-width:0;min-height:52px;border:0;background:transparent;border-radius:12px;color:var(--hint);font-size:11px;font-weight:750;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px}.primary-nav button.active,.primary-nav button[aria-current=page]{color:var(--button);background:var(--bg)}.primary-icon{font-size:12px;line-height:1}.busy{opacity:.66;pointer-events:none}@supports not (color:color-mix(in srgb,black,white)){.primary-block{background:var(--surface)}}@media(max-width:520px){.grid,.metrics,.money{grid-template-columns:1fr}.shell{padding-left:12px;padding-right:12px}h1{font-size:27px}.pill{max-width:52%}.card{min-height:auto}.view-toolbar{position:sticky;top:env(safe-area-inset-top);z-index:2;background:var(--surface);padding:2px 0 8px}.primary-nav{border-radius:16px 16px 0 0}.primary-nav button{padding:4px 1px}}"""

_JS = r"""(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const statusText = document.getElementById('status-text');
  const statusAction = document.getElementById('status-action');
  const nav = document.getElementById('navigation');
  const primaryNav = document.getElementById('primary-nav');
  const select = document.getElementById('business-select');
  const role = document.getElementById('role');
  const explanation = document.getElementById('explanation');
  const title = document.getElementById('explanation-title');
  const summary = document.getElementById('explanation-summary');
  const when = document.getElementById('explanation-when');
  const reason = document.getElementById('explanation-reason');
  const close = document.getElementById('close-explanation');
  const home = document.getElementById('home-view');
  const customers = document.getElementById('customers-view');
  const calendar = document.getElementById('calendar-view');
  const sales = document.getElementById('sales-view');
  const connections = document.getElementById('connections-view');
  const settingsView = document.getElementById('settings-view');
  const homeBack = document.getElementById('home-back');
  const homeRefresh = document.getElementById('home-refresh');
  const homeMeta = document.getElementById('home-meta');
  const homeMetrics = document.getElementById('home-metrics');
  const homeMoney = document.getElementById('home-money');
  const homePrimaryBlock = document.getElementById('home-primary-block');
  const homePrimaryAction = document.getElementById('home-primary-action');
  const homeAttentionBlock = document.getElementById('home-attention-block');
  const homeActionsBlock = document.getElementById('home-actions-block');
  const homeAttention = document.getElementById('home-attention');
  const homeActions = document.getElementById('home-actions');
  const homeEmpty = document.getElementById('home-empty');
  const homeLimitations = document.getElementById('home-limitations');
  const initData = tg && typeof tg.initData === 'string' ? tg.initData : '';
  const roleNames = {owner:'Владелец',administrator:'Администратор',manager:'Менеджер',marketer:'Маркетолог',analyst:'Аналитик',content_manager:'Контент-менеджер',support:'Поддержка',customer:'Клиент'};
  const periodNames = {'7d':'7 дней','30d':'30 дней','today':'сегодня'};
  const nativeSections = new Set(['home','customers','calendar','sales','connections','settings']);
  let navigationItems = [];
  let currentView = 'home';
  let lastHomePayload = null;

  const text = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const screenStatus = (item) => {
    if (item.status === 'restricted') return 'restricted';
    if (item.status === 'available') return 'available';
    return 'planned';
  };
  const setPrimaryActive = (id) => {
    for (const button of primaryNav.querySelectorAll('button[data-primary]')) {
      const active = button.dataset.primary === id;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    }
  };
  const hideViews = () => {
    nav.hidden = true; home.hidden = true; customers.hidden = true; calendar.hidden = true; sales.hidden = true; connections.hidden = true; settingsView.hidden = true; explanation.hidden = true;
  };
  const syncBackButton = () => {
    if (!tg || !tg.BackButton) return;
    if (currentView === 'home') tg.BackButton.hide(); else tg.BackButton.show();
  };
  const showNavigation = () => { currentView = 'navigation'; hideViews(); nav.hidden = false; setPrimaryActive('more'); syncBackButton(); };
  const showHomeView = () => { currentView = 'home'; hideViews(); home.hidden = false; setPrimaryActive('home'); syncBackButton(); };
  const showHome = () => { if (lastHomePayload) renderHome(lastHomePayload); else loadHome().catch(homeFail); };
  const enterCustomers = () => { currentView = 'customers'; hideViews(); customers.hidden = false; setPrimaryActive('customers'); syncBackButton(); };
  const enterCalendar = () => { currentView = 'calendar'; hideViews(); calendar.hidden = false; setPrimaryActive('calendar'); syncBackButton(); };
  const enterSales = () => { currentView = 'sales'; hideViews(); sales.hidden = false; setPrimaryActive('sales'); syncBackButton(); };
  const enterConnections = () => { currentView = 'connections'; hideViews(); connections.hidden = false; setPrimaryActive('more'); syncBackButton(); };
  const enterSettings = () => { currentView = 'settings'; hideViews(); settingsView.hidden = false; setPrimaryActive('more'); syncBackButton(); };
  const setHomeBusy = (busy) => { home.classList.toggle('busy', Boolean(busy)); homeRefresh.disabled = Boolean(busy); home.setAttribute('aria-busy', busy ? 'true' : 'false'); };
  const closeToBot = () => { if (tg && typeof tg.close === 'function') tg.close(); else window.history.back(); };
  statusAction.addEventListener('click', closeToBot);
  close.addEventListener('click', showNavigation);
  homeBack.addEventListener('click', showNavigation);

  const showExplanation = (item) => {
    const state = screenStatus(item);
    currentView = 'explanation';
    text(title, item.title); text(summary, item.summary); text(when, `Когда пригодится: ${item.when_to_use}`);
    if (state === 'planned') text(reason, 'Этот раздел ещё подключается. Пользуйтесь доступными разделами ниже — данные бизнеса от этого не теряются.');
    else if (state === 'restricted') text(reason, item.reason || 'Для Вашей роли этот раздел недоступен. Если он нужен, попросите владельца бизнеса изменить доступ.');
    else text(reason, item.reason || 'Раздел доступен.');
    hideViews(); explanation.hidden = false; setPrimaryActive('more'); syncBackButton();
  };

  const appendMetric = (container, label, value, note) => {
    const card = document.createElement('div'); card.className = container === homeMoney ? 'money-card' : 'metric';
    const caption = document.createElement('span'); const number = document.createElement('strong'); const meaning = document.createElement('span');
    text(caption, label); text(number, value); text(meaning, note); card.append(caption, number, meaning); container.appendChild(card);
  };

  const appendHomeAction = (item, container, primary) => {
    const target = navigationItems.find((entry) => entry.id === item.section); const state = target ? screenStatus(target) : 'planned';
    const button = document.createElement('button'); button.type = 'button'; button.className = primary ? 'action-card primary-action' : 'action-card';
    const label = document.createElement('span'); const detail = document.createElement('small'); const cleanTitle = String(item.title || '').replace(/^Открыть:\s*/, '');
    text(label, state === 'available' ? cleanTitle : `Подробнее: ${cleanTitle}`);
    text(detail, state === 'available' ? item.reason : `${item.reason} Экран раздела пока подключается.`);
    button.append(label, detail); button.addEventListener('click', () => { if (target) showItem(target, button); }); container.appendChild(button);
  };

  const renderHome = (payload) => {
    lastHomePayload = payload;
    homeMetrics.replaceChildren(); homeMoney.replaceChildren(); homePrimaryAction.replaceChildren(); homeAttention.replaceChildren(); homeActions.replaceChildren();
    text(homeMeta, `${payload.business_name} · данные на сегодня`);
    const actions = payload.actions || [];
    if (actions.length) appendHomeAction(actions[0], homePrimaryAction, true);
    homePrimaryBlock.hidden = !actions.length;
    for (const item of payload.metrics || []) appendMetric(homeMetrics, item.title, item.value, item.meaning);
    for (const item of payload.money || []) appendMetric(homeMoney, `Подтверждённая выручка · ${periodNames[item.period] || item.period}`, item.display, item.meaning);
    for (const item of payload.attention || []) { const card = document.createElement('div'); card.className = 'attention-card'; text(card, item); homeAttention.appendChild(card); }
    homeAttentionBlock.hidden = !(payload.attention || []).length;
    for (const item of actions.slice(1)) appendHomeAction(item, homeActions, false);
    homeActionsBlock.hidden = actions.length <= 1;
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
    homeMetrics.replaceChildren(); homeMoney.replaceChildren(); homePrimaryAction.replaceChildren(); homeAttention.replaceChildren(); homeActions.replaceChildren(); homePrimaryBlock.hidden = true; homeAttentionBlock.hidden = true; homeActionsBlock.hidden = true;
    text(homeMeta, 'Не удалось обновить сводку'); text(homeEmpty, 'Сводка временно недоступна. Нажмите «Обновить» или откройте другой раздел.'); text(homeLimitations, 'Ваши данные и права доступа не менялись.'); showHomeView();
  };
  const closeAfterDelivery = () => {
    if (tg && typeof tg.close === 'function') { tg.close(); return true; }
    return false;
  };
  const notifySuccess = () => {
    if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.notificationOccurred === 'function') tg.HapticFeedback.notificationOccurred('success');
  };
  const openSection = async (item, button) => {
    const badge = button ? button.querySelector('.badge') : null;
    const priorBadge = badge ? badge.textContent : '';
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
    if (badge) text(badge, 'Открываю…');
    try {
      await post('/clientplatform/cockpit/section-open', select.value, {section:item.id});
      notifySuccess();
      if (closeAfterDelivery()) return;
      showExplanation({...item, reason:'Полные действия этого раздела открыты в чате с ботом. Вернитесь в Telegram.'});
    } catch (error) {
      const accessChanged = error && ['section_access_denied','business_access_denied'].includes(error.message);
      showExplanation({...item, reason:accessChanged ? 'Доступ к разделу изменился. Обновите кабинет.' : 'Не удалось открыть действия в Telegram. Обновите кабинет и попробуйте ещё раз.'});
    } finally {
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
      if (badge) text(badge, priorBadge);
    }
  };
  const openCanonicalSection = (section, button = null) => {
    const item = navigationItems.find((entry) => entry.id === section);
    if (!item) return;
    if (item.status !== 'available') { showExplanation(item); return; }
    void openSection(item, button);
  };
  const showItem = (item, button = null) => {
    if (item.status === 'available') {
      if (item.id === 'home') { loadHome().catch(homeFail); return; }
      if (item.id === 'customers' && window.ClientPlatformCustomers) { window.ClientPlatformCustomers.open(); return; }
      if (item.id === 'calendar' && window.ClientPlatformCalendar) { window.ClientPlatformCalendar.open(); return; }
      if (item.id === 'sales' && window.ClientPlatformSales) { window.ClientPlatformSales.open(); return; }
      if (item.id === 'connections' && window.ClientPlatformConnections) { window.ClientPlatformConnections.open(); return; }
      if (item.id === 'settings' && window.ClientPlatformSettings) { window.ClientPlatformSettings.open(); return; }
      void openSection(item, button); return;
    }
    showExplanation(item);
  };

  window.ClientPlatformCockpitNavigation = Object.freeze({showNavigation, showHome, enterCustomers, enterCalendar, enterSales, enterConnections, enterSettings, openCanonicalSection});

  const render = (payload) => {
    nav.replaceChildren(); select.replaceChildren(); navigationItems = payload.navigation || [];
    text(role, payload.role ? `Роль: ${roleNames[payload.role] || payload.role}` : 'Нужен бизнес'); statusAction.hidden = true;
    if (payload.onboarding_required) { primaryNav.hidden = true; text(statusText, 'У Вас пока нет подключённого бизнеса. Вернитесь в бот и нажмите «Подключить мой бизнес».'); select.disabled = true; statusAction.hidden = false; showNavigation(); return; }
    for (const business of payload.businesses || []) { const option = document.createElement('option'); option.value = business.id; text(option, `${business.name} · ${roleNames[business.role] || business.role}`); option.selected = Boolean(business.selected); select.appendChild(option); }
    select.disabled = false; primaryNav.hidden = false; text(statusText, `Открыт бизнес «${payload.business_name}». Ниже — главное и рабочие разделы.`);
    for (const item of navigationItems) {
      const state = screenStatus(item); const button = document.createElement('button'); button.type = 'button'; button.className = `card ${state}`;
      const heading = document.createElement('h2'); const copy = document.createElement('p'); const badge = document.createElement('span'); badge.className = `badge ${state}`;
      const nativeHere = nativeSections.has(item.id);
      text(heading, item.title); text(copy, item.summary); text(badge, state === 'available' ? (nativeHere ? 'В кабинете' : 'Открыть') : state === 'planned' ? 'Скоро' : 'Нет доступа'); button.append(heading, copy, badge); button.addEventListener('click', () => showItem(item, button)); nav.appendChild(button);
    }
    loadHome().catch(homeFail);
  };
  const load = async (businessId) => { primaryNav.hidden = true; select.disabled = true; text(statusText, 'Проверяем доступ и загружаем бизнес…'); return render(await post('/clientplatform/cockpit/context', businessId)); };
  select.addEventListener('change', () => load(select.value).catch(fail));

  for (const button of primaryNav.querySelectorAll('button[data-primary]')) {
    button.addEventListener('click', () => {
      const section = button.dataset.primary;
      if (section === 'more') { showNavigation(); return; }
      const item = navigationItems.find((entry) => entry.id === section);
      if (item) showItem(item, button);
    });
  }

  function fail(error) {
    nav.replaceChildren(); hideViews(); primaryNav.hidden = true; select.disabled = true; currentView = 'navigation'; syncBackButton(); text(role, 'Доступ не подтверждён'); statusAction.hidden = false;
    text(statusText, error && error.message === 'expired_init_data' ? 'Сессия Telegram устарела. Вернитесь в бот и откройте кабинет ещё раз.' : 'Не удалось подтвердить безопасный доступ. Вернитесь в бот и откройте кабинет ещё раз.');
  }
  if (tg && tg.BackButton && typeof tg.BackButton.onClick === 'function') tg.BackButton.onClick(() => {
    if (currentView === 'customers' && window.ClientPlatformCustomers) { window.ClientPlatformCustomers.back(); return; }
    if (currentView === 'calendar' && window.ClientPlatformCalendar) { window.ClientPlatformCalendar.back(); return; }
    if (currentView === 'sales' && window.ClientPlatformSales) { window.ClientPlatformSales.back(); return; }
    if (currentView === 'connections' && window.ClientPlatformConnections) { window.ClientPlatformConnections.back(); return; }
    if (currentView === 'settings' && window.ClientPlatformSettings) { window.ClientPlatformSettings.back(); return; }
    if (currentView === 'explanation' || currentView === 'navigation') { showHome(); }
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


async def cockpit_calendar_script(_request: web.Request) -> web.Response:
    return web.Response(
        text=_CALENDAR_SCRIPT.read_text(encoding="utf-8"),
        content_type="application/javascript",
        charset="utf-8",
        headers=_base_headers(),
    )


async def cockpit_sales_script(_request: web.Request) -> web.Response:
    return web.Response(
        text=_SALES_SCRIPT.read_text(encoding="utf-8"),
        content_type="application/javascript",
        charset="utf-8",
        headers=_base_headers(),
    )


async def cockpit_connections_script(_request: web.Request) -> web.Response:
    return web.Response(
        text=_CONNECTIONS_SCRIPT.read_text(encoding="utf-8"),
        content_type="application/javascript",
        charset="utf-8",
        headers=_base_headers(),
    )


async def cockpit_settings_script(_request: web.Request) -> web.Response:
    return web.Response(
        text=_SETTINGS_SCRIPT.read_text(encoding="utf-8"),
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


def _context_payload_with_routes(context: Any) -> dict[str, object]:
    payload = context.as_dict()
    business_id = str(payload.get("business_id") or "").strip()
    navigation = payload.get("navigation")
    if not business_id or not isinstance(navigation, (list, tuple)):
        return payload
    for item in navigation:
        if not isinstance(item, dict):
            continue
        section = str(item.get("id") or "").strip().lower()
        if item.get("status") != "available" or section in {"home", "customers", "calendar", "sales", "connections", "settings"}:
            continue
        try:
            start_payload = build_cockpit_section_start_payload(
                business_id=business_id,
                section=section,
            )
        except ValueError:
            continue
        route_url = _telegram_action_url(start_payload)
        if route_url is not None:
            item["route_url"] = route_url
    return payload


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
    return web.json_response(
        {"ok": True, **_context_payload_with_routes(context)},
        headers=_base_headers(),
    )


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


async def cockpit_calendar(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    try:
        calendar = await asyncio.to_thread(
            resolve_cockpit_calendar,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            limit=payload.get("limit", 30),
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "calendar_access_denied")
    except ValueError:
        return _error(400, "invalid_calendar_request")
    except OSError:
        return _error(503, "calendar_unavailable")
    except RuntimeError:
        return _error(503, "calendar_unavailable")
    return web.json_response({"ok": True, **calendar.as_dict()}, headers=_base_headers())


async def cockpit_calendar_management(request: web.Request) -> web.Response:
    scope = await _verified_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business = scope
    try:
        snapshot = await asyncio.to_thread(
            resolve_cockpit_calendar_management,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "calendar_manage_denied")
    except ValueError:
        return _error(400, "invalid_calendar_request")
    except OSError:
        return _error(503, "calendar_unavailable")
    except RuntimeError:
        return _error(503, "calendar_unavailable")
    return web.json_response({"ok": True, **snapshot.as_dict()}, headers=_base_headers())


async def cockpit_calendar_create(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    offering_id = payload.get("offering_id")
    local_start = payload.get("local_start")
    duration = payload.get("duration_minutes")
    if (
        not isinstance(offering_id, str)
        or not isinstance(local_start, str)
        or isinstance(duration, bool)
        or not isinstance(duration, int)
    ):
        return _error(400, "invalid_calendar_change")
    try:
        slot = await asyncio.to_thread(
            create_cockpit_calendar_slot,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            offering_id=offering_id,
            local_start=local_start,
            duration_minutes=duration,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "calendar_manage_denied")
    except BookingNotFound:
        return _error(404, "calendar_slot_not_found")
    except BookingInvariantViolation:
        return _error(409, "calendar_change_rejected")
    except ActivityInvariantViolation:
        return _error(409, "calendar_change_rejected")
    except ValueError:
        return _error(400, "invalid_calendar_change")
    except OSError:
        return _error(503, "calendar_unavailable")
    except RuntimeError:
        return _error(503, "calendar_unavailable")
    return web.json_response({"ok": True, "slot_id": slot.slot.id}, headers=_base_headers())


async def cockpit_calendar_replace(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    slot_id = payload.get("slot_id")
    local_start = payload.get("local_start")
    duration = payload.get("duration_minutes")
    if (
        not isinstance(slot_id, str)
        or not isinstance(local_start, str)
        or isinstance(duration, bool)
        or not isinstance(duration, int)
    ):
        return _error(400, "invalid_calendar_change")
    try:
        slot = await asyncio.to_thread(
            replace_cockpit_calendar_slot,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            slot_id=slot_id,
            local_start=local_start,
            duration_minutes=duration,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "calendar_manage_denied")
    except BookingNotFound:
        return _error(404, "calendar_slot_not_found")
    except BookingInvariantViolation:
        return _error(409, "calendar_change_rejected")
    except ActivityInvariantViolation:
        return _error(409, "calendar_change_rejected")
    except ValueError:
        return _error(400, "invalid_calendar_change")
    except OSError:
        return _error(503, "calendar_unavailable")
    except RuntimeError:
        return _error(503, "calendar_unavailable")
    return web.json_response({"ok": True, "slot_id": slot.slot.id}, headers=_base_headers())


async def cockpit_calendar_cancel(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    slot_id = payload.get("slot_id")
    if not isinstance(slot_id, str):
        return _error(400, "invalid_calendar_change")
    try:
        slot = await asyncio.to_thread(
            cancel_cockpit_calendar_slot,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            slot_id=slot_id,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "calendar_manage_denied")
    except BookingNotFound:
        return _error(404, "calendar_slot_not_found")
    except BookingInvariantViolation:
        return _error(409, "calendar_change_rejected")
    except ValueError:
        return _error(400, "invalid_calendar_change")
    except OSError:
        return _error(503, "calendar_unavailable")
    except RuntimeError:
        return _error(503, "calendar_unavailable")
    return web.json_response({"ok": True, "slot_id": slot.slot.id}, headers=_base_headers())


async def cockpit_sales(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    try:
        sales_snapshot = await asyncio.to_thread(
            resolve_cockpit_sales,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            limit=payload.get("limit", 20),
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "sales_access_denied")
    except ValueError:
        return _error(400, "invalid_sales_request")
    except OSError:
        return _error(503, "sales_unavailable")
    except RuntimeError:
        return _error(503, "sales_unavailable")
    return web.json_response({"ok": True, **sales_snapshot.as_dict()}, headers=_base_headers())



def _sales_lead_id(payload: dict[str, Any]) -> str | None:
    lead_id = payload.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id.strip() or len(lead_id) > 80:
        return None
    return lead_id.strip()


async def _sales_management_call(
    operation: Any, **kwargs: Any
) -> tuple[CockpitSalesLeadManagement | None, web.Response | None]:
    try:
        item = await asyncio.to_thread(operation, **kwargs)
    except TenantAccessDenied:
        return None, _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return None, _error(403, "sales_manage_denied")
    except SalesLeadNotFound:
        return None, _error(404, "sales_lead_not_found")
    except SalesInvariantViolation:
        return None, _error(409, "sales_change_rejected")
    except ValueError:
        return None, _error(400, "invalid_sales_change")
    except OSError:
        return None, _error(503, "sales_unavailable")
    except RuntimeError:
        return None, _error(503, "sales_unavailable")
    return item, None


async def cockpit_sales_manage(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    lead_id = _sales_lead_id(payload)
    if lead_id is None:
        return _error(400, "invalid_sales_change")
    item, error = await _sales_management_call(
        resolve_cockpit_sales_lead,
        telegram_user_id=user_id,
        requested_business_id=requested_business,
        lead_id=lead_id,
    )
    if error is not None:
        return error
    if item is None:
        return _error(503, "sales_unavailable")
    return web.json_response({"ok": True, **item.as_dict()}, headers=_base_headers())


async def cockpit_sales_assignment(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    lead_id = _sales_lead_id(payload)
    action = payload.get("action")
    if lead_id is None or action not in {"assign", "unassign"}:
        return _error(400, "invalid_sales_change")
    operation = assign_cockpit_sales_lead if action == "assign" else unassign_cockpit_sales_lead
    item, error = await _sales_management_call(
        operation,
        telegram_user_id=user_id,
        requested_business_id=requested_business,
        lead_id=lead_id,
    )
    if error is not None:
        return error
    if item is None:
        return _error(503, "sales_unavailable")
    return web.json_response({"ok": True, **item.as_dict()}, headers=_base_headers())


async def cockpit_sales_stage(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    lead_id = _sales_lead_id(payload)
    stage = payload.get("stage")
    reason = payload.get("reason")
    if (
        lead_id is None
        or not isinstance(stage, str)
        or not stage.strip()
        or len(stage) > 40
        or (reason is not None and (not isinstance(reason, str) or len(reason) > 500))
    ):
        return _error(400, "invalid_sales_change")
    item, error = await _sales_management_call(
        set_cockpit_sales_stage,
        telegram_user_id=user_id,
        requested_business_id=requested_business,
        lead_id=lead_id,
        stage=stage.strip(),
        reason=reason,
    )
    if error is not None:
        return error
    if item is None:
        return _error(503, "sales_unavailable")
    return web.json_response({"ok": True, **item.as_dict()}, headers=_base_headers())


async def cockpit_sales_reopen(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    lead_id = _sales_lead_id(payload)
    if lead_id is None:
        return _error(400, "invalid_sales_change")
    item, error = await _sales_management_call(
        reopen_cockpit_sales_lead,
        telegram_user_id=user_id,
        requested_business_id=requested_business,
        lead_id=lead_id,
    )
    if error is not None:
        return error
    if item is None:
        return _error(503, "sales_unavailable")
    return web.json_response({"ok": True, **item.as_dict()}, headers=_base_headers())


async def cockpit_sales_next_action(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    lead_id = _sales_lead_id(payload)
    next_action = payload.get("next_action")
    due_local = payload.get("due_local")
    if (
        lead_id is None
        or (next_action is not None and (not isinstance(next_action, str) or len(next_action) > 500))
        or (due_local is not None and (not isinstance(due_local, str) or len(due_local) > 40))
    ):
        return _error(400, "invalid_sales_change")
    item, error = await _sales_management_call(
        set_cockpit_sales_next_action,
        telegram_user_id=user_id,
        requested_business_id=requested_business,
        lead_id=lead_id,
        next_action=next_action,
        due_local=due_local,
    )
    if error is not None:
        return error
    if item is None:
        return _error(503, "sales_unavailable")
    return web.json_response({"ok": True, **item.as_dict()}, headers=_base_headers())


async def cockpit_sales_note(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    lead_id = _sales_lead_id(payload)
    note = payload.get("note")
    interaction_key = payload.get("interaction_key")
    if (
        lead_id is None
        or not isinstance(note, str)
        or not note.strip()
        or len(note) > 4000
        or not isinstance(interaction_key, str)
        or not interaction_key.strip()
        or len(interaction_key) > 120
    ):
        return _error(400, "invalid_sales_change")
    item, error = await _sales_management_call(
        add_cockpit_sales_note,
        telegram_user_id=user_id,
        requested_business_id=requested_business,
        lead_id=lead_id,
        note=note,
        interaction_key=interaction_key,
    )
    if error is not None:
        return error
    if item is None:
        return _error(503, "sales_unavailable")
    return web.json_response({"ok": True, **item.as_dict()}, headers=_base_headers())


async def cockpit_connections(request: web.Request) -> web.Response:
    scope = await _verified_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business = scope
    try:
        snapshot = await asyncio.to_thread(
            resolve_cockpit_connections,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "connections_access_denied")
    except OSError:
        return _error(503, "connections_unavailable")
    except RuntimeError:
        return _error(503, "connections_unavailable")
    return web.json_response({"ok": True, **snapshot.as_dict()}, headers=_base_headers())


async def cockpit_connection_setup(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    platform = payload.get("platform")
    if not isinstance(platform, str):
        return _error(400, "invalid_connection_request")
    public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not public_base.startswith("https://"):
        return _error(503, "connections_unavailable")
    try:
        issued = await asyncio.to_thread(
            issue_cockpit_messenger_setup,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            platform=platform,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "connections_access_denied")
    except ValueError:
        return _error(400, "invalid_connection_request")
    except RuntimeError:
        return _error(409, "connection_not_connectable")
    return web.json_response(
        {
            "ok": True,
            "platform": issued.platform.value,
            "setup_url": f"{public_base}/clientplatform/connect/{issued.token}",
            "expires_at": issued.expires_at,
        },
        headers=_base_headers(),
    )


async def cockpit_settings(request: web.Request) -> web.Response:
    scope = await _verified_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business = scope
    try:
        snapshot = await asyncio.to_thread(
            resolve_cockpit_settings,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "settings_access_denied")
    except OSError:
        return _error(503, "settings_unavailable")
    except RuntimeError:
        return _error(503, "settings_unavailable")
    return web.json_response({"ok": True, **snapshot.as_dict()}, headers=_base_headers())


async def cockpit_settings_update(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    user_id, requested_business, payload = scope
    values = (payload.get("business_name"), payload.get("activity_description"), payload.get("timezone_name"))
    if not all(isinstance(value, str) for value in values):
        return _error(400, "invalid_settings_request")
    try:
        snapshot = await asyncio.to_thread(
            update_cockpit_settings,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
            business_name=values[0],
            activity_description=values[1],
            timezone_name=values[2],
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "settings_access_denied")
    except ValueError:
        return _error(400, "invalid_settings_request")
    except OSError:
        return _error(503, "settings_unavailable")
    except RuntimeError:
        return _error(503, "settings_unavailable")
    return web.json_response({"ok": True, **snapshot.as_dict()}, headers=_base_headers())


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
    response_payload = detail.as_dict()
    if detail.next_action is not None:
        try:
            action_route = await asyncio.to_thread(
                resolve_cockpit_customer_action_route,
                telegram_user_id=user_id,
                requested_business_id=requested_business,
                customer_id=detail.customer_id,
                expected_action_key=detail.next_action.action_key,
            )
        except (
            CockpitCustomerActionUnavailable,
            TenantAccessDenied,
            TenantPermissionDenied,
            ValueError,
        ):
            action_route = None
        if action_route is not None:
            route_url = _telegram_action_url(action_route.start_payload)
            next_action = response_payload.get("next_action")
            if route_url is not None and isinstance(next_action, dict):
                next_action["route_url"] = route_url
    return web.json_response({"ok": True, **response_payload}, headers=_base_headers())


async def _send_section_to_bot(
    *,
    sender: Any,
    bot: Any,
    telegram_user_id: int,
    canonical_user_id: int,
    business_id: str,
    section: str,
) -> None:
    await sender(
        _BotMessageTarget(bot, chat_id=telegram_user_id),
        user_id=canonical_user_id,
        business_id=business_id,
        section=section,
    )


async def _send_action_to_bot(
    *,
    sender: Any,
    bot: Any,
    telegram_user_id: int,
    canonical_user_id: int,
    route: Any,
) -> None:
    await sender(
        _BotMessageTarget(bot, chat_id=telegram_user_id),
        user_id=canonical_user_id,
        route=route,
    )


async def cockpit_section_open(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    telegram_user_id, requested_business, payload = scope
    section = payload.get("section")
    if not isinstance(section, str) or not section.strip() or len(section) > 40:
        return _error(400, "invalid_section")
    bot = request.app.get(_COCKPIT_BOT_APP_KEY)
    sender = request.app.get(_COCKPIT_SECTION_SENDER_APP_KEY)
    if bot is None or sender is None:
        return _error(503, "telegram_delivery_unavailable")
    normalized = section.strip().lower()
    try:
        context = await asyncio.to_thread(
            resolve_cockpit_context,
            telegram_user_id=telegram_user_id,
            requested_business_id=requested_business,
        )
        if context.onboarding_required or context.business_id is None:
            raise TenantAccessDenied("active business membership was not found")
        item = next((entry for entry in context.navigation if entry.id == normalized), None)
        if item is None:
            raise ValueError("unsupported cockpit section")
        if item.status != "available":
            raise TenantPermissionDenied("cockpit section is not available for this role")
        await _send_section_to_bot(
            sender=sender,
            bot=bot,
            telegram_user_id=telegram_user_id,
            canonical_user_id=int(context.user_id),
            business_id=context.business_id,
            section=normalized,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except TenantPermissionDenied:
        return _error(403, "section_access_denied")
    except ValueError:
        return _error(400, "invalid_section")
    except (TelegramAPIError, OSError):
        return _error(503, "telegram_delivery_failed")
    return web.json_response(
        {"ok": True, "delivery": "telegram_chat", "section": normalized},
        headers=_base_headers(),
    )


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


async def cockpit_customer_action_open(request: web.Request) -> web.Response:
    scope = await _verified_payload_scope(request)
    if isinstance(scope, web.Response):
        return scope
    telegram_user_id, requested_business, payload = scope
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
    bot = request.app.get(_COCKPIT_BOT_APP_KEY)
    sender = request.app.get(_COCKPIT_ACTION_SENDER_APP_KEY)
    if bot is None or sender is None:
        return _error(503, "telegram_delivery_unavailable")
    try:
        route = await asyncio.to_thread(
            resolve_cockpit_customer_action_route,
            telegram_user_id=telegram_user_id,
            requested_business_id=requested_business,
            customer_id=customer_id,
            expected_action_key=expected_action_key,
        )
        context = await asyncio.to_thread(
            resolve_cockpit_context,
            telegram_user_id=telegram_user_id,
            requested_business_id=route.business_id,
        )
        if context.onboarding_required or context.business_id != route.business_id:
            raise TenantAccessDenied("customer action business is no longer active")
        parsed = parse_cockpit_action_start_payload(route.start_payload)
        if parsed is None or parsed.section is not None:
            raise ValueError("invalid customer action route")
        await _send_action_to_bot(
            sender=sender,
            bot=bot,
            telegram_user_id=telegram_user_id,
            canonical_user_id=int(context.user_id),
            route=parsed,
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
    except (TelegramAPIError, OSError):
        return _error(503, "telegram_delivery_failed")
    return web.json_response(
        {"ok": True, "delivery": "telegram_chat", "schema_version": route.schema_version},
        headers=_base_headers(),
    )


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


def register_cockpit_routes(
    app: web.Application,
    *,
    bot: Any = None,
    section_sender: Any = None,
    action_sender: Any = None,
) -> None:
    app.router.add_get(_COCKPIT_PREFIX, cockpit_shell)
    app.router.add_get(f"{_COCKPIT_PREFIX}/app.js", cockpit_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/styles.css", cockpit_styles)
    app.router.add_get(f"{_COCKPIT_PREFIX}/customers.js", cockpit_customers_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/calendar.js", cockpit_calendar_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/sales.js", cockpit_sales_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/connections.js", cockpit_connections_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/settings.js", cockpit_settings_script)
    app.router.add_post(f"{_COCKPIT_PREFIX}/context", cockpit_context)
    app.router.add_post(f"{_COCKPIT_PREFIX}/home", cockpit_home)
    app.router.add_post(f"{_COCKPIT_PREFIX}/calendar", cockpit_calendar)
    app.router.add_post(f"{_COCKPIT_PREFIX}/calendar/manage", cockpit_calendar_management)
    app.router.add_post(f"{_COCKPIT_PREFIX}/calendar/create", cockpit_calendar_create)
    app.router.add_post(f"{_COCKPIT_PREFIX}/calendar/replace", cockpit_calendar_replace)
    app.router.add_post(f"{_COCKPIT_PREFIX}/calendar/cancel", cockpit_calendar_cancel)
    app.router.add_post(f"{_COCKPIT_PREFIX}/sales", cockpit_sales)
    app.router.add_post(f"{_COCKPIT_PREFIX}/sales/manage", cockpit_sales_manage)
    app.router.add_post(f"{_COCKPIT_PREFIX}/sales/assignment", cockpit_sales_assignment)
    app.router.add_post(f"{_COCKPIT_PREFIX}/sales/stage", cockpit_sales_stage)
    app.router.add_post(f"{_COCKPIT_PREFIX}/sales/reopen", cockpit_sales_reopen)
    app.router.add_post(f"{_COCKPIT_PREFIX}/sales/next-action", cockpit_sales_next_action)
    app.router.add_post(f"{_COCKPIT_PREFIX}/sales/note", cockpit_sales_note)
    app.router.add_post(f"{_COCKPIT_PREFIX}/connections", cockpit_connections)
    app.router.add_post(f"{_COCKPIT_PREFIX}/connections/setup", cockpit_connection_setup)
    app.router.add_post(f"{_COCKPIT_PREFIX}/settings", cockpit_settings)
    app.router.add_post(f"{_COCKPIT_PREFIX}/settings/update", cockpit_settings_update)
    app.router.add_post(f"{_COCKPIT_PREFIX}/section-open", cockpit_section_open)
    app.router.add_post(f"{_COCKPIT_PREFIX}/section-route", cockpit_section_route)
    app.router.add_post(f"{_COCKPIT_PREFIX}/customers", cockpit_customers)
    app.router.add_post(
        f"{_COCKPIT_PREFIX}/customers/detail", cockpit_customer_detail
    )
    app.router.add_post(
        f"{_COCKPIT_PREFIX}/customers/action-open", cockpit_customer_action_open
    )
    app.router.add_post(
        f"{_COCKPIT_PREFIX}/customers/action-route", cockpit_customer_action_route
    )
    app[_COCKPIT_BOT_APP_KEY] = bot
    app[_COCKPIT_SECTION_SENDER_APP_KEY] = section_sender
    app[_COCKPIT_ACTION_SENDER_APP_KEY] = action_sender
    app[_COCKPIT_APP_KEY] = True


__all__ = [
    "cockpit_connection_setup",
    "cockpit_connections",
    "cockpit_connections_script",
    "cockpit_context",
    "cockpit_calendar",
    "cockpit_calendar_cancel",
    "cockpit_calendar_create",
    "cockpit_calendar_management",
    "cockpit_calendar_replace",
    "cockpit_sales",
    "cockpit_sales_assignment",
    "cockpit_sales_manage",
    "cockpit_sales_next_action",
    "cockpit_sales_note",
    "cockpit_sales_reopen",
    "cockpit_sales_stage",
    "cockpit_settings",
    "cockpit_settings_script",
    "cockpit_settings_update",
    "cockpit_customer_action_open",
    "cockpit_customer_action_route",
    "cockpit_customer_detail",
    "cockpit_customers",
    "cockpit_customers_script",
    "cockpit_home",
    "cockpit_http_enabled",
    "cockpit_script",
    "cockpit_section_open",
    "cockpit_section_route",
    "cockpit_shell",
    "cockpit_styles",
    "register_cockpit_routes",
]
