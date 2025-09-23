import os
import sys
import traceback
import time
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def main() -> int:
    # Load .env next to this script and from environment
    env_path = Path(__file__).with_name('.env')
    load_dotenv(dotenv_path=env_path)
    load_dotenv()

    grid_login = os.getenv('SELENIUM_GRID_LOGIN')
    grid_password = os.getenv('SELENIUM_GRID_PASSWORD')
    # Allow override of full URL; fallback to fixed host as in itrans.py
    grid_url = os.getenv('SELENIUM_GRID_URL')
    if not grid_url:
        grid_url = f"http://{grid_login}:{grid_password}@91.188.214.7:4444/wd/hub"

    print(f"Using GRID URL: {grid_url}")

    try:
        options = Options()
        options.add_argument('--window-size=1920,1080')

        driver = webdriver.Remote(command_executor=grid_url, options=options)
        try:
            driver.set_page_load_timeout(30)
            driver.get('https://ya.ru')
            title = driver.title
            time.sleep(30)
            print(f"Opened https://ya.ru successfully. Title: {title}")
            return 0
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    except Exception as e:
        print("Failed to connect/open via Selenium Grid:")
        print(str(e))
        print(traceback.format_exc())
        return 1


if __name__ == '__main__':
    sys.exit(main())


