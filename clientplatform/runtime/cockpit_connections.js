(() => {
  "use strict";

  const view = document.getElementById("connections-view");
  const select = document.getElementById("business-select");
  const refresh = document.getElementById("connections-refresh");
  const more = document.getElementById("connections-more");
  const meta = document.getElementById("connections-meta");
  const list = document.getElementById("connections-list");
  const empty = document.getElementById("connections-empty");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";

  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };
  const controller = () => window.ClientPlatformCockpitNavigation;

  const post = async (path, extra) => {
    const body = {init_data: initData, ...(extra || {})};
    if (select.value) body.business_id = select.value;
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: "invalid_response"}));
    if (!response.ok) throw new Error(payload.error || "connections_unavailable");
    return payload;
  };

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const openSetup = async (platform, button) => {
    button.disabled = true;
    const prior = button.textContent;
    text(button, "Готовим защищённое подключение…");
    try {
      const payload = await post("/clientplatform/cockpit/connections/setup", {platform});
      if (typeof payload.setup_url !== "string" || !payload.setup_url.startsWith("https://")) {
        throw new Error("invalid_setup_url");
      }
      window.location.assign(payload.setup_url);
    } catch (_error) {
      text(empty, "Не удалось подготовить подключение. Обновите состояние каналов и попробуйте ещё раз.");
      button.disabled = false;
      text(button, prior);
    }
  };

  const render = (payload) => {
    list.replaceChildren();
    text(meta, `${payload.business_name} · каналы общения с клиентами`);
    const items = payload.items || [];
    for (const item of items) {
      const card = document.createElement("section");
      const top = document.createElement("div");
      const title = document.createElement("strong");
      const state = document.createElement("span");
      const note = document.createElement("p");
      card.className = `connection-card ${item.availability || "unavailable"}`;
      top.className = "connection-card-top";
      state.className = "connection-state";
      text(title, item.title || item.platform || "Канал");
      text(state, item.state_label || "Состояние неизвестно");
      text(note, item.active
        ? "Канал подключён к этому бизнесу и доступен в ClientPlatform."
        : item.can_connect
          ? "Можно подключить сейчас. Данные подключения вводятся только на защищённой одноразовой странице."
          : "Подключение сейчас недоступно или требует технического внимания.");
      top.append(title, state);
      card.append(top, note);
      if (item.can_connect) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary connection-connect";
        text(button, `Подключить ${item.title || "канал"}`);
        button.addEventListener("click", () => { void openSetup(item.platform, button); });
        card.appendChild(button);
      }
      list.appendChild(card);
    }
    text(empty, items.length ? "" : "Каналы пока не найдены.");
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterConnections === "function") api.enterConnections();
    else view.hidden = false;
  };

  const load = async () => {
    show();
    setBusy(true);
    text(meta, "Проверяем реальные подключения…");
    try { render(await post("/clientplatform/cockpit/connections")); }
    catch (error) {
      list.replaceChildren();
      text(meta, "Не удалось проверить подключения");
      text(empty, error && error.message === "connections_access_denied"
        ? "Для Вашей роли управление подключениями недоступно."
        : "Состояние каналов временно недоступно. Нажмите «Обновить».");
    } finally { setBusy(false); }
  };

  refresh.addEventListener("click", () => { void load(); });
  more.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.showNavigation === "function") api.showNavigation();
  });

  window.ClientPlatformConnections = Object.freeze({
    open: () => { void load(); },
    back: () => { const api = controller(); if (api && typeof api.showHome === "function") api.showHome(); },
  });
})();
