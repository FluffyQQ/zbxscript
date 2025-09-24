#!/usr/lib64/zabbix7-lts/externalscripts/myenv/bin/python3
import os
import sys
import json
import time
import logging
import base64
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# Загрузка переменных окружения
env_path = Path(__file__).with_name('_env')
load_dotenv(dotenv_path=env_path)
load_dotenv()

# Конфигурация
URL = 'https://isales.trcont.com'
SCREENSHOTS_DIR = '/opt/screenshots'

# Selenium Grid credentials
SELENIUM_GRID_LOGIN = os.getenv('SELENIUM_GRID_LOGIN')
SELENIUM_GRID_PASSWORD = os.getenv('SELENIUM_GRID_PASSWORD')
SELENIUM_GRID_URL = f"http://{SELENIUM_GRID_LOGIN}:{SELENIUM_GRID_PASSWORD}@172.18.65.116:4444/wd/hub"

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def ensure_screenshots_dir():
    """Создает директорию для скриншотов если она не существует"""
    try:
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        logger.info(f"Директория для скриншотов готова: {SCREENSHOTS_DIR}")
        return True
    except Exception as e:
        logger.error(f"Ошибка при создании директории {SCREENSHOTS_DIR}: {e}")
        return False


def create_driver():
    """Создает WebDriver с настройками для Selenium Grid"""
    chrome_options = Options()
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-web-security')
    chrome_options.add_argument('--disable-features=VizDisplayCompositor')
    
    try:
        driver = webdriver.Remote(
            command_executor=SELENIUM_GRID_URL,
            options=chrome_options
        )
        logger.info("WebDriver создан успешно через Selenium Grid")
        return driver
    except Exception as e:
        logger.error(f"Ошибка при создании WebDriver: {e}")
        raise


def save_screenshot(driver, filename="isales_homepage.png"):
    """Сохраняет скриншот в /opt/screenshots.

    - Если имя файла isales_homepage.png — сохраняет без таймстампа, предварительно удалив старый файл.
    - Для любых других имен добавляет таймстамп (YYYYMMDD-HHMMSS), чтобы не перезаписывать.
    """
    try:
        # Создаем директорию
        if not ensure_screenshots_dir():
            return None
            
        # Создаем скриншот
        screenshot_bytes = driver.get_screenshot_as_png()
        
        # Особый режим для isales_homepage.png: без таймстампа, но с удалением существующего
        if filename == "isales_homepage.png":
            filepath = os.path.join(SCREENSHOTS_DIR, filename)
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    logger.info(f"Удален старый файл: {filepath}")
            except Exception as rm_err:
                logger.warning(f"Не удалось удалить старый файл {filepath}: {rm_err}")
        else:
            # Для прочих имен — добавляем таймстамп (YYYYMMDD-HHMMSS)
            now_str = datetime.now().strftime('%Y%m%d-%H%M%S')
            name, ext = os.path.splitext(filename)
            filename = f"{name}_{now_str}{ext or '.png'}"
            filepath = os.path.join(SCREENSHOTS_DIR, filename)
        
        # Сохраняем файл
        with open(filepath, 'wb') as f:
            f.write(screenshot_bytes)
            
        logger.info(f"Скриншот сохранен: {filepath}")
        return filepath
        
    except Exception as e:
        logger.error(f"Ошибка при сохранении скриншота: {e}")
        return None


def main():
    """Основная функция"""
    driver = None
    
    try:
        # Проверяем учетные данные для Selenium Grid
        if not SELENIUM_GRID_LOGIN or not SELENIUM_GRID_PASSWORD:
            logger.error("Учетные данные Selenium Grid не настроены")
            logger.error("Установите переменные окружения SELENIUM_GRID_LOGIN и SELENIUM_GRID_PASSWORD")
            return 1
            
        logger.info("Запуск скрипта для создания скриншота isales.trcont.com")
        
        # Создаем WebDriver
        driver = create_driver()
        
        # Открываем сайт
        logger.info(f"Открываем сайт: {URL}")
        driver.get(URL)
        
        # Ждем загрузки страницы
        wait = WebDriverWait(driver, 30)
        
        # Ждем появления основного контента (например, элемента с классом Header)
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(@class, 'Header')]")))
            logger.info("Страница загружена успешно")
        except TimeoutException:
            logger.warning("Таймаут ожидания загрузки страницы, но продолжаем")
        
        # Ждем немного для полной загрузки
        time.sleep(3)
        
        # Делаем скриншот (без таймстампа и с удалением старого файла)
        screenshot_path = save_screenshot(driver, "isales_homepage.png")
        
        if screenshot_path:
            result = {
                "success": True,
                "message": "Скриншот создан успешно",
                "screenshot_path": screenshot_path,
                "url": URL,
                "timestamp": datetime.now().isoformat()
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
            logger.info("Скрипт выполнен успешно")
            return 0
        else:
            result = {
                "success": False,
                "message": "Ошибка при сохранении скриншота",
                "url": URL,
                "timestamp": datetime.now().isoformat()
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1
            
    except Exception as e:
        error_msg = f"Критическая ошибка: {str(e)}"
        logger.error(error_msg)
        
        # Пытаемся сделать скриншот ошибки
        if driver:
            try:
                save_screenshot(driver, "isales_error.png")
            except Exception:
                pass
        
        result = {
            "success": False,
            "message": error_msg,
            "url": URL,
            "timestamp": datetime.now().isoformat()
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1
        
    finally:
        # Закрываем браузер
        if driver:
            try:
                driver.quit()
                logger.info("WebDriver закрыт")
            except Exception as e:
                logger.warning(f"Ошибка при закрытии WebDriver: {e}")


if __name__ == "__main__":
    sys.exit(main())
