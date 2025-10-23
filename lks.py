#!/usr/lib64/zabbix7-lts/externalscripts/myenv/bin/python3
import base64
import json
import os
import sys
import time
import warnings
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

# Silence Selenium warning about creds in URL to keep stdout pure JSON
warnings.filterwarnings(
    "ignore",
    message="Embedding username and password in URL could be insecure, use ClientConfig instead",
    category=UserWarning,
    module="selenium.webdriver.remote.remote_connection",
)

# Load env
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv()

# Defaults and configuration
DEFAULT_URL = os.getenv("LKS_URL", "https://itrans.trcont.com/coexec-web")
DEFAULT_WAIT = 10
CLICK_WAIT = 10
VISIBILITY_WAIT = 10
SCREENSHOTS_DIR = '/opt/screenshots'

# Переключатель для локального запуска (как в itrans_zd.py)
USE_SELENIUM_GRID = True  # False — локально, True — через Grid

# Данные Selenium Grid (как в itrans_zd.py)
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
        self.steps[step_name] = {"status": status, "timing": {"duration_seconds": round(duration_seconds, 2)}}
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
        self.test_info = {"total_duration": round(end - self.start_time, 2)}

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "test_info": self.test_info,
            "steps": self.steps,
            "screenshot": self.screenshot,
        }
        # Do not include error field when there is no error to avoid null in Zabbix
        if self.error is not None:
            data["error"] = self.error
        return data


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip()


def build_driver() -> WebDriver:
    chrome_options = Options()
    chrome_options.add_argument("--window-size=1920,1080")
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
                "Ошибка подключения к Selenium Grid: не удалось установить соединение."
            )
    else:
        return webdriver.Chrome(options=chrome_options)


def get_screenshot_b64(driver: WebDriver) -> str:
    png_bytes = driver.get_screenshot_as_png()
    return base64.b64encode(png_bytes).decode("ascii")


def ensure_screenshots_dir() -> None:
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    except Exception:
        pass


def save_screenshot_file(driver: WebDriver, filename: str) -> None:
    try:
        ensure_screenshots_dir()
        filepath = os.path.join(SCREENSHOTS_DIR, filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        with open(filepath, 'wb') as f:
            f.write(driver.get_screenshot_as_png())
    except Exception:
        pass


class LksTest:
    def __init__(self):
        self.driver: Optional[WebDriver] = None
        self.wait: Optional[WebDriverWait] = None

    def wait_element(self, xpath: str, timeout: int = DEFAULT_WAIT):
        try:
            wait = self.wait if timeout == DEFAULT_WAIT and self.wait is not None else WebDriverWait(self.driver, timeout)
            element = wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
            return element
        except TimeoutException:
            raise Exception(f"Элемент не найден: {xpath}")

    def click_element(self, xpath: str, timeout: int = CLICK_WAIT, retries: int = 2):
        for attempt in range(retries):
            try:
                element = self.wait_element(xpath, timeout)
                WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                try:
                    element.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", element)
                return
            except (StaleElementReferenceException, TimeoutException) as e:
                if attempt == retries - 1:
                    raise Exception(f"Не удалось нажать элемент после {retries} попыток: {xpath}. Причина: {e}")
                time.sleep(1)

    def element_is_present(self, xpath: str, timeout: int = DEFAULT_WAIT) -> bool:
        try:
            self.wait_element(xpath, timeout)
            return True
        except (TimeoutException, WebDriverException):
            return False


def send_telegram_alert(
    token: Optional[str],
    chat_id: Optional[str],
    text: str,
    screenshot_b64: Optional[str] = None,
    step_number: Optional[str] = None,
    step_description: Optional[str] = None,
) -> None:
    """Отправка уведомления в Telegram. Не бросает исключения наружу."""
    debug = os.getenv("TELEGRAM_DEBUG", "").lower() in ("1", "true", "yes")
    if not token or not chat_id:
        if debug:
            print("[telegram] пропуск: не задан TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID", file=sys.stderr)
        return

    # Получаем текущую дату и время
    from datetime import datetime
    now = datetime.now()
    date_str = now.strftime('%d.%m.%Y')
    time_str = now.strftime('%H:%M')
    
    # Формируем информацию о шаге
    if step_number and step_description:
        step_info = f"Шаг {step_number} - {step_description}"
    elif step_number:
        step_info = f"Шаг {step_number}"
    elif step_description:
        step_info = step_description
    else:
        step_info = "Шаг N/A"
    
    # Формируем caption в формате как в test_isales.py
    caption = f"""<b>Ошибка в тесте iTrans "ЛК соисполнителя"</b>\n\n<b>Шаг:</b> {step_info}\n<b>Дата:</b> {date_str}\n<b>Время:</b> {time_str}"""

    api_url = f"https://api.telegram.org/bot{token}"
    try:
        if screenshot_b64:
            photo_bytes = base64.b64decode(screenshot_b64)
            files = {"photo": ("error.png", photo_bytes, "image/png")}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            resp = requests.post(f"{api_url}/sendPhoto", data=data, files=files, timeout=20)
        else:
            payload = {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"}
            resp = requests.post(f"{api_url}/sendMessage", json=payload, timeout=20)
        if not resp.ok and debug:
            try:
                body = resp.text
            except Exception:
                body = "<no body>"
            print(f"[telegram] ошибка HTTP {resp.status_code}: {body}", file=sys.stderr)
    except Exception as e:
        if debug:
            print(f"[telegram] исключение при отправке: {e}", file=sys.stderr)
        pass


# Step implementations, ported from work_test.py into the common structure
def step_01_open_and_authenticate(test: LksTest, base_url: str, username: str, password: str) -> None:
    d = test.driver
    d.get(base_url)
    time.sleep(4)
    # Keycloak page expected
    test.wait_element("//input[@id='username']").send_keys(username)
    test.wait_element("//input[@id='password']").send_keys(password)
    test.click_element("//input[@type='submit']")
    # Wait for navigation
    WebDriverWait(test.driver, 10).until(lambda drv: "keycloak" not in (drv.current_url or "").lower())


def step_02_open_journal(test: LksTest) -> None:
    # Expand 'Личный кабинет соисполнителя' if present
    try:
        test.click_element("//button[@data-bs-target='#coexecutor_personal_account-collapse' or @title='Личный кабинет соисполнителя']", timeout=5)
        time.sleep(1)
    except Exception:
        pass
    # Click journal link
    try:
        test.click_element("//div[@id='coexecutor_personal_account-collapse']//a[@href='/coexec-web/claim']", timeout=10)
    except Exception:
        test.click_element("//a[@href='/coexec-web/claim']", timeout=10)
    time.sleep(3)
    if "claim" not in (test.driver.current_url or ""):
        raise Exception("Переход в журнал заявок не выполнен")


def step_03_open_expeditor_report_and_select_rail(test: LksTest) -> None:
    # Open report
    for selector in [
        "//a[@href='/coexec-web/advance_report']",
        "//a[contains(text(), 'Отчет Экспедитора')]",
        "//a[contains(@href, 'advance_report')]",
    ]:
        try:
            test.click_element(selector, timeout=10)
            break
        except Exception:
            continue
    else:
        raise Exception("Ссылка на отчет экспедитора не найдена")
    time.sleep(3)
    # Select executor containing 'rail'
    executor_select = None
    for selector in [
        "//select[@class='form-select']",
        "//select[contains(@id, 'executor')]",
        "//select[contains(@name, 'executor')]",
    ]:
        try:
            executor_select = test.wait_element(selector, timeout=10)
            break
        except Exception:
            continue
    if not executor_select:
        raise Exception("Селектор исполнителей не найден")
    options = executor_select.find_elements(By.TAG_NAME, "option")
    for option in options:
        if "rail" in (option.text or "").lower():
            option.click()
            return
    raise Exception("RAIL исполнитель не найден")


def step_04_open_available_equipment(test: LksTest) -> None:
    equipment_url = "https://itrans.trcont.com/coexec-web/available_equipment"
    test.driver.get(equipment_url)
    time.sleep(3)
    if "available_equipment" not in (test.driver.current_url or ""):
        raise Exception("Переход к доступному оборудованию не выполнен")


def step_05_check_container_filter(test: LksTest) -> None:
    # Try to click filter button by several selectors
    for selector in [
        "//span[contains(@class, 'filter-button') and contains(text(), 'Фильтр')]",
        "//button[contains(text(), 'Фильтр')]",
        "//span[contains(text(), 'Фильтр')]",
        "//*[contains(@class, 'filter-button')]",
    ]:
        try:
            test.click_element(selector, timeout=10)
            break
        except Exception:
            continue
    else:
        raise Exception("Кнопка фильтра не найдена")

    time.sleep(2)
    # Find label 'Тип контейнера'
    label = None
    for selector in ["//label[contains(text(), 'Тип контейнера')]", "//*[contains(text(), 'Тип контейнера')]"]:
        try:
            label = test.driver.find_element(By.XPATH, selector)
            break
        except Exception:
            continue
    if not label:
        raise Exception("Заголовок 'Тип контейнера' не найден")

    # Find checkboxes with 40
    for selector in [
        "//input[@type='checkbox' and contains(@value, '40')]",
        "//input[@type='checkbox' and contains(@id, '40')]",
        "//label[contains(text(), '40')]/input[@type='checkbox']",
    ]:
        checkboxes = test.driver.find_elements(By.XPATH, selector)
        if checkboxes:
            for checkbox in checkboxes:
                checkbox_id = checkbox.get_attribute("id")
                label_text = ""
                if checkbox_id:
                    try:
                        lbl = test.driver.find_element(By.XPATH, f"//label[@for='{checkbox_id}']")
                        label_text = (lbl.text or "").strip()
                    except Exception:
                        pass
                else:
                    try:
                        sibling = checkbox.find_element(By.XPATH, "./following-sibling::*[1]")
                        if sibling.tag_name.lower() == "label":
                            label_text = (sibling.text or "").strip()
                    except Exception:
                        pass
                if "40" in label_text:
                    return
    raise Exception("Типы 40** не обнаружены")


def execute_step_with_retry(step_number, step_key, action, driver, test_result, telegram_token, telegram_chat_id, step_description=None, max_retries=2):
    """Выполняет шаг с повторными попытками и обновлением страницы при ошибках"""
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            action()
            test_result.add_step(step_key, status="1", duration_seconds=time.time() - t0)
            return True
        except Exception as e:
            if attempt < max_retries:
                # Обновляем страницу и повторяем попытку
                try:
                    if driver:
                        driver.refresh()
                        WebDriverWait(driver, DEFAULT_WAIT).until(
                            lambda drv: drv.execute_script("return document.readyState") == "complete"
                        )
                        time.sleep(3)
                except Exception:
                    pass
                continue
            else:
                # Последняя попытка неудачна - завершаем с ошибкой
                err = f"Шаг {step_number} ошибка: {str(e)}"
                ss_b64 = None
                try:
                    ss_b64 = get_screenshot_b64(driver)
                    test_result.set_screenshot_b64(ss_b64)
                    save_screenshot_file(driver, 'lks_error.png')
                except Exception:
                    pass
                test_result.add_step(step_key, status="0", duration_seconds=time.time() - t0)
                send_telegram_alert(telegram_token, telegram_chat_id, text=err, screenshot_b64=ss_b64, step_number=str(step_number), step_description=step_description)
                test_result.finalize(
                    success=False,
                    message=f"Тест завершился с ошибкой на шаге {step_number}",
                    error=err,
                )
                print(json.dumps(test_result.to_dict(), ensure_ascii=False, indent=2))
                return False


def main() -> int:
    driver: Optional[WebDriver] = None
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    base_url = os.getenv("LKS_URL", DEFAULT_URL)
    username = os.getenv("ITRANS_LOGIN") or os.getenv("LOGIN")
    password = os.getenv("ITRANS_PASSWORD") or os.getenv("PASSWORD")

    test_result = TestResult()
    test = LksTest()

    try:
        if not username or not password:
            raise ValueError("Не заданы учетные данные: ITRANS_LOGIN и ITRANS_PASSWORD")

        driver = build_driver()
        test.driver = driver
        test.wait = WebDriverWait(driver, DEFAULT_WAIT)

        # Steps sequence adapted and merged: open site + authenticate combined
        if not execute_step_with_retry(1, "step_01_open_and_authenticate", lambda: step_01_open_and_authenticate(test, base_url, username, password), driver, test_result, telegram_token, telegram_chat_id, step_description="Вход в систему"):
            return 1
        if not execute_step_with_retry(2, "step_02_open_journal", lambda: step_02_open_journal(test), driver, test_result, telegram_token, telegram_chat_id, step_description="Открытие Журнал заявок"):
            return 1
        if not execute_step_with_retry(3, "step_03_open_expeditor_report_and_select_rail", lambda: step_03_open_expeditor_report_and_select_rail(test), driver, test_result, telegram_token, telegram_chat_id, step_description="Открытие Отчет экспедитора"):
            return 1
        if not execute_step_with_retry(4, "step_04_open_available_equipment", lambda: step_04_open_available_equipment(test), driver, test_result, telegram_token, telegram_chat_id, step_description="Доступное оборудование"):
            return 1
        if not execute_step_with_retry(5, "step_05_check_container_filter", lambda: step_05_check_container_filter(test), driver, test_result, telegram_token, telegram_chat_id, step_description="Проверка фильтра контейнеров"):
            return 1

        # Success
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
                save_screenshot_file(driver, 'lks_error.png')
        except Exception:
            pass
        
        # Используем текст ошибки как описание шага
        step_description = str(e)
        send_telegram_alert(telegram_token, telegram_chat_id, text=err_text, screenshot_b64=ss_b64, step_number=None, step_description=step_description)
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


