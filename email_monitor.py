import imaplib
import email
from email.header import decode_header
import os
import requests
import re
import time
from datetime import datetime

# ===================== НАСТРОЙКИ =====================
MAIL_SERVER = 'imap.mail.ru'
MAIL_USER = 'it_monitoring@delotech.ru'
MAIL_PASSWORD = 'PkVKc89pje0p5v2pcbFr'

TELEGRAM_BOT_TOKEN = "7605576127:AAG5bufEsRYCZQYcC7FnBLYN7yb9IrPuJkU"
TELEGRAM_CHANNEL_ID = "-1002079738497"


FOLDER_1C = 'INBOX/1C &BC0EQgRABDAEPQ-'
ERROR_KEYWORDS = ["Ошибка: Отсутствует утверждённый Проект технических решений подключения"]
CHECK_INTERVAL = 300  # 5 минут
LAST_N_MESSAGES = 10  # Проверяем только последние 10 писем
NO_MAIL_ALERT_SECONDS = 4200  # Алерт если нет писем > 4200 секунд (1 час 10 минут)
ALERT_CHECK_INTERVAL = 600  # Проверка на алерт каждые 10 минут (600 сек)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SENT_UIDS_FILE = os.path.join(SCRIPT_DIR, 'sent_uids_1c.txt')

# Глобальные переменные для отслеживания времени
last_mail_time = None
last_alert_time = None

# ===================== ФУНКЦИИ =====================

def log(msg, icon=""):
    """Логирование с временной меткой"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {icon} {msg}")

def send_telegram(message):
    """Отправляет сообщение в Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHANNEL_ID, 'text': message}, timeout=10)
        log("Сообщение отправлено в Telegram", "[OK]")
    except Exception as e:
        log(f"Ошибка Telegram: {e}", "[ERROR]")

def decode_header_str(s):
    """Декодирует MIME слова"""
    if not s:
        return ""
    return ''.join(
        word.decode(encoding or 'utf-8', errors='replace') if isinstance(word, bytes) else word
        for word, encoding in decode_header(s)
    )

def read_uids(file_path):
    """Читает список отправленных UID"""
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return set(f.read().splitlines())
        except Exception as e:
            log(f"Ошибка чтения {file_path}: {e}", "[ERROR]")
    return set()

def write_uids(file_path, uids):
    """Записывает список отправленных UID"""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(uids))
    except Exception as e:
        log(f"Ошибка записи {file_path}: {e}", "[ERROR]")

def extract_uid(data):
    """Извлекает UID из ответа сервера"""
    data = data.decode('utf-8', errors='ignore') if isinstance(data, bytes) else data
    match = re.search(r'UID\s+(\d+)', data)
    return match.group(1) if match else None

def get_email_body(msg):
    """Извлекает текст письма"""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    return part.get_payload(decode=True).decode('utf-8', errors='replace')
        else:
            if msg.get_content_type() in ("text/plain", "text/html"):
                return msg.get_payload(decode=True).decode('utf-8', errors='replace')
    except Exception as e:
        log(f"Ошибка чтения письма: {e}", "[ERROR]")
    return "Ошибка при чтении письма"

def has_error_keyword(body):
    """Проверяет наличие ошибок из списка"""
    return any(err.lower() in body.lower() for err in ERROR_KEYWORDS)

def has_large_number(body):
    """Проверяет наличие чисел больше 1000 в основном тексте (без дат/времени)"""
    # Удаляем даты вида DD.MM.YYYY, DD/MM/YYYY
    body_clean = re.sub(r'\d{1,2}[./]\d{1,2}[./]\d{2,4}', '', body)
    
    # Удаляем время вида HH:MM:SS
    body_clean = re.sub(r'\d{1,2}:\d{1,2}:\d{1,2}', '', body_clean)
    
    # Ищем оставшиеся числа
    numbers = re.findall(r'\d+', body_clean)
    
    # Проверяем есть ли число > 1000
    result = any(int(num) > 1000 for num in numbers)
    
    if result:
        log(f"Найдено число > 1000 в тексте", "[INFO]")
    
    return result

def check_mail():
    """Проверяет последние N писем в папке 1С Этран"""
    global last_mail_time
    
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(MAIL_SERVER, timeout=10)
        mail.login(MAIL_USER, MAIL_PASSWORD)
        
        log(f"Проверка '{FOLDER_1C}'...", "[INFO]")
        
        # Подключаемся к папке
        status, data = mail.select(f'"{FOLDER_1C}"')
        if status != 'OK':
            log(f"Не удалось подключиться к папке: {status}", "[ERROR]")
            return False  # Возвращаем False - не удалось проверить
        
        # Получаем количество писем в папке
        try:
            total_msgs = int(data[0])
        except Exception:
            log("Не удалось определить количество писем в папке", "[ERROR]")
            return False
        
        if total_msgs == 0:
            log("В папке нет писем", "[INFO]")
            return False
        
        # Берём только последние N (или меньше, если писем мало)
        start_seq = max(1, total_msgs - LAST_N_MESSAGES + 1)
        
        log(f"Всего в папке {total_msgs} писем, проверяем последние {LAST_N_MESSAGES} (номера {start_seq}–{total_msgs})", "[INFO]")
        
        # Ищем письма по диапазону sequence number (все, не только UNSEEN)
        status, messages = mail.search(None, f"ALL")
        if status != 'OK':
            log("Ошибка поиска писем", "[ERROR]")
            return False
        
        all_mail_ids = messages[0].split()
        
        # Берём только последние LAST_N_MESSAGES
        if len(all_mail_ids) > LAST_N_MESSAGES:
            mail_ids = all_mail_ids[-LAST_N_MESSAGES:]
        else:
            mail_ids = all_mail_ids
        
        log(f"Проверяем {len(mail_ids)} последних писем", "[MAIL]" if len(mail_ids) > 0 else "[INFO]")
        
        sent_uids = read_uids(SENT_UIDS_FILE)
        new_uids = set()
        found_new_mail = False
        
        for mail_id in mail_ids:
            try:
                status, msg_data = mail.fetch(mail_id, '(BODY.PEEK[] UID)')
                if status != 'OK':
                    continue
                
                uid = extract_uid(mail.fetch(mail_id, '(UID)')[1][0])
                if not uid:
                    continue
                
                # Пропускаем уже обработанные письма
                if uid in sent_uids:
                    log(f"UID {uid}: уже обработано, пропускаем", "[INFO]")
                    continue
                
                msg = email.message_from_bytes(msg_data[0][1])
                subject = decode_header_str(msg.get('Subject', ''))
                from_ = decode_header_str(msg.get('From', ''))
                date_ = msg.get('Date', '')
                body = get_email_body(msg)
                
                # Проверяем наличие ошибок или больших чисел
                error_found = has_error_keyword(body)
                large_number_found = has_large_number(body)
                
                if not error_found and not large_number_found:
                    log("Письмо без ошибок и больших чисел, пропускаем", "[INFO]")
                    new_uids.add(uid)
                    found_new_mail = True
                    continue
                
                # Если найдена ошибка или число > 1000 - отправляем письмо целиком
                body_short = body[:4000]
                message = f"Получено новое сообщение:\nОт: {from_}\nТема: {subject}\nДата: {date_}\nТекст:\n\n{body_short}"
                send_telegram(message)
                new_uids.add(uid)
                found_new_mail = True
                log(f"Письмо отправлено: {subject}", "[OK]")
                
                # Помечаем письмо как прочитанное
                mail.store(mail_id, '+FLAGS', '\\Seen')
                
            except Exception as e:
                log(f"Ошибка обработки письма: {e}", "[ERROR]")
        
        sent_uids.update(new_uids)
        write_uids(SENT_UIDS_FILE, sent_uids)
        
        # Если найдено новое письмо (независимо от содержания), обновляем время
        if found_new_mail:
            last_mail_time = datetime.now()
            log(f"Обновлено время последнего письма: {last_mail_time.strftime('%Y-%m-%d %H:%M:%S')}", "[INFO]")
            return True
        
        return False
    
    except Exception as e:
        log(f"Ошибка проверки: {e}", "[ERROR]")
        return False
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass

def check_no_mail_alert():
    """Проверяет отсутствие писем в течение NO_MAIL_ALERT_SECONDS"""
    global last_mail_time, last_alert_time
    
    if last_mail_time is None:
        log("Нет информации о времени последнего письма", "[INFO]")
        return
    
    # Текущее время
    now = datetime.now()
    
    # Считаем сколько секунд прошло
    time_since_last_mail = now - last_mail_time
    seconds_since = time_since_last_mail.total_seconds()
    
    log(f"Времени прошло с последнего письма: {seconds_since:.0f} сек ({seconds_since/60:.1f} мин)", "[INFO]")
    
    # Если прошло больше NO_MAIL_ALERT_SECONDS
    if seconds_since > NO_MAIL_ALERT_SECONDS:
        # Отправляем алерт только если предыдущего не было или он был давно (больше часа назад)
        if last_alert_time is None or (now - last_alert_time).total_seconds() > 3600:
            message = "Перестали поступать письма с ошибками по интеграции ЭТРАН РЖД - 1С: Этран !!!"
            send_telegram(message)
            last_alert_time = datetime.now()
            log("Отправлен алерт об отсутствии писем", "[ALERT]")
        else:
            log("Алерт об отсутствии писем уже отправляли менее часа назад", "[INFO]")

def main():
    """Основной цикл мониторинга"""
    log("=" * 40, "")
    log("Email Monitor запущен (с проверкой на отсутствие писем)", "")
    log(f"Интервал проверки: {CHECK_INTERVAL} сек", "")
    log(f"Алерт если нет писем > {NO_MAIL_ALERT_SECONDS} сек ({NO_MAIL_ALERT_SECONDS / 60:.0f} мин)", "")
    log(f"Аккаунт: {MAIL_USER}", "")
    log("=" * 40, "")
    
    last_alert_check = time.time()
    
    try:
        while True:
            try:
                # Основная проверка писем
                check_mail()
                
                # Проверка на алерт (реже, чтобы не нагружать)
                current_time = time.time()
                if current_time - last_alert_check >= ALERT_CHECK_INTERVAL:
                    check_no_mail_alert()
                    last_alert_check = current_time
                
                log(f"Ожидание {CHECK_INTERVAL} сек до следующей проверки...", "[WAIT]")
                print()
                time.sleep(CHECK_INTERVAL)
            except KeyboardInterrupt:
                log("Скрипт остановлен пользователем", "[STOP]")
                break
            except Exception as e:
                log(f"Ошибка в основном цикле: {e}", "[ERROR]")
                time.sleep(10)
    except KeyboardInterrupt:
        log("Скрипт завершён", "[OK]")

if __name__ == "__main__":
    main()
