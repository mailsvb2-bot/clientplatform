(() => {
  "use strict";

  const view = document.getElementById("events-view");
  const select = document.getElementById("business-select");
  const more = document.getElementById("events-more");
  const refresh = document.getElementById("events-refresh");
  const meta = document.getElementById("events-meta");
  const list = document.getElementById("events-list");
  const empty = document.getElementById("events-empty");
  const limitations = document.getElementById("events-limitations");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";

  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };
  const controller = () => window.ClientPlatformCockpitNavigation;
  const captureContext = () => controller().captureBusinessContext();
  const contextChanged = (error) => controller().isContextChangedError(error);
  const assertCurrent = (snapshot, payload = null) => {
    controller().assertBusinessContextCurrent(snapshot);
    const payloadBusiness = String(payload && payload.business_id || "").trim();
    if (payloadBusiness && payloadBusiness !== snapshot.businessId) {
      throw new Error("workspace_context_changed");
    }
  };

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const post = async (path, extra = {}, businessId = String(select.value || "").trim()) => {
    const body = {init_data: initData, ...extra};
    if (businessId) body.business_id = businessId;
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: "invalid_response"}));
    if (!response.ok) {
      const error = new Error(payload.error || "events_unavailable");
      error.code = payload.error || "events_unavailable";
      throw error;
    }
    return payload;
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterEvents === "function") api.enterEvents();
    else view.hidden = false;
  };

  const revenueText = (items) => (items || []).map((item) => item.display).filter(Boolean).join(", ") || "—";

  const openLive = async (eventId, panel, button) => {
    const snapshot = captureContext();
    button.disabled = true;
    text(panel, "Открываем комнаты вебинара…");
    try {
      const payload = await post("/clientplatform/cockpit/events/live", {event_id: eventId}, snapshot.businessId);
      assertCurrent(snapshot, payload);
      panel.replaceChildren();
      const sessions = (payload.sessions || []).filter((session) => session.join_ready && session.join_url);
      if (!sessions.length) {
        text(panel, "Ссылка на эфир пока не добавлена.");
        return;
      }
      const heading = document.createElement("strong");
      text(heading, sessions.length > 1 ? "Выберите день вебинара:" : "Эфир готов:");
      panel.appendChild(heading);
      for (const session of sessions) {
        const link = document.createElement("a");
        link.className = "primary-cta";
        link.href = String(session.join_url);
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        text(
          link,
          sessions.length > 1
            ? `▶️ День ${session.position} · ${session.local_start}`
            : `▶️ Открыть эфир · ${session.local_start}`,
        );
        panel.appendChild(link);
      }
    } catch (error) {
      if (contextChanged(error)) return;
      const code = error && (error.code || error.message);
      text(
        panel,
        code === "event_manage_denied"
          ? "Для Вашей роли запуск эфира недоступен."
          : code === "event_not_found"
            ? "Вебинар уже недоступен. Обновите список."
            : "Не удалось открыть комнаты. Обновите вебинары и попробуйте ещё раз.",
      );
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) button.disabled = false;
    }
  };

  const render = (payload) => {
    list.replaceChildren();
    text(meta, `${payload.business_name || "Ваш бизнес"} · часовой пояс ${payload.timezone_name || "бизнеса"}`);
    const items = payload.items || [];
    for (const item of items) {
      const card = document.createElement("section");
      const top = document.createElement("div");
      const title = document.createElement("strong");
      const status = document.createElement("span");
      const when = document.createElement("p");
      const result = document.createElement("small");
      const livePanel = document.createElement("div");
      card.className = "home-block";
      top.className = "schedule-card-top";
      status.className = "schedule-status";
      livePanel.className = "workspace-form";
      text(title, item.title || "Вебинар");
      text(status, item.status === "published" ? "Опубликован" : item.status || "Черновик");
      text(when, item.local_start || "");
      text(
        result,
        `Регистрации: ${Number(item.registered || 0)} · Пришли: ${Number(item.attendance_confirmed || 0)} · Оплаты: ${Number(item.paid || 0)} · Выручка: ${revenueText(item.revenue)}`,
      );
      top.append(title, status);
      card.append(top, when, result);
      if (item.status === "published" && item.join_ready) {
        const conduct = document.createElement("button");
        conduct.type = "button";
        conduct.className = "primary-cta";
        text(conduct, "▶️ Провести вебинар");
        conduct.addEventListener("click", () => { void openLive(item.id, livePanel, conduct); });
        card.append(conduct, livePanel);
      } else if (item.status === "published") {
        const note = document.createElement("p");
        note.className = "muted";
        text(note, "Ссылка на эфир ещё не добавлена. Добавьте площадку в разделе вебинаров в боте.");
        card.appendChild(note);
      }
      list.appendChild(card);
    }
    text(empty, items.length ? "" : "Вебинаров пока нет. Создайте первый в разделе вебинаров в боте.");
    const notes = payload.limitations || [];
    text(limitations, notes.join(" "));
  };

  const fail = (error) => {
    list.replaceChildren();
    text(meta, "Не удалось обновить вебинары");
    text(
      empty,
      error && (error.code || error.message) === "events_access_denied"
        ? "Для Вашей роли вебинары недоступны."
        : "Вебинары временно недоступны. Нажмите «Обновить».",
    );
    text(limitations, "Расписание, регистрации и ссылки на эфир не изменялись.");
    show();
  };

  const load = async () => {
    const snapshot = captureContext();
    show();
    setBusy(true);
    list.replaceChildren();
    text(meta, "Обновляем вебинары…");
    text(empty, "");
    text(limitations, "");
    try {
      const payload = await post("/clientplatform/cockpit/events", {limit: 30}, snapshot.businessId);
      assertCurrent(snapshot, payload);
      render(payload);
      controller().focusRegion(view);
    } catch (error) {
      if (!contextChanged(error)) fail(error);
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  refresh.addEventListener("click", () => { void load(); });
  more.addEventListener("click", () => controller().showNavigation());
  window.addEventListener("clientplatform:business-context-changing", () => {
    list.replaceChildren();
    text(meta, "");
    text(empty, "");
    text(limitations, "");
  });

  window.ClientPlatformEvents = Object.freeze({
    open: load,
    back: () => controller().showNavigation(),
  });
})();
