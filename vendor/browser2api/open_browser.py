import asyncio
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# Конфигурация
CDP_PORT = 9225  # Используем другой порт, чтобы не конфликтовать, если Chrome тоже запущен
USER_DATA_DIR = Path.home() / ".browser2api" / "edge_data" / "debug_profile"

def find_edge_path():
    """Ищет путь к Microsoft Edge."""
    system = platform.system()

    if system == "Darwin":  # macOS
        paths = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    elif system == "Windows":
        paths = [
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]
    else:  # Linux
        paths = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/edge",
        ]
        # Проверка через which
        result = shutil.which("microsoft-edge")
        if result:
            return result
        result = shutil.which("microsoft-edge-stable")
        if result:
            return result

    for path in paths:
        if os.path.exists(path):
            return path
    return None

async def main():
    edge_path = find_edge_path()
    if not edge_path:
        print("❌ Microsoft Edge не найден. Установи его или проверь путь.")
        return

    print(f"🚀 Найден Edge: {edge_path}")

    # Создаем директорию профиля
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Аргументы запуска для Edge
    args = [
        edge_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
    ]

    print(f"⚙️  Запуск Edge с портом отладки {CDP_PORT}...")

    # Запуск процесса
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
    except Exception as e:
        print(f"❌ Ошибка запуска Edge: {e}")
        return

    # Даем время на запуск (Edge иногда чуть дольше стартует)
    await asyncio.sleep(3)

    print(f"🔌 Подключение к CDP на порту {CDP_PORT}...")

    async with async_playwright() as p:
        try:
            # Подключаемся к запущенному Edge
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")

            default_context = browser.contexts[0]

            if default_context.pages:
                page = default_context.pages[0]
            else:
                page = await default_context.new_page()

            await page.goto("https://www.google.com")
            print("✅ Edge открыт и управляется через Playwright!")

            input("Нажми Enter, чтобы закрыть браузер и завершить скрипт...\n")

            await browser.close()

        except Exception as e:
            print(f"❌ Ошибка подключения к CDP: {e}")
            print("   Возможно, Edge еще не успел запуститься или порт занят.")

if __name__ == "__main__":
    asyncio.run(main())