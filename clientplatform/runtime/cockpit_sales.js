(() => {
  "use strict";

  const view = document.getElementById("sales-view");
  const select = document.getElementById("business-select");
  const refresh = document.getElementById("sales-refresh");
  const more = document.getElementById("sales-more");
  const manage = document.getElementById("sales-manage");
  const meta = document.getElementById("sales-meta");
  const handoff = document.getElementById("sales-handoff");
  const list = document.getElementById("sales-list");
  const empty = document.getElementById("sales-empty");
  const limitations = document.getElementById("sales-limitations");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";
  const sourceNames = {telegram: "Telegram", vk: "ВКонтакте", max: "MAX", web: "Сайт"};

  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };
  const controller = () => window.ClientPlatformCockpitNavigation;

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    manage.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const post = async () => {
    const body = {init_data: initData, limit: 20};
    if (select.value) body.business_id = select.value;
    const response = await fetch("/clientplatform/cockpit/sales", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: "invalid_response"}));
    if (!response.ok) throw new Error(payload.error || "sales_unavailable");
    return payload;
  };

  const openCustomer = (customerId) => {
    const customers = window.ClientPlatformCustomers;
    if (customers && typeof customers.openCustomer === "function") customers.openCustomer(customerId, "sales");
  };

  const render = (payload) => {
    list.replaceChildren();
    text(meta, `${payload.business_name} · активные продажи`);
    const handoffCount = Number(payload.handoff_count || 0);
    text(handoff, handoffCount ? `Нужно личное участие сотрудника: ${handoffCount}` : "Срочных передач сотруднику сейчас нет.");
    handoff.classList.toggle("attention-card", handoffCount > 0);
    const items = payload.items || [];
    for (const item of items) {
      const button = document.createElement("button");
      const top = document.createElement("div");
      const name = document.createElement("strong");
      const stage = document.createElement("span");
      const action = document.createElement("p");
      const details = document.createElement("small");
      button.type = "button";
      button.className = `sales-card${item.overdue ? " overdue" : ""}`;
      top.className = "sales-card-top";
      stage.className = "sales-stage";
      text(name, item.customer_name || "Клиент");
      text(stage, item.stage_label || item.stage || "В работе");
      text(action, item.next_action ? `Дальше: ${item.next_action}` : "Следующий шаг ещё не задан");
      const parts = [];
      if (item.due_display) parts.push(`${item.overdue ? "Просрочено" : "Срок"}: ${item.due_display}`);
      if (item.source_kind) parts.push(sourceNames[item.source_kind] || item.source_kind);
      text(details, parts.join(" · "));
      top.append(name, stage);
      button.append(top, action, details);
      button.addEventListener("click", () => openCustomer(item.customer_id));
      list.appendChild(button);
    }
    text(empty, items.length ? "" : "Активных продаж сейчас нет. Новые лиды появятся здесь автоматически.");
    text(limitations, payload.has_more ? "Показаны 20 ближайших по сроку продаж. Полная очередь доступна в разделе продаж." : "");
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterSales === "function") api.enterSales();
    else view.hidden = false;
  };

  const fail = (error) => {
    list.replaceChildren();
    text(meta, "Не удалось обновить продажи");
    text(handoff, "");
    text(empty, error && error.message === "sales_access_denied"
      ? "Для Вашей роли продажи недоступны."
      : "Продажи временно недоступны. Нажмите «Обновить».");
    text(limitations, "Клиенты и история продаж не изменялись.");
    show();
  };

  const load = async () => {
    show();
    setBusy(true);
    text(meta, "Обновляем очередь продаж…");
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
    if (api && typeof api.openCanonicalSection === "function") api.openCanonicalSection("sales", manage);
  });

  window.ClientPlatformSales = Object.freeze({open, back: handleBack});
})();
