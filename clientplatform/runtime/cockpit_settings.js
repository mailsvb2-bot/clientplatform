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
  const timezoneHelp = document.getElementById("settings-timezone-help");
  const save = document.getElementById("settings-save");
  const meta = document.getElementById("settings-meta");
  const message = document.getElementById("settings-message");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";

  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };
  const controller = () => window.ClientPlatformCockpitNavigation;
  const captureContext = () => controller().captureBusinessContext();
  const assertCurrent = (snapshot, payload = null) => {
    controller().assertBusinessContextCurrent(snapshot);
    const payloadBusiness = String(payload && payload.business_id || "").trim();
    if (payloadBusiness && payloadBusiness !== snapshot.businessId) throw new Error("workspace_context_changed");
  };
  const contextChanged = (error) => controller().isContextChangedError(error);
  const focusView = () => controller().focusRegion(view);

  const post = async (path, extra, businessId = String(select.value || "").trim()) => {
    const body = {init_data: initData, ...(extra || {})};
    if (businessId) body.business_id = businessId;
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

  const COMMON_TIMEZONES = Object.freeze([
    ["Europe/Moscow", "Москва"], ["Europe/Kaliningrad", "Калининград"], ["Europe/Samara", "Самара"],
    ["Asia/Yekaterinburg", "Екатеринбург"], ["Asia/Omsk", "Омск"], ["Asia/Novosibirsk", "Новосибирск"],
    ["Asia/Krasnoyarsk", "Красноярск"], ["Asia/Irkutsk", "Иркутск"], ["Asia/Yakutsk", "Якутск"],
    ["Asia/Vladivostok", "Владивосток"], ["Asia/Magadan", "Магадан"], ["Asia/Kamchatka", "Камчатка"],
    ["Europe/Tallinn", "Таллин"], ["Europe/Helsinki", "Хельсинки"], ["Europe/Berlin", "Берлин"],
    ["Europe/London", "Лондон"], ["Asia/Dubai", "Дубай"], ["Asia/Tbilisi", "Тбилиси"],
  ]);
  const timeZoneOffset = (zone) => {
    try {
      const parts = new Intl.DateTimeFormat("en-US", {timeZone:zone, timeZoneName:"shortOffset", hour:"2-digit"}).formatToParts(new Date());
      return (parts.find((part) => part.type === "timeZoneName") || {}).value || "";
    } catch (_error) { return ""; }
  };
  const addTimezone = (zone, label = zone) => {
    const option = document.createElement("option"); option.value = zone;
    const offset = timeZoneOffset(zone).replace("GMT", "UTC"); text(option, `${label}${offset ? ` (${offset})` : ""}`); timezone.appendChild(option);
  };
  const populateTimezones = (selected) => {
    timezone.replaceChildren(); const added = new Set();
    for (const [zone, label] of COMMON_TIMEZONES) { addTimezone(zone, label); added.add(zone); }
    let supported = [];
    if (Intl.supportedValuesOf) { try { supported = Intl.supportedValuesOf("timeZone"); } catch (_error) { supported = []; } }
    const more = supported.filter((zone) => !added.has(zone)).sort((a, b) => a.localeCompare(b));
    if (more.length) { const group = document.createElement("optgroup"); group.label = "Другие города и регионы"; timezone.appendChild(group); for (const zone of more) { const option = document.createElement("option"); option.value = zone; text(option, zone.replaceAll("_", " ")); group.appendChild(option); } }
    if (selected && !Array.from(timezone.options).some((option) => option.value === selected)) addTimezone(selected, selected.replaceAll("_", " "));
    timezone.value = selected || "Europe/Moscow";
  };

  const render = (payload) => {
    name.value = String(payload.business_name || "");
    description.value = String(payload.activity_description || "");
    populateTimezones(String(payload.timezone_name || "Europe/Moscow"));
    text(meta, `${payload.business_name} · основные настройки бизнеса`);
    text(message, "Изменения сохраняются в тех же данных, которыми пользуются записи, продажи и автоматизация.");
    text(timezoneHelp, "Выберите город — ClientPlatform сохранит нужный часовой пояс автоматически.");
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterSettings === "function") api.enterSettings();
    else view.hidden = false;
  };

  const load = async () => {
    const snapshot = captureContext();
    show(); setBusy(true); text(meta, "Загружаем настройки…");
    try {
      const payload = await post("/clientplatform/cockpit/settings", {}, snapshot.businessId);
      assertCurrent(snapshot, payload); render(payload); focusView();
    } catch (error) {
      if (contextChanged(error)) return;
      text(meta, "Не удалось загрузить настройки");
      text(message, error && error.message === "settings_access_denied"
        ? "Для Вашей роли изменение бизнеса недоступно."
        : "Настройки временно недоступны. Нажмите «Обновить».");
    } finally { if (controller().isBusinessContextCurrent(snapshot)) setBusy(false); }
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const snapshot = captureContext();
    setBusy(true); text(message, "Сохраняем…");
    try {
      const payload = await post("/clientplatform/cockpit/settings/update", {
        business_name: name.value, activity_description: description.value, timezone_name: timezone.value,
      }, snapshot.businessId);
      assertCurrent(snapshot, payload); render(payload);
      const api = controller();
      if (api && typeof api.syncBusinessName === "function") api.syncBusinessName(payload.business_name);
      text(message, "Сохранено. Новые данные сразу используются каноническими сервисами бизнеса.");
      if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.notificationOccurred === "function") {
        tg.HapticFeedback.notificationOccurred("success");
      }
    } catch (error) {
      if (contextChanged(error)) return;
      text(message, error && error.message === "invalid_settings_request"
        ? "Проверьте название, описание и выбранный часовой пояс."
        : "Не удалось сохранить настройки. Данные не были подтверждены как изменённые.");
    } finally { if (controller().isBusinessContextCurrent(snapshot)) setBusy(false); }
  });

  refresh.addEventListener("click", () => { void load(); });
  more.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.showNavigation === "function") api.showNavigation();
  });

  window.addEventListener("clientplatform:business-context-changing", () => {
    name.value = ""; description.value = ""; timezone.replaceChildren(); text(meta, ""); text(message, "");
  });

  window.ClientPlatformSettings = Object.freeze({
    open: () => { void load(); },
    back: () => { const api = controller(); if (api && typeof api.showHome === "function") api.showHome(); },
  });
})();
