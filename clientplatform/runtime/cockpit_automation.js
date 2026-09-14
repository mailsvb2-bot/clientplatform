(() => {
  "use strict";

  const view = document.getElementById("automation-view");
  const select = document.getElementById("business-select");
  const refresh = document.getElementById("automation-refresh");
  const more = document.getElementById("automation-more");
  const meta = document.getElementById("automation-meta");
  const state = document.getElementById("automation-state");
  const toggle = document.getElementById("automation-toggle");
  const approvals = document.getElementById("automation-approvals");
  const empty = document.getElementById("automation-empty");
  const note = document.getElementById("automation-note");
  const advanced = document.getElementById("automation-advanced");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";
  let autopilotEnabled = false;

  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };
  const controller = () => window.ClientPlatformCockpitNavigation;
  const captureContext = () => controller().captureBusinessContext();
  const contextChanged = (error) => controller().isContextChangedError(error);
  const assertCurrent = (snapshot, payload = null) => {
    controller().assertBusinessContextCurrent(snapshot);
    const payloadBusiness = String(payload && payload.business_id || "").trim();
    if (payloadBusiness && payloadBusiness !== snapshot.businessId) throw new Error("workspace_context_changed");
  };
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
    if (!response.ok) throw new Error(payload.error || "automation_unavailable");
    return payload;
  };

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    toggle.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const appendApproval = (item) => {
    const card = document.createElement("section");
    const top = document.createElement("div");
    const title = document.createElement("strong");
    const status = document.createElement("span");
    const details = document.createElement("p");
    card.className = "workspace-card automation-card";
    top.className = "connection-card-top";
    status.className = "connection-state";
    text(title, item.title || "Автоматическое действие");
    text(status, item.status_label || "Состояние неизвестно");
    const parts = [];
    if (item.channel_label) parts.push(`Канал: ${item.channel_label}`);
    if (item.amount_display) parts.push(`Сумма: ${item.amount_display}`);
    if (item.reason) parts.push(`Почему нужно решение: ${item.reason}`);
    if (item.expires_at) parts.push(`Действует до: ${item.expires_at}`);
    text(details, parts.join(" · "));
    top.append(title, status);
    card.append(top, details);

    const actions = document.createElement("div");
    actions.className = "workspace-actions";
    const addDecision = (label, decision) => {
      const button = document.createElement("button");
      button.type = "button";
      text(button, label);
      button.addEventListener("click", () => { void decide(item, decision, button); });
      actions.appendChild(button);
    };
    if (item.can_approve) addDecision("Разрешить", "approve");
    if (item.can_reject) addDecision("Отклонить", "reject");
    if (item.can_revoke) addDecision("Отозвать разрешение", "revoke");
    if (actions.childElementCount) card.appendChild(actions);
    approvals.appendChild(card);
  };

  const render = (payload) => {
    approvals.replaceChildren();
    text(meta, `${payload.business_name} · ${payload.policy_state}`);
    const expiry = payload.policy_expires_at ? ` · до ${payload.policy_expires_at}` : "";
    text(state, `Режим: ${payload.mode_label || "Осторожный"}${expiry}`);
    text(note, payload.safety_note || "");
    autopilotEnabled = Boolean(payload.autopilot_enabled);
    toggle.hidden = !payload.can_change_policy;
    text(toggle, autopilotEnabled ? "Выключить автопилот анализа" : "Включить автопилот анализа");
    const items = payload.approvals || [];
    for (const item of items) appendApproval(item);
    text(empty, items.length
      ? (payload.pending_count ? `Ждут решения: ${payload.pending_count}.` : "Сейчас решений, ожидающих владельца, нет.")
      : "Сейчас нет действий, которые ждут подтверждения или могут быть отозваны.");
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterAutomation === "function") api.enterAutomation();
    else view.hidden = false;
  };

  const load = async () => {
    const snapshot = captureContext();
    show(); setBusy(true); approvals.replaceChildren(); text(empty, "");
    text(meta, "Проверяем актуальные разрешения…");
    try {
      const payload = await post("/clientplatform/cockpit/automation", {}, snapshot.businessId);
      assertCurrent(snapshot, payload); render(payload); focusView();
    } catch (error) {
      if (contextChanged(error)) return;
      approvals.replaceChildren(); text(state, ""); text(note, "");
      text(meta, "Не удалось проверить автоматические действия");
      text(empty, error && error.message === "automation_access_denied"
        ? "Для Вашей роли этот раздел недоступен."
        : "Состояние автоматизации временно недоступно. Нажмите «Обновить».");
    } finally { if (controller().isBusinessContextCurrent(snapshot)) setBusy(false); }
  };

  const setAutopilot = async () => {
    const snapshot = captureContext();
    const desired = !autopilotEnabled;
    setBusy(true);
    try {
      const payload = await post("/clientplatform/cockpit/automation/autopilot", {enabled: desired}, snapshot.businessId);
      assertCurrent(snapshot, payload); render(payload);
    } catch (error) {
      if (!contextChanged(error)) text(empty, error && error.message === "automation_change_rejected"
        ? "Политика изменилась. Обновите экран и повторите решение."
        : "Не удалось изменить режим. Обновите экран и попробуйте ещё раз.");
    } finally { if (controller().isBusinessContextCurrent(snapshot)) setBusy(false); }
  };

  const decide = async (item, decision, button) => {
    const snapshot = captureContext();
    button.disabled = true;
    const prior = String(button.textContent || "");
    text(button, "Сохраняем решение…");
    try {
      const payload = await post("/clientplatform/cockpit/automation/decision", {
        approval_id: item.id,
        request_fingerprint: item.request_fingerprint,
        decision,
      }, snapshot.businessId);
      assertCurrent(snapshot, payload); render(payload);
    } catch (error) {
      if (!contextChanged(error)) text(empty, error && error.message === "automation_change_rejected"
        ? "Это решение уже изменилось или устарело. Обновите экран — старое действие не выполнено."
        : "Не удалось сохранить решение. Обновите экран и попробуйте ещё раз.");
    } finally {
      if (controller().isBusinessContextCurrent(snapshot) && document.contains(button)) {
        button.disabled = false; text(button, prior);
      }
    }
  };

  refresh.addEventListener("click", () => { void load(); });
  toggle.addEventListener("click", () => { void setAutopilot(); });
  more.addEventListener("click", () => controller().showNavigation());
  advanced.addEventListener("click", () => controller().openCanonicalSection("automation", advanced));
  window.addEventListener("clientplatform:business-context-changing", () => {
    approvals.replaceChildren(); text(meta, ""); text(state, ""); text(empty, ""); text(note, "");
  });

  window.ClientPlatformAutomation = Object.freeze({
    open: () => { void load(); },
    back: () => { const api = controller(); if (api && typeof api.showHome === "function") api.showHome(); },
  });
})();
