import os
import sys
import json
import time
import base64
import traceback
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException, WebDriverException


DEFAULT_URL = "https://itrans.trcont.ru/"

# Базовый CSS-селектор для видимых алертов внутри контейнера ошибок
# Дальше фильтруем по классу/тексту, чтобы определить именно ошибку
ERROR_CONTAINER_VISIBLE_ALERTS_SELECTOR = ".errors-container .alert.show"

# Путь к .env и загрузка переменных, как в test_isales.py
from pathlib import Path
env_path = Path(__file__).with_name('.env')
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
        return webdriver.Remote(command_executor=SELENIUM_GRID_URL, options=chrome_options)
    else:
        return webdriver.Chrome(options=chrome_options)


def wait_for(driver: WebDriver, condition, timeout: int = 30):
    return WebDriverWait(driver, timeout).until(condition)


def get_screenshot_b64(driver: WebDriver) -> str:
    # Keep screenshot only in memory
    png_bytes = driver.get_screenshot_as_png()
    return base64.b64encode(png_bytes).decode("ascii")


def format_exception_full(e: Exception) -> str:
    """Возвращает полный traceback для исключения."""
    try:
        return "".join(traceback.format_exception(type(e), e, e.__traceback__))
    except Exception:
        try:
            return traceback.format_exc()
        except Exception:
            return str(e)


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

    def wait_element(self, xpath: str, timeout: int = 10):
        try:
            wait = self.wait if timeout == 10 and self.wait is not None else WebDriverWait(self.driver, timeout)
            element = wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
            # Проверяем, не появился ли баннер ошибки
            self.raise_if_error_banner()
            return element
        except TimeoutException:
            # В первую очередь проверим, не появился ли баннер ошибки
            self.raise_if_error_banner()
            raise Exception(f"Element not found: {xpath}")

    def click_element(self, xpath: str, timeout: int = 10, retries: int = 2):
        for attempt in range(retries):
            try:
                element = self.wait_element(xpath, timeout)
                WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                try:
                    element.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", element)
                # Проверяем, не появился ли баннер ошибки сразу после клика
                self.raise_if_error_banner()
                return
            except (StaleElementReferenceException, TimeoutException) as e:
                if attempt == retries - 1:
                    raise Exception(f"Failed to click element after {retries} attempts: {xpath}. Cause: {e}")
                time.sleep(1)

    def element_is_present(self, xpath: str, timeout: int = 5) -> bool:
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


def send_telegram_alert(token: Optional[str], chat_id: Optional[str], text: str, screenshot_b64: Optional[str] = None) -> None:
    if not token or not chat_id:
        return
    api_url = f"https://api.telegram.org/bot{token}"
    try:
        if screenshot_b64:
            # Send as photo with caption
            photo_bytes = base64.b64decode(screenshot_b64)
            files = {"photo": ("error.png", photo_bytes, "image/png")}
            data = {"chat_id": chat_id, "caption": text}
            requests.post(f"{api_url}/sendPhoto", data=data, files=files, timeout=15)
        else:
            requests.post(f"{api_url}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception:
        # Avoid breaking the run due to alert failures
        pass


def step_open_and_login(test: ItransTest, base_url: str, username: str, password: str) -> None:
    d = test.driver
    d.get(base_url)
    time.sleep(3)
    # Нажатие кнопки "Модуль планирования и координации"
    test.click_element("//*[@id='app']/div/div[3]/a[1]/span", timeout=30)
    # Ввод логина/пароля по фиксированным XPath, как в test_isales.py
    test.wait_element("//input[@id='username']", timeout=20).send_keys(username)
    test.wait_element("//input[@id='password']", timeout=20).send_keys(password)
    # Сабмит
    test.click_element("//input[@type='submit']", timeout=15)
    # Проверка успешного входа — наличие чего-то после логина
    WebDriverWait(d, 30).until(
        EC.presence_of_element_located((By.XPATH, "//*[contains(normalize-space(.), 'Модуль координатора')]"))
    )


def step_open_coordinator_module_and_verify_tabs(test: ItransTest) -> None:
    # Если есть кнопка "Обновить" — нажать
    try:
        test.click_element("//*[@id='app']/div[4]/div/div[2]/a[1]", timeout=5)
        time.sleep(1)
    except Exception:
        pass
    
    # Нажать кнопку для раскрытия меню
    test.click_element("//*[@id='app']/div[4]/div/div[1]/span", timeout=10)
    time.sleep(5)

    # Проверяем наличие вкладки "Модуль координатора"
    coordinator_xpath = "//*[@id='coordinator']/button/span"
    if not test.element_is_present(coordinator_xpath, timeout=10):
        raise Exception("Вкладка 'Модуль координатора' не найдена на странице")

    # Открыть вкладку "Модуль координатора"
    test.click_element(coordinator_xpath, timeout=30)
    time.sleep(2)
    
    # Проверяем наличие подвкладок после открытия, ожидая видимость по тексту ссылки
    subtabs_to_check = [
        ("//*[@id='coordinator-collapse']/ul/li[2]/a", "Монитор заказов"),
    ]
    missing_tabs = []
    for _xpath, tab_name in subtabs_to_check:
        try:
            WebDriverWait(test.driver, 5).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")
    
    # Открыть вкладку "Монитор заказов"
    test.click_element("//*[@id='coordinator-collapse']/ul/li[2]/a", timeout=30)
    time.sleep(2)


def step_open_sales_module_and_verify_tabs(test: ItransTest) -> None:
    # Проверяем наличие вкладки "Регулировка"
    sales_tab_xpath = "//*[@id='sales']/button/span"
    if not test.element_is_present(sales_tab_xpath, timeout=10):
        raise Exception("Вкладка 'Регулировка' не найдена на странице")

    # Открыть вкладку
    test.click_element(sales_tab_xpath, timeout=30)
    time.sleep(2)

    # Проверяем наличие подвкладок, ожидая видимость по тексту ссылки
    subtabs_to_check = [
        ("//*[@id='sales-collapse']/ul/li[1]/a", "Журнал регулировочных заказов"),
        ("//*[@id='sales-collapse']/ul/li[2]/a", "Создать заказ на регулировку"),
    ]
    missing_tabs = []
    for _xpath, tab_name in subtabs_to_check:
        try:
            WebDriverWait(test.driver, 5).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")

    # Открыть подвкладки по очереди
    test.click_element("//*[@id='sales-collapse']/ul/li[1]/a", timeout=30)
    time.sleep(2)
    # Открыть вкладку 'Регулировка'
    test.click_element("//*[@id='root']/main/div[2]/aside/div[1]/div[4]/button/div[1]", timeout=30)
    time.sleep(2)
    test.click_element("//*[@id='root']/main/div[2]/aside/div[1]/div[4]/a[2]", timeout=30)
    time.sleep(2)


def step_open_analytics_module_and_verify_tabs(test: ItransTest) -> None:
    # Проверяем наличие вкладки "Аналитика"
    analytics_tab_xpath = "//*[@id='analitycs']/button/span"
    if not test.element_is_present(analytics_tab_xpath, timeout=10):
        raise Exception("Вкладка 'Аналитика' не найдена на странице")

    # Открыть вкладку
    test.click_element(analytics_tab_xpath, timeout=30)
    time.sleep(2)

    # Проверяем наличие подвкладок, ожидая видимость по тексту ссылки
    subtabs_to_check = [
        ("//*[@id='analitycs-collapse']/ul/li[1]/a", "Отчеты"),
    ]
    missing_tabs = []
    for _xpath, tab_name in subtabs_to_check:
        try:
            WebDriverWait(test.driver, 5).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")

    # Открыть подвкладки по очереди
    test.click_element("//*[@id='analitycs-collapse']/ul/li[1]/a", timeout=30)
    time.sleep(2)


 


def step_open_resources_module_and_verify_tabs(test: ItransTest) -> None:
    # Проверяем наличие вкладки "Модуль управления ресурсами"
    resources_tab_xpath = "//*[@id='resources']/button/span"
    if not test.element_is_present(resources_tab_xpath, timeout=10):
        raise Exception("Вкладка 'Модуль управления ресурсами' не найдена на странице")

    # Открыть вкладку
    test.click_element(resources_tab_xpath, timeout=30)
    time.sleep(2)

    # Проверяем наличие подвкладок, ожидая видимость по тексту ссылки
    subtabs_to_check = [
        ("//*[@id='resources-collapse']/ul/li[1]/a", "Контейнеры без заказа"),
        ("//*[@id='resources-collapse']/ul/li[2]/a", "События дислокации"),
        ("//*[@id='resources-collapse']/ul/li[3]/a", "Справочник текущей дислокации ресурсов"),
    ]
    missing_tabs = []
    for _xpath, tab_name in subtabs_to_check:
        try:
            WebDriverWait(test.driver, 5).until(
                EC.visibility_of_element_located((By.LINK_TEXT, tab_name))
            )
            test.raise_if_error_banner()
        except TimeoutException:
            missing_tabs.append(tab_name)
    if missing_tabs:
        raise Exception(f"Отсутствуют подвкладки: {', '.join(missing_tabs)}")

    # Открыть подвкладки по очереди
    test.click_element("//*[@id='resources-collapse']/ul/li[1]/a", timeout=30)
    time.sleep(2)
    test.click_element("//*[@id='resources-collapse']/ul/li[2]/a", timeout=30)
    time.sleep(2)
    test.click_element("//*[@id='resources-collapse']/ul/li[3]/a", timeout=30)
    time.sleep(2)
    

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
        # Шаг 1
        s1_t0 = time.time()
        try:
            # Во время шага 1 отключаем проверку баннера ошибки
            test.check_error_banner = False
            step_open_and_login(test, base_url, username, password)
            # После успешного логина включаем обратно
            test.check_error_banner = True
            test_result.add_step("test_01_open_and_login", status="1", duration_seconds=time.time() - s1_t0)
        except Exception as e:
            err = f"Шаг 1 ошибка: {format_exception_full(e)}"
            ss_b64 = get_screenshot_b64(driver)
            test_result.add_step("test_01_open_and_login", status="0", duration_seconds=time.time() - s1_t0)
            test_result.set_screenshot_b64(ss_b64)
            send_telegram_alert(telegram_token, telegram_chat_id, text=err, screenshot_b64=ss_b64)
            test_result.finalize(success=False, message="Тест завершился с ошибкой на шаге 1", error=err)
            print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
            return 1

        # Шаг 2
        s2_t0 = time.time()
        try:
            step_open_coordinator_module_and_verify_tabs(test)
            test_result.add_step("test_02_open_coordinator_module", status="1", duration_seconds=time.time() - s2_t0)
        except Exception as e:
            err = f"Шаг 2 ошибка: {format_exception_full(e)}"
            ss_b64 = get_screenshot_b64(driver)
            test_result.add_step("test_02_open_coordinator_module", status="0", duration_seconds=time.time() - s2_t0)
            test_result.set_screenshot_b64(ss_b64)
            send_telegram_alert(telegram_token, telegram_chat_id, text=err, screenshot_b64=ss_b64)
            test_result.finalize(success=False, message="Тест завершился с ошибкой на шаге 2", error=err)
            print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
            return 1

        # Шаг 3 — Аналитика
        s4_t0 = time.time()
        try:
            step_open_analytics_module_and_verify_tabs(test)
            test_result.add_step("test_03_open_analytics_module", status="1", duration_seconds=time.time() - s4_t0)
        except Exception as e:
            err = f"Шаг 3 ошибка: {format_exception_full(e)}"
            ss_b64 = get_screenshot_b64(driver)
            test_result.add_step("test_03_open_analytics_module", status="0", duration_seconds=time.time() - s4_t0)
            test_result.set_screenshot_b64(ss_b64)
            send_telegram_alert(telegram_token, telegram_chat_id, text=err, screenshot_b64=ss_b64)
            test_result.finalize(success=False, message="Тест завершился с ошибкой на шаге 3", error=err)
            print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
            return 1

        # (Удалён шаг Администрирование)

        # Шаг 4 — Модуль управления ресурсами
        s6_t0 = time.time()
        try:
            step_open_resources_module_and_verify_tabs(test)
            test_result.add_step("test_04_open_resources_module", status="1", duration_seconds=time.time() - s6_t0)
        except Exception as e:
            err = f"Шаг 4 ошибка: {format_exception_full(e)}"
            ss_b64 = get_screenshot_b64(driver)
            test_result.add_step("test_04_open_resources_module", status="0", duration_seconds=time.time() - s6_t0)
            test_result.set_screenshot_b64(ss_b64)
            send_telegram_alert(telegram_token, telegram_chat_id, text=err, screenshot_b64=ss_b64)
            test_result.finalize(success=False, message="Тест завершился с ошибкой на шаге 4", error=err)
            print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
            return 1

        # Шаг 5 — Регулировка
        s3_t0 = time.time()
        try:
            step_open_sales_module_and_verify_tabs(test)
            test_result.add_step("test_05_open_sales_module", status="1", duration_seconds=time.time() - s3_t0)
        except Exception as e:
            err = f"Шаг 5 ошибка: {format_exception_full(e)}"
            ss_b64 = get_screenshot_b64(driver)
            test_result.add_step("test_05_open_sales_module", status="0", duration_seconds=time.time() - s3_t0)
            test_result.set_screenshot_b64(ss_b64)
            send_telegram_alert(telegram_token, telegram_chat_id, text=err, screenshot_b64=ss_b64)
            test_result.finalize(success=False, message="Тест завершился с ошибкой на шаге 5", error=err)
            print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
            return 1

        # Успех
        test_result.finalize(success=True, message="Тест выполнен успешно", error=None)
        print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    except Exception as e:
        # Общая критическая ошибка
        err_text = f"Критическая ошибка: {format_exception_full(e)}"
        ss_b64 = None
        try:
            if driver:
                ss_b64 = get_screenshot_b64(driver)
                test_result.set_screenshot_b64(ss_b64)
        except Exception:
            pass
        send_telegram_alert(telegram_token, telegram_chat_id, text=err_text, screenshot_b64=ss_b64)
        test_result.finalize(success=False, message="Тест завершился с критической ошибкой", error=err_text)
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


