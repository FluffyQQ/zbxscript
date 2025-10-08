#!/usr/lib64/zabbix7-lts/externalscripts/myenv/bin/python3
import base64
import json
import logging
import os
import random
import re
import sys
import time
import warnings
from datetime import datetime
from functools import wraps
from pathlib import Path

import requests
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

env_path = Path(__file__).with_name('.env')
load_dotenv(dotenv_path=env_path)
load_dotenv()

ISALES_LOGIN = os.getenv('ISALES_LOGIN')
ISALES_PASSWORD = os.getenv('ISALES_PASSWORD')

# Конфигурация Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Учетные данные Selenium Grid
SELENIUM_GRID_LOGIN = os.getenv('SELENIUM_GRID_LOGIN')
SELENIUM_GRID_PASSWORD = os.getenv('SELENIUM_GRID_PASSWORD')

URL = 'https://pro.delo-logistics.com'
DRIVER_WAIT_EXPLICIT = 10
SCREENSHOTS_DIR = '/opt/screenshots'

# Подавление лишних предупреждений в stdout/stderr
warnings.filterwarnings("ignore", category=UserWarning)

# Полное отключение логирования — выводится только JSON
logging.disable(logging.CRITICAL)
logger = logging.getLogger(__name__)

# Selenium Grid url
SELENIUM_GRID_URL = f"http://{SELENIUM_GRID_LOGIN}:{SELENIUM_GRID_PASSWORD}@172.18.65.116:4444/wd/hub"

# Переключатель для локального запуска
USE_SELENIUM_GRID = True  # False — запустить локально, True — через Grid

# Конфигурация Zabbix — нужен только JSON-вывод

class TestResult:
    """Класс для хранения результатов тестирования"""
    def __init__(self):
        self.start_time = datetime.now()
        self.start_timestamp = time.time()
        self.steps = {}
        self.success = True
        self.status = "1"
        self.message = ""
        self.error = None
        self.screenshot = None
        self.screenshot_base64 = None
        self.order_id = None
        self.selected_route = None

    def add_step(self, step_name, status="1", timing=None):
        """Добавить шаг теста (минимальные данные: статус и тайминги)"""
        if timing is None:
            timing = {
                "start_time": datetime.now().isoformat(),
                "start_timestamp": time.time(),
                "end_time": datetime.now().isoformat(),
                "end_timestamp": time.time(),
                "duration_seconds": 0.0
            }

        self.steps[step_name] = {
            "status": status,
            "timing": timing
        }

        # Обновляем общий статус
        if status == "0":
            self.success = False
            self.status = "0"

    def set_screenshot(self, screenshot_bytes):
        """Установить скриншот из bytes"""
        try:
            self.screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            self.screenshot = self.screenshot_base64
        except Exception as e:
            logger.error(f"Ошибка при кодировании скриншота: {e}")

    def set_order_id(self, order_id):
        """Установить ID заказа"""
        self.order_id = order_id

    def finalize(self, success, message, error=None):
        """Завершить тест"""
        self.end_time = datetime.now()
        self.end_timestamp = time.time()
        self.total_duration = self.end_timestamp - self.start_timestamp

        self.success = success
        self.status = "1" if success else "0"
        self.message = message
        self.error = error

        self.test_info = {
            "start_time": self.start_time.isoformat(),
            "start_timestamp": self.start_timestamp,
            "end_time": self.end_time.isoformat(),
            "end_timestamp": self.end_timestamp,
            "total_duration": round(self.total_duration, 2)
        }

    def to_dict(self):
        """Преобразовать в словарь для JSON"""
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "test_info": self.test_info,
            "steps": self.steps,
            "screenshot": self.screenshot,
            "order_id": self.order_id,
            "selected_route": self.selected_route
        }


class TelegramBot:
    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"

    def send_photo_from_bytes(self, photo_bytes, caption, filename):
        """Отправить фото из bytes в Telegram с подписью и именем файла"""
        try:
            files = {'photo': (filename, photo_bytes)}
            data = {
                'chat_id': self.chat_id,
                'caption': caption,
                'parse_mode': 'HTML'
            }
            response = requests.post(f"{self.api_url}/sendPhoto", files=files, data=data)
            response.raise_for_status()
            logger.info(f"Успешно отправлено фото в Telegram: {filename}")
            return True
        except Exception as e:
            logger.error(f"Не удалось отправить фото в Telegram: {e}")
            return False

telegram_bot = None
if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'):
    telegram_bot = TelegramBot(os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID'))


def ensure_screenshots_dir():
    """Создает директорию для скриншотов если она не существует"""
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        logger.info(f"Директория для скриншотов готова: {SCREENSHOTS_DIR}")
    except Exception as e:
        logger.error(f"Ошибка при создании директории {SCREENSHOTS_DIR}: {e}")



def retry_on_exception(retries=2, delay=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except (TimeoutException, StaleElementReferenceException, WebDriverException, Exception):
                    if attempt == retries - 1:
                        logger.error(f"Неудача после {retries} попыток: {func.__name__}")
                        raise
                    logger.warning(f"Повтор {attempt + 1}/{retries} для {func.__name__}")
                    # Обновляем страницу
                    try:
                        args[0].driver.refresh()
                        logger.info("Страница обновлена после ошибки")
                    except Exception as refresh_exc:
                        logger.error(f"Ошибка при обновлении страницы: {refresh_exc}")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

class TestIsales:
    add_info = {}

    TEST_DESCRIPTIONS = {
        "test_01_login": "Вход в систему",
        "test_02_select_points": "Выбор терминала отправления/назначения",
        "test_03_select_cargo": "Выбор груза",
        "test_04_select_contract": "Выбор контракта",
        "test_05_calculation": "Расчет стоимости",
        "test_06_select_transport_solution": "Выбор транспортного решения",
        "test_07_update_services": "Выбор услуг ТР",
        "test_08_select_shipper_consignee": "Заполнение грузоотправителя/грузополучателя",
        "test_09_order_create": "Создание заказа",
        "test_10_order_approval": "Утверждение заказа",
        "test_11_order_reserving_equipment_complete": "Резервирование оборудования",
        "test_12_order_need_pay_status": "Статус \"Требуется оплата\"",
        "test_13_order_documents": "Проверка счета",
        "test_14_order_cancel": "Отмена заказа",
    }

    def send_screenshot_to_telegram(self, screenshot_bytes, test_name):
        """Отправить скриншот в Telegram с форматированной подписью"""
        if telegram_bot is None:
            logger.warning("Бот Telegram не настроен, пропускаю уведомление")
            return

        try:
            now = datetime.now()
            date_str = now.strftime('%d.%m.%Y')
            time_str = now.strftime('%H:%M')
            order_id = self.add_info.get('order_id', 'Не оформлен')

            # Получаем номер шага и описание
            test_number = 'N/A'
            test_description = ''
            if test_name and test_name.startswith('test_'):
                try:
                    test_num = test_name.split('_')[1]
                    test_number = f"Шаг {test_num}"
                    test_description = self.TEST_DESCRIPTIONS.get(test_name, '')
                    if test_description:
                        test_number = f"{test_number} - {test_description}"
                except (IndexError, ValueError):
                    test_number = test_name

            # Формируем caption
            caption = f"""<b>Ошибка в тесте DL</b>\n\n<b>Заказ:</b> {order_id}\n<b>Шаг:</b> {test_number}\n<b>Дата:</b> {date_str}\n<b>Время:</b> {time_str}"""

            # Добавляем ссылку на заказ для тестов 9-14
            tests_with_link = [
                "test_09_order_create",
                "test_10_order_approval",
                "test_11_order_reserving_equipment_complete",
                "test_12_order_need_pay_status",
                "test_13_order_documents",
                "test_14_order_cancel",
            ]
            if test_name in tests_with_link and order_id and str(order_id).isdigit():
                caption += f"\n<a href=\"https://pro.delo-logistics.com/private/order/{order_id}\">Ссылка на заказ</a>"

            # Генерируем имя файла для Telegram
            time_str = datetime.now().strftime('%Y%m%d-%H%M%S')
            d = f"D_{self.add_info.get('draft_id')}_" if self.add_info.get('draft_id') else ""
            o = f"O_{self.add_info.get('order_id')}_" if self.add_info.get('order_id') else ""
            filename = f"test_isales_{d}{o}test_error_{test_name}_{time_str}.png"

            telegram_bot.send_photo_from_bytes(screenshot_bytes, caption, filename)

        except Exception as e:
            logger.error(f"Не удалось отправить скриншот в Telegram: {e}")

    def save_screenshot(self, name, test_name=None):
        try:
            if name.startswith('test_error'):
                logger.info(f"Делаю скриншот и отправляю в Telegram: {name}")
                time.sleep(1)
                # Создаем скриншот в памяти
                screenshot_bytes = self.driver.get_screenshot_as_png()

                # Сохраняем локально как фиксированное имя и предварительно удаляем существующий файл
                try:
                    ensure_screenshots_dir()
                    filename = "test_isales_error.png"
                    filepath = os.path.join(SCREENSHOTS_DIR, filename)
                    try:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                            logger.info(f"Удален старый файл скриншота: {filepath}")
                    except Exception as rm_err:
                        logger.warning(f"Не удалось удалить старый скриншот {filepath}: {rm_err}")
                    with open(filepath, 'wb') as f:
                        f.write(screenshot_bytes)
                    logger.info(f"Скриншот сохранён: {filepath}")
                except Exception as save_error:
                    logger.error(f"Не удалось сохранить скриншот локально: {save_error}")

                # Отправляем в Telegram
                self.send_screenshot_to_telegram(screenshot_bytes, test_name)
                return "screenshot_sent_to_telegram"
            return None

        except WebDriverException as e:
            logger.error(f"Не удалось сделать скриншот: {e}")
            raise

    def wait_element(self, loc, timeout=DRIVER_WAIT_EXPLICIT):
        try:
            wait = TestIsales.wait if timeout == DRIVER_WAIT_EXPLICIT else WebDriverWait(TestIsales.driver, timeout)
            return wait.until(EC.presence_of_element_located((By.XPATH, loc)))
        except TimeoutException:
            logger.error(f"Элемент не найден: {loc}")
            self.save_screenshot(f"element_not_found_{loc.replace('/', '_')}", "element_wait")
            raise Exception(f"Элемент не найден: {loc}")

    def click_element(self, loc, timeout=DRIVER_WAIT_EXPLICIT, retries=2):
        for attempt in range(retries):
            try:
                element = self.wait_element(loc, timeout)
                wait = WebDriverWait(TestIsales.driver, timeout)
                wait.until(EC.element_to_be_clickable((By.XPATH, loc)))
                element.click()
                return
            except (StaleElementReferenceException, TimeoutException) as e:
                if attempt == retries - 1:
                    logger.error(f"Не удалось нажать элемент после {retries} попыток: {loc}")
                    self.save_screenshot(f"click_failed_{loc.replace('/', '_')}", "element_click")
                    raise Exception(f"Не удалось нажать элемент после {retries} попыток: {loc}. Причина: {e}")
                logger.warning(f"Повтор {attempt + 1}/{retries} нажатия элемента: {loc}")
                time.sleep(1)

    def element_is_present(self, loc, timeout=DRIVER_WAIT_EXPLICIT):
        try:
            self.wait_element(loc, timeout)
            return True
        except (TimeoutException, WebDriverException):
            return False



    def open_site_with_retries(self, url, retries=3, wait=10):
        for attempt in range(retries):
            try:
                self.driver.get(url)
                time.sleep(wait)
                # Проверка: наличие элемента Header_freeCallLink
                if self.element_is_present("//*[contains(@class, 'Header_freeCallLink')]", timeout=5):
                    logger.info("Сайт успешно открыт")
                    return
            except Exception as e:
                logger.warning(f"Попытка {attempt+1} не удалась: {e}")
            time.sleep(3)
        self.save_screenshot('test_error_open_site', 'test_01_login')
        raise Exception("Не удалось открыть сайт после нескольких попыток")

    def test_01_login(self):
        """Тест процесса входа в систему"""
        self.open_site_with_retries(URL, retries=3, wait=10)

        try:

            accept_cookie_button = '//*[@id="root"]/div[2]/div/section/div/div[3]/div[1]/div/button'
            if self.element_is_present(accept_cookie_button, timeout=5):
                try:
                    cookie_button = self.driver.find_element(By.XPATH, accept_cookie_button)
                    self.driver.execute_script("arguments[0].click();", cookie_button)
                    logger.info("Нажатие на кнопку принятия cookie")
                    time.sleep(2)
                except Exception as e:
                    logger.error(f"Ошибка при нажатии на кнопку cookie: {e}")
                try:
                    actions = webdriver.ActionChains(self.driver)
                    actions.move_by_offset(10, 10).click().perform()
                    logger.info("Обход капчи")
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"Ошибка при обходе капчи: {e}")

            try:
                self.click_element("//div[contains(@class, 'FooterPublic_managerLogin')]")
                logger.info("Нажата кнопка входа для сотрудников")
                time.sleep(2)
            except Exception as e:
                logger.error(f"Ошибка при нажатии кнопки входа: {e}")
                self.save_screenshot('login_button_error', 'test_01_login')
                raise Exception(f"Ошибка при нажатии кнопки входа: {e}")

            # Проверяем наличие учетных данных
            if not ISALES_LOGIN or not ISALES_PASSWORD:
                raise ValueError("Учетные данные iSales не настроены. Установите переменные окружения ISALES_LOGIN и ISALES_PASSWORD")

            try:
                self.wait_element("//input[@id='username']").send_keys(ISALES_LOGIN)
                self.wait_element("//input[@id='password']").send_keys(ISALES_PASSWORD)
                self.click_element("//input[@type='submit']")
                logger.info("Введены учетные данные")
            except Exception as e:
                logger.error(f"Ошибка при вводе учетных данных: {e}")
                self.save_screenshot('credentials_error', 'test_01_login')
                raise Exception(f"Ошибка при вводе учетных данных: {e}")

            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'Header_headerInfoUser')]"))
                )
                logger.info("Вход выполнен успешно")
            except Exception as e:
                logger.error(f"Ошибка при проверке успешного входа: {e}")
                self.save_screenshot('login_verification_error', 'test_01_login')
                raise Exception(f"Ошибка при проверке успешного входа: {e}")

        except Exception as e:
            logger.error(f"Ошибка при выполнении теста: {e}")
            raise

    def test_02_select_points(self, route_data):
        try:
            self.wait_element('//div[@name="locationFrom"]//input').send_keys(route_data['from'])
            self.click_element('//li[contains(@id, "option")]')
        except Exception as e:
            logger.error(f"Ошибка при выборе терминала отправления: {e}")
            raise Exception(f"Ошибка при выборе терминала отправления: {e}")
        try:
            self.wait_element('//div[@name="locationTo"]//input').send_keys(route_data['to'])
            self.click_element('//li[contains(@id, "option")]')
        except Exception as e:
            logger.error(f"Ошибка при выборе терминала назначения: {e}")
            raise Exception(f"Ошибка при выборе терминала назначения: {e}")

    def test_03_select_cargo(self, cargo_data=None):
        if cargo_data is None:
            cargo_data = {'code': '123018', 'name': 'Тестовый груз 1'}
        try:
            self.wait_element('//input[@id="cargoEtsng"]').send_keys(cargo_data['code'])
            self.click_element('//li[contains(@id, "cargoEtsng-option")]')
        except Exception as e:
            logger.error(f"Ошибка при выборе груза {cargo_data['code']}: {e}")
            raise Exception(f"Ошибка при выборе груза: {e}")

    def test_04_select_contract(self, contract_data=None):
        if contract_data is None:
            contract_data = {'number': 'ТЕСТ-0000001', 'description': 'Тестовый контракт'}
        try:
            self.wait_element('//div[@class="contracts-search"]//input').send_keys(contract_data['number'])
            self.click_element('//div[@class="contracts-search"]//li')
        except Exception as e:
            logger.error(f"Ошибка при выборе контракта {contract_data['number']}: {e}")
            raise Exception(f"Ошибка при выборе контракта: {e}")

    def test_05_calculation(self):
        try:
            self.click_element("//div[contains(@class, 'CalculationButton_button')]//button")
            time.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка при запуске расчета стоимости: {e}")
            raise Exception(f"Ошибка при запуске расчета стоимости: {e}")

    def test_06_select_transport_solution(self):
        timeout = 240
        try:
            offer_button = self.wait_element("//div[contains(@class, 'Offer_buttonCol')]//button", timeout)
            draft_id = re.search(r'/private/current-draft/(\d+)', self.driver.current_url)
            if draft_id:
                self.add_info["draft_id"] = draft_id.group(1)
            offer_button.click()
        except Exception as e:
            logger.error(f"Ошибка при выборе транспортного решения: {e}")
            raise Exception(f"Ошибка при выборе транспортного решения: {e}")

    def test_07_update_services(self):
        try:
            if self.element_is_present("//div[contains(@class, 'online-chat-root')]", 5):
                chat_element = self.wait_element("//div[contains(@class, 'online-chat-root')]")
                self.driver.execute_script("arguments[0].remove()", chat_element)
            self.click_element("//div[contains(@class, 'services-last-block__button-save')]//button")
        except Exception as e:
            logger.error(f"Ошибка при сохранении услуг: {e}")
            raise Exception(f"Ошибка при сохранении услуг: {e}")

    @retry_on_exception(retries=2)
    def test_08_select_shipper_consignee(self):
        try:
            time.sleep(3)
            save_button_loc = "//div[contains(@class, 'AdditionalInfo_buttonSave')]//button"
            self.wait_element(save_button_loc)

            try:
                self.wait_element("((//div[contains(@class, 'AdditionalInfo_item')])[1]//input[@type])[1]").send_keys('Рога и копыта')
                time.sleep(5)
                self.click_element("//div[contains(@class, 'InputConsignor')]//li")
                time.sleep(2)
            except Exception as e:
                raise Exception(f"Ошибка при заполнении грузоотправителя: {e}")

            def check_value(xpath):
                element = self.wait_element(xpath)
                return bool(element.get_attribute('value'))

            try:
                WebDriverWait(self.driver, 10).until(
                    lambda x: check_value("((//div[contains(@class, 'AdditionalInfo_item')])[1]//input[@type])[2]")
                )
            except Exception as e:
                raise Exception(f"Ошибка проверки заполнения данных грузоотправителя: {e}")

            try:
                self.wait_element("((//div[contains(@class, 'AdditionalInfo_item')])[2]//input[@type])[1]").send_keys('Рога и копыта')
                time.sleep(5)
                self.click_element("//div[contains(@class, 'InputConsignor')]//li")
                time.sleep(2)
            except Exception as e:
                raise Exception(f"Ошибка при заполнении грузополучателя: {e}")

            try:
                WebDriverWait(self.driver, 10).until(
                    lambda x: check_value("((//div[contains(@class, 'AdditionalInfo_item')])[2]//input[@type])[2]")
                )
            except Exception as e:
                raise Exception(f"Ошибка проверки заполнения данных грузополучателя: {e}")

            try:
                self.click_element("((//div[contains(@class, 'AdditionalInfo_package')])[1]//input[@type])[1]")
                time.sleep(3)
                self.wait_element("//ul[contains(@class,'MuiAutocomplete-listbox')]")
                self.click_element("(//ul[contains(@class,'MuiAutocomplete-listbox')]//li[@role='option'])[1]")
                WebDriverWait(self.driver, 10).until(
                    lambda x: check_value("((//div[contains(@class, 'AdditionalInfo_package')])[1]//input[@type])[1]")
                )
            except Exception as e:
                raise Exception(f"Ошибка при выборе типа упаковки: {e}")

            try:
                self.click_element(save_button_loc)
            except Exception as e:
                raise Exception(f"Ошибка при сохранении дополнительных сведений: {e}")
        except Exception as e:
            logger.error(f"Ошибка при заполнении грузоотправителя/грузополучателя: {e}")
            raise

    @retry_on_exception(retries=2)
    def test_09_order_create(self):
        try:
            self.click_element("//div[contains(@class, 'ConfirmationInfo_saveButtonCol')]//button")
            self.wait.until(EC.visibility_of_element_located((By.XPATH, '//div[@class="view-order-pro"]')))
            order_id = re.search(r'/private/order/(\d+)', self.driver.current_url)
            if order_id:
                order_id_value = order_id.group(1)
                self.add_info["order_id"] = order_id_value
                if hasattr(self, 'test_result'):
                    self.test_result.set_order_id(order_id_value)
            else:
                raise Exception("Не удалось получить номер заказа из URL")
        except Exception as e:
            logger.error(f"Ошибка при создании заказа: {e}")
            raise Exception(f"Ошибка при создании заказа: {e}")

    def test_10_order_approval(self):
        try:
            self.driver.execute_script("window.scrollTo(0, 0);")
            self.click_element("//button[.='Утвердить' or .='Approve']", 60)
        except Exception as e:
            logger.error(f"Ошибка при утверждении заказа: {e}")
            raise Exception(f"Ошибка при утверждении заказа: {e}")

    def test_11_order_reserving_equipment_complete(self):
        try:
            wait = WebDriverWait(self.driver, 180)
            expected_texts = ['Резервирование оборудования', 'Reserving Equipment']
            def check_status(driver):
                elements = driver.find_elements(By.XPATH, "//div[@class='view-order-pro__status-line-price']//div[contains(@class, 'MuiStep-root')]//span[contains(@class, 'Mui-completed')]//div[@class='line-pro__title']")
                texts = [el.text for el in elements]
                return any(text in texts for text in expected_texts)
            wait.until(check_status)
        except TimeoutException:
            raise Exception("Ошибка при ожидании статуса 'Резервирование оборудования': превышено время ожидания")
        except Exception as e:
            logger.error(f"Ошибка при ожидании статуса 'Резервирование оборудования': {e}")
            raise Exception(f"Ошибка при ожидании статуса 'Резервирование оборудования': {e}")

    def test_12_order_need_pay_status(self):
        try:
            wait = WebDriverWait(self.driver, 180)
            expected_texts = ['Требуется оплата', 'Payment is required']
            def check_payment_status(driver):
                element = self.wait_element("//div[@class='view-order-pro__status-line-price']//div[contains(@class, 'MuiStep-root')]//span[contains(@class, 'Mui-active')]//div[@class='line-pro__title']")
                return any(text in element.text for text in expected_texts)
            wait.until(check_payment_status)
        except TimeoutException:
            raise Exception("Ошибка при ожидании статуса 'Требуется оплата': превышено время ожидания")
        except Exception as e:
            logger.error(f"Ошибка при ожидании статуса 'Требуется оплата': {e}")
            raise Exception(f"Ошибка при ожидании статуса 'Требуется оплата': {e}")

    def test_13_order_documents(self):
        try:
            time.sleep(5)
            self.driver.refresh()
            self.click_element("//div[contains(@class, 'parts-view-order-pro')]/a[6]")
            self.click_element("//div[contains(@class, 'DocumentsBlock_listItem')]")
        except Exception as e:
            logger.error(f"Ошибка при открытии документов заказа: {e}")
            raise Exception(f"Ошибка при открытии документов заказа: {e}")

    def test_14_order_cancel(self):
        try:
            wait = WebDriverWait(self.driver, 180)
            expected_texts = ['Ваш заказ отменен', 'Your order has been cancelled']
            self.click_element("//div[contains(@class, 'ActionsBlock_cancelPro')]")
            time.sleep(2)
            self.click_element("//div[text()='Другая причина']")
            time.sleep(1)
            try:
                self.driver.find_element(By.XPATH, "//textarea[contains(@class, 'MuiInputBase-input')]").send_keys("TEST")
            except Exception as e:
                raise Exception(f"Ошибка при вводе причины отмены: {e}")
            time.sleep(1)
            self.click_element("//button[contains(@class, 'MuiButton-containedPrimary') and .//text()='Подтвердить']")
            try:
                def check_cancel_status(driver):
                    element = self.wait_element("//div[@class='view-order-pro__status-line-price']//div[contains(@class,'pro-view-status-block')]//div[contains(@class,'OrderRejectBlock_statusTitle')]")
                    return any(text in element.text for text in expected_texts)
                wait.until(check_cancel_status)
            except TimeoutException:
                raise Exception("Превышено время ожидания статуса 'Ваш заказ отменен'")
        except Exception as e:
            logger.error(f"Ошибка при отмене заказа: {e}")
            raise Exception(f"Ошибка при отмене заказа: {e}")

def log_test_result(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        self = args[0]
        test_name = func.__name__

        # Инициализируем результат теста
        if not hasattr(self, 'test_result'):
            self.test_result = TestResult()

        # Начинаем отслеживание шага
        step_start_time = datetime.now()
        step_start_timestamp = time.time()

        logger.info(f"Запуск теста: {test_name}")

        try:
            result = func(*args, **kwargs)
            logger.info(f"Тест {test_name} выполнен успешно")

            # Завершаем шаг успешно
            step_end_time = datetime.now()
            step_end_timestamp = time.time()
            step_duration = step_end_timestamp - step_start_timestamp

            self.test_result.add_step(
                step_name=test_name,
                status="1",
                timing={
                    "start_time": step_start_time.isoformat(),
                    "start_timestamp": step_start_timestamp,
                    "end_time": step_end_time.isoformat(),
                    "end_timestamp": step_end_timestamp,
                    "duration_seconds": round(step_duration, 2)
                }
            )

            return result

        except Exception as e:
            logger.error(f"Тест {test_name} завершился с ошибкой: {e}")

            # Завершаем шаг с ошибкой
            step_end_time = datetime.now()
            step_end_timestamp = time.time()
            step_duration = step_end_timestamp - step_start_timestamp

            # Сохраняем скриншот
            screenshot_bytes = None
            try:
                screenshot_bytes = self.driver.get_screenshot_as_png()

                # Сохраняем локально как фиксированное имя и предварительно удаляем существующий файл
                try:
                    ensure_screenshots_dir()
                    filename = "test_isales_error.png"
                    filepath = os.path.join(SCREENSHOTS_DIR, filename)
                    try:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                            logger.info(f"Удален старый файл скриншота: {filepath}")
                    except Exception as save_rm_err:
                        logger.warning(f"Не удалось удалить старый скриншот {filepath}: {save_rm_err}")
                    with open(filepath, 'wb') as f:
                        f.write(screenshot_bytes)
                    logger.info(f"Screenshot saved to: {filepath}")
                except Exception as save_error:
                    logger.error(f"Failed to save screenshot locally: {save_error}")

                # Отправляем в Telegram
                self.send_screenshot_to_telegram(screenshot_bytes, test_name)
            except Exception as screenshot_error:
                logger.error(f"Ошибка при сохранении скриншота: {screenshot_error}")

            self.test_result.add_step(
                step_name=test_name,
                status="0",
                timing={
                    "start_time": step_start_time.isoformat(),
                    "start_timestamp": step_start_timestamp,
                    "end_time": step_end_time.isoformat(),
                    "end_timestamp": step_end_timestamp,
                    "duration_seconds": round(step_duration, 2)
                }
            )

            # Устанавливаем скриншот в результат
            if screenshot_bytes:
                self.test_result.set_screenshot(screenshot_bytes)

            # Сохраняем сообщение и ошибку с указанием шага
            try:
                step_num = None
                if test_name and test_name.startswith('test_'):
                    parts = test_name.split('_')
                    if len(parts) > 1 and parts[1].isdigit():
                        step_num = int(parts[1])
                if step_num is not None:
                    self.test_result.message = f"Тест завершился с ошибкой на шаге {step_num}"
                else:
                    self.test_result.message = "Тест завершился с ошибкой"
            except Exception:
                self.test_result.message = "Тест завершился с ошибкой"
            try:
                self.test_result.error = str(e)
            except Exception:
                self.test_result.error = ""

            raise
    return wrapper

# Применяем декоратор к каждой test_ функции
for name, method in TestIsales.__dict__.items():
    if name.startswith('test_'):
        setattr(TestIsales, name, log_test_result(method))

def run_test_cycle():
    """Запуск одного цикла тестов"""

        # Инициализация тестового класса и WebDriver
    test = TestIsales()
    test.driver = None
    # Инициализируем результат тестирования как можно раньше, чтобы в случае
    # ранних ошибок (например, при подключении к Selenium Grid) мы всё равно
    # смогли вывести корректный JSON
    if not hasattr(test, 'test_result'):
        test.test_result = TestResult()

    try:
        # Инициализация WebDriver через Selenium Grid или локально
        chrome_options = Options()
        chrome_options.add_argument('--window-size=1920,1080')
        if USE_SELENIUM_GRID:
            # Проверка Selenium Grid настроек и соединения
            try:
                if not SELENIUM_GRID_URL:
                    raise Exception("Ошибка подключения к Selenium Grid: не задан SELENIUM_GRID_URL.")
                test.driver = webdriver.Remote(
                    command_executor=SELENIUM_GRID_URL,
                    options=chrome_options
                )
            except Exception:
                raise Exception("Ошибка подключения к Selenium Grid: не удалось установить соединение с удалённым драйвером.")
        else:
            test.driver = webdriver.Chrome(options=chrome_options)
        test.wait = WebDriverWait(test.driver, DRIVER_WAIT_EXPLICIT)
        TestIsales.driver = test.driver  # Set class-level driver
        TestIsales.wait = test.wait      # Set class-level wait

        # Инициализируем результат тестирования (уже инициализирован выше как подстраховка)
        if not hasattr(test, 'test_result'):
            test.test_result = TestResult()

        # Тестовые данные
        routes = [
            {'from': 'Кунцево 2', 'to': 'Клещиха'},
            {'from': 'Шушары, терминал АО Логистика-Терминал, Санкт-Петербург, РОССИЯ', 'to': 'Екатеринбург-Товарный, терминал ТК, Екатеринбург, РОССИЯ'},
            {'from': 'Хабаровск 2, терминал ТК, Хабаровск, РОССИЯ', 'to': 'Купавна, терминал ООО "Контейнерный терминал Купавна", Москва, РОССИЯ'},
            {'from': 'Угловая, подъездной путь ООО "Дальневосточная юридическая компания Авеста", Владивосток, РОССИЯ', 'to': 'Челябинск-Грузовой, терминал ТК, Челябинск, РОССИЯ'},
            {'from': 'Столбовая, подъездной путь АО АК ЖДЯ, Столбовая, РОССИЯ', 'to': 'Нижний Бестях, терминал ЖДЯ, Якутск, РОССИЯ'},
            {'from': 'Селятино, подъездной путь ООО СТС-Логистика, Москва, РОССИЯ', 'to': 'Блочная, терминал ТК, Пермь, РОССИЯ'},
            {'from': 'Селятино, подъездной путь АО "Славтранс-Сервис", Москва, РОССИЯ', 'to': 'Угольная, терминал АО "Пасифик Интермодал Контейнер", Владивосток, РОССИЯ'},
            {'from': 'Петропавловск-Камчатский, порт, Петропавловск-Камчатский, РОССИЯ', 'to': 'Ростов-Товарный, терминал ТК, Ростов-на-Дону, РОССИЯ'},
            {'from': 'Первая Речка, терминал ТК, Владивосток, РОССИЯ', 'to': 'Базаиха, терминал ТК, Красноярск, РОССИЯ'},
            {'from': 'Осенцы, подъездной путь ООО "ЛУКОЙЛ-Пермнефтеоргсинтез", Пермь, РОССИЯ', 'to': 'Ростов-Товарный, терминал ТК, Ростов-на-Дону, РОССИЯ'},
            {'from': 'Нижний Бестях, терминал ЖДЯ, Якутск, РОССИЯ', 'to': 'Петропавловск-Камчатский, порт, Петропавловск-Камчатский, РОССИЯ'},
            {'from': 'Находка (Восточный), порт, Находка, РОССИЯ', 'to': 'Челябинск-Грузовой, терминал ТК, Челябинск, РОССИЯ'},
            {'from': 'Находка (Восточный), порт, Находка, РОССИЯ', 'to': 'Селятино, подъездной путь ООО СТС-Логистика, Москва, РОССИЯ'},
            {'from': 'Находка (Восточный), порт, Находка, РОССИЯ', 'to': 'Оренбург, терминал РЖД, Оренбург, РОССИЯ'},
            {'from': 'Находка (Восточный), порт, Находка, РОССИЯ', 'to': 'Купавна, терминал ООО "Контейнерный терминал Купавна", Москва, РОССИЯ'},
            {'from': 'Находка (Восточный), порт, Находка, РОССИЯ', 'to': 'Костариха, терминал ТК, Нижний Новгород, РОССИЯ'},
            {'from': 'Находка (Восточный), порт, Находка, РОССИЯ', 'to': 'Екатеринбург-Товарный, терминал ТК, Екатеринбург, РОССИЯ'},
            {'from': 'Кресты, терминал ОАО Мосагронаучприбор, Москва, РОССИЯ', 'to': 'Магадан, порт, Магадан, РОССИЯ'},
            {'from': 'Комсомольск-на-Амуре, терминал РЖД, Комсомольск-на-Амуре, РОССИЯ', 'to': 'Санкт-Петербург-Финляндский, терминал РЖД, Санкт-Петербург, РОССИЯ'},
            {'from': 'Комсомольск-на-Амуре, терминал РЖД, Комсомольск-на-Амуре, РОССИЯ', 'to': 'Пыть-Ях, подъездной путь ООО "НТС-Лидер", Пыть-Ях, РОССИЯ'},
            {'from': 'Комсомольск-на-Амуре, терминал РЖД, Комсомольск-на-Амуре, РОССИЯ', 'to': 'Придача, терминал ТК, Воронеж, РОССИЯ'},
            {'from': 'Качалино, терминал РЖД, Волгоград, РОССИЯ', 'to': 'Клещиха, терминал ТК, Новосибирск, РОССИЯ'},
            {'from': 'Ворсино, терминал ТК, Обнинск, РОССИЯ', 'to': 'Хабаровск 2, терминал ТК, Хабаровск, РОССИЯ'},
            {'from': 'Базаиха, терминал ТК, Красноярск, РОССИЯ', 'to': 'Безымянка, терминал РЖД, Самара, РОССИЯ'},
        ]
        route_data = random.choice(routes)
        # Сохраняем выбранный маршрут в результат теста для JSON как строку "откуда - куда"
        if hasattr(test, 'test_result'):
            try:
                test.test_result.selected_route = f"{route_data['from']} - {route_data['to']}"
            except Exception:
                # На случай, если структура данных будет иной
                test.test_result.selected_route = str(route_data)
        cargo_data = {'code': '123018', 'name': 'Тестовый груз 1'}
        contract_data = {'number': 'ТЕСТ-0000001', 'description': 'Тестовый контракт'}

        # Запуск всех тестов последовательно
        tests = [
            (test.test_01_login, {}),
            (test.test_02_select_points, {'route_data': route_data}),
            (test.test_03_select_cargo, {'cargo_data': cargo_data}),
            (test.test_04_select_contract, {'contract_data': contract_data}),
            (test.test_05_calculation, {}),
            (test.test_06_select_transport_solution, {}),
            (test.test_07_update_services, {}),
            (test.test_08_select_shipper_consignee, {}),
            (test.test_09_order_create, {}),
            (test.test_10_order_approval, {}),
            (test.test_11_order_reserving_equipment_complete, {}),
            (test.test_12_order_need_pay_status, {}),
            (test.test_13_order_documents, {}),
            (test.test_14_order_cancel, {})
        ]

        success_count = 0
        total_tests = len(tests)

        for i, (test_func, test_kwargs) in enumerate(tests, 1):
            try:
                logging.info(f"Запуск теста {i}/{total_tests}: {test_func.__name__}")
                test_func(**test_kwargs)
                logging.info(f"Тест {test_func.__name__} успешно завершён")
                success_count += 1
                time.sleep(2)  # Небольшая пауза между тестами
            except Exception as e:
                logging.error(f"Тест {test_func.__name__} завершился с ошибкой: {str(e)}")
                # Останавливаем выполнение при ошибке в любом тесте
                raise

        # Завершаем результат тестирования
        all_success = success_count == total_tests
        total_duration_seconds = time.time() - test.test_result.start_timestamp
        message = (
            f"Тест выполнен успешно: {success_count}/{total_tests} шагов за {total_duration_seconds:.2f} сек"
            if all_success else
            f"Тест завершен: {success_count}/{total_tests} шагов за {total_duration_seconds:.2f} сек"
        )

        test.test_result.finalize(
            success=all_success,
            message=message,
            error=None if all_success else f"Ошибки в {total_tests - success_count} тестах"
        )

        # Печатаем JSON для элемента Zabbix
        json_data = test.test_result.to_dict()
        print(json.dumps(json_data, indent=2, ensure_ascii=False))

        logging.info("Все тесты выполнены!")
        return all_success

    except Exception as e:
        logging.error(f"Цикл тестов завершился с ошибкой: {str(e)}")

        # Гарантируем наличие test_result
        if not hasattr(test, 'test_result') or test.test_result is None:
            test.test_result = TestResult()

        # Формируем ошибку и пробуем сделать скриншот
        err_text = str(e)
        try:
            if getattr(test, 'driver', None):
                screenshot_bytes = test.driver.get_screenshot_as_png()
                test.test_result.set_screenshot(screenshot_bytes)
        except Exception:
            pass

        # Если декоратор уже сохранил сообщение/ошибку (в т.ч. номер шага), используем их
        final_message = test.test_result.message or "Тест завершился с ошибкой"
        final_error = test.test_result.error or err_text

        test.test_result.finalize(
            success=False,
            message=final_message,
            error=final_error,
        )
        json_data = test.test_result.to_dict()
        print(json.dumps(json_data, indent=2, ensure_ascii=False))

        return False

    finally:
        # Очистка ресурсов
        driver_ref = getattr(test, 'driver', None)
        if driver_ref:
            try:
                driver_ref.quit()
                logging.info("WebDriver успешно закрыт")
            except Exception as quit_err:
                logging.warning(f"Ошибка при закрытии WebDriver: {quit_err}")

if __name__ == "__main__":
    import sys
    # Одноразовый запуск для интеграции с Zabbix
    logging.info("Одноразовый запуск тестового сценария")
    success = run_test_cycle()
    sys.exit(0 if success else 1)
