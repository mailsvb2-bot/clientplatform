(() => {
  "use strict";

  const view = document.getElementById("settings-view");
  const select = document.getElementById("business-select");
  const refresh = document.getElementById("settings-refresh");
  const more = document.getElementById("settings-more");
  const form = document.getElementById("settings-form");
  const name = document.getElementById("settings-business-name");
  const description = document.getElementById("settings-activity");
  const timezone = document.getElementById("settings-timezone");
  const save = document.getElementById("settings-save");
  const meta = document.getElementById("settings-meta");
  const message = document.getElementById("settings-message");
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
    if (!response.ok) throw new Error(payload.error || "settings_unavailable");
    return payload;
  };

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    save.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const render = (payload) => {
    name.value = String(payload.business_name || "");
    description.value = String(payload.activity_description || "");
    timezone.value = String(payload.timezone_name || "");
    text(meta, `${payload.business_name} · основные настройки бизнеса`);
    text(message, "Изменения сохраняются в тех же данных, которыми пользуются записи, продажи и автоматизация.");
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterSettings === "function") api.enterSettings();
    else view.hidden = false;
  };

  const load = async () => {
    show();
    setBusy(true);
    text(meta, "Загружаем настройки…");
    try { render(await post("/clientplatform/cockpit/settings")); }
    catch (error) {
      text(meta, "Не удалось загрузить настройки");
      text(message, error && error.message === "settings_access_denied"
        ? "Для Вашей роли изменение бизнеса недоступно."
        : "Настройки временно недоступны. Нажмите «Обновить».");
    } finally { setBusy(false); }
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setBusy(true);
    text(message, "Сохраняем…");
    try {
      const payload = await post("/clientplatform/cockpit/settings/update", {
        business_name: name.value,
        activity_description: description.value,
        timezone_name: timezone.value,
      });
      render(payload);
      const current = select.options[select.selectedIndex];
      if (current) {
        const suffix = String(current.textContent || "").split(" · ").slice(1).join(" · ");
        text(current, suffix ? `${payload.business_name} · ${suffix}` : payload.business_name);
      }
      text(message, "Сохранено. Новые данные сразу используются каноническими сервисами бизнеса.");
      if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.notificationOccurred === "function") {
        tg.HapticFeedback.notificationOccurred("success");
      }
    } catch (error) {
      text(message, error && error.message === "invalid_settings_request"
        ? "Проверьте название, описание и часовой пояс. Часовой пояс нужен в формате IANA, например Europe/Moscow."
        : "Не удалось сохранить настройки. Данные не были подтверждены как изменённые.");
    } finally { setBusy(false); }
  });

  refresh.addEventListener("click", () => { void load(); });
  more.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.showNavigation === "function") api.showNavigation();
  });

  window.ClientPlatformSettings = Object.freeze({
    open: () => { void load(); },
    back: () => { const api = controller(); if (api && typeof api.showHome === "function") api.showHome(); },
  });
})();
