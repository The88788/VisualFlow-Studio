"""Cleanup script - run before deployment to remove unnecessary files."""
import shutil
import os
from pathlib import Path

# Files and directories to remove
TO_REMOVE = [
    "test_proxy.py",
    "veoapi2.iml",
    ".idea",
    "__pycache__",
    "logs",
    ".claude",
    "data",
    "src/__pycache__",
    "src/api/__pycache__",
    "src/core/__pycache__",
    "src/services/__pycache__",
]

def cleanup():
    base_dir = Path(__file__).parent
    removed = []
    
    for item in TO_REMOVE:
        path = base_dir / item
        if path.exists():
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed.append(str(item))
                print(f"✓ Удалено: {item}")
            except Exception as e:
                print(f"✗ Ошибка при удалении {item}: {e}")
        else:
            print(f"- Не найдено: {item}")
    
    # Try to remove 'nul' file (Windows artifact)
    nul_path = base_dir / "nul"
    try:
        if nul_path.exists():
            nul_path.unlink()
            print("✓ Удалено: nul")
    except:
        print("- nul пропущен (системный файл Windows)")
    
    print(f"\n✨ Cleanup завершён! Удалено: {len(removed)} элементов")

if __name__ == "__main__":
    print("🧹 VeoAPI2 Cleanup Script\n")
    cleanup()
