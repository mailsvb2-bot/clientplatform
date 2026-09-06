(() => {
  "use strict";

  const view = document.getElementById("calendar-view");
  const select = document.getElementById("business-select");
  const refresh = document.getElementById("calendar-refresh");
  const more = document.getElementById("calendar-more");
  const manage = document.getElementById("calendar-manage");
  const meta = document.getElementById("calendar-meta");
  const list = document.getElementById("calendar-list");
  const empty = document.getElementById("calendar-empty");
  const limitations = document.getElementById("calendar-limitations");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";

  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };

  const controller = () => window.ClientPlatformCockpitNavigation;

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    manage.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const post = async () => {
    const body = {init_data: initData, limit: 30};
    if (select.value) body.business_id = select.value;
    const response = await fetch("/clientplatform/cockpit/calendar", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: "invalid_response"}));
    if (!response.ok) throw new Error(payload.error || "calendar_unavailable");
    return payload;
  };

  const render = (payload) => {
    list.replaceChildren();
    text(meta, `${payload.business_name} · ближайшее время`);
    const items = payload.items || [];
    for (const item of items) {
      const card = document.createElement("div");
      const top = document.createElement("div");
      const when = document.createElement("strong");
      const badge = document.createElement("span");
      const title = document.createElement("p");
      const duration = document.createElement("small");
      card.className = `schedule-card ${item.status === "booked" ? "booked" : "open"}`;
      top.className = "schedule-card-top";
      badge.className = `schedule-status ${item.status === "booked" ? "booked" : "open"}`;
      text(when, item.local_start);
      text(badge, item.status === "booked" ? "Записано" : "Свободно");
      text(title, item.offering_title || "Услуга");
      text(duration, item.duration_minutes ? `${item.duration_minutes} мин.` : "");
      top.append(when, badge);
      card.append(top, title, duration);
      list.appendChild(card);
    }
    text(empty, items.length ? "" : "Ближайших открытых или занятых окон пока нет.");
    text(limitations, payload.has_more ? "Показаны ближайшие 30 окон. Остальные доступны в полном разделе расписания." : "");
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterCalendar === "function") api.enterCalendar();
    else view.hidden = false;
  };

  const fail = (error) => {
    list.replaceChildren();
    text(meta, "Не удалось обновить расписание");
    text(empty, error && error.message === "calendar_access_denied"
      ? "Для Вашей роли расписание недоступно."
      : "Расписание временно недоступно. Нажмите «Обновить».");
    text(limitations, "Ваши записи и настройки не изменялись.");
    show();
  };

  const load = async () => {
    show();
    setBusy(true);
    text(meta, "Обновляем ближайшие записи…");
    try { render(await post()); }
    catch (error) { fail(error); }
    finally { setBusy(false); }
  };

  const open = () => { void load(); };
  const handleBack = () => {
    const api = controller();
    if (api && typeof api.showHome === "function") api.showHome();
  };

  refresh.addEventListener("click", () => { void load(); });
  more.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.showNavigation === "function") api.showNavigation();
  });
  manage.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.openCanonicalSection === "function") api.openCanonicalSection("calendar", manage);
  });

  window.ClientPlatformCalendar = Object.freeze({open, back: handleBack});
})();
