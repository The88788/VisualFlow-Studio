import requests
import time

# Укажи тут порт своего API
BASE_URL = "http://127.0.0.1:8000/api/videos"

# Вставь сюда video_id (из твоей базы данных), которое УЖЕ сгенерировано (status=completed)
VIDEO_ID = "ТВОЙ_АЙДИ_ГОТОВОГО_ВИДЕО"

def test_extend():
    print("Testing EXTEND...")
    payload = {
        "prompt": "video continues",
        "continuation_video_id": VIDEO_ID,
        "model": "veo31_fast"
    }
    resp = requests.post(BASE_URL, json=payload)
    print("Extend Response:", resp.json())

def test_insert():
    print("Testing INSERT...")
    payload = {
        "prompt": "собачка в левом углу",
        "insert_video_id": VIDEO_ID,
        "model": "veo31_fast"
    }
    resp = requests.post(BASE_URL, json=payload)
    print("Insert Response:", resp.json())

def test_reshoot():
    print("Testing RESHOOT (orbit_left)...")
    payload = {
        "reshoot_video_id": VIDEO_ID,
        "reshoot_motion": "orbit_left",
        "model": "veo31_fast"
    }
    resp = requests.post(BASE_URL, json=payload)
    print("Reshoot Response:", resp.json())

if __name__ == "__main__":
    if VIDEO_ID == "ТВОЙ_АЙДИ_ГОТОВОГО_ВИДЕО":
        print("Сначала впиши VIDEO_ID готового видео в начале скрипта!")
    else:
        print("Выбери, что тестируем (раскомментируй нужное):")
        # test_extend()
        test_insert()
        # test_reshoot()
