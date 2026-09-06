(() => {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === 'string' ? tg.initData : '';
  const view = document.getElementById('customers-view');
  const nav = document.getElementById('navigation');
  const home = document.getElementById('home-view');
  const explanation = document.getElementById('explanation');
  const calendar = document.getElementById('calendar-view');
  const sales = document.getElementById('sales-view');
  const select = document.getElementById('business-select');
  const back = document.getElementById('customers-back');
  const refresh = document.getElementById('customers-refresh');
  const listPanel = document.getElementById('customer-list-panel');
  const searchForm = document.getElementById('customer-search-form');
  const search = document.getElementById('customer-search');
  const listMeta = document.getElementById('customer-list-meta');
  const list = document.getElementById('customer-list');
  const prev = document.getElementById('customer-prev');
  const next = document.getElementById('customer-next');
  const detail = document.getElementById('customer-detail');
  const detailBack = document.getElementById('customer-detail-back');
  const detailName = document.getElementById('customer-detail-name');
  const detailMeta = document.getElementById('customer-detail-meta');
  const contacts = document.getElementById('customer-contacts');
  const action = document.getElementById('customer-action');
  const timeline = document.getElementById('customer-timeline');
  const limitations = document.getElementById('customer-limitations');
  let page = null;
  let detailReturn = 'customers';

  const text = (node, value) => {
    node.textContent = value == null ? '' : String(value);
  };

  const setBusy = (busy) => {
    view.classList.toggle('busy', Boolean(busy));
    refresh.disabled = Boolean(busy);
    search.disabled = Boolean(busy);
    view.setAttribute('aria-busy', busy ? 'true' : 'false');
  };

  const post = async (path, extra) => {
    const body = {init_data: initData, ...(extra || {})};
    if (select.value) body.business_id = select.value;
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: 'invalid_response'}));
    if (!response.ok) throw new Error(payload.error || 'customer_unavailable');
    return payload;
  };

  const dateText = (value) => {
    const raw = String(value || '');
    const day = raw.slice(0, 10).split('-');
    return day.length === 3 ? `${day[2]}.${day[1]}.${day[0]}` : raw;
  };

  const showNavigation = () => {
    const controller = window.ClientPlatformCockpitNavigation;
    if (controller && typeof controller.showNavigation === 'function') { controller.showNavigation(); return; }
    view.hidden = true; explanation.hidden = true; home.hidden = true; calendar.hidden = true; sales.hidden = true; nav.hidden = false;
  };

  const enterCustomers = () => {
    const controller = window.ClientPlatformCockpitNavigation;
    if (controller && typeof controller.enterCustomers === 'function') controller.enterCustomers();
  };

  const showList = () => {
    enterCustomers();
    view.hidden = false;
    nav.hidden = true;
    home.hidden = true;
    explanation.hidden = true;
    calendar.hidden = true;
    sales.hidden = true;
    listPanel.hidden = false;
    detail.hidden = true;
  };

  const showFailure = (message) => {
    list.replaceChildren();
    text(listMeta, message || 'Не удалось загрузить клиентов. Нажмите «Обновить».');
    prev.disabled = true;
    next.disabled = true;
    showList();
  };

  const closeAfterDelivery = () => {
    if (tg && typeof tg.close === 'function') { tg.close(); return true; }
    return false;
  };

  const openAction = async (customerId, expectedActionKey, button) => {
    setBusy(true);
    if (button) button.disabled = true;
    try {
      await post('/clientplatform/cockpit/customers/action-open', {
        customer_id: customerId,
        expected_action_key: expectedActionKey,
      });
      if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.notificationOccurred === 'function') {
        tg.HapticFeedback.notificationOccurred('success');
      }
      if (!closeAfterDelivery()) {
        text(limitations, 'Следующий шаг открыт в чате с ботом. Вернитесь в Telegram.');
      }
    } catch (error) {
      text(
        limitations,
        error && error.message === 'customer_action_changed'
          ? 'Следующий шаг уже изменился. Обновите карточку клиента.'
          : 'Не удалось открыть следующий шаг. Обновите карточку и попробуйте ещё раз.',
      );
    } finally {
      if (button) button.disabled = false;
      setBusy(false);
    }
  };

  const renderDetail = (payload) => {
    contacts.replaceChildren();
    timeline.replaceChildren();
    action.replaceChildren();
    text(detailName, payload.display_name || 'Клиент');
    text(detailMeta, `Статус: ${payload.status === 'active' ? 'активный' : payload.status} · обновлено ${dateText(payload.updated_at)}`);

    for (const item of payload.contacts || []) {
      const card = document.createElement('div');
      const label = document.createElement('strong');
      const display = document.createElement('small');
      card.className = 'contact-card';
      text(label, item.label);
      text(display, item.display);
      card.append(label, display);
      contacts.appendChild(card);
    }
    if (!(payload.contacts || []).length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      text(empty, 'Подтверждённых контактных данных пока нет.');
      contacts.appendChild(empty);
    }

    if (payload.next_action) {
      const button = document.createElement('button');
      const label = document.createElement('span');
      const reason = document.createElement('small');
      button.type = 'button';
      button.className = 'action-card';
      text(label, payload.next_action.title);
      text(reason, payload.next_action.reason);
      button.append(label, reason);
      button.addEventListener('click', () => {
        void openAction(payload.customer_id, payload.next_action.action_key, button);
      });
      action.appendChild(button);
    } else {
      const empty = document.createElement('p');
      empty.className = 'muted';
      text(empty, 'Сохранённого следующего шага сейчас нет.');
      action.appendChild(empty);
    }

    for (const item of payload.timeline || []) {
      const card = document.createElement('div');
      const label = document.createElement('strong');
      const meta = document.createElement('small');
      card.className = 'timeline-card';
      text(label, item.title);
      const parts = [dateText(item.occurred_at), item.detail, item.money].filter(Boolean);
      text(meta, parts.join(' · '));
      card.append(label, meta);
      timeline.appendChild(card);
    }
    if (!(payload.timeline || []).length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      text(empty, 'История пока пуста или временно недоступна.');
      timeline.appendChild(empty);
    }

    text(
      limitations,
      (payload.limitations || []).length
        ? 'Часть дополнительных данных временно недоступна. Показаны только подтверждённые сведения.'
        : '',
    );
    enterCustomers();
    listPanel.hidden = true;
    detail.hidden = false;
    view.hidden = false;
    nav.hidden = true;
    home.hidden = true;
    explanation.hidden = true;
    calendar.hidden = true;
    sales.hidden = true;
  };

  const loadDetail = async (customerId) => {
    setBusy(true);
    try {
      const payload = await post('/clientplatform/cockpit/customers/detail', {
        customer_id: customerId,
        timeline_limit: 20,
      });
      renderDetail(payload);
    } catch (error) {
      text(listMeta, error && error.message === 'customer_not_found'
        ? 'Клиент больше недоступен в этом бизнесе.'
        : 'Не удалось открыть карточку клиента.');
      showList();
    } finally {
      setBusy(false);
    }
  };

  const renderPage = (payload) => {
    page = payload;
    list.replaceChildren();
    const items = payload.items || [];
    text(
      listMeta,
      items.length
        ? `Показано клиентов: ${items.length}${payload.query ? ` · поиск «${payload.query}»` : ''}`
        : payload.query ? 'По этому запросу клиентов не найдено.' : 'Клиентов пока нет.',
    );
    for (const item of items) {
      const button = document.createElement('button');
      const label = document.createElement('strong');
      const meta = document.createElement('small');
      button.type = 'button';
      button.className = 'customer-row';
      text(label, item.display_name || 'Клиент');
      text(meta, `Обновлено: ${dateText(item.updated_at)}`);
      button.append(label, meta);
      button.addEventListener('click', () => { detailReturn = 'customers'; loadDetail(item.customer_id); });
      list.appendChild(button);
    }
    prev.disabled = payload.previous_offset == null;
    next.disabled = payload.next_offset == null;
    showList();
  };

  const loadPage = async (offset) => {
    setBusy(true);
    try {
      const payload = await post('/clientplatform/cockpit/customers', {
        query: search.value.trim(),
        limit: 20,
        offset: Number.isInteger(offset) ? offset : 0,
      });
      renderPage(payload);
    } catch (error) {
      if (error && error.message === 'customer_access_denied') {
        showFailure('Для Вашей роли список клиентов недоступен.');
      } else {
        showFailure('Не удалось обновить список клиентов. Нажмите «Обновить».');
      }
    } finally {
      setBusy(false);
    }
  };

  const open = () => {
    detailReturn = 'customers';
    showList();
    loadPage(0);
  };

  const openCustomer = (customerId, returnView = 'customers') => {
    detailReturn = returnView === 'sales' ? 'sales' : 'customers';
    showList();
    void loadDetail(customerId);
  };

  const returnFromDetail = () => {
    if (detailReturn === 'sales') {
      const controller = window.ClientPlatformCockpitNavigation;
      if (controller && typeof controller.enterSales === 'function') { controller.enterSales(); return; }
    }
    showList();
  };

  const handleBack = () => {
    if (!detail.hidden) { returnFromDetail(); return; }
    showNavigation();
  };

  back.addEventListener('click', showNavigation);
  detailBack.addEventListener('click', returnFromDetail);
  refresh.addEventListener('click', () => loadPage(page ? page.offset : 0));
  searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    loadPage(0);
  });
  prev.addEventListener('click', () => {
    if (page && page.previous_offset != null) loadPage(page.previous_offset);
  });
  next.addEventListener('click', () => {
    if (page && page.next_offset != null) loadPage(page.next_offset);
  });

  window.ClientPlatformCustomers = Object.freeze({open, openCustomer, back:handleBack});
})();
