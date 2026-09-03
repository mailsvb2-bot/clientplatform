from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web

from clientplatform.application.cockpit import resolve_cockpit_actor, resolve_cockpit_context
from clientplatform.application.cockpit_home import get_cockpit_home
from clientplatform.domain.tenancy import TenantAccessDenied
from clientplatform.runtime.telegram_webapp_auth import (
    TelegramWebAppAuthError,
    verify_telegram_webapp_init_data,
)
from config.settings import settings
from core.runtime_env import env_int

_COCKPIT_PREFIX = "/clientplatform/cockpit"

_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ClientPlatform · Кабинет</title>
<link rel="stylesheet" href="/clientplatform/cockpit/styles.css">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script defer src="/clientplatform/cockpit/app.js"></script>
</head><body>
<main class="shell">
<header><div><p class="eyebrow">ClientPlatform</p><h1>Ваш бизнес</h1></div><span id="role" class="pill">Проверяем доступ…</span></header>
<section class="business"><label for="business-select">Какой бизнес открыт</label><select id="business-select" disabled><option>Загрузка…</option></select></section>
<section id="status" class="status">Проверяем безопасный вход через Telegram…</section>
<section id="home" class="home" hidden aria-live="polite"><div class="home-head"><div><p class="eyebrow">Сегодня</p><h2>Что происходит и что делать дальше</h2></div><button id="refresh-home" class="secondary" type="button">Обновить</button></div><div id="home-next" class="next"></div><h3>Что произошло</h3><div id="home-today" class="facts"></div><h3>Деньги</h3><div id="home-money" class="facts"></div><h3>Требует внимания</h3><div id="home-attention" class="facts"></div><p id="home-note" class="note"></p></section>
<h2 class="section-title">Все возможности</h2><section id="navigation" class="grid" aria-live="polite"></section>
<section id="explanation" class="explanation" hidden><button id="close-explanation" type="button">Назад</button><h2 id="explanation-title"></h2><p id="explanation-summary"></p><p id="explanation-when"></p><p id="explanation-reason"></p></section>
</main></body></html>"""

_CSS = """*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f6f8;color:#17202a}.shell{max-width:760px;margin:0 auto;padding:20px 16px 40px}header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}.eyebrow{margin:0 0 4px;font-size:13px;font-weight:700;letter-spacing:.04em}h1{margin:0;font-size:30px;line-height:1.1}.pill{font-size:12px;background:#fff;border:1px solid #d9dee5;border-radius:999px;padding:8px 10px}.business,.status,.explanation{background:#fff;border:1px solid #e1e5ea;border-radius:16px;padding:14px;margin-bottom:14px}.business label{display:block;font-size:13px;font-weight:700;margin-bottom:8px}select{width:100%;min-height:44px;border:1px solid #cfd6de;border-radius:12px;background:#fff;padding:0 12px;font:inherit}.status{font-size:14px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{min-height:132px;text-align:left;border:1px solid #e1e5ea;border-radius:16px;background:#fff;padding:15px;font:inherit}.card h2{font-size:17px;margin:0 0 7px}.card p{font-size:13px;line-height:1.35;margin:0;color:#52606d}.card.restricted,.card.planned{opacity:.62}.home{background:#fff;border:1px solid #e1e5ea;border-radius:18px;padding:16px;margin-bottom:20px}.home-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.home h2{font-size:21px;margin:0}.home h3{font-size:15px;margin:18px 0 8px}.secondary{min-height:40px;border:1px solid #cfd6de;border-radius:12px;background:#fff;padding:0 12px;font:inherit}.facts{display:grid;gap:8px}.fact{border-top:1px solid #edf0f3;padding-top:8px;font-size:14px;line-height:1.4}.fact strong{display:block}.next{margin-top:14px}.primary-action{width:100%;min-height:48px;border:0;border-radius:14px;padding:10px 14px;font:inherit;font-weight:800;text-align:left}.primary-action span{display:block;font-size:12px;font-weight:500;margin-top:4px}.note{font-size:12px;color:#64717d}.section-title{font-size:18px;margin:0 0 10px}.explanation h2{margin:12px 0 8px}.explanation p{line-height:1.45}.explanation button{min-height:42px;border:0;border-radius:12px;padding:0 14px;font:inherit;font-weight:700}@media(max-width:520px){.grid{grid-template-columns:1fr}.shell{padding:14px 12px 28px}h1{font-size:27px}}"""

_JS = r"""(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const status = document.getElementById('status');
  const nav = document.getElementById('navigation');
  const select = document.getElementById('business-select');
  const role = document.getElementById('role');
  const explanation = document.getElementById('explanation');
  const title = document.getElementById('explanation-title');
  const summary = document.getElementById('explanation-summary');
  const when = document.getElementById('explanation-when');
  const reason = document.getElementById('explanation-reason');
  const close = document.getElementById('close-explanation');
  const home = document.getElementById('home');
  const homeNext = document.getElementById('home-next');
  const homeToday = document.getElementById('home-today');
  const homeMoney = document.getElementById('home-money');
  const homeAttention = document.getElementById('home-attention');
  const homeNote = document.getElementById('home-note');
  const refreshHome = document.getElementById('refresh-home');
  const initData = tg && typeof tg.initData === 'string' ? tg.initData : '';

  let lastNavigation = [];
  const text = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const addFact = (container, heading, detail) => {
    const row = document.createElement('div');
    row.className = 'fact';
    const strong = document.createElement('strong');
    text(strong, heading);
    row.appendChild(strong);
    if (detail) {
      const copy = document.createElement('span');
      text(copy, detail);
      row.appendChild(copy);
    }
    container.appendChild(row);
  };
  const showItem = (item) => {
    text(title, item.title);
    text(summary, item.summary);
    text(when, `Если Вам нужно: ${item.when_to_use}`);
    text(reason, item.reason || 'Доступ подтверждён сервером. Действия внутри раздела всё равно проверяют свои права отдельно.');
    nav.hidden = true;
    explanation.hidden = false;
  };
  close.addEventListener('click', () => { explanation.hidden = true; nav.hidden = false; });

  const openRoute = (route) => {
    const item = lastNavigation.find((candidate) => candidate.id === route && candidate.status === 'available');
    if (item) showItem(item);
  };
  const renderHome = (payload) => {
    homeNext.replaceChildren(); homeToday.replaceChildren(); homeMoney.replaceChildren(); homeAttention.replaceChildren();
    for (const item of payload.today || []) addFact(homeToday, `${item.title}: ${item.value}`, item.detail);
    if (!(payload.today || []).length) addFact(homeToday, 'Нет доступных фактов дня', 'Для этой роли или текущего состояния бизнеса подробности не показываются.');
    for (const item of payload.money || []) addFact(homeMoney, `${item.title}: ${item.display_amount}`, item.detail);
    if (!(payload.money || []).length) addFact(homeMoney, 'Деньги не показаны', 'Либо подтверждённой выручки пока нет, либо эта роль не имеет доступа к денежным фактам.');
    for (const item of payload.attention || []) addFact(homeAttention, item, 'Откройте соответствующий раздел, чтобы увидеть детали.');
    if (!(payload.attention || []).length) addFact(homeAttention, 'Срочных сигналов нет', 'Доступные источники не требуют немедленного действия.');
    if (payload.next_action) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'primary-action'; text(button, payload.next_action.title);
      const why = document.createElement('span'); text(why, payload.next_action.reason); button.appendChild(why);
      button.addEventListener('click', () => openRoute(payload.next_action.route)); homeNext.appendChild(button);
    }
    const limited = (payload.sources || []).filter((item) => item.status === 'unavailable' || item.status === 'restricted');
    text(homeNote, limited.length ? limited.slice(0, 2).map((item) => item.message).join(' ') : `День считается по часовому поясу ${payload.timezone_name}.`);
    home.hidden = false;
  };

  const render = (payload) => {
    nav.replaceChildren();
    select.replaceChildren();
    lastNavigation = payload.navigation || [];
    text(role, payload.role ? `Роль: ${payload.role}` : 'Нужен бизнес');
    if (payload.onboarding_required) {
      home.hidden = true;
      text(status, 'У Вас пока нет подключённого бизнеса. Вернитесь в бот и выберите «Подключить мой бизнес».');
      select.disabled = true;
      return;
    }
    for (const business of payload.businesses || []) {
      const option = document.createElement('option');
      option.value = business.id;
      text(option, `${business.name} · ${business.role}`);
      option.selected = Boolean(business.selected);
      select.appendChild(option);
    }
    select.disabled = false;
    text(status, `Открыт бизнес «${payload.business_name}». Выберите раздел — под каждой кнопкой есть объяснение.`);
    for (const item of payload.navigation || []) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `card ${item.status}`;
      const heading = document.createElement('h2');
      const copy = document.createElement('p');
      text(heading, item.title);
      text(copy, item.summary);
      button.append(heading, copy);
      button.addEventListener('click', () => {
        if (item.id === 'home' && item.status === 'available') home.scrollIntoView({behavior: 'smooth'});
        else showItem(item);
      });
      nav.appendChild(button);
    }
  };

  const loadHome = async (businessId) => {
    const body = {init_data: initData};
    if (businessId) body.business_id = businessId;
    const response = await fetch('/clientplatform/cockpit/home', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({error: 'invalid_response'}));
    if (!response.ok) throw new Error(payload.error || 'home_unavailable');
    renderHome(payload);
  };
  const homeFail = () => {
    homeNext.replaceChildren(); homeToday.replaceChildren(); homeMoney.replaceChildren(); homeAttention.replaceChildren();
    addFact(homeToday, 'Сводка временно недоступна', 'Используйте разделы ниже — их права и данные проверяются отдельно.');
    text(homeNote, 'Обновите Home позже. Недоступный источник не показан как нулевой результат.');
    home.hidden = false;
  };

  const load = async (businessId) => {
    if (!initData) {
      text(status, 'Откройте этот кабинет из Telegram — браузер сам по себе не подтверждает Вашу личность.');
      role.textContent = 'Нет Telegram-подтверждения';
      return;
    }
    select.disabled = true;
    const body = {init_data: initData};
    if (businessId) body.business_id = businessId;
    const response = await fetch('/clientplatform/cockpit/context', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({error: 'invalid_response'}));
    if (!response.ok) throw new Error(payload.error || 'access_denied');
    render(payload);
    if (!payload.onboarding_required && payload.business_id) await loadHome(payload.business_id).catch(homeFail);
  };

  select.addEventListener('change', () => load(select.value).catch(fail));
  refreshHome.addEventListener('click', () => loadHome(select.value).catch(homeFail));
  const fail = (error) => {
    nav.replaceChildren();
    select.disabled = true;
    text(role, 'Доступ не подтверждён');
    text(status, error && error.message === 'expired_init_data'
      ? 'Сессия Telegram устарела. Закройте кабинет и откройте его снова из бота.'
      : 'Не удалось подтвердить безопасный доступ. Закройте кабинет и откройте его снова из бота.');
  };
  if (tg) { tg.ready(); tg.expand(); }
  load(null).catch(fail);
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


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"ok": False, "error": code}, status=status, headers=_base_headers())


async def _verified_cockpit_request(request: web.Request) -> tuple[int, str | None] | web.Response:
    try:
        raw = await request.read()
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _error(400, "invalid_json")
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
    return principal.user_id, requested_business


async def cockpit_context(request: web.Request) -> web.Response:
    verified = await _verified_cockpit_request(request)
    if isinstance(verified, web.Response):
        return verified
    user_id, requested_business = verified
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
    verified = await _verified_cockpit_request(request)
    if isinstance(verified, web.Response):
        return verified
    user_id, requested_business = verified
    try:
        actor = await asyncio.to_thread(
            resolve_cockpit_actor,
            telegram_user_id=user_id,
            requested_business_id=requested_business,
        )
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except ValueError:
        return _error(400, "invalid_business_id")

    try:
        projection = await asyncio.to_thread(get_cockpit_home, actor=actor)
    except TenantAccessDenied:
        return _error(403, "business_access_denied")
    except (OSError, RuntimeError, ValueError):
        return _error(503, "home_unavailable")
    return web.json_response({"ok": True, **projection.as_dict()}, headers=_base_headers())


def register_cockpit_routes(app: web.Application) -> None:
    app.router.add_get(_COCKPIT_PREFIX, cockpit_shell)
    app.router.add_get(f"{_COCKPIT_PREFIX}/app.js", cockpit_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/styles.css", cockpit_styles)
    app.router.add_post(f"{_COCKPIT_PREFIX}/context", cockpit_context)
    app.router.add_post(f"{_COCKPIT_PREFIX}/home", cockpit_home)
    app["clientplatform_cockpit"] = True


__all__ = [
    "cockpit_context",
    "cockpit_home",
    "cockpit_http_enabled",
    "cockpit_script",
    "cockpit_shell",
    "cockpit_styles",
    "register_cockpit_routes",
]
