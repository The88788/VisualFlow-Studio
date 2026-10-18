import subprocess
import os

def remove_watermark(input_video_path, output_video_path):
    """
    Удаляет вотермарку Veo (в правом нижнем углу) путем обрезки (кропа) видео.

    Параметры обрезки подобраны так, чтобы отрезать только нужную часть 
    снизу и справа, где находится логотип "Veo".
    """
    if not os.path.exists(input_video_path):
        print(f"Ошибка: Файл {input_video_path} не найден.")
        return

    # FFmpeg команда для кропа
    # crop=w:h:x:y
    # Мы отрезаем 120 пикселей снизу и 120 пикселей справа
    # in_w - ширина исходного видео
    # in_h - высота исходного видео
    # Размеры кропа: ширина = in_w - 120, высота = in_h - 120
    # Начинаем с верхнего левого угла (x=0, y=0)
    
    crop_filter = "crop=in_w-120:in_h-120:0:0"

    command = [
        "ffmpeg",
        "-i", input_video_path,
        "-vf", crop_filter,
        "-c:a", "copy",       # Копируем аудио без изменений (если есть)
        "-c:v", "libx264",    # Перекодируем видео кодеком H.264
        "-crf", "18",         # Высокое качество (0-51, меньше = лучше, 18 = визуально без потерь)
        "-preset", "fast",    # Скорость кодирования
        "-y",                 # Перезаписывать выходной файл
        output_video_path
    ]

    print(f"Запускаем FFMPEG для обрезки видео: {input_video_path}")
    print(f"Новое видео будет сохранено в: {output_video_path}")
    
    try:
        # Запускаем процесс, перехватываем вывод чтобы не засорять консоль если не нужно
        process = subprocess.run(
            command, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True
        )
        
        if process.returncode == 0:
            print("✅ Успешно! Вотермарка обрезана.")
        else:
            print("❌ Ошибка при выполнении FFMPEG:")
            print(process.stderr)
            
    except FileNotFoundError:
        print("❌ Ошибка: Утилита 'ffmpeg' не найдена в системе.")
        print("Пожалуйста, установите FFmpeg и добавьте его в системный путь (PATH).")

if __name__ == "__main__":
    # --- ТЕСТОВАЯ ЗОНА ---
    
    # Сюда нужно вписать путь к любому твоему тестовому видео с вотермаркой
    input_file = r"C:\Users\никитос\IdeaProjects\veoapi\opt\veoapi2\test.mp4"
    
    # Куда сохранить результат
    output_file = r"C:\Users\никитос\IdeaProjects\veoapi\opt\veoapi2\test_video_no_watermark.mp4"
    
    # Для теста создадим фейковый файл если его нет, чтобы скрипт просто запустился 
    # (В реальности укажи настоящий mp4)
    if not os.path.exists(input_file):
        print(f"⚠️ Тестовый файл {input_file} не существует. Создаю пустой для теста запуска скрипта...")
        with open(input_file, 'w') as f:
            f.write("dummy")

    remove_watermark(input_file, output_file)
