import os
import time
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler
from config import ZABBIX_URL, ZABBIX_LOGIN, ZABBIX_PASSWORD, TELEGRAM_TOKEN, ZABBIX_API_TOKEN, GRAFANA_URL, GRAFANA_LOGIN, GRAFANA_PASSWORD, ALLOWED_TELEGRAM_USERS
import pandas as pd
from io import BytesIO

ZABBIX_API_URL = f"{ZABBIX_URL}/api_jsonrpc.php"

# Глобальная переменная для хранения активного драйвера
active_driver = None

SELECT_SOURCE, SELECT_DASHBOARD = range(2)
INVENTORY_SELECT_GROUP = 100
INVENTORY_MANUAL_GROUP = 101
ALERT_SELECT_SEVERITY = 200

SEVERITY_MAP = {
    'critical': 5,
    'high': 4
}
SEVERITY_LABELS = {
    'critical': 'Критичные',
    'high': 'Высокие'
}

def get_dashboard_list():
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ZABBIX_API_TOKEN}"
    }
    data = {
        "jsonrpc": "2.0",
        "method": "dashboard.get",
        "params": {
            "output": ["dashboardid", "name"]
        },
        "id": 1
    }
    response = requests.post(ZABBIX_API_URL, json=data, headers=headers)
    dashboards = []
    if response.status_code == 200:
        result = response.json().get("result", [])
        for dash in result:
            dashboards.append((dash["name"], dash["dashboardid"]))
    return dashboards

def make_dashboard_screenshot(dashboard_id):
    global active_driver
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    active_driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    try:
        # Увеличиваем размер окна для большего охвата
        active_driver.set_window_size(1920, 3000)
        active_driver.get(ZABBIX_URL)
        active_driver.find_element(By.ID, "name").send_keys(ZABBIX_LOGIN)
        active_driver.find_element(By.ID, "password").send_keys(ZABBIX_PASSWORD)
        active_driver.find_element(By.ID, "enter").click()
        time.sleep(2)
        active_driver.get(f"{ZABBIX_URL}/zabbix.php?action=dashboard.view&dashboardid={dashboard_id}")
        time.sleep(3)
        # Скроллим страницу вниз для полной отрисовки
        scroll_height = active_driver.execute_script("return document.body.scrollHeight")
        active_driver.set_window_size(1920, scroll_height)
        time.sleep(1)
        screenshot_path = f"dashboard_{dashboard_id}.png"
        active_driver.save_screenshot(screenshot_path)
        return screenshot_path
    finally:
        if active_driver:
            active_driver.quit()
            active_driver = None

def is_user_allowed(user_id):
    return user_id in ALLOWED_TELEGRAM_USERS

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("Zabbix", callback_data="source_zabbix")],
        [InlineKeyboardButton("Grafana", callback_data="source_grafana")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Выберите источник дашбордов:', reply_markup=reply_markup)
    return SELECT_SOURCE

async def select_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.callback_query.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # Удаляем кнопки после нажатия
    source = query.data
    context.user_data['source'] = source
    if source == "source_zabbix":
        dashboards = get_dashboard_list()
    else:
        dashboards = get_grafana_dashboard_list()
    keyboard = [
        [InlineKeyboardButton(name, callback_data=dashboard_id)]
        for name, dashboard_id in dashboards
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text('Выберите дашборд:', reply_markup=reply_markup)
    return SELECT_DASHBOARD

async def select_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.callback_query.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    query = update.callback_query
    dashboard_id = query.data
    source = context.user_data.get('source')
    # Получаем имя дашборда
    dashboard_name = None
    if source == "source_zabbix":
        screenshot_path = make_dashboard_screenshot(dashboard_id)
        dashboards = get_dashboard_list()
        for name, did in dashboards:
            if str(did) == str(dashboard_id):
                dashboard_name = name
                break
    else:
        screenshot_path = make_grafana_dashboard_screenshot(dashboard_id)
        dashboards = get_grafana_dashboard_list()
        for name, uid in dashboards:
            if str(uid) == str(dashboard_id):
                dashboard_name = name
                break
    await query.message.reply_photo(photo=open(screenshot_path, 'rb'))
    os.remove(screenshot_path)
    if dashboard_name:
        await query.message.reply_text(f'скриншот дашборда создан "{dashboard_name}"')
    return ConversationHandler.END

async def inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    groups = get_zabbix_host_groups()
    context.user_data['all_groups'] = groups
    context.user_data['group_page'] = 0
    PAGE_SIZE = 7
    def get_page(page):
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        return groups[start:end]
    page = 0
    page_groups = get_page(page)
    keyboard = [[InlineKeyboardButton(group['name'], callback_data=group['groupid'])] for group in page_groups]
    nav_buttons = []
    if len(groups) > PAGE_SIZE:
        nav_buttons.append(InlineKeyboardButton('Вперёд ▶️', callback_data='next_page'))
    nav_buttons.append(InlineKeyboardButton('Ввести название', callback_data='manual_input'))
    keyboard.append(nav_buttons)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Выберите группу (или введите название):', reply_markup=reply_markup)
    return INVENTORY_SELECT_GROUP

async def inventory_select_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.callback_query.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    data = query.data
    groups = context.user_data.get('all_groups', get_zabbix_host_groups())
    page = context.user_data.get('group_page', 0)
    PAGE_SIZE = 7
    def get_page(page):
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        return groups[start:end]
    if data == 'next_page':
        page += 1
        if page * PAGE_SIZE >= len(groups):
            page = 0
        context.user_data['group_page'] = page
        page_groups = get_page(page)
        keyboard = [[InlineKeyboardButton(group['name'], callback_data=group['groupid'])] for group in page_groups]
        nav_buttons = []
        if len(groups) > PAGE_SIZE:
            nav_buttons.append(InlineKeyboardButton('Вперёд ▶️', callback_data='next_page'))
        nav_buttons.append(InlineKeyboardButton('Ввести название', callback_data='manual_input'))
        keyboard.append(nav_buttons)
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return INVENTORY_SELECT_GROUP
    elif data == 'manual_input':
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text('Введите название группы (или часть):')
        return INVENTORY_MANUAL_GROUP
    else:
        await query.edit_message_reply_markup(reply_markup=None)
        group_id = data
        hosts = get_zabbix_hosts_by_group(group_id)
        if not hosts:
            await query.message.reply_text('В этой группе нет хостов.')
            return ConversationHandler.END
        # Формируем Excel
        df = pd.DataFrame(hosts)
        output = BytesIO()
        df.to_excel(output, index=False)
        output.seek(0)
        await query.message.reply_document(document=output, filename='hosts.xlsx')
        return ConversationHandler.END

async def inventory_manual_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    text = update.message.text.strip().lower()
    groups = context.user_data.get('all_groups', get_zabbix_host_groups())
    found = [g for g in groups if text in g['name'].lower()]
    if not found:
        await update.message.reply_text('Группа не найдена. Попробуйте ещё раз или используйте кнопки.')
        return INVENTORY_MANUAL_GROUP
    if len(found) == 1:
        group_id = found[0]['groupid']
        hosts = get_zabbix_hosts_by_group(group_id)
        if not hosts:
            await update.message.reply_text('В этой группе нет хостов.')
            return ConversationHandler.END
        df = pd.DataFrame(hosts)
        output = BytesIO()
        df.to_excel(output, index=False)
        output.seek(0)
        await update.message.reply_document(document=output, filename='hosts.xlsx')
        return ConversationHandler.END
    # Если найдено несколько, показать выбор
    keyboard = [[InlineKeyboardButton(g['name'], callback_data=g['groupid'])] for g in found[:10]]
    await update.message.reply_text('Найдено несколько групп, выберите:', reply_markup=InlineKeyboardMarkup(keyboard))
    return INVENTORY_SELECT_GROUP

def get_grafana_dashboard_list():
    api_url = f"{GRAFANA_URL}/api/search?query=&type=dash-db"
    response = requests.get(api_url, auth=(GRAFANA_LOGIN, GRAFANA_PASSWORD))
    dashboards = []
    if response.status_code == 200:
        for dash in response.json():
            dashboards.append((dash['title'], dash['uid']))
    return dashboards

def make_grafana_dashboard_screenshot(dashboard_uid):
    global active_driver
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    active_driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    try:
        active_driver.set_window_size(1920, 1080)
        active_driver.get(GRAFANA_URL)
        time.sleep(2)
        active_driver.find_element(By.NAME, "user").send_keys(GRAFANA_LOGIN)
        active_driver.find_element(By.NAME, "password").send_keys(GRAFANA_PASSWORD)
        active_driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        time.sleep(3)
        active_driver.get(f"{GRAFANA_URL}/d/{dashboard_uid}")
        time.sleep(5)
        screenshot_path = f"grafana_dashboard_{dashboard_uid}.png"
        active_driver.save_screenshot(screenshot_path)
        return screenshot_path
    finally:
        if active_driver:
            active_driver.quit()
            active_driver = None

def get_zabbix_host_groups():
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ZABBIX_API_TOKEN}"
    }
    data = {
        "jsonrpc": "2.0",
        "method": "hostgroup.get",
        "params": {
            "output": ["groupid", "name"]
        },
        "id": 1
    }
    response = requests.post(ZABBIX_API_URL, json=data, headers=headers)
    if response.status_code == 200:
        return response.json().get("result", [])
    return []

def get_zabbix_hosts_by_group(group_id):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ZABBIX_API_TOKEN}"
    }
    data = {
        "jsonrpc": "2.0",
        "method": "host.get",
        "params": {
            "groupids": group_id,
            "output": ["hostid", "host", "name"],
            "selectInterfaces": ["ip", "type"],
            "selectParentTemplates": ["name"],
            "selectGroups": ["name"]
        },
        "id": 1
    }
    response = requests.post(ZABBIX_API_URL, json=data, headers=headers)
    hosts = []
    if response.status_code == 200:
        for h in response.json().get("result", []):
            ip = h.get('interfaces', [{}])[0].get('ip', '') if h.get('interfaces') else ''
            # Определяем тип интерфейса (1 - агент, 2 - SNMP, 3 - IPMI, 4 - JMX)
            iface_type_map = {1: 'Agent', 2: 'SNMP', 3: 'IPMI', 4: 'JMX'}
            iface_type = ''
            if h.get('interfaces'):
                iface_type = iface_type_map.get(h['interfaces'][0].get('type', 1), str(h['interfaces'][0].get('type', '')))
            template = ', '.join([t['name'] for t in h.get('parentTemplates', [])])
            hosts.append({
                'Имя': h.get('name', ''),
                'IP': ip,
                'Интерфейс': iface_type,
                'Шаблон': template
            })
    return hosts

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton('Критичные', callback_data='critical')],
        [InlineKeyboardButton('Высокие', callback_data='high')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Выберите уровень критичности:', reply_markup=reply_markup)
    return ALERT_SELECT_SEVERITY

async def alert_select_severity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.callback_query.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    severity_key = query.data
    severity = SEVERITY_MAP.get(severity_key, 5)
    alerts = get_zabbix_critical_alerts(severity)
    label = SEVERITY_LABELS.get(severity_key, 'Критичные')
    if not alerts:
        await query.message.reply_text(f'Нет {label.lower()} триггеров.')
        return ConversationHandler.END
    # Формируем более информативный вывод
    msg = f'<b>{label} триггеры:</b>\n\n'
    for a in alerts:
        msg += (
            f"<b>Хост:</b> {a['host']}\n"
            f"<b>Описание:</b> {a['description']}\n"
            f"<b>Критичность:</b> {a.get('priority_label', 'N/A')} ({a.get('priority', 'N/A')})\n"
            f"<b>Время срабатывания:</b> {a['lastchange']}\n"
            "-----------------------------\n"
        )
    await query.message.reply_text(msg, parse_mode='HTML')
    return ConversationHandler.END

def get_zabbix_critical_alerts(min_severity=5):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ZABBIX_API_TOKEN}"
    }
    data = {
        "jsonrpc": "2.0",
        "method": "trigger.get",
        "params": {
            "output": ["description", "priority", "lastchange"],
            "selectHosts": ["host"],
            "filter": {"value": 1},
            "sortfield": "lastchange",
            "sortorder": "DESC",
            "limit": 10,
            "only_true": True,
            "min_severity": min_severity
        },
        "id": 1
    }
    response = requests.post(ZABBIX_API_URL, json=data, headers=headers)
    alerts = []
    PRIORITY_LABELS = {
        '0': 'Не классифицировано',
        '1': 'Информация',
        '2': 'Предупреждение',
        '3': 'Средняя',
        '4': 'Высокая',
        '5': 'Критичная',
    }
    if response.status_code == 200:
        for trig in response.json().get("result", []):
            host = trig.get('hosts', [{}])[0].get('host', '') if trig.get('hosts') else ''
            priority = str(trig.get('priority', 'N/A'))
            alerts.append({
                'host': host,
                'description': trig.get('description', ''),
                'lastchange': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(trig.get('lastchange', '0')))),
                'priority': priority,
                'priority_label': PRIORITY_LABELS.get(priority, priority)
            })
    return alerts

# Conversation handler setup
from telegram.ext import filters

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_driver
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    
    # Останавливаем активный браузер, если он есть
    if active_driver:
        try:
            active_driver.quit()
            active_driver = None
        except Exception as e:
            # Игнорируем ошибки при закрытии браузера
            pass
    
    # Очищаем данные пользователя
    if context.user_data:
        context.user_data.clear()
    
    await update.message.reply_text('🔄 Бот перезапущен! Все состояния сброшены. Можете использовать команды заново.')
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text('У вас нет доступа к этому боту.')
        return ConversationHandler.END
    
    await update.message.reply_text('❌ Операция отменена.')
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "/dashboard — выбрать источник (Zabbix/Grafana) и получить скриншот дашборда.\n"
        "/inventory — получить список групп и экспортировать хосты выбранной группы в Excel.\n"
        "/alert — получить список критичных триггеров.\n"
        "/stop — принудительно остановить активный браузер (если завис).\n"
        "/restart — перезапустить бота и сбросить все состояния.\n"
        "/cancel — отменить текущую операцию.\n"
        "/help — показать это сообщение."
    )
    await update.message.reply_text(help_text)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_driver
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await update.message.reply_text('У вас нет доступа к этому боту.')
        return
    
    if active_driver:
        try:
            active_driver.quit()
            active_driver = None
            await update.message.reply_text('✅ Браузер успешно остановлен!')
        except Exception as e:
            await update.message.reply_text(f'❌ Ошибка при остановке браузера: {str(e)}')
    else:
        await update.message.reply_text('ℹ️ Активный браузер не найден.')

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Создаем обработчики команд, которые могут прерывать диалог
    dashboard_handler = CommandHandler("dashboard", dashboard_command)
    inventory_handler = CommandHandler("inventory", inventory_command)
    alert_handler = CommandHandler("alert", alert_command)
    help_handler = CommandHandler("help", help_command)
    stop_handler = CommandHandler("stop", stop_command)
    restart_handler = CommandHandler("restart", restart_command)
    cancel_handler = CommandHandler("cancel", cancel_command)
    
    conv_handler = ConversationHandler(
        entry_points=[dashboard_handler, inventory_handler, alert_handler],
        states={
            SELECT_SOURCE: [
                CallbackQueryHandler(select_source),
                dashboard_handler, inventory_handler, alert_handler, help_handler, stop_handler, restart_handler, cancel_handler
            ],
            SELECT_DASHBOARD: [
                CallbackQueryHandler(select_dashboard),
                dashboard_handler, inventory_handler, alert_handler, help_handler, stop_handler, restart_handler, cancel_handler
            ],
            INVENTORY_SELECT_GROUP: [
                CallbackQueryHandler(inventory_select_group),
                dashboard_handler, inventory_handler, alert_handler, help_handler, stop_handler, restart_handler, cancel_handler
            ],
            INVENTORY_MANUAL_GROUP: [
                CommandHandler('cancel', cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, inventory_manual_group),
            ],
            ALERT_SELECT_SEVERITY: [
                CallbackQueryHandler(alert_select_severity),
                dashboard_handler, inventory_handler, alert_handler, help_handler, stop_handler, restart_handler, cancel_handler
            ],
        },
        fallbacks=[cancel_handler]
    )
    
    app.add_handler(conv_handler)
    app.add_handler(help_handler)
    app.add_handler(stop_handler)
    app.add_handler(restart_handler)
    print("Бот запущен")
    try:
        app.run_polling()
    except KeyboardInterrupt:
        print("Бот остановлен")

if __name__ == "__main__":
    main()
