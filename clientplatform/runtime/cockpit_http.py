from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web

from clientplatform.application.cockpit import resolve_cockpit_context
from clientplatform.application.cockpit_home import (
    CockpitHomeUnavailable,
    resolve_cockpit_home,
)
from clientplatform.domain.tenancy import TenantAccessDenied, TenantPermissionDenied
from clientplatform.runtime.telegram_webapp_auth import (
    TelegramWebAppAuthError,
    verify_telegram_webapp_init_data,
)
from config.settings import settings
from core.runtime_env import env_int

_COCKPIT_PREFIX = "/clientplatform/cockpit"
_COCKPIT_APP_KEY = web.AppKey("clientplatform_cockpit", bool)

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
<section id="navigation" class="grid" aria-live="polite"></section>
<section id="home-view" class="home-view" hidden><button id="home-back" type="button">Назад к разделам</button><div class="home-heading"><p class="eyebrow">Home / Today</p><h2>Сегодня</h2><p id="home-meta"></p></div><div id="home-metrics" class="metrics"></div><div id="home-money" class="money"></div><section class="home-block"><h3>Требует внимания</h3><div id="home-attention"></div></section><section class="home-block"><h3>Что сделать дальше</h3><div id="home-actions"></div></section><p id="home-empty" class="muted"></p><p id="home-limitations" class="muted"></p></section>
<section id="explanation" class="explanation" hidden><button id="close-explanation" type="button">Назад</button><h2 id="explanation-title"></h2><p id="explanation-summary"></p><p id="explanation-when"></p><p id="explanation-reason"></p></section>
</main></body></html>"""

_CSS = """*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f6f8;color:#17202a}.shell{max-width:760px;margin:0 auto;padding:20px 16px 40px}header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}.eyebrow{margin:0 0 4px;font-size:13px;font-weight:700;letter-spacing:.04em}h1{margin:0;font-size:30px;line-height:1.1}.pill{font-size:12px;background:#fff;border:1px solid #d9dee5;border-radius:999px;padding:8px 10px}.business,.status,.explanation{background:#fff;border:1px solid #e1e5ea;border-radius:16px;padding:14px;margin-bottom:14px}.business label{display:block;font-size:13px;font-weight:700;margin-bottom:8px}select{width:100%;min-height:44px;border:1px solid #cfd6de;border-radius:12px;background:#fff;padding:0 12px;font:inherit}.status{font-size:14px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{min-height:132px;text-align:left;border:1px solid #e1e5ea;border-radius:16px;background:#fff;padding:15px;font:inherit}.card h2{font-size:17px;margin:0 0 7px}.card p{font-size:13px;line-height:1.35;margin:0;color:#52606d}.card.restricted,.card.planned{opacity:.62}.explanation h2{margin:12px 0 8px}.explanation p{line-height:1.45}.explanation button,.home-view button{min-height:42px;border:0;border-radius:12px;padding:0 14px;font:inherit;font-weight:700}.home-view{background:#fff;border:1px solid #e1e5ea;border-radius:16px;padding:14px}.home-heading h2{margin:12px 0 4px}.home-heading p{margin:0 0 12px}.metrics,.money{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}.metric,.money-card,.attention-card{border:1px solid #e1e5ea;border-radius:14px;padding:12px}.metric strong,.money-card strong{display:block;font-size:24px;margin-top:4px}.metric span,.money-card span,.muted{font-size:12px;color:#66717d}.home-block{margin-top:18px}.home-block h3{margin:0 0 9px}.attention-card{margin-bottom:8px}.action-card{display:block;width:100%;text-align:left;background:#eef3f8;margin-bottom:8px}.action-card small{display:block;font-weight:400;margin-top:4px;color:#52606d}@media(max-width:520px){.grid,.metrics,.money{grid-template-columns:1fr}.shell{padding:14px 12px 28px}h1{font-size:27px}}"""

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
  const home = document.getElementById('home-view');
  const homeBack = document.getElementById('home-back');
  const homeMeta = document.getElementById('home-meta');
  const homeMetrics = document.getElementById('home-metrics');
  const homeMoney = document.getElementById('home-money');
  const homeAttention = document.getElementById('home-attention');
  const homeActions = document.getElementById('home-actions');
  const homeEmpty = document.getElementById('home-empty');
  const homeLimitations = document.getElementById('home-limitations');
  const initData = tg && typeof tg.initData === 'string' ? tg.initData : '';
  let navigationItems = [];

  const text = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const showNavigation = () => { explanation.hidden = true; home.hidden = true; nav.hidden = false; };
  close.addEventListener('click', showNavigation);
  homeBack.addEventListener('click', showNavigation);

  const showExplanation = (item) => {
    text(title, item.title);
    text(summary, item.summary);
    text(when, `Если Вам нужно: ${item.when_to_use}`);
    text(reason, item.reason || 'Доступ подтверждён сервером. Действия внутри раздела всё равно проверяют свои права отдельно.');
    nav.hidden = true;
    home.hidden = true;
    explanation.hidden = false;
  };

  const appendMetric = (container, label, value, note) => {
    const card = document.createElement('div');
    card.className = container === homeMoney ? 'money-card' : 'metric';
    const caption = document.createElement('span');
    const number = document.createElement('strong');
    const meaning = document.createElement('span');
    text(caption, label); text(number, value); text(meaning, note);
    card.append(caption, number, meaning); container.appendChild(card);
  };

  const renderHome = (payload) => {
    homeMetrics.replaceChildren(); homeMoney.replaceChildren();
    homeAttention.replaceChildren(); homeActions.replaceChildren();
    text(homeMeta, `${payload.business_name} · ${payload.timezone_name}`);
    for (const item of payload.metrics || []) appendMetric(homeMetrics, item.title, item.value, item.meaning);
    for (const item of payload.money || []) appendMetric(homeMoney, `Подтверждённая выручка · ${item.period}`, item.display, item.meaning);
    for (const item of payload.attention || []) {
      const card = document.createElement('div'); card.className = 'attention-card'; text(card, item); homeAttention.appendChild(card);
    }
    for (const item of payload.actions || []) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'action-card';
      const label = document.createElement('span'); const detail = document.createElement('small');
      text(label, item.title); text(detail, item.reason); button.append(label, detail);
      button.addEventListener('click', () => {
        const target = navigationItems.find((entry) => entry.id === item.section);
        if (target) showExplanation(target);
      });
      homeActions.appendChild(button);
    }
    text(homeEmpty, payload.empty_message || '');
    text(homeLimitations, (payload.limitations || []).length ? 'Часть источников сейчас недоступна. ClientPlatform не подменяет недостающие данные нулями.' : '');
    nav.hidden = true; explanation.hidden = true; home.hidden = false;
  };

  const post = async (path, businessId) => {
    const body = {init_data: initData};
    if (businessId) body.business_id = businessId;
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, credentials:'same-origin', cache:'no-store', body:JSON.stringify(body)});
    const payload = await response.json().catch(() => ({error:'invalid_response'}));
    if (!response.ok) throw new Error(payload.error || 'access_denied');
    return payload;
  };

  const loadHome = async () => renderHome(await post('/clientplatform/cockpit/home', select.value));
  const homeFail = (error) => {
    if (error && ['expired_init_data','business_access_denied','access_denied'].includes(error.message)) { fail(error); return; }
    homeMetrics.replaceChildren(); homeMoney.replaceChildren(); homeAttention.replaceChildren(); homeActions.replaceChildren();
    text(homeMeta, 'Не удалось обновить сводку');
    text(homeEmpty, 'Home временно недоступен. Остальные разрешённые разделы кабинета продолжают работать.');
    text(homeLimitations, 'Вернитесь к разделам и попробуйте обновить сводку позже.');
    nav.hidden = true; explanation.hidden = true; home.hidden = false;
  };
  const showItem = (item) => {
    if (item.id === 'home' && item.status === 'available') { loadHome().catch(homeFail); return; }
    showExplanation(item);
  };

  const render = (payload) => {
    nav.replaceChildren(); select.replaceChildren(); navigationItems = payload.navigation || [];
    text(role, payload.role ? `Роль: ${payload.role}` : 'Нужен бизнес');
    if (payload.onboarding_required) {
      text(status, 'У Вас пока нет подключённого бизнеса. Вернитесь в бот и выберите «Подключить мой бизнес».');
      select.disabled = true; return;
    }
    for (const business of payload.businesses || []) {
      const option = document.createElement('option'); option.value = business.id;
      text(option, `${business.name} · ${business.role}`); option.selected = Boolean(business.selected); select.appendChild(option);
    }
    select.disabled = false;
    text(status, `Открыт бизнес «${payload.business_name}». Начните с «Сегодня» — там видно, что требует внимания.`);
    for (const item of navigationItems) {
      const button = document.createElement('button'); button.type = 'button'; button.className = `card ${item.status}`;
      const heading = document.createElement('h2'); const copy = document.createElement('p');
      text(heading, item.title); text(copy, item.summary); button.append(heading, copy);
      button.addEventListener('click', () => showItem(item)); nav.appendChild(button);
    }
    showNavigation();
  };

  const load = async (businessId) => render(await post('/clientplatform/cockpit/context', businessId));
  select.addEventListener('change', () => load(select.value).catch(fail));
  function fail(error) {
    nav.replaceChildren(); home.hidden = true; explanation.hidden = true; select.disabled = true;
    text(role, 'Доступ не подтверждён');
    text(status, error && error.message === 'expired_init_data'
      ? 'Сессия Telegram устарела. Закройте кабинет и откройте его снова из бота.'
      : 'Не удалось подтвердить безопасный доступ. Закройте кабинет и откройте его снова из бота.');
  }
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


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"ok": False, "error": code}, status=status, headers=_base_headers())


async def _verified_scope(request: web.Request) -> tuple[int, str | None] | web.Response:
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
    return principal.user_id, requested_business


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


def register_cockpit_routes(app: web.Application) -> None:
    app.router.add_get(_COCKPIT_PREFIX, cockpit_shell)
    app.router.add_get(f"{_COCKPIT_PREFIX}/app.js", cockpit_script)
    app.router.add_get(f"{_COCKPIT_PREFIX}/styles.css", cockpit_styles)
    app.router.add_post(f"{_COCKPIT_PREFIX}/context", cockpit_context)
    app.router.add_post(f"{_COCKPIT_PREFIX}/home", cockpit_home)
    app[_COCKPIT_APP_KEY] = True


__all__ = [
    "cockpit_context",
    "cockpit_home",
    "cockpit_http_enabled",
    "cockpit_script",
    "cockpit_shell",
    "cockpit_styles",
    "register_cockpit_routes",
]
