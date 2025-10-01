#!/usr/lib64/zabbix7-lts/externalscripts/myenv/bin/python3
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

DEFAULT_URL = "https://itrans.trcont.ru/"

# Единые таймауты ожиданий
DEFAULT_WAIT = 10
CLICK_WAIT = 10
VISIBILITY_WAIT = 10

# Базовый CSS-селектор для видимых алертов внутри контейнера ошибок
# Дальше фильтруем по классу/тексту, чтобы определить именно ошибку
ERROR_CONTAINER_VISIBLE_ALERTS_SELECTOR = ".errors-container .alert.show"

# Путь к .env и загрузка переменных, как в test_isales.py
env_path = Path(__file__).with_name("_env")
load_dotenv(dotenv_path=env_path)
load_dotenv()

# Переключатель для локального запуска (как в test_isales.py)
USE_SELENIUM_GRID = True  # False — локально, True — через Grid

# Данные Selenium Grid (как в test_isales.py)
SELENIUM_GRID_LOGIN = os.getenv('SELENIUM_GRID_LOGIN')
SELENIUM_GRID_PASSWORD = os.getenv('SELENIUM_GRID_PASSWORD')

SELENIUM_GRID_URL = f"http://{SELENIUM_GRID_LOGIN}:{SELENIUM_GRID_PASSWORD}@172.18.65.116:4444/wd/hub"

class TestResult:
    def __init__(self):
        self.start_time = time.time()
        self.steps: Dict[str, Dict[str, Any]] = {}
        self.success = True
        self.status = "1"
        self.message = ""
        self.error: Optional[str] = None
        self.screenshot: Optional[str] = None
        self.test_info: Dict[str, Any] = {}

    def add_step(self, step_name: str, status: str = "1", duration_seconds: float = 0.0):
        self.steps[step_name] = {
            "status": status,
            "timing": {
                "duration_seconds": round(duration_seconds, 2)
            }
        }
        if status == "0":
            self.success = False
            self.status = "0"

    def set_screenshot_b64(self, b64: str) -> None:
        self.screenshot = b64

    def finalize(self, success: bool, message: str, error: Optional[str] = None):
        end = time.time()
        self.success = success
        self.status = "1" if success else "0"
        self.message = message
        self.error = error
        self.test_info = {
            "total_duration": round(end - self.start_time, 2)
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "test_info": self.test_info,
            "steps": self.steps,
            "screenshot": self.screenshot,
        }


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip()


def build_driver() -> WebDriver:
    chrome_options = Options()
    chrome_options.add_argument('--window-size=1920,1080')
    if USE_SELENIUM_GRID:
        try:
            if not SELENIUM_GRID_URL:
                raise Exception("Ошибка подключения к Selenium Grid: не задан SELENIUM_GRID_URL.")
            return webdriver.Remote(
                command_executor=SELENIUM_GRID_URL,
                options=chrome_options,
            )
        except Exception:
            raise Exception(
                "Ошибка подключения к Selenium Grid: не удалось установить соединение с удалённым драйвером."
            )
    else:
        return webdriver.Chrome(options=chrome_options)


# Функция wait_for не используется — удалена


def get_screenshot_b64(driver: WebDriver) -> str:
    # Keep screenshot only in memory
    png_bytes = driver.get_screenshot_as_png()
    return base64.b64encode(png_bytes).decode("ascii")


class ItransTest:
    def __init__(self):
        self.driver: Optional[WebDriver] = None
        self.wait: Optional[WebDriverWait] = None
        self.check_error_banner: bool = True

    def raise_if_error_banner(self) -> None:
        """Если на странице появился баннер ошибки — бросаем исключение."""
        if not self.check_error_banner:
            return
        if not self.driver:
            return
        try:
            alerts = self.driver.find_elements(By.CSS_SELECTOR, ERROR_CONTAINER_VISIBLE_ALERTS_SELECTOR)
            visible_alerts = [el for el in alerts if el.is_displayed()]
            for alert in visible_alerts:
                classes = (alert.get_attribute("class") or "").lower()
                text = (alert.text or "").strip()
                is_error_by_class = "alert-error" in classes
                # Подстрахуемся по тексту — иногда класс может меняться, а текст остаётся
                is_error_by_text = ("ошибка" in text.lower()) or ("произошла ошибка" in text.lower())
                if is_error_by_class or is_error_by_text:
                    raise Exception(f"Обнаружено сообщение об ошибке на странице: {text}")
        except WebDriverException:
            # Не ломаем логику при временных сбоях драйвера на find_elements
            pass

    def wait_element(self, xpath: str, timeout: int = DEFAULT_WAIT):
        try:
            wait = (
                self.wait
                if timeout == DEFAULT_WAIT and self.wait is not None
                else WebDriverWait(self.driver, timeout)
            )
            element = wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
            # Проверяем, не появился ли баннер ошибки
            self.raise_if_error_banner()
            return element
        except TimeoutException:
            # В первую очередь проверим, не появился ли баннер ошибки
            self.raise_if_error_banner()
            raise Exception(f"Ошибка при поиске элемента Локатор = {xpath}.")

    def click_element(self, xpath: str, timeout: int = CLICK_WAIT, retries: int = 2, description: Optional[str] = None):
        for attempt in range(retries):
            try:
                element = self.wait_element(xpath, timeout)
                # Сначала видимость, затем кликабельность — стабильнее
                WebDriverWait(self.driver, timeout).until(EC.visibility_of_element_located((By.XPATH, xpath)))
                WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                try:
                    element.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", element)
                # Проверяем, не появился ли баннер ошибки сразу после клика
                self.raise_if_error_banner()
                return
            except (StaleElementReferenceException, TimeoutException):
                if attempt == retries - 1:
                    # Формат требуемого сообщения об ошибке при клике
                    title = description or ""
                    title_part = f' "{title}"' if title else ""
                    raise Exception(f"Ошибка при нажатии на кнопку{title_part} Локатор = {xpath}.")
                time.sleep(1)

    def element_is_present(self, xpath: str, timeout: int = DEFAULT_WAIT) -> bool:
        """Проверяет наличие элемента на странице"""
        try:
            WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))
            # Если нашли элемент — параллельно проверим отсутствие баннера ошибки
            self.raise_if_error_banner()
            return True
        except TimeoutException:
            # Даже при отсутствии искомого элемента проверим баннер ошибки
            self.raise_if_error_banner()
            return False

    def wait_visible_by_xpath(self, xpath: str, timeout: int = VISIBILITY_WAIT, description: Optional[str] = None):
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.visibility_of_element_located((By.XPATH, xpath))
            )
            self.raise_if_error_banner()
        except TimeoutException:
            # Формат требуемого сообщения об ошибке при ожидании видимости текста
            title = description or ""
            title_part = f' "{title}"' if title else ""
            raise Exception(
                f"Ошибка при проверке видимости надписи{title_part} Локатор = {xpath}."
            )


def send_telegram_alert(
    token: Optional[str],
    chat_id: Optional[str],
    text: str,
    screenshot_b64: Optional[str] = None,
) -> None:
    """Отправка уведомления в Telegram. Не бросает исключения наружу.

    Поведение при отладке управляется переменной окружения TELEGRAM_DEBUG=true/1.
    """
    debug = (os.getenv("TELEGRAM_DEBUG", "").lower() in ("1", "true", "yes"))
    if not token or not chat_id:
        if debug:
            print("[telegram] пропуск: не задан TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID", file=sys.stderr)
        return

    # Ограничим длину текста для caption (Telegram: до ~1024 символов для фото)
    safe_caption = (text or "")
    if screenshot_b64 and len(safe_caption) > 1000:
        safe_caption = safe_caption[:1000] + "…"

    api_url = f"https://api.telegram.org/bot{token}"
    try:
        if screenshot_b64:
            # Send as photo with caption
            photo_bytes = base64.b64decode(screenshot_b64)
            files = {"photo": ("error.png", photo_bytes, "image/png")}
            data = {"chat_id": chat_id, "caption": safe_caption}
            resp = requests.post(f"{api_url}/sendPhoto", data=data, files=files, timeout=20)
        else:
            payload = {"chat_id": chat_id, "text": text}
            resp = requests.post(f"{api_url}/sendMessage", json=payload, timeout=20)

        if not resp.ok:
            if debug:
                try:
                    body = resp.text
                except Exception:
                    body = "<no body>"
                print(f"[telegram] ошибка HTTP {resp.status_code}: {body}", file=sys.stderr)
    except Exception as e:
        # Avoid breaking the run due to alert failures
        if debug:
            print(f"[telegram] исключение при отправке: {e}", file=sys.stderr)
        pass


def step_01_open_and_login(test: ItransTest, base_url: str, username: str, password: str) -> None:
    d = test.driver
    # Предварительно проверим доступность URL по HTTP (ожидаем код 200)
    try:
        resp = requests.get(base_url, timeout=DEFAULT_WAIT)
        if resp.status_code != 200:
            raise Exception(f"Ошибка при открытии сайта: HTTP статус = {resp.status_code}")
    except Exception:
        raise Exception("Ошибка при открытии сайта: не удалось получить HTTP 200 от сервера")

    d.get(base_url)
    # Ждём полной загрузки документа
    WebDriverWait(d, DEFAULT_WAIT).until(lambda drv: drv.execute_script("return document.readyState") == "complete")
    time.sleep(3)
    # Проверяем, что открыли нужный домен
    try:
        current_url = (d.current_url or "").lower()
        if "itrans.trcont.ru" not in current_url:
            raise Exception(f"Ошибка при открытии сайта: текущий URL = {d.current_url}")
    except Exception:
        raise Exception(f"Ошибка при открытии сайта: текущий URL = {d.current_url}")

    # Нажатие кнопки "Модуль управления ЖД плечом"
    test.click_element("//*[@id='app']/div/div[3]/a[2]/span", timeout=30)

    # Ввод логина/пароля
    test.wait_element("//input[@id='username']", timeout=20).send_keys(username)
    test.wait_element("//input[@id='password']", timeout=20).send_keys(password)
    test.click_element("//input[@type='submit']", timeout=CLICK_WAIT)

    # Проверка успешного входа — наличие вкладки "Модуль управления ЖД плечом"
    WebDriverWait(d, VISIBILITY_WAIT).until(
        EC.presence_of_element_located((By.XPATH, "//*[@id='journals']/button/span"))
    )


def step_02_open_rail_module(test: ItransTest) -> None:
   # Если есть кнопка "Обновить" — нажать
    try:
        test.click_element("//*[@id='app']/div[4]/div/div[2]/a[1]", timeout=5, description="Обновить")
        time.sleep(1)
    except Exception:
        pass

    # Нажать кнопку для раскрытия меню
    test.click_element("//*[@id='app']/div[4]/div/div[1]/span", timeout=CLICK_WAIT)
    time.sleep(5)

    # Проверяем наличие вкладки "Модуль управления ЖД плечом"
    tab_xpath = "//*[@id='journals']/button/span"
    if not test.element_is_present(tab_xpath, timeout=DEFAULT_WAIT):
        raise Exception("Вкладка 'Модуль управления ЖД плечом' не найдена на странице")

    # Открыть вкладку "Модуль управления ЖД плечом"
    test.click_element(tab_xpath, timeout=CLICK_WAIT, description="Модуль управления ЖД плечом")
    time.sleep(2)

    # Подвкладки и ожидаемые надписи
    subtabs = [
        ("//*[@id='journals-collapse']/ul/li[3]/a", "Журнал накладных ЭТРАН", "№ накладной"),
        ("//*[@id='journals-collapse']/ul/li[4]/a", "Журнал инструкций грузоотправителю", "№ заказа"),
        (
            "//*[@id='journals-collapse']/ul/li[6]/a",
            "Бестелеграммная технология при транзитных перевозках",
            "№ заказа",
        ),
        ("//*[@id='journals-collapse']/ul/li[7]/a", "Журнал заказов", "Заказ"),
        (
            "//*[@id='journals-collapse']/ul/li[8]/a",
            "Журнал несоответствия заказов и накладных",
            "Заказ",
        ),
        (
            "//*[@id='journals-collapse']/ul/li[9]/a",
            "Журнал контроля устранения несоответствий заказов и накладных",
            "Всего по ЖД",
        ),
        (
            "//*[@id='journals-collapse']/ul/li[10]/a",
            "Журнал памяток приемосдатчика (ГУ-45)",
            "Номер памятки",
        ),
        (
            "//*[@id='journals-collapse']/ul/li[11]/a",
            "Журнал Отправок в Заявке на перевозку ф. ГУ-12",
            "Состояние заявки",
        ),
        (
            "//*[@id='journals-collapse']/ul/li[12]/a",
            "Журнал заявок на перевозку ф. ГУ-12",
            "Состояние заявки",
        ),
        ("//*[@id='journals-collapse']/ul/li[14]/a", "Журнал актов АОФ", "Номер АОФ"),
        ("//*[@id='journals-collapse']/ul/li[15]/a", "Поезд", "Номер поезда"),
    ]
    # Проверка наличия подвкладок
    missing_tabs = []
    for _xpath, tab_name, _expected in subtabs:
        try:
            WebDriverWait(test.driver, VISIBILITY_WAIT).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")

    for xpath, title, expected_text in subtabs:
        test.click_element(xpath, timeout=CLICK_WAIT, description=title)
        time.sleep(2)
        test.wait_visible_by_xpath(
            f"//*[contains(normalize-space(.), '{expected_text}')]",
            timeout=VISIBILITY_WAIT,
            description=expected_text,
        )
        test.raise_if_error_banner()


def step_03_open_reports_module(test: ItransTest) -> None:
    # Вкладка: Отчеты
    tab_xpath = "//*[@id='reports']/button/span"
    if not test.element_is_present(tab_xpath, timeout=DEFAULT_WAIT):
        raise Exception("Вкладка 'Отчеты' не найдена на странице")
    test.click_element(tab_xpath, timeout=CLICK_WAIT, description="Отчеты")
    time.sleep(2)

    # Подвкладки и ожидаемые надписи
    subtabs = [
        (
            "//*[@id='reports-collapse']/ul/li[1]/a",
            "Отчет об объемах и стоимости услуг за месяц",
            "Номер контейнера",
        ),
        (
            "//*[@id='reports-collapse']/ul/li[2]/a",
            "Отчет об объемах и стоимости услуг за месяц Забайкальск (по ГУ-45)",
            "Номер контейнера",
        ),
        (
            "//*[@id='reports-collapse']/ul/li[3]/a",
            "Отчет об объемах и стоимости услуг за месяц Забайкальск (по ЖДН)",
            "Номер контейнера",
        ),
        (
            "//*[@id='reports-collapse']/ul/li[7]/a",
            "Отчет по формированию данных о поездах, следующих по договорной нитке графика",
            "Отчет по формированию данных о поездах, следующих по договорной нитке графика",
        ),
        (
            "//*[@id='reports-collapse']/ul/li[8]/a",
            "Отчет о сторнированных жд накладных со снятием тарифной отметки 05 (КП)",
            "Все поля таблицы",
        ),
    ]
    # Проверка наличия подвкладок
    missing_tabs = []
    for _xpath, tab_name, _expected in subtabs:
        try:
            WebDriverWait(test.driver, VISIBILITY_WAIT).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")

    for xpath, title, expected_text in subtabs:
        test.click_element(xpath, timeout=CLICK_WAIT, description=title)
        time.sleep(2)
        test.wait_visible_by_xpath(
            f"//*[contains(normalize-space(.), '{expected_text}')]",
            timeout=VISIBILITY_WAIT,
            description=expected_text,
        )
        test.raise_if_error_banner()


def step_04_open_directory_module(test: ItransTest) -> None:
    # Вкладка: Справочники
    tab_xpath = "//*[@id='directory']/button/span"
    if not test.element_is_present(tab_xpath, timeout=DEFAULT_WAIT):
        raise Exception("Вкладка 'Справочники' не найдена на странице")
    test.click_element(tab_xpath, timeout=CLICK_WAIT, description="Справочники")
    time.sleep(2)

    # Подвкладки и ожидаемые надписи
    subtabs = [
        ("//*[@id='directory-collapse']/ul/li[1]/a", "Справочники ЭТРАН", "Аварийные карты"),
        ("//*[@id='directory-collapse']/ul/li[2]/a", "Справочники ТрансКонтейнер", "Справочник владельцев оборудования"),
        ("//*[@id='directory-collapse']/ul/li[4]/a", "Тарифы по услугам", "Периоды для тарифа"),
        ("//*[@id='directory-collapse']/ul/li[5]/a", "Менеджеры-исполнители перевозки", "Соисполнитель"),
        ("//*[@id='directory-collapse']/ul/li[6]/a", "Менеджеры ТК", "Менеджер"),
        (
            "//*[@id='directory-collapse']/ul/li[8]/a",
            "Специальные отметки следования оборудования в поезде в ЖДН",
            "Идентификатор условия перевозки",
        ),
        ("//*[@id='directory-collapse']/ul/li[9]/a", "Справочник номеров документов", "Класс документа"),
        ("//*[@id='directory-collapse']/ul/li[13]/a", "Справочник станций эквивалентов", "Наименование станции"),
    ]
    # Проверка наличия подвкладок
    missing_tabs = []
    for _xpath, tab_name, _expected in subtabs:
        try:
            WebDriverWait(test.driver, VISIBILITY_WAIT).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")

    for xpath, title, expected_text in subtabs:
        test.click_element(xpath, timeout=CLICK_WAIT, description=title)
        time.sleep(2)
        test.wait_visible_by_xpath(
            f"//*[contains(normalize-space(.), '{expected_text}')]",
            timeout=VISIBILITY_WAIT,
            description=expected_text,
        )
        test.raise_if_error_banner()


def step_05_open_unified_window_module(test: ItransTest) -> None:
    # Вкладка: Единое окно
    tab_xpath = "//*[@id='ed_okno']/button/span"
    if not test.element_is_present(tab_xpath, timeout=DEFAULT_WAIT):
        raise Exception("Вкладка 'Единое окно' не найдена на странице")
    test.click_element(tab_xpath, timeout=CLICK_WAIT, description="Единое окно")
    time.sleep(2)

    # Подвкладки и ожидаемые надписи
    subtabs = [
        ("//*[@id='ed_okno-collapse']/ul/li[1]", "Дислокация контейнеров", "Список КТК"),
        ("//*[@id='ed_okno-collapse']/ul/li[2]", "Дислокация вагонов", "Список КТК"),
        (
            "//*[@id='ed_okno-collapse']/ul/li[3]",
            "Предоставление данных о дислокации вагонов на иностранных территориях",
            "Список вагонов",
        ),
        ("//*[@id='ed_okno-collapse']/ul/li[4]", "Натурные листы поездов", "Индекс поезда"),
        ("//*[@id='ed_okno-collapse']/ul/li[5]", "Последний переход вагона межгосударственных стыков", "Индекс поезда"),
        (
            "//*[@id='ed_okno-collapse']/ul/li[6]",
            "Последний переход контейнера межгосударственных стыков",
            "Список КТК",
        ),
        (
            "//*[@id='ed_okno-collapse']/ul/li[7]",
            "О переходе контейнера межгосударственных и международных стыковых пунктов, припортовых станций и станций передачи в/из “третьих стран”",
            "Список КТК",
        ),
        (
            "//*[@id='ed_okno-collapse']/ul/li[8]",
            "О сроках временного ввоза и номерах таможенных деклараций на транспортные средства",
            "Список КТК",
        ),
        (
            "//*[@id='ed_okno-collapse']/ul/li[9]",
            "О результатах осмотра вагонов перед погрузкой (по данным формы ВУ-14МВЦ)",
            "Список вагонов",
        ),
        ("//*[@id='ed_okno-collapse']/ul/li[10]", "Журнал «О техническом паспорте вагона (полный)»", "Список вагонов"),
        (
            "//*[@id='ed_okno-collapse']/ul/li[11]",
            "Об истории изменения данных по собственности, аренде, оперировании по доверенности вагонов",
            "Список вагонов",
        ),
        ("//*[@id='ed_okno-collapse']/ul/li[12]", "Журнал «О техническом состоянии вагона (полный)»", "Список вагонов"),
    ]
    # Проверка наличия подвкладок
    missing_tabs = []
    for _xpath, tab_name, _expected in subtabs:
        try:
            WebDriverWait(test.driver, VISIBILITY_WAIT).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")

    for xpath, title, expected_text in subtabs:
        test.click_element(xpath, timeout=CLICK_WAIT, description=title)
        time.sleep(2)
        test.wait_visible_by_xpath(
            f"//*[contains(normalize-space(.), '{expected_text}')]",
            timeout=VISIBILITY_WAIT,
            description=expected_text,
        )
        test.raise_if_error_banner()


def main() -> int:
    driver: Optional[WebDriver] = None
    telegram_token = os.getenv("TEST_TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TEST_TELEGRAM_CHAT_ID")

    base_url = os.getenv("ITRANS_URL", DEFAULT_URL)
    username = os.getenv("ITRANS_LOGIN") or os.getenv("ITRANS_USER") or os.getenv("LOGIN")
    password = os.getenv("ITRANS_PASSWORD") or os.getenv("PASSWORD")

    test_result = TestResult()
    test = ItransTest()

    try:
        if not username or not password:
            raise ValueError("Не заданы учетные данные: ITRANS_LOGIN и ITRANS_PASSWORD")

        driver = build_driver()
        test.driver = driver
        test.wait = WebDriverWait(driver, 10)

        def execute_step(step_number, step_key, action, before=None, after=None):
            t0 = time.time()
            try:
                if before:
                    before()
                action()
                if after:
                    after()
                test_result.add_step(step_key, status="1", duration_seconds=time.time() - t0)
                return True
            except Exception as e:
                err = f"Шаг {step_number} ошибка: {str(e)}"
                ss_b64 = get_screenshot_b64(driver)
                test_result.add_step(step_key, status="0", duration_seconds=time.time() - t0)
                test_result.set_screenshot_b64(ss_b64)
                send_telegram_alert(
                    telegram_token, telegram_chat_id, text=err, screenshot_b64=ss_b64
                )
                test_result.finalize(
                    success=False,
                    message=f"Тест завершился с ошибкой на шаге {step_number}",
                    error=err,
                )
                print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
                return False

        # Шаг 1 — Логин и вход в модуль управления ЖД плечом
        if not execute_step(
            1,
            "step_01_open_and_login",
            lambda: step_01_open_and_login(test, base_url, username, password),
            before=lambda: setattr(test, "check_error_banner", False),
            after=lambda: setattr(test, "check_error_banner", True),
        ):
            return 1

        # Шаг 2 — Модуль управления ЖД плечом
        if not execute_step(
            2,
            "step_02_open_rail_module",
            lambda: step_02_open_rail_module(test),
        ):
            return 1

        # Шаг 3 — Отчеты
        if not execute_step(
            3,
            "step_03_open_reports_module",
            lambda: step_03_open_reports_module(test),
        ):
            return 1

        # Шаг 4 — Справочники
        if not execute_step(
            4,
            "step_04_open_directory_module",
            lambda: step_04_open_directory_module(test),
        ):
            return 1

        # Шаг 5 — Единое окно
        if not execute_step(
            5,
            "step_05_open_unified_window_module",
            lambda: step_05_open_unified_window_module(test),
        ):
            return 1

        test_result.finalize(success=True, message="Тест выполнен успешно", error=None)
        print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        err_text = f"Общая ошибка: {str(e)}"
        ss_b64 = None
        try:
            if driver:
                ss_b64 = get_screenshot_b64(driver)
                test_result.set_screenshot_b64(ss_b64)
        except Exception:
            pass
        send_telegram_alert(telegram_token, telegram_chat_id, text=err_text, screenshot_b64=ss_b64)
        test_result.finalize(success=False, message="Тест завершился с ошибкой", error=err_text)
        print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
        return 1
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())


